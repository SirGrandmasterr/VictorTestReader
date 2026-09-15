"""Tests for the automatic manuscript workflow (core logic, no Tk)."""

import json
import os
import queue
import threading
import time
from pathlib import Path

import pytest

from core.backend import (
    BackendUnavailable,
    EditCancelled,
    OutputTruncated,
    StructuredOutputUnsupported,
    UsageRecord,
    empty_usage,
)
from core.models import ACCEPTED, PENDING, REJECTED
from core.change_kinds import CHANGE_KIND_LABELS, CHANGE_KINDS, levenshtein
from core.workflow import (
    ALL_CHECKS,
    AUTHOR_EXPLANATION,
    CHANGE_KINDS as WORKFLOW_CHANGE_KINDS,
    CHECK_AUTHOR,
    CHECK_EXPRESSION,
    CHECK_GRAMMAR,
    CHECK_INSTRUCTIONS,
    CHECK_SPELLING,
    CHECKS,
    CANNED_EXPLANATIONS,
    COMBINED_SCHEMA,
    DECISION_LOG_LIMIT,
    EVALUATION_COMBINED,
    EVALUATION_SEPARATE,
    EXPLANATION_GROUP_SIZE,
    FLAG_GROWTH,
    FLAG_NOVEL_WORDS,
    FLAG_REPORT_MARK,
    FLAG_REWRITE_IN_STRICT_CHECK,
    GLOSSARY_EXPLANATION,
    GLOSSARY_HEADER,
    GLOSSARY_MAX_CHARS,
    GLOSSARY_MAX_TERMS,
    STYLE_GUIDE_HEADER,
    STYLE_GUIDE_MAX_CHARS,
    STATE_APPLIED,
    STATE_PENDING,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATUS_CLEAN,
    STATUS_ERROR,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_REVIEWED,
    Change,
    CheckResult,
    Project,
    ProjectOptions,
    ProjectRunner,
    Segment,
    apply_model_outline,
    apply_result,
    applied_changes,
    build_check_instruction,
    build_combined_messages,
    build_explanation_messages,
    build_grouped_explanation_messages,
    canned_explanation,
    change_states,
    classify_change,
    create_project,
    detect_language,
    evaluate,
    explain_result,
    explanation_language,
    extract_changes,
    flag_changes,
    flag_suspicious,
    format_glossary,
    format_style_guide,
    glossary_matcher,
    glossary_prompt_terms,
    new_decision_group,
    normalise_style_guide,
    novel_words,
    parse_combined,
    parse_explanations,
    parse_glossary,
    parse_outline,
    pending_changes,
    render_chapter_annotated,
    render_segment,
    render_segment_annotated,
    request_explanations,
    resync_project,
    text_fingerprint,
    run_check,
    run_segment_combined,
    sanity_check_proposal,
    strip_fences,
    suppress_glossary_changes,
)

ALL = {check: True for check in CHECKS}


# --------------------------------------------------------------- changes
def test_extract_changes_anchors_offsets_in_the_original():
    original = "Teh cat sat on teh mat. It were happy.\n\nAnother line."
    proposed = "The cat sat on the mat. It was happy.\n\nAnother line."

    changes = extract_changes(original, proposed, CHECK_SPELLING)

    assert [(c.original_text, c.proposed_text) for c in changes] == [
        ("Teh", "The"), ("teh", "the"), ("were", "was")
    ]
    for change in changes:
        assert original[change.start:change.end] == change.original_text
        assert change.check == CHECK_SPELLING
        assert change.decision == PENDING
    assert [c.change_id for c in changes] == ["spelling-1", "spelling-2", "spelling-3"]


def test_insertions_and_deletions_get_zero_or_full_spans():
    original = "I saw the the dog"
    changes = extract_changes(original, "I saw the dog", CHECK_GRAMMAR)
    assert len(changes) == 1
    assert original[changes[0].start:changes[0].end] == changes[0].original_text
    assert changes[0].proposed_text == ""

    changes = extract_changes("I saw dog", "I saw a dog", CHECK_GRAMMAR)
    assert any(c.original_text == "" and "a" in c.proposed_text for c in changes)


# ----------------------------------------------------------------- kinds
KIND_EXAMPLES = [
    ("whitespace", "a  b", "a b"),
    ("whitespace", "word\n", " word "),
    ("punctuation", "", ","),
    ("punctuation", "word.", "word,"),
    ("punctuation", "Hallo Welt", "Hallo, Welt"),
    ("capitalization", "hello", "Hello"),
    ("capitalization", "the river", "The River"),
    ("spelling", "teh", "the"),
    ("spelling", "Grossmutter", "Großmutter"),  # ß is not a case change
    ("spelling", "recieved", "received"),
    ("word_choice", "were", "was"),
    ("word_choice", "very very", "extremely"),
    ("word_choice", "at this point", "now"),
    ("insertion", "", "a "),
    ("insertion", " ", "sehr "),
    ("deletion", "the ", ""),
    ("deletion", " die, dass", "  "),
    ("rewrite", "in the event that it might possibly rain", "in case it rained"),
    ("rewrite", "Die Tatsache der Sachlage", "Tatsächlich, so"),
    ("rewrite", "at this point in", "now"),  # one side longer than three words
]


@pytest.mark.parametrize("kind, original, proposed", KIND_EXAMPLES, ids=[
    "{0}:{1!r}->{2!r}".format(*example) for example in KIND_EXAMPLES
])
def test_classify_change_examples(kind, original, proposed):
    assert classify_change(original, proposed) == kind


def test_change_kinds_are_fixed_order_and_labelled():
    assert CHANGE_KINDS == (
        "whitespace", "punctuation", "capitalization", "spelling",
        "word_choice", "insertion", "deletion", "rewrite",
    )
    assert WORKFLOW_CHANGE_KINDS is CHANGE_KINDS
    assert set(CHANGE_KIND_LABELS) == set(CHANGE_KINDS)
    assert all(classify_change(o, p) in CHANGE_KINDS for _, o, p in KIND_EXAMPLES)


def test_levenshtein_helper():
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "") == 3
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("teh", "the") == 2
    assert levenshtein("flaw", "lawn") == 2
    assert levenshtein("Straße", "Strasse") == 2


def test_mixed_german_sentence_is_classified_per_change():
    original = "Er wusste nicht ob der Zug kommt, und die Tatsache der Sachlage war die, dass er müde war."
    proposed = "Er wusste nicht, ob der Zug kommt, und tatsächlich war er müde."

    changes = extract_changes(original, proposed, CHECK_GRAMMAR)

    assert [(c.original_text, c.proposed_text, c.kind) for c in changes] == [
        ("", ",", "punctuation"),
        ("die Tatsache der Sachlage", "tatsächlich", "rewrite"),
        (" die, dass", "", "deletion"),
        (" war", "", "deletion"),
    ]

    typo = extract_changes("Er hatte kaum geschlaffen.", "Er hatte kaum geschlafen.", CHECK_SPELLING)
    assert [(c.original_text, c.kind) for c in typo] == [("geschlaffen", "spelling")]


def test_kind_round_trips_and_is_recomputed_when_missing_or_unknown():
    change = Change("grammar-1", CHECK_GRAMMAR, 15, 15, "", ",")
    assert change.kind == "punctuation"
    data = change.to_dict()
    assert data["kind"] == "punctuation"
    assert Change.from_dict(data) == change

    legacy = {key: value for key, value in data.items() if key != "kind"}
    assert Change.from_dict(legacy).kind == "punctuation"
    assert Change.from_dict(dict(data, kind="")).kind == "punctuation"
    assert Change.from_dict(dict(data, kind="banana")).kind == "punctuation"

    # A stored, valid kind is trusted even if the heuristic would now differ.
    assert Change.from_dict(dict(data, kind="rewrite")).kind == "rewrite"
    assert Change("x", CHECK_SPELLING, 0, 3, "teh", "the", kind="word_choice").kind == "word_choice"


def make_segment():
    text = "Teh dog were very very big and it run fast."
    segment = Segment(1, text)
    spelling = extract_changes(text, "The dog were very very big and it run fast.", CHECK_SPELLING)
    grammar = extract_changes(text, "Teh dog was very very big and it ran fast.", CHECK_GRAMMAR)
    expression = extract_changes(text, "Teh dog was enormous and it ran fast.", CHECK_EXPRESSION)
    segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", "", spelling)
    segment.results[CHECK_GRAMMAR] = CheckResult(CHECK_GRAMMAR, "done", "", grammar)
    segment.results[CHECK_EXPRESSION] = CheckResult(CHECK_EXPRESSION, "done", "", expression)
    return segment


def test_merge_applies_accepted_changes_and_supersedes_overlaps():
    segment = make_segment()
    for change in segment.changes(ALL):
        change.decision = ACCEPTED

    states = change_states(segment, ALL)
    grammar_was = next(c for c in segment.changes(ALL) if c.check == CHECK_GRAMMAR and c.original_text == "were")
    expression_was = next(c for c in segment.changes(ALL) if c.check == CHECK_EXPRESSION and c.original_text == "were")
    assert states[grammar_was.change_id] == STATE_APPLIED
    assert states[expression_was.change_id] == STATE_SUPERSEDED
    rendered = render_segment(segment, ALL)
    assert rendered.startswith("The dog was ")
    assert "ran fast" in rendered
    assert "very very" not in rendered  # expression's non-overlapping change applied
    assert segment.status(ALL) == STATUS_REVIEWED


def test_rejecting_the_winner_lets_the_lower_priority_change_apply():
    segment = make_segment()
    for change in segment.changes(ALL):
        change.decision = ACCEPTED
    grammar_was = next(c for c in segment.changes(ALL) if c.check == CHECK_GRAMMAR and c.original_text == "were")
    grammar_was.decision = REJECTED

    states = change_states(segment, ALL)
    expression_was = next(c for c in segment.changes(ALL) if c.check == CHECK_EXPRESSION and c.original_text == "were")
    assert states[grammar_was.change_id] == STATE_REJECTED
    assert states[expression_was.change_id] == STATE_APPLIED
    assert "was" in render_segment(segment, ALL)


def test_disabled_checks_are_ignored_everywhere():
    segment = make_segment()
    for change in segment.changes(ALL):
        change.decision = ACCEPTED
    enabled = {CHECK_SPELLING: True, CHECK_GRAMMAR: False, CHECK_EXPRESSION: False}

    assert render_segment(segment, enabled) == "The dog were very very big and it run fast."
    assert all(c.check == CHECK_SPELLING for c in segment.changes(enabled))
    assert segment.status(enabled) == STATUS_REVIEWED
    assert segment.status({CHECK_SPELLING: True, CHECK_GRAMMAR: True, CHECK_EXPRESSION: True}) == STATUS_REVIEWED


def test_segment_status_progression():
    segment = Segment(1, "Some text.")
    assert segment.status(ALL) == STATUS_QUEUED
    segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", "Some text.", [])
    segment.results[CHECK_GRAMMAR] = CheckResult(CHECK_GRAMMAR, "error", error="boom")
    assert segment.status({CHECK_SPELLING: True, CHECK_GRAMMAR: False, CHECK_EXPRESSION: False}) == STATUS_CLEAN
    assert segment.status({CHECK_SPELLING: True, CHECK_GRAMMAR: True, CHECK_EXPRESSION: False}) == STATUS_ERROR
    segment.results[CHECK_GRAMMAR] = CheckResult(
        CHECK_GRAMMAR, "done", "Some texts.", [Change("grammar-1", CHECK_GRAMMAR, 5, 9, "text", "texts")]
    )
    enabled = {CHECK_SPELLING: True, CHECK_GRAMMAR: True, CHECK_EXPRESSION: False}
    assert segment.status(enabled) == STATUS_READY
    segment.find_change("grammar-1").decision = REJECTED
    assert segment.status(enabled) == STATUS_REVIEWED
    assert Segment(2, "   \n").status(ALL) == STATUS_CLEAN


# ---------------------------------------------------------- explanations
def test_parse_explanations_accepts_common_shapes():
    assert parse_explanations('{"1": "Typo.", "2": "Agreement."}') == {1: "Typo.", 2: "Agreement."}
    assert parse_explanations('```json\n{"1": "Typo."}\n```') == {1: "Typo."}
    assert parse_explanations('Sure! {"explanations": ["One", "Two"]} done') == {1: "One", 2: "Two"}
    assert parse_explanations('{"1": {"explanation": "Nested"}, "x": "ignored", "2": ""}') == {1: "Nested"}
    assert parse_explanations("no json here") == {}
    assert strip_fences("```\ntext\n```") == "text"
    assert strip_fences("plain") == "plain"


def test_explanation_prompt_lists_changes_with_context():
    text = "Teh dog sat."
    changes = extract_changes(text, "The dog sat.", CHECK_SPELLING)
    messages = build_explanation_messages(text, changes, CHECK_SPELLING, language="German")
    content = messages[1]["content"]
    assert '1. "Teh" → "The"' in content
    assert "in German" in content
    assert "Text:\nTeh dog sat." in content


def test_parse_outline():
    starts, titles = parse_outline('{"chapters": [{"start": 0, "title": "A"}, {"start": 12, "title": "B"}, {"start": "x"}]}')
    assert starts == [0, 12] and titles == ["A", "B"]
    assert parse_outline("garbage") == ([], [])


def test_sanity_check_rejects_summaries_and_empty_answers():
    with pytest.raises(Exception):
        sanity_check_proposal("A long paragraph of text that goes on and on and on.", "Summary.")
    with pytest.raises(Exception):
        sanity_check_proposal("text", "")
    sanity_check_proposal("Almost same text.", "Almost same text!")


