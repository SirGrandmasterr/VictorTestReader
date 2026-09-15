"""Tests for the shared backend helpers (prompt construction, streamed reasoning)."""

from core.backend import STREAM_ANSWER, STREAM_THINKING, SYSTEM_PROMPT, TEXT_FIRST_NOTE, ThinkingSplitter, build_messages


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


# ------------------------------------------------------ streamed reasoning
def split(chunks):
    """Feed ``chunks`` through a ThinkingSplitter and return the (kind, text) pieces it hands out."""
    pieces = []
    splitter = ThinkingSplitter(lambda kind, text: pieces.append((kind, text)))
    for chunk in chunks:
        splitter.feed(chunk)
    splitter.flush()
    return pieces


def test_splitter_routes_a_leading_think_block_even_when_tags_straddle_chunks():
    assert split(["<thi", "nk>\nplan ", "more</th", "ink>\n\nEdited."]) == [
        (STREAM_THINKING, "\nplan "), (STREAM_THINKING, "more"), (STREAM_ANSWER, "Edited."),
    ]
    assert split(["  <think>a</think>b", "c"]) == [(STREAM_THINKING, "a"), (STREAM_ANSWER, "b"), (STREAM_ANSWER, "c")]


def test_splitter_passes_plain_answers_and_non_leading_tags_through():
    assert split(["Plain ", "text"]) == [(STREAM_ANSWER, "Plain "), (STREAM_ANSWER, "text")]
    assert split(["Hello <think>x</think>"]) == [(STREAM_ANSWER, "Hello <think>x</think>")]
    assert split(["<t"]) == [(STREAM_ANSWER, "<t")]  # looked like a tag until the stream ended
    assert split(["", "   "]) == []


def test_splitter_keeps_an_unterminated_block_as_reasoning():
    assert split(["<think>unterminated ", "reason", "</thi"]) == [
        (STREAM_THINKING, "unterminated "), (STREAM_THINKING, "reason"), (STREAM_THINKING, "</thi"),
    ]


def test_splitter_without_callback_is_inert():
    splitter = ThinkingSplitter(None)
    splitter.feed("<think>x</think>y")
    splitter.flush()
