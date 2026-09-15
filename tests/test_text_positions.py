"""Tests for offset ↔ Tk-index conversion and selection-only edit sessions."""

import pytest

from core.diff_engine import build_edit_session, render_reviewed_text
from core.models import EditSession
from core.text_positions import char_offset, describe_span, normalise_span, tk_index

TEXT = "First line\nsecond, longer line\n\nfourth"


@pytest.mark.parametrize("offset, index", [
    (0, "1.0"), (5, "1.5"), (10, "1.10"), (11, "2.0"), (18, "2.7"),
    (30, "2.19"), (31, "3.0"), (32, "4.0"), (38, "4.6"),
])
def test_offsets_and_tk_indices_convert_both_ways(offset, index):
    assert tk_index(TEXT, offset) == index
    assert char_offset(TEXT, index) == offset


def test_conversions_clamp_out_of_range_values():
    assert tk_index(TEXT, -5) == "1.0"
    assert tk_index(TEXT, 999) == "4.6"
    assert char_offset(TEXT, "1.99") == 10  # past the end of the line stops at the newline
    assert char_offset(TEXT, "9.0") == len(TEXT)
    assert char_offset(TEXT, "0.3") == 0
    with pytest.raises(ValueError):
        char_offset(TEXT, "1.0 + 3 chars")


def test_normalise_span_orders_clamps_and_rejects_empty_spans():
    assert normalise_span(TEXT, 12, 4) == (4, 12)
    assert normalise_span(TEXT, -3, 500) == (0, len(TEXT))
    assert normalise_span(TEXT, 7, 7) is None
    assert describe_span((4, 12)) == "chars 4–12"


def test_edit_session_keeps_selection_and_exposes_context():
    full = "Untouched start. Teh cat sat. Untouched end."
    selection = (17, 29)
    assert full[selection[0]:selection[1]] == "Teh cat sat."

    session = build_edit_session(
        "Teh cat sat.", "The cat sat.", instruction="Fix", model="m", revision_id=1,
        selection=selection, full_text=full,
    )

    assert session.selection == selection and session.full_text == full
    assert session.context_text == full and session.context_offset == 17
    item = session.review_items[0]
    assert full[session.context_offset + item.original_start:session.context_offset + item.original_end] == "Teh cat sat."
    session.accept_all()
    assert full[:17] + render_reviewed_text(session) + full[29:] == "Untouched start. The cat sat. Untouched end."

    whole = build_edit_session("Teh cat.", "The cat.", selection=None, full_text="ignored")
    assert whole.selection is None and whole.full_text == "" and whole.context_offset == 0
    assert EditSession("a", "b", "i", "m", 0).selection is None
