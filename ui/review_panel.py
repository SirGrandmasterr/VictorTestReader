"""Structured sentence and hunk review interface."""

import tkinter as tk
from tkinter import ttk

from core.models import ACCEPTED, PENDING, REJECTED
from .i18n import N_, tr
from .theme import DECISION_GLYPHS, PALETTE, bind_restyle, font, style_text


DECISION_LABELS = {  # translated at render time, see decision_text()
    ACCEPTED: N_("Accepted"),
    REJECTED: N_("Rejected"),
    PENDING: N_("Pending"),
    "mixed": N_("Partially accepted"),
}
# The quick review's decisions map onto the review glyphs (never colour alone).
QUICK_DECISION_GLYPHS = {
    ACCEPTED: DECISION_GLYPHS["applied"],
    REJECTED: DECISION_GLYPHS["rejected"],
    PENDING: DECISION_GLYPHS["pending"],
    "mixed": DECISION_GLYPHS["edited"],
}
# ttk style prefix per decision (styles are configured by theme.apply_theme)
DECISION_STYLES = {ACCEPTED: "Applied", REJECTED: "Rejected", PENDING: "Pending", "mixed": "Mixed"}
HUNK_KIND_LABELS = {"delete": N_("removed"), "insert": N_("added"), "replace": N_("replaced")}


def decision_text(decision):
    """A decision as glyph plus translated word, e.g. "\u2713 Accepted"."""
    label = DECISION_LABELS.get(decision)
    return "{0} {1}".format(QUICK_DECISION_GLYPHS.get(decision, ""), tr(label) if label else decision.title()).strip()


def hunk_kind_text(kind):
    label = HUNK_KIND_LABELS.get(kind)
    return tr(label) if label else kind


