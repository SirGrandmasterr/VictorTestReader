"""Structured sentence and hunk review interface.

One suggestion at a time: the sentence as a single paragraph with the removed
words struck and the added words underlined (a side-by-side view is a
checkbox away), the model's reasons as a note under it, the individual
change groups as rows with their own decision, and a strip of small bars
that shows where in the review the reader is.
"""

import tkinter as tk
from tkinter import ttk

from core.models import ACCEPTED, PENDING, REJECTED
from .i18n import N_, format_number, tr
from .theme import DECISION_GLYPHS, PALETTE, Tooltip, bind_restyle, font, scaled, style_text


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
CONTEXT_CHARS = 48  # faint text shown on both sides of the suggestion in the unified view


def decision_text(decision):
    """A decision as glyph plus translated word, e.g. "✓ Accepted"."""
    label = DECISION_LABELS.get(decision)
    return "{0} {1}".format(QUICK_DECISION_GLYPHS.get(decision, ""), tr(label) if label else decision.title()).strip()


def hunk_kind_text(kind):
    label = HUNK_KIND_LABELS.get(kind)
    return tr(label) if label else kind


def display_text(text):
    """A hunk's text for a one-line row: paragraph breaks become a pilcrow, nothing becomes a word."""
    if not text:
        return tr("(nothing)")
    return text.replace("\n", " ¶ ")


