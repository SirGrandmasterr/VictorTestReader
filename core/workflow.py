"""Automatic manuscript review: checks, changes, merging, evaluation, persistence.

A *project* is one manuscript split into chapters and segments (see
``chunking``). Every segment is evaluated by up to three independent checks
(spelling, grammar, expression). Each check asks the model for the corrected
segment, diffs it against the original into word-level *changes* with exact
character offsets, and asks the model for a one-line explanation per change.

Because the checks run independently on the same original text, the author
can accept or reject each change in any order. When two accepted changes touch
the same words, the earlier check wins (spelling > grammar > expression) and
the other is reported as *superseded* instead of being applied.
"""

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .backend import BackendUnavailable, EditCancelled, OutputTruncated, StructuredOutputUnsupported
from .change_kinds import CHANGE_KIND_LABELS, CHANGE_KINDS, classify_change, edit_distance
from .chunking import (
    DEFAULT_MAX_CHAPTER_CHARS,
    DEFAULT_MAX_CHARS,
    DEFAULT_TARGET_CHARS,
    chapters_from_outline,
    paragraph_outline,
    paragraphs,
    split_document,
    split_segments,
    word_count,
)
from .diff_engine import build_edit_session
from .documents import KIND_TXT, DocumentError, LoadedDocument, load_document, save_document, text_document
from .models import ACCEPTED, PENDING, REJECTED, ReviewItem

PROJECT_FORMAT = 2  # 2: "document" (source kind and paragraph locators for .docx/.odt export)
PROJECT_DIR_SUFFIX = ".teai"
PROJECT_FILE = "project.json"
DECISION_LOG_LIMIT = 5000  # oldest decisions are dropped beyond this

CHECK_SPELLING = "spelling"
CHECK_GRAMMAR = "grammar"
CHECK_EXPRESSION = "expression"
CHECKS = (CHECK_SPELLING, CHECK_GRAMMAR, CHECK_EXPRESSION)  # the model checks, also their priority order
# The author's own corrections live under a pseudo-check that is always enabled,
# never queued for the model and beats every model check when changes overlap.
CHECK_AUTHOR = "author"
ALL_CHECKS = (CHECK_AUTHOR,) + CHECKS  # priority order for merging
CHECK_LABELS = {
    CHECK_AUTHOR: "Author",
    CHECK_SPELLING: "Spelling",
    CHECK_GRAMMAR: "Grammar",
    CHECK_EXPRESSION: "Expression",
}
CHECK_DESCRIPTIONS = {
    CHECK_AUTHOR: "Corrections you added yourself while reviewing.",
    CHECK_SPELLING: "Typos, misspellings, capitalization, accents and umlauts.",
    CHECK_GRAMMAR: "Agreement, tense, cases, articles, commas and sentence structure.",
    CHECK_EXPRESSION: "Clearer, more natural wording where a phrase is awkward or vague.",
}
AUTHOR_EXPLANATION = "Author's correction"
EDITED_REPORT_MARK = "✎ edited by the author"
CHECK_INSTRUCTIONS = {
    CHECK_SPELLING: (
        "Correct spelling mistakes and typos only: misspelled words, wrong or "
        "swapped letters, missing or wrong accents and umlauts, and capitalization "
        "errors. Do not change punctuation, grammar, word choice, or sentence "
        "structure."
    ),
    CHECK_GRAMMAR: (
        "Fix grammar and punctuation errors only: agreement, tense, cases, articles, "
        "prepositions, missing or misplaced commas, sentence fragments, and run-on "
        "sentences. Keep the author's wording and style; do not rephrase sentences "
        "that are already correct. Do not rewrite sentences."
    ),
    CHECK_EXPRESSION: (
        "Improve expression only where it clearly helps: replace awkward, repetitive, "
        "vague, or unidiomatic phrasing with clearer and more natural wording. "
        "Preserve the author's voice, tone, meaning, and rhythm; leave sentences that "
        "already read well untouched, and never add new content. Never add facts, "
        "names, clauses or sentences that are not already in the text."
    ),
}
FALLBACK_EXPLANATIONS = {
    CHECK_AUTHOR: AUTHOR_EXPLANATION + ".",
    CHECK_SPELLING: "Spelling correction.",
    CHECK_GRAMMAR: "Grammar or punctuation fix.",
    CHECK_EXPRESSION: "Clearer, more natural expression.",
}
EXPLANATION_SYSTEM_PROMPT = (
    "You are an editor explaining proposed corrections to an author. Answer with a "
    "single JSON object and nothing else."
)
SAME_LANGUAGE = "same as text"

# Explanations that need no model: trivial kinds get a fixed sentence in the
# explanation language (see ``canned_explanation``); everything else is asked
# in batches of EXPLANATION_GROUP_SIZE changes of one check per request.
CANNED_EXPLANATIONS = {
    "whitespace": {"en": "Spacing corrected.", "de": "Leerzeichen korrigiert."},
    "punctuation": {"en": "Punctuation corrected.", "de": "Zeichensetzung korrigiert."},
    "capitalization": {"en": "Capitalization corrected.", "de": "Groß-/Kleinschreibung korrigiert."},
    "spelling": {"en": "Typo: one letter corrected.", "de": "Tippfehler: ein Buchstabe korrigiert."},
}
CANNED_SPELLING_MAX_DISTANCE = 1
EXPLANATION_GROUP_SIZE = 6
EXPLANATION_MAX_TOKENS = 2048
LANGUAGE_CODES = {
    "english": "en", "englisch": "en", "en": "en",
    "german": "de", "deutsch": "de", "de": "de",
}
# Frequent function words used to guess whether a segment is German or English.
_GERMAN_MARKERS = frozenset(
    "der die das und nicht ist ein eine mit sich auf für von dem den dass ich er sie es zu wir "
    "auch wie oder aber noch wenn nur nach bei aus über als war hatte wurde sind".split()
)
_ENGLISH_MARKERS = frozenset(
    "the and of to is in that it was for with as his her on at be this have not but are they "
    "were she he you by from had which their there been would could into".split()
)

# Standing instructions from the author, appended to every check's instruction
# and to the explanation prompt (see ``format_style_guide``).
STYLE_GUIDE_MAX_CHARS = 1500
STYLE_GUIDE_HEADER = (
    "Author's instructions (they take precedence over the rules above where they conflict):"
)

# Protected terms (names, invented words, technical terms). They are listed in
# the prompt up to these caps; beyond them only the post-filter protects them.
GLOSSARY_MAX_TERMS = 200
GLOSSARY_MAX_CHARS = 2000
GLOSSARY_HEADER = "Protected terms — never change their spelling, capitalization or form: "
GLOSSARY_CONTEXT = 20  # characters around an insertion point checked against the glossary
GLOSSARY_EXPLANATION = "Suppressed: touches a protected term."

# Hallucination guard: heuristics that mark a change as possibly inventing
# content rather than correcting it (see ``flag_suspicious``).
FLAG_GROWTH = "growth"
FLAG_NOVEL_WORDS = "novel_words"
FLAG_REWRITE_IN_STRICT_CHECK = "rewrite_in_strict_check"
FLAG_LABELS = {
    FLAG_GROWTH: "The replacement is much longer than the text it replaces.",
    FLAG_NOVEL_WORDS: "Adds words that appear nowhere else in the segment.",
    FLAG_REWRITE_IN_STRICT_CHECK: "A spelling or grammar check rewrote a whole phrase.",
}
FLAG_REPORT_MARK = "\u26a0 possibly invented"
GROWTH_FACTOR = 1.6
GROWTH_SLACK = 12
NOVEL_MIN_WORDS = 2
NOVEL_MIN_LENGTH = 4
STRICT_CHECKS = (CHECK_SPELLING, CHECK_GRAMMAR)
# Function words (>= 4 letters) that never count as "novel" content, German and English.
STOPWORDS = frozenset("""
about above after again against alone along already also although always among another anyone anything
around because been before being below between both cannot could does doing done down during each either
else enough even ever every everything from further having here hers herself himself however into itself
just last less like many might more most much must myself neither never nevertheless next nobody none
nothing often once only other ours ourselves over perhaps quite rather really same several shall should
since some something sometimes soon still such than that their theirs them themselves then there these
they things this those though through thus toward towards under until upon very were what whatever when
whenever where whether which while whom whose will with within without would yours yourself
aber alle allem allen aller allerdings alles also andere anderem anderen anderer anderes auch aufs beide
beiden beim bereits bevor bloss bloß dabei dadurch dafür dagegen daher damit danach dann daran darauf
daraus darin darum darunter dass davon dazu dein deine deinem deinen deiner deines denen denn dennoch deren
derer deshalb dessen desto dich diese diesem diesen dieser dieses doch dort durch eben eher eine einem einen
einer eines einige einigen einiger einiges einmal entweder erst etwa etwas euch euer eure eurem euren eurer
eures falls ganz gegen gern gewesen haben habt hast hatte hatten hätte hätten hier hinter ihre ihrem ihren
ihrer ihres immer indem innerhalb irgend jede jedem jeden jeder jedes jedoch jene jenem jenen jener jenes
jetzt kann kannst kaum kein keine keinem keinen keiner keines können könnt könnte könnten machen mehr mein
meine meinem meinen meiner meines muss musst musste müssen nach nachdem neben nicht nichts noch nochmals nun
obwohl oder ohne schon sehr sein seine seinem seinen seiner seines seit seitdem selbst sich sind soll sollen
sollte sollten solche solchem solchen solcher solches somit sondern sonst soweit sowie sowohl trotz trotzdem
über überhaupt unser unsere unserem unseren unserer unseres unter viel viele vielem vielen vieler vielleicht
vom von vor während wann waren warst warum weder weil weiter weitere weiterem weiteren weiterer weiteres
welche welchem welchen welcher welches wenig wenige weniger wenn werde werden werdet wieder wird wirst wobei
wodurch wohl wollen wollte wollten worden wurde wurden würde würden zwar zwischen
""".split())
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Evaluation modes: one combined request per segment that returns every
# category at once, or the classic three separate requests per segment.
EVALUATION_COMBINED = "combined"
EVALUATION_SEPARATE = "separate"
EVALUATION_MODES = (EVALUATION_COMBINED, EVALUATION_SEPARATE)
EVALUATION_LABELS = {
    EVALUATION_COMBINED: "One combined request per segment (faster)",
    EVALUATION_SEPARATE: "Three separate requests (more thorough)",
}
COMBINED_SYSTEM_PROMPT = (
    "You are a careful text editor reviewing one segment of a manuscript. Preserve the "
    "original language, meaning, paragraphs, quotations and formatting. Answer with a "
    "single JSON object and nothing else."
)
COMBINED_MAX_TOKENS = 4096
COMBINED_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CHECKS)},
                    "original": {"type": "string"},
                    "replacement": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["category", "original", "replacement", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}
COMBINED_EXAMPLE = (
    '{"edits": [{"category": "spelling", "original": "teh dog", "replacement": "the dog", '
    '"reason": "Typo."}]}'
)