# ------------------------------------------------------------ evaluation
class FakeService:
    """Deterministic backend: fixes 'teh', 'were' and 'very very'; explains in JSON."""

    display_name = "fake"

    def __init__(self, fail_first=0, truncate=False, delay=0.0, usage=None):
        self.calls = []
        self.instructions = []  # full instruction of every edit request
        self.prompts = []  # full user message of every explanation request
        self.fail_first = fail_first
        self.truncate = truncate
        self.delay = delay
        self.usage = usage  # (prompt_tokens, completion_tokens) reported for every answered request
        self.lock = threading.Lock()

    def _report(self, on_usage, model):
        if on_usage is not None and self.usage is not None:
            on_usage(UsageRecord(self.usage[0], self.usage[1], 0.5, model))

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None):
        with self.lock:
            self.calls.append(("edit", instruction[:20], text, text_first))
            self.instructions.append(instruction)
            if self.fail_first > 0:
                self.fail_first -= 1
                raise BackendUnavailable("transient")
        if self.truncate:
            raise OutputTruncated("cut")
        if self.delay:
            if cancel_event.wait(self.delay):
                raise EditCancelled("cancelled")
        self._report(on_usage, model)
        if instruction.startswith("Correct spelling"):
            return text.replace("teh", "the").replace("Teh", "The")
        if instruction.startswith("Fix grammar"):
            return text.replace("were", "was")
        return "```\n" + text.replace("very very", "extremely") + "\n```"

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None):
        content = messages[1]["content"]
        self._report(on_usage, model)
        if content.startswith("Review the text below"):
            with self.lock:
                self.calls.append(("combined", content[:20], response_format is not None))
            return self.combined_answer(content, response_format)
        with self.lock:
            self.calls.append(("explain", content[:20], None))
            self.prompts.append(content)
        count = content.count("→")
        return json.dumps({str(n): "Reason {0}".format(n) for n in range(1, count + 1)})

    def combined_answer(self, content, response_format):
        """Answer a combined request: edits for the known test sentence, nothing otherwise."""
        edits = []
        if "Teh dog were very very big." in content:
            edits = [
                {"category": "spelling", "original": "Teh", "replacement": "The", "reason": "Typo."},
                {"category": "grammar", "original": "dog were", "replacement": "dog was", "reason": "Agreement."},
                {"category": "expression", "original": "very very big", "replacement": "extremely big",
                 "reason": "Repetition."},
            ]
        return json.dumps({"edits": edits})


class GarbageCombinedService(FakeService):
    """Answers the combined request with prose so the parser must give up."""

    def combined_answer(self, content, response_format):
        return "Sure! Here are my thoughts on the text: it reads fine overall."


class NoSchemaService(FakeService):
    """Rejects response_format like a server without guided decoding, answers plain requests."""

    def combined_answer(self, content, response_format):
        if response_format is not None:
            raise StructuredOutputUnsupported("Relay error 400: response_format is not supported")
        return super().combined_answer(content, response_format)


# ----------------------------------------------------------- style guide
GUIDE = "British spelling\nkeep dialect inside dialogue\nnever touch quotations"
GUIDE_BLOCK = "\n\n" + STYLE_GUIDE_HEADER + "\n" + GUIDE


def test_check_instruction_without_style_guide_is_the_plain_instruction():
    for check in CHECKS:
        assert build_check_instruction(check) == CHECK_INSTRUCTIONS[check]
        assert build_check_instruction(check, ProjectOptions()) == CHECK_INSTRUCTIONS[check]
        assert build_check_instruction(check, style_guide="  \n\t ") == CHECK_INSTRUCTIONS[check]
    assert format_style_guide("") == "" and format_style_guide(None) == ""


def test_check_instruction_appends_the_authors_rules_one_per_line():
    options = ProjectOptions(style_guide=GUIDE)
    instruction = build_check_instruction(CHECK_GRAMMAR, options)
    assert instruction == CHECK_INSTRUCTIONS[CHECK_GRAMMAR] + GUIDE_BLOCK
    assert instruction.startswith(CHECK_INSTRUCTIONS[CHECK_GRAMMAR])
    # an explicit string wins over the options
    assert build_check_instruction(CHECK_GRAMMAR, options, style_guide="x") == CHECK_INSTRUCTIONS[CHECK_GRAMMAR] + (
        "\n\n" + STYLE_GUIDE_HEADER + "\nx"
    )


def test_style_guide_is_normalised_to_one_rule_per_line():
    typed = "  British spelling · keep   dialect inside dialogue  \n\n\r\n never touch quotations \n"
    assert normalise_style_guide(typed) == GUIDE
    assert format_style_guide(typed) == GUIDE_BLOCK


def test_style_guide_is_trimmed_to_the_limit():
    long_rules = "\n".join("rule number {0} is rather long".format(n) for n in range(200))
    assert len(long_rules) > STYLE_GUIDE_MAX_CHARS
    trimmed = normalise_style_guide(long_rules)
    assert len(trimmed) <= STYLE_GUIDE_MAX_CHARS == 1500
    assert long_rules.startswith(trimmed)
    assert build_check_instruction(CHECK_SPELLING, style_guide=long_rules).endswith(trimmed)


def test_explanation_prompt_carries_the_authors_rules():
    text = "Teh colour."
    changes = extract_changes(text, "The colour.", CHECK_SPELLING)
    content = build_explanation_messages(text, changes, CHECK_SPELLING, style_guide=GUIDE)[1]["content"]
    assert GUIDE_BLOCK in content
    assert content.index(STYLE_GUIDE_HEADER) < content.index("Text:\nTeh colour.")
    plain = build_explanation_messages(text, changes, CHECK_SPELLING)[1]["content"]
    assert STYLE_GUIDE_HEADER not in plain


def test_style_guide_round_trips_through_project_options_and_project(tmp_path):
    options = ProjectOptions(style_guide=GUIDE)
    assert options.to_dict()["style_guide"] == GUIDE
    assert ProjectOptions.from_dict(options.to_dict()).style_guide == GUIDE

    legacy = {key: value for key, value in options.to_dict().items() if key != "style_guide"}
    assert ProjectOptions.from_dict(legacy).style_guide == ""
    assert ProjectOptions.from_dict(dict(legacy, style_guide=None)).style_guide == ""

    source = tmp_path / "novel.txt"
    source.write_text("Some text.", encoding="utf-8")
    project = create_project(source, "Some text.", options, model="m", backend="fake")
    data = project.to_dict()
    assert data["options"]["style_guide"] == GUIDE
    assert Project.from_dict(data).options.style_guide == GUIDE
    del data["options"]["style_guide"]
    assert Project.from_dict(data).options.style_guide == ""


def test_run_check_sends_the_authors_rules_with_edit_and_explanation():
    service = FakeService()
    result = run_check(service, "m", "Teh dog.", CHECK_SPELLING, threading.Event(), style_guide=GUIDE)

    assert result.status == "done" and len(result.changes) == 1
    assert service.instructions == [CHECK_INSTRUCTIONS[CHECK_SPELLING] + GUIDE_BLOCK]
    assert GUIDE_BLOCK in service.prompts[0]
    # keyword stays optional: positional callers are unaffected
    assert run_check(FakeService(), "m", "Teh dog.", CHECK_SPELLING, threading.Event(), True, "German", 1).status == "done"


def test_runner_passes_the_project_style_guide_to_every_check(tmp_path):
    project = make_project(tmp_path, style_guide=GUIDE)
    events = queue.Queue()
    service = FakeService()
    ProjectRunner(project, service, "m", events, parallelism=2).start()
    drain(events)

    assert service.instructions and all(GUIDE_BLOCK in instruction for instruction in service.instructions)
    assert service.prompts and all(GUIDE_BLOCK in prompt for prompt in service.prompts)


# -------------------------------------------------------------- glossary
def test_parse_glossary_trims_dedupes_and_accepts_lists():
    assert parse_glossary("Thalbrück\n\n  Meret   Aubinger \nThalbrück\n*\nhyper*") == [
        "Thalbrück", "Meret Aubinger", "hyper*"
    ]
    assert parse_glossary("a · b") == ["a", "b"]
    assert parse_glossary(["x", " y ", "", "x"]) == ["x", "y"]
    assert parse_glossary(None) == [] and parse_glossary("") == []


def test_glossary_matcher_semantics():
    hit = glossary_matcher(["Thalbrück", "Meret Aubinger", "hyper*", "C++", "Straße"])
    # exact terms need word boundaries on both sides
    assert hit("nach Thalbrück fahren") and hit("Thalbrück,") and hit("(Thalbrück)")
    assert not hit("Thalbrücker") and not hit("Neuthalbrück")
    # case matters: a protected name keeps its capitalization
    assert not hit("thalbrück") and not hit("THALBRÜCK")
    # umlauts and ß are word characters, not boundaries
    assert hit("die Straße") and not hit("Straßenbahn") and not hit("Strasse")
    # multi-word terms match as a phrase only
    assert hit("Meret Aubinger kam") and not hit("Meret kam") and not hit("Aubinger")
    # wildcard: word-boundary at the start, any suffix
    assert hit("hyperdrive") and hit("hyper") and hit("hyper-drive")
    assert not hit("superhyper") and not hit("ahyperb")
    # punctuation inside terms is escaped
    assert hit("C++ code") and not hit("C code") and not hit("xC++")
    assert not hit("") and not glossary_matcher([])("Thalbrück") and not glossary_matcher(["*"])("anything")


def test_post_filter_drops_changes_touching_protected_terms():
    text = "Teh Kestenholz dog were very very big."
    changes = extract_changes(text, "The Kestenholz dog was extremely big.", CHECK_GRAMMAR)
    kept, suppressed = suppress_glossary_changes(text, changes, glossary_matcher(["Teh", "very*"]))
    assert [c.original_text for c in kept] == ["were"]
    assert [c.original_text for c in suppressed] == ["Teh", "very very"]
    assert all(c.explanation == GLOSSARY_EXPLANATION for c in suppressed)

    # an insertion is dropped only when a protected term sits within 20 characters
    near = "I saw Kestenholz there"
    inserted = extract_changes(near, "I saw a Kestenholz there", CHECK_GRAMMAR)
    assert inserted[0].original_text == ""
    assert suppress_glossary_changes(near, inserted, glossary_matcher(["Kestenholz"])) == ([], inserted)
    far = "I saw dog. " + "x" * 30 + " Kestenholz"
    inserted = extract_changes(far, far.replace("saw dog", "saw a dog"), CHECK_GRAMMAR)
    assert suppress_glossary_changes(far, inserted, glossary_matcher(["Kestenholz"])) == (inserted, [])
    # no glossary: nothing is touched
    assert suppress_glossary_changes(text, changes, glossary_matcher([])) == (changes, [])


def test_run_check_suppresses_and_keeps_the_dropped_changes():
    service = FakeService()
    result = run_check(service, "m", "Teh dog were big.", CHECK_SPELLING, threading.Event(), glossary=["Teh"])

    assert result.status == "done"
    assert result.changes == []
    assert [c.original_text for c in result.suppressed] == ["Teh"]
    assert service.prompts == []  # nothing left to explain
    assert GLOSSARY_HEADER + "Teh" in service.instructions[0]

    result = run_check(FakeService(), "m", "Teh dog were big.", CHECK_SPELLING, threading.Event(), glossary=["Kestenholz"])
    assert [c.original_text for c in result.changes] == ["Teh"] and result.suppressed == []


def test_suppressed_changes_round_trip_and_are_counted(tmp_path):
    result = run_check(FakeService(), "m", "Teh dog.", CHECK_SPELLING, threading.Event(), glossary=["Teh"])
    data = result.to_dict()
    assert data["suppressed"][0]["original"] == "Teh"
    restored = CheckResult.from_dict(data)
    assert [c.to_dict() for c in restored.suppressed] == data["suppressed"]
    legacy = {key: value for key, value in data.items() if key != "suppressed"}
    assert CheckResult.from_dict(legacy).suppressed == []

    project = make_project(tmp_path, glossary=["Teh", "very*"])
    events = queue.Queue()
    ProjectRunner(project, FakeService(), "m", events, parallelism=2).start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    stats = project.progress()
    assert stats["suppressed"] >= 2  # "Teh" (spelling) and "very very" (expression)
    assert stats["per_check"][CHECK_SPELLING]["suppressed"] == 1
    assert stats["per_check"][CHECK_EXPRESSION]["suppressed"] == 1
    assert stats["per_check"][CHECK_GRAMMAR]["suppressed"] == 0
    assert stats["suppressed"] == sum(per["suppressed"] for per in stats["per_check"].values())
    assert not any("Teh" in c.original_text for _, s in project.all_segments() for c in s.changes(ALL))
    report = project.build_report()
    assert "  - Spelling: 0 proposed, 0 accepted, 0 rejected\n    - Glossary suppressed 1 proposed change(s)" in report
    assert "  - Grammar:" in report and report.count("Glossary suppressed") == 2

    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.options.glossary == ["Teh", "very*"]
    assert reloaded.progress()["suppressed"] == stats["suppressed"]


def test_glossary_option_round_trips_and_defaults_to_empty():
    options = ProjectOptions(glossary=["Thalbrück", "hyper*"])
    assert options.to_dict()["glossary"] == ["Thalbrück", "hyper*"]
    assert ProjectOptions.from_dict(options.to_dict()).glossary == ["Thalbrück", "hyper*"]
    legacy = {key: value for key, value in options.to_dict().items() if key != "glossary"}
    assert ProjectOptions.from_dict(legacy).glossary == []
    assert ProjectOptions.from_dict(dict(legacy, glossary=None)).glossary == []
    assert ProjectOptions.from_dict(dict(legacy, glossary="a\nb")).glossary == ["a", "b"]  # tolerant of text


