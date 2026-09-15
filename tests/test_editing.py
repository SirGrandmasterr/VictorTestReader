"""Tests for chained quick-editor requests (core/editing.py) with a fake backend."""

import threading

import pytest

from core.backend import EditCancelled
from core.diff_engine import build_edit_session
from core.editing import explain_session, run_chain, session_hunks
from core.models import ChainStep
from core.workflow import CANNED_EXPLANATIONS, EXPLANATION_MAX_TOKENS


class FakeService:
    """Applies simple text substitutions per instruction and records every call."""

    display_name = "fake"

    def __init__(self, cancel_after=None):
        self.calls = []
        self.cancel_after = cancel_after  # set the cancel event after this many edits

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None, on_stream=None):
        self.calls.append((instruction, text))
        if on_progress:
            on_progress(len(text))
        if on_stream:
            on_stream("thinking", "about " + instruction)
        if self.cancel_after is not None and len(self.calls) >= self.cancel_after:
            cancel_event.set()
        if cancel_event.is_set() and self.cancel_after is None:
            raise EditCancelled("cancelled")
        if instruction == "fix":
            return text.replace("teh", "the")
        if instruction == "upper":
            return text.upper()
        return text + " (" + instruction + ")"

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None, on_stream=None):
        raise AssertionError("not used")


def test_run_chain_feeds_each_output_into_the_next_step():
    service = FakeService()
    seen = []

    result = run_chain(service, "m", [("Fix", "fix"), ("Loud", "upper")], "teh cat", threading.Event(),
                       on_step=lambda index, total, name: seen.append((index, total, name)))

    assert result.text == "THE CAT"
    assert result.steps == [ChainStep("Fix", "fix", "the cat"), ChainStep("Loud", "upper", "THE CAT")]
    assert service.calls == [("fix", "teh cat"), ("upper", "the cat")]
    assert seen == [(1, 2, "Fix"), (2, 2, "Loud")]


def test_run_chain_hands_the_stream_callback_to_every_step():
    pieces = []
    run_chain(FakeService(), "m", [("Fix", "fix"), ("Loud", "upper")], "teh", threading.Event(),
              on_stream=lambda kind, text: pieces.append((kind, text)))
    assert pieces == [("thinking", "about fix"), ("thinking", "about upper")]


def test_plain_instruction_strings_work_as_steps():
    result = run_chain(FakeService(), "m", ["fix"], "teh", threading.Event())
    assert result.text == "the" and result.steps[0].name == "fix"
    assert run_chain(FakeService(), "m", [], "x", threading.Event()).text == ""


def test_cancellation_between_steps_stops_the_chain():
    service = FakeService(cancel_after=1)  # the first step sets the cancel event on its way out
    with pytest.raises(EditCancelled):
        run_chain(service, "m", [("Fix", "fix"), ("Loud", "upper")], "teh", threading.Event())
    assert len(service.calls) == 1  # the second step never started


def test_cancellation_before_the_first_step():
    cancel = threading.Event()
    cancel.set()
    service = FakeService()
    with pytest.raises(EditCancelled):
        run_chain(service, "m", [("Fix", "fix")], "teh", cancel)
    assert service.calls == []


# ---------------------------------------------------------- explanations
class ExplainingService(FakeService):
    """Answers explanation requests with a numbered JSON object (or whatever ``answer`` says)."""

    def __init__(self, answer=None, cancel=False):
        super().__init__()
        self.answer = answer
        self.cancel = cancel
        self.requests = []

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None, on_stream=None):
        self.requests.append((messages, max_tokens))
        if self.cancel:
            raise EditCancelled("cancelled")
        if self.answer is not None:
            return self.answer
        count = messages[1]["content"].count("\u2192")
        return '{"explanations": {' + ", ".join('"{0}": "Reason {0}"'.format(n) for n in range(1, count + 1)) + "}}"


def make_session():
    original = "Teh cat sat down. the dog were loud,and very very tired."
    proposed = "The cat sat down. The dog was loud, and extremely tired."
    return build_edit_session(original, proposed, instruction="Fix grammar.", model="m", revision_id=1)


def test_session_hunks_carry_offsets_into_the_original_text():
    session = make_session()
    hunks = session_hunks(session)
    assert hunks and all(session.original_text[h.start:h.end] == h.original_text for h in hunks)
    assert [h.original_text for h in hunks][:2] == ["Teh", "the"]


def test_explain_session_numbers_changes_across_items_and_uses_canned_sentences():
    session = make_session()
    service = ExplainingService()

    explained = explain_session(service, "m", session, "Fix grammar issues.", threading.Event())

    hunks = [hunk for item in session.review_items for hunk in item.changed_hunks]
    assert explained == len(hunks) and all(hunk.explanation for hunk in hunks)
    canned = {hunk.explanation for hunk in hunks} & {texts["en"] for texts in CANNED_EXPLANATIONS.values()}
    assert canned  # "Teh"→"The", "the"→"The" and ",and"→", and" need no model
    modelled = [hunk for hunk in hunks if hunk.explanation.startswith("Reason ")]
    assert [hunk.explanation for hunk in modelled] == ["Reason {0}".format(n) for n in range(1, len(modelled) + 1)]
    assert len(service.requests) == 1
    messages, max_tokens = service.requests[0]
    assert max_tokens == EXPLANATION_MAX_TOKENS
    assert "Instruction: Fix grammar issues." in messages[1]["content"]
    assert session.original_text in messages[1]["content"]


def test_malformed_answer_leaves_model_explanations_empty_but_keeps_canned_ones():
    session = make_session()
    service = ExplainingService(answer="Sure! Here are my thoughts...")

    explained = explain_session(service, "m", session, "Fix grammar.", threading.Event())

    hunks = [hunk for item in session.review_items for hunk in item.changed_hunks]
    assert 0 < explained < len(hunks)
    assert all(not hunk.explanation.startswith("Reason") for hunk in hunks)


def test_explain_session_honours_cancellation():
    session = make_session()
    with pytest.raises(EditCancelled):
        explain_session(ExplainingService(cancel=True), "m", session, "Fix grammar.", threading.Event())


def test_explain_session_without_changes_makes_no_request():
    session = build_edit_session("Same text.", "Same text.")
    service = ExplainingService()
    assert explain_session(service, "m", session, "x", threading.Event()) == 0
    assert service.requests == []