STATUS_QUEUED = "queued"
STATUS_ERROR = "error"
STATUS_CLEAN = "clean"
STATUS_READY = "ready"
STATUS_REVIEWED = "reviewed"

STATE_APPLIED = "applied"
STATE_SUPERSEDED = "superseded"
STATE_REJECTED = "rejected"
STATE_PENDING = "pending"

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


# ------------------------------------------------------------------ models
@dataclass
class Change:
    """One word-level edit proposed by a check, anchored in the segment text.

    ``kind`` (see ``change_kinds.CHANGE_KINDS``) describes what the edit does
    regardless of which check proposed it; it is derived from the two texts
    whenever it is missing or unknown, so older project files need no upgrade.

    ``proposed_text`` is what gets applied; when the author reworded a
    suggestion (``edited``), ``model_proposed_text`` still holds what the
    model originally proposed.
    """

    change_id: str
    check: str
    start: int
    end: int
    original_text: str
    proposed_text: str
    explanation: str = ""
    decision: str = PENDING
    kind: str = ""
    flags: List[str] = field(default_factory=list)  # hallucination-guard reason ids, see flag_suspicious
    model_proposed_text: Optional[str] = None  # None = same as proposed_text (never edited)
    edited: bool = False

    def __post_init__(self):
        if self.kind not in CHANGE_KINDS:
            self.kind = classify_change(self.original_text, self.proposed_text)
        self.flags = [str(flag) for flag in (self.flags or []) if flag]
        if self.model_proposed_text is None:
            self.model_proposed_text = self.proposed_text
        self.edited = bool(self.edited)

    @property
    def flagged(self):
        return bool(self.flags)

    @property
    def distance(self):
        """Levenshtein distance between the collapsed original and proposed text."""
        return edit_distance(self.original_text, self.proposed_text)

    @property
    def priority(self):
        return ALL_CHECKS.index(self.check)

    @property
    def is_author(self):
        return self.check == CHECK_AUTHOR

    def to_dict(self):
        return {
            "id": self.change_id,
            "check": self.check,
            "start": self.start,
            "end": self.end,
            "original": self.original_text,
            "proposed": self.proposed_text,
            "explanation": self.explanation,
            "decision": self.decision,
            "kind": self.kind,
            "flags": list(self.flags),
            "model_proposed": self.model_proposed_text,
            "edited": self.edited,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["id"], data["check"], int(data["start"]), int(data["end"]),
            data.get("original", ""), data.get("proposed", ""),
            data.get("explanation", ""), data.get("decision", PENDING),
            data.get("kind") or "", list(data.get("flags") or []),
            data.get("model_proposed"), bool(data.get("edited", False)),
        )


@dataclass
class CheckResult:
    """Outcome of running one check on one segment."""

    check: str
    status: str  # "done" or "error"
    proposed_text: str = ""
    changes: List[Change] = field(default_factory=list)
    error: str = ""
    model: str = ""
    duration: float = 0.0
    explained: bool = False
    suppressed: List[Change] = field(default_factory=list)  # dropped by the glossary post-filter
    method: str = EVALUATION_SEPARATE  # "combined" (one request for all checks) or "separate"

    def to_dict(self):
        return {
            "check": self.check,
            "status": self.status,
            "proposed": self.proposed_text,
            "changes": [change.to_dict() for change in self.changes],
            "error": self.error,
            "model": self.model,
            "duration": round(self.duration, 2),
            "explained": self.explained,
            "suppressed": [change.to_dict() for change in self.suppressed],
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data):
        method = data.get("method")
        return cls(
            data["check"], data.get("status", "done"), data.get("proposed", ""),
            [Change.from_dict(item) for item in data.get("changes", [])],
            data.get("error", ""), data.get("model", ""), float(data.get("duration", 0.0)),
            bool(data.get("explained", False)),
            [Change.from_dict(item) for item in data.get("suppressed", []) or []],
            method if method in EVALUATION_MODES else EVALUATION_SEPARATE,
        )


@dataclass
class Segment:
    """A reviewable chunk with per-check results and decisions."""

    index: int
    text: str
    trailing: str = ""
    results: Dict[str, CheckResult] = field(default_factory=dict)

    @property
    def is_blank(self):
        return not self.text.strip()

    def changes(self, enabled, hidden_kinds=()):
        """Return changes of enabled, successful checks sorted by position.

        ``hidden_kinds`` is a display filter for the UI only: status(),
        progress() and render_segment() always see every change.
        """
        found = []
        for check in ALL_CHECKS:
            if check != CHECK_AUTHOR and not enabled.get(check):
                continue
            result = self.results.get(check)
            if result and result.status == "done":
                found.extend(change for change in result.changes if change.kind not in hidden_kinds)
        return sorted(found, key=lambda change: (change.start, change.priority, change.end))

    def author_result(self, create=False):
        """Return the pseudo-result holding the author's own corrections (created on demand)."""
        result = self.results.get(CHECK_AUTHOR)
        if result is None and create:
            result = CheckResult(CHECK_AUTHOR, "done", explained=True)
            self.results[CHECK_AUTHOR] = result
        return result

    def find_change(self, change_id):
        for result in self.results.values():
            for change in result.changes:
                if change.change_id == change_id:
                    return change
        return None

    def status(self, enabled):
        """Summarise where this segment stands for the enabled checks."""
        if self.is_blank:
            return STATUS_CLEAN
        needed = [check for check in CHECKS if enabled.get(check)]
        if any(check not in self.results for check in needed):
            return STATUS_QUEUED
        if any(self.results[check].status == "error" for check in needed):
            return STATUS_ERROR
        changes = self.changes(enabled)
        if not changes:
            return STATUS_CLEAN
        if any(change.decision == PENDING for change in changes):
            return STATUS_READY
        return STATUS_REVIEWED

    def to_dict(self):
        return {
            "index": self.index,
            "text": self.text,
            "trailing": self.trailing,
            "results": {check: result.to_dict() for check, result in self.results.items()},
        }

    @classmethod
    def from_dict(cls, data):
        segment = cls(int(data["index"]), data["text"], data.get("trailing", ""))
        for check, result in (data.get("results") or {}).items():
            segment.results[check] = CheckResult.from_dict(result)
        return segment


@dataclass
class Chapter:
    index: int
    title: str
    heading: str
    trailing: str = ""
    segments: List[Segment] = field(default_factory=list)

    @property
    def body(self):
        return "".join(segment.text + segment.trailing for segment in self.segments)

    @property
    def file_name(self):
        safe = re.sub(r"[^\w\- ]+", "", self.title, flags=re.UNICODE).strip() or "chapter"
        return "{0:02d} - {1}.txt".format(self.index, safe[:60])

    def to_dict(self):
        return {
            "index": self.index,
            "title": self.title,
            "heading": self.heading,
            "trailing": self.trailing,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            int(data["index"]), data["title"], data.get("heading", ""), data.get("trailing", ""),
            [Segment.from_dict(item) for item in data.get("segments", [])],
        )


@dataclass
class ProjectOptions:
    checks: Dict[str, bool] = field(default_factory=lambda: {check: True for check in CHECKS})
    auto_accept: Dict[str, bool] = field(default_factory=lambda: {check: False for check in CHECKS})
    target_chars: int = DEFAULT_TARGET_CHARS
    max_chars: int = DEFAULT_MAX_CHARS
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS
    chapter_mode: str = "auto"  # auto | size | single | model
    explain: bool = True
    language: str = SAME_LANGUAGE
    parallelism: int = 2
    style_guide: str = ""  # author's standing instructions, see build_check_instruction
    glossary: List[str] = field(default_factory=list)  # protected terms, see glossary_matcher
    hidden_kinds: List[str] = field(default_factory=list)  # change kinds hidden in the review (view filter)
    evaluation_mode: str = EVALUATION_COMBINED  # see EVALUATION_MODES

    def enabled_checks(self):
        return [check for check in CHECKS if self.checks.get(check)]

    @property
    def combined(self):
        return self.evaluation_mode == EVALUATION_COMBINED

    def to_dict(self):
        return {
            "checks": dict(self.checks),
            "auto_accept": dict(self.auto_accept),
            "target_chars": self.target_chars,
            "max_chars": self.max_chars,
            "max_chapter_chars": self.max_chapter_chars,
            "chapter_mode": self.chapter_mode,
            "explain": self.explain,
            "language": self.language,
            "parallelism": self.parallelism,
            "style_guide": self.style_guide,
            "glossary": list(self.glossary),
            "hidden_kinds": list(self.hidden_kinds),
            "evaluation_mode": self.evaluation_mode,
        }

    @classmethod
    def from_dict(cls, data):
        options = cls()
        for key, value in (data or {}).items():
            if key in ("checks", "auto_accept"):
                merged = getattr(options, key)
                merged.update({k: bool(v) for k, v in (value or {}).items() if k in CHECKS})
            elif key == "style_guide":
                options.style_guide = str(value or "")
            elif key == "glossary":
                options.glossary = parse_glossary(value)
            elif key == "hidden_kinds":
                options.hidden_kinds = [kind for kind in (value or []) if kind in CHANGE_KINDS]
            elif key == "evaluation_mode":
                options.evaluation_mode = value if value in EVALUATION_MODES else EVALUATION_COMBINED
            elif hasattr(options, key):
                setattr(options, key, value)
        return options