def test_prompt_lists_protected_terms_up_to_the_caps():
    instruction = build_check_instruction(CHECK_SPELLING, glossary=["Thalbrück", "hyper*"], style_guide="British spelling")
    assert instruction.endswith(GLOSSARY_HEADER + "Thalbrück, hyper*")
    assert instruction.index(STYLE_GUIDE_HEADER) < instruction.index(GLOSSARY_HEADER)
    assert build_check_instruction(CHECK_SPELLING, glossary=[]) == CHECK_INSTRUCTIONS[CHECK_SPELLING]
    assert format_glossary(None) == ""

    many = ["term{0}".format(n) for n in range(GLOSSARY_MAX_TERMS + 50)]
    listed = glossary_prompt_terms(many)
    assert listed == many[:GLOSSARY_MAX_TERMS]
    assert "term{0}".format(GLOSSARY_MAX_TERMS) not in format_glossary(many)

    long_terms = ["{0}{1}".format("x" * 150, n) for n in range(30)]
    block = format_glossary(long_terms)
    assert len(block) - len("\n\n" + GLOSSARY_HEADER) <= GLOSSARY_MAX_CHARS
    assert long_terms[0] in block and long_terms[-1] not in block

    # beyond the caps the post-filter still protects the term
    hit = glossary_matcher(many + ["Kestenholz"])
    assert hit("Kestenholz") and "Kestenholz" not in format_glossary(many + ["Kestenholz"])


# ---------------------------------------------------- hallucination guard
def _single(segment, proposed, check):
    changes = extract_changes(segment, proposed, check)
    assert len(changes) == 1, changes
    return changes[0]


def test_growth_rule_fires_on_added_clauses():
    segment = "The dog sat on the mat."
    change = _single(segment, "The dog sat on the mat, watching the stranger approach slowly.", CHECK_EXPRESSION)
    assert change.original_text == "" and len(change.proposed_text) > 12
    assert flag_suspicious(segment, change) == FLAG_GROWTH

    # a long replacement of a short phrase also counts
    change = Change("x", CHECK_EXPRESSION, 4, 7, "sat", "settled down comfortably for the evening")
    assert flag_suspicious(segment, change) == FLAG_GROWTH
    # exactly at the limit does not fire: 1.6 * 3 + 12 = 16.8 → 16 characters are fine
    assert flag_suspicious(segment, Change("x", CHECK_EXPRESSION, 4, 7, "sat", "s" * 16)) != FLAG_GROWTH


def test_novel_words_rule_needs_two_unknown_content_words():
    segment = "The dog sat on the mat while the children played."
    # two content words absent from the segment, but not long enough to trip growth
    change = Change("x", CHECK_EXPRESSION, 4, 7, "dog", "golden retriever")
    assert flag_suspicious(segment, change) == FLAG_NOVEL_WORDS
    assert novel_words(segment, "golden retriever") == ["golden", "retriever"]
    # one novel word is a normal rephrase
    assert flag_suspicious(segment, Change("x", CHECK_EXPRESSION, 4, 7, "dog", "the retriever")) is None
    # stopwords and words already in the segment (any case) never count
    assert novel_words(segment, "While THE Children played") == []
    assert novel_words("Er kam nach Hause.", "obwohl er dennoch nach Hause kam") == []
    assert flag_suspicious(segment, Change("x", CHECK_GRAMMAR, 0, 3, "The", "Although they")) is None


def test_rewrite_rule_only_fires_for_strict_checks():
    segment = "Die Tatsache der Sachlage war die, dass er müde war."
    proposed = "Tatsächlich war er müde."
    grammar = extract_changes(segment, proposed, CHECK_GRAMMAR)
    rewrite = next(c for c in grammar if c.kind == "rewrite")
    assert flag_suspicious(segment, rewrite) == FLAG_REWRITE_IN_STRICT_CHECK
    assert [flag_suspicious(segment, c) for c in grammar if c.kind != "rewrite"] == [None, None]
    spelling = next(c for c in extract_changes(segment, proposed, CHECK_SPELLING) if c.kind == "rewrite")
    assert flag_suspicious(segment, spelling) == FLAG_REWRITE_IN_STRICT_CHECK
    expression = next(c for c in extract_changes(segment, proposed, CHECK_EXPRESSION) if c.kind == "rewrite")
    assert flag_suspicious(segment, expression) is None


def test_ordinary_corrections_are_not_flagged():
    assert flag_suspicious("Teh dog sat.", _single("Teh dog sat.", "The dog sat.", CHECK_SPELLING)) is None
    german = "Er wusste nicht ob der Zug kommt."
    assert flag_suspicious(german, _single(german, "Er wusste nicht, ob der Zug kommt.", CHECK_GRAMMAR)) is None
    segment = "It were very very big."
    assert flag_suspicious(segment, _single(segment, "It were extremely big.", CHECK_EXPRESSION)) is None
    assert flag_suspicious(segment, Change("x", CHECK_EXPRESSION, 8, 17, "very very", "really quite")) is None
    assert flag_suspicious("He run fast.", _single("He run fast.", "He ran fast.", CHECK_GRAMMAR)) is None
    assert flag_suspicious("a b", Change("x", CHECK_GRAMMAR, 1, 2, " b", "")) is None  # deletion


def test_flags_persist_and_default_to_empty():
    change = Change("g-1", CHECK_GRAMMAR, 0, 3, "dog", "golden retriever", flags=[FLAG_NOVEL_WORDS])
    assert change.flagged and change.to_dict()["flags"] == [FLAG_NOVEL_WORDS]
    assert Change.from_dict(change.to_dict()) == change
    legacy = {key: value for key, value in change.to_dict().items() if key != "flags"}
    restored = Change.from_dict(legacy)
    assert restored.flags == [] and not restored.flagged
    assert Change.from_dict(dict(legacy, flags=None)).flags == []
    plain = Change("g-2", CHECK_GRAMMAR, 0, 3, "teh", "the")
    assert plain.flags == [] and plain.to_dict()["flags"] == []


class InventingService(FakeService):
    """Fake backend whose expression check appends a clause the text never had."""

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None):
        if instruction.startswith("Improve expression"):
            return text.replace("It run fast.", "It run fast, chasing the stranger.")
        return super().stream_edit(model, instruction, text, cancel_event, on_progress, text_first)


def test_run_check_flags_after_the_glossary_filter_and_reports(tmp_path):
    text = "Teh dog were very very big. It run fast."
    result = run_check(InventingService(), "m", text, CHECK_EXPRESSION, threading.Event())
    flagged = [c for c in result.changes if c.flagged]
    assert len(flagged) == 1 and flagged[0].flags == [FLAG_GROWTH]
    assert "stranger" in flagged[0].proposed_text
    assert result.status == "done" and len(result.changes) == 1

    # a suppressed change is not flagged (the glossary filter runs first; "fast" sits next to the insertion)
    result = run_check(InventingService(), "m", text, CHECK_EXPRESSION, threading.Event(), glossary=["fast"])
    assert result.suppressed and not any(c.flagged for c in result.suppressed)
    assert not any(c.flagged for c in result.changes)

    project = make_project(tmp_path)
    events = queue.Queue()
    ProjectRunner(project, InventingService(), "m", events, parallelism=2).start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    stats = project.progress()
    assert stats["flagged"] == stats["flagged_pending"] == 1
    assert stats["per_check"][CHECK_EXPRESSION]["flagged"] == 1
    assert stats["per_check"][CHECK_SPELLING]["flagged"] == 0
    report = project.build_report()
    assert FLAG_REPORT_MARK + " (growth)" in report
    assert "    - " + FLAG_REPORT_MARK + ": 1 change(s) flagged" in report
    assert report.count(FLAG_REPORT_MARK) == 2

    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.progress()["flagged"] == 1
    the_change = next(c for _, s in reloaded.all_segments() for c in s.changes(ALL) if c.flagged)
    the_change.decision = REJECTED
    assert reloaded.progress()["flagged_pending"] == 0 and reloaded.progress()["flagged"] == 1


def test_flag_changes_is_idempotent():
    segment = "The dog sat on the mat."
    change = Change("x", CHECK_EXPRESSION, 4, 7, "dog", "golden retriever")
    assert flag_changes(segment, [change]) == [change]
    flag_changes(segment, [change])
    assert change.flags == [FLAG_NOVEL_WORDS]


def test_instructions_forbid_invented_content():
    assert CHECK_INSTRUCTIONS[CHECK_EXPRESSION].endswith(
        "Never add facts, names, clauses or sentences that are not already in the text."
    )
    assert CHECK_INSTRUCTIONS[CHECK_GRAMMAR].endswith("Do not rewrite sentences.")


def test_run_check_produces_changes_with_explanations_and_strips_fences():
    service = FakeService()
    result = run_check(service, "m", "It were very very big.", CHECK_EXPRESSION, threading.Event())

    # Review edits put the segment before the instruction (prefix-cache friendly).
    assert service.calls[0] == ("edit", "Improve expression o", "It were very very big.", True)
    # Without author's instructions the plain check instruction is sent.
    assert service.instructions == [CHECK_INSTRUCTIONS[CHECK_EXPRESSION]]
    assert STYLE_GUIDE_HEADER not in service.prompts[0]

    assert result.status == "done"
    assert result.proposed_text == "It were extremely big."
    assert [c.explanation for c in result.changes] == ["Reason 1"]
    assert result.explained is True
    assert result.duration >= 0
    assert [call[0] for call in service.calls] == ["edit", "explain"]


def test_run_check_retries_transient_errors_but_not_truncation():
    service = FakeService(fail_first=1)
    result = run_check(service, "m", "Teh dog.", CHECK_SPELLING, threading.Event())
    assert result.status == "done"
    assert len([c for c in service.calls if c[0] == "edit"]) == 2

    service = FakeService(fail_first=2)
    result = run_check(service, "m", "Teh dog.", CHECK_SPELLING, threading.Event(), retries=1)
    assert result.status == "error" and "transient" in result.error

    result = run_check(FakeService(truncate=True), "m", "Teh dog.", CHECK_SPELLING, threading.Event())
    assert result.status == "error" and "cut" in result.error


def test_run_check_without_explanations_uses_fallback_text():
    result = run_check(FakeService(), "m", "Teh dog.", CHECK_SPELLING, threading.Event(), explain=False)
    assert result.changes[0].explanation == "Spelling correction."
    assert result.explained is False


def test_run_check_propagates_cancellation():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EditCancelled):
        run_check(FakeService(delay=1), "m", "Teh dog.", CHECK_SPELLING, cancel)


# ------------------------------------------------------- combined pass
GERMAN = (
    "Er hatte in der Nacht kaum geschlaffen und wusste nicht ob der Zug kommt. "
    "Die Tatsache der Sachlage war die, dass er müde war. Er ging nach Hause, und er ging nach Hause."
)


def _answer(edits):
    return json.dumps({"edits": edits}, ensure_ascii=False)


def test_combined_prompt_lists_rules_text_shape_and_author_settings():
    options = ProjectOptions(style_guide="British spelling", glossary=["Thalbrück"], language="German")
    system, user = build_combined_messages("Some text.", options)
    assert "single JSON object" in system["content"]
    content = user["content"]
    for number, check in enumerate(CHECKS, 1):
        assert "{0}. {1}: {2}".format(number, check, CHECK_INSTRUCTIONS[check]) in content
    assert content.index(STYLE_GUIDE_HEADER) < content.index(GLOSSARY_HEADER) < content.index("Text:\nSome text.")
    assert '"edits"' in content and "exact substring" in content and "in German" in content
    only_spelling = ProjectOptions(checks={CHECK_SPELLING: True, CHECK_GRAMMAR: False, CHECK_EXPRESSION: False})
    assert "2. grammar" not in build_combined_messages("x", only_spelling)[1]["content"]


def test_parse_combined_realistic_german_answer():
    answer = _answer([
        {"category": "spelling", "original": "geschlaffen", "replacement": "geschlafen", "reason": "Tippfehler."},
        {"category": "grammar", "original": "nicht ob", "replacement": "nicht, ob", "reason": "Komma vor dem Nebensatz."},
        {"category": "expression", "original": "Die Tatsache der Sachlage war die, dass er müde war.",
         "replacement": "Tatsächlich war er müde.", "reason": "Umständliche Formulierung."},
        {"category": "grammar", "original": "Hause, und er ging", "replacement": "Hause und er ging",
         "reason": "Kein Komma vor und."},
    ])
    results = parse_combined(GERMAN, answer, ProjectOptions())

    assert set(results) == set(CHECKS)
    assert all(result.status == "done" and result.method == EVALUATION_COMBINED for result in results.values())
    spelling = results[CHECK_SPELLING].changes
    assert [(c.original_text, c.proposed_text, c.kind, c.explanation) for c in spelling] == [
        ("geschlaffen", "geschlafen", "spelling", "Tippfehler.")
    ]
    assert GERMAN[spelling[0].start:spelling[0].end] == "geschlaffen"
    grammar = results[CHECK_GRAMMAR].changes
    assert [(c.original_text, c.proposed_text, c.kind) for c in grammar] == [("", ",", "punctuation"), (",", "", "punctuation")]
    assert [c.change_id for c in grammar] == ["grammar-1", "grammar-2"]
    assert grammar[0].explanation == "Komma vor dem Nebensatz." and grammar[1].explanation == "Kein Komma vor und."
    expression = results[CHECK_EXPRESSION].changes
    assert expression[0].kind == "rewrite" and not expression[0].flagged  # rewrites are fine for expression
    assert results[CHECK_EXPRESSION].proposed_text.startswith(
        "Er hatte in der Nacht kaum geschlaffen und wusste nicht ob der Zug kommt. Tatsächlich war er müde."
    )
    assert results[CHECK_SPELLING].proposed_text == GERMAN.replace("geschlaffen", "geschlafen")
    assert all(result.explained for result in results.values())
    for result in results.values():
        for change in result.changes:
            assert GERMAN[change.start:change.end] == change.original_text


