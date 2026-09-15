"""Chapter-spanning consistency check: names, hyphenation, numbers, quotes, POV and tense.

Two layers. ``collect_candidates`` is deterministic and needs no model: it
scans every segment and groups suspicious variants into ``Finding`` objects
with their occurrences. ``build_consistency_messages`` / ``parse_verdicts``
then let the model triage the name, hyphenation and number candidates in
batches ("is this really an inconsistency, and which form is preferred?");
quote styles and POV/tense drift are reported deterministically.

The scan reads each segment's ORIGINAL text and skips the ranges covered by
applied (accepted) changes, so a typo the checks already fixed is not
reported again and every occurrence offset can be handed straight to
``Project.add_author_change`` ("Apply preferred everywhere"). Text that an
accepted change inserts is therefore not scanned.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List

from .backend import EditCancelled
from .change_kinds import levenshtein
from .workflow import EXPLANATION_SYSTEM_PROMPT, _parse_json_object, applied_changes, new_decision_group

FINDING_NAME = "name"
FINDING_HYPHENATION = "hyphenation"
FINDING_NUMBER = "number"
FINDING_QUOTES = "quotes"
FINDING_POV = "pov"
FINDING_TENSE = "tense"
FINDING_KINDS = (FINDING_NAME, FINDING_HYPHENATION, FINDING_NUMBER, FINDING_QUOTES, FINDING_POV, FINDING_TENSE)
FINDING_LABELS = {
    FINDING_NAME: "Name spelling",
    FINDING_HYPHENATION: "Hyphenation",
    FINDING_NUMBER: "Number style",
    FINDING_QUOTES: "Quotation marks",
    FINDING_POV: "Point of view",
    FINDING_TENSE: "Tense",
}
MODEL_TRIAGED = (FINDING_NAME, FINDING_HYPHENATION, FINDING_NUMBER)  # the others are facts, not guesses

STATUS_UNVERIFIED = "unverified"  # deterministic candidate, model verdict pending
STATUS_ISSUE = "issue"  # confirmed inconsistency
STATUS_OK = "ok"  # the model considers the variants intentional
STATUS_DISMISSED = "dismissed"  # hidden by the author
STATUS_LABELS = {
    STATUS_UNVERIFIED: "unverified",
    STATUS_ISSUE: "issue",
    STATUS_OK: "intentional",
    STATUS_DISMISSED: "dismissed",
}

MAX_OCCURRENCES = 10  # stored per variant; the total count is kept separately
MAX_NAME_FINDINGS = 60
MIN_NAME_LENGTH = 4
NAME_MAX_DISTANCE = 2
NAME_MIN_PREFIX = 3  # shared letters at the start or the end
BATCH_SIZE = 15
PROFILE_MIN_MARKERS = 20  # pronouns / verb forms a chapter needs before its profile counts
PROFILE_DEVIATION = 25.0  # percentage points from the project mean

_TOKEN = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)
_NUMBER = re.compile(r"(?<![\w.,:])\d{1,2}(?![\w.,:%])", re.UNICODE)
_SENTENCE_END = ".!?…:\n"
_SKIP_BEFORE = " \t\"'„“”‚‘’»«(—–-"
NUMBER_WORDS = {
    "null": 0, "zero": 0, "eins": 1, "zwei": 2, "two": 2, "drei": 3, "three": 3, "vier": 4, "four": 4,
    "fünf": 5, "five": 5, "sechs": 6, "six": 6, "sieben": 7, "seven": 7, "acht": 8, "eight": 8,
    "neun": 9, "nine": 9, "zehn": 10, "ten": 10, "elf": 11, "eleven": 11, "zwölf": 12, "twelve": 12,
    "dreizehn": 13, "thirteen": 13, "vierzehn": 14, "fourteen": 14, "fünfzehn": 15, "fifteen": 15,
    "sechzehn": 16, "sixteen": 16, "siebzehn": 17, "seventeen": 17, "achtzehn": 18, "eighteen": 18,
    "neunzehn": 19, "nineteen": 19, "zwanzig": 20, "twenty": 20,
}  # "one" is left out: as a pronoun it is far more common than as a number
FIRST_PERSON = {"ich", "mir", "mich", "wir", "uns", "i", "me", "my", "mine", "we", "us", "our", "ours"}
THIRD_PERSON = {"er", "sie", "es", "ihm", "ihn", "ihr", "ihnen", "he", "she", "him", "his", "her", "hers",
                "they", "them", "their", "theirs"}
PAST_MARKERS = {"war", "waren", "hatte", "hatten", "wurde", "wurden", "sagte", "sagten", "ging", "gingen", "kam",
                "kamen", "konnte", "konnten", "musste", "mussten", "wollte", "wollten", "sollte", "sollten",
                "wusste", "wussten", "sah", "sahen", "dachte", "dachten", "stand", "standen",
                "was", "were", "had", "did", "said", "went", "came", "could", "would", "should", "knew", "saw",
                "thought", "stood", "took", "looked", "seemed"}
PRESENT_MARKERS = {"ist", "sind", "hat", "haben", "wird", "werden", "sagt", "sagen", "geht", "gehen", "kommt",
                   "kommen", "kann", "können", "muss", "müssen", "will", "wollen", "soll", "sollen",
                   "weiß", "wissen", "sieht", "sehen", "denkt", "denken", "steht", "stehen",
                   "is", "are", "has", "have", "does", "says", "goes", "comes", "can", "knows", "sees", "thinks",
                   "stands", "takes", "looks", "seems"}


# ------------------------------------------------------------------ model
@dataclass
class Finding:
    """One suspected inconsistency with the places it occurs."""

    finding_id: str
    kind: str
    variants: List[str]
    counts: Dict[str, int] = field(default_factory=dict)
    occurrences: Dict[str, List[dict]] = field(default_factory=dict)  # variant -> [{chapter, segment, start, end, text}]
    status: str = STATUS_UNVERIFIED
    preferred: str = ""
    reason: str = ""
    detail: str = ""  # one line of context, e.g. the chapter's profile
    chapter: int = 0  # POV/tense findings: the deviating chapter

    @property
    def total(self):
        return sum(self.counts.values())

    @property
    def applicable(self):
        """Whether "Apply preferred everywhere" makes sense (a preferred token exists to write)."""
        return self.kind in MODEL_TRIAGED and bool(self.preferred) and self.preferred in self.variants

    def to_dict(self):
        return {
            "id": self.finding_id, "kind": self.kind, "variants": list(self.variants), "counts": dict(self.counts),
            "occurrences": {variant: [dict(item) for item in items] for variant, items in self.occurrences.items()},
            "status": self.status, "preferred": self.preferred, "reason": self.reason, "detail": self.detail,
            "chapter": self.chapter,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            str(data["id"]), str(data.get("kind", FINDING_NAME)), [str(v) for v in data.get("variants") or []],
            {str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            {str(k): [dict(item) for item in items] for k, items in (data.get("occurrences") or {}).items()},
            str(data.get("status") or STATUS_UNVERIFIED), str(data.get("preferred") or ""),
            str(data.get("reason") or ""), str(data.get("detail") or ""), int(data.get("chapter") or 0),
        )


def _finding_id(kind, variants, extra=""):
    key = "|".join(sorted(v.casefold() for v in variants))
    return "{0}:{1}{2}".format(kind, key, ":" + extra if extra else "")


# --------------------------------------------------------------- scanning
def _skipped_ranges(segment, enabled):
    return [(change.start, change.end) for change in applied_changes(segment, enabled)]


def _covered(start, end, ranges):
    return any(start < r_end and r_start < end for r_start, r_end in ranges)


def _sentence_start(text, position):
    """Whether the token at ``position`` opens a sentence (or a line)."""
    index = position - 1
    while index >= 0 and text[index] in _SKIP_BEFORE:
        index -= 1
    return index < 0 or text[index] in _SENTENCE_END


def iter_tokens(project):
    """Yield (chapter, segment, start, end, token, sentence_start) for every word not replaced by an applied change."""
    enabled = project.enabled
    for chapter, segment in project.all_segments():
        if segment.is_blank:
            continue
        skipped = _skipped_ranges(segment, enabled)
        text = segment.text
        for match in _TOKEN.finditer(text):
            if _covered(match.start(), match.end(), skipped):
                continue
            yield chapter, segment, match.start(), match.end(), match.group(0), _sentence_start(text, match.start())


def _occurrence(chapter, segment, start, end, text):
    return {"chapter": chapter.index, "segment": segment.index, "start": start, "end": end, "text": text}


def _add(store, variant, occurrence):
    counts, occurrences = store
    counts[variant] = counts.get(variant, 0) + 1
    bucket = occurrences.setdefault(variant, [])
    if len(bucket) < MAX_OCCURRENCES:
        bucket.append(occurrence)


def _protected(project):
    return {term for term in project.options.glossary if not term.endswith("*")}


def _common_prefix(a, b):
    length = 0
    while length < min(len(a), len(b)) and a[length] == b[length]:
        length += 1
    return length


def _look_alike(a, b):
    """Two spellings of one name: at most two edits apart and sharing three letters at either end."""
    if levenshtein(a, b) > NAME_MAX_DISTANCE:
        return False
    return _common_prefix(a, b) >= NAME_MIN_PREFIX or _common_prefix(a[::-1], b[::-1]) >= NAME_MIN_PREFIX


def _name_candidates(project, tokens):
    """Capitalised words whose spellings differ by at most two letters.

    A form counts as a name when it appears capitalised somewhere other than
    at a sentence start and never in lower case; all its occurrences
    (sentence starts included) are then collected.
    """
    counts, occurrences = {}, {}
    lowercase_seen = set()
    mid_sentence = set()
    for _, _, _, _, token, at_start in tokens:
        if token[0].islower():
            lowercase_seen.add(token.casefold())
        elif not at_start and len(token) >= MIN_NAME_LENGTH and token[0].isupper():
            mid_sentence.add(token)
    for chapter, segment, start, end, token, _ in tokens:
        if token in mid_sentence:
            _add((counts, occurrences), token, _occurrence(chapter, segment, start, end, token))
    forms = [form for form in counts if form.casefold() not in lowercase_seen]
    forms.sort(key=lambda form: (-counts[form], form))
    protected = _protected(project)
    findings = []
    used = set()
    for index, form in enumerate(forms):
        if form in used:
            continue
        group = [form]
        for other in forms[index + 1:]:
            if other in used or other.casefold() == form.casefold():
                continue
            if _look_alike(form.casefold(), other.casefold()):
                group.append(other)
        if len(group) < 2:
            continue
        if all(variant in protected for variant in group):
            continue  # every form is protected on purpose: intentional variants
        used.update(group)
        preferred = next((variant for variant in group if variant in protected), "")
        findings.append(Finding(
            _finding_id(FINDING_NAME, group), FINDING_NAME, group,
            {variant: counts[variant] for variant in group},
            {variant: occurrences[variant] for variant in group},
            preferred=preferred,
            detail=", ".join("{0} ×{1}".format(variant, counts[variant]) for variant in group),
        ))
        if len(findings) >= MAX_NAME_FINDINGS:
            break
    return findings


def _hyphenation_candidates(project, tokens):
    """The same letters written with and without a hyphen (E-Mail / Email)."""
    groups = {}
    for chapter, segment, start, end, token, _ in tokens:
        key = token.replace("-", "").casefold()
        if not key or len(key) < 4:
            continue
        store = groups.setdefault(key, ({}, {}))
        _add(store, token, _occurrence(chapter, segment, start, end, token))
    findings = []
    for key, (counts, occurrences) in groups.items():
        hyphenated = [form for form in counts if "-" in form]
        plain = [form for form in counts if "-" not in form]
        if not hyphenated or not plain:
            continue
        variants = sorted(counts, key=lambda form: (-counts[form], form))
        findings.append(Finding(
            _finding_id(FINDING_HYPHENATION, variants), FINDING_HYPHENATION, variants, dict(counts),
            {variant: occurrences[variant] for variant in variants},
            detail=", ".join("{0} ×{1}".format(variant, counts[variant]) for variant in variants),
        ))
    findings.sort(key=lambda finding: -finding.total)
    return findings


def _number_candidates(project, tokens):
    """Values 0-20 written both as digits and as words."""
    enabled = project.enabled
    values = {}
    for chapter, segment, start, end, token, _ in tokens:
        value = NUMBER_WORDS.get(token.casefold())
        if value is not None:
            _add(values.setdefault(value, ({}, {})), token, _occurrence(chapter, segment, start, end, token))
    for chapter, segment in project.all_segments():
        if segment.is_blank:
            continue
        skipped = _skipped_ranges(segment, enabled)
        for match in _NUMBER.finditer(segment.text):
            if _covered(match.start(), match.end(), skipped):
                continue
            value = int(match.group(0))
            if value <= 20:
                _add(values.setdefault(value, ({}, {})), match.group(0),
                     _occurrence(chapter, segment, match.start(), match.end(), match.group(0)))
    findings = []
    for value in sorted(values):
        counts, occurrences = values[value]
        digits = [form for form in counts if form.isdigit()]
        words = [form for form in counts if not form.isdigit()]
        if not digits or not words:
            continue
        variants = sorted(counts, key=lambda form: (-counts[form], form))
        findings.append(Finding(
            _finding_id(FINDING_NUMBER, variants, str(value)), FINDING_NUMBER, variants, dict(counts),
            {variant: occurrences[variant] for variant in variants},
            detail="{0}: ".format(value) + ", ".join("{0} ×{1}".format(v, counts[v]) for v in variants),
        ))
    return findings


def _quote_candidates(project):
    """Several styles of double quotation marks in one manuscript."""
    enabled = project.enabled
    counts, occurrences = {}, {}
    german = any("„" in segment.text for _, segment in project.all_segments())
    for chapter, segment in project.all_segments():
        if segment.is_blank:
            continue
        skipped = _skipped_ranges(segment, enabled)
        for position, char in enumerate(segment.text):
            if _covered(position, position + 1, skipped):
                continue
            style = None
            if char == "„":
                style = "„“"
            elif char == "\"":
                style = "\"\""
            elif char in "»«":
                style = "»«"
            elif char == "”" or (char == "“" and not german):
                style = "“”"
            if style is None:
                continue
            context = segment.text[max(0, position - 12):position + 13].replace("\n", " ")
            _add((counts, occurrences), style, _occurrence(chapter, segment, position, position + 1, context))
    if len(counts) < 2:
        return []
    variants = sorted(counts, key=lambda style: (-counts[style], style))
    return [Finding(
        _finding_id(FINDING_QUOTES, variants), FINDING_QUOTES, variants, dict(counts),
        {variant: occurrences[variant] for variant in variants}, status=STATUS_ISSUE, preferred=variants[0],
        reason="Several quotation-mark styles are mixed; the most frequent one is listed first.",
        detail=", ".join("{0} ×{1}".format(v, counts[v]) for v in variants),
    )]


def _profile_candidates(project, tokens, kind, group_a, group_b, label_a, label_b):
    """Chapters whose share of ``group_a`` markers deviates from the project mean by > 25 points."""
    per_chapter = {}
    firsts = {}
    for chapter, segment, start, end, token, _ in tokens:
        word = token.casefold()
        if word in group_a:
            side = "a"
        elif word in group_b:
            side = "b"
        else:
            continue
        stats = per_chapter.setdefault(chapter.index, {"a": 0, "b": 0, "title": chapter.title})
        stats[side] += 1
        bucket = firsts.setdefault((chapter.index, side), [])
        if len(bucket) < MAX_OCCURRENCES:
            bucket.append(_occurrence(chapter, segment, start, end, token))
    total_a = sum(stats["a"] for stats in per_chapter.values())
    total = sum(stats["a"] + stats["b"] for stats in per_chapter.values())
    if total < PROFILE_MIN_MARKERS:
        return []
    mean = 100.0 * total_a / total
    findings = []
    for chapter_index, stats in sorted(per_chapter.items()):
        markers = stats["a"] + stats["b"]
        if markers < PROFILE_MIN_MARKERS:
            continue
        share = 100.0 * stats["a"] / markers
        if abs(share - mean) <= PROFILE_DEVIATION:
            continue
        variants = [label_a, label_b]
        findings.append(Finding(
            _finding_id(kind, variants, str(chapter_index)), kind, variants,
            {label_a: stats["a"], label_b: stats["b"]},
            {label_a: firsts.get((chapter_index, "a"), []), label_b: firsts.get((chapter_index, "b"), [])},
            status=STATUS_ISSUE, chapter=chapter_index,
            reason="Chapter {0} deviates from the rest of the manuscript.".format(chapter_index),
            detail="{0}: {1:.0f}% {2} (project mean {3:.0f}%)".format(stats["title"], share, label_a, mean),
        ))
    return findings


def collect_candidates(project, previous=None):
    """Return every deterministic candidate as a Finding; statuses of ``previous`` findings carry over by id."""
    tokens = list(iter_tokens(project))
    findings = []
    findings.extend(_name_candidates(project, tokens))
    findings.extend(_hyphenation_candidates(project, tokens))
    findings.extend(_number_candidates(project, tokens))
    findings.extend(_quote_candidates(project))
    findings.extend(_profile_candidates(project, tokens, FINDING_POV, FIRST_PERSON, THIRD_PERSON,
                                        "first person", "third person"))
    findings.extend(_profile_candidates(project, tokens, FINDING_TENSE, PAST_MARKERS, PRESENT_MARKERS,
                                        "past tense", "present tense"))
    if previous:
        known = {finding.finding_id: finding for finding in previous}
        for finding in findings:
            old = known.get(finding.finding_id)
            if old is None:
                continue
            if old.status in (STATUS_DISMISSED, STATUS_OK) or (old.status == STATUS_ISSUE and old.kind in MODEL_TRIAGED):
                finding.status = old.status
                finding.reason = old.reason or finding.reason
            if old.preferred and (not finding.preferred or old.status != STATUS_UNVERIFIED):
                finding.preferred = old.preferred
    return findings


def find_occurrences(project, variant):
    """Every place ``variant`` occurs as a whole token (ignoring text replaced by applied changes)."""
    found = []
    if variant.isdigit():
        enabled = project.enabled
        for chapter, segment in project.all_segments():
            if segment.is_blank:
                continue
            skipped = _skipped_ranges(segment, enabled)
            for match in _NUMBER.finditer(segment.text):
                if match.group(0) == variant and not _covered(match.start(), match.end(), skipped):
                    found.append(_occurrence(chapter, segment, match.start(), match.end(), variant))
        return found
    for chapter, segment, start, end, token, _ in iter_tokens(project):
        if token == variant:
            found.append(_occurrence(chapter, segment, start, end, token))
    return found


# ------------------------------------------------------------ model triage
def _sentence_around(segment_text, start, end, radius=140):
    """The sentence (or a window) containing ``[start, end)``, single-spaced."""
    left = max(0, start - radius)
    right = min(len(segment_text), end + radius)
    window = segment_text[left:right]
    head = window[:start - left]
    tail = window[end - left:]
    cut = max(head.rfind(mark) for mark in ".!?\n")
    if cut != -1:
        head = head[cut + 1:]
    ends = [tail.find(mark) for mark in ".!?\n"]
    ends = [position for position in ends if position != -1]
    if ends:
        tail = tail[:min(ends) + 1]
    return " ".join((head + segment_text[start:end] + tail).split())


def representative_sentences(project, finding):
    """One sentence per variant, taken from the first stored occurrence."""
    sentences = {}
    for variant in finding.variants:
        for occurrence in finding.occurrences.get(variant, []):
            _, segment = project.find(occurrence["chapter"], occurrence["segment"])
            if segment is None:
                continue
            sentences[variant] = _sentence_around(segment.text, occurrence["start"], occurrence["end"])
            break
    return sentences


def build_consistency_messages(project, batch):
    """Ask the model which candidate groups are real inconsistencies and which form to prefer."""
    items = []
    for finding in batch:
        sentences = representative_sentences(project, finding)
        items.append({
            "id": finding.finding_id,
            "type": finding.kind,
            "variants": [
                {"form": variant, "count": finding.counts.get(variant, 0), "example": sentences.get(variant, "")}
                for variant in finding.variants
            ],
        })
    request = {
        "task": (
            "Each item lists spellings or forms that occur in the same manuscript and might be inconsistent "
            "(a name spelled two ways, a compound with and without a hyphen, a number as digits and as a word). "
            "Decide for every item whether it is an inconsistency the author should fix (\"issue\": true) or an "
            "intentional difference such as two different characters, an inflected form, or a deliberate style "
            "(\"issue\": false). When it is an issue, name the preferred form exactly as it should be written "
            "and give a one-sentence reason in the language of the examples."
        ),
        "answer_format": {"verdicts": [{"id": "<id>", "issue": True, "preferred": "<form>", "reason": "<why>"}]},
        "items": items,
    }
    return [
        {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, indent=1)},
    ]


def parse_verdicts(text):
    """Return {id: {"issue": bool, "preferred": str, "reason": str}} from a model answer (tolerant)."""
    data = _parse_json_object(text)
    if data is None:
        return {}
    items = data.get("verdicts") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict):
        # a bare {"<id>": {...}} mapping
        items = [dict(value, id=key) for key, value in data.items() if isinstance(value, dict)]
    if not isinstance(items, list):
        return {}
    verdicts = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        issue = item.get("issue")
        if isinstance(issue, str):
            issue = issue.strip().lower() in ("true", "yes", "1", "issue")
        verdicts[str(item["id"])] = {
            "issue": bool(issue),
            "preferred": " ".join(str(item.get("preferred") or "").split()),
            "reason": " ".join(str(item.get("reason") or "").split()),
        }
    return verdicts


def apply_verdicts(findings, verdicts):
    """Update findings in place from parsed verdicts; returns how many were updated."""
    updated = 0
    for finding in findings:
        verdict = verdicts.get(finding.finding_id)
        if verdict is None or finding.status == STATUS_DISMISSED:
            continue
        finding.status = STATUS_ISSUE if verdict["issue"] else STATUS_OK
        finding.reason = verdict["reason"]
        preferred = verdict["preferred"]
        if preferred:
            match = next((variant for variant in finding.variants if variant.casefold() == preferred.casefold()), None)
            finding.preferred = match or preferred
        elif verdict["issue"] and not finding.preferred:
            finding.preferred = finding.variants[0]
        updated += 1
    return updated


def batches(findings, size=BATCH_SIZE):
    """Split the model-triaged, undecided findings into request batches."""
    pending = [finding for finding in findings if finding.kind in MODEL_TRIAGED and finding.status == STATUS_UNVERIFIED]
    return [pending[index:index + size] for index in range(0, len(pending), size)]


# ------------------------------------------------------------ applying
def apply_preferred(project, finding, group=None):
    """Write the preferred form over every other variant as author corrections; returns (count, group)."""
    if not finding.applicable:
        return 0, None
    group = group or new_decision_group()
    count = 0
    for variant in finding.variants:
        if variant == finding.preferred:
            continue
        for occurrence in find_occurrences(project, variant):
            _, segment = project.find(occurrence["chapter"], occurrence["segment"])
            if segment is None or segment.text[occurrence["start"]:occurrence["end"]] != variant:
                continue
            replacement = _match_case(variant, finding.preferred)
            change = project.add_author_change(occurrence["chapter"], occurrence["segment"], occurrence["start"],
                                               occurrence["end"], replacement, group=group)
            if change is not None:
                count += 1
    return count, group


def _match_case(sample, replacement):
    """Keep an all-caps or lower-case sample's casing for word-like replacements."""
    if replacement.isdigit() or sample.isdigit():
        return replacement
    if sample.isupper() and len(sample) > 1:
        return replacement.upper()
    if sample[0].islower() and replacement[0].isupper() and replacement[1:].islower():
        return replacement[0].lower() + replacement[1:]
    return replacement


