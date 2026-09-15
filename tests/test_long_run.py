"""Opt-in long-run stability test of the evaluation runner (``pytest -m slow -q``).

A 3,000-segment synthetic manuscript is evaluated by ``ProjectRunner`` against
a fake backend that fails 3 % of its calls, truncates 0.5 % and answers with a
20–80 ms jitter, on four parallel workers. The run is cancelled, saved,
reloaded from disk and resumed five times at random moments, then driven to
completion (errors are retried the way the Retry button does). Afterwards
every segment must carry one result per enabled check, no (segment, check)
may have received two "done" results, the worker threads must be gone and the
saved project must equal the in-memory one.

The test takes a few minutes and is deselected by default (see pytest.ini).
"""

import queue
import random
import threading
import time
import tracemalloc
from collections import Counter

import pytest

from core.backend import BackendUnavailable, EditCancelled, OutputTruncated
from core.workflow import (
    CHECK_EXPRESSION,
    CHECK_GRAMMAR,
    CHECK_SPELLING,
    EVALUATION_COMBINED,
    EVALUATION_SEPARATE,
    Project,
    ProjectOptions,
    ProjectRunner,
    apply_result,
    create_project,
)
from test_workflow import FakeService

SEGMENTS = 3000
CHAPTERS = 30
PARALLELISM = 4
CYCLES = 5  # cancel / save / reload / resume rounds before the run to completion
MAX_RETRY_PASSES = 10  # "Retry failed" rounds allowed until every error result is gone
MEMORY_LIMIT = 200 * 1024 * 1024
THREAD_SETTLE_SECONDS = 2.0

# None of these words contains "teh", "were" or "very", the tokens the fake service corrects.
VOCABULARY = (
    "morning river quiet lantern garden letter window harbour silver winter bridge candle meadow stone "
    "shadow valley hollow orchard market thunder pocket ribbon sailor cellar chapel copper fabric hunger "
    "island jacket kettle ladder magnet needle oyster pillow rabbit saddle tablet umbrella violin walnut "
    "yellow zipper anchor basket carpet dinner engine forest gravel hammer iron jungle kitchen lemon mirror"
).split()
PLANTED = (
    (CHECK_SPELLING, "teh", "the"),
    (CHECK_GRAMMAR, "were", "was"),
    (CHECK_EXPRESSION, "very very", "extremely"),
)


def synthetic_manuscript(rng, chapters=CHAPTERS, paragraphs_per_chapter=SEGMENTS // CHAPTERS):
    """Chapters of short paragraphs (one segment each) with typos planted from ``PLANTED``."""
    parts = []
    for chapter in range(1, chapters + 1):
        parts.append("Chapter {0}\n\n".format(chapter))
        for _ in range(paragraphs_per_chapter):
            words = [rng.choice(VOCABULARY) for _ in range(rng.randint(30, 42))]
            for _, token, _ in PLANTED:
                if rng.random() < 0.5:
                    words.insert(rng.randrange(1, len(words)), token)  # at most once per paragraph
            words[0] = words[0].capitalize()
            parts.append(" ".join(words) + ".\n\n")
    return "".join(parts)


class LongRunService(FakeService):
    """FakeService with probabilistic failures, truncation and latency jitter (thread-safe)."""

    def __init__(self, seed, failure=0.03, truncation=0.005, delay=(0.02, 0.08)):
        super().__init__()
        self.rng = random.Random(seed)
        self.failure = failure
        self.truncation = truncation
        self.delay_range = delay
        self.requests = Counter()  # "edit" / "combined" / "explain" / "failed" / "truncated"

    def _roll(self, kind, cancel_event):
        """Count the request, wait the jitter (cancellable) and maybe raise a fault."""
        with self.lock:
            self.requests[kind] += 1
            roll = self.rng.random()
            delay = self.rng.uniform(*self.delay_range)
        if cancel_event.wait(delay):
            raise EditCancelled("cancelled")
        if roll < self.truncation:
            with self.lock:
                self.requests["truncated"] += 1
            raise OutputTruncated("cut")
        if roll < self.truncation + self.failure:
            with self.lock:
                self.requests["failed"] += 1
            raise BackendUnavailable("transient")

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None):
        self._roll("edit", cancel_event)
        if instruction.startswith("Correct spelling"):
            return text.replace("teh", "the")
        if instruction.startswith("Fix grammar"):
            return text.replace("were", "was")
        return text.replace("very very", "extremely")

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None,
                 on_usage=None):
        content = messages[1]["content"]
        if content.startswith("Review the text below"):
            self._roll("combined", cancel_event)
            return self.combined_answer(content, response_format)
        self._roll("explain", cancel_event)
        count = content.count("→")
        return '{{{0}}}'.format(", ".join('"{0}": "Reason {0}"'.format(n) for n in range(1, count + 1)))

    def combined_answer(self, content, response_format):
        """List the planted tokens of the segment in text order (each occurs at most once)."""
        text = content.split("\n\nText:\n", 1)[1].split("\n\nReturn a JSON object", 1)[0]
        edits = []
        for check, token, fix in PLANTED:
            position = text.find(token)
            if position != -1:
                edits.append((position, check, token, fix))
        edits.sort()
        return '{"edits": [' + ", ".join(
            '{{"category": "{0}", "original": "{1}", "replacement": "{2}", "reason": "Planted."}}'.format(check, token, fix)
            for _, check, token, fix in edits
        ) + "]}"


