from __future__ import annotations

import json
import queue
import threading
from typing import Callable, Protocol

import httpx

from r2.config import Settings
from r2.privacy import EgressLedger, Gateway
from r2.types import Decision, ToolCall


class BrainError(Exception):
    pass


class Cancelled(Exception):
    """Raised by a brain when the user pressed Stop while it was working."""


class Brain(Protocol):
    def decide(
        self,
        messages: list[dict],
        tools: list[dict],
        on_token: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Decision: ...


class FakeBrain:
    """Plays back a scripted list of Decisions. Used in tests."""

    def __init__(self, script: list[Decision]):
        self._script = list(script)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    def decide(self, messages, tools, on_token=None, should_stop=None) -> Decision:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools))
        decision = self._script.pop(0) if self._script else Decision(final="(script exhausted)")
        if on_token and decision.final:
            on_token(decision.final)
        return decision


class OllamaBrain:
    """Talks to a local Ollama server. Replies are streamed so words can appear straight
    away, and the request runs on a helper thread so Stop can abandon it instantly."""

    def __init__(
        self,
        url: str,
        model: str,
        client: httpx.Client | None = None,
        keep_alive: str = "30m",
        num_ctx: int | None = None,
        num_predict: int | None = None,
        num_gpu: int | None = None,
    ):
        self._url = url.rstrip("/")
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(300, connect=5))
        self._keep_alive = keep_alive
        self._options = {
            k: v for k, v in (("num_ctx", num_ctx), ("num_predict", num_predict), ("num_gpu", num_gpu))
            if v is not None
        }

    def decide(self, messages, tools, on_token=None, should_stop=None) -> Decision:
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "keep_alive": self._keep_alive,
        }
        if tools:
            payload["tools"] = tools
        if self._options:
            payload["options"] = self._options

        events: queue.Queue = queue.Queue()
        holder: dict = {}

        def worker() -> None:
            try:
                with self._client.stream("POST", f"{self._url}/api/chat", json=payload) as resp:
                    holder["resp"] = resp
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if line:
                            events.put(("chunk", json.loads(line)))
                events.put(("done", None))
            except Exception as exc:  # reported to the caller below
                events.put(("error", exc))

        threading.Thread(target=worker, name="r2-ollama-request", daemon=True).start()

        def abandon() -> None:
            resp = holder.get("resp")
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        pieces: list[str] = []
        tool_calls: list = []
        while True:
            try:
                kind, data = events.get(timeout=0.1)
            except queue.Empty:
                if should_stop and should_stop():
                    abandon()
                    raise Cancelled()
                continue
            if kind == "error":
                if isinstance(data, httpx.HTTPError):
                    raise BrainError(f"Could not get an answer from Ollama at {self._url}: {data}") from data
                raise BrainError(f"Unexpected reply from Ollama: {data!r}") from data
            if kind == "done":
                break
            try:
                message = data.get("message", {})
                piece = message.get("content") or ""
                tool_calls.extend(message.get("tool_calls") or [])
            except AttributeError as exc:
                raise BrainError(f"Unexpected reply from Ollama: {exc!r}") from exc
            if piece:
                pieces.append(piece)
                if on_token:
                    on_token(piece)
            if should_stop and should_stop():
                abandon()
                raise Cancelled()
        try:
            return self._parse({"message": {"content": "".join(pieces), "tool_calls": tool_calls}})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BrainError(f"Unexpected reply from Ollama: {exc!r}") from exc

    @staticmethod
    def _parse(body: dict) -> Decision:
        message = body.get("message", {})
        calls = message.get("tool_calls") or []
        if calls:
            fn = calls[0]["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return Decision(tool_call=ToolCall(fn["name"], args))
        return Decision(final=message.get("content", ""))


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert Ollama-style chat messages into (system, Anthropic messages)."""
    system_parts: list[str] = []
    out: list[dict] = []
    pending_ids: list[str] = []
    counter = 0
    for m in messages:
        role = m["role"]
        if role == "system":
            system_parts.append(m["content"])
        elif role == "user":
            if m["content"]:
                out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for call in calls:
                    counter += 1
                    tool_id = f"toolu_{counter:04d}"
                    pending_ids.append(tool_id)
                    fn = call["function"]
                    blocks.append(
                        {"type": "tool_use", "id": tool_id, "name": fn["name"], "input": fn.get("arguments") or {}}
                    )
                out.append({"role": "assistant", "content": blocks})
            elif m["content"]:
                out.append({"role": "assistant", "content": m["content"]})
        elif role == "tool":
            tool_id = pending_ids.pop(0) if pending_ids else f"toolu_{counter:04d}"
            out.append(
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": m["content"]}]}
            )
    while out and out[0]["role"] != "user":
        out.pop(0)  # the API requires the conversation to start with a user turn
    return "\n".join(system_parts), out


class CloudBrain:
    """Anthropic Messages API over plain HTTP. Used only when R2_BRAIN=cloud."""

    hosted = True

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None):
        self._key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=120)

    def decide(self, messages, tools, on_token=None, should_stop=None) -> Decision:
        system, converted = to_anthropic_messages(messages)
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": converted,
            "tools": [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ],
        }
        if system:
            payload["system"] = system
        try:
            resp = self._client.post(
                self.URL,
                json=payload,
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrainError(f"Cloud model request failed: {type(exc).__name__}: {exc}") from exc
        try:
            blocks = resp.json()["content"]
            for block in blocks:
                if block["type"] == "tool_use":
                    return Decision(tool_call=ToolCall(block["name"], block.get("input") or {}))
            text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
            if on_token and text:
                on_token(text)
            return Decision(final=text)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BrainError(f"Unexpected reply from the cloud model: {exc!r}") from exc


def ledger_path(settings: Settings):
    return settings.data_dir / "egress.jsonl"


def ollama_up(url: str) -> bool:
    try:
        return httpx.get(url.rstrip("/") + "/api/tags", timeout=0.4).status_code == 200
    except Exception:
        return False


def _local_brain(settings: Settings) -> "OllamaBrain":
    return OllamaBrain(
        settings.ollama_url, settings.model, keep_alive=settings.keep_alive, num_ctx=4096, num_gpu=settings.gpu_layers
    )


def _hosted(settings: Settings, model: str, ledger: EgressLedger) -> "OpenAIBrain":
    return OpenAIBrain(settings.openai_base_url, settings.openai_api_key, model, gateway=Gateway(ledger))


def make_brain(settings: Settings) -> Brain:
    if settings.brain == "openai":
        if not (settings.openai_base_url and settings.openai_api_key and settings.openai_model):
            raise BrainError("R2_BRAIN=openai needs R2_OPENAI_BASE_URL, R2_OPENAI_API_KEY and R2_OPENAI_MODEL in .env (run: r2 cloud)")
        from r2.tiers import TieredBrain

        ledger = EgressLedger(ledger_path(settings))
        act = _hosted(settings, settings.openai_model, ledger)
        deep = _hosted(settings, settings.deep_model, ledger) if settings.deep_model else None
        # The local model is used only while Ollama is actually running (checked per request);
        # otherwise secrets are masked and sent.
        return TieredBrain(
            act, deep=deep, local=_local_brain(settings), private_local=settings.private_local, ledger=ledger,
            names={"act": settings.openai_model, "deep": settings.deep_model, "local": settings.model},
            local_check=lambda: ollama_up(settings.ollama_url),
        )
    if settings.brain == "cloud":
        if not settings.anthropic_api_key:
            raise BrainError("R2_BRAIN=cloud but ANTHROPIC_API_KEY is not set")
        return CloudBrain(settings.anthropic_api_key, settings.cloud_model)
    return _local_brain(settings)


def make_chat_brain(settings: Settings) -> Brain | None:
    """A small, fast model for plain conversation (no tools). None when not applicable."""
    if settings.brain == "openai" and settings.fast_model:
        from r2.tiers import TieredBrain

        ledger = EgressLedger(ledger_path(settings))
        local_chat = OllamaBrain(
            settings.ollama_url, settings.chat_model or settings.model, keep_alive=settings.keep_alive,
            num_ctx=2048, num_predict=300, num_gpu=settings.gpu_layers,
        )
        # Chat obeys the same privacy routing as tasks: a message with a secret in it stays on this PC.
        return TieredBrain(
            _hosted(settings, settings.fast_model, ledger), local=local_chat,
            private_local=settings.private_local, ledger=ledger,
            names={"act": settings.fast_model, "local": settings.chat_model or settings.model},
            local_check=lambda: ollama_up(settings.ollama_url),
        )
    if settings.brain not in ("ollama", "openai") or not settings.chat_model:
        return None
    if settings.brain == "ollama" and settings.chat_model == settings.model:
        return None
    return OllamaBrain(
        settings.ollama_url,
        settings.chat_model,
        keep_alive=settings.keep_alive,
        num_ctx=2048,
        num_predict=300,
        num_gpu=settings.gpu_layers,
    )


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Convert R2's chat messages to the OpenAI chat format (adds tool-call ids and JSON arguments)."""
    out: list[dict] = []
    pending: list[tuple[str, str]] = []
    counter = 0
    for m in messages:
        role = m["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": m["content"]})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                tool_calls = []
                for call in calls:
                    counter += 1
                    call_id = f"call_{counter:04d}"
                    fn = call["function"]
                    pending.append((call_id, fn["name"]))
                    args = fn.get("arguments") or {}
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": fn["name"], "arguments": args if isinstance(args, str) else json.dumps(args)},
                        }
                    )
                out.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            elif m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
        elif role == "tool":
            call_id, name = pending.pop(0) if pending else (f"call_{counter:04d}", "tool")
            out.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": m["content"]})
    return out


