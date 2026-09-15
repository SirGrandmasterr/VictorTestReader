"""Tests for the model-thinking view: the Tk-free stream log and the request labels."""

from types import SimpleNamespace

import pytest

from core.settings import BACKEND_OLLAMA, BACKEND_REMOTE, AppSettings
from core.workflow import CHECK_GRAMMAR
from ui.app import EditorApp
from ui.i18n import set_language
from ui.thinking_window import (
    STATE_ANSWERING,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ERROR,
    STATE_THINKING,
    STATE_WAITING,
    StreamLog,
    state_text,
)


@pytest.fixture(autouse=True)
def english_after_each_test():
    yield
    set_language("en")


def recording_log(**kwargs):
    log = StreamLog(**kwargs)
    seen = []
    log.listeners.append(lambda event, entry, text: seen.append((event, entry.key, text)))
    return log, seen


# --------------------------------------------------------------- StreamLog
def test_log_follows_a_request_through_its_states_and_tells_listeners():
    log, seen = recording_log()

    log.handle(("quick", 1, 1), "started", {"scope": "quick", "mode": "Grammar"})
    entry = log.entries[("quick", 1, 1)]
    assert entry.state == STATE_WAITING and entry.active and entry.info["mode"] == "Grammar"
    log.handle(("quick", 1, 1), "thinking", "Let me ")
    log.handle(("quick", 1, 1), "thinking", "see.")
    assert entry.state == STATE_THINKING and entry.thinking == "Let me see." and entry.thinking_chars == 11
    log.handle(("quick", 1, 1), "answer", "Edited.")
    assert entry.state == STATE_ANSWERING and entry.answer_chars == 7 and entry.thinking == "Let me see."
    log.handle(("quick", 1, 1), "finished", "done")
    assert entry.state == STATE_DONE and not entry.active and entry.finished_at is not None
    assert entry.seconds >= 0

    assert seen == [
        ("started", ("quick", 1, 1), ""),
        ("thinking", ("quick", 1, 1), "Let me "),
        ("thinking", ("quick", 1, 1), "see."),
        ("updated", ("quick", 1, 1), ""),
        ("updated", ("quick", 1, 1), ""),
    ]
    assert log.newest is entry and log.active_entries() == []


def test_outcomes_map_to_states_and_unknown_keys_are_ignored_or_created():
    log, _ = recording_log()
    for outcome, state in (("done", STATE_DONE), ("error", STATE_ERROR), ("cancelled", STATE_CANCELLED),
                           ("weird", STATE_DONE)):
        log.begin(("k", outcome), {})
        log.finish(("k", outcome), outcome)
        assert log.entries[("k", outcome)].state == state
    log.finish(("never", "started"), "done")  # nothing to close
    assert ("never", "started") not in log.entries
    # a piece of text that arrives before its announcement still gets an entry
    log.handle(("early",), "thinking", "x")
    assert log.entries[("early",)].thinking == "x" and log.entries[("early",)].info == {}
    # a late piece after the end does not revive the request
    log.handle(("k", "done"), "thinking", "late")
    assert log.entries[("k", "done")].state == STATE_DONE and log.entries[("k", "done")].thinking == "late"


def test_reasoning_is_capped_per_request_but_still_counted():
    log, seen = recording_log(max_chars=10)
    log.begin(("r",), {})
    log.append_thinking(("r",), "12345678")
    log.append_thinking(("r",), "90ABCDEF")  # only "90" fits
    log.append_thinking(("r",), "more")
    entry = log.entries[("r",)]
    assert entry.thinking == "1234567890" and entry.truncated and entry.thinking_chars == 20
    assert [text for event, _, text in seen if event == "thinking"] == ["12345678", "90", ""]


def test_oldest_finished_entries_are_dropped_beyond_the_limit_but_running_ones_stay():
    log, seen = recording_log(max_entries=3)
    for number in range(3):
        log.begin(("r", number), {})
    log.finish(("r", 1), "done")  # r0 and r2 keep running
    log.begin(("r", 3), {})
    assert list(log.entries) == [("r", 0), ("r", 2), ("r", 3)]
    assert ("removed", ("r", 1), "") in seen
    log.begin(("r", 4), {})  # nothing finished: the oldest goes anyway
    assert list(log.entries) == [("r", 2), ("r", 3), ("r", 4)]