def test_parse_combined_anchors_repeated_words_through_context():
    # "nach Hause" occurs twice; the model disambiguated with the surrounding words
    answer = _answer([
        {"category": "expression", "original": "und er ging nach Hause", "replacement": "und er ging heim", "reason": "Kürzer."},
    ])
    results = parse_combined(GERMAN, answer, ProjectOptions())
    change = results[CHECK_EXPRESSION].changes[0]
    assert (change.original_text, change.proposed_text) == ("nach Hause", "heim")
    assert change.start == GERMAN.rindex("nach Hause")

    # ... but a bare repeated substring is ambiguous and cannot be anchored
    ambiguous = _answer([{"category": "expression", "original": "nach Hause", "replacement": "heim", "reason": "x"}])
    assert parse_combined(GERMAN, ambiguous, ProjectOptions()) is None


def test_parse_combined_gives_up_on_unanchorable_or_malformed_answers():
    good = {"category": "spelling", "original": "geschlaffen", "replacement": "geschlafen", "reason": "Tippfehler."}
    assert parse_combined(GERMAN, _answer([good, {"category": "grammar", "original": "nicht da", "replacement": "x", "reason": ""}])) is None
    assert parse_combined(GERMAN, _answer([{"category": "grammar", "original": "", "replacement": "x", "reason": ""}])) is None
    assert parse_combined(GERMAN, _answer([{"category": "layout", "original": "Zug", "replacement": "Bus", "reason": ""}])) is None
    assert parse_combined(GERMAN, _answer([{"category": "grammar", "original": 5, "replacement": "x", "reason": ""}])) is None
    assert parse_combined(GERMAN, "I cannot help with that.") is None
    assert parse_combined(GERMAN, '{"changes": []}') is None
    assert parse_combined(GERMAN, "```json\n" + _answer([good]) + "\n```") is not None  # fences are tolerated
    empty = parse_combined(GERMAN, '{"edits": []}', ProjectOptions())
    assert all(result.changes == [] and result.proposed_text == GERMAN for result in empty.values())
    # an unchanged "edit" and a disabled category are skipped, not fatal
    skipped = parse_combined(GERMAN, _answer([
        dict(good, replacement="geschlaffen"),
        {"category": "expression", "original": "müde", "replacement": "erschöpft", "reason": "x"},
    ]), ProjectOptions(checks={CHECK_SPELLING: True, CHECK_GRAMMAR: True, CHECK_EXPRESSION: False}))
    assert set(skipped) == {CHECK_SPELLING, CHECK_GRAMMAR} and skipped[CHECK_SPELLING].changes == []


def test_parse_combined_applies_glossary_and_hallucination_guard():
    answer = _answer([
        {"category": "spelling", "original": "geschlaffen", "replacement": "geschlafen", "reason": "Tippfehler."},
        {"category": "grammar", "original": "kommt.", "replacement": "kommt, obwohl der Schaffner und der Lokführer es versprochen hatten.",
         "reason": "Ergänzt."},
    ])
    results = parse_combined(GERMAN, answer, ProjectOptions(glossary=["geschlaffen"]))
    assert results[CHECK_SPELLING].changes == [] and len(results[CHECK_SPELLING].suppressed) == 1
    grammar = results[CHECK_GRAMMAR].changes
    assert grammar and grammar[0].flags == [FLAG_GROWTH]


def test_run_segment_combined_uses_one_request_and_marks_results():
    service = FakeService()
    text = "Teh dog were very very big. It run fast."
    results = run_segment_combined(service, "m", text, ProjectOptions(), threading.Event())

    assert [call[0] for call in service.calls] == ["combined"]
    assert service.calls[0][2] is True  # asked for the JSON schema
    assert all(result.method == EVALUATION_COMBINED and result.model == "m" for result in results.values())
    assert [c.proposed_text for c in results[CHECK_SPELLING].changes] == ["The"]
    assert [c.proposed_text for c in results[CHECK_GRAMMAR].changes] == ["was"]
    assert [c.proposed_text for c in results[CHECK_EXPRESSION].changes] == ["extremely"]
    assert results[CHECK_GRAMMAR].changes[0].explanation == "Agreement."


def test_run_segment_combined_falls_back_to_separate_checks_on_garbage():
    service = GarbageCombinedService()
    text = "Teh dog were very very big."
    results = run_segment_combined(service, "m", text, ProjectOptions(), threading.Event())

    assert [call[0] for call in service.calls] == ["combined", "edit", "explain", "edit", "explain", "edit", "explain"]
    assert all(result.status == "done" and result.method == EVALUATION_SEPARATE for result in results.values())
    assert [c.proposed_text for c in results[CHECK_SPELLING].changes] == ["The"]
    assert results[CHECK_SPELLING].changes[0].explanation == "Reason 1"


def test_run_segment_combined_reports_missing_structured_output_support():
    service = NoSchemaService()
    disabled = []
    results = run_segment_combined(
        service, "m", "Teh dog were very very big.", ProjectOptions(), threading.Event(),
        on_structured_unsupported=lambda: disabled.append(True),
    )
    assert disabled == [True]
    assert all(result.method == EVALUATION_SEPARATE for result in results.values())  # this segment fell back
    # without the schema the same service answers the combined request
    results = run_segment_combined(service, "m", "Teh dog were very very big.", ProjectOptions(), threading.Event(), structured=False)
    assert all(result.method == EVALUATION_COMBINED for result in results.values())


def test_combined_schema_and_method_persist():
    assert COMBINED_SCHEMA["properties"]["edits"]["items"]["properties"]["category"]["enum"] == list(CHECKS)
    result = CheckResult(CHECK_SPELLING, "done", method=EVALUATION_COMBINED)
    assert CheckResult.from_dict(result.to_dict()).method == EVALUATION_COMBINED
    legacy = {key: value for key, value in result.to_dict().items() if key != "method"}
    assert CheckResult.from_dict(legacy).method == EVALUATION_SEPARATE
    assert CheckResult.from_dict(dict(legacy, method="magic")).method == EVALUATION_SEPARATE
    options = ProjectOptions()
    assert options.evaluation_mode == EVALUATION_COMBINED and options.combined
    assert ProjectOptions.from_dict({"evaluation_mode": "separate"}).evaluation_mode == EVALUATION_SEPARATE
    assert ProjectOptions.from_dict({"evaluation_mode": "weird"}).evaluation_mode == EVALUATION_COMBINED
    assert ProjectOptions.from_dict({}).evaluation_mode == EVALUATION_COMBINED
    assert ProjectOptions.from_dict(ProjectOptions(evaluation_mode=EVALUATION_SEPARATE).to_dict()).evaluation_mode == EVALUATION_SEPARATE


FILLER = " ".join(["The evening settled quietly over the small town by the river."] * 5)
MANUSCRIPT = (
    "Kapitel 1\n\nTeh dog were very very big. It run fast.\n\n" + FILLER + "\n\n"
    "Kapitel 2\n\n" + FILLER + "\n\nNothing wrong here at all.\n"
)


def make_project(tmp_path, **option_overrides):
    source = tmp_path / "novel.txt"
    source.write_text(MANUSCRIPT, encoding="utf-8")
    option_overrides.setdefault("evaluation_mode", EVALUATION_SEPARATE)  # the classic per-check tasks
    options = ProjectOptions(target_chars=200, max_chars=400, parallelism=2, **option_overrides)
    return create_project(source, MANUSCRIPT, options, model="m", backend="fake")


def drain(events, timeout=10):
    finished = None
    collected = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        collected.append(event)
        if event[0] == "workflow_finished":
            finished = event
            break
    assert finished is not None, "runner never finished"
    return collected


def test_project_creation_splits_and_writes_chapter_files(tmp_path):
    project = make_project(tmp_path)

    assert project.method == "headings:chapter-word"
    assert [c.title for c in project.chapters] == ["Kapitel 1", "Kapitel 2"]
    assert project.root == tmp_path / "novel.teai"
    assert project.render_document() == MANUSCRIPT
    paths = project.write_chapter_files()
    assert [p.name for p in paths] == ["01 - Kapitel 1.txt", "02 - Kapitel 2.txt"]
    assert paths[0].read_text(encoding="utf-8").startswith("Kapitel 1\n\nTeh dog")
    assert len(project.pending_tasks()) == 3 * sum(len(c.segments) for c in project.chapters)


def test_runner_evaluates_everything_and_results_round_trip(tmp_path):
    project = make_project(tmp_path, auto_accept={CHECK_SPELLING: True})
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events)
    queued = runner.start()
    collected = drain(events)

    results = [event for event in collected if event[0] == "workflow_result"]
    assert len(results) == queued == runner.total
    for _, chapter_index, segment_index, check, result in results:
        apply_result(project, chapter_index, segment_index, check, result)
    assert collected[-1] == ("workflow_finished", False)
    assert project.pending_tasks() == []

    stats = project.progress()
    assert stats["tasks_done"] == stats["tasks_total"]
    assert stats["per_check"][CHECK_SPELLING]["accepted"] == stats["per_check"][CHECK_SPELLING]["changes"] >= 1
    assert stats["per_check"][CHECK_GRAMMAR]["changes"] >= 1
    assert stats["pending"] > 0  # grammar/expression are not auto-accepted
    assert set(stats["per_kind"]) == set(CHANGE_KINDS)
    assert sum(kind["changes"] for kind in stats["per_kind"].values()) == stats["changes"]
    assert stats["per_kind"]["spelling"]["accepted"] == stats["per_kind"]["spelling"]["changes"] >= 1  # Teh -> The
    assert stats["per_kind"]["word_choice"]["changes"] >= 2  # were -> was, very very -> extremely
    assert stats["per_check"][CHECK_SPELLING]["by_kind"]["spelling"] == stats["per_check"][CHECK_SPELLING]["changes"]
    assert stats["per_check"][CHECK_EXPRESSION]["by_kind"]["word_choice"] >= 1

    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.to_dict()["chapters"] == project.to_dict()["chapters"]
    assert reloaded.options.auto_accept[CHECK_SPELLING] is True
    assert reloaded.pending_tasks() == []

    for _, segment in reloaded.all_segments():
        for change in segment.changes(reloaded.enabled):
            change.decision = ACCEPTED
    paths = reloaded.export()
    document = paths["document"].read_text(encoding="utf-8")
    assert "The dog was extremely big." in document
    assert document.startswith("Kapitel 1\n\n")
    assert document.endswith("Nothing wrong here at all.\n")
    report = paths["report"].read_text(encoding="utf-8")
    assert "# Review report: novel" in report
    assert "[Spelling] `Teh` → `The` — applied — Reason" in report
    assert "  - Spelling: 1 proposed, 1 accepted, 0 rejected\n    - by kind: spelling 1\n" in report
    assert "    - by kind: word choice 1\n" in report  # expression: very very -> extremely
    assert (reloaded.root / "reviewed" / "01 - Kapitel 1.txt").exists()


def test_runner_can_be_cancelled_and_resumed(tmp_path):
    project = make_project(tmp_path)
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(delay=0.5), "m", events, parallelism=1)
    runner.start()
    time.sleep(0.1)
    runner.cancel()
    collected = drain(events)
    assert collected[-1] == ("workflow_finished", True)
    for event in collected:
        if event[0] == "workflow_result":
            apply_result(project, event[1], event[2], event[3], event[4])
    remaining = project.pending_tasks()
    assert 0 < len(remaining) <= runner.total

    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=3)
    assert runner.start() == len(remaining)
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, event[1], event[2], event[3], event[4])
    assert project.pending_tasks() == []


def test_runner_dispatches_all_checks_of_a_segment_consecutively(tmp_path):
    project = make_project(tmp_path)
    pending = project.pending_tasks()
    assert len(pending) > len(CHECKS)  # several segments
    # pending_tasks() is (chapter, segment, check) with checks innermost ...
    for index in range(0, len(pending), len(CHECKS)):
        group = pending[index:index + len(CHECKS)]
        assert {(chapter, segment) for chapter, segment, _ in group} == {group[0][:2]}
        assert [check for _, _, check in group] == list(CHECKS)
    assert [task[:2] for task in pending] == sorted(task[:2] for task in pending)

    # ... and the runner starts the tasks in exactly that order.
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=1)
    runner.start()
    started = [event[1:] for event in drain(events) if event[0] == "workflow_started"]
    assert started == pending


def test_runner_dispatches_all_checks_of_a_segment_consecutively(tmp_path):
    project = make_project(tmp_path)
    pending = project.pending_tasks()
    assert len(pending) > len(CHECKS)  # several segments
    # pending_tasks() is (chapter, segment, check) with checks innermost ...
    for index in range(0, len(pending), len(CHECKS)):
        group = pending[index:index + len(CHECKS)]
        assert {(chapter, segment) for chapter, segment, _ in group} == {group[0][:2]}
        assert [check for _, _, check in group] == list(CHECKS)
    assert [task[:2] for task in pending] == sorted(task[:2] for task in pending)

    # ... and the runner starts the tasks in exactly that order.
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=1)
    runner.start()
    started = [event[1:] for event in drain(events) if event[0] == "workflow_started"]
    assert started == pending


def test_runner_dispatches_all_checks_of_a_segment_consecutively(tmp_path):
    project = make_project(tmp_path)
    pending = project.pending_tasks()
    assert len(pending) > len(CHECKS)  # several segments
    # pending_tasks() is (chapter, segment, check) with checks innermost ...
    for index in range(0, len(pending), len(CHECKS)):
        group = pending[index:index + len(CHECKS)]
        assert {(chapter, segment) for chapter, segment, _ in group} == {group[0][:2]}
        assert [check for _, _, check in group] == list(CHECKS)
    assert [task[:2] for task in pending] == sorted(task[:2] for task in pending)

    # ... and the runner starts the tasks in exactly that order.
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=1)
    runner.start()
    started = [event[1:] for event in drain(events) if event[0] == "workflow_started"]
    assert started == pending


