"""Tests for the chapter-spanning consistency check (core/consistency.py, no Tk, no live model)."""

import json
import queue
import threading
import time

from core.backend import BackendUnavailable
from core.consistency import (
    FINDING_HYPHENATION,
    FINDING_NAME,
    FINDING_NUMBER,
    FINDING_POV,
    FINDING_QUOTES,
    FINDING_TENSE,
    MODEL_TRIAGED,
    STATUS_DISMISSED,
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_UNVERIFIED,
    ConsistencyRunner,
    Finding,
    apply_preferred,
    apply_verdicts,
    batches,
    build_consistency_messages,
    collect_candidates,
    find_occurrences,
    parse_verdicts,
    representative_sentences,
)
from core.models import ACCEPTED
from core.workflow import CHECK_AUTHOR, CHECK_SPELLING, Change, CheckResult, Project, ProjectOptions, create_project

DAY = "Am Abend ging Nyxara durch die Stadt und sie sah das Haus. Er war müde und er sagte nichts. "
NIGHT = "Am Morgen kam Nixara zurück und sie hatte drei Briefe. Sie sagte nichts, denn er war fort. "
DIARY = "Ich ging nach Hause und ich hatte Angst. Mir war kalt und ich sagte nichts, wir waren allein. "
CHAPTER_1 = "Kapitel 1\n\n" + DAY * 6 + "Sie schrieb eine E-Mail an Meret. Es waren 3 Tage.\n\n"
CHAPTER_2 = "Kapitel 2\n\n" + NIGHT * 6 + "Die Email war kurz. „Komm“, sagte er zu Meret.\n\n"
CHAPTER_3 = "Kapitel 3\n\n" + DIARY * 6 + "\"Warum\", fragte ich. Dann rief ich Nyxara an.\n"
MANUSCRIPT = CHAPTER_1 + CHAPTER_2 + CHAPTER_3


def make_project(tmp_path, text=MANUSCRIPT, **options):
    source = tmp_path / "book.txt"
    source.write_text(text, encoding="utf-8")
    return create_project(source, text, ProjectOptions(target_chars=300, max_chars=600, **options),
                          model="m", backend="fake")


def by_kind(findings, kind):
    return [finding for finding in findings if finding.kind == kind]


def test_collects_name_hyphenation_number_quote_and_pov_candidates(tmp_path):
    project = make_project(tmp_path)
    assert [chapter.title for chapter in project.chapters] == ["Kapitel 1", "Kapitel 2", "Kapitel 3"]
    findings = collect_candidates(project)

    names = by_kind(findings, FINDING_NAME)
    nyx = next(finding for finding in names if "Nyxara" in finding.variants)
    assert sorted(nyx.variants) == ["Nixara", "Nyxara"]
    assert nyx.counts == {"Nyxara": 7, "Nixara": 6}  # 6 in chapter 1 + 1 in chapter 3
    assert nyx.status == STATUS_UNVERIFIED and nyx.preferred == "" and nyx.finding_id == "name:nixara|nyxara"
    assert len(nyx.occurrences["Nyxara"]) == 7 and nyx.occurrences["Nyxara"][0]["chapter"] == 1
    first = nyx.occurrences["Nixara"][0]
    _, segment = project.find(first["chapter"], first["segment"])
    assert segment.text[first["start"]:first["end"]] == "Nixara"
    # "Meret" has no look-alike, "Haus"/"Hause" are grouped as candidates (the model sorts those out)
    assert not any("Meret" in finding.variants for finding in names)
    assert any(sorted(finding.variants) == ["Haus", "Hause"] for finding in names)

    hyphen = by_kind(findings, FINDING_HYPHENATION)
    assert len(hyphen) == 1 and sorted(hyphen[0].variants) == ["E-Mail", "Email"]
    assert hyphen[0].counts == {"E-Mail": 1, "Email": 1}

    numbers = by_kind(findings, FINDING_NUMBER)
    assert len(numbers) == 1 and numbers[0].variants == ["drei", "3"] and numbers[0].counts == {"drei": 6, "3": 1}
    assert numbers[0].finding_id == "number:3|drei:3"

    quotes = by_kind(findings, FINDING_QUOTES)
    assert len(quotes) == 1 and quotes[0].status == STATUS_ISSUE
    assert quotes[0].counts == {"\"\"": 2, "„“": 1} and quotes[0].preferred == "\"\""

    # chapter 1 is purely third person, chapter 3 purely first person; chapter 2 has too few pronouns to count
    pov = by_kind(findings, FINDING_POV)
    assert [finding.chapter for finding in pov] == [1, 3] and all(f.status == STATUS_ISSUE for f in pov)
    assert pov[1].counts["first person"] == 32 and pov[1].counts["third person"] == 0
    assert pov[0].counts["first person"] == 0 and pov[0].counts["third person"] == 20
    assert pov[1].detail.startswith("Kapitel 3: 100% first person (project mean")
    assert len(pov[1].occurrences["first person"]) == 10  # capped, the counts keep the totals
    # every chapter is in the past tense: no tense drift
    assert by_kind(findings, FINDING_TENSE) == []