@dataclass
class Project:
    """One manuscript under automatic review."""

    name: str
    source_path: str
    created_at: str
    options: ProjectOptions
    chapters: List[Chapter]
    model: str = ""
    backend: str = ""
    method: str = ""
    # one dict per accept/reject the author made, oldest first; see decide() for the keys
    decision_log: List[dict] = field(default_factory=list)
    source_sha256: str = ""  # fingerprint of the manuscript text the project was split from
    source_mtime: float = 0.0
    consistency: list = field(default_factory=list)  # consistency.Finding objects, see core/consistency.py
    # source format and paragraph locators (documents.LoadedDocument.to_dict without the text)
    document: dict = field(default_factory=lambda: {"kind": KIND_TXT, "paragraphs": [], "meta": {}})
    root: Optional[Path] = field(default=None, repr=False, compare=False)
    source_check_reason: str = field(default="", repr=False, compare=False)  # set by source_changed()

    # ------------------------------------------------------------ queries
    @property
    def document_kind(self):
        return self.document.get("kind") or KIND_TXT

    def original_text(self):
        """The manuscript text this project was split from (chapters concatenate back to it)."""
        return "".join(chapter.heading + chapter.body + chapter.trailing for chapter in self.chapters)

    def loaded_document(self):
        """Rebuild the LoadedDocument (text plus paragraph locators) the project was created from."""
        return LoadedDocument.from_dict(self.document, self.original_text(), self.source_path)

    def all_segments(self):
        """Yield (chapter, segment) pairs in document order."""
        for chapter in self.chapters:
            for segment in chapter.segments:
                yield chapter, segment

    @property
    def enabled(self):
        return self.options.checks

    def pending_checks(self, segment):
        """Return the enabled checks that still lack a successful result for ``segment``."""
        return [
            check for check in self.options.enabled_checks()
            if segment.results.get(check) is None or segment.results[check].status == "error"
        ]

    def pending_tasks(self):
        """Return the evaluation tasks still to run, in (chapter, segment, check) order.

        In separate mode every missing (segment, check) pair is one task; in
        combined mode a segment with any missing check is one task whose check
        is ``None`` (one request answers all its checks).
        """
        tasks = []
        for chapter in self.chapters:
            for segment in chapter.segments:
                if segment.is_blank:
                    continue
                pending = self.pending_checks(segment)
                if not pending:
                    continue
                if self.options.combined:
                    tasks.append((chapter.index, segment.index, None))
                else:
                    tasks.extend((chapter.index, segment.index, check) for check in pending)
        return tasks

    def find(self, chapter_index, segment_index):
        for chapter in self.chapters:
            if chapter.index == chapter_index:
                for segment in chapter.segments:
                    if segment.index == segment_index:
                        return chapter, segment
        return None, None

    def invalidate(self, chapter_index, segment_index=None, checks=None):
        """Drop the results of one segment (or a whole chapter) so they are evaluated again.

        ``checks`` limits the drop to those checks (default: every stored
        result). Decisions on the dropped changes are gone with the results;
        their decision-log entries stay but are marked ``"stale": True`` so
        the Decisions view can grey them out and undo() skips them.
        Returns the removed (chapter_index, segment_index, check) tuples, which
        are exactly the tasks pending_tasks() will report for enabled checks.
        """
        removed = []
        for chapter in self.chapters:
            if chapter.index != chapter_index:
                continue
            for segment in chapter.segments:
                if segment_index is not None and segment.index != segment_index:
                    continue
                for check in list(segment.results):
                    if checks is not None and check not in checks:
                        continue
                    if checks is None and check == CHECK_AUTHOR:
                        continue  # the author's corrections are not model results
                    result = segment.results.pop(check)
                    removed.append((chapter.index, segment.index, check))
                    stale_ids = {change.change_id for change in result.changes}
                    for entry in self.decision_log:
                        if (entry["chapter"], entry["segment"]) == (chapter.index, segment.index) \
                                and entry["change_id"] in stale_ids:
                            entry["stale"] = True
        return removed

    def changes_by_kind(self, kind, decision=None):
        """Return (chapter, segment, change) for every change of ``kind`` in document order.

        ``decision`` restricts the list to changes with that decision.
        """
        found = []
        for chapter, segment in self.all_segments():
            for change in segment.changes(self.enabled):
                if change.kind == kind and (decision is None or change.decision == decision):
                    found.append((chapter, segment, change))
        return found

    def progress(self):
        """Return counters for progress displays and reports."""
        enabled = self.enabled
        stats = {
            "segments": 0, "queued": 0, "error": 0, "clean": 0, "ready": 0, "reviewed": 0,
            "tasks_total": 0, "tasks_done": 0, "changes": 0, "accepted": 0, "rejected": 0, "pending": 0,
            "suppressed": 0, "flagged": 0, "flagged_pending": 0, "words": 0,
            "methods": {mode: 0 for mode in EVALUATION_MODES},
            "per_check": {
                check: {
                    "changes": 0, "accepted": 0, "rejected": 0, "suppressed": 0, "flagged": 0,
                    "by_kind": {kind: 0 for kind in CHANGE_KINDS},
                }
                for check in ALL_CHECKS
            },
            "edited": 0,
            "per_kind": {kind: {"changes": 0, "accepted": 0, "rejected": 0} for kind in CHANGE_KINDS},
        }
        checks = self.options.enabled_checks()
        for _, segment in self.all_segments():
            stats["segments"] += 1
            stats["words"] += word_count(segment.text)
            stats[segment.status(enabled)] += 1
            if segment.is_blank:
                continue
            stats["tasks_total"] += len(checks)
            stats["tasks_done"] += sum(
                1 for check in checks
                if check in segment.results and segment.results[check].status == "done"
            )
            for check in checks:
                result = segment.results.get(check)
                if result is None or result.status != "done":
                    continue
                stats["methods"][result.method] = stats["methods"].get(result.method, 0) + 1
                if result.suppressed:
                    stats["suppressed"] += len(result.suppressed)
                    stats["per_check"][check]["suppressed"] += len(result.suppressed)
            for change in segment.changes(enabled):
                per_check = stats["per_check"][change.check]
                per_kind = stats["per_kind"][change.kind]
                stats["changes"] += 1
                per_check["changes"] += 1
                per_check["by_kind"][change.kind] += 1
                per_kind["changes"] += 1
                if change.edited:
                    stats["edited"] += 1
                if change.flagged:
                    stats["flagged"] += 1
                    per_check["flagged"] += 1
                    if change.decision == PENDING:
                        stats["flagged_pending"] += 1
                if change.decision == ACCEPTED:
                    stats["accepted"] += 1
                    per_check["accepted"] += 1
                    per_kind["accepted"] += 1
                elif change.decision == REJECTED:
                    stats["rejected"] += 1
                    per_check["rejected"] += 1
                    per_kind["rejected"] += 1
                else:
                    stats["pending"] += 1
        return stats

    # ------------------------------------------------------------- source
    def source_changed(self):
        """Whether the manuscript file differs from the text this project was split from.

        False when the file is missing or the project has no fingerprint; the
        reason is left in ``source_check_reason`` ("missing", "unknown",
        "unchanged" or "changed").
        """
        path = Path(self.source_path) if self.source_path else None
        if path is None or not path.is_file():
            self.source_check_reason = "missing"
            return False
        if not self.source_sha256:
            self.source_check_reason = "unknown"
            return False
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self.source_check_reason = "missing"
            return False
        if self.source_mtime and abs(mtime - self.source_mtime) < 1e-6:
            self.source_check_reason = "unchanged"
            return False
        try:
            digest = text_fingerprint(load_document(path).text)
        except (OSError, UnicodeError, DocumentError):
            self.source_check_reason = "missing"
            return False
        changed = digest != self.source_sha256
        self.source_check_reason = "changed" if changed else "unchanged"
        if not changed:
            self.source_mtime = mtime  # touched but identical: remember so the next check is cheap
        return changed

    def remember_source(self, text):
        """Store the fingerprint (and mtime) of ``text`` as the manuscript this project reflects."""
        self.source_sha256 = text_fingerprint(text)
        try:
            self.source_mtime = Path(self.source_path).stat().st_mtime
        except (OSError, ValueError):
            self.source_mtime = 0.0

    # ---------------------------------------------------------- decisions
    def decide(self, chapter_index, segment_index, change, decision, group=None):
        """Set ``change.decision`` and record it so that it can be undone.

        Bulk actions pass the same ``group`` (see ``new_decision_group``) for
        every change they touch so that one undo reverts them together.
        Returns the log entry, or None when the change already had that decision.
        """
        if change.decision == decision:
            return None
        entry = self._log_entry(chapter_index, segment_index, change, change.decision, decision, group)
        change.decision = decision
        self.decision_log.append(entry)
        self._trim_log()
        return entry

    def _log_entry(self, chapter_index, segment_index, change, before, after, group, **extra):
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "chapter": chapter_index,
            "segment": segment_index,
            "change_id": change.change_id,
            "before": before,
            "after": after,
            "group": group,
        }
        entry.update(extra)
        return entry

    def _trim_log(self):
        if len(self.decision_log) > DECISION_LOG_LIMIT:
            del self.decision_log[:-DECISION_LOG_LIMIT]

    def edit_change(self, chapter_index, segment_index, change, new_text, group=None):
        """Replace a suggestion's text with the author's wording and accept it.

        The log entry carries ``before_text``/``after_text`` so undo() restores
        both the text and the decision. Returns the entry, or None when nothing changed.
        """
        new_text = str(new_text)
        if new_text == change.proposed_text and change.decision == ACCEPTED:
            return None
        entry = self._log_entry(chapter_index, segment_index, change, change.decision, ACCEPTED, group,
                                before_text=change.proposed_text, after_text=new_text)
        change.proposed_text = new_text
        change.edited = new_text != change.model_proposed_text
        change.kind = classify_change(change.original_text, new_text)
        change.decision = ACCEPTED
        self.decision_log.append(entry)
        self._trim_log()
        return entry

    def add_author_change(self, chapter_index, segment_index, start, end, new_text, group=None):
        """Record the author's own correction of ``segment.text[start:end]`` as an accepted change.

        Returns the new Change, or None when the span is invalid or the text is unchanged.
        """
        _, segment = self.find(chapter_index, segment_index)
        if segment is None:
            return None
        start, end = int(start), int(end)
        if not 0 <= start <= end <= len(segment.text):
            return None
        original = segment.text[start:end]
        new_text = str(new_text)
        if new_text == original:
            return None
        result = segment.author_result(create=True)
        numbers = [int(c.change_id.rsplit("-", 1)[1]) for c in result.changes if c.change_id.rsplit("-", 1)[1].isdigit()]
        change = Change(
            "{0}-{1}".format(CHECK_AUTHOR, max(numbers, default=0) + 1), CHECK_AUTHOR, start, end,
            original, new_text, AUTHOR_EXPLANATION, ACCEPTED,
        )
        result.changes.append(change)
        result.changes.sort(key=lambda c: (c.start, c.end))
        self.decision_log.append(self._log_entry(chapter_index, segment_index, change, "", ACCEPTED, group, created=True))
        self._trim_log()
        return change

    def decide_kind(self, kind, decision, group=None):
        """Apply ``decision`` to every pending change of ``kind`` as one undoable group.

        Returns the log entries that were written.
        """
        group = group or new_decision_group()
        entries = []
        for chapter, segment, change in self.changes_by_kind(kind, decision=PENDING):
            entry = self.decide(chapter.index, segment.index, change, decision, group=group)
            if entry is not None:
                entries.append(entry)
        return entries

    def undo(self):
        """Revert the most recent decision, or the whole group it belongs to.

        Returns the reverted entries in the order they were made, or None when
        there is nothing to undo.
        """
        live = [index for index, entry in enumerate(self.decision_log) if not entry.get("stale")]
        if not live:
            return None
        group = self.decision_log[live[-1]]["group"]
        indices = []
        for index in reversed(live):
            if indices and (group is None or self.decision_log[index]["group"] != group):
                break
            indices.append(index)
            if group is None:
                break
        undone = []
        for index in indices:  # newest first, so the remaining indices stay valid
            entry = self.decision_log.pop(index)
            _, segment = self.find(entry["chapter"], entry["segment"])
            change = segment.find_change(entry["change_id"]) if segment is not None else None
            if change is not None:
                if entry.get("created"):
                    result = segment.author_result()
                    if result is not None:
                        result.changes = [c for c in result.changes if c is not change]
                        if not result.changes:
                            del segment.results[CHECK_AUTHOR]
                else:
                    if "before_text" in entry:
                        change.proposed_text = entry["before_text"]
                        change.edited = change.proposed_text != change.model_proposed_text
                        change.kind = classify_change(change.original_text, change.proposed_text)
                    change.decision = entry["before"]
            undone.append(entry)
        undone.reverse()
        return undone

    # -------------------------------------------------------- persistence
    @property
    def project_file(self):
        return self.root / PROJECT_FILE if self.root else None

    def to_dict(self):
        return {
            "format": PROJECT_FORMAT,
            "name": self.name,
            "source_path": self.source_path,
            "created_at": self.created_at,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "model": self.model,
            "backend": self.backend,
            "method": self.method,
            "options": self.options.to_dict(),
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "decision_log": [dict(entry) for entry in self.decision_log],
            "source_sha256": self.source_sha256,
            "source_mtime": self.source_mtime,
            "consistency": [finding.to_dict() for finding in self.consistency],
            "document": LoadedDocument.from_dict(self.document).to_dict(),
        }

    @classmethod
    def from_dict(cls, data, root=None):
        from .consistency import Finding  # consistency imports this module

        if data.get("format", PROJECT_FORMAT) > PROJECT_FORMAT:
            raise ValueError("This project was saved by a newer TextEnhanceAI version.")
        findings = []
        for item in data.get("consistency") or []:
            try:
                findings.append(Finding.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue  # a damaged finding is dropped, the project still loads
        return cls(
            data["name"], data.get("source_path", ""), data.get("created_at", ""),
            ProjectOptions.from_dict(data.get("options")),
            [Chapter.from_dict(item) for item in data.get("chapters", [])],
            data.get("model", ""), data.get("backend", ""), data.get("method", ""),
            decision_log=[dict(entry) for entry in data.get("decision_log") or []],
            source_sha256=str(data.get("source_sha256") or ""),
            source_mtime=float(data.get("source_mtime") or 0.0),
            consistency=findings,
            document=LoadedDocument.from_dict(data.get("document")).to_dict(),  # format 1 files: plain text
            root=root,
        )

    def save(self):
        """Write project.json atomically (safe against crashes mid-write)."""
        if self.root is None:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.project_file
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(temporary), str(path))
        return path

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.is_dir():
            path = path / PROJECT_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data, root=path.parent)

    def write_chapter_files(self):
        """Write each chapter's original text as its own file (chapters/NN - Title.txt)."""
        directory = self.root / "chapters"
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for chapter in self.chapters:
            path = directory / chapter.file_name
            path.write_text(chapter.heading + chapter.body + chapter.trailing, encoding="utf-8")
            paths.append(path)
        return paths

    # -------------------------------------------------------------- export
    def render_chapter(self, chapter):
        return chapter.heading + "".join(
            render_segment(segment, self.enabled) + segment.trailing for segment in chapter.segments
        ) + chapter.trailing

    def render_document(self):
        return "".join(self.render_chapter(chapter) for chapter in self.chapters)

    def export(self, on_warning=None):
        """Write reviewed chapters, the combined document and a Markdown report.

        A manuscript loaded from ``.md``/``.docx``/``.odt`` is also written
        back in that format as ``<name>-reviewed.<ext>`` (``"formatted"`` in the
        returned dict; see ``documents.save_document`` for what survives).
        Limitations hit on the way go to ``on_warning`` and ``"warnings"``.
        """
        reviewed = self.root / "reviewed"
        reviewed.mkdir(parents=True, exist_ok=True)
        written = []
        for chapter in self.chapters:
            path = reviewed / chapter.file_name
            path.write_text(self.render_chapter(chapter), encoding="utf-8")
            written.append(path)
        document_text = self.render_document()
        combined = self.root / "{0}-reviewed.txt".format(self.name)
        combined.write_text(document_text, encoding="utf-8")
        report = self.root / "report.md"
        report.write_text(self.build_report(), encoding="utf-8")
        result = {"chapters": written, "document": combined, "report": report, "warnings": []}

        def warn(message):
            result["warnings"].append(message)
            if on_warning:
                on_warning(message)

        loaded = self.loaded_document()
        if loaded.kind != KIND_TXT:
            formatted = self.root / "{0}-reviewed{1}".format(self.name, loaded.suffix)
            try:
                result["formatted"] = save_document(loaded, document_text, formatted, warn)
            except DocumentError as exc:
                warn("The {0} file could not be written: {1}".format(loaded.suffix, exc))
        return result

    def build_report(self):
        from .statistics import project_statistics, statistics_markdown  # statistics imports this module

        stats = self.progress()
        lines = [
            "# Review report: {0}\n".format(self.name),
            "- Source: `{0}`".format(self.source_path),
            "- Model: `{0}` ({1})".format(self.model or "?", self.backend or "?"),
            "- Checks: {0}".format(", ".join(CHECK_LABELS[c] for c in self.options.enabled_checks()) or "none"),
            "- Evaluation: {0} ({1} results combined, {2} separate)".format(
                self.options.evaluation_mode, stats["methods"][EVALUATION_COMBINED], stats["methods"][EVALUATION_SEPARATE]
            ),
            "- Chapters: {0} ({1}), segments: {2}, words: {3}".format(
                len(self.chapters), self.method or "?", stats["segments"], stats["words"]
            ),
            "- Changes: {0} proposed, {1} accepted, {2} rejected, {3} pending".format(
                stats["changes"], stats["accepted"], stats["rejected"], stats["pending"]
            ),
        ]
        for check in self.options.enabled_checks():
            per = stats["per_check"][check]
            lines.append("  - {0}: {1} proposed, {2} accepted, {3} rejected".format(
                CHECK_LABELS[check], per["changes"], per["accepted"], per["rejected"]
            ))
            if per["changes"]:
                lines.append("    - by kind: " + ", ".join(
                    "{0} {1}".format(CHANGE_KIND_LABELS[kind].lower(), per["by_kind"][kind])
                    for kind in CHANGE_KINDS if per["by_kind"][kind]
                ))
            if per["suppressed"]:
                lines.append("    - Glossary suppressed {0} proposed change(s)".format(per["suppressed"]))
            if per["flagged"]:
                lines.append("    - {0}: {1} change(s) flagged".format(FLAG_REPORT_MARK, per["flagged"]))
        if stats["per_check"][CHECK_AUTHOR]["changes"]:
            lines.append("  - Author's corrections: {0}".format(stats["per_check"][CHECK_AUTHOR]["changes"]))
        if stats["edited"]:
            lines.append("  - Suggestions reworded by the author: {0}".format(stats["edited"]))
        lines.append("")
        for chapter in self.chapters:
            lines.append("## {0}. {1}\n".format(chapter.index, chapter.title))
            for segment in chapter.segments:
                changes = segment.changes(self.enabled)
                status = segment.status(self.enabled)
                if not changes:
                    lines.append("- Segment {0}: {1}".format(segment.index, status))
                    continue
                states = change_states(segment, self.enabled)
                lines.append("- Segment {0}: {1} change(s), {2}".format(segment.index, len(changes), status))
                for change in changes:
                    line = "  - [{0}] `{1}` → `{2}` — {3} — {4}".format(
                        CHECK_LABELS[change.check],
                        _inline(change.original_text), _inline(change.proposed_text),
                        states[change.change_id], change.explanation or FALLBACK_EXPLANATIONS[change.check],
                    )
                    if change.flagged:
                        line += " — {0} ({1})".format(FLAG_REPORT_MARK, ", ".join(change.flags))
                    if change.edited:
                        line += " — {0} (model proposed `{1}`)".format(EDITED_REPORT_MARK, _inline(change.model_proposed_text))
                    lines.append(line)
            lines.append("")
        lines.append(statistics_markdown(project_statistics(self)))
        lines.append("")
        return "\n".join(lines)