class OpenAIBrain:
    """Any OpenAI-compatible hosted model (Groq, Google Gemini, OpenRouter, ...). Used only when the
    user has set R2_BRAIN=openai and supplied their own key; the key never appears in errors or logs."""

    hosted = True  # text leaves the PC (masked by the gateway)

    def __init__(self, base_url: str, api_key: str, model: str, client: httpx.Client | None = None, gateway=None):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(120, connect=10))
        self._gateway = gateway  # r2.privacy.Gateway: masks sensitive text before it leaves, logs what was sent

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def decide(self, messages, tools, on_token=None, should_stop=None) -> Decision:
        prepared = self._gateway.prepare(messages) if self._gateway else None
        payload = {"model": self._model, "messages": to_openai_messages(prepared.messages if prepared else messages)}
        if tools:
            payload["tools"] = tools
        box: dict = {}
        done = threading.Event()

        def worker() -> None:
            try:
                box["resp"] = self._client.post(
                    f"{self._base}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            except Exception as exc:  # reported below, without anything secret
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="r2-cloud-request", daemon=True).start()
        while not done.wait(0.1):
            if should_stop and should_stop():
                raise Cancelled()  # the request is abandoned; the thread finishes on its own
        sent = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if "error" in box:
            exc = box["error"]
            if prepared:
                self._gateway.record(prepared, self._base, self._model, sent, "failed")
            raise BrainError(self._scrub(f"Cloud model request failed: {type(exc).__name__}: {exc}")) from None
        resp = box["resp"]
        if prepared:
            self._gateway.record(prepared, self._base, self._model, sent, f"HTTP {resp.status_code}")
        if resp.status_code == 429:
            raise BrainError("The cloud provider says you hit its rate limit (free tiers are limited). Wait a minute and try again.")
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))[:200]
            except (ValueError, AttributeError):
                detail = resp.text[:200]
            raise BrainError(self._scrub(f"Cloud model error {resp.status_code}: {detail}"))
        try:
            message = resp.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if calls:
                fn = calls[0]["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if prepared:
                    args = self._gateway.restore(args, prepared)
                return Decision(tool_call=ToolCall(fn["name"], args))
            text = message.get("content") or ""
            if prepared:
                text = self._gateway.restore(text, prepared)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise BrainError(f"Unexpected reply from the cloud model: {exc!r}") from None
        if on_token and text:
            on_token(text)
        return Decision(final=text)