def test_pending_tasks_are_per_segment_in_combined_mode(tmp_path):
    project = make_project(tmp_path, evaluation_mode=EVALUATION_COMBINED)
    segments = [(c.index, s.index) for c, s in project.all_segments() if not s.is_blank]
    assert project.pending_tasks() == [(c, s, None) for c, s in segments]
    chapter, segment = project.find(*segments[0])
    segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", segment.text, [])
    segment.results[CHECK_GRAMMAR] = CheckResult(CHECK_GRAMMAR, "error", error="boom")
    assert project.pending_checks(segment) == [CHECK_GRAMMAR, CHECK_EXPRESSION]
    assert project.pending_tasks()[0] == (chapter.index, segment.index, None)
    segment.results[CHECK_GRAMMAR] = CheckResult(CHECK_GRAMMAR, "done", segment.text, [])
    segment.results[CHECK_EXPRESSION] = CheckResult(CHECK_EXPRESSION, "done", segment.text, [])
    assert project.pending_tasks()[0] == (segments[1][0], segments[1][1], None)
    project.options.evaluation_mode = EVALUATION_SEPARATE
    assert all(check is not None for _, _, check in project.pending_tasks())


def test_runner_emits_three_results_per_segment_in_combined_mode(tmp_path):
    project = make_project(tmp_path, evaluation_mode=EVALUATION_COMBINED)
    events = queue.Queue()
    service = FakeService()
    runner = ProjectRunner(project, service, "m", events, parallelism=2)
    queued = runner.start()
    collected = drain(events)

    segments = [(c.index, s.index) for c, s in project.all_segments() if not s.is_blank]
    assert queued == runner.total == len(segments)
    started = [event[1:] for event in collected if event[0] == "workflow_started"]
    results = [event for event in collected if event[0] == "workflow_result"]
    assert len(started) == len(results) == 3 * len(segments)
    assert sorted(started) == sorted((c, s, check) for c, s in segments for check in CHECKS)
    for _, chapter_index, segment_index, check, result in results:
        assert result.check == check and result.method == EVALUATION_COMBINED
        apply_result(project, chapter_index, segment_index, check, result)
    assert [call[0] for call in service.calls].count("combined") == len(segments)
    assert project.pending_tasks() == []
    progress = [event for event in collected if event[0] == "workflow_progress"]
    assert progress[-1][1:3] == (len(segments), len(segments))
    chapter, segment = project.find(1, 1)
    assert [c.proposed_text for c in segment.changes(ALL)] == ["The", "was", "extremely"]
    assert runner.structured_output is True
    stats = project.progress()
    assert stats["methods"] == {EVALUATION_COMBINED: 3 * len(segments), EVALUATION_SEPARATE: 0}
    assert "- Evaluation: combined ({0} results combined, 0 separate)".format(3 * len(segments)) in project.build_report()


def test_runner_disables_structured_output_after_a_rejection(tmp_path):
    project = make_project(tmp_path, evaluation_mode=EVALUATION_COMBINED)
    events = queue.Queue()
    service = NoSchemaService()
    runner = ProjectRunner(project, service, "m", events, parallelism=1)
    runner.start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])

    combined_calls = [call for call in service.calls if call[0] == "combined"]
    assert combined_calls[0][2] is True and all(call[2] is False for call in combined_calls[1:])
    assert runner.structured_output is False
    methods = [segment.results[check].method for _, segment in project.all_segments() for check in CHECKS if check in segment.results]
    assert EVALUATION_SEPARATE in methods and EVALUATION_COMBINED in methods  # first segment fell back, the rest did not


# --------------------------------------------------------- explanations
def test_canned_explanations_cover_trivial_kinds_only():
    de = "Er wusste nicht ob der Zug kommt und ging nach Hause."
    comma = extract_changes(de, "Er wusste nicht, ob der Zug kommt und ging nach Hause.", CHECK_GRAMMAR)[0]
    assert comma.kind == "punctuation"
    assert canned_explanation(comma, "de") == CANNED_EXPLANATIONS["punctuation"]["de"]
    assert canned_explanation(comma, "en") == "Punctuation corrected."
    assert canned_explanation(comma, None) is None and canned_explanation(comma, "fr") is None
    assert canned_explanation(Change("x", CHECK_SPELLING, 0, 5, "hello", "Hello"), "en") == "Capitalization corrected."
    assert canned_explanation(Change("x", CHECK_SPELLING, 0, 4, "a  b", "a b"), "en") == "Spacing corrected."
    one_letter = Change("x", CHECK_SPELLING, 0, 7, "occured", "occurred")
    assert one_letter.kind == "spelling" and one_letter.distance == 1
    assert canned_explanation(one_letter, "en") == "Typo: one letter corrected."
    two_letters = Change("x", CHECK_SPELLING, 0, 8, "recieved", "received")
    assert two_letters.kind == "spelling" and two_letters.distance == 2
    assert canned_explanation(two_letters, "en") is None
    assert canned_explanation(Change("x", CHECK_GRAMMAR, 0, 4, "were", "was"), "en") is None  # word choice
    assert canned_explanation(Change("x", CHECK_EXPRESSION, 0, 3, "", "a very long added clause"), "en") is None


def test_language_heuristic_and_setting_mapping():
    assert detect_language("Er hatte in der Nacht kaum geschlafen und wusste nicht, ob der Zug kommt.") == "de"
    assert detect_language("She had barely slept that night and did not know whether the train would come.") == "en"
    assert detect_language("") == "en"
    assert explanation_language("same as text", "Der Zug ist da und wir sind froh.") == "de"
    assert explanation_language("same as text", "The train is here and we are glad.") == "en"
    assert explanation_language("German", "whatever") == "de" and explanation_language("Deutsch", "") == "de"
    assert explanation_language("English", "Der Zug") == "en"
    assert explanation_language("French", "Le train") is None


class CommaService(FakeService):
    """Grammar check that only inserts a comma (a canned kind); spelling fixes two letters."""

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None):
        with self.lock:
            self.calls.append(("edit", instruction[:20], text, text_first))
            self.instructions.append(instruction)
        if instruction.startswith("Fix grammar"):
            return text.replace("nicht ob", "nicht, ob")
        if instruction.startswith("Correct spelling"):
            return text.replace("Bahnohf", "Bahnhof")  # transposition: distance 2, not canned
        return text


def test_explanation_request_is_skipped_when_every_change_is_canned():
    service = CommaService()
    text = "Er hatte kaum geschlafen und wusste nicht ob der Zug zum Bahnohf kommt."
    result = run_check(service, "m", text, CHECK_GRAMMAR, threading.Event())

    assert [c.explanation for c in result.changes] == ["Zeichensetzung korrigiert."]
    assert result.explained is True
    assert service.prompts == []  # no explanation request went out

    # a two-letter typo still needs the model; canned changes are not re-asked
    result = run_check(service, "m", text, CHECK_SPELLING, threading.Event())
    assert [c.explanation for c in result.changes] == ["Reason 1"] and result.explained is True
    assert len(service.prompts) == 1 and "Bahnohf" in service.prompts[0]

    # explanations off: fallback text, explained False
    result = run_check(CommaService(), "m", text, CHECK_GRAMMAR, threading.Event(), explain=False)
    assert [c.explanation for c in result.changes] == ["Grammar or punctuation fix."] and result.explained is False


def test_explained_is_true_only_when_no_fallback_remains():
    class SilentService(FakeService):
        def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None):
            self.prompts.append(messages[1]["content"])
            return "{}"  # the model answered nothing useful

    result = run_check(SilentService(), "m", "Teh dog were big.", CHECK_SPELLING, threading.Event())
    assert [c.explanation for c in result.changes] == ["Spelling correction."] and result.explained is False
    result = evaluate(FakeService(), "m", "Teh dog were big.", CHECK_SPELLING, threading.Event())
    assert result.changes[0].explanation == "" and result.explained is False
    explained = explain_result(FakeService(), "m", "Teh dog were big.", result, threading.Event())
    assert explained.changes[0].explanation == "Reason 1" and explained.explained is True


def test_grouped_prompt_numbers_across_segments_and_includes_each_text_once():
    first = "Teh dog were big."
    second = "It run fast and it run far."
    first_changes = extract_changes(first, "The dog was big.", CHECK_GRAMMAR)
    second_changes = extract_changes(second, "It ran fast and it ran far.", CHECK_GRAMMAR)
    assert len(first_changes) == 2 and len(second_changes) == 2

    single = build_grouped_explanation_messages([(first, first_changes)], CHECK_GRAMMAR)
    assert single == build_explanation_messages(first, first_changes, CHECK_GRAMMAR)

    grouped = build_grouped_explanation_messages([(first, first_changes), (second, second_changes)], CHECK_GRAMMAR, "German")
    content = grouped[1]["content"]
    assert "for 2 segments" in content and "in German" in content
    assert content.count(first) == 1 and content.count(second) == 1
    assert content.index("Segment 1:\n" + first) < content.index("Segment 2:\n" + second)
    for number, change in enumerate(first_changes + second_changes, 1):
        assert '{0}. "{1}" → "{2}"'.format(number, change.original_text, change.proposed_text) in content
    assert "Changes in segment 1:\n1." in content and "Changes in segment 2:\n3." in content

    # the fake answers by counting arrows, so numbering is verified end to end
    numbered = request_explanations(FakeService(), "m", [(first, first_changes), (second, second_changes)], CHECK_GRAMMAR, threading.Event())
    assert numbered == {1: "Reason 1", 2: "Reason 2", 3: "Reason 3", 4: "Reason 4"}


def test_explainer_batches_by_check_and_distributes_answers(tmp_path):
    project = make_project(tmp_path)
    events = queue.Queue()
    service = FakeService()
    runner = ProjectRunner(project, service, "m", events, parallelism=1)
    entries = []
    for n in range(EXPLANATION_GROUP_SIZE + 2):  # 8 grammar entries → 6 + 2
        text = "Segment {0}: it were big and it were fast.".format(n)
        result = evaluate(service, "m", text, CHECK_GRAMMAR, threading.Event())
        entries.append((1, n + 1, CHECK_GRAMMAR, result, list(result.changes), text))
    text = "Teh dog."
    spelling = evaluate(service, "m", text, CHECK_SPELLING, threading.Event())
    entries.append((2, 1, CHECK_SPELLING, spelling, list(spelling.changes), text))
    service.prompts.clear()

    runner._explain_batch(entries)

    assert len(service.prompts) == 3  # grammar 6 + grammar 2 + spelling 1
    assert "for 6 segments" in service.prompts[1] and "for 2 segments" in service.prompts[2]
    assert "Text:\nTeh dog." in service.prompts[0]  # spelling group first (CHECKS order), single-segment prompt
    # numbering continues across the six segments of the first group and restarts per request
    assert [c.explanation for c in entries[0][3].changes] == ["Reason 1", "Reason 2"]
    assert [c.explanation for c in entries[5][3].changes] == ["Reason 11", "Reason 12"]
    assert [c.explanation for c in entries[6][3].changes] == ["Reason 1", "Reason 2"]
    assert [c.explanation for c in entries[7][3].changes] == ["Reason 3", "Reason 4"]
    assert [c.explanation for c in spelling.changes] == ["Reason 1"]
    assert all(entry[3].explained for entry in entries)
    emitted = [event for event in list(events.queue) if event[0] == "workflow_result"]
    assert [(e[1], e[2], e[3]) for e in emitted] == [(2, 1, CHECK_SPELLING)] + [(1, n + 1, CHECK_GRAMMAR) for n in range(8)]
    assert runner.done == 9 and runner.explanation_requests == 3


class SlowExplainService(FakeService):
    """Explanations take a moment so evaluations pile up and get grouped."""

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None):
        if not messages[1]["content"].startswith("Review the text below"):
            if cancel_event.wait(0.15):
                raise EditCancelled("cancelled")
        return super().generate(model, messages, cancel_event, on_progress, max_tokens, response_format)


def test_runner_emits_each_result_once_with_batched_explanations(tmp_path):
    body = "\n\n".join("Paragraph {0}: teh dog were very very big and it run fast.".format(n) for n in range(6))
    source = tmp_path / "many.txt"
    source.write_text(body, encoding="utf-8")
    options = ProjectOptions(target_chars=60, max_chars=100, parallelism=3, evaluation_mode=EVALUATION_SEPARATE)
    project = create_project(source, body, options, model="m", backend="fake")
    segments = [(c.index, s.index) for c, s in project.all_segments() if not s.is_blank]
    assert len(segments) >= 4
    events = queue.Queue()
    service = SlowExplainService()
    runner = ProjectRunner(project, service, "m", events, parallelism=3)
    queued = runner.start()
    collected = drain(events, timeout=30)

    results = [event for event in collected if event[0] == "workflow_result"]
    keys = [(e[1], e[2], e[3]) for e in results]
    assert sorted(keys) == sorted((c, s, check) for c, s in segments for check in CHECKS)
    assert len(keys) == len(set(keys)) == queued == runner.total
    assert collected[-1] == ("workflow_finished", False)
    assert all(event[0] != "workflow_result" for event in collected[collected.index(collected[-1]):])
    for _, chapter_index, segment_index, check, result in results:
        apply_result(project, chapter_index, segment_index, check, result)
        assert result.status == "done" and result.explained, (segment_index, check)
    explained_results = sum(1 for _, s in project.all_segments() for r in s.results.values() if r.changes)
    assert runner.explanation_requests < explained_results  # some requests covered several segments
    assert runner.done == runner.total
    progress = [event for event in collected if event[0] == "workflow_progress"]
    assert progress[-1][1:3] == (runner.total, runner.total)


class BlockingExplainService(FakeService):
    """The first explanation request blocks until cancelled; lets a test cancel mid-explain."""

    def __init__(self):
        super().__init__()
        self.explaining = threading.Event()

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None):
        if not messages[1]["content"].startswith("Review the text below"):
            self.explaining.set()
            if cancel_event.wait(10):
                raise EditCancelled("cancelled")
        return super().generate(model, messages, cancel_event, on_progress, max_tokens, response_format)