def _inline(text):
    text = text.replace("`", "'").replace("\n", "↵")
    return text if text else "∅"


# ------------------------------------------------------------ diff/merge
def extract_changes(original, proposed, check):
    """Diff ``proposed`` against ``original`` into offset-anchored changes."""
    session = build_edit_session(original, proposed)
    changes = []
    number = 0
    for part in session.parts:
        if not isinstance(part, ReviewItem):
            continue
        position = part.original_start
        for hunk in part.hunks:
            length = len(hunk.original_text)
            if hunk.is_change:
                number += 1
                changes.append(
                    Change(
                        "{0}-{1}".format(check, number), check, position, position + length,
                        hunk.original_text, hunk.proposed_text,
                    )
                )
            position += length
    return changes


def overlaps(a, b):
    """Return whether two changes touch the same characters (or the same insertion point)."""
    if a.start == a.end and b.start == b.end:
        return a.start == b.start
    return a.start < b.end and b.start < a.end


def applied_changes(segment, enabled):
    """Return the accepted changes that will actually be applied, in text order."""
    accepted = [change for change in segment.changes(enabled) if change.decision == ACCEPTED]
    accepted.sort(key=lambda change: (change.priority, change.start))
    kept = []
    for change in accepted:
        if not any(overlaps(change, other) for other in kept):
            kept.append(change)
    kept.sort(key=lambda change: (change.start, change.end))
    return kept


def change_states(segment, enabled):
    """Map change ids to applied / superseded / rejected / pending."""
    applied = {change.change_id for change in applied_changes(segment, enabled)}
    states = {}
    for change in segment.changes(enabled):
        if change.decision == REJECTED:
            states[change.change_id] = STATE_REJECTED
        elif change.decision == ACCEPTED:
            states[change.change_id] = STATE_APPLIED if change.change_id in applied else STATE_SUPERSEDED
        else:
            states[change.change_id] = STATE_PENDING
    return states


def render_segment(segment, enabled):
    """Apply the effective changes to the segment text."""
    output = []
    position = 0
    for change in applied_changes(segment, enabled):
        output.append(segment.text[position:change.start])
        output.append(change.proposed_text)
        position = change.end
    output.append(segment.text[position:])
    return "".join(output)


def render_segment_annotated(segment, enabled, offset=0):
    """Render a segment like render_segment() and locate every change in the output.

    Returns ``(text, spans)``; each span is a dict with ``start``/``end`` in
    output coordinates (shifted by ``offset``), the change's ``state`` (see
    change_states), ``change_id``, ``segment`` index, ``check`` and ``flags``.
    Applied changes cover their proposed text, all others (pending, rejected,
    superseded) the original text they would touch — inside an applied
    replacement that collapses onto the replacement.
    """
    text = segment.text
    applied = applied_changes(segment, enabled)
    states = change_states(segment, enabled)
    # start_map[i]/end_map[i]: where original character i begins/ends in the output
    start_map = [0] * (len(text) + 1)
    end_map = [0] * (len(text) + 1)
    output = []
    out = offset
    position = 0
    replaced = {}
    for change in applied:
        for index in range(position, change.start):
            start_map[index] = out
            out += 1
            end_map[index] = out
        replacement_start = out
        out += len(change.proposed_text)
        for index in range(change.start, change.end):
            start_map[index] = replacement_start
            end_map[index] = out
        replaced[change.change_id] = (replacement_start, out)
        output.append(text[position:change.start])
        output.append(change.proposed_text)
        position = change.end
    for index in range(position, len(text)):
        start_map[index] = out
        out += 1
        end_map[index] = out
    start_map[len(text)] = end_map[len(text)] = out
    output.append(text[position:])

    spans = []
    for change in segment.changes(enabled):
        if change.change_id in replaced:
            start, end = replaced[change.change_id]
        elif change.end > change.start:
            start, end = start_map[change.start], end_map[change.end - 1]
        else:
            start = end = start_map[change.start]
        spans.append({
            "start": start, "end": end, "state": states[change.change_id], "change_id": change.change_id,
            "segment": segment.index, "check": change.check, "flags": list(change.flags),
        })
    return "".join(output), spans


