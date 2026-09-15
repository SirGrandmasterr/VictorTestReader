"""Replay recorded model answers through ``run_check`` and judge the outcomes.

The sample texts in ``tests/recorded/samples`` carry planted errors; the
cassettes next to them hold what a real model answered. Tests assert on parsed
outcomes (kinds of changes, explanations, untouched names), never on exact
strings, so a re-recording with a different model can still pass. Samples
without a cassette are skipped; ``scripts/record_responses.py`` creates them.
The harness itself (recording, replay, cache misses) is tested offline below.
"""

import json
import threading

import pytest

from core.backend import BackendUnavailable, build_messages
from core.workflow import (
    CHECK_EXPRESSION,
    CHECK_GRAMMAR,
    CHECK_INSTRUCTIONS,
    CHECK_SPELLING,
    CHECKS,
    EVALUATION_COMBINED,
    FALLBACK_EXPLANATIONS,
    ProjectOptions,
    build_combined_messages,
    build_explanation_messages,
    extract_changes,
    run_check,
    run_segment_combined,
    sanity_check_proposal,
)
from recorded_service import (
    RERECORD_HINT,
    RecordedService,
    RecordingService,
    cassette_path,
    cassettes_for,
    check_name,
    load_sample,
    model_slug,
    request_key,
    sample_names,
    split_edit_request,
    write_cassette,
)

# run_check asks for edits with the segment before the instruction so the
# checks of one segment share a prompt prefix; keys for review edits must be
# computed the same way.
REVIEW_TEXT_FIRST = True

# What each sample plants: the typo the spelling check must catch and the
# proper noun no check may touch. Keep in sync with tests/recorded/samples.
PLANTED = {
    "de_bahnhof": {"typo": "geschlaffen", "proper_noun": "Thalbrück"},
    "de_werkstatt": {"typo": "Schraubenziher", "proper_noun": "Aubinger"},
    "de_brief": {"typo": "Herbstveranstalltung", "proper_noun": "Hohenwart"},
    "en_harbor": {"typo": "recieved", "proper_noun": "Kellanby"},
    "en_library": {"typo": "seperate", "proper_noun": "Ashcombe"},
    "en_orchard": {"typo": "occured", "proper_noun": "Brackenholt"},
}


# ---------------------------------------------------------------- samples
def test_samples_match_the_planted_manifest():
    assert sample_names() == sorted(PLANTED)
    for sample, planted in PLANTED.items():
        text = load_sample(sample)
        assert 300 <= len(text) <= 900, sample
        assert text.count(planted["typo"]) == 1, sample
        assert planted["proper_noun"] in text, sample


# ---------------------------------------------------------------- replay
def _cases():
    for sample in sample_names():
        cassettes = cassettes_for(sample)
        if not cassettes:
            reason = "no cassette for {0}; {1}".format(sample, RERECORD_HINT)
            yield pytest.param((sample, None), id=sample, marks=pytest.mark.skip(reason=reason))
        for path in cassettes:
            yield pytest.param((sample, path), id=path.stem)


_REPLAYS = {}


@pytest.fixture(params=list(_cases()))
def replay(request):
    """Run every check for one (sample, cassette) once and share the results."""
    sample, path = request.param
    if path not in _REPLAYS:
        service = RecordedService(path)
        text = load_sample(sample)
        results = {
            check: run_check(service, service.model, text, check, threading.Event(), explain=True)
            for check in CHECKS
        }
        _REPLAYS[path] = (sample, text, service, results)
    return _REPLAYS[path]


def _spans(text, needle):
    spans, start = [], text.find(needle)
    while start != -1:
        spans.append((start, start + len(needle)))
        start = text.find(needle, start + 1)
    return spans


def test_every_check_completes_without_retry_or_rejection(replay):
    sample, text, service, results = replay
    for check, result in results.items():
        assert result.status == "done", "{0}/{1}: {2}".format(sample, check, result.error)
        sanity_check_proposal(text, result.proposed_text)  # raises ProposalRejected otherwise
    edit_calls = [key for name, key in service.calls if name in CHECKS]
    assert len(edit_calls) == len(set(edit_calls)), "an edit request was retried"


