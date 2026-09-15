"""Live view of the model's reasoning: one entry per request, newest first.

``StreamLog`` (Tk-free) collects the ``("stream", key, kind, payload)``
events every backend request publishes (see ``core.workflow.start_stream``)
and keeps the reasoning text of the last requests. ``ThinkingWindow`` shows
the log: a list of requests with their state on the left, the reasoning of
the selected one on the right, appended while it streams. The window follows
the newest request until the user picks another one.
"""

import time
import tkinter as tk
from collections import OrderedDict
from tkinter import ttk

from core.backend import STREAM_ANSWER, STREAM_THINKING
from .i18n import N_, format_number, tr
from .theme import PALETTE, Tooltip, bind_restyle, font, scaled, style_text

MAX_ENTRIES = 40  # requests kept; the oldest finished one is dropped first
MAX_THINKING_CHARS = 200_000  # per request; further reasoning is counted but not kept
FLUSH_INTERVAL_MS = 100  # streamed pieces are batched into the widgets this often

STATE_WAITING = "waiting"
STATE_THINKING = "thinking"
STATE_ANSWERING = "answering"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"
ACTIVE_STATES = (STATE_WAITING, STATE_THINKING, STATE_ANSWERING)
# glyph plus word for every state, never colour alone (see ui.theme)
STATE_LABELS = {
    STATE_WAITING: ("◌", N_("waiting")),
    STATE_THINKING: ("◐", N_("thinking")),
    STATE_ANSWERING: ("◐", N_("answering")),
    STATE_DONE: ("✓", N_("done")),
    STATE_ERROR: ("■", N_("failed")),
    STATE_CANCELLED: ("✗", N_("cancelled")),
}
OUTCOME_STATES = {"done": STATE_DONE, "error": STATE_ERROR, "cancelled": STATE_CANCELLED}


def state_text(state):
    """``"▶ thinking"``: the glyph and the translated word of a state."""
    glyph, word = STATE_LABELS[state]
    return "{0} {1}".format(glyph, tr(word))


class StreamEntry:
    """What one request streamed so far."""

    def __init__(self, key, info):
        self.key = key
        self.info = info or {}
        self.state = STATE_WAITING
        self.thinking = ""
        self.thinking_chars = 0  # counts what was dropped beyond MAX_THINKING_CHARS too
        self.answer_chars = 0
        self.truncated = False
        self.started_at = time.time()
        self.finished_at = None

    @property
    def active(self):
        return self.state in ACTIVE_STATES

    @property
    def seconds(self):
        return (self.finished_at or time.time()) - self.started_at


class StreamLog:
    """The reasoning of the last requests, keyed by request.

    ``handle(key, kind, payload)`` takes the fields of one ``stream`` event.
    Listeners are called as ``listener(event, entry, text)`` with ``event``
    ``"started"``, ``"thinking"`` (``text`` is the new piece), ``"updated"``
    (state or counters changed) or ``"removed"``. Everything happens on the
    thread that drains the event queue (the UI thread).
    """

    def __init__(self, max_entries=MAX_ENTRIES, max_chars=MAX_THINKING_CHARS):
        self.entries = OrderedDict()  # key -> StreamEntry, oldest first
        self.listeners = []
        self.max_entries = max_entries
        self.max_chars = max_chars

    def handle(self, key, kind, payload):
        if kind == "started":
            self.begin(key, payload)
        elif kind == STREAM_THINKING:
            self.append_thinking(key, payload)
        elif kind == STREAM_ANSWER:
            self.append_answer(key, payload)
        elif kind == "finished":
            self.finish(key, payload)

    @property
    def newest(self):
        return next(reversed(self.entries.values())) if self.entries else None

    def active_entries(self):
        return [entry for entry in self.entries.values() if entry.active]

    def begin(self, key, info):
        """Start a fresh entry for ``key`` (a re-evaluated segment replaces its old entry)."""
        old = self.entries.pop(key, None)
        if old is not None:
            self._notify("removed", old, "")
        entry = StreamEntry(key, info)
        self.entries[key] = entry
        self._prune()
        self._notify("started", entry, "")
        return entry

    def _prune(self):
        while len(self.entries) > self.max_entries:
            victim = next((entry for entry in self.entries.values() if not entry.active), None)
            if victim is None:
                victim = next(iter(self.entries.values()))
            del self.entries[victim.key]
            self._notify("removed", victim, "")

    def _entry(self, key):
        entry = self.entries.get(key)
        if entry is None:  # text before its announcement: keep it anyway
            entry = self.begin(key, {})
        return entry

    def append_thinking(self, key, text):
        entry = self._entry(key)
        entry.thinking_chars += len(text)
        room = self.max_chars - len(entry.thinking)
        if len(text) > room:
            text = text[:max(0, room)]
            entry.truncated = True
        entry.thinking += text
        if entry.active:
            entry.state = STATE_THINKING
        self._notify("thinking", entry, text)

    def append_answer(self, key, text):
        entry = self._entry(key)
        entry.answer_chars += len(text)
        if entry.active:
            entry.state = STATE_ANSWERING
        self._notify("updated", entry, "")

    def finish(self, key, outcome):
        entry = self.entries.get(key)
        if entry is None:
            return
        entry.state = OUTCOME_STATES.get(outcome, STATE_DONE)
        entry.finished_at = time.time()
        self._notify("updated", entry, "")

    def clear_finished(self):
        """Drop every entry whose request has ended; returns how many were dropped."""
        finished = [entry for entry in self.entries.values() if not entry.active]
        for entry in finished:
            del self.entries[entry.key]
            self._notify("removed", entry, "")
        return len(finished)

    def _notify(self, event, entry, text):
        for listener in list(self.listeners):
            listener(event, entry, text)


