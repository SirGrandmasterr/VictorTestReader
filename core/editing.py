"""Quick-editor requests that go beyond one ``stream_edit`` call (chains, explanations).

Everything here is Tk-free and works with any backend that follows the
``core.backend`` contract, so it can be exercised with a fake service.
"""

from dataclasses import dataclass, field
from typing import List

from .backend import EditCancelled
from .change_kinds import classify_change, edit_distance
from .models import ChainStep
from .workflow import (
    SAME_LANGUAGE,
    apply_canned_explanations,
    distribute_explanations,
    request_explanations,
)


@dataclass
class ChainResult:
    """Outcome of ``run_chain``: every step with its output; ``text`` is the final one."""

    steps: List[ChainStep] = field(default_factory=list)

    @property
    def text(self):
        return self.steps[-1].output if self.steps else ""


def run_chain(service, model, steps, text, cancel_event, on_step=None, on_progress=None):
    """Run editing ``steps`` one after another, each editing the previous step's output.

    ``steps`` is a list of ``(name, instruction)`` pairs (plain instruction
    strings work too). ``on_step(index, total, name)`` is called before each
    step (1-based). Cancellation is honoured between steps as well as inside
    a step: ``EditCancelled`` is raised as soon as ``cancel_event`` is set.
    Returns a ``ChainResult``.
    """
    result = ChainResult()
    current = text
    total = len(steps)
    for index, step in enumerate(steps, 1):
        name, instruction = step if isinstance(step, (tuple, list)) else (str(step)[:30], str(step))
        if cancel_event is not None and cancel_event.is_set():
            raise EditCancelled("Editing was cancelled.")
        if on_step:
            on_step(index, total, name)
        current = service.stream_edit(model, instruction, current, cancel_event, on_progress=on_progress)
        result.steps.append(ChainStep(name, instruction, current))
    return result


class HunkChange:
    """Adapter that lets a quick-editor ``ChangeHunk`` go through the workflow's explanation helpers.

    Those helpers expect ``start``/``end`` offsets in the text, the two texts,
    a ``kind``, a ``distance`` and an ``explanation`` attribute; writes to
    ``explanation`` land on the hunk.
    """

    def __init__(self, hunk, start, end):
        self.hunk = hunk
        self.start = start
        self.end = end
        self.original_text = hunk.original_text
        self.proposed_text = hunk.proposed_text
        self.kind = classify_change(hunk.original_text, hunk.proposed_text)

    @property
    def distance(self):
        return edit_distance(self.original_text, self.proposed_text)

    @property
    def explanation(self):
        return self.hunk.explanation

    @explanation.setter
    def explanation(self, value):
        self.hunk.explanation = value


def session_hunks(session):
    """Every changed hunk of ``session`` as ``HunkChange`` with offsets into ``session.original_text``."""
    result = []
    for item in session.review_items:
        position = item.original_start
        for hunk in item.hunks:
            end = position + len(hunk.original_text)
            if hunk.is_change:
                result.append(HunkChange(hunk, position, end))
            position = end
    return result


def explain_session(service, model, session, instruction, cancel_event, language=SAME_LANGUAGE):
    """Fill ``ChangeHunk.explanation`` for every change of a quick-editor session.

    Trivial changes get the canned sentences the manuscript review uses; the
    rest are numbered across all review items and explained in one request
    (``EXPLANATION_MAX_TOKENS``). A malformed or failed answer leaves the
    affected hunks without explanation; cancellation raises ``EditCancelled``.
    Returns the number of hunks that now carry an explanation.
    """
    hunks = session_hunks(session)
    remaining = apply_canned_explanations(session.original_text, hunks, language)
    if remaining:
        numbered = request_explanations(
            service, model, [(session.original_text, remaining)], instruction, cancel_event, language
        )
        distribute_explanations(remaining, numbered)
    return sum(1 for hunk in hunks if hunk.explanation)