def test_planted_typo_is_a_spelling_change(replay):
    sample, _, _, results = replay
    typo = PLANTED[sample]["typo"]
    spelling = results[CHECK_SPELLING].changes
    assert any(typo in change.original_text for change in spelling), (
        "{0}: spelling did not touch {1!r}; got {2}".format(
            sample, typo, [(c.original_text, c.proposed_text) for c in spelling]
        )
    )


def test_proper_noun_is_never_touched(replay):
    sample, text, _, results = replay
    noun = PLANTED[sample]["proper_noun"]
    spans = _spans(text, noun)
    for check, result in results.items():
        assert noun in result.proposed_text, "{0}/{1} dropped {2!r}".format(sample, check, noun)
        for change in result.changes:
            assert noun not in change.original_text, (sample, check, change)
            assert not any(change.start < end and start < change.end for start, end in spans), (
                sample, check, change
            )


def test_explanations_are_mostly_model_written(replay):
    sample, _, _, results = replay
    changes = [(check, change) for check, result in results.items() for change in result.changes]
    assert changes, "{0}: no check proposed anything".format(sample)
    explained = [
        change for check, change in changes if change.explanation != FALLBACK_EXPLANATIONS[check]
    ]
    assert 2 * len(explained) >= len(changes), "{0}: {1}/{2} explained".format(
        sample, len(explained), len(changes)
    )


# ------------------------------------------------------- combined mode
def _combined_cases():
    for sample in sample_names():
        cassettes = cassettes_for(sample, combined=True)
        if not cassettes:
            reason = "no combined cassette for {0}; run scripts/record_responses.py --mode combined".format(sample)
            yield pytest.param((sample, None), id=sample + "-combined", marks=pytest.mark.skip(reason=reason))
        for path in cassettes:
            yield pytest.param((sample, path), id=path.stem)


_COMBINED_REPLAYS = {}


@pytest.fixture(params=list(_combined_cases()))
def combined_replay(request):
    """Run the single combined request for one (sample, cassette) once and share the results."""
    sample, path = request.param
    if path not in _COMBINED_REPLAYS:
        service = RecordedService(path)
        text = load_sample(sample)
        options = ProjectOptions(evaluation_mode=EVALUATION_COMBINED)
        results = run_segment_combined(service, service.model, text, options, threading.Event())
        _COMBINED_REPLAYS[path] = (sample, text, service, results)
    return _COMBINED_REPLAYS[path]


def test_combined_answer_is_used_without_fallback(combined_replay):
    sample, text, service, results = combined_replay
    assert [name for name, _ in service.calls] == ["combined"], "the combined request fell back to separate checks"
    for check in CHECKS:
        assert results[check].status == "done" and results[check].method == EVALUATION_COMBINED, (sample, check)
        sanity_check_proposal(text, results[check].proposed_text)


def test_combined_answer_fixes_the_typo_and_keeps_the_proper_noun(combined_replay):
    sample, text, _, results = combined_replay
    typo = PLANTED[sample]["typo"]
    noun = PLANTED[sample]["proper_noun"]
    assert any(typo in change.original_text for change in results[CHECK_SPELLING].changes), sample
    for check in CHECKS:
        assert noun in results[check].proposed_text, (sample, check)
        assert not any(noun in change.original_text for change in results[check].changes), (sample, check)
    changes = [change for result in results.values() for change in result.changes]
    assert changes and all(change.explanation for change in changes)