def render_chapter_annotated(project, chapter):
    """Render a chapter with its heading and locate every change of every segment.

    The text equals ``project.render_chapter(chapter)``; the spans (see
    render_segment_annotated) use offsets into that text.
    """
    enabled = project.enabled
    parts = [chapter.heading]
    spans = []
    offset = len(chapter.heading)
    for segment in chapter.segments:
        rendered, segment_spans = render_segment_annotated(segment, enabled, offset)
        parts.append(rendered)
        parts.append(segment.trailing)
        spans.extend(segment_spans)
        offset += len(rendered) + len(segment.trailing)
    parts.append(chapter.trailing)
    return "".join(parts), spans


def pending_changes(project):
    """Yield (chapter, segment, change) for every pending change in document order."""
    for chapter, segment in project.all_segments():
        for change in segment.changes(project.enabled):
            if change.decision == PENDING:
                yield chapter, segment, change


# ------------------------------------------------------------- creation
def _as_document(source_path, text):
    """Accept either a ``LoadedDocument`` or plain text for the manuscript."""
    if isinstance(text, LoadedDocument):
        return text
    return text_document(text, str(source_path))


def create_project(source_path, text, options, model="", backend="", root=None):
    """Split a manuscript and return an unevaluated project.

    ``text`` is the manuscript text or the ``documents.LoadedDocument`` it was
    read from; the latter lets ``export`` write the reviewed text back in the
    source format.
    """
    source_path = Path(source_path)
    document = _as_document(source_path, text)
    text = document.text
    result = split_document(
        text, mode="auto" if options.chapter_mode == "model" else options.chapter_mode,
        target_chars=options.target_chars, max_chars=options.max_chars,
        max_chapter_chars=options.max_chapter_chars,
    )
    chapters = [
        Chapter(
            chapter.index, chapter.title, chapter.heading, chapter.trailing,
            [Segment(segment.index, segment.text, segment.trailing) for segment in chapter.segments],
        )
        for chapter in result.chapters
    ]
    project = Project(
        source_path.stem, str(source_path), datetime.now().isoformat(timespec="seconds"),
        options, chapters, model, backend, result.method,
        document=document.to_dict(),
        root=Path(root) if root else source_path.with_name(source_path.stem + PROJECT_DIR_SUFFIX),
    )
    project.remember_source(text)
    return project


def text_fingerprint(text):
    """SHA-256 of the manuscript text (after read_text_file's newline normalisation)."""
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def _chapters_for_resync(project, text):
    """Split ``text`` the way the project was split; model-chosen boundaries are kept by matching.

    For ``chapter_mode == "model"`` each old chapter's first paragraph is looked
    up in the new text; when every chapter is found again the old boundaries
    (and titles) are reused, otherwise the automatic split is the fallback.
    """
    options = project.options
    if options.chapter_mode == "model" and len(project.chapters) > 1:
        new_paragraphs = [paragraph.strip() for paragraph, _ in paragraphs(text)]
        starts = []
        for chapter in project.chapters[1:]:
            first = next((segment.text for segment in chapter.segments if segment.text.strip()), "")
            lead = first.strip().split("\n\n")[0].strip()
            index = next((number for number, paragraph in enumerate(new_paragraphs) if lead and paragraph == lead), None)
            if index is None or (starts and index <= starts[-1]):
                starts = None
                break
            starts.append(index)
        if starts:
            result = chapters_from_outline(text, starts, [chapter.title for chapter in project.chapters])
            for chapter in result.chapters:
                chapter.segments = split_segments(chapter.body, options.target_chars, options.max_chars)
            return result
    return split_document(
        text, mode="auto" if options.chapter_mode == "model" else options.chapter_mode,
        target_chars=options.target_chars, max_chars=options.max_chars, max_chapter_chars=options.max_chapter_chars,
    )


def resync_project(project, new_text):
    """Re-split the changed manuscript and carry results and decisions over to identical segments.

    Segments are matched by exact text (duplicates pair up in order). Unmatched
    old segments that still carry pending or accepted changes are written to
    ``<root>/resync-<timestamp>.md`` before they are dropped. Decision-log
    entries are re-pointed at the kept segments' new positions, those of
    dropped segments are marked stale, and a stale "resync" marker entry
    records the summary. Returns ``{"kept", "new", "removed", "chapters", "report"}``.
    ``new_text`` may be a ``LoadedDocument``; its paragraph locators then
    replace the project's.
    """
    document = _as_document(project.source_path, new_text)
    new_text = document.text
    result = _chapters_for_resync(project, new_text)
    pool = {}
    for chapter in project.chapters:
        for segment in chapter.segments:
            pool.setdefault(segment.text, []).append((chapter.index, segment.index, segment))
    kept = new = 0
    remap = {}
    chapters = []
    for chapter in result.chapters:
        segments = []
        for piece in chapter.segments:
            candidates = pool.get(piece.text)
            if candidates:
                old_chapter, old_index, old_segment = candidates.pop(0)
                segment = Segment(piece.index, piece.text, piece.trailing, old_segment.results)
                remap[(old_chapter, old_index)] = (chapter.index, piece.index)
                kept += 1
            else:
                segment = Segment(piece.index, piece.text, piece.trailing)
                if piece.text.strip():
                    new += 1
            segments.append(segment)
        chapters.append(Chapter(chapter.index, chapter.title, chapter.heading, chapter.trailing, segments))

    dropped = [(chapter_index, segment_index, segment) for entries in pool.values()
               for chapter_index, segment_index, segment in entries if segment.text.strip()]
    report = None
    if project.root is not None:
        report = _write_resync_report(project, dropped)
    removed = len(dropped)
    dropped_keys = {(chapter_index, segment_index) for chapter_index, segment_index, _ in dropped}
    for entry in project.decision_log:
        key = (entry.get("chapter"), entry.get("segment"))
        if key in remap:
            entry["chapter"], entry["segment"] = remap[key]
        elif key in dropped_keys or entry.get("chapter") is not None:
            entry["stale"] = True
    project.chapters = chapters
    project.method = result.method
    project.document = document.to_dict()
    project.remember_source(new_text)
    summary = {"kept": kept, "new": new, "removed": removed, "chapters": len(chapters), "report": report}
    project.decision_log.append({
        "ts": datetime.now().isoformat(timespec="seconds"), "chapter": None, "segment": None, "change_id": None,
        "before": None, "after": None, "group": None, "stale": True,
        "resync": {"kept": kept, "new": new, "removed": removed},
    })
    project._trim_log()
    return summary


def _write_resync_report(project, dropped):
    """List the pending and accepted changes of segments that a re-sync dropped; None when there are none."""
    enabled = project.enabled
    lines = []
    for chapter_index, segment_index, segment in dropped:
        changes = [change for change in segment.changes(enabled) if change.decision != REJECTED]
        if not changes:
            continue
        chapter, _ = project.find(chapter_index, segment_index)
        title = chapter.title if chapter is not None else str(chapter_index)
        lines.append("## {0} · segment {1}\n".format(title, segment_index))
        lines.append("> " + segment.text.strip().replace("\n", "\n> ") + "\n")
        states = change_states(segment, enabled)
        for change in changes:
            lines.append("- [{0}] `{1}` → `{2}` — {3}".format(
                CHECK_LABELS[change.check], _inline(change.original_text), _inline(change.proposed_text),
                states[change.change_id]))
        lines.append("")
    if not lines:
        return None
    project.root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = project.root / "resync-{0}.md".format(stamp)
    number = 1
    while path.exists():
        number += 1
        path = project.root / "resync-{0}-{1}.md".format(stamp, number)
    header = [
        "# Changes dropped by the re-sync of {0}\n".format(project.name),
        "The manuscript changed and these segments no longer exist in it. Their pending and accepted "
        "changes are listed here so that nothing is lost silently.\n",
    ]
    path.write_text("\n".join(header + lines), encoding="utf-8")
    return path


def apply_model_outline(project, text, starts, titles=None):
    """Replace the chapters using boundaries proposed by the model."""
    result = chapters_from_outline(text, starts, titles)
    project.chapters = []
    for chapter in result.chapters:
        segments = split_segments(chapter.body, project.options.target_chars, project.options.max_chars)
        project.chapters.append(
            Chapter(
                chapter.index, chapter.title, chapter.heading, chapter.trailing,
                [Segment(segment.index, segment.text, segment.trailing) for segment in segments],
            )
        )
    project.method = result.method
    return project


def build_outline_messages(text):
    """Ask the model where chapters start (used when no headings were found)."""
    outline = paragraph_outline(text)
    listing = "\n".join("{0}: {1}".format(index, preview) for index, preview in outline)
    return [
        {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Below is a numbered outline of the paragraphs of a manuscript (first "
                "characters of each paragraph). Identify where new chapters or major "
                "sections begin. Return JSON: {\"chapters\": [{\"start\": <paragraph "
                "number>, \"title\": \"short title\"}, ...]}. The first chapter starts at "
                "paragraph 0. Prefer clear structural breaks; return at most 60 chapters.\n\n"
                + listing
            ),
        },
    ]


def parse_outline(text):
    """Return ([starts], [titles]) from the model's JSON answer."""
    data = _parse_json_object(text)
    starts, titles = [], []
    for item in (data or {}).get("chapters", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("start"), int):
            starts.append(item["start"])
            titles.append(str(item.get("title", "")).strip())
    return starts, titles


# ----------------------------------------------------------- style guide
def normalise_style_guide(style_guide):
    """Return the author's rules one per line, trimmed to ``STYLE_GUIDE_MAX_CHARS``.

    Authors type rules separated by line breaks or by " · " bullets (as in the
    placeholder); both become one rule per line. Blank lines are dropped.
    """
    rules = []
    for line in (style_guide or "").replace("\u00b7", "\n").splitlines():
        rule = " ".join(line.split())
        if rule:
            rules.append(rule)
    return "\n".join(rules)[:STYLE_GUIDE_MAX_CHARS].rstrip()


def format_style_guide(style_guide):
    """Return the block appended to a prompt for a non-empty style guide, else ""."""
    rules = normalise_style_guide(style_guide)
    if not rules:
        return ""
    return "\n\n" + STYLE_GUIDE_HEADER + "\n" + rules