def test_restarting_a_key_replaces_its_entry_and_clear_keeps_running_requests():
    log, seen = recording_log()
    log.begin(("review", 1, 2, "grammar"), {"scope": "evaluate"})
    log.append_thinking(("review", 1, 2, "grammar"), "old")
    log.finish(("review", 1, 2, "grammar"), "done")
    log.begin(("review", 1, 2, "grammar"), {"scope": "evaluate"})  # re-evaluated segment
    assert log.entries[("review", 1, 2, "grammar")].thinking == "" and len(log.entries) == 1
    assert [event for event, _, _ in seen][-2:] == ["removed", "started"]

    log.begin(("other",), {})
    log.finish(("other",), "error")
    assert log.clear_finished() == 1
    assert list(log.entries) == [("review", 1, 2, "grammar")]


def test_state_text_pairs_glyph_and_translated_word():
    assert state_text(STATE_THINKING) == "◐ thinking"
    assert state_text(STATE_ERROR) == "■ failed"
    set_language("de")
    assert state_text(STATE_THINKING) == "◐ denkt nach"


# ------------------------------------------------------------ app labels
def make_app_shell(backend=BACKEND_OLLAMA, thinking=False, project=None):
    app = EditorApp.__new__(EditorApp)
    app.settings = AppSettings()
    app.settings.backend = backend
    app.settings.remote_enable_thinking = thinking
    app.workflow_screen = SimpleNamespace(project=project)
    return app


def fake_project():
    def find(chapter_index, segment_index):
        if chapter_index == 1:
            return SimpleNamespace(title="Kapitel 1"), SimpleNamespace(index=segment_index)
        return None, None

    return SimpleNamespace(find=find)


def test_request_labels_cover_every_producer():
    app = make_app_shell(project=fake_project())
    describe = app.describe_request

    assert describe({"scope": "quick", "mode": "Grammar", "step": 1, "total": 1}) == "Quick edit · Grammar"
    assert describe({"scope": "quick", "mode": "My preset", "step": 2, "total": 3}) == (
        "Quick edit · My preset · step 2/3")
    assert describe({"scope": "quick_explain"}) == "Quick edit · explanations"
    assert describe({"scope": "evaluate", "chapter": 1, "segment": 4, "check": CHECK_GRAMMAR}) == (
        "Kapitel 1 · Segment 4 · Grammar")
    assert describe({"scope": "combined", "chapter": 7, "segment": 2}) == "Chapter 7 · Segment 2 · all checks"
    assert describe({"scope": "explain", "check": CHECK_GRAMMAR, "segments": [(1, 4)]}) == (
        "Explanations · Grammar · Kapitel 1 · Segment 4")
    assert describe({"scope": "explain", "check": CHECK_GRAMMAR, "segments": [(1, 4), (1, 5), (1, 6)]}) == (
        "Explanations · Grammar · Kapitel 1 · Segment 4 and 2 more")
    assert describe({"scope": "explain", "check": "odd", "segments": []}) == "Explanations · odd"
    assert describe({"scope": "consistency", "batch": 2, "total": 3}) == "Consistency check · batch 2 of 3"
    assert describe({"scope": "outline"}) == "Chapter outline"
    assert describe({}) == "Request"

    # without an open project the indices are shown as they are
    assert make_app_shell().describe_request({"scope": "evaluate", "chapter": 1, "segment": 4, "check": "spelling"}) == (
        "Chapter 1 · Segment 4 · Spelling")

    set_language("de")
    assert describe({"scope": "quick", "mode": "Grammar", "step": 1, "total": 1}) == "Schnellbearbeitung · Grammatik"
    assert describe({"scope": "evaluate", "chapter": 1, "segment": 4, "check": CHECK_GRAMMAR}) == (
        "Kapitel 1 · Abschnitt 4 · Grammatik")


def test_thinking_hint_depends_on_the_backend():
    assert "qwen3" in make_app_shell(BACKEND_OLLAMA).thinking_hint()
    assert "Connection dialog" in make_app_shell(BACKEND_REMOTE, thinking=False).thinking_hint()
    assert "reasoning parser" in make_app_shell(BACKEND_REMOTE, thinking=True).thinking_hint()
