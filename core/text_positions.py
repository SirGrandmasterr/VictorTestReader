"""Convert between character offsets and Tk text indices without importing Tk.

The quick editor keeps selections as ``(start, end)`` character offsets into
the editor text (what ``EditSession.selection`` stores); the Tk ``Text``
widget speaks ``"line.column"`` indices with 1-based lines and 0-based
columns. Both directions only need the text itself, so they live here where
they can be tested.
"""

import re

_INDEX = re.compile(r"^\s*(\d+)\.(\d+)\s*$")


def tk_index(text, offset):
    """Return the ``"line.column"`` index of character ``offset`` in ``text``."""
    offset = max(0, min(int(offset), len(text)))
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1)
    return "{0}.{1}".format(line, column)


def char_offset(text, index):
    """Return the character offset of a ``"line.column"`` index (clamped to the text)."""
    match = _INDEX.match(str(index))
    if not match:
        raise ValueError("Not a line.column index: {0!r}".format(index))
    line = int(match.group(1))
    column = int(match.group(2))
    if line < 1:
        return 0
    position = 0
    for _ in range(line - 1):
        newline = text.find("\n", position)
        if newline == -1:
            return len(text)
        position = newline + 1
    line_end = text.find("\n", position)
    if line_end == -1:
        line_end = len(text)
    return min(position + max(0, column), line_end)


def normalise_span(text, start, end):
    """Return ``(start, end)`` ordered and clamped to ``text``; ``None`` when empty."""
    start, end = sorted((max(0, min(int(start), len(text))), max(0, min(int(end), len(text)))))
    if start == end:
        return None
    return start, end


def describe_span(span):
    """Human-readable form used in status lines and the scratchpad ("chars 12–48")."""
    return "chars {0}–{1}".format(span[0], span[1])