# --------------------------------------------------------------- runner
class ConsistencyRunner:
    """Collect the model's verdicts on the undecided candidates on a background thread.

    Events: ``("consistency_verdicts", {id: verdict})`` after every batch,
    followed by ``("consistency_progress", done, total)``, and finally
    ``("consistency_finished", all_verdicts, error)`` (``error`` is None
    unless a request failed; a cancelled run finishes without error). Token
    counts arrive as ``("workflow_usage", UsageRecord)`` like the evaluation
    runner's. The caller applies the verdicts to the project's findings on
    its own thread.
    """

    def __init__(self, project, findings, service, model, events, max_tokens=2048):
        self.project = project
        self.batches = batches(findings)
        self.service = service
        self.model = model
        self.events = events
        self.max_tokens = max_tokens
        self.cancel_event = threading.Event()
        self.done = 0
        self.total = len(self.batches)
        self._thread = None

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Start the requests; returns the number of batches (0 = nothing to ask, finished at once)."""
        if not self.batches:
            self.events.put(("consistency_finished", {}, None))
            return 0
        self._thread = threading.Thread(target=self._run, name="teai-consistency", daemon=True)
        self._thread.start()
        return self.total

    def cancel(self):
        self.cancel_event.set()

    def _report_usage(self, record):
        self.events.put(("workflow_usage", record))

    def _run(self):
        collected = {}
        error = None
        for batch in self.batches:
            if self.cancel_event.is_set():
                break
            try:
                answer = self.service.generate(self.model, build_consistency_messages(self.project, batch),
                                               self.cancel_event, max_tokens=self.max_tokens,
                                               on_usage=self._report_usage)
            except EditCancelled:
                break
            except Exception as exc:  # the UI reports it; the findings stay unverified
                error = str(exc)
                break
            verdicts = parse_verdicts(answer)
            collected.update(verdicts)
            self.done += 1
            self.events.put(("consistency_verdicts", verdicts))
            self.events.put(("consistency_progress", self.done, self.total))
        self.events.put(("consistency_finished", collected, error))
