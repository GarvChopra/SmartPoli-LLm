from r2.agent import Agent
from r2.brain import OllamaBrain
from r2.cli import build_agent
from r2.config import Settings


def test_build_agent_wires_ollama_brain_and_default_tools(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", data_dir=tmp_path, model="qwen2.5:3b", max_steps=5)
    agent = build_agent(settings, approver=lambda call: False)
    assert isinstance(agent, Agent)
    assert isinstance(agent.brain, OllamaBrain)
    assert agent.max_steps == 5
    names = {s["function"]["name"] for s in agent.registry.schemas()}
    assert {"list_dir", "read_file", "remember"} <= names


def test_build_agent_uses_fast_chat_routing_when_models_differ(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", data_dir=tmp_path, model="qwen2.5:7b", chat_model="qwen2.5:3b")
    agent = build_agent(settings, approver=lambda call: False)
    assert agent.smart_routing is True and isinstance(agent.chat_brain, OllamaBrain)
    same = build_agent(Settings(db_path=tmp_path / "u.db", data_dir=tmp_path, model="qwen2.5:3b", chat_model="qwen2.5:3b"), lambda c: False)
    assert same.chat_brain is None and same.smart_routing is True