def run_until_finished(project, service, events, counts, cancel_after=None):
    """Start a runner on ``project`` and consume its events until ``workflow_finished``.

    Results are applied to the project as the UI does; ``counts`` receives one
    hit per (chapter, segment, check, status) result event. With
    ``cancel_after`` the runner is cancelled that many seconds after the start.
    Returns the finished event.
    """
    runner = ProjectRunner(project, service, "m", events, parallelism=PARALLELISM)
    runner.start()
    cancel_at = time.time() + cancel_after if cancel_after is not None else None
    deadline = time.time() + 1800
    while True:
        if cancel_at is not None and time.time() >= cancel_at:
            runner.cancel()
            cancel_at = None
        try:
            event = events.get(timeout=0.05)
        except queue.Empty:
            assert time.time() < deadline, "runner did not finish within 30 minutes"
            continue
        kind = event[0]
        if kind == "workflow_result":
            _, chapter_index, segment_index, check, result = event
            apply_result(project, chapter_index, segment_index, check, result)
            counts[(chapter_index, segment_index, check, result.status)] += 1
        elif kind == "workflow_finished":
            return event


def _wait_for_threads(baseline, seconds=THREAD_SETTLE_SECONDS):
    deadline = time.time() + seconds
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.05)
    return threading.active_count()


@pytest.mark.slow
@pytest.mark.parametrize("mode", [EVALUATION_COMBINED, EVALUATION_SEPARATE])
def test_runner_survives_cancel_resume_cycles_on_a_large_project(tmp_path, mode):
    baseline_threads = threading.active_count()
    rng = random.Random(20260915)
    text = synthetic_manuscript(rng)
    source = tmp_path / "long-run.txt"
    source.write_text(text, encoding="utf-8")
    options = ProjectOptions(target_chars=200, max_chars=600, parallelism=PARALLELISM, evaluation_mode=mode)
    project = create_project(source, text, options, model="m", backend="fake")
    assert len(project.chapters) == CHAPTERS
    assert sum(len(chapter.segments) for chapter in project.chapters) == SEGMENTS
    assert all(not segment.is_blank for _, segment in project.all_segments())
    project.save()
    checks = project.options.enabled_checks()
    total_tasks = len(project.pending_tasks())

    service = LongRunService(seed=7)
    counts = Counter()
    events = queue.Queue()
    tracemalloc.start()
    try:
        started = time.time()
        for cycle in range(CYCLES):
            finished = run_until_finished(project, service, events, counts, cancel_after=rng.uniform(0.2, 1.5))
            assert finished == ("workflow_finished", True), "cycle {0} did not report the cancel".format(cycle)
            assert events.empty(), "events after workflow_finished in cycle {0}".format(cycle)
            pending = project.pending_tasks()
            assert 0 < len(pending) <= total_tasks
            project.save()
            project = Project.load(project.root)  # resume from disk, as the app does after a restart
            assert project.pending_tasks() == pending

        passes = 0
        while project.pending_tasks():
            assert passes < MAX_RETRY_PASSES, "errors kept coming back after {0} retry passes".format(passes)
            finished = run_until_finished(project, service, events, counts)
            assert finished == ("workflow_finished", False)
            assert events.empty()
            passes += 1
        elapsed = time.time() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    # every non-blank segment has one result per enabled check, all of them done
    for _, segment in project.all_segments():
        for check in checks:
            assert check in segment.results, (segment.index, check)
            assert segment.results[check].status == "done", (segment.index, check)  # retry passes cleared the errors
    assert project.pending_tasks() == []

    # no (segment, check) received two "done" results across all runs
    duplicates = {key: n for key, n in counts.items() if key[3] == "done" and n > 1}
    assert duplicates == {}
    done_keys = {key[:3] for key in counts if key[3] == "done"}
    assert len(done_keys) == SEGMENTS * len(checks)
    stats = project.progress()
    assert stats["tasks_done"] == stats["tasks_total"] == SEGMENTS * len(checks)
    assert stats["error"] == 0 and stats["queued"] == 0
    assert stats["changes"] > SEGMENTS  # about 1.5 planted typos per paragraph
    if mode == EVALUATION_SEPARATE:
        assert service.requests["explain"] > 0  # the explainer thread was exercised

    # the faults were actually injected
    assert service.requests["failed"] > 0
    assert service.requests["truncated"] > 0

    # the worker and explainer threads are gone
    assert _wait_for_threads(baseline_threads) <= baseline_threads
    assert peak < MEMORY_LIMIT, "tracemalloc peak {0:.0f} MB".format(peak / 1024 / 1024)

    # the saved file equals the in-memory project
    project.save()
    assert Project.load(project.root).progress() == project.progress()
    print("\n{0}: {1} segments, {2} retry pass(es), {3} requests ({4} failed, {5} truncated), "
          "{6:.0f} s, peak {7:.0f} MB".format(
              mode, SEGMENTS, passes, sum(service.requests[k] for k in ("edit", "combined", "explain")),
              service.requests["failed"], service.requests["truncated"], elapsed, peak / 1024 / 1024))
