"""Sentence-aligned, word-level diff construction."""

import difflib
import re

from .models import ACCEPTED, FIXED, ChangeHunk, EditSession, ReviewItem


SENTENCE_END_RE = re.compile(
    r"[.!?]+[\"'\N{RIGHT DOUBLE QUOTATION MARK}"
    r"\N{RIGHT SINGLE QUOTATION MARK})\]]*(?=\s+|$)"
)
BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
TOKEN_RE = re.compile(r"\w+(?:['\N{RIGHT SINGLE QUOTATION MARK}-]\w+)*|[^\w\s]|\s+", re.UNICODE)
GAP_COST = 0.75
MERGE_PENALTY = 0.12
MERGE_SIMILARITY_THRESHOLD = 0.40


def _sentence_units(text):
    """Split text into sentence-like units while retaining every character."""
    if not text:
        return []

    units = []
    start = 0
    length = len(text)
    while start < length:
        sentence_match = SENTENCE_END_RE.search(text, start)
        blank_match = BLANK_LINE_RE.search(text, start)

        matches = [match for match in (sentence_match, blank_match) if match]
        if not matches:
            units.append(text[start:])
            break

        boundary = min(matches, key=lambda match: match.start())
        end = boundary.end()
        if boundary.re is SENTENCE_END_RE:
            whitespace = re.match(r"\s+", text[end:])
            if whitespace:
                end += whitespace.end()

        if end <= start:
            end = start + 1
        units.append(text[start:end])
        start = end

    return units