# -------------------------------------------------------------- glossary
def parse_glossary(text):
    """Return the protected terms from a text box (one per line) or a stored list.

    Terms are trimmed, empty lines and duplicates dropped (first occurrence
    wins, order kept). A term may end in ``*`` to protect every word that
    starts with it (``hyper*`` covers ``hyperdrive``).
    """
    if text is None:
        return []
    if isinstance(text, str):
        items = text.replace("\u00b7", "\n").splitlines()
    else:
        items = list(text)
    terms = []
    seen = set()
    for item in items:
        term = " ".join(str(item).split())
        if not term or term == "*" or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def glossary_pattern(terms):
    """Return the compiled regex matching any protected term, or ``None`` when there is none."""
    parts = []
    for term in parse_glossary(terms):
        if term.endswith("*"):
            stem = term[:-1]
            parts.append("(?<!\\w)" + re.escape(stem))
        else:
            parts.append("(?<!\\w)" + re.escape(term) + "(?!\\w)")
    if not parts:
        return None
    # Longest first so "Meret Aubinger" wins over "Meret"; lookarounds instead of
    # \b so terms may start or end with punctuation. Case-sensitive on purpose:
    # a protected name keeps its capitalization.
    parts.sort(key=len, reverse=True)
    return re.compile("|".join(parts), re.UNICODE)


def glossary_matcher(terms):
    """Return ``callable(text) -> bool`` telling whether ``text`` contains a protected term."""
    pattern = glossary_pattern(terms)
    if pattern is None:
        return lambda text: False
    return lambda text: bool(text) and pattern.search(text) is not None


def glossary_prompt_terms(terms):
    """Return the terms listed in the prompt: at most ``GLOSSARY_MAX_TERMS`` / ``GLOSSARY_MAX_CHARS``."""
    listed = []
    length = 0
    for term in parse_glossary(terms)[:GLOSSARY_MAX_TERMS]:
        length += len(term) + (2 if listed else 0)
        if length > GLOSSARY_MAX_CHARS:
            break
        listed.append(term)
    return listed


def format_glossary(terms):
    """Return the prompt block naming the protected terms, or "" when there are none."""
    listed = glossary_prompt_terms(terms)
    if not listed:
        return ""
    return "\n\n" + GLOSSARY_HEADER + ", ".join(listed)


def suppress_glossary_changes(text, changes, matcher):
    """Split ``changes`` into (kept, suppressed) using the glossary ``matcher``.

    A change is suppressed when its original text contains a protected term
    or, for insertions, when the ``GLOSSARY_CONTEXT`` characters on either
    side of the insertion point do.
    """
    kept, suppressed = [], []
    for change in changes:
        if change.original_text.strip():
            hit = matcher(change.original_text)
        else:
            window = text[max(0, change.start - GLOSSARY_CONTEXT):change.end + GLOSSARY_CONTEXT]
            hit = matcher(window)
        if hit:
            change.explanation = GLOSSARY_EXPLANATION
            suppressed.append(change)
        else:
            kept.append(change)
    return kept, suppressed


# ---------------------------------------------------- hallucination guard
def content_words(text):
    """Return the lower-cased words of ``text`` that can carry content (long enough, not stopwords)."""
    return {
        word for word in _WORD_RE.findall((text or "").lower())
        if len(word) >= NOVEL_MIN_LENGTH and word not in STOPWORDS
    }


def novel_words(segment_text, proposed_text):
    """Return the content words of ``proposed_text`` that occur nowhere in ``segment_text`` (case-insensitive)."""
    known = set(_WORD_RE.findall((segment_text or "").lower()))
    return sorted(word for word in content_words(proposed_text) if word not in known)


def flag_suspicious(segment_text, change):
    """Return a reason id when ``change`` looks like added content rather than a correction, else ``None``.

    Rules, first match wins: ``growth`` (the replacement is far longer than the
    original), ``novel_words`` (at least ``NOVEL_MIN_WORDS`` content words that
    appear nowhere in the segment) and ``rewrite_in_strict_check`` (a spelling
    or grammar check produced a ``rewrite``-kind change).
    """
    original = change.original_text
    proposed = change.proposed_text
    if len(proposed) > GROWTH_FACTOR * len(original) + GROWTH_SLACK:
        return FLAG_GROWTH
    if len(novel_words(segment_text, proposed)) >= NOVEL_MIN_WORDS:
        return FLAG_NOVEL_WORDS
    if change.kind == "rewrite" and change.check in STRICT_CHECKS:
        return FLAG_REWRITE_IN_STRICT_CHECK
    return None


def flag_changes(segment_text, changes):
    """Append the ``flag_suspicious`` reason to every change it fires on; returns the flagged ones."""
    flagged = []
    for change in changes:
        reason = flag_suspicious(segment_text, change)
        if reason and reason not in change.flags:
            change.flags.append(reason)
        if reason:
            flagged.append(change)
    return flagged


def build_check_instruction(check, options=None, style_guide=None, glossary=None):
    """Return the instruction sent for ``check`` with the author's rules and protected terms appended.

    ``style_guide`` / ``glossary`` win over the values in ``options``; all may
    be omitted, in which case the plain ``CHECK_INSTRUCTIONS`` entry is returned.
    """
    if style_guide is None:
        style_guide = getattr(options, "style_guide", "") if options is not None else ""
    if glossary is None:
        glossary = getattr(options, "glossary", None) if options is not None else None
    return CHECK_INSTRUCTIONS[check] + format_style_guide(style_guide) + format_glossary(glossary or [])


# ---------------------------------------------------------- explanations
def _context(text, start, end, radius=40):
    before = text[max(0, start - radius):start].replace("\n", " ")
    after = text[end:end + radius].replace("\n", " ")
    return before, after


def detect_language(text):
    """Return ``"de"`` when a text uses more German than English function words, else ``"en"``."""
    words = _WORD_RE.findall((text or "").lower())
    german = sum(1 for word in words if word in _GERMAN_MARKERS)
    english = sum(1 for word in words if word in _ENGLISH_MARKERS)
    return "de" if german > english else "en"


def explanation_language(language, segment_text):
    """Return the code ("en"/"de") canned explanations may use, or ``None`` for other languages."""
    if language == SAME_LANGUAGE:
        return detect_language(segment_text)
    return LANGUAGE_CODES.get((language or "").strip().lower())


def canned_explanation(change, language):
    """Return a fixed explanation for a trivial change in ``language`` ("en"/"de"), else ``None``."""
    texts = CANNED_EXPLANATIONS.get(change.kind)
    if not texts or language not in texts:
        return None
    if change.kind == "spelling" and change.distance > CANNED_SPELLING_MAX_DISTANCE:
        return None
    return texts[language]


def apply_canned_explanations(segment_text, changes, language=SAME_LANGUAGE):
    """Fill canned explanations in place; returns the changes that still need the model."""
    code = explanation_language(language, segment_text)
    remaining = []
    for change in changes:
        canned = canned_explanation(change, code) if code else None
        if canned:
            change.explanation = canned
        else:
            remaining.append(change)
    return remaining


def _change_lines(segment_text, changes, first_number):
    lines = []
    for number, change in enumerate(changes, first_number):
        before, after = _context(segment_text, change.start, change.end)
        lines.append(
            "{0}. \"{1}\" → \"{2}\"   (context: …{3}[{1}]{4}…)".format(
                number, change.original_text, change.proposed_text, before, after
            )
        )
    return lines


def _check_clause(check):
    """``(who, what)`` for the explanation prompt: a CHECKS member or a free-text instruction.

    ``check`` may be one of ``CHECKS`` (the manuscript review) or any other
    string, which is used verbatim as the instruction the edit followed (the
    quick editor's modes). ``changes`` given to the builders below only need
    ``start``/``end``/``original_text``/``proposed_text``.
    """
    if check in CHECK_LABELS:
        return "A {0} check".format(CHECK_LABELS[check].lower()), "Check: {0}".format(CHECK_DESCRIPTIONS[check])
    return "An editing pass", "Instruction: {0}".format(str(check).strip())


def build_explanation_messages(segment_text, changes, check, language=SAME_LANGUAGE, style_guide=""):
    """Build the prompt asking for one short explanation per change of one segment.

    The author's rules are appended after the check description so the
    explanations do not argue against them. ``check`` is a CHECKS member or
    a plain instruction string (see ``_check_clause``).
    """
    who, what = _check_clause(check)
    return [
        {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "{0} proposed the changes listed below for the text. For each "
                "numbered change, write one short explanation (at most 15 words, {1}) of "
                "why the new version is better. Return a JSON object mapping the change "
                "number to its explanation, e.g. {{\"1\": \"...\", \"2\": \"...\"}}.\n\n"
                "{2}{3}\n\nText:\n{4}\n\nChanges:\n{5}"
            ).format(
                who, _language_clause(language), what,
                format_style_guide(style_guide), segment_text, "\n".join(_change_lines(segment_text, changes, 1)),
            ),
        },
    ]


def build_grouped_explanation_messages(entries, check, language=SAME_LANGUAGE, style_guide=""):
    """Build one explanation prompt for several segments' changes of the same check.

    ``entries`` is a list of ``(segment_text, changes)``. Numbering continues
    across segments, every segment text appears once under a "Segment N"
    header, and the answer is the same flat number → explanation object. A
    single entry produces exactly the single-segment prompt.
    """
    if len(entries) == 1:
        return build_explanation_messages(entries[0][0], entries[0][1], check, language, style_guide)
    blocks = []
    number = 1
    for position, (segment_text, changes) in enumerate(entries, 1):
        blocks.append("Segment {0}:\n{1}\n\nChanges in segment {0}:\n{2}".format(
            position, segment_text, "\n".join(_change_lines(segment_text, changes, number))
        ))
        number += len(changes)
    who, what = _check_clause(check)
    return [
        {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "{0} proposed the changes listed below for {1} segments of a manuscript. For "
                "each numbered change, write one short explanation (at most 15 words, {2}) of why the "
                "new version is better. Return a JSON object mapping the change number to its "
                "explanation, e.g. {{\"1\": \"...\", \"2\": \"...\"}}.\n\n"
                "{3}{4}\n\n{5}"
            ).format(
                who, len(entries), _language_clause(language),
                what, format_style_guide(style_guide), "\n\n".join(blocks),
            ),
        },
    ]


def _parse_json_object(text):
    text = strip_fences(text or "")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def parse_explanations(text):
    """Return {change_number: explanation} from the model's answer (best effort)."""
    data = _parse_json_object(text)
    explanations = {}
    if isinstance(data, dict):
        items = data.get("explanations", data) if isinstance(data.get("explanations", None), (dict, list)) else data
        if isinstance(items, list):
            for number, item in enumerate(items, 1):
                if isinstance(item, str):
                    explanations[number] = item.strip()
                elif isinstance(item, dict):
                    value = item.get("explanation") or item.get("reason") or ""
                    explanations[number] = str(value).strip()
        else:
            for key, value in items.items():
                try:
                    number = int(str(key).strip())
                except ValueError:
                    continue
                if isinstance(value, dict):
                    value = value.get("explanation") or value.get("reason") or ""
                if isinstance(value, str) and value.strip():
                    explanations[number] = value.strip()
    return {number: value[:200] for number, value in explanations.items() if value}


