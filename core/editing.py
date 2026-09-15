"""Quick-editor requests that go beyond one ``stream_edit`` call (chains, explanations).

Everything here is Tk-free and works with any backend that follows the
``core.backend`` contract, so it can be exercised with a fake service.
"""

from dataclasses import dataclass, field
from typing import List

from .backend import EditCancelled
from .models import ChainStep


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