def test_cancel_during_the_explain_stage_finishes_cleanly(tmp_path):
    project = make_project(tmp_path)
    events = queue.Queue()
    service = BlockingExplainService()
    runner = ProjectRunner(project, service, "m", events, parallelism=2)
    runner.start()
    assert service.explaining.wait(10), "the explainer never started a request"
    runner.cancel()
    collected = drain(events, timeout=10)

    assert collected[-1] == ("workflow_finished", True)
    for thread in runner._threads:
        thread.join(5)
    assert not runner.active
    results = [event for event in collected if event[0] == "workflow_result"]
    keys = [(e[1], e[2], e[3]) for e in results]
    assert len(keys) == len(set(keys))  # never twice
    for _, chapter_index, segment_index, check, result in results:
        apply_result(project, chapter_index, segment_index, check, result)
        assert result.status == "done"
        for change in result.changes:
            assert change.explanation  # fallback or canned, never empty
    # whatever was still waiting for an explanation came out with fallback text ...
    assert any(not r.explained for _, s in project.all_segments() for r in s.results.values() if r.changes)
    # ... and the project is consistent: every emitted result is stored, the rest is simply pending
    assert len(results) + len(project.pending_tasks()) == runner.total


def _subdir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def test_runner_reports_usage_per_request_and_the_project_sums_it(tmp_path):
    project = make_project(tmp_path)
    assert project.usage == empty_usage()
    events = queue.Queue()
    service = FakeService(usage=(100, 20))
    runner = ProjectRunner(project, service, "m", events, parallelism=2)
    runner.start()
    collected = drain(events)

    usage_events = [event for event in collected if event[0] == "workflow_usage"]
    requests = len(service.calls)  # every edit and explanation request reported once
    assert requests > 3
    assert len(usage_events) == requests
    for _, record in usage_events:
        project.add_usage(record)
    assert project.usage == {
        "prompt_tokens": 100 * requests, "completion_tokens": 20 * requests, "requests": requests,
        "seconds": 0.5 * requests,
    }

    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.usage == project.usage
    report = reloaded.build_report()
    assert "- Usage: {0:,} tokens ({1:,} in, {2:,} out) \u00b7 {3} requests \u00b7 40 tok/s".format(
        120 * requests, 100 * requests, 20 * requests, requests
    ) in report

    # combined mode reports the single request per segment, too
    combined = make_project(_subdir(tmp_path, "c"), evaluation_mode=EVALUATION_COMBINED)
    events = queue.Queue()
    service = FakeService(usage=(300, 50))
    ProjectRunner(combined, service, "m", events, parallelism=1).start()
    collected = drain(events)
    assert len([e for e in collected if e[0] == "workflow_usage"]) == len(service.calls)


def test_usage_is_optional_in_saved_projects(tmp_path):
    project = make_project(tmp_path)
    data = project.to_dict()
    assert data["usage"] == empty_usage()
    del data["usage"]  # a project saved before token accounting existed
    assert Project.from_dict(data, root=project.root).usage == empty_usage()
    data["usage"] = {"prompt_tokens": "12", "requests": 2}  # partial or stringly-typed: tolerated
    loaded = Project.from_dict(data, root=project.root)
    assert loaded.usage == {"prompt_tokens": 12, "completion_tokens": 0, "requests": 2, "seconds": 0.0}
    assert "- Usage: no token counts reported" in make_project(_subdir(tmp_path, "n")).build_report()

    # a backend that reports nothing leaves the total untouched
    events = queue.Queue()
    ProjectRunner(make_project(_subdir(tmp_path, "q")), FakeService(), "m", events).start()
    assert not [e for e in drain(events) if e[0] == "workflow_usage"]


def test_runner_with_nothing_to_do_finishes_immediately(tmp_path):
    project = make_project(tmp_path, checks={check: False for check in CHECKS})
    events = queue.Queue()
    assert ProjectRunner(project, FakeService(), "m", events).start() == 0
    assert events.get(timeout=1) == ("workflow_finished", False)


# ------------------------------------------------------------ decision log
def make_reviewed_project(tmp_path):
    """A project whose first non-blank segment carries the three checks of make_segment()."""
    project = make_project(tmp_path)
    chapter, segment = next((c, s) for c, s in project.all_segments() if not s.is_blank)
    reviewed = make_segment()
    segment.text = reviewed.text
    segment.results = reviewed.results
    return project, chapter, segment


