"""Structured sentence and hunk review interface."""

import tkinter as tk
from tkinter import ttk

from core.models import ACCEPTED, PENDING, REJECTED


DECISION_LABELS = {
    ACCEPTED: "Accepted",
    REJECTED: "Rejected",
    PENDING: "Pending",
    "mixed": "Partially accepted",
}

DECISION_COLORS = {
    ACCEPTED: "#17823b",
    REJECTED: "#c62828",
    PENDING: "#1565c0",
    "mixed": "#d97706",
}


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
        style = ttk.Style(self)
        for decision, color in DECISION_COLORS.items():
            style.configure(
                "{0}.ReviewDecision.TLabel".format(decision.title()),
                foreground=color,
                font=("Segoe UI", 10, "bold"),
            )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.counter_var = tk.StringVar(value="No suggestions")
        ttk.Label(
            header, textvariable=self.counter_var, font=("Segoe UI", 11, "bold")
        ).pack(side=tk.LEFT)
        self.decision_var = tk.StringVar(value="")
        self.decision_label = ttk.Label(
            header,
            textvariable=self.decision_var,
            style="Pending.ReviewDecision.TLabel",
        )
        self.decision_label.pack(side=tk.LEFT, padx=12)
        self.scope_var = tk.StringVar(value="")
        self.scope_label = ttk.Label(header, textvariable=self.scope_var, foreground="#6b7280")
        self.scope_label.pack(side=tk.LEFT, padx=(0, 12))

        self.context_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            header,
            text="Show unchanged context",
            variable=self.context_var,
            command=self._toggle_context,
        ).pack(side=tk.RIGHT)

        self.context_frame = ttk.LabelFrame(self, text="Original context")
        self.context_text = tk.Text(
            self.context_frame,
            height=3,
            wrap=tk.WORD,
            relief=tk.FLAT,
            background="#f3f4f6",
            font=("Segoe UI", 9),
        )
        self.context_text.pack(fill=tk.X, padx=5, pady=4)
        self.context_text.configure(state=tk.DISABLED)

        self.comparison = ttk.Panedwindow(self, orient=tk.VERTICAL)
        self.comparison.grid(row=2, column=0, sticky="nsew")

        original_frame = ttk.LabelFrame(
            self.comparison, text="Original — struck text will be removed"
        )
        proposed_frame = ttk.LabelFrame(
            self.comparison, text="Suggestion — underlined text will be added"
        )
        self.comparison.add(original_frame, weight=1)
        self.comparison.add(proposed_frame, weight=1)

        self.original_text = tk.Text(
            original_frame,
            height=6,
            wrap=tk.WORD,
            font=("Segoe UI", 11),
            padx=6,
            pady=6,
        )
        self.original_text.pack(fill=tk.BOTH, expand=True)
        self.proposed_text = tk.Text(
            proposed_frame,
            height=6,
            wrap=tk.WORD,
            font=("Segoe UI", 11),
            padx=6,
            pady=6,
        )
        self.proposed_text.pack(fill=tk.BOTH, expand=True)
        self.explanation_var = tk.StringVar(value="")
        self.explanation_label = ttk.Label(
            proposed_frame, textvariable=self.explanation_var, foreground="#6b7280",
            font=("Segoe UI", 9), wraplength=900, justify=tk.LEFT,
        )

        self.original_text.tag_configure(
            "removed", foreground="#8b1a1a", background="#ffe5e5", overstrike=True
        )
        self.proposed_text.tag_configure(
            "added", foreground="#145a32", background="#e2f5e9", underline=True
        )
        self.original_text.configure(state=tk.DISABLED)
        self.proposed_text.configure(state=tk.DISABLED)

        decision_row = ttk.Frame(self)
        decision_row.grid(row=3, column=0, sticky="ew", pady=6)
        self.accept_item_button = tk.Button(
            decision_row,
            text="Accept sentence",
            command=self.accept_current,
            foreground=DECISION_COLORS[ACCEPTED],
            activeforeground=DECISION_COLORS[ACCEPTED],
        )
        self.accept_item_button.pack(side=tk.LEFT)
        self.reject_item_button = tk.Button(
            decision_row,
            text="Reject sentence",
            command=self.reject_current,
            foreground=DECISION_COLORS[REJECTED],
            activeforeground=DECISION_COLORS[REJECTED],
        )
        self.reject_item_button.pack(side=tk.LEFT, padx=5)

        self.details_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            decision_row,
            text="Review individual changes",
            variable=self.details_var,
            command=self._toggle_details,
        ).pack(side=tk.RIGHT)

        self.details_frame = ttk.LabelFrame(self, text="Individual change groups")
        self.hunk_tree = ttk.Treeview(
            self.details_frame,
            columns=("change", "decision"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        self.hunk_tree.heading("change", text="Original → Suggestion")
        self.hunk_tree.heading("decision", text="Decision")
        self.hunk_tree.column("change", width=520, stretch=True)
        self.hunk_tree.column("decision", width=110, stretch=False)
        self.hunk_tree.tag_configure("explanation", foreground="#6b7280", font=("Segoe UI", 9))
        self.hunk_tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=4)
        hunk_actions = ttk.Frame(self.details_frame)
        hunk_actions.pack(fill=tk.X, padx=5, pady=(0, 5))
        ttk.Button(
            hunk_actions, text="Accept selected change", command=self.accept_hunk
        ).pack(side=tk.LEFT)
        ttk.Button(
            hunk_actions, text="Reject selected change", command=self.reject_hunk
        ).pack(side=tk.LEFT, padx=5)

        self.navigation = ttk.Frame(self)
        self.navigation.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        self.previous_button = ttk.Button(
            self.navigation, text="Previous", command=self.previous
        )
        self.previous_button.pack(side=tk.LEFT)
        self.next_button = ttk.Button(self.navigation, text="Next", command=self.next)
        self.next_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(self.navigation, text="Accept all", command=self.accept_all).pack(
            side=tk.LEFT, padx=(14, 5)
        )
        ttk.Button(self.navigation, text="Reject all", command=self.reject_all).pack(
            side=tk.LEFT
        )

        self.apply_button = ttk.Button(
            self.navigation, text="Apply reviewed changes", command=self._apply
        )
        self.apply_button.pack(side=tk.RIGHT)
        ttk.Button(self.navigation, text="Back to editor", command=self._back).pack(
            side=tk.RIGHT, padx=5
        )

    def set_session(self, session):
        """Display a new editing session."""
        self.session = session
        self.current_index = 0
        self.scope_var.set("Reviewing the selected passage only" if session.selection else "")
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
        context = "{0}[ current suggestion ]{1}".format(before, after)
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
                values=(self._display_change(hunk), DECISION_LABELS[hunk.decision]),
                open=True,
            )
            if hunk.explanation:
                # a grey line under the hunk; selecting it counts as selecting the hunk
                self.hunk_tree.insert(
                    iid, tk.END, iid="e{0}".format(index), values=("      \u21b3 " + hunk.explanation, ""),
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
            self.explanation_var.set("Why: " + explanations[0])
        else:
            self.explanation_var.set("Why:\n" + "\n".join("\u2022 " + text for text in explanations))
        self.explanation_label.pack(fill=tk.X, padx=6, pady=(0, 6))

    def _refresh(self):
        item = self._current_item()
        if item is None:
            return
        total = len(self.session.review_items)
        self.counter_var.set(
            "Suggestion {0} of {1}".format(self.current_index + 1, total)
        )
        decision = item.decision
        self.decision_var.set(DECISION_LABELS.get(decision, decision.title()))
        self.decision_label.configure(
            style="{0}.ReviewDecision.TLabel".format(decision.title())
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
                "{0} change group(s) still need a decision.".format(pending)
            )
        else:
            self.on_status("All suggestions reviewed. Ready to apply.")

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
            self.on_status("Select an individual change first.")
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