def test_glossary_protected_forms_are_exempt_or_preferred(tmp_path):
    both = make_project(tmp_path, glossary=["Nyxara", "Nixara"])
    assert not any("Nyxara" in finding.variants for finding in collect_candidates(both))
    one = make_project(tmp_path, glossary=["Nyxara"])
    nyx = next(finding for finding in collect_candidates(one) if "Nyxara" in finding.variants)
    assert nyx.preferred == "Nyxara" and nyx.status == STATUS_UNVERIFIED
    # a wildcard term protects nothing exactly, so it does not exempt
    wildcard = make_project(tmp_path, glossary=["Nyx*", "Nix*"])
    assert any("Nyxara" in finding.variants for finding in collect_candidates(wildcard))


def test_applied_changes_are_skipped_and_statuses_carry_over(tmp_path):
    project = make_project(tmp_path)
    findings = collect_candidates(project)
    nyx = next(finding for finding in findings if "Nyxara" in finding.variants)
    # the spelling check already fixed every "Nixara" and the author accepted it
    for occurrence in find_occurrences(project, "Nixara"):
        _, segment = project.find(occurrence["chapter"], occurrence["segment"])
        result = segment.results.setdefault(CHECK_SPELLING, CheckResult(CHECK_SPELLING, "done", segment.text))
        result.changes.append(Change("spelling-{0}".format(len(result.changes) + 1), CHECK_SPELLING,
                                     occurrence["start"], occurrence["end"], "Nixara", "Nyxara", "", ACCEPTED))
    again = collect_candidates(project)
    assert not any("Nixara" in finding.variants for finding in again)

    # dismissed / verified statuses survive a re-collection by id
    nyx.status = STATUS_DISMISSED
    hyphen = next(finding for finding in findings if finding.kind == FINDING_HYPHENATION)
    hyphen.status, hyphen.preferred, hyphen.reason = STATUS_ISSUE, "E-Mail", "House style."
    project.decide  # noqa: B018 - the decisions are irrelevant here, only the ids matter
    for _, segment in project.all_segments():
        segment.results.clear()
    third = collect_candidates(project, previous=findings)
    assert next(f for f in third if f.finding_id == nyx.finding_id).status == STATUS_DISMISSED
    kept = next(f for f in third if f.kind == FINDING_HYPHENATION)
    assert (kept.status, kept.preferred, kept.reason) == (STATUS_ISSUE, "E-Mail", "House style.")