def test_decide_records_and_applies_and_undo_reverts_one_entry(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    change = segment.changes(ALL)[0]
    assert project.decision_log == []

    entry = project.decide(chapter.index, segment.index, change, ACCEPTED)
    assert change.decision == ACCEPTED
    assert entry["chapter"] == chapter.index and entry["segment"] == segment.index
    assert entry["change_id"] == change.change_id
    assert (entry["before"], entry["after"], entry["group"]) == (PENDING, ACCEPTED, None)
    assert entry["ts"]
    assert project.decision_log == [entry]

    # deciding the same thing again is not a decision and leaves the log alone
    assert project.decide(chapter.index, segment.index, change, ACCEPTED) is None
    assert len(project.decision_log) == 1

    second = project.decide(chapter.index, segment.index, change, REJECTED)
    assert change.decision == REJECTED
    assert (second["before"], second["after"]) == (ACCEPTED, REJECTED)
    assert project.undo() == [second]
    assert change.decision == ACCEPTED
    assert len(project.decision_log) == 1
    assert project.undo() == [entry]
    assert change.decision == PENDING
    assert project.decision_log == []


def test_undo_reverts_a_whole_group_but_not_the_decision_before_it(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    changes = segment.changes(ALL)
    assert len(changes) >= 3
    first = project.decide(chapter.index, segment.index, changes[0], REJECTED)

    group = new_decision_group()
    assert group and group != new_decision_group()
    for change in changes:
        project.decide(chapter.index, segment.index, change, ACCEPTED, group=group)
    assert all(change.decision == ACCEPTED for change in changes)
    assert len(project.decision_log) == 1 + len(changes)

    undone = project.undo()
    assert [entry["change_id"] for entry in undone] == [change.change_id for change in changes]
    assert all(entry["group"] == group for entry in undone)
    assert changes[0].decision == REJECTED  # back to the single decision made before the group
    assert all(change.decision == PENDING for change in changes[1:])
    assert len(project.decision_log) == 1 and project.decision_log[0]["group"] is None

    assert project.undo() == [first]
    assert changes[0].decision == PENDING
    assert project.undo() is None


def test_undo_on_an_empty_log_returns_none(tmp_path):
    project, _, _ = make_reviewed_project(tmp_path)
    assert project.undo() is None
    assert project.decision_log == []


def test_decision_log_is_capped_at_the_limit_dropping_the_oldest(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    change = segment.changes(ALL)[0]
    decisions = [ACCEPTED, REJECTED]
    for index in range(DECISION_LOG_LIMIT + 7):
        project.decide(chapter.index, segment.index, change, decisions[index % 2])
    assert len(project.decision_log) == DECISION_LOG_LIMIT
    # the newest entry is kept, the oldest seven were dropped
    assert project.decision_log[-1]["after"] == decisions[(DECISION_LOG_LIMIT + 6) % 2]
    assert project.decision_log[0]["after"] == decisions[7 % 2]


def test_decision_log_round_trips_through_save_and_load(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    changes = segment.changes(ALL)
    project.decide(chapter.index, segment.index, changes[0], ACCEPTED)
    group = new_decision_group()
    project.decide(chapter.index, segment.index, changes[1], REJECTED, group=group)
    project.decide(chapter.index, segment.index, changes[2], REJECTED, group=group)
    project.save()

    data = json.loads(project.project_file.read_text(encoding="utf-8"))
    assert data["decision_log"] == project.decision_log
    reloaded = Project.load(project.root)
    assert reloaded.decision_log == project.decision_log

    # the reloaded log still undoes against the reloaded changes
    undone = reloaded.undo()
    assert [entry["change_id"] for entry in undone] == [changes[1].change_id, changes[2].change_id]
    _, reloaded_segment = reloaded.find(chapter.index, segment.index)
    assert reloaded_segment.find_change(changes[1].change_id).decision == PENDING
    assert reloaded_segment.find_change(changes[0].change_id).decision == ACCEPTED

    # projects saved before the log existed load with an empty one
    del data["decision_log"]
    assert Project.from_dict(data).decision_log == []


# -------------------------------------------------------------- kind filters
def test_hidden_kinds_only_filter_the_view():
    segment = make_segment()
    all_changes = segment.changes(ALL)
    kinds = {change.kind for change in all_changes}
    assert len(kinds) > 1
    hidden = sorted(kinds)[0]
    shown = segment.changes(ALL, hidden_kinds=[hidden])
    assert shown and all(change.kind != hidden for change in shown)
    assert len(shown) < len(all_changes)
    # the filter never changes what is applied, counted or reported
    for change in all_changes:
        change.decision = ACCEPTED
    assert render_segment(segment, ALL) == "The dog was enormous and it ran fast."
    assert segment.status(ALL) == STATUS_REVIEWED
    assert len(change_states(segment, ALL)) == len(all_changes)


def test_hidden_kinds_round_trip_and_drop_unknown_kinds():
    options = ProjectOptions(hidden_kinds=["punctuation", "spelling"])
    data = options.to_dict()
    assert data["hidden_kinds"] == ["punctuation", "spelling"]
    assert ProjectOptions.from_dict(data).hidden_kinds == ["punctuation", "spelling"]
    assert ProjectOptions.from_dict({"hidden_kinds": ["spelling", "bogus", 3]}).hidden_kinds == ["spelling"]
    assert ProjectOptions.from_dict({}).hidden_kinds == []


def test_changes_by_kind_and_bulk_decide_only_touch_pending_changes_of_that_kind(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    changes = segment.changes(ALL)
    by_kind = {}
    for change in changes:
        by_kind.setdefault(change.kind, []).append(change)
    kind, targets = max(by_kind.items(), key=lambda item: len(item[1]))
    others = [change for change in changes if change.kind != kind]
    assert others, "need a second kind to prove the filter"
    assert [c for _, _, c in project.changes_by_kind(kind)] == targets
    assert project.changes_by_kind("bogus") == []

    # one already decided change of that kind stays as it is
    project.decide(chapter.index, segment.index, targets[0], REJECTED)
    entries = project.decide_kind(kind, ACCEPTED)
    assert len(entries) == len(targets) - 1
    assert len({entry["group"] for entry in entries}) == 1 and entries[0]["group"] is not None
    assert targets[0].decision == REJECTED
    assert all(change.decision == ACCEPTED for change in targets[1:])
    assert all(change.decision == PENDING for change in others)
    assert project.changes_by_kind(kind, decision=PENDING) == []
    assert project.decide_kind(kind, ACCEPTED) == []  # nothing pending any more

    undone = project.undo()
    assert len(undone) == len(targets) - 1
    assert all(change.decision == PENDING for change in targets[1:])
    assert targets[0].decision == REJECTED  # the earlier single decision survives


# ------------------------------------------------------------- re-evaluate
def evaluated_project(tmp_path, **overrides):
    """A fully evaluated project (FakeService results applied)."""
    project = make_project(tmp_path, **overrides)
    events = queue.Queue()
    ProjectRunner(project, FakeService(), "m", events, parallelism=2).start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    assert project.pending_tasks() == []
    return project


def test_invalidate_counts_and_reports_the_pending_tasks(tmp_path):
    project = evaluated_project(tmp_path)
    chapter = project.chapters[0]
    segments = [segment for segment in chapter.segments if not segment.is_blank]
    assert len(segments) >= 2
    first, second = segments[0], segments[1]

    removed = project.invalidate(chapter.index, first.index, [CHECK_GRAMMAR])
    assert removed == [(chapter.index, first.index, CHECK_GRAMMAR)]
    assert CHECK_GRAMMAR not in first.results and CHECK_SPELLING in first.results
    assert project.pending_tasks() == removed
    assert first.status(ALL) == STATUS_QUEUED

    removed = project.invalidate(chapter.index, second.index)
    assert sorted(removed) == sorted((chapter.index, second.index, check) for check in CHECKS)
    assert second.results == {}
    assert len(project.pending_tasks()) == 1 + len(CHECKS)

    # whole chapter: everything still stored in it goes, other chapters are untouched
    removed = project.invalidate(chapter.index)
    assert len(removed) == sum(len(CHECKS) for segment in segments) - 1 - len(CHECKS)
    assert all(segment.results == {} for segment in chapter.segments)
    assert all(segment.results for segment in project.chapters[1].segments if not segment.is_blank)
    assert project.invalidate(chapter.index) == []
    assert project.invalidate(99) == []
    assert project.invalidate(project.chapters[1].index, 999) == []


def test_invalidate_marks_log_entries_stale_and_undo_skips_them(tmp_path):
    project = evaluated_project(tmp_path)
    chapter = project.chapters[0]
    segment = next(segment for segment in chapter.segments if segment.changes(ALL))
    change = next(c for c in segment.changes(ALL) if c.check == CHECK_SPELLING)
    kept_change = next(c for c in segment.changes(ALL) if c.check == CHECK_GRAMMAR)

    kept = project.decide(chapter.index, segment.index, kept_change, REJECTED)
    doomed = project.decide(chapter.index, segment.index, change, ACCEPTED)
    project.invalidate(chapter.index, segment.index, [change.check])
    assert doomed.get("stale") is True and kept.get("stale") is None
    assert len(project.decision_log) == 2  # nothing is dropped

    # undo skips the stale entry (whose change no longer exists) and reverts the live one
    assert project.undo() == [kept]
    assert kept_change.decision == PENDING and CHECK_GRAMMAR in segment.results
    assert project.decision_log == [doomed]
    assert project.undo() is None  # only a stale entry is left: nothing to undo
    assert project.decision_log == [doomed]

    # stale entries survive a save/load round trip
    project.save()
    assert Project.load(project.root).decision_log[0]["stale"] is True

    # a re-evaluated result with the same change ids is not touched by the stale entry
    project.pending_tasks()
    events = queue.Queue()
    ProjectRunner(project, FakeService(), "m", events, parallelism=1).start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    fresh = segment.find_change(change.change_id)
    assert fresh is not None and fresh is not change and fresh.decision == PENDING


def test_runner_picks_up_tasks_enqueued_mid_run(tmp_path):
    project = make_project(tmp_path)
    chapter = project.chapters[0]
    target = next(segment for segment in chapter.segments if not segment.is_blank)
    # pre-fill the target so it is not part of the initial batch ...
    for check in CHECKS:
        target.results[check] = CheckResult(check, "done", target.text)
    initial = project.pending_tasks()
    assert all(task[:2] != (chapter.index, target.index) for task in initial)

    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(delay=0.2), "m", events, parallelism=2)
    assert runner.start() == len(initial)
    # ... then drop its results while the workers are busy and hand the tasks to the runner
    removed = project.invalidate(chapter.index, target.index)
    assert runner.enqueue(removed) == len(CHECKS)
    assert runner.total == len(initial) + len(CHECKS)
    assert runner.enqueue([]) == 0

    collected = drain(events, timeout=30)
    for event in collected:
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    finished = [event for event in collected if event[0] == "workflow_finished"]
    assert finished == [("workflow_finished", False)]
    started = {event[1:] for event in collected if event[0] == "workflow_started"}
    assert set(removed) <= started
    assert project.pending_tasks() == []
    assert all(target.results[check].status == "done" for check in CHECKS)
    assert not runner.active and not runner.has_work()


def test_enqueue_takes_whole_segment_tasks_in_combined_mode(tmp_path):
    project = evaluated_project(tmp_path, evaluation_mode=EVALUATION_COMBINED)
    chapter = project.chapters[0]
    target = next(segment for segment in chapter.segments if not segment.is_blank)
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=2)
    assert runner.start() == 0
    assert events.get(timeout=1) == ("workflow_finished", False)

    removed = project.invalidate(chapter.index, target.index)
    assert len(removed) == len(CHECKS)
    assert project.pending_tasks() == [(chapter.index, target.index, None)]
    assert runner.enqueue(project.pending_tasks()) == 1
    collected = drain(events)
    for event in collected:
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    started = sorted(event[1:] for event in collected if event[0] == "workflow_started")
    assert started == sorted(removed)  # one task, one started/result event per check
    assert collected[-1] == ("workflow_finished", False)
    assert project.pending_tasks() == [] and all(target.results[check].status == "done" for check in CHECKS)


def test_enqueue_after_the_runner_finished_starts_new_workers(tmp_path):
    project = evaluated_project(tmp_path)
    chapter = project.chapters[0]
    target = next(segment for segment in chapter.segments if not segment.is_blank)
    events = queue.Queue()
    runner = ProjectRunner(project, FakeService(), "m", events, parallelism=2)
    assert runner.start() == 0
    assert events.get(timeout=1) == ("workflow_finished", False)

    removed = project.invalidate(chapter.index, target.index, [CHECK_SPELLING])
    assert runner.enqueue(removed) == 1
    collected = drain(events)
    assert [event[1:] for event in collected if event[0] == "workflow_started"] == removed
    assert collected[-1] == ("workflow_finished", False)


# ------------------------------------------------- inline edits and author fixes
def test_edit_change_uses_the_new_text_and_undo_restores_it(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    change = next(c for c in segment.changes(ALL) if c.original_text == "Teh")
    assert (change.proposed_text, change.model_proposed_text, change.edited) == ("The", "The", False)

    entry = project.edit_change(chapter.index, segment.index, change, "That")
    assert (entry["before"], entry["after"]) == (PENDING, ACCEPTED)
    assert (entry["before_text"], entry["after_text"]) == ("The", "That")
    assert change.proposed_text == "That" and change.model_proposed_text == "The"
    assert change.edited and change.decision == ACCEPTED
    assert change.kind == "word_choice"  # recomputed: Teh -> That is no longer a spelling fix
    assert render_segment(segment, ALL).startswith("That dog")
    # the same text again is not a change
    assert project.edit_change(chapter.index, segment.index, change, "That") is None

    assert project.undo() == [entry]
    assert change.proposed_text == "The" and not change.edited and change.decision == PENDING
    assert change.kind == "spelling"
    assert render_segment(segment, ALL).startswith("Teh dog")

    # reverting to the model's own wording clears the edited flag
    project.edit_change(chapter.index, segment.index, change, "That")
    project.edit_change(chapter.index, segment.index, change, "The")
    assert not change.edited and change.decision == ACCEPTED
    assert project.progress()["edited"] == 0


def test_author_change_beats_an_overlapping_spelling_change_and_is_reported(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    spelling = next(c for c in segment.changes(ALL) if c.original_text == "Teh")
    spelling.decision = ACCEPTED
    assert render_segment(segment, ALL).startswith("The dog")

    change = project.add_author_change(chapter.index, segment.index, 0, 3, "Their")
    assert change is not None and change.check == CHECK_AUTHOR and change.is_author
    assert change.change_id == "author-1" and change.decision == ACCEPTED
    assert change.explanation == AUTHOR_EXPLANATION and change.priority == 0
    assert ALL_CHECKS.index(CHECK_AUTHOR) == 0 and CHECK_AUTHOR not in CHECKS
    assert render_segment(segment, ALL).startswith("Their dog")
    states = change_states(segment, ALL)
    assert states[change.change_id] == STATE_APPLIED and states[spelling.change_id] == STATE_SUPERSEDED
    # author changes are always listed, even with every model check disabled
    assert segment.changes({check: False for check in CHECKS}) == [change]
    assert all(check != CHECK_AUTHOR for _, _, check in project.pending_tasks())  # never queued for the model

    second = project.add_author_change(chapter.index, segment.index, 4, 7, "cat")
    assert second.change_id == "author-2"
    assert [c.change_id for c in segment.author_result().changes] == ["author-1", "author-2"]
    stats = project.progress()
    assert stats["per_check"][CHECK_AUTHOR]["changes"] == 2 and stats["accepted"] >= 2
    report = project.build_report()
    assert "- Author's corrections: 2" in report
    assert "[Author] `Teh` → `Their` — applied — Author's correction" in report

    # invalid or empty corrections are refused
    assert project.add_author_change(chapter.index, segment.index, 5, 2, "x") is None
    assert project.add_author_change(chapter.index, segment.index, 0, 999, "x") is None
    assert project.add_author_change(chapter.index, segment.index, 0, 3, "Teh") is None
    assert project.add_author_change(99, 1, 0, 3, "x") is None

    # undo removes the correction again (the last one first), the empty pseudo-result disappears
    entry = project.decision_log[-1]
    assert entry["created"] is True and entry["change_id"] == "author-2"
    assert project.undo() == [entry]
    assert segment.find_change("author-2") is None
    project.undo()
    assert segment.author_result() is None and render_segment(segment, ALL).startswith("The dog")

    # re-evaluating a segment keeps the author's corrections
    change = project.add_author_change(chapter.index, segment.index, 0, 3, "Their")
    removed = project.invalidate(chapter.index, segment.index)
    assert CHECK_AUTHOR not in {check for _, _, check in removed} and segment.author_result() is not None
    assert project.invalidate(chapter.index, segment.index, [CHECK_AUTHOR]) == [(chapter.index, segment.index, CHECK_AUTHOR)]


def test_edited_and_author_changes_round_trip_and_old_files_default(tmp_path):
    project, chapter, segment = make_reviewed_project(tmp_path)
    change = next(c for c in segment.changes(ALL) if c.original_text == "Teh")
    project.edit_change(chapter.index, segment.index, change, "That")
    author = project.add_author_change(chapter.index, segment.index, 4, 7, "cat")
    project.save()

    reloaded = Project.load(project.root)
    _, reloaded_segment = reloaded.find(chapter.index, segment.index)
    restored = reloaded_segment.find_change(change.change_id)
    assert (restored.proposed_text, restored.model_proposed_text, restored.edited) == ("That", "The", True)
    restored_author = reloaded_segment.find_change(author.change_id)
    assert restored_author.check == CHECK_AUTHOR and restored_author.decision == ACCEPTED
    assert render_segment(reloaded_segment, ALL) == render_segment(segment, ALL)
    # undo still works on the reloaded project, text included
    reloaded.undo()
    reloaded.undo()
    assert restored.proposed_text == "The" and reloaded_segment.author_result() is None

    # a project file written before these fields existed
    data = change.to_dict()
    legacy = {key: value for key, value in data.items() if key not in ("model_proposed", "edited")}
    legacy["proposed"] = "The"
    old = Change.from_dict(legacy)
    assert old.model_proposed_text == "The" and old.edited is False
    deletion = Change.from_dict({"id": "spelling-9", "check": CHECK_SPELLING, "start": 0, "end": 3,
                                 "original": "Teh", "proposed": ""})
    assert deletion.model_proposed_text == ""  # a deletion is not mistaken for an edit


# ------------------------------------------------------------ chapter view
def _check_spans(text, segment, spans):
    """Every span must cover exactly the text it stands for.

    A non-applied change that overlaps an applied replacement collapses onto
    that replacement, so it may cover the replacement's text instead.
    """
    applied = [(span["start"], span["end"]) for span in spans if span["state"] == STATE_APPLIED]
    for span in spans:
        change = segment.find_change(span["change_id"])
        covered = text[span["start"]:span["end"]]
        if span["state"] == STATE_APPLIED:
            assert covered == change.proposed_text, (span, covered)
        elif covered != change.original_text:
            assert any(start <= span["start"] and span["end"] <= end for start, end in applied), (span, covered)


def test_annotated_segment_text_equals_render_segment_and_spans_line_up():
    segment = make_segment()
    changes = segment.changes(ALL)
    text, spans = render_segment_annotated(segment, ALL)
    assert text == render_segment(segment, ALL) == segment.text  # nothing accepted yet
    assert [span["change_id"] for span in spans] == [change.change_id for change in changes]
    assert all(span["state"] == STATE_PENDING and span["segment"] == 1 for span in spans)
    _check_spans(text, segment, spans)

    # accept a replacement (Teh->The), a deletion-like rewrite (very very big -> enormous)
    # and the overlapping grammar fix that it supersedes, reject the rest
    for change in changes:
        change.decision = ACCEPTED if change.check != CHECK_GRAMMAR else REJECTED
    grammar_was = next(c for c in changes if c.original_text == "were")
    grammar_was.decision = ACCEPTED  # overlaps nothing, gets applied
    text, spans = render_segment_annotated(segment, ALL, offset=10)
    assert text == render_segment(segment, ALL) == "The dog was enormous and it ran fast."
    shifted = [dict(span, start=span["start"] - 10, end=span["end"] - 10) for span in spans]
    _check_spans(text, segment, shifted)
    by_id = {span["change_id"]: span for span in shifted}
    states = change_states(segment, ALL)
    assert {span["state"] for span in shifted} == {STATE_APPLIED, STATE_REJECTED, STATE_SUPERSEDED}
    assert all(by_id[cid]["state"] == state for cid, state in states.items())
    applied = [span for span in shifted if span["state"] == STATE_APPLIED]
    assert applied == sorted(applied, key=lambda span: span["start"])
    assert text[by_id[grammar_was.change_id]["start"]:by_id[grammar_was.change_id]["end"]] == "was"


def test_annotated_spans_collapse_inside_replacements_and_handle_insertions_and_deletions():
    text = "a bb c dd e"
    segment = Segment(1, text)
    spelling = extract_changes(text, "a c dd e", CHECK_SPELLING)  # deletion of " bb"
    grammar = extract_changes(text, "a bb c dd e f", CHECK_GRAMMAR)  # insertion at the end
    expression = extract_changes(text, "a bb c XX e", CHECK_EXPRESSION)  # replacement of dd
    segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", "", spelling)
    segment.results[CHECK_GRAMMAR] = CheckResult(CHECK_GRAMMAR, "done", "", grammar)
    segment.results[CHECK_EXPRESSION] = CheckResult(CHECK_EXPRESSION, "done", "", expression)
    for change in segment.changes(ALL):
        change.decision = ACCEPTED
    rendered, spans = render_segment_annotated(segment, ALL)
    assert rendered == render_segment(segment, ALL)
    _check_spans(rendered, segment, spans)
    by_id = {span["change_id"]: span for span in spans}
    deletion = next(c for c in spelling if c.proposed_text == "")
    insertion = next(c for c in grammar if c.original_text == "")
    assert by_id[deletion.change_id]["start"] == by_id[deletion.change_id]["end"]  # zero width in the output
    assert rendered[by_id[insertion.change_id]["start"]:by_id[insertion.change_id]["end"]] == insertion.proposed_text
    # a pending change inside an applied replacement collapses onto the replacement
    segment.results[CHECK_GRAMMAR].changes.append(Change("grammar-9", CHECK_GRAMMAR, 7, 8, "d", "p"))
    rendered, spans = render_segment_annotated(segment, ALL)
    inner = next(span for span in spans if span["change_id"] == "grammar-9")
    outer = next(span for span in spans if span["check"] == CHECK_EXPRESSION)
    assert inner["state"] == STATE_PENDING
    assert (inner["start"], inner["end"]) == (outer["start"], outer["end"])


def test_render_chapter_annotated_matches_render_chapter_across_segments(tmp_path):
    project = evaluated_project(tmp_path)
    for chapter in project.chapters:
        text, spans = render_chapter_annotated(project, chapter)
        assert text == project.render_chapter(chapter)
        for span in spans:
            _, segment = project.find(chapter.index, span["segment"])
            _check_spans(text, segment, [span])
    # accept everything in one chapter and check again, offsets must follow the shifted text
    chapter = project.chapters[0]
    listed = [(c.index, s.index, ch.change_id) for c, s, ch in pending_changes(project) if c is chapter]
    assert listed
    for _, segment in project.all_segments():
        for change in segment.changes(ALL):
            change.decision = ACCEPTED
    text, spans = render_chapter_annotated(project, chapter)
    assert text == project.render_chapter(chapter) != chapter.heading + chapter.body + chapter.trailing
    assert len(spans) == len(listed) and all(span["state"] == STATE_APPLIED for span in spans)
    for span in spans:
        _, segment = project.find(chapter.index, span["segment"])
        assert text[span["start"]:span["end"]] == segment.find_change(span["change_id"]).proposed_text
    assert list(pending_changes(project)) == []


# ------------------------------------------------------------------ re-sync
def _source(project):
    return Path(project.source_path)


def test_unchanged_source_is_detected_cheaply_and_resync_is_a_noop(tmp_path):
    project = evaluated_project(tmp_path)
    assert project.source_sha256 == text_fingerprint(MANUSCRIPT) and project.source_mtime > 0
    assert project.source_changed() is False and project.source_check_reason == "unchanged"
    # touched but identical content: the hash decides, and the new mtime is remembered
    os.utime(str(_source(project)), (time.time() + 5, time.time() + 5))
    assert project.source_changed() is False and project.source_check_reason == "unchanged"
    assert abs(project.source_mtime - _source(project).stat().st_mtime) < 1e-6

    segment = next(s for _, s in project.all_segments() if s.changes(ALL))
    change = segment.changes(ALL)[0]
    project.decide(1, segment.index, change, ACCEPTED)
    before = {(c.index, s.index): [ch.to_dict() for ch in s.changes(ALL)] for c, s in project.all_segments()}
    summary = resync_project(project, MANUSCRIPT)
    assert summary["new"] == 0 and summary["removed"] == 0 and summary["report"] is None
    assert summary["kept"] == sum(len(c.segments) for c in project.chapters) and summary["chapters"] == 2
    after = {(c.index, s.index): [ch.to_dict() for ch in s.changes(ALL)] for c, s in project.all_segments()}
    assert after == before
    assert project.pending_tasks() == []
    marker = project.decision_log[-1]
    assert marker["resync"] == {"kept": summary["kept"], "new": 0, "removed": 0} and marker["stale"] is True
    assert project.undo()[0]["change_id"] == change.change_id  # the marker is skipped, the decision reverts


def test_edited_paragraph_becomes_a_new_segment_while_others_keep_their_decisions(tmp_path):
    project = evaluated_project(tmp_path)
    first = project.chapters[0].segments[0]
    change = next(c for c in first.changes(ALL) if c.original_text == "Teh")
    entry = project.decide(1, 1, change, ACCEPTED)
    edited = MANUSCRIPT.replace("settled quietly over the small town", "settled QUIETLY over the small town", 1)
    assert edited != MANUSCRIPT
    _source(project).write_text(edited, encoding="utf-8")
    os.utime(str(_source(project)), (time.time() + 5, time.time() + 5))
    assert project.source_changed() is True and project.source_check_reason == "changed"

    summary = resync_project(project, edited)
    assert (summary["kept"], summary["new"], summary["removed"]) == (2, 1, 1)
    assert summary["report"] is None  # the dropped filler segment had no changes
    kept = project.chapters[0].segments[0]
    assert kept.find_change(change.change_id).decision == ACCEPTED and kept.results
    fresh = project.chapters[0].segments[1]
    assert "QUIETLY" in fresh.text and fresh.results == {}
    assert sorted(project.pending_tasks()) == sorted((1, 2, check) for check in CHECKS)
    assert project.chapters[1].segments[0].results
    assert project.source_changed() is False and project.source_sha256 == text_fingerprint(edited)
    # the decision log still points at the kept segment and undo works
    assert entry in project.decision_log and entry.get("stale") is None
    assert project.undo() == [entry] and kept.find_change(change.change_id).decision == PENDING

    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.source_sha256 == project.source_sha256 and reloaded.source_mtime == project.source_mtime
    assert len(reloaded.pending_tasks()) == len(CHECKS)


def test_deleted_paragraph_with_pending_changes_is_written_to_a_resync_report(tmp_path):
    project = evaluated_project(tmp_path)
    first = project.chapters[0].segments[0]
    changes = first.changes(ALL)
    assert changes
    project.decide(1, 1, changes[0], ACCEPTED)
    project.decide(1, 1, changes[1], REJECTED)
    kept_entries = [dict(entry) for entry in project.decision_log]
    shortened = MANUSCRIPT.replace("Teh dog were very very big. It run fast.\n\n", "")
    summary = resync_project(project, shortened)
    assert summary["removed"] == 1 and summary["new"] == 0
    report = summary["report"]
    assert report is not None and report.parent == project.root and report.name.startswith("resync-")
    text = report.read_text(encoding="utf-8")
    assert "Kapitel 1 · segment 1" in text and "Teh dog were very very big" in text
    assert "`Teh` → `The` — applied" in text
    assert changes[1].original_text in text and "rejected" not in text.split("\n> ")[-1].split("- [")[0]
    assert all(entry["stale"] is True for entry in project.decision_log[:2])
    assert [entry["change_id"] for entry in project.decision_log[:2]] == [e["change_id"] for e in kept_entries]
    assert project.undo() is None  # nothing live is left to undo
    # a second re-sync with dropped changes gets its own file
    project.chapters[0].segments[0].results[CHECK_SPELLING] = CheckResult(
        CHECK_SPELLING, "done", "", [Change("spelling-1", CHECK_SPELLING, 0, 3, "The", "Teh")])
    other = resync_project(project, "Kapitel 1\n\nSomething else entirely.\n")
    assert other["report"] is not None and other["report"] != report and other["report"].exists()


def test_duplicate_segments_pair_up_in_order(tmp_path):
    text = "Kapitel 1\n\n" + FILLER + "\n\n" + FILLER + "\n\n" + FILLER + "\n"
    source = tmp_path / "twins.txt"
    source.write_text(text, encoding="utf-8")
    project = create_project(source, text, ProjectOptions(target_chars=200, max_chars=400), model="m", backend="fake")
    segments = [segment for segment in project.chapters[0].segments if segment.text == FILLER]
    assert len(segments) == 3
    for number, segment in enumerate(segments, 1):
        segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", "", [
            Change("spelling-1", CHECK_SPELLING, 0, 3, "The", "Ze{0}".format(number))])
    # drop the middle twin: the first two new segments take the first two old ones, in order
    summary = resync_project(project, "Kapitel 1\n\n" + FILLER + "\n\n" + FILLER + "\n")
    assert (summary["new"], summary["removed"]) == (0, 1)
    proposed = [s.results[CHECK_SPELLING].changes[0].proposed_text
                for s in project.chapters[0].segments if s.text == FILLER]
    assert proposed == ["Ze1", "Ze2"]
    assert summary["report"] is not None and "Ze3" in summary["report"].read_text(encoding="utf-8")


def test_resync_keeps_model_chapter_boundaries_when_their_first_paragraphs_survive(tmp_path):
    text = "Once upon a time.\n\n" + FILLER + "\n\nThe next morning.\n\n" + FILLER + "\n"
    source = tmp_path / "model.txt"
    source.write_text(text, encoding="utf-8")
    project = create_project(source, text, ProjectOptions(target_chars=200, max_chars=400, chapter_mode="model"),
                             model="m", backend="fake")
    apply_model_outline(project, text, [2], ["Dawn", "Morning"])
    assert [c.title for c in project.chapters] == ["Dawn", "Morning"] and project.method == "model"
    for _, segment in project.all_segments():
        segment.results[CHECK_SPELLING] = CheckResult(CHECK_SPELLING, "done", segment.text)

    changed = text.replace("Once upon a time.", "Once upon a time, long ago.")
    summary = resync_project(project, changed)
    assert [c.title for c in project.chapters] == ["Dawn", "Morning"]
    assert project.chapters[1].segments[0].text.startswith("The next morning.")
    assert summary["removed"] == 1 and summary["new"] == 1
    assert project.chapters[1].segments[0].results  # the second chapter's segments were kept

    # when a chapter's first paragraph is gone the automatic split is the fallback
    fallback = changed.replace("The next morning.", "Later.")
    resync_project(project, fallback)
    assert project.method != "model" and len(project.chapters) >= 1


def test_source_tracking_reports_missing_files_and_old_projects(tmp_path):
    project = make_project(tmp_path)
    data = project.to_dict()
    del data["source_sha256"]
    del data["source_mtime"]
    old = Project.from_dict(data, root=project.root)
    assert old.source_sha256 == "" and old.source_mtime == 0.0
    assert old.source_changed() is False and old.source_check_reason == "unknown"
    _source(project).unlink()
    assert project.source_changed() is False and project.source_check_reason == "missing"
    project.source_path = ""
    assert project.source_changed() is False and project.source_check_reason == "missing"
    assert text_fingerprint("a\r\nb") == text_fingerprint("a\nb")


# ------------------------------------------------------- document formats
def _docx_manuscript(path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for block in MANUSCRIPT.strip("\n").split("\n\n"):
        if block.startswith("Kapitel"):
            document.add_paragraph(block, style="Heading 1")
        else:
            paragraph = document.add_paragraph()
            paragraph.add_run(block[:3]).bold = True
            paragraph.add_run(block[3:])
    document.save(str(path))
    return path


def test_project_created_from_a_docx_exports_the_reviewed_docx(tmp_path):
    from core.documents import KIND_DOCX, STRUCTURE_WARNING, load_document

    docx = pytest.importorskip("docx")
    source = _docx_manuscript(tmp_path / "novel.docx")
    loaded = load_document(source)
    assert loaded.kind == KIND_DOCX and loaded.text.startswith("# Kapitel 1\n\n")
    options = ProjectOptions(target_chars=200, max_chars=400, parallelism=2, evaluation_mode=EVALUATION_SEPARATE)
    project = create_project(source, loaded, options, model="m", backend="fake")
    assert project.document_kind == KIND_DOCX and project.original_text() == loaded.text
    assert [c.title for c in project.chapters] == ["Kapitel 1", "Kapitel 2"]
    assert project.source_changed() is False and project.source_check_reason == "unchanged"

    events = queue.Queue()
    ProjectRunner(project, FakeService(), "m", events, parallelism=2).start()
    for event in drain(events):
        if event[0] == "workflow_result":
            apply_result(project, *event[1:])
    project.save()
    reloaded = Project.load(project.root)
    assert reloaded.to_dict()["format"] == 2
    assert reloaded.document == project.document and reloaded.loaded_document().paragraphs == loaded.paragraphs
    for _, segment in reloaded.all_segments():
        for change in segment.changes(reloaded.enabled):
            if change.original_text == "Teh":
                change.decision = ACCEPTED

    warnings = []
    paths = reloaded.export(warnings.append)
    assert warnings == [] and paths["warnings"] == []
    assert paths["formatted"].name == "novel-reviewed.docx"
    exported = docx.Document(str(paths["formatted"]))
    texts = [paragraph.text for paragraph in exported.paragraphs]
    assert texts[0] == "Kapitel 1" and exported.paragraphs[0].style.name == "Heading 1"
    assert texts[1].startswith("The dog were")
    assert len(exported.paragraphs[1].runs) == 1  # the changed paragraph lost its second run
    assert len(exported.paragraphs[2].runs) == 2  # the untouched filler kept both runs
    assert load_document(paths["formatted"]).text == reloaded.render_document()
    assert paths["document"].read_text(encoding="utf-8") == reloaded.render_document()

    # an author's fix that splits a paragraph forces a rebuilt document and a warning
    segment = reloaded.chapters[1].segments[-1]
    reloaded.add_author_change(2, segment.index, 0, 0, "Neu.\n\n")
    paths = reloaded.export()
    assert STRUCTURE_WARNING in paths["warnings"]
    assert load_document(paths["formatted"]).text == reloaded.render_document()


def test_format_one_projects_load_as_plain_text_documents(tmp_path):
    from core.documents import KIND_TXT

    project = make_project(tmp_path)
    data = project.to_dict()
    del data["document"]
    data["format"] = 1
    old = Project.from_dict(data, root=project.root)
    assert old.document_kind == KIND_TXT and old.loaded_document().paragraphs == []
    assert old.loaded_document().text == MANUSCRIPT
    paths = old.export()
    assert "formatted" not in paths and paths["warnings"] == []
    with pytest.raises(ValueError):
        Project.from_dict(dict(data, format=99))


def test_resync_from_a_loaded_document_keeps_the_paragraph_locators(tmp_path):
    from core.documents import KIND_DOCX, load_document

    pytest.importorskip("docx")
    source = _docx_manuscript(tmp_path / "novel.docx")
    options = ProjectOptions(target_chars=200, max_chars=400, parallelism=2, evaluation_mode=EVALUATION_SEPARATE)
    project = create_project(source, load_document(source), options, model="m", backend="fake")
    project.save()
    _docx_manuscript(source)  # rewritten identically: still "unchanged"
    os.utime(str(source), (time.time() + 5, time.time() + 5))
    assert project.source_changed() is False
    project.document["paragraphs"] = []
    resync_project(project, load_document(source))
    assert project.document_kind == KIND_DOCX and len(project.document["paragraphs"]) == len(load_document(source).paragraphs)


def test_explanation_prompt_accepts_a_plain_instruction_instead_of_a_check():
    text = "Teh cat sat."
    changes = extract_changes(text, "The cat sat.", CHECK_SPELLING)
    quick = build_explanation_messages(text, changes, "Fix grammar issues without altering the meaning.")
    content = quick[1]["content"]
    assert content.startswith("An editing pass proposed the changes listed below for the text.")
    assert "Instruction: Fix grammar issues without altering the meaning." in content
    assert "Check:" not in content and "1. \"Teh\" \u2192 \"The\"" in content
    checked = build_explanation_messages(text, changes, CHECK_SPELLING)[1]["content"]
    assert checked.startswith("A spelling check proposed") and "Check: Typos" in checked
    grouped = build_grouped_explanation_messages([(text, changes), (text, changes)], "Polish it.")[1]["content"]
    assert grouped.startswith("An editing pass proposed the changes listed below for 2 segments")
    assert "Instruction: Polish it." in grouped