# ------------------------------------------------------- harness itself
class ScriptedBackend:
    """Deterministic stand-in for a live backend used to exercise record/replay."""

    display_name = "scripted"
    backend_id = "scripted"

    def __init__(self, fail_first=0):
        self.fail_first = fail_first
        self.generate_calls = 0

    def list_models(self):
        return ["scripted-1b"]

    def connection_summary(self):
        return "Connected"

    def no_models_hint(self):
        return "none"

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None):
        self.generate_calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise BackendUnavailable("transient")
        user = messages[1]["content"]
        edit = split_edit_request(user)
        if edit is not None:
            instruction, text = edit
            if instruction.startswith("Correct spelling"):
                return text.replace("teh", "the")
            if instruction.startswith("Fix grammar"):
                return text.replace("were", "was")
            return text.replace("very very", "extremely")
        count = user.count("→")
        return json.dumps({str(n): "Because {0}".format(n) for n in range(1, count + 1)})

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None):
        return self.generate(model, build_messages(instruction, text, text_first), cancel_event)


TEXT = "teh dog were very very big and it run fast, said Ravenscourt."


def _record(tmp_path, backend, model="scripted-1b"):
    recorder = RecordingService(backend)
    results = {
        check: run_check(recorder, model, TEXT, check, threading.Event()) for check in CHECKS
    }
    path = write_cassette(cassette_path("demo", model, tmp_path), model, "scripted", recorder.entries.values(), "demo")
    return path, results


def test_record_then_replay_reproduces_the_pipeline_outcome(tmp_path):
    backend = ScriptedBackend()
    path, live = _record(tmp_path, backend)
    assert path.name == "demo.scripted-1b.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "scripted-1b" and data["sample"] == "demo"
    # three edits + three explanation requests, every one keyed and tagged with its check
    assert len(data["requests"]) == 6
    assert sorted(entry["check"] for entry in data["requests"]) == sorted(
        list(CHECKS) + [check + "-explanation" for check in CHECKS]
    )
    for entry in data["requests"]:
        assert entry["key"] == request_key(entry["model"], entry["messages"], entry["max_tokens"])
    edits = {entry["check"]: entry for entry in data["requests"] if entry["check"] in CHECKS}
    for check in CHECKS:
        expected = build_messages(CHECK_INSTRUCTIONS[check], TEXT, text_first=REVIEW_TEXT_FIRST)
        assert edits[check]["messages"] == expected
        assert edits[check]["messages"][1]["content"].startswith("Text:\n" + TEXT)
        assert edits[check]["key"] == request_key("scripted-1b", expected)

    service = RecordedService(path)
    assert service.list_models() == ["scripted-1b"]
    replayed = {check: run_check(service, service.model, TEXT, check, threading.Event()) for check in CHECKS}
    for check in CHECKS:
        assert replayed[check].status == "done"
        assert [c.to_dict() for c in replayed[check].changes] == [c.to_dict() for c in live[check].changes]
        assert replayed[check].explained is True
    assert backend.generate_calls == 6  # replay never touched the live backend
    assert len(service.calls) == 6


def test_changed_prompt_fails_with_the_unrecorded_request(tmp_path):
    path, _ = _record(tmp_path, ScriptedBackend())
    service = RecordedService(path)

    with pytest.raises(KeyError) as excinfo:
        service.stream_edit("scripted-1b", "Correct spelling but differently", TEXT, threading.Event())
    message = str(excinfo.value)
    assert "edit request" in message  # instruction no longer matches a known check
    assert "Instruction: Correct spelling but differently" in message
    assert RERECORD_HINT in message
    assert path.name in message

    with pytest.raises(KeyError) as excinfo:
        run_check(service, "other-model", TEXT, CHECK_GRAMMAR, threading.Event())
    assert "the grammar request" in str(excinfo.value)
    assert "'other-model'" in str(excinfo.value)

    # A replaced text reaches the explanation step only if the edit was recorded,
    # so the miss is reported for the edit and says which check it belongs to.
    with pytest.raises(KeyError) as excinfo:
        run_check(service, "scripted-1b", TEXT + " More.", CHECK_EXPRESSION, threading.Event())
    assert "the expression request" in str(excinfo.value)


