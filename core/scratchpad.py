"""Markdown audit logging for editing sessions."""

from datetime import datetime
from pathlib import Path

from .text_positions import describe_span


class ScratchpadLogger:
    """Write proposal and decision records for one app run."""

    def __init__(self, directory=None, now_provider=None):
        self.directory = Path(directory or Path.cwd())
        self.now_provider = now_provider or datetime.now
        self.path = None

    def _ensure_path(self):
        if self.path is None:
            timestamp = self.now_provider().strftime("%Y%m%d_%H%M%S")
            self.path = self.directory / (
                "TextEnhanceAI-scratchpad_{0}.md".format(timestamp)
            )
        return self.path

    @staticmethod
    def _block(text):
        return "~~~text\n{0}\n~~~\n".format(text)

    def log_proposal(self, session):
        """Create the scratchpad after a successful proposal."""
        path = self._ensure_path()
        lines = [
            "# TextEnhanceAI editing session\n",
            "- Model: `{0}`\n".format(session.model),
            "- Instruction: {0}\n".format(session.instruction),
        ]
        if session.selection:
            lines.append("- Selection: {0} of {1} characters\n".format(
                describe_span(session.selection), len(session.full_text)
            ))
        lines += [
            "- Suggestions: {0}\n\n".format(len(session.review_items)),
            "## Original text\n\n",
            self._block(session.original_text),
        ]
        if len(session.steps) > 1:
            # a chain: every intermediate output is kept, the last one is the proposal
            lines.append("\n## Steps\n\n")
            for number, step in enumerate(session.steps, 1):
                lines.extend([
                    "### Step {0}: {1}\n\n".format(number, step.name),
                    "- Instruction: {0}\n\n".format(step.instruction),
                    self._block(step.output),
                    "\n",
                ])
        lines += [
            "\n## Proposed text\n\n",
            self._block(session.proposed_text),
            "\n## Suggestions\n\n",
        ]
        for item in session.review_items:
            lines.extend(
                [
                    "### Suggestion {0}\n\n".format(item.item_id),
                    "Original:\n\n",
                    self._block(item.original_text),
                    "Proposed:\n\n",
                    self._block(item.proposed_text),
                ]
            )
            for index, hunk in enumerate(item.changed_hunks, 1):
                if hunk.explanation:
                    lines.append("- Change {0}: `{1}` \u2192 `{2}` \u2014 {3}\n".format(
                        index, hunk.original_text.replace("`", "'"), hunk.proposed_text.replace("`", "'"),
                        hunk.explanation,
                    ))
            lines.append("\n")
        if path.exists():
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n---\n\n")
                handle.write("".join(lines))
        else:
            path.write_text("".join(lines), encoding="utf-8")
        return path

    def log_outcome(self, session, outcome, final_text=None):
        """Append final decisions and the resulting text."""
        path = self._ensure_path()
        lines = [
            "\n## Review outcome\n\n",
            "- Outcome: **{0}**\n".format(outcome),
        ]
        for item in session.review_items:
            lines.append(
                "- Suggestion {0}: {1}\n".format(item.item_id, item.decision)
            )
            for index, hunk in enumerate(item.changed_hunks, 1):
                lines.append(
                    "  - Change {0}: {1} — `{2}` → `{3}`\n".format(
                        index,
                        hunk.decision,
                        hunk.original_text.replace("`", "'"),
                        hunk.proposed_text.replace("`", "'"),
                    )
                )
        if final_text is not None:
            lines.extend(["\n## Final text\n\n", self._block(final_text)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))
        return path
