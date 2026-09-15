"""Tests for the shared backend helpers (prompt construction)."""

from core.backend import SYSTEM_PROMPT, TEXT_FIRST_NOTE, build_messages


def test_default_order_is_instruction_then_text_and_unchanged():
    messages = build_messages("Fix grammar.", "Original.")

    assert messages == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Instruction:\nFix grammar.\n\nText:\nOriginal."},
    ]
    assert build_messages("Fix grammar.", "Original.", text_first=False) == messages
    assert TEXT_FIRST_NOTE not in SYSTEM_PROMPT


def test_text_first_shares_a_prefix_across_instructions():
    first = build_messages("Correct spelling.", "Same segment.", text_first=True)
    second = build_messages("Fix grammar.", "Same segment.", text_first=True)

    assert first[0] == second[0]
    assert first[0]["content"] == SYSTEM_PROMPT + " " + TEXT_FIRST_NOTE
    assert first[1]["content"] == "Text:\nSame segment.\n\nInstruction:\nCorrect spelling."
    prefix = "Text:\nSame segment.\n\nInstruction:\n"
    assert first[1]["content"].startswith(prefix) and second[1]["content"].startswith(prefix)


def test_usage_totals_accumulate_and_format():
    from core.backend import UsageRecord, add_usage, empty_usage, format_usage, normalise_usage

    totals = empty_usage()
    assert totals == {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0, "seconds": 0.0}
    add_usage(totals, UsageRecord(1200, 300, 2.0, "m"))
    add_usage(totals, UsageRecord(800, 100, 1.5, "m"))
    assert totals == {"prompt_tokens": 2000, "completion_tokens": 400, "requests": 2, "seconds": 3.5}
    assert format_usage(totals) == "2.4k tokens (2.0k in, 400 out) \u00b7 2 requests \u00b7 114 tok/s"
    assert format_usage(totals, compact=False) == "2,400 tokens (2,000 in, 400 out) \u00b7 2 requests \u00b7 114 tok/s"
    assert format_usage(empty_usage()) == "0 tokens (0 in, 0 out) \u00b7 0 requests"
    assert format_usage({"prompt_tokens": 1_500_000, "completion_tokens": 20_000, "requests": 1}).startswith(
        "1.5M tokens (1.5M in, 20k out)"
    )
    # damaged or old data never raises
    assert normalise_usage(None) == empty_usage()
    assert normalise_usage({"prompt_tokens": "7", "seconds": None, "requests": "x"}) == {
        "prompt_tokens": 7, "completion_tokens": 0, "requests": 0, "seconds": 0.0,
    }