def test_miss_message_shows_the_first_80_characters_of_the_user_message():
    service = RecordedService({"model": "m", "requests": []})
    long_text = "x" * 200
    with pytest.raises(KeyError) as excinfo:
        service.generate("m", [{"role": "system", "content": "s"}, {"role": "user", "content": long_text}],
                         threading.Event())
    message = str(excinfo.value)
    assert "x" * 80 in message and "x" * 81 not in message
    assert "the unknown request" in message


def test_recording_keeps_the_error_until_the_request_succeeds(tmp_path):
    backend = ScriptedBackend(fail_first=1)
    recorder = RecordingService(backend)
    result = run_check(recorder, "scripted-1b", TEXT, CHECK_SPELLING, threading.Event(), explain=False)
    assert result.status == "done"
    key = request_key(
        "scripted-1b", build_messages(CHECK_INSTRUCTIONS[CHECK_SPELLING], TEXT, text_first=REVIEW_TEXT_FIRST)
    )
    assert "response" in recorder.entries[key] and "error" not in recorder.entries[key]

    always_failing = RecordingService(ScriptedBackend(fail_first=10))
    result = run_check(always_failing, "scripted-1b", TEXT, CHECK_SPELLING, threading.Event(), explain=False)
    assert result.status == "error"
    entry = always_failing.entries[key]
    assert entry["error"] == "transient" and "response" not in entry

    replay = RecordedService({"model": "scripted-1b", "requests": [entry]})
    with pytest.raises(BackendUnavailable):
        replay.stream_edit(
            "scripted-1b", CHECK_INSTRUCTIONS[CHECK_SPELLING], TEXT, threading.Event(),
            text_first=REVIEW_TEXT_FIRST,
        )


def test_keys_ignore_nothing_that_the_backends_send():
    messages = build_messages("Do it", "Text")
    assert request_key("m", messages) == request_key("m", build_messages("Do it", "Text"))
    assert request_key("m", messages) != request_key("m", messages, max_tokens=2048)
    assert request_key("m", messages) != request_key("n", messages)
    assert request_key("m", build_messages("Do it", "Text ")) != request_key("m", messages)
    # The two message orders are different requests (and different cassette entries).
    assert request_key("m", build_messages("Do it", "Text", text_first=True)) != request_key("m", messages)


def test_combined_cassettes_are_kept_apart_from_separate_ones(tmp_path):
    separate = cassette_path("demo", "m", tmp_path)
    combined = cassette_path("demo", "m", tmp_path, combined=True)
    assert separate.name == "demo.m.json" and combined.name == "demo.m.combined.json"
    for path in (separate, combined):
        path.write_text('{"model": "m", "requests": []}', encoding="utf-8")
    assert cassettes_for("demo", tmp_path) == [separate]
    assert cassettes_for("demo", tmp_path, combined=True) == [combined]
    assert check_name(build_combined_messages("t", ProjectOptions())) == "combined"


def test_helpers_name_checks_and_slug_models():
    assert model_slug("Qwen/Qwen3-32B-AWQ") == "qwen-qwen3-32b-awq"
    assert model_slug("llama3.1:8b") == "llama3.1-8b"
    assert model_slug("") == "model"
    assert check_name(build_messages(CHECK_INSTRUCTIONS[CHECK_GRAMMAR], "t")) == CHECK_GRAMMAR
    assert check_name(build_messages(CHECK_INSTRUCTIONS[CHECK_GRAMMAR], "t", text_first=True)) == CHECK_GRAMMAR
    assert check_name(build_messages("Anything else", "t", text_first=True)) == "edit"
    assert split_edit_request("Text:\nA\n\nInstruction:\nB\n\nInstruction:\nC") == ("C", "A\n\nInstruction:\nB")
    changes = extract_changes("teh", "the", CHECK_SPELLING)
    assert check_name(build_explanation_messages("teh", changes, CHECK_SPELLING)) == "spelling-explanation"
    assert check_name([{"role": "user", "content": "hello"}]) == "unknown"
