import time
import pytest

from r2.memory import Memory, SecretRejected, redact


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "t.db")


def test_messages_come_back_oldest_first_and_limited(mem):
    for i in range(5):
        mem.add_message("user", f"m{i}")
    got = mem.recent_messages(3)
    assert [m["content"] for m in got] == ["m2", "m3", "m4"]
    assert got[0]["role"] == "user"


def test_facts_save_list_delete(mem):
    fid = mem.save_fact("my sister is Anya", "user")
    assert [f.text for f in mem.list_facts()] == ["my sister is Anya"]
    assert mem.delete_fact(fid) is True
    assert mem.delete_fact(fid) is False
    assert mem.list_facts() == []


def test_search_ranks_by_word_overlap_then_recency(mem):
    mem.save_fact("my sister is Anya", "user")
    mem.save_fact("my sister lives in Pune", "user")
    mem.save_fact("I like tea", "user")
    hits = mem.search_facts("where does my sister live in Pune")
    assert [f.text for f in hits][0] == "my sister lives in Pune"
    assert all("tea" not in f.text for f in hits)


def test_search_ignores_short_words_and_empty_queries(mem):
    mem.save_fact("I am ok", "user")
    assert mem.search_facts("am is ok") == []
    assert mem.search_facts("") == []


@pytest.mark.parametrize(
    "text",
    [
        "my card is 4111 1111 1111 1111",
        "card 4111-1111-1111-1111",
        "my password is hunter2",
        "password: hunter2",
        "pin = 1234",
    ],
)
def test_save_fact_rejects_secrets(mem, text):
    with pytest.raises(SecretRejected):
        mem.save_fact(text, "user")
    assert mem.list_facts() == []


def test_ordinary_text_is_not_rejected(mem):
    mem.save_fact("I need to reset my password tomorrow", "user")
    mem.save_fact("please pin the window", "user")
    assert len(mem.list_facts()) == 2


def test_secrets_typed_in_chat_are_redacted_in_history(mem):
    mem.add_message("user", "pay with 4111 1111 1111 1111 and my password is hunter2")
    stored = mem.recent_messages(1)[0]["content"]
    assert "4111" not in stored
    assert "hunter2" not in stored
    assert "[redacted]" in stored


def test_redact_leaves_clean_text_alone():
    assert redact("open notepad") == "open notepad"


def test_action_log_is_newest_first_and_redacts(mem):
    mem.log_action("echo", {"text": "hi"}, "ok", "auto")
    mem.log_action("type_text", {"text": "password is hunter2"}, "typed", "approved")
    log = mem.list_actions()
    assert [a["tool"] for a in log] == ["type_text", "echo"]
    assert log[0]["approval"] == "approved"
    assert "hunter2" not in log[0]["args"]


def test_memory_persists_across_instances(tmp_path):
    path = tmp_path / "t.db"
    Memory(path).save_fact("my name is Garv", "user")
    assert Memory(path).list_facts()[0].text == "my name is Garv"


def test_memory_is_safe_under_concurrent_writers(mem):
    import threading

    def worker(n):
        for i in range(20):
            mem.add_message("user", f"{n}-{i}")
            mem.log_action("t", {"i": i}, "ok", "auto")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(mem.recent_messages(10000)) == 120
    assert len(mem.list_actions(10000)) == 120


# ---------- learning from the user's own history ----------
def _ask(mem, text, tools=(), answer="ok"):
    mem.add_message("user", text)
    for t in tools:
        mem.log_action(t, {}, "ok", "auto")
    mem.add_message("assistant", answer)
    time.sleep(0.01)


def test_similar_requests_find_past_phrasing_and_the_tools_used(mem):
    _ask(mem, "open spotify and play some lofi music", tools=["open_app", "type_text"])
    _ask(mem, "what is the capital of france")
    _ask(mem, "find my tax pdf in downloads", tools=["find_files"])
    hits = mem.similar_requests("open spotify and play lofi")
    assert [h["request"] for h in hits] == ["open spotify and play some lofi music"]
    assert hits[0]["tools"] == ["open_app", "type_text"]


def test_similar_requests_ignore_unrelated_or_empty_queries(mem):
    _ask(mem, "find my tax pdf in downloads", tools=["find_files"])
    assert mem.similar_requests("what is the weather") == []
    assert mem.similar_requests("") == []
    assert mem.similar_requests("the and for") == []


def test_similar_requests_are_deduplicated_limited_and_newest_first(mem):
    for _ in range(3):
        _ask(mem, "open notepad and write my todo list", tools=["open_app"])
    _ask(mem, "open notepad and write my shopping list", tools=["open_app", "type_text"])
    _ask(mem, "open notepad and write meeting notes", tools=["open_app"])
    hits = mem.similar_requests("open notepad and write a list", limit=2)
    # best matches first (4 shared words each), the newest of equals first; duplicates collapsed
    assert [h["request"] for h in hits] == [
        "open notepad and write my shopping list",
        "open notepad and write my todo list",
    ]


def test_clear_history_wipes_messages_and_actions_but_keeps_facts(mem):
    _ask(mem, "open notepad", tools=["open_app"])
    mem.save_fact("my name is Garv", "user")
    mem.clear_history()
    assert mem.recent_messages(10) == [] and mem.list_actions() == []
    assert mem.similar_requests("open notepad") == []
    assert [f.text for f in mem.list_facts()] == ["my name is Garv"]