def strip_fences(text):
    """Remove a Markdown code fence wrapping the whole answer."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


# ------------------------------------------------------------ evaluation
class ProposalRejected(Exception):
    """The model's answer is not a plausible edit of the segment."""


def sanity_check_proposal(original, proposed):
    """Reject answers that are clearly not the edited text (summaries, refusals)."""
    original_length = max(1, len(original.strip()))
    proposed_length = len(proposed.strip())
    if proposed_length == 0:
        raise ProposalRejected("The model returned an empty answer.")
    ratio = proposed_length / original_length
    if ratio < 0.5 or ratio > 2.0:
        raise ProposalRejected(
            "The model did not return the complete edited text ({0:.0%} of the original length).".format(ratio)
        )


def evaluate(service, model, text, check, cancel_event, retries=1, style_guide="", glossary=None):
    """Run the edit request of one check on one segment; returns a CheckResult without explanations.

    Edits are requested with ``text_first=True``: the segment comes before the
    check's instruction, so the three checks of one segment share a prompt
    prefix that the GPU server can serve from its prefix cache. ``style_guide``
    holds the author's standing instructions; they are appended to the check's
    instruction. ``glossary`` lists protected terms: they are named in the
    prompt and any change touching one is moved to ``CheckResult.suppressed``
    instead of being proposed. Never raises except on cancel.
    """
    started = time.time()
    attempt = 0
    instruction = build_check_instruction(check, style_guide=style_guide, glossary=glossary)
    while True:
        try:
            proposed = strip_fences(
                service.stream_edit(model, instruction, text, cancel_event, text_first=True)
            ).strip("\n")
            sanity_check_proposal(text, proposed)
            break
        except EditCancelled:
            raise
        except (BackendUnavailable, ProposalRejected) as exc:
            attempt += 1
            if isinstance(exc, OutputTruncated) or attempt > retries:
                return CheckResult(check, "error", error=str(exc), model=model, duration=time.time() - started)
            if cancel_event.wait(1.5):
                raise EditCancelled("Editing was cancelled.")

    changes, suppressed = suppress_glossary_changes(text, extract_changes(text, proposed, check), glossary_matcher(glossary))
    flag_changes(text, changes)
    return CheckResult(
        check, "done", proposed, changes, model=model, duration=time.time() - started, suppressed=suppressed,
    )


def finish_explanations(result):
    """Fill missing explanations with the fallback text and set ``explained``.

    ``explained`` is True only when every change carries a real (canned or
    model-written) explanation.
    """
    fallback = FALLBACK_EXPLANATIONS[result.check]
    for change in result.changes:
        change.explanation = change.explanation or fallback
    result.explained = bool(result.changes) and all(change.explanation != fallback for change in result.changes)
    return result


def request_explanations(service, model, entries, check, cancel_event, language=SAME_LANGUAGE, style_guide=""):
    """Ask the model to explain the changes of one or more segments; returns {number: text}.

    ``entries`` is a list of ``(segment_text, changes)``; numbers continue
    across segments in the order given. Failures other than cancellation
    yield an empty mapping (explanations are best effort).
    """
    if not any(changes for _, changes in entries):
        return {}
    try:
        answer = service.generate(
            model, build_grouped_explanation_messages(entries, check, language, style_guide), cancel_event,
            max_tokens=EXPLANATION_MAX_TOKENS,
        )
    except EditCancelled:
        raise
    except Exception:
        return {}
    return parse_explanations(answer)


def distribute_explanations(changes, numbered):
    """Write the model's numbered answers into ``changes`` (numbered from 1 in list order)."""
    for number, change in enumerate(changes, 1):
        if numbered.get(number):
            change.explanation = numbered[number]


def explain_result(service, model, text, result, cancel_event, language=SAME_LANGUAGE, style_guide=""):
    """Explain the changes of one evaluated result: canned sentences first, one request for the rest."""
    remaining = apply_canned_explanations(text, result.changes, language)
    if remaining:
        numbered = request_explanations(
            service, model, [(text, remaining)], result.check, cancel_event, language, style_guide
        )
        distribute_explanations(remaining, numbered)
    return finish_explanations(result)


def run_check(service, model, text, check, cancel_event, explain=True, language=SAME_LANGUAGE, retries=1,
              style_guide="", glossary=None):
    """Evaluate one check on one segment and explain its changes; returns a CheckResult.

    Convenience wrapper around ``evaluate`` and ``explain`` for callers that
    want one result per call (the combined-mode fallback, scripts, tests).
    The runner uses the two steps separately so explanations can be batched.
    """
    result = evaluate(service, model, text, check, cancel_event, retries, style_guide, glossary)
    if result.status != "done":
        return result
    if explain:
        return explain_result(service, model, text, result, cancel_event, language, style_guide)
    return finish_explanations(result)


# --------------------------------------------------------- combined pass
def _language_clause(language):
    return "in the same language as the text" if language == SAME_LANGUAGE else "in " + language


