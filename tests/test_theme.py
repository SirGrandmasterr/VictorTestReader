"""Tests for the palette, glyph and marker helpers of the theme (no Tk window needed)."""

import pytest

from core.models import ACCEPTED, PENDING, REJECTED
from core.workflow import (
    STATE_APPLIED,
    STATE_PENDING,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATUS_CLEAN,
    STATUS_ERROR,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_REVIEWED,
)
from ui import theme
from ui.review_panel import HUNK_KIND_LABELS, decision_text, display_text
from ui.workflow_screen import (
    STATE_LABELS,
    STATUS_LABELS,
    estimate_text,
    mark_spans,
    short_term,
    state_text,
    status_short,
    status_text,
)


@pytest.fixture(autouse=True)
def normal_palette_after_each_test():
    yield
    theme.select_palette(False)


def test_both_palettes_define_the_same_tokens():
    assert set(theme.PALETTES["normal"]) == set(theme.PALETTES["high_contrast"])
    assert set(theme.CHECK_PALETTES["normal"]) == set(theme.CHECK_PALETTES["high_contrast"])
    for palette in theme.PALETTES.values():
        assert all(value.startswith("#") and len(value) == 7 for value in palette.values())


def test_select_palette_updates_the_shared_dictionaries_in_place():
    palette, checks, statuses = theme.PALETTE, theme.CHECK_COLORS, theme.STATUS_COLORS
    assert theme.select_palette(True) == "high_contrast"
    assert theme.is_high_contrast()
    assert theme.PALETTE is palette and palette["text"] == "#000000" and palette["border"] == "#000000"
    assert theme.CHECK_COLORS is checks and checks["spelling"] == theme.CHECK_PALETTES["high_contrast"]["spelling"]
    assert theme.STATUS_COLORS is statuses and statuses["error"] == palette["danger"]
    assert theme.select_palette(False) == "normal"
    assert not theme.is_high_contrast()
    assert palette["text"] == "#2a2521"
    assert set(statuses) == {STATUS_QUEUED, "running", STATUS_ERROR, STATUS_CLEAN, STATUS_READY, STATUS_REVIEWED}


def test_every_status_and_decision_state_has_a_glyph_and_a_word():
    for status in (STATUS_QUEUED, "running", STATUS_ERROR, STATUS_CLEAN, STATUS_READY, STATUS_REVIEWED):
        assert theme.STATUS_GLYPHS[status]
        text = status_text(status)
        assert text.startswith(theme.STATUS_GLYPHS[status] + " ")
        assert STATUS_LABELS[status] in text
    assert theme.STATUS_GLYPHS["flagged"] == "◆"
    assert status_text(STATUS_READY, "2 pending") == "○ To review · 2 pending"
    # every status has its own shape, and none of them is a symbol Windows renders as a colour emoji
    assert len(set(theme.STATUS_GLYPHS.values())) == len(theme.STATUS_GLYPHS)
    emoji = {"⏳", "⚠", "✔", "✖", "▶"}
    assert not emoji & set(theme.STATUS_GLYPHS.values())
    assert not emoji & set(theme.DECISION_GLYPHS.values())
    for state in (STATE_APPLIED, STATE_SUPERSEDED, STATE_REJECTED, STATE_PENDING):
        assert theme.DECISION_GLYPHS[state]
        assert state_text(state) == "{0} {1}".format(theme.DECISION_GLYPHS[state], STATE_LABELS[state])
    assert theme.DECISION_GLYPHS["edited"] == "✎"
    assert len({theme.DECISION_GLYPHS[s] for s in (STATE_APPLIED, STATE_REJECTED, STATE_PENDING)}) == 3


def test_quick_review_decisions_get_glyphs_and_hunk_kinds_get_words():
    assert decision_text(ACCEPTED) == "✓ Accepted"
    assert decision_text(REJECTED) == "✗ Rejected"
    assert decision_text(PENDING) == "○ Pending"
    assert decision_text("mixed") == "✎ Partially accepted"
    assert HUNK_KIND_LABELS == {"delete": "removed", "insert": "added", "replace": "replaced"}
    # a hunk row never shows a bare symbol for "nothing" or a paragraph break
    assert display_text("") == "(nothing)"
    assert display_text("one\ntwo") == "one ¶ two"