def _normalise_sentence(text):
    """Return a comparison key that ignores inconsequential whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def _tokenise(text):
    """Tokenize text while preserving whitespace and punctuation."""
    return TOKEN_RE.findall(text)


def _token_key(token):
    """Return a stable alignment key for a token."""
    if token.isspace():
        return " "
    return token


def _sentence_similarity(original_units, proposed_units):
    """Return lexical similarity for one sentence or a split/merge group."""
    original = _normalise_sentence("".join(original_units))
    proposed = _normalise_sentence("".join(proposed_units))
    return difflib.SequenceMatcher(
        None, original, proposed, autojunk=False
    ).ratio()


def _align_changed_units(original_units, proposed_units):
    """Align changed sentences, including one-to-two splits and two-to-one merges."""
    original_count = len(original_units)
    proposed_count = len(proposed_units)
    infinity = float("inf")
    costs = [
        [infinity for _ in range(proposed_count + 1)]
        for _ in range(original_count + 1)
    ]
    previous = [
        [None for _ in range(proposed_count + 1)]
        for _ in range(original_count + 1)
    ]
    costs[0][0] = 0.0

    def consider(old_i, old_j, new_i, new_j, extra_cost):
        candidate = costs[old_i][old_j] + extra_cost
        if candidate < costs[new_i][new_j]:
            costs[new_i][new_j] = candidate
            previous[new_i][new_j] = (old_i, old_j)

    for i in range(original_count + 1):
        for j in range(proposed_count + 1):
            if costs[i][j] == infinity:
                continue

            if i < original_count and j < proposed_count:
                similarity = _sentence_similarity(
                    original_units[i:i + 1], proposed_units[j:j + 1]
                )
                consider(i, j, i + 1, j + 1, 1.0 - similarity)

            if i + 1 < original_count and j < proposed_count:
                similarity = _sentence_similarity(
                    original_units[i:i + 2], proposed_units[j:j + 1]
                )
                if similarity >= MERGE_SIMILARITY_THRESHOLD:
                    consider(
                        i,
                        j,
                        i + 2,
                        j + 1,
                        1.0 - similarity + MERGE_PENALTY,
                    )

            if i < original_count and j + 1 < proposed_count:
                similarity = _sentence_similarity(
                    original_units[i:i + 1], proposed_units[j:j + 2]
                )
                if similarity >= MERGE_SIMILARITY_THRESHOLD:
                    consider(
                        i,
                        j,
                        i + 1,
                        j + 2,
                        1.0 - similarity + MERGE_PENALTY,
                    )

            if i < original_count:
                consider(i, j, i + 1, j, GAP_COST)
            if j < proposed_count:
                consider(i, j, i, j + 1, GAP_COST)

    alignments = []
    i = original_count
    j = proposed_count
    while i or j:
        prior = previous[i][j]
        if prior is None:
            raise RuntimeError("Unable to align changed sentence groups.")
        old_i, old_j = prior
        alignments.append((old_i, i, old_j, j))
        i, j = old_i, old_j
    alignments.reverse()
    return alignments


def _build_hunks(original_text, proposed_text):
    """Build explicit change hunks for an aligned review item."""
    original_tokens = _tokenise(original_text)
    proposed_tokens = _tokenise(proposed_text)
    matcher = difflib.SequenceMatcher(
        None,
        [_token_key(token) for token in original_tokens],
        [_token_key(token) for token in proposed_tokens],
        autojunk=False,
    )

    hunks = []
    for kind, i1, i2, j1, j2 in matcher.get_opcodes():
        original = "".join(original_tokens[i1:i2])
        proposed = "".join(proposed_tokens[j1:j2])
        decision = FIXED if kind == "equal" else "pending"
        hunks.append(ChangeHunk(kind, original, proposed, decision))
    return hunks


def build_edit_session(
    original_text,
    proposed_text,
    instruction="",
    model="",
    revision_id=0,
    selection=None,
    full_text="",
):
    """Create an EditSession from original and proposed text.

    ``selection``/``full_text`` record that ``original_text`` is only the
    selected span of the editor text (see ``EditSession``).
    """
    original_units = _sentence_units(original_text)
    proposed_units = _sentence_units(proposed_text)
    original_keys = [_normalise_sentence(unit) for unit in original_units]
    proposed_keys = [_normalise_sentence(unit) for unit in proposed_units]
    matcher = difflib.SequenceMatcher(
        None, original_keys, proposed_keys, autojunk=False
    )

    offsets = [0]
    for unit in original_units:
        offsets.append(offsets[-1] + len(unit))

    session = EditSession(
        original_text=original_text,
        proposed_text=proposed_text,
        instruction=instruction,
        model=model,
        revision_id=revision_id,
        selection=tuple(selection) if selection else None,
        full_text=full_text if selection else "",
    )

    next_item_id = 1
    for kind, i1, i2, j1, j2 in matcher.get_opcodes():
        if kind == "equal":
            for original_index, proposed_index in zip(
                range(i1, i2), range(j1, j2)
            ):
                original = original_units[original_index]
                proposed = proposed_units[proposed_index]
                if original == proposed:
                    session.parts.append(original)
                    continue
                item = ReviewItem(
                    item_id=next_item_id,
                    original_start=offsets[original_index],
                    original_end=offsets[original_index + 1],
                    original_text=original,
                    proposed_text=proposed,
                    hunks=_build_hunks(original, proposed),
                )
                session.parts.append(item)
                session.review_items.append(item)
                next_item_id += 1
            continue

        changed_original = original_units[i1:i2]
        changed_proposed = proposed_units[j1:j2]
        for local_i1, local_i2, local_j1, local_j2 in _align_changed_units(
            changed_original, changed_proposed
        ):
            original_start_index = i1 + local_i1
            original_end_index = i1 + local_i2
            original = "".join(changed_original[local_i1:local_i2])
            proposed = "".join(changed_proposed[local_j1:local_j2])
            if original == proposed:
                session.parts.append(original)
                continue
            item = ReviewItem(
                item_id=next_item_id,
                original_start=offsets[original_start_index],
                original_end=offsets[original_end_index],
                original_text=original,
                proposed_text=proposed,
                hunks=_build_hunks(original, proposed),
            )
            session.parts.append(item)
            session.review_items.append(item)
            next_item_id += 1

    return session


def render_reviewed_text(session):
    """Reconstruct a document from the current session decisions."""
    return "".join(
        part if isinstance(part, str) else part.render()
        for part in session.parts
    )


def accept_all(session):
    """Accept every change and return the proposed document."""
    session.accept_all()
    return render_reviewed_text(session)


def reject_all(session):
    """Reject every change and return the original document."""
    session.reject_all()
    return render_reviewed_text(session)