def build_combined_messages(text, options=None):
    """Build the single request that returns the edits of every enabled check at once."""
    options = options or ProjectOptions()
    checks = options.enabled_checks() or list(CHECKS)
    rules = "\n".join(
        "{0}. {1}: {2}".format(number, check, CHECK_INSTRUCTIONS[check])
        for number, check in enumerate(checks, 1)
    )
    extras = format_style_guide(options.style_guide) + format_glossary(options.glossary)
    content = (
        "Review the text below and list every correction you would make, in the order in which "
        "they occur in the text. Assign each edit exactly one of these categories:\n{rules}{extras}\n\n"
        "Text:\n{text}\n\n"
        "Return a JSON object of this shape (one entry per edit):\n{example}\n"
        "Rules: \"original\" must be an exact substring of the text, with the same spelling, punctuation "
        "and spacing; when it occurs more than once, extend it with the neighbouring words until it is "
        "unique. \"replacement\" is the text that takes its place (repeat the context words unchanged; "
        "an empty string deletes). \"reason\" is one short sentence {language} explaining why the new "
        "version is better. Return {{\"edits\": []}} when nothing needs to change."
    ).format(rules=rules, extras=extras, text=text, example=COMBINED_EXAMPLE,
             language=_language_clause(options.language))
    return [
        {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _anchor(text, snippet, cursor):
    """Return the unique position of ``snippet`` at/after ``cursor`` (else anywhere), or -1."""
    position = text.find(snippet, cursor)
    if position != -1 and text.find(snippet, position + 1) == -1:
        return position
    if position == -1:
        position = text.find(snippet)
        if position != -1 and text.find(snippet, position + 1) == -1:
            return position
    return -1


def apply_changes(text, changes):
    """Return ``text`` with every change applied (changes must not overlap)."""
    output = []
    position = 0
    for change in sorted(changes, key=lambda change: (change.start, change.end)):
        output.append(text[position:change.start])
        output.append(change.proposed_text)
        position = change.end
    output.append(text[position:])
    return "".join(output)


def parse_combined(text, answer, options=None):
    """Turn the combined answer into one ``CheckResult`` per enabled check.

    Every edit is anchored by an exact ``find`` of its ``original`` from a
    moving cursor (falling back to a unique match anywhere); the snippet is
    then diffed against its replacement so the changes are word-level, like
    the separate mode's. Returns ``None`` when the JSON is malformed or any
    edit cannot be anchored unambiguously, so the caller can fall back.
    """
    options = options or ProjectOptions()
    checks = options.enabled_checks() or list(CHECKS)
    data = _parse_json_object(answer)
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        return None
    per_check = {check: [] for check in checks}
    cursor = 0
    for item in data["edits"]:
        if not isinstance(item, dict):
            return None
        category = str(item.get("category", "")).strip().lower()
        original = item.get("original")
        replacement = item.get("replacement")
        if category not in CHECKS or not isinstance(original, str) or not isinstance(replacement, str):
            return None
        if not original:
            return None  # an insertion without context cannot be placed
        position = _anchor(text, original, cursor)
        if position == -1:
            return None
        cursor = max(cursor, position + len(original))
        if category not in per_check or original == replacement:
            continue
        reason = " ".join(str(item.get("reason") or "").split())[:200]
        existing = per_check[category]
        for change in extract_changes(original, replacement, category):
            change.start += position
            change.end += position
            if any(overlaps(change, other) for other in existing):
                continue  # the model listed the same words twice for one category
            change.explanation = reason
            existing.append(change)

    matcher = glossary_matcher(options.glossary)
    results = {}
    for check in checks:
        changes = sorted(per_check[check], key=lambda change: (change.start, change.end))
        for number, change in enumerate(changes, 1):
            change.change_id = "{0}-{1}".format(check, number)
        kept, suppressed = suppress_glossary_changes(text, changes, matcher)
        flag_changes(text, kept)
        for change in kept:
            change.explanation = change.explanation or FALLBACK_EXPLANATIONS[check]
        results[check] = CheckResult(
            check, "done", apply_changes(text, kept), kept, explained=any(
                change.explanation != FALLBACK_EXPLANATIONS[check] for change in kept
            ), suppressed=suppressed, method=EVALUATION_COMBINED,
        )
    return results


def run_segment_combined(service, model, text, options, cancel_event, structured=True,
                         on_structured_unsupported=None):
    """Evaluate every enabled check of one segment with a single request.

    Falls back to ``run_check`` per check (results marked ``separate``) when
    the answer cannot be parsed or anchored, when the backend fails, or when
    it rejects the JSON schema; in the last case ``on_structured_unsupported``
    is called so the runner can stop asking for structured output.
    """
    started = time.time()
    checks = options.enabled_checks() or list(CHECKS)
    messages = build_combined_messages(text, options)
    results = None
    try:
        answer = service.generate(
            model, messages, cancel_event, max_tokens=COMBINED_MAX_TOKENS,
            response_format=COMBINED_SCHEMA if structured else None,
        )
        results = parse_combined(text, strip_fences(answer), options)
    except EditCancelled:
        raise
    except StructuredOutputUnsupported:
        if on_structured_unsupported is not None:
            on_structured_unsupported()
    except Exception:  # any backend failure: the separate mode has its own retries
        results = None
    if results is not None:
        duration = time.time() - started
        for result in results.values():
            result.model = model
            result.duration = duration
        return results
    return {
        check: run_check(
            service, model, text, check, cancel_event, explain=options.explain,
            language=options.language, style_guide=options.style_guide, glossary=options.glossary,
        )
        for check in checks
    }


class ProjectRunner:
    """Evaluate every missing (segment, check) pair on background threads.

    Events: ``("workflow_started", chapter_index, segment_index, check)`` when
    a task begins, ``("workflow_result", chapter_index, segment_index, check,
    CheckResult)`` followed by ``("workflow_progress", done, total, running)``
    when it ends, and finally ``("workflow_finished", cancelled)``. The caller applies results to the
    project on its own thread, so the runner never mutates project state.

    In combined mode a task covers a whole segment: one request answers every
    pending check, and the runner still emits one ``workflow_started`` and one
    ``workflow_result`` per check so the UI needs no special case. When the
    server rejects the JSON schema once, ``structured_output`` turns off for
    the rest of the run.

    In separate mode with explanations enabled the evaluation workers only run
    the edit requests; changes that still need an explanation after the
    canned sentences are handed to a single *explainer* thread, which groups
    up to ``EXPLANATION_GROUP_SIZE`` segments' changes of the same check into
    one request and emits each ``workflow_result`` once its explanations are
    in. Results without open explanations are emitted by the workers directly,
    so every (segment, check) still produces exactly one result event. On
    cancellation the explainer stops after the request in flight and emits
    every waiting result with fallback text.
    """

    def __init__(self, project, service, model, events, parallelism=None):
        self.project = project
        self.service = service
        self.model = model
        self.events = events
        self.parallelism = max(1, int(parallelism or project.options.parallelism or 1))
        self.cancel_event = threading.Event()
        self.tasks = queue.Queue()
        self.explain_queue = queue.Queue()  # (chapter, segment, check, result, remaining changes, text)
        self.total = 0
        self.done = 0
        self.failed = 0
        self.running = 0
        self.explanation_requests = 0
        self._active_workers = 0
        self._lock = threading.Lock()
        self._threads = []
        self._explainer = None
        self._explainer_exiting = False  # set under the lock when the explainer decided to stop
        self.started_at = None
        self.structured_output = True  # cleared when the backend rejects response_format

    @property
    def batches_explanations(self):
        """Whether an explainer thread handles explanations (separate mode with explanations on)."""
        return bool(self.project.options.explain) and not self.project.options.combined

    @property
    def active(self):
        return any(thread.is_alive() for thread in self._threads)

    def has_work(self):
        """Whether tasks are queued, running or waiting for explanations.

        A ``workflow_finished`` event seen while this is True is stale: tasks
        were enqueued after the workers that emitted it had retired.
        """
        with self._lock:
            return self.running > 0 or not self.tasks.empty() or not self.explain_queue.empty()

    def start(self):
        """Queue every pending task and start the workers; returns the task count.

        The queue keeps ``pending_tasks()`` order, (chapter, segment, check):
        all checks of one segment are dispatched consecutively, and because
        ``run_check`` puts the segment text before the instruction, the GPU
        server sees the same prompt prefix back to back and can reuse it from
        its prefix cache (vLLM ``--enable-prefix-caching``) instead of
        re-encoding the segment for every check. Keep the order when changing
        the scheduling.
        """
        pending = self.project.pending_tasks()
        self.total = len(pending)
        self.started_at = time.time()
        for task in pending:
            self.tasks.put(task)
        if not pending:
            self.events.put(("workflow_finished", False))
            return 0
        workers = min(self.parallelism, len(pending))
        self._active_workers = workers
        if self.batches_explanations:
            self._start_explainer()
        self._spawn(workers)
        return len(pending)

    def enqueue(self, tasks):
        """Add (chapter, segment, check) tasks to a running evaluation; returns how many were queued.

        Workers retire as soon as the queue is empty, so new workers are started
        (up to ``parallelism``) for the tasks, and the explainer is restarted if
        it already stopped. Works after the runner finished, too: the new tasks
        then produce a second ``workflow_finished`` event.
        """
        tasks = list(tasks)
        if not tasks:
            return 0
        if self.started_at is None:
            self.started_at = time.time()
        with self._lock:
            self.total += len(tasks)
            for task in tasks:
                self.tasks.put(task)
            spawn = max(0, min(self.parallelism - self._active_workers, len(tasks)))
            self._active_workers += spawn
            restart_explainer = self.batches_explanations and (self._explainer is None or self._explainer_exiting)
            if restart_explainer:
                self._explainer_exiting = False
        if restart_explainer:
            self._start_explainer()
        self._spawn(spawn)
        return len(tasks)

    def _start_explainer(self):
        self._explainer = threading.Thread(target=self._explainer_loop, name="teai-explain", daemon=True)
        self._threads.append(self._explainer)
        self._explainer.start()

    def _spawn(self, count):
        for _ in range(count):
            thread = threading.Thread(target=self._worker, name="teai-eval-{0}".format(len(self._threads)), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _emit_results(self, chapter_index, segment_index, results):
        """Publish the results of one finished task (one or several checks) plus its progress."""
        with self._lock:
            self.done += 1
            if any(result.status == "error" for result in results.values()):
                self.failed += 1
            done, running = self.done, self.running
        for check, result in results.items():
            self.events.put(("workflow_result", chapter_index, segment_index, check, result))
        self.events.put(("workflow_progress", done, self.total, running))

    def _emit_result(self, chapter_index, segment_index, check, result):
        self._emit_results(chapter_index, segment_index, {check: result})

    def cancel(self):
        self.cancel_event.set()

    def _next_task(self):
        """Take the next task and count it as running; None when the queue is empty.

        Both happen under one lock so that has_work() never sees a task that is
        neither queued nor running.
        """
        with self._lock:
            try:
                task = self.tasks.get_nowait()
            except queue.Empty:
                return None
            self.running += 1
            return task

    def _skip_task(self):
        with self._lock:
            self.running -= 1

    def _worker(self):
        options = self.project.options
        try:
            while not self.cancel_event.is_set():
                task = self._next_task()
                if task is None:
                    break
                chapter_index, segment_index, check = task
                _, segment = self.project.find(chapter_index, segment_index)
                if segment is None:
                    self._skip_task()
                    continue
                checks = self.project.pending_checks(segment) if check is None else [check]
                if not checks:
                    self._skip_task()
                    continue
                for pending in checks:
                    self.events.put(("workflow_started", chapter_index, segment_index, pending))
                deferred = None  # (result, remaining changes) handed to the explainer
                try:
                    if check is None:
                        results = run_segment_combined(
                            self.service, self.model, segment.text, options, self.cancel_event,
                            structured=self.structured_output,
                            on_structured_unsupported=self._disable_structured_output,
                        )
                        results = {pending: results[pending] for pending in checks}
                    else:
                        result = evaluate(
                            self.service, self.model, segment.text, check, self.cancel_event,
                            style_guide=options.style_guide, glossary=options.glossary,
                        )
                        if result.status == "done" and self.batches_explanations:
                            remaining = apply_canned_explanations(segment.text, result.changes, options.language)
                            if remaining:
                                deferred = (result, remaining)
                            else:
                                finish_explanations(result)
                        elif result.status == "done":
                            finish_explanations(result)
                        results = {check: result}
                except EditCancelled:
                    with self._lock:
                        self.running -= 1
                    break
                except Exception as exc:  # defensive: a crash must not kill the worker
                    results = {
                        pending: CheckResult(pending, "error", error="Unexpected error: {0!r}".format(exc), model=self.model)
                        for pending in checks
                    }
                with self._lock:
                    self.running -= 1
                if deferred is not None:
                    self.explain_queue.put((chapter_index, segment_index, check, deferred[0], deferred[1], segment.text))
                    continue
                self._emit_results(chapter_index, segment_index, {pending: results[pending] for pending in checks})
        finally:
            with self._lock:
                # Decided and sent under the lock so that enqueue() cannot slip new
                # tasks in between the count reaching zero and the event being sent.
                self._active_workers -= 1
                if self._active_workers == 0:
                    if self._explainer is not None and not self._explainer_exiting:
                        self.explain_queue.put(None)  # the explainer emits workflow_finished
                    else:
                        self.events.put(("workflow_finished", self.cancel_event.is_set()))

    # ------------------------------------------------------------ explainer
    def _explainer_loop(self):
        """Group waiting explanations per check and request them in batches.

        Runs until the workers' sentinel arrives so ``workflow_finished`` is
        the last event. Entries that arrive after a cancel, or that were
        waiting when it happened, are emitted with fallback explanations.
        """
        finished = False
        try:
            while not finished:
                try:
                    item = self.explain_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                batch = []
                sentinel = item is None
                if not sentinel:
                    batch.append(item)
                while True:  # take everything that is ready now, so groups form naturally
                    try:
                        more = self.explain_queue.get_nowait()
                    except queue.Empty:
                        break
                    if more is None:
                        sentinel = True
                    else:
                        batch.append(more)
                if sentinel:
                    with self._lock:
                        # enqueue() may have started new workers since the sentinel was sent;
                        # then this thread keeps serving them instead of stopping.
                        if self._active_workers == 0:
                            self._explainer_exiting = True
                            finished = True
                if batch:
                    self._explain_batch(batch)
        finally:
            while True:  # never leave a result unpublished
                try:
                    item = self.explain_queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    self._emit_explained(item, cancelled=True)
            self.events.put(("workflow_finished", self.cancel_event.is_set()))

    def _explain_batch(self, entries):
        options = self.project.options
        by_check = {}
        for entry in entries:
            by_check.setdefault(entry[2], []).append(entry)
        for check in CHECKS:
            group = by_check.get(check) or []
            for start in range(0, len(group), EXPLANATION_GROUP_SIZE):
                chunk = group[start:start + EXPLANATION_GROUP_SIZE]
                numbered = {}
                if not self.cancel_event.is_set():
                    try:
                        with self._lock:
                            self.explanation_requests += 1
                        numbered = request_explanations(
                            self.service, self.model, [(text, remaining) for _, _, _, _, remaining, text in chunk],
                            check, self.cancel_event, options.language, options.style_guide,
                        )
                    except EditCancelled:
                        numbered = {}
                flat = [change for _, _, _, _, remaining, _ in chunk for change in remaining]
                distribute_explanations(flat, numbered)
                for entry in chunk:
                    self._emit_explained(entry)

    def _emit_explained(self, entry, cancelled=False):
        chapter_index, segment_index, check, result, _, _ = entry
        finish_explanations(result)
        self._emit_result(chapter_index, segment_index, check, result)

    def _disable_structured_output(self):
        self.structured_output = False

    def eta_seconds(self):
        """Rough remaining time based on the throughput so far."""
        if not self.started_at or not self.done:
            return None
        elapsed = time.time() - self.started_at
        remaining = self.total - self.done
        return elapsed / self.done * remaining


def new_decision_group():
    """Return a fresh id that ties the decisions of one bulk action together."""
    return uuid.uuid4().hex


def apply_result(project, chapter_index, segment_index, check, result):
    """Store a result on the project, honouring auto-accept options."""
    _, segment = project.find(chapter_index, segment_index)
    if segment is None:
        return None
    if result.status == "done" and project.options.auto_accept.get(check):
        for change in result.changes:
            change.decision = ACCEPTED
    segment.results[check] = result
    return segment