def test_messages_carry_one_sentence_per_variant_and_verdicts_parse_tolerantly(tmp_path):
    project = make_project(tmp_path)
    findings = collect_candidates(project)
    triaged = [finding for finding in findings if finding.kind in MODEL_TRIAGED]
    assert batches(triaged) == [triaged]  # fewer than one batch of 15
    nyx = next(finding for finding in triaged if "Nyxara" in finding.variants)
    sentences = representative_sentences(project, nyx)
    assert sentences["Nyxara"].startswith("Am Abend ging Nyxara durch die Stadt")
    assert sentences["Nixara"] == "Am Morgen kam Nixara zurück und sie hatte drei Briefe."
    messages = build_consistency_messages(project, triaged)
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    request = json.loads(messages[1]["content"])
    assert {item["id"] for item in request["items"]} == {finding.finding_id for finding in triaged}
    item = next(item for item in request["items"] if item["id"] == nyx.finding_id)
    assert item["type"] == FINDING_NAME
    assert {variant["form"]: variant["count"] for variant in item["variants"]} == nyx.counts
    assert all(variant["example"] for variant in item["variants"])
    assert "verdicts" in request["answer_format"]

    answer = '```json\n{"verdicts": [{"id": "%s", "issue": "yes", "preferred": "nyxara", "reason": "Same  character."},' \
             ' {"id": "%s", "issue": false}, {"no": "id"}, "junk"]}\n```' % (nyx.finding_id, "number:3|drei:3")
    verdicts = parse_verdicts(answer)
    assert verdicts == {
        nyx.finding_id: {"issue": True, "preferred": "nyxara", "reason": "Same character."},
        "number:3|drei:3": {"issue": False, "preferred": "", "reason": ""},
    }
    assert parse_verdicts("no json here") == {} and parse_verdicts("") == {}
    assert parse_verdicts('{"x:y": {"issue": true}}') == {"x:y": {"issue": True, "preferred": "", "reason": ""}}
    assert parse_verdicts('{"verdicts": "nope"}') == {}

    assert apply_verdicts(findings, verdicts) == 2
    assert nyx.status == STATUS_ISSUE and nyx.preferred == "Nyxara"  # matched to the variant's spelling
    number = next(finding for finding in findings if finding.kind == FINDING_NUMBER)
    assert number.status == STATUS_OK and number.preferred == ""
    hyphen = next(finding for finding in findings if finding.kind == FINDING_HYPHENATION)
    assert hyphen.status == STATUS_UNVERIFIED  # no verdict for it
    # an issue without a preferred form falls back to the most frequent variant
    apply_verdicts([hyphen], {hyphen.finding_id: {"issue": True, "preferred": "", "reason": ""}})
    assert hyphen.preferred == hyphen.variants[0]
    # dismissed findings keep their status
    hyphen.status = STATUS_DISMISSED
    apply_verdicts([hyphen], {hyphen.finding_id: {"issue": False, "preferred": "", "reason": ""}})
    assert hyphen.status == STATUS_DISMISSED


def test_apply_preferred_everywhere_creates_author_changes_in_one_undo_group(tmp_path):
    project = make_project(tmp_path)
    findings = collect_candidates(project)
    nyx = next(finding for finding in findings if "Nyxara" in finding.variants)
    assert not nyx.applicable
    count, group = apply_preferred(project, nyx)
    assert (count, group) == (0, None)

    nyx.status, nyx.preferred = STATUS_ISSUE, "Nyxara"
    assert nyx.applicable
    count, group = apply_preferred(project, nyx)
    assert count == 6 and group
    authored = [change for _, segment in project.all_segments()
                for change in (segment.author_result().changes if segment.author_result() else [])]
    assert len(authored) == 6 and all(c.check == CHECK_AUTHOR and c.proposed_text == "Nyxara" for c in authored)
    assert all(c.original_text == "Nixara" and c.decision == ACCEPTED for c in authored)
    assert not any("Nixara" in project.render_chapter(chapter) for chapter in project.chapters)
    entries = project.decision_log[-6:]
    assert all(entry["group"] == group and entry["created"] for entry in entries)
    assert find_occurrences(project, "Nixara") == []  # replaced text is no longer scanned
    assert not any("Nixara" in finding.variants for finding in collect_candidates(project))
    # one undo removes them all
    assert len(project.undo()) == 6
    assert all(segment.author_result() is None for _, segment in project.all_segments())
    assert len(find_occurrences(project, "Nixara")) == 6

    number = next(finding for finding in findings if finding.kind == FINDING_NUMBER)
    number.status, number.preferred = STATUS_ISSUE, "drei"
    count, _ = apply_preferred(project, number)
    assert count == 1
    assert "Es waren drei Tage." in project.render_chapter(project.chapters[0])

    # occurrences that do not match any more (text edited underneath) are skipped
    hyphen = next(finding for finding in findings if finding.kind == FINDING_HYPHENATION)
    hyphen.status, hyphen.preferred = STATUS_ISSUE, "E-Mail"
    for _, segment in project.all_segments():
        if "Email" in segment.text:
            segment.text = segment.text.replace("Email", "Mail")
    count, _ = apply_preferred(project, hyphen)
    assert count == 0


def test_findings_round_trip_through_the_project_file(tmp_path):
    project = make_project(tmp_path)
    project.consistency = collect_candidates(project)
    project.consistency[0].status = STATUS_DISMISSED
    project.save()
    reloaded = Project.load(project.root)
    assert [finding.to_dict() for finding in reloaded.consistency] == [f.to_dict() for f in project.consistency]
    assert reloaded.consistency[0].status == STATUS_DISMISSED
    data = project.to_dict()
    del data["consistency"]
    assert Project.from_dict(data).consistency == []
    data["consistency"] = [{"kind": FINDING_NAME}, project.consistency[1].to_dict()]  # a damaged entry is dropped
    assert len(Project.from_dict(data).consistency) == 1
    assert Finding.from_dict({"id": "x", "variants": ["a"]}).status == STATUS_UNVERIFIED


