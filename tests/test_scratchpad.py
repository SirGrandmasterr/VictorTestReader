"""Tests for Markdown proposal and outcome logging."""

from datetime import datetime

from core.diff_engine import build_edit_session, render_reviewed_text
from core.scratchpad import ScratchpadLogger


def test_scratchpad_records_proposal_decisions_and_final_text(tmp_path):
    session = build_edit_session(
        "This are wrong.",
        "This is correct.",
        instruction="Fix grammar.",
        model="test-model",
        revision_id=3,
    )
    session.review_items[0].changed_hunks[0].decision = "accepted"
    for hunk in session.review_items[0].changed_hunks[1:]:
        hunk.decision = "rejected"
    final_text = render_reviewed_text(session)
    logger = ScratchpadLogger(
        tmp_path, now_provider=lambda: datetime(2026, 7, 12, 10, 11, 12)
    )

    path = logger.log_proposal(session)
    logger.log_outcome(session, "applied", final_text)
    content = path.read_text(encoding="utf-8")

    assert path.name == "TextEnhanceAI-scratchpad_20260712_101112.md"
    assert "Model: `test-model`" in content
    assert "## Original text" in content
    assert "## Proposed text" in content
    assert "Outcome: **applied**" in content
    assert "Change 1: accepted" in content
    assert final_text in content


def test_discarded_session_is_logged_without_losing_proposal(tmp_path):
    session = build_edit_session("Old text.", "New text.")
    logger = ScratchpadLogger(
        tmp_path, now_provider=lambda: datetime(2026, 7, 12, 10, 11, 13)
    )

    path = logger.log_proposal(session)
    logger.log_outcome(session, "discarded", session.original_text)
    content = path.read_text(encoding="utf-8")

    assert "## Suggestions" in content
    assert "Outcome: **discarded**" in content
    assert "Old text." in content


def test_selection_only_sessions_record_the_span(tmp_path):
    full = "Keep. Teh cat. Keep."
    session = build_edit_session("Teh cat.", "The cat.", selection=(6, 14), full_text=full)
    logger = ScratchpadLogger(tmp_path, now_provider=lambda: datetime(2026, 7, 12, 10, 11, 14))

    content = logger.log_proposal(session).read_text(encoding="utf-8")

    assert "- Selection: chars 6–14 of 20 characters" in content
    assert "Keep." not in content.split("## Original text")[1].split("## Proposed text")[0]


def test_chain_steps_are_logged_with_their_intermediate_output(tmp_path):
    from core.models import ChainStep

    session = build_edit_session("teh cat", "THE CAT", instruction="Chain: Fix -> Loud")
    session.steps = [ChainStep("Fix", "fix", "the cat"), ChainStep("Loud", "upper", "THE CAT")]
    logger = ScratchpadLogger(tmp_path, now_provider=lambda: datetime(2026, 7, 12, 10, 11, 15))

    content = logger.log_proposal(session).read_text(encoding="utf-8")

    assert "## Steps" in content
    assert "### Step 1: Fix" in content and "- Instruction: fix" in content and "the cat" in content
    assert "### Step 2: Loud" in content
    assert content.index("## Steps") < content.index("## Proposed text")

    single = build_edit_session("a", "b")
    single.steps = [ChainStep("Grammar", "g", "b")]
    assert "## Steps" not in logger.log_proposal(single).read_text(encoding="utf-8").split("---")[-1]