class ThinkingWindow(tk.Toplevel):
    """Show the reasoning the model streams, request by request.

    ``describe(info)`` turns the ``info`` dict of a request into its label
    (the host knows chapters, checks and editing modes); ``hint()`` returns a
    sentence explaining why the current backend may send no reasoning, shown
    while an entry has none.
    """

    COLUMNS = (  # widths at 100 %, scaled with the fonts
        ("state", N_("State"), 115),
        ("request", N_("Request"), 150),
        ("size", N_("Reasoning"), 90),
    )
    LIST_WIDTH = 380  # initial width of the request list at 100 %

    def __init__(self, parent, log, describe, hint=None):
        super().__init__(parent)
        self.title(tr("Model thinking"))
        self.transient(parent.winfo_toplevel())
        self.geometry("960x600")
        self.minsize(720, 400)
        self.log = log
        self.describe = describe
        self.hint = hint or (lambda: "")
        self._iids = {}  # entry key -> tree iid
        self._entries = {}  # tree iid -> entry
        self._counter = 0
        self._shown = None  # entry whose reasoning the text pane shows
        self._pending_text = []  # pieces of the shown entry not yet inserted
        self._shown_changed = False  # the shown entry's state or counters changed since the last flush
        self._dirty = set()  # iids whose row needs a refresh
        self._flush_after = None
        self._tick_after = None
        self._sash_placed = False  # the list gets its initial width once the panes have a real size
        self._build()
        self.log.listeners.append(self._on_log)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Escape>", lambda event: self.destroy())
        bind_restyle(self, self._restyle)
        for entry in self.log.entries.values():
            self._add_row(entry)
        self._select_newest()
        self._tick()

    # ---------------------------------------------------------------- build
    def _build(self):
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        description = ttk.Label(
            top, style="Muted.TLabel", justify=tk.LEFT,
            text=tr("The reasoning the model streams while it works, newest request first. "
                    "Only reasoning models send it; the edited text itself never contains it."),
        )
        description.grid(row=0, column=0, sticky="w")
        self.follow_var = tk.BooleanVar(value=True)
        follow = ttk.Checkbutton(top, text=tr("Follow newest request"), variable=self.follow_var,
                                 command=self._on_follow_toggled)
        follow.grid(row=0, column=1, sticky="e", padx=(12, 0))
        Tooltip(follow, tr("Switch to every new request as it starts. Selecting an older request turns this off."))
        clear = ttk.Button(top, text=tr("Clear finished"), style="Ghost.TButton", command=self._clear_finished)
        clear.grid(row=0, column=2, sticky="e", padx=(6, 0))

        def wrap_description(event):  # keep the text clear of the controls at any width or scale
            controls = follow.winfo_reqwidth() + clear.winfo_reqwidth() + 30
            description.configure(wraplength=max(240, event.width - controls))

        top.bind("<Configure>", wrap_description)

        self.panes = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        panes = self.panes
        panes.grid(row=1, column=0, sticky="nsew")
        panes.bind("<Configure>", self._on_panes_configure)

        left = ttk.Frame(panes)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, columns=[key for key, _, _ in self.COLUMNS], show="headings",
                                 selectmode="browse")
        for key, heading, width in self.COLUMNS:
            self.tree.heading(key, text=tr(heading), anchor="w")
            self.tree.column(key, stretch=(key == "request"), anchor="w")
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        panes.add(left, weight=2)

        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.header_var = tk.StringVar(value=tr("No request selected"))
        header = ttk.Label(right, textvariable=self.header_var, style="Heading.TLabel", justify=tk.LEFT)
        header.grid(row=0, column=0, sticky="w")
        self.detail_var = tk.StringVar(value="")
        detail = ttk.Label(right, textvariable=self.detail_var, style="Muted.TLabel", justify=tk.LEFT)
        detail.grid(row=1, column=0, sticky="w", pady=(2, 6))

        def wrap_labels(event):  # long labels and the counters wrap instead of pushing the pane wider
            for label in (header, detail):
                label.configure(wraplength=max(200, event.width - 12))

        right.bind("<Configure>", wrap_labels)
        text_frame = ttk.Frame(right)
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, undo=False)
        text_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        text_scroll.grid(row=0, column=1, sticky="ns")
        self._apply_styles()
        panes.add(right, weight=5)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text=tr("Close"), style="Ghost.TButton", command=self.destroy).pack(side=tk.RIGHT)

    def _restyle(self):
        """Palette or scale changed: recolour the plain Tk widgets and re-fit the list."""
        self._apply_styles()
        if self._sash_placed:
            self.after_idle(self._place_sash)

    def _apply_styles(self):
        style_text(self.text, size=10, readonly=True)
        self.text.tag_configure("note", foreground=PALETTE["muted"], font=font(10, slant="italic"))
        self.tree.tag_configure("active", foreground=PALETTE["accent_dark"])
        self.tree.tag_configure("error", foreground=PALETTE["danger"])
        for key, _, width in self.COLUMNS:
            self.tree.column(key, width=scaled(width), minwidth=scaled(width))

    def _on_panes_configure(self, event):
        # Placing the sash before the panes have their final size clamps it to
        # zero, so wait for the first Configure with a real width.
        if not self._sash_placed and event.width > 100:
            self._sash_placed = True
            self._place_sash()

    def _place_sash(self):
        """Give the request list room for its three columns at the current scale."""
        try:
            self.panes.sashpos(0, scaled(self.LIST_WIDTH))
        except tk.TclError:
            pass

    # ------------------------------------------------------------ log events
    def _on_log(self, event, entry, text):
        if event == "started":
            self._add_row(entry)
            if self.follow_var.get():
                self._show(entry)
            return
        if event == "removed":
            iid = self._iids.pop(entry.key, None)
            if iid is not None:
                self._entries.pop(iid, None)
                self._dirty.discard(iid)
                if self.tree.exists(iid):
                    self.tree.delete(iid)
            if entry is self._shown:
                self._show(None)
            return
        iid = self._iids.get(entry.key)
        if iid is None:
            self._add_row(entry)
            iid = self._iids[entry.key]
        self._dirty.add(iid)
        if entry is self._shown:
            self._shown_changed = True
            if event == "thinking" and text:
                self._pending_text.append(text)
        self._schedule_flush()

    def _add_row(self, entry):
        if entry.key in self._iids:
            return
        self._counter += 1
        iid = "r{0}".format(self._counter)
        self._iids[entry.key] = iid
        self._entries[iid] = entry
        self.tree.insert("", 0, iid=iid, values=self._row_values(entry), tags=self._row_tags(entry))

    def _row_values(self, entry):
        return (state_text(entry.state), self.describe(entry.info), format_number(entry.thinking_chars))

    @staticmethod
    def _row_tags(entry):
        if entry.state == STATE_ERROR:
            return ("error",)
        return ("active",) if entry.active else ()

    def _schedule_flush(self):
        if self._flush_after is None:
            self._flush_after = self.after(FLUSH_INTERVAL_MS, self._flush)

    def _flush(self):
        """Apply the batched changes: refresh dirty rows, append the shown entry's new reasoning."""
        self._flush_after = None
        for iid in list(self._dirty):
            entry = self._entries.get(iid)
            if entry is not None and self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(entry), tags=self._row_tags(entry))
        self._dirty.clear()
        if self._shown is not None and self._shown_changed:
            self._shown_changed = False
            if self._pending_text:
                self._append_text("".join(self._pending_text))
                self._pending_text = []
            if not self._shown.active and not self._shown.thinking:
                self._render_shown()  # the request ended without reasoning: show the note
            else:
                self._refresh_detail()

    def _tick(self):
        """Keep the elapsed time of a running request moving."""
        self._tick_after = None
        if self._shown is not None and self._shown.active:
            self._refresh_detail()
        self._tick_after = self.after(1000, self._tick)

    # -------------------------------------------------------------- selection
    def _on_select(self, event=None):
        selection = self.tree.selection()
        entry = self._entries.get(selection[0]) if selection else None
        if entry is None or entry is self._shown:
            return
        if entry is not self.log.newest:
            self.follow_var.set(False)
        self._show(entry, select=False)

    def _on_follow_toggled(self):
        if self.follow_var.get():
            self._select_newest()

    def _select_newest(self):
        newest = self.log.newest
        if newest is not None:
            self._show(newest)
        else:
            self._show(None)

    def _show(self, entry, select=True):
        """Put ``entry`` (or the empty state) into the text pane and, with ``select``, the list."""
        self._shown = entry
        self._pending_text = []
        self._shown_changed = False
        if select and entry is not None:
            iid = self._iids.get(entry.key)
            if iid is not None and self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
        elif select:
            self.tree.selection_remove(*self.tree.selection())
        self._render_shown()

    def _render_shown(self):
        entry = self._shown
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        if entry is None:
            self.header_var.set(tr("No request selected"))
            self.detail_var.set("")
            self.text.insert(tk.END, tr("Start a quick edit or an automatic review: the model's reasoning "
                                        "appears here while it works."), "note")
        else:
            self.header_var.set(self.describe(entry.info))
            self._refresh_detail()
            if entry.thinking:
                self.text.insert(tk.END, entry.thinking)
                if entry.truncated:
                    self._insert_truncation_note()
            elif entry.active:
                self.text.insert(tk.END, tr("Waiting for the model's reasoning..."), "note")
            else:
                note = tr("This request produced no reasoning.")
                hint = self.hint()
                if hint:
                    note += " " + hint
                self.text.insert(tk.END, note, "note")
        self.text.configure(state=tk.DISABLED)
        self.text.see(tk.END)

    def _append_text(self, text):
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state=tk.NORMAL)
        if self._shown.thinking == text:  # first piece: replace the waiting note
            self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, text)
        if self._shown.truncated and len(self._shown.thinking) >= self.log.max_chars and not self._truncation_shown():
            self._insert_truncation_note()
        self.text.configure(state=tk.DISABLED)
        if at_bottom:
            self.text.see(tk.END)

    def _truncation_shown(self):
        return bool(self.text.tag_ranges("note"))

    def _insert_truncation_note(self):
        self.text.insert(tk.END, "\n\n" + tr("(reasoning cut off after {count} characters)",
                                             count=format_number(self.log.max_chars)), "note")

    def _refresh_detail(self):
        entry = self._shown
        if entry is None:
            return
        self.detail_var.set(tr(
            "{state} · {thinking} characters of reasoning · {answer} characters of answer · {seconds:.0f} s",
            state=state_text(entry.state), thinking=format_number(entry.thinking_chars),
            answer=format_number(entry.answer_chars), seconds=entry.seconds,
        ))

    # ---------------------------------------------------------------- actions
    def _clear_finished(self):
        self.log.clear_finished()
        if self._shown is None and self.follow_var.get():
            self._select_newest()

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if self._on_log in self.log.listeners:
            self.log.listeners.remove(self._on_log)
        for after_id in (self._flush_after, self._tick_after):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._flush_after = self._tick_after = None