def test_tree_column_gets_the_short_status_with_the_same_glyph():
    assert status_short(STATUS_READY, pending=3) == "○ 3 open"
    assert status_short(STATUS_REVIEWED) == "● done"
    assert status_short(STATUS_QUEUED) == "◌ queued"
    assert status_short("running") == "◐ evaluating"
    for status in (STATUS_QUEUED, "running", STATUS_ERROR, STATUS_CLEAN, STATUS_READY, STATUS_REVIEWED):
        assert status_short(status).split(" ")[0] == status_text(status).split(" ")[0]
    assert short_term("  a   name ") == "a name"
    assert short_term("x" * 40) == "x" * 23 + "…"


def test_start_view_estimate_turns_into_minutes_once_timing_is_known():
    assert estimate_text(1240) == "≈ 1,240 model requests"
    assert estimate_text(1240, None, 4) == "≈ 1,240 model requests"
    assert estimate_text(3, 12.0) == "≈ 3 model requests · about a minute with this model"
    assert estimate_text(120, 6.0, 1) == "≈ 120 model requests · about 12 min with this model"
    assert estimate_text(120, 6.0, 4) == "≈ 120 model requests · about 3 min with this model"
    assert estimate_text(1240, 9.0, 2) == "≈ 1,240 model requests · about 1 h 33 min with this model"


def test_font_roles_put_the_authors_text_and_titles_in_the_serif_face():
    assert theme.FONT_ROLES["text"][0] > theme.FONT_ROLES["body"][0]
    assert set(theme.SERIF_ROLES) == {"title", "text"}
    assert theme.FONT_ROLES["title"][1] == "normal"  # a page title, not a form heading


def test_scale_clamping_and_scaled_sizes(monkeypatch):
    assert theme.clamp_scale(1.0) == 1.0
    assert theme.clamp_scale(0.2) == 0.8
    assert theme.clamp_scale(5) == 2.0
    assert theme.clamp_scale("x") == 1.0
    monkeypatch.setattr(theme, "_scale", 1.6)
    assert theme.scaled(10) == 16
    assert theme.scaled(26) == 42
    monkeypatch.setattr(theme, "_scale", 0.8)
    assert theme.scaled(5) == 6  # never below 6


def _span(start, end, state, change_id, flags=()):
    return {"start": start, "end": end, "state": state, "change_id": change_id, "segment": 1, "check": "spelling",
            "flags": list(flags)}


def test_mark_spans_inserts_markers_and_shifts_the_offsets():
    text = "The dog was extremely big."
    spans = [
        _span(4, 7, STATE_APPLIED, "a"),  # "dog"
        _span(8, 11, STATE_PENDING, "b"),  # "was"
        _span(12, 21, STATE_REJECTED, "c"),  # "extremely"
    ]
    marked, shifted = mark_spans(text, spans)
    assert marked == "The [+]dog [~]was [−]extremely big."
    assert [marked[s["start"]:s["end"]] for s in shifted] == ["dog", "was", "extremely"]
    assert [s["change_id"] for s in shifted] == ["a", "b", "c"]
    assert spans[0]["start"] == 4  # the input is untouched
    for original, copy in zip(spans, shifted):
        assert copy["state"] == original["state"] and copy["flags"] == original["flags"]


def test_mark_spans_handles_shared_starts_empty_spans_and_unknown_states():
    text = "Der dog were big."
    spans = [
        _span(0, 3, STATE_APPLIED, "author"),  # "Der" replaced "Teh"
        _span(0, 3, STATE_SUPERSEDED, "spelling"),  # collapsed onto the replacement
        _span(8, 8, STATE_PENDING, "insert"),  # an insertion point
        _span(13, 16, "unknown", "x"),  # no marker for states without one
    ]
    marked, shifted = mark_spans(text, spans)
    assert marked == "[+][−]Der dog [~]were big."
    assert marked[shifted[0]["start"]:shifted[0]["end"]] == "Der"
    assert marked[shifted[1]["start"]:shifted[1]["end"]] == "Der"
    assert shifted[2]["start"] == shifted[2]["end"] == marked.index("were")
    assert marked[shifted[3]["start"]:shifted[3]["end"]] == "big"


def test_mark_spans_without_markable_spans_returns_copies():
    text = "Nothing here."
    marked, shifted = mark_spans(text, [_span(0, 7, "unknown", "x")])
    assert marked == text
    assert shifted == [_span(0, 7, "unknown", "x")]
    assert mark_spans(text, []) == (text, [])