class VerdictService:
    """Answers every batch with 'issue' for the first item and 'intentional' for the rest."""

    def __init__(self, fail=False, delay=0.0):
        self.fail = fail
        self.delay = delay
        self.requests = []

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, on_usage=None, on_stream=None):
        self.requests.append(messages)
        if self.fail:
            raise BackendUnavailable("down")
        if self.delay and cancel_event.wait(self.delay):
            from core.backend import EditCancelled
            raise EditCancelled("cancelled")
        items = json.loads(messages[1]["content"])["items"]
        verdicts = [{"id": item["id"], "issue": index == 0, "preferred": item["variants"][0]["form"], "reason": "r"}
                    for index, item in enumerate(items)]
        return json.dumps({"verdicts": verdicts})


def drain(events, timeout=10):
    collected = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        collected.append(event)
        if event[0] == "consistency_finished":
            return collected
    raise AssertionError("runner never finished")


def test_runner_batches_requests_and_reports_verdicts_progress_and_errors(tmp_path):
    project = make_project(tmp_path)
    findings = collect_candidates(project)
    triaged = [finding for finding in findings if finding.kind in MODEL_TRIAGED]
    # more than one batch: pad with synthetic name candidates
    for number in range(20):
        findings.append(Finding("name:x{0}|y{0}".format(number), FINDING_NAME, ["X{0}".format(number), "Y{0}".format(number)],
                                {"X{0}".format(number): 1, "Y{0}".format(number): 1}))
    events = queue.Queue()
    service = VerdictService()
    runner = ConsistencyRunner(project, findings, service, "m", events)
    assert runner.start() == 2 and runner.total == 2
    collected = drain(events)
    assert not runner.active
    streams = [event[1:] for event in collected if event[0] == "stream"]
    collected = [event for event in collected if event[0] != "stream"]
    kinds = [event[0] for event in collected]
    assert kinds == ["consistency_verdicts", "consistency_progress", "consistency_verdicts", "consistency_progress",
                     "consistency_finished"]
    # every request is announced to the thinking view and closed again
    assert streams == [
        (("consistency", 1), "started", {"scope": "consistency", "batch": 1, "total": 2}),
        (("consistency", 1), "finished", "done"),
        (("consistency", 2), "started", {"scope": "consistency", "batch": 2, "total": 2}),
        (("consistency", 2), "finished", "done"),
    ]
    assert collected[1][1:] == (1, 2) and collected[3][1:] == (2, 2)
    verdicts, error = collected[-1][1:]
    assert error is None and len(verdicts) == len(triaged) + 20
    assert apply_verdicts(findings, verdicts) == len(triaged) + 20
    assert sum(1 for finding in findings if finding.status == STATUS_ISSUE) >= 1 + 1  # first of each batch (+ quotes/pov)
    assert all(finding.status in (STATUS_ISSUE, STATUS_OK) for finding in findings if finding.kind in MODEL_TRIAGED)
    assert len(service.requests) == 2

    # nothing to ask: finishes at once
    events = queue.Queue()
    assert ConsistencyRunner(project, findings, VerdictService(), "m", events).start() == 0
    assert events.get(timeout=1) == ("consistency_finished", {}, None)

    # a failing backend leaves the findings unverified and reports the error
    fresh = collect_candidates(project)
    events = queue.Queue()
    ConsistencyRunner(project, fresh, VerdictService(fail=True), "m", events).start()
    collected = drain(events)
    assert collected[-1][0] == "consistency_finished" and collected[-1][2] == "down" and collected[-1][1] == {}
    assert collected[-2] == ("stream", ("consistency", 1), "finished", "error")
    assert all(f.status == STATUS_UNVERIFIED for f in fresh if f.kind in MODEL_TRIAGED)

    # cancellation finishes without error
    events = queue.Queue()
    runner = ConsistencyRunner(project, fresh, VerdictService(delay=5), "m", events)
    runner.start()
    time.sleep(0.1)
    runner.cancel()
    collected = drain(events)
    assert collected[-1] == ("consistency_finished", {}, None)
    assert [event for event in collected if event[0] != "stream"] == [("consistency_finished", {}, None)]
    assert collected[-2] == ("stream", ("consistency", 1), "finished", "cancelled")
