"""Tests for chained quick-editor requests (core/editing.py) with a fake backend."""

import threading

import pytest

from core.backend import EditCancelled
from core.editing import run_chain
from core.models import ChainStep


class FakeService:
    """Applies simple text substitutions per instruction and records every call."""

    display_name = "fake"

    def __init__(self, cancel_after=None):
        self.calls = []
        self.cancel_after = cancel_after  # set the cancel event after this many edits

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False):
        self.calls.append((instruction, text))
        if on_progress:
            on_progress(len(text))
        if self.cancel_after is not None and len(self.calls) >= self.cancel_after:
            cancel_event.set()
        if cancel_event.is_set() and self.cancel_after is None:
            raise EditCancelled("cancelled")
        if instruction == "fix":
            return text.replace("teh", "the")
        if instruction == "upper":
            return text.upper()
        return text + " (" + instruction + ")"

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None):
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