class StepStrip(tk.Canvas):
    """One small bar per suggestion, coloured by its decision; the current one in the accent colour."""

    BAR = 22
    GAP = 5
    HEIGHT = 8

    def __init__(self, parent, on_jump):
        super().__init__(parent, height=self.HEIGHT, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.on_jump = on_jump
        self.items = []  # decisions, current index
        self.current = 0
        self.bind("<Configure>", lambda event: self.redraw())
        self.bind("<Button-1>", self._clicked)
        bind_restyle(self, self.redraw)

    def show(self, decisions, current):
        self.items = list(decisions)
        self.current = current
        self.redraw()

    def _bar_width(self):
        count = max(1, len(self.items))
        available = max(40, self.winfo_width())
        return max(4, min(scaled(self.BAR), (available - (count - 1) * self.GAP) // count))

    def redraw(self):
        self.configure(background=PALETTE["bg"], height=scaled(self.HEIGHT))
        self.delete("all")
        if not self.items:
            return
        width = self._bar_width()
        colours = {ACCEPTED: PALETTE["success"], REJECTED: PALETTE["danger"], "mixed": PALETTE["warning"]}
        height = scaled(self.HEIGHT)
        for index, decision in enumerate(self.items):
            x = index * (width + self.GAP)
            colour = PALETTE["accent"] if index == self.current else colours.get(decision, PALETTE["border_strong"])
            self.create_rectangle(x, 1, x + width, height - 1, fill=colour, outline=colour)
        self.configure(width=len(self.items) * (width + self.GAP) - self.GAP)

    def _clicked(self, event):
        if not self.items:
            return
        index = int(event.x // (self._bar_width() + self.GAP))
        if 0 <= index < len(self.items):
            self.on_jump(index)


class HunkRow(ttk.Frame):
    """One change group of the current suggestion: mark, the change in serif, its kind and decision."""

    def __init__(self, parent, index, hunk, on_select):
        super().__init__(parent, style="Surface.TFrame", padding=(10, 5), takefocus=1)
        self.index = index
        self.hunk = hunk
        self.on_select = on_select
        self.mark = ttk.Label(self, text="", style="Surface.TLabel", width=2)
        self.mark.pack(side=tk.LEFT)
        self.text = tk.Text(self, height=1, cursor="arrow", takefocus=0)
        style_text(self.text, size=11, serif=True)
        self.text.configure(padx=2, pady=0, highlightthickness=0, spacing1=0, spacing3=0, wrap=tk.NONE)
        self.text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.decision = ttk.Label(self, text="", style="Pending.State.TLabel")
        self.decision.pack(side=tk.RIGHT)
        self.kind = ttk.Label(self, text=hunk_kind_text(hunk.kind), style="SurfaceMuted.TLabel")
        self.kind.pack(side=tk.RIGHT, padx=(0, 10))
        for widget in (self, self.mark, self.text, self.decision, self.kind):
            widget.bind("<Button-1>", self._clicked, add="+")
        self.bind("<FocusIn>", self._clicked, add="+")
        self.refresh(selected=False)

    def _clicked(self, event=None):
        self.on_select(self.index)
        return "break"

    def refresh(self, selected):
        hunk = self.hunk
        surface = PALETTE["selection"] if selected else PALETTE["surface"]
        self.configure(style="Selected.TFrame" if selected else "Surface.TFrame")
        self.mark.configure(text=QUICK_DECISION_GLYPHS.get(hunk.decision, ""),
                            style="Selected.TLabel" if selected else "Surface.TLabel")
        self.kind.configure(style="SelectedMuted.TLabel" if selected else "SurfaceMuted.TLabel")
        self.decision.configure(text=decision_text(hunk.decision),
                                style="{0}.State.TLabel".format(DECISION_STYLES.get(hunk.decision, "Pending")))
        self.text.configure(state=tk.NORMAL, background=surface)
        self.text.delete("1.0", tk.END)
        self.text.tag_configure("removed", foreground=PALETTE["danger"], overstrike=True)
        self.text.tag_configure("added", foreground=PALETTE["success"], underline=True)
        self.text.tag_configure("arrow", foreground=PALETTE["faint"], font=font(10))
        self.text.tag_configure("faint", foreground=PALETTE["faint"], font=font(10, slant="italic"))
        if hunk.original_text:
            self.text.insert(tk.END, display_text(hunk.original_text), "removed")
        else:
            self.text.insert(tk.END, display_text(""), "faint")
        self.text.insert(tk.END, "  →  ", "arrow")
        if hunk.proposed_text:
            self.text.insert(tk.END, display_text(hunk.proposed_text), "added")
        else:
            self.text.insert(tk.END, display_text(""), "faint")
        self.text.configure(state=tk.DISABLED)


class ReviewPanel(ttk.Frame):
    """Display and update one EditSession."""

    def __init__(self, parent, on_apply, on_back, on_status):
        super().__init__(parent, padding=(16, 10, 16, 4))
        self.on_apply = on_apply
        self.on_back = on_back
        self.on_status = on_status
        self.session = None
        self.current_index = 0
        self.selected_hunk = None  # index into item.hunks
        self.rows = []
        self._build_widgets()

    def _build_widgets(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.counter_var = tk.StringVar(value=tr("No suggestions"))
        ttk.Label(header, textvariable=self.counter_var, style="Title.TLabel").pack(side=tk.LEFT)
        self.steps = StepStrip(header, on_jump=self.jump_to)
        self.steps.pack(side=tk.LEFT, padx=(14, 12), pady=(8, 0))
        Tooltip(self.steps, tr("One bar per suggestion: green accepted, red rejected, grey still open. Click to jump."))
        self.decision_var = tk.StringVar(value="")
        self.decision_label = ttk.Label(header, textvariable=self.decision_var, style="Pending.State.TLabel")
        self.decision_label.pack(side=tk.LEFT, pady=(6, 0))
        self.scope_var = tk.StringVar(value="")
        self.scope_label = ttk.Label(header, textvariable=self.scope_var, style="Muted.TLabel")
        self.scope_label.pack(side=tk.LEFT, padx=(12, 0), pady=(6, 0))
        self.context_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text=tr("Show surrounding text"), variable=self.context_var,
                        command=self._toggle_context).pack(side=tk.RIGHT, pady=(6, 0))
        self.side_by_side_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text=tr("Side by side"), variable=self.side_by_side_var,
                        command=self._toggle_layout).pack(side=tk.RIGHT, padx=(0, 14), pady=(6, 0))

        legend = ttk.Frame(self)
        legend.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(legend, text=tr("Reading the diff:"), style="SmallMuted.TLabel").pack(side=tk.LEFT)
        self.legend_text = tk.Text(legend, height=1, cursor="arrow", takefocus=0)
        self.legend_text.pack(side=tk.LEFT, padx=(8, 0))
        self.request_var = tk.StringVar(value="")
        ttk.Label(legend, textvariable=self.request_var, style="SmallMuted.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        self.context_frame = ttk.Frame(self, style="Card.TFrame", padding=1)
        self.context_text = tk.Text(self.context_frame, height=3)
        self.context_text.pack(fill=tk.X)

        # unified view: one sheet with the suggestion as a paragraph and the reasons as a note
        self.sheet = tk.Text(self, height=6)
        self.sheet.grid(row=3, column=0, sticky="nsew")

        # side-by-side view (hidden until asked for)
        self.comparison = ttk.Panedwindow(self, orient=tk.VERTICAL)
        original_frame = ttk.LabelFrame(self.comparison, text=tr("Original — struck text will be removed"))
        proposed_frame = ttk.LabelFrame(self.comparison, text=tr("Suggestion — underlined text will be added"))
        self.comparison.add(original_frame, weight=1)
        self.comparison.add(proposed_frame, weight=1)
        self.original_text = tk.Text(original_frame, height=5)
        self.original_text.pack(fill=tk.BOTH, expand=True)
        self.proposed_text = tk.Text(proposed_frame, height=5)
        self.proposed_text.pack(fill=tk.BOTH, expand=True)

        decision_row = ttk.Frame(self)
        decision_row.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.reject_item_button = ttk.Button(decision_row, text=tr("Reject sentence"), command=self.reject_current,
                                             style="Danger.TButton")
        self.reject_item_button.pack(side=tk.LEFT)
        ttk.Label(decision_row, text="Alt+R", style="Kbd.TLabel").pack(side=tk.LEFT, padx=(6, 14))
        self.accept_item_button = ttk.Button(decision_row, text=tr("Accept sentence"), command=self.accept_current,
                                             style="Fill.Success.TButton")
        self.accept_item_button.pack(side=tk.LEFT)
        ttk.Label(decision_row, text="Alt+A", style="Kbd.TLabel").pack(side=tk.LEFT, padx=(6, 0))
        self.details_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(decision_row, text=tr("Review individual changes"), variable=self.details_var,
                        command=self._toggle_details).pack(side=tk.RIGHT)

        self.details_frame = ttk.Frame(self, style="Card.TFrame", padding=1)
        heading = ttk.Frame(self.details_frame, style="Surface.TFrame", padding=(10, 5))
        heading.pack(fill=tk.X)
        ttk.Label(heading, text=tr("Individual changes in this sentence group"), style="SurfaceMuted.TLabel").pack(side=tk.LEFT)
        ttk.Label(heading, text=tr("Alt+↑/↓ select"), style="Surface.Kbd.TLabel").pack(side=tk.RIGHT)
        self.rows_frame = ttk.Frame(self.details_frame, style="Surface.TFrame")
        self.rows_frame.pack(fill=tk.X)
        hunk_actions = ttk.Frame(self.details_frame, style="Surface.TFrame", padding=(10, 6))
        hunk_actions.pack(fill=tk.X)
        ttk.Button(hunk_actions, text=tr("Reject selected change"), style="Small.Danger.TButton",
                   command=self.reject_hunk).pack(side=tk.LEFT)
        ttk.Button(hunk_actions, text=tr("Accept selected change"), style="Small.Success.TButton",
                   command=self.accept_hunk).pack(side=tk.LEFT, padx=(6, 0))
        self.details_frame.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        self.navigation = ttk.Frame(self)
        self.navigation.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self.previous_button = ttk.Button(self.navigation, text="‹ " + tr("Previous"), style="Ghost.TButton",
                                          command=self.previous)
        self.previous_button.pack(side=tk.LEFT)
        self.next_button = ttk.Button(self.navigation, text=tr("Next") + " ›", style="Ghost.TButton",
                                      command=self.next)
        self.next_button.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(self.navigation, text="Alt+←/→", style="Kbd.TLabel").pack(side=tk.LEFT, padx=(6, 14))
        ttk.Button(self.navigation, text=tr("Accept all"), style="Ghost.TButton", command=self.accept_all).pack(side=tk.LEFT)
        ttk.Button(self.navigation, text=tr("Reject all"), style="Ghost.TButton", command=self.reject_all).pack(
            side=tk.LEFT, padx=(4, 0))
        self.apply_button = ttk.Button(self.navigation, text=tr("Apply reviewed changes"), style="Primary.TButton",
                                       command=self._apply)
        self.apply_button.pack(side=tk.RIGHT)
        ttk.Button(self.navigation, text=tr("Back to text"), command=self._back).pack(side=tk.RIGHT, padx=(0, 8))
        self._style_texts()
        bind_restyle(self, self._style_texts)

    def _style_texts(self):
        """Palette colours and scalable fonts for the text areas (called again after a theme change)."""
        style_text(self.sheet, size=12, readonly=True, serif=True)
        self.sheet.configure(spacing3=4)
        self.sheet.tag_configure("context", foreground=PALETTE["faint"])
        self.sheet.tag_configure("removed", foreground=PALETTE["danger"], background=PALETTE["danger_soft"], overstrike=True)
        self.sheet.tag_configure("added", foreground=PALETTE["success"], background=PALETTE["success_soft"], underline=True)
        self.sheet.tag_configure("why", foreground=PALETTE["text"], background=PALETTE["warning_soft"], font=font(10),
                                 lmargin1=scaled(12), lmargin2=scaled(12), rmargin=scaled(12), spacing1=6, spacing3=6)
        self.sheet.tag_configure("why_label", foreground=PALETTE["warning"], background=PALETTE["warning_soft"],
                                 font=font(10, "bold"), lmargin1=scaled(12), lmargin2=scaled(12), spacing1=6)
        self.sheet.tag_configure("gap", font=font(4))
        style_text(self.legend_text, size=10, readonly=True, serif=True, background=PALETTE["bg"])
        self.legend_text.configure(padx=0, pady=0, highlightthickness=0, spacing1=0, spacing3=0)
        self.legend_text.tag_configure("removed", foreground=PALETTE["danger"], background=PALETTE["danger_soft"], overstrike=True)
        self.legend_text.tag_configure("added", foreground=PALETTE["success"], background=PALETTE["success_soft"], underline=True)
        self.legend_text.tag_configure("muted", foreground=PALETTE["muted"], font=font(9))
        self.legend_text.configure(state=tk.NORMAL)
        self.legend_text.delete("1.0", tk.END)
        self.legend_text.insert(tk.END, tr("struck"), "removed")
        self.legend_text.insert(tk.END, " " + tr("will be removed") + "    ", "muted")
        self.legend_text.insert(tk.END, tr("underlined"), "added")
        self.legend_text.insert(tk.END, " " + tr("will be added"), "muted")
        self.legend_text.configure(state=tk.DISABLED, width=max(20, len(self.legend_text.get("1.0", "end-1c"))))
        style_text(self.context_text, size=10, readonly=True, serif=True, background=PALETTE["surface_alt"])
        self.context_text.configure(highlightthickness=0, padx=12, pady=6)
        self.context_text.tag_configure("marker", foreground=PALETTE["accent"], font=font(9, "bold"))
        for widget in (self.original_text, self.proposed_text):
            style_text(widget, size=12, readonly=True, serif=True)
            widget.configure(padx=14, pady=10)
        self.original_text.tag_configure("removed", foreground=PALETTE["danger"], background=PALETTE["danger_soft"],
                                         overstrike=True)
        self.proposed_text.tag_configure("added", foreground=PALETTE["success"], background=PALETTE["success_soft"],
                                         underline=True)
        for row in self.rows:
            row.refresh(selected=row.index == self.selected_hunk)

    def set_session(self, session):
        """Display a new editing session."""
        self.session = session
        self.current_index = 0
        self.selected_hunk = None
        self.scope_var.set(tr("Reviewing the selected passage only") if session.selection else "")
        self.request_var.set("· {0} · {1}".format(
            " › ".join(step.name for step in session.steps) if session.steps else session.instruction[:40], session.model))
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

    def _surroundings(self, item, radius):
        """The text before and after the suggestion in the edited text."""
        text = self.session.context_text
        start = self.session.context_offset + item.original_start
        end = self.session.context_offset + item.original_end
        before = text[max(0, start - radius):start]
        after = text[end:min(len(text), end + radius)]
        if start - radius > 0:
            before = "…" + before.lstrip()
        if end + radius < len(text):
            after = after.rstrip() + "…"
        return before, after

    def _render_sheet(self, item):
        before, after = self._surroundings(item, CONTEXT_CHARS)
        explanations = []
        for hunk in item.changed_hunks:
            if hunk.explanation and hunk.explanation not in explanations:
                explanations.append(hunk.explanation)

        def insert():
            self.sheet.insert(tk.END, before.replace("\n", " "), "context")
            for hunk in item.hunks:
                if hunk.kind == "equal":
                    self.sheet.insert(tk.END, hunk.original_text)
                    continue
                if hunk.original_text:
                    self.sheet.insert(tk.END, hunk.original_text, "removed")
                if hunk.proposed_text:
                    if hunk.original_text:
                        self.sheet.insert(tk.END, " ")
                    self.sheet.insert(tk.END, hunk.proposed_text, "added")
            self.sheet.insert(tk.END, after.replace("\n", " "), "context")
            if explanations:
                self.sheet.insert(tk.END, "\n\n", "gap")
                self.sheet.insert(tk.END, tr("Why") + "  ", "why_label")
                if len(explanations) == 1:
                    self.sheet.insert(tk.END, explanations[0] + " ", "why")
                else:
                    self.sheet.insert(tk.END, "\n".join("• " + text for text in explanations) + " ", "why")

        self._set_text(self.sheet, insert)

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
        before, after = self._surroundings(item, 180)

        def insert():
            self.context_text.insert(tk.END, before)
            self.context_text.insert(tk.END, tr("[ current suggestion ]"), "marker")
            self.context_text.insert(tk.END, after)

        self._set_text(self.context_text, insert)

    def _render_hunks(self, item):
        for row in self.rows:
            row.destroy()
        self.rows = []
        changed = [(index, hunk) for index, hunk in enumerate(item.hunks) if hunk.is_change]
        if self.selected_hunk is None or self.selected_hunk >= len(item.hunks) or not item.hunks[self.selected_hunk].is_change:
            pending = next((index for index, hunk in changed if hunk.decision == PENDING), None)
            self.selected_hunk = pending if pending is not None else (changed[0][0] if changed else None)
        for index, hunk in changed:
            row = HunkRow(self.rows_frame, index, hunk, on_select=self._select_hunk)
            row.pack(fill=tk.X)
            row.refresh(selected=index == self.selected_hunk)
            self.rows.append(row)

    def _select_hunk(self, index):
        self.selected_hunk = index
        for row in self.rows:
            row.refresh(selected=row.index == index)

    def _refresh(self):
        item = self._current_item()
        if item is None:
            return
        total = len(self.session.review_items)
        self.counter_var.set(tr("Suggestion {number} of {total}", number=self.current_index + 1, total=total))
        self.steps.show([entry.decision for entry in self.session.review_items], self.current_index)
        decision = item.decision
        self.decision_var.set(decision_text(decision))
        self.decision_label.configure(style="{0}.State.TLabel".format(DECISION_STYLES.get(decision, "Pending")))
        self._render_sheet(item)
        self._render_comparison(item)
        self._render_hunks(item)
        self._render_context(item)
        self.previous_button.configure(state=tk.NORMAL if self.current_index > 0 else tk.DISABLED)
        self.next_button.configure(state=tk.NORMAL if self.current_index < total - 1 else tk.DISABLED)
        pending = self.session.pending_count
        changes = sum(len(entry.changed_hunks) for entry in self.session.review_items)
        self.apply_button.configure(state=tk.NORMAL if pending == 0 else tk.DISABLED,
                                    text=tr("Apply {count} decision(s)", count=format_number(changes)))
        if pending:
            self.on_status(tr("{pending} change group(s) still need a decision.", pending=pending))
        else:
            self.on_status(tr("All suggestions reviewed. Ready to apply."))

    def _toggle_context(self):
        if self.context_var.get():
            self.context_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        else:
            self.context_frame.grid_remove()

    def _toggle_layout(self):
        if self.side_by_side_var.get():
            self.sheet.grid_remove()
            self.comparison.grid(row=3, column=0, sticky="nsew")
        else:
            self.comparison.grid_remove()
            self.sheet.grid()

    def _toggle_details(self):
        if self.details_var.get():
            self.details_frame.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        else:
            self.details_frame.grid_remove()

    def jump_to(self, index):
        if self.session and 0 <= index < len(self.session.review_items) and index != self.current_index:
            self.current_index = index
            self.selected_hunk = None
            self._refresh()

    def previous(self, event=None):
        if self.current_index > 0:
            self.current_index -= 1
            self.selected_hunk = None
            self._refresh()
        return "break" if event else None

    def next(self, event=None):
        if self.session and self.current_index < len(self.session.review_items) - 1:
            self.current_index += 1
            self.selected_hunk = None
            self._refresh()
        return "break" if event else None

    def select_previous_hunk(self, event=None):
        return self._step_hunk(-1, event)

    def select_next_hunk(self, event=None):
        return self._step_hunk(1, event)

    def _step_hunk(self, delta, event):
        indexes = [row.index for row in self.rows]
        if indexes:
            position = indexes.index(self.selected_hunk) if self.selected_hunk in indexes else -1
            self._select_hunk(indexes[max(0, min(len(indexes) - 1, position + delta))])
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
        if not item or self.selected_hunk is None or self.selected_hunk >= len(item.hunks):
            self.on_status(tr("Select an individual change first."))
            return None
        return item.hunks[self.selected_hunk]

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