class ReviewPanel(ttk.Frame):
    """Display and update one EditSession."""

    def __init__(self, parent, on_apply, on_back, on_status):
        super().__init__(parent, padding=8)
        self.on_apply = on_apply
        self.on_back = on_back
        self.on_status = on_status
        self.session = None
        self.current_index = 0
        self._build_widgets()

    def _build_widgets(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.counter_var = tk.StringVar(value=tr("No suggestions"))
        ttk.Label(header, textvariable=self.counter_var, style="Heading.TLabel").pack(side=tk.LEFT)
        self.decision_var = tk.StringVar(value="")
        self.decision_label = ttk.Label(
            header,
            textvariable=self.decision_var,
            style="Pending.ReviewDecision.TLabel",
        )
        self.decision_label.pack(side=tk.LEFT, padx=12)
        self.scope_var = tk.StringVar(value="")
        self.scope_label = ttk.Label(header, textvariable=self.scope_var, style="Muted.TLabel")
        self.scope_label.pack(side=tk.LEFT, padx=(0, 12))

        self.context_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            header,
            text=tr("Show unchanged context"),
            variable=self.context_var,
            command=self._toggle_context,
        ).pack(side=tk.RIGHT)

        self.context_frame = ttk.LabelFrame(self, text=tr("Original context"))
        self.context_text = tk.Text(self.context_frame, height=3)
        self.context_text.pack(fill=tk.X, padx=5, pady=4)

        self.comparison = ttk.Panedwindow(self, orient=tk.VERTICAL)
        self.comparison.grid(row=2, column=0, sticky="nsew")

        original_frame = ttk.LabelFrame(
            self.comparison, text=tr("Original — struck text will be removed")
        )
        proposed_frame = ttk.LabelFrame(
            self.comparison, text=tr("Suggestion — underlined text will be added")
        )
        self.comparison.add(original_frame, weight=1)
        self.comparison.add(proposed_frame, weight=1)

        self.original_text = tk.Text(original_frame, height=6)
        self.original_text.pack(fill=tk.BOTH, expand=True)
        self.proposed_text = tk.Text(proposed_frame, height=6)
        self.proposed_text.pack(fill=tk.BOTH, expand=True)
        self.explanation_var = tk.StringVar(value="")
        self.explanation_label = ttk.Label(
            proposed_frame, textvariable=self.explanation_var, style="SmallMuted.TLabel",
            wraplength=900, justify=tk.LEFT,
        )
        decision_row = ttk.Frame(self)
        decision_row.grid(row=3, column=0, sticky="ew", pady=6)
        self.accept_item_button = ttk.Button(
            decision_row, text=tr("Accept sentence"), command=self.accept_current, style="Success.TButton",
        )
        self.accept_item_button.pack(side=tk.LEFT)
        self.reject_item_button = ttk.Button(
            decision_row, text=tr("Reject sentence"), command=self.reject_current, style="Danger.TButton",
        )
        self.reject_item_button.pack(side=tk.LEFT, padx=5)

        self.details_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            decision_row,
            text=tr("Review individual changes"),
            variable=self.details_var,
            command=self._toggle_details,
        ).pack(side=tk.RIGHT)

        self.details_frame = ttk.LabelFrame(self, text=tr("Individual change groups"))
        self.hunk_tree = ttk.Treeview(
            self.details_frame,
            columns=("kind", "change", "decision"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        self.hunk_tree.heading("kind", text=tr("Edit"), anchor="w")
        self.hunk_tree.heading("change", text=tr("Original → Suggestion"), anchor="w")
        self.hunk_tree.heading("decision", text=tr("Decision"), anchor="w")
        self.hunk_tree.column("kind", width=90, stretch=False)
        self.hunk_tree.column("change", width=460, stretch=True)
        self.hunk_tree.column("decision", width=130, stretch=False)
        self.hunk_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=4)
        hunk_actions = ttk.Frame(self.details_frame)
        hunk_actions.pack(fill=tk.X, padx=5, pady=(0, 5))
        ttk.Button(
            hunk_actions, text=tr("Accept selected change"), command=self.accept_hunk
        ).pack(side=tk.LEFT)
        ttk.Button(
            hunk_actions, text=tr("Reject selected change"), command=self.reject_hunk
        ).pack(side=tk.LEFT, padx=5)

        self.navigation = ttk.Frame(self)
        self.navigation.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        self.previous_button = ttk.Button(
            self.navigation, text=tr("Previous"), command=self.previous
        )
        self.previous_button.pack(side=tk.LEFT)
        self.next_button = ttk.Button(self.navigation, text=tr("Next"), command=self.next)
        self.next_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(self.navigation, text=tr("Accept all"), command=self.accept_all).pack(
            side=tk.LEFT, padx=(14, 5)
        )
        ttk.Button(self.navigation, text=tr("Reject all"), command=self.reject_all).pack(
            side=tk.LEFT
        )

        self.apply_button = ttk.Button(
            self.navigation, text=tr("Apply reviewed changes"), command=self._apply
        )
        self.apply_button.pack(side=tk.RIGHT)
        ttk.Button(self.navigation, text=tr("Back to editor"), command=self._back).pack(
            side=tk.RIGHT, padx=5
        )
        self._style_texts()
        bind_restyle(self, self._style_texts)

    def _style_texts(self):
        """Palette colours and scalable fonts for the text areas (called again after a theme change)."""
        for widget, size in ((self.context_text, 9), (self.original_text, 11), (self.proposed_text, 11)):
            style_text(widget, size=size, readonly=True)
            widget.configure(padx=6, pady=6)
        self.context_text.configure(background=PALETTE["surface_alt"], highlightthickness=0)
        self.original_text.tag_configure(
            "removed", foreground=PALETTE["danger"], background=PALETTE["danger_soft"], overstrike=True
        )
        self.proposed_text.tag_configure(
            "added", foreground=PALETTE["success"], background=PALETTE["success_soft"], underline=True
        )
        self.hunk_tree.tag_configure("explanation", foreground=PALETTE["muted"], font=font(9))

    def set_session(self, session):
        """Display a new editing session."""
        self.session = session
        self.current_index = 0
        self.scope_var.set(tr("Reviewing the selected passage only") if session.selection else "")
        self._refresh()

    def _current_item(self):
        if not self.session or not self.session.review_items:
            return None
        return self.session.review_items[self.current_index]

    @staticmethod
    def _set_text(widget, insert_callback):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        insert_callback()
        widget.configure(state=tk.DISABLED)

    def _render_comparison(self, item):
        def insert_original():
            for hunk in item.hunks:
                if hunk.original_text:
                    tags = ("removed",) if hunk.kind in ("delete", "replace") else ()
                    self.original_text.insert(tk.END, hunk.original_text, tags)

        def insert_proposed():
            for hunk in item.hunks:
                if hunk.proposed_text:
                    tags = ("added",) if hunk.kind in ("insert", "replace") else ()
                    self.proposed_text.insert(tk.END, hunk.proposed_text, tags)

        self._set_text(self.original_text, insert_original)
        self._set_text(self.proposed_text, insert_proposed)

    def _render_context(self, item):
        if not self.session:
            return
        radius = 180
        # with a selection the context comes from the whole editor text around it
        text = self.session.context_text
        start = self.session.context_offset + item.original_start
        end = self.session.context_offset + item.original_end
        before = text[max(0, start - radius):start]
        after = text[end:min(len(text), end + radius)]
        context = tr("{before}[ current suggestion ]{after}", before=before, after=after)
        self.context_text.configure(state=tk.NORMAL)
        self.context_text.delete("1.0", tk.END)
        self.context_text.insert("1.0", context)
        self.context_text.configure(state=tk.DISABLED)

    @staticmethod
    def _display_change(hunk):
        original = hunk.original_text.replace("\n", "↵") or "∅"
        proposed = hunk.proposed_text.replace("\n", "↵") or "∅"
        value = "{0} → {1}".format(original, proposed)
        return value if len(value) <= 100 else value[:97] + "..."

    def _render_hunks(self, item):
        for row in self.hunk_tree.get_children():
            self.hunk_tree.delete(row)
        for index, hunk in enumerate(item.hunks):
            if not hunk.is_change:
                continue
            iid = "h{0}".format(index)
            self.hunk_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(hunk_kind_text(hunk.kind), self._display_change(hunk), decision_text(hunk.decision)),
                open=True,
            )
            if hunk.explanation:
                # a grey line under the hunk; selecting it counts as selecting the hunk
                self.hunk_tree.insert(
                    iid, tk.END, iid="e{0}".format(index), values=("", "      \u21b3 " + hunk.explanation, ""),
                    tags=("explanation",),
                )

    def _render_explanations(self, item):
        """One grey line per explained change under the suggestion (hidden when there are none)."""
        explanations = []
        for hunk in item.changed_hunks:
            if hunk.explanation and hunk.explanation not in explanations:
                explanations.append(hunk.explanation)
        if not explanations:
            self.explanation_var.set("")
            self.explanation_label.pack_forget()
            return
        if len(explanations) == 1:
            self.explanation_var.set(tr("Why: {reason}", reason=explanations[0]))
        else:
            self.explanation_var.set(tr("Why:\n{reasons}", reasons="\n".join("\u2022 " + text for text in explanations)))
        self.explanation_label.pack(fill=tk.X, padx=6, pady=(0, 6))

    def _refresh(self):
        item = self._current_item()
        if item is None:
            return
        total = len(self.session.review_items)
        self.counter_var.set(tr("Suggestion {number} of {total}", number=self.current_index + 1, total=total))
        decision = item.decision
        self.decision_var.set(decision_text(decision))
        self.decision_label.configure(
            style="{0}.ReviewDecision.TLabel".format(DECISION_STYLES.get(decision, "Pending"))
        )
        self._render_comparison(item)
        self._render_explanations(item)
        self._render_hunks(item)
        self._render_context(item)
        self.previous_button.configure(
            state=tk.NORMAL if self.current_index > 0 else tk.DISABLED
        )
        self.next_button.configure(
            state=tk.NORMAL if self.current_index < total - 1 else tk.DISABLED
        )
        pending = self.session.pending_count
        self.apply_button.configure(state=tk.NORMAL if pending == 0 else tk.DISABLED)
        if pending:
            self.on_status(
                tr("{pending} change group(s) still need a decision.", pending=pending)
            )
        else:
            self.on_status(tr("All suggestions reviewed. Ready to apply."))

    def _toggle_context(self):
        if self.context_var.get():
            self.context_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        else:
            self.context_frame.grid_remove()

    def _toggle_details(self):
        if self.details_var.get():
            self.details_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        else:
            self.details_frame.grid_remove()

    def previous(self, event=None):
        if self.current_index > 0:
            self.current_index -= 1
            self._refresh()
        return "break" if event else None

    def next(self, event=None):
        if self.session and self.current_index < len(self.session.review_items) - 1:
            self.current_index += 1
            self._refresh()
        return "break" if event else None

    def accept_current(self, event=None):
        item = self._current_item()
        if item:
            item.accept()
            self._refresh()
        return "break" if event else None

    def reject_current(self, event=None):
        item = self._current_item()
        if item:
            item.reject()
            self._refresh()
        return "break" if event else None

    def _selected_hunk(self):
        item = self._current_item()
        selection = self.hunk_tree.selection()
        if not item or not selection:
            self.on_status(tr("Select an individual change first."))
            return None
        return item.hunks[int(selection[0][1:])]  # "h3" or its explanation row "e3"

    def accept_hunk(self):
        hunk = self._selected_hunk()
        if hunk:
            hunk.decision = ACCEPTED
            self._refresh()

    def reject_hunk(self):
        hunk = self._selected_hunk()
        if hunk:
            hunk.decision = REJECTED
            self._refresh()

    def accept_all(self):
        if self.session:
            self.session.accept_all()
            self._refresh()

    def reject_all(self):
        if self.session:
            self.session.reject_all()
            self._refresh()

    def _apply(self):
        if self.session and self.session.pending_count == 0:
            self.on_apply(self.session)

    def _back(self):
        if self.session:
            self.on_back(self.session)
