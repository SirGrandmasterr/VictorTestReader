"""Automatic manuscript review: start screen and project review screen."""

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.backend import EditCancelled
from core.change_kinds import CHANGE_KIND_LABELS, CHANGE_KINDS
from core.chunking import split_document, word_count
from core.consistency import (
    FINDING_LABELS,
    MODEL_TRIAGED,
    STATUS_DISMISSED,
    STATUS_ISSUE,
    STATUS_LABELS as FINDING_STATUS_LABELS,
    STATUS_OK,
    STATUS_UNVERIFIED,
    ConsistencyRunner,
    apply_preferred,
    apply_verdicts,
    collect_candidates,
)
from core.documents import FORMATTING_NOTE, KIND_LABELS, MANUSCRIPT_PATTERNS, DocumentError, load_document
from core.models import ACCEPTED, PENDING, REJECTED
from core.remote_service import relay_throughput
from core.statistics import project_statistics, statistics_markdown
from core.workflow import (
    CHECK_AUTHOR,
    CHECK_DESCRIPTIONS,
    CHECK_LABELS,
    CHECKS,
    EVALUATION_COMBINED,
    EVALUATION_LABELS,
    EVALUATION_MODES,
    FLAG_LABELS,
    PROJECT_DIR_SUFFIX,
    PROJECT_FILE,
    SAME_LANGUAGE,
    STATE_APPLIED,
    STATE_PENDING,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATUS_CLEAN,
    STATUS_ERROR,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_REVIEWED,
    Project,
    ProjectOptions,
    ProjectRunner,
    apply_model_outline,
    apply_result,
    build_outline_messages,
    change_states,
    create_project,
    new_decision_group,
    normalise_style_guide,
    parse_glossary,
    parse_outline,
    pending_changes,
    render_chapter_annotated,
    render_segment,
    resync_project,
)
from .theme import (
    CHECK_COLORS,
    DECISION_GLYPHS,
    PALETTE,
    SPAN_MARKERS,
    STATUS_COLORS,
    STATUS_GLYPHS,
    ScrollableFrame,
    Tooltip,
    bind_restyle,
    font,
    scaled,
    style_text,
)

STATUS_LABELS = {
    STATUS_QUEUED: "Queued",
    "running": "Evaluating",
    STATUS_ERROR: "Error",
    STATUS_CLEAN: "No changes",
    STATUS_READY: "To review",
    STATUS_REVIEWED: "Reviewed",
}
STATE_LABELS = {
    STATE_APPLIED: "Applied",
    STATE_SUPERSEDED: "Superseded",
    STATE_REJECTED: "Rejected",
    STATE_PENDING: "Pending",
}
LANGUAGES = [SAME_LANGUAGE, "English", "German", "French", "Spanish", "Italian", "Dutch"]
STYLE_GUIDE_PLACEHOLDER = "British spelling · keep dialect inside dialogue · never touch quotations"
STYLE_GUIDE_HINT = ("Standing rules every check must respect, one per line. They are sent with every request "
                    "and take precedence over the built-in rules where they conflict.")
GLOSSARY_PLACEHOLDER = "Thalbrück\nMeret Aubinger\nhyper*"
GLOSSARY_HINT = ("Protected terms: names, invented words and technical terms the checks must never change, one per "
                 "line. A trailing * protects every word starting with it (hyper* covers hyperdrive). Changes that "
                 "touch a protected term are dropped before you see them.")


def flag_tooltip(change):
    """Human-readable reasons behind a change's hallucination-guard flags."""
    return "\n".join(FLAG_LABELS.get(flag, flag) for flag in change.flags) or "Possibly invented content."


def _flagged_note(stats):
    return " \u00b7 {0} flagged \u26a0".format(stats["flagged"]) if stats.get("flagged") else ""


def _suppressed_note(stats):
    return " · {0} suppressed by glossary".format(stats["suppressed"]) if stats.get("suppressed") else ""


RELAY_STATUS_INTERVAL_MS = 10000  # how often the relay's /status is polled while evaluating


def _format_throughput(throughput):
    """Progress-line suffix for a relay throughput summary (``relay_throughput``), or ""."""
    if not throughput:
        return ""
    rate = throughput.get("chunks_per_s") or 0
    return "GPU ≈ {0:.0f} chunks/s · {1} queued".format(rate, throughput.get("queued", 0))


def _format_eta(seconds):
    if seconds is None:
        return ""
    if seconds < 90:
        return "about a minute left"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return "~{0} min left".format(minutes)
    return "~{0} h {1:02d} min left".format(minutes // 60, minutes % 60)


class StyleGuideBox(tk.Text):
    """Multi-line entry for the author's instructions with a grey placeholder."""

    def __init__(self, parent, height=4, placeholder=STYLE_GUIDE_PLACEHOLDER, **kwargs):
        super().__init__(parent, height=height, **kwargs)
        style_text(self, size=10)
        self.placeholder = placeholder
        self._showing_placeholder = False
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.set("")
        bind_restyle(self, self.restyle)

    def restyle(self):
        style_text(self, size=10)
        self.configure(foreground=PALETTE["muted"] if self._showing_placeholder else PALETTE["text"])

    def get_text(self):
        """Return the author's text ("" while the placeholder is shown)."""
        if self._showing_placeholder:
            return ""
        return self.get("1.0", tk.END).rstrip()

    def set(self, text):
        self.delete("1.0", tk.END)
        if text:
            self._showing_placeholder = False
            self.configure(foreground=PALETTE["text"])
            self.insert("1.0", text)
        else:
            self._showing_placeholder = True
            self.configure(foreground=PALETTE["muted"])
            self.insert("1.0", self.placeholder)

    def _focus_in(self, event=None):
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.delete("1.0", tk.END)
            self.configure(foreground=PALETTE["text"])

    def _focus_out(self, event=None):
        if not self.get("1.0", tk.END).strip():
            self.set("")


class StyleGuideDialog(tk.Toplevel):
    """Edit the author's instructions and protected terms of an open project."""

    def __init__(self, parent, style_guide, on_save, glossary=()):
        super().__init__(parent)
        self.on_save = on_save
        self.title("Review options")
        self.transient(parent.winfo_toplevel())
        self.resizable(True, False)
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Author's instructions", font=font(11, "bold")).pack(anchor="w")
        ttk.Label(body, text=STYLE_GUIDE_HINT, style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=(2, 8))
        self.box = StyleGuideBox(body, height=5, width=60)
        self.box.pack(fill=tk.BOTH, expand=True)
        self.box.set(style_guide)
        ttk.Label(body, text="Protected terms", font=font(11, "bold")).pack(anchor="w", pady=(12, 0))
        ttk.Label(body, text=GLOSSARY_HINT, style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=(2, 8))
        self.glossary_box = StyleGuideBox(body, height=5, width=60, placeholder=GLOSSARY_PLACEHOLDER)
        self.glossary_box.pack(fill=tk.BOTH, expand=True)
        self.glossary_box.set("\n".join(glossary))
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", style="Primary.TButton", command=self.save).pack(side=tk.RIGHT, padx=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        self.box.focus_set()
        self.update_idletasks()
        try:
            top = parent.winfo_toplevel()
            x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
            y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 2
            self.geometry("+{0}+{1}".format(max(x, 0), max(y, 0)))
        except tk.TclError:
            pass

    def save(self):
        text = self.box.get_text()
        glossary = parse_glossary(self.glossary_box.get_text())
        self.destroy()
        self.on_save(text, glossary)


class DecisionLogDialog(tk.Toplevel):
    """Read-only list of every accept/reject the author made; double-click jumps to the change."""

    COLUMNS = (
        ("time", "Time", 130),
        ("chapter", "Chapter", 150),
        ("segment", "Segment", 70),
        ("change", "Original → proposed", 320),
        ("decision", "Before → after", 150),
    )

    def __init__(self, parent, project, on_jump):
        super().__init__(parent)
        self.title("Decisions · {0}".format(project.name))
        self.transient(parent.winfo_toplevel())
        self.geometry("880x420")
        self.project = project
        self.on_jump = on_jump
        self.entries = {}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text="Newest decision first. Double-click a row to jump to the change.",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.tree = ttk.Treeview(frame, columns=[key for key, _, _ in self.COLUMNS], show="headings",
                                 selectmode="browse")
        for key, heading, width in self.COLUMNS:
            self.tree.heading(key, text=heading, anchor="w")
            self.tree.column(key, width=width, stretch=(key == "change"), anchor="w")
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.tag_configure("stale", foreground=PALETTE["faint"])
        self.tree.bind("<Double-1>", self._jump)
        self.tree.bind("<Return>", self._jump)
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Go to change", command=self._jump).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Close", style="Ghost.TButton", command=self.destroy).pack(side=tk.LEFT, padx=(6, 0))
        self.bind("<Escape>", lambda event: self.destroy())
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        log = self.project.decision_log
        if not log:
            self.tree.insert("", tk.END, values=("", "", "", "No decisions yet.", ""))
            return
        for position, entry in enumerate(reversed(log)):
            if entry.get("resync"):
                summary = entry["resync"]
                self.tree.insert("", tk.END, iid="d{0}".format(position), tags=("stale",), values=(
                    str(entry.get("ts", "")).replace("T", " "), "—", "",
                    "Re-synced with the manuscript: {0} segment(s) kept, {1} new, {2} removed".format(
                        summary.get("kept", 0), summary.get("new", 0), summary.get("removed", 0)), ""))
                continue
            chapter, segment = self.project.find(entry["chapter"], entry["segment"])
            change = segment.find_change(entry["change_id"]) if segment is not None else None
            if change is not None:
                text = "{0} → {1}".format(_one_line(change.original_text) or "∅",
                                              _one_line(change.proposed_text) or "∅")
            else:
                text = "(change no longer exists)"
            stale = bool(entry.get("stale"))
            if stale:
                text += "  (re-evaluated)"
            iid = "d{0}".format(position)
            self.tree.insert("", tk.END, iid=iid, tags=("stale",) if stale else (), values=(
                str(entry.get("ts", "")).replace("T", " "),
                chapter.title if chapter is not None else str(entry["chapter"]),
                entry["segment"],
                text,
                "{0} → {1}".format(entry["before"] or "new", entry["after"]),
            ))
            self.entries[iid] = entry

    def _jump(self, event=None):
        selection = self.tree.selection()
        entry = self.entries.get(selection[0]) if selection else None
        if entry is not None and entry.get("chapter") is not None:
            # A stale entry's change was replaced by a re-evaluation: only the segment is left to show.
            self.on_jump(entry["chapter"], entry["segment"], None if entry.get("stale") else entry["change_id"])
        return "break"


class StatisticsDialog(tk.Toplevel):
    """Chapters, checks and frequent corrections as tables, with Markdown export."""

    def __init__(self, parent, project, on_jump):
        super().__init__(parent)
        self.title("Statistics · {0}".format(project.name))
        self.transient(parent.winfo_toplevel())
        self.geometry("900x460")
        self.project = project
        self.on_jump = on_jump
        self.stats = None
        self.frequent_rows = {}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.summary_var, style="Muted.TLabel", wraplength=860,
                  justify=tk.LEFT).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.notebook = ttk.Notebook(frame)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.chapters_tree = self._table(
            "Chapters", (("index", "#", 40), ("title", "Chapter", 220), ("words", "Words", 80),
                         ("changes", "Changes", 80), ("per_1000", "per 1,000", 80), ("accepted", "Accepted", 80),
                         ("rejected", "Rejected", 80), ("pending", "Pending", 80), ("rate", "Acceptance", 90)))
        self.checks_tree = self._table(
            "Checks", (("check", "Check", 160), ("changes", "Changes", 90), ("per_1000", "per 1,000", 90),
                       ("accepted", "Accepted", 90), ("rejected", "Rejected", 90), ("pending", "Pending", 90),
                       ("rate", "Acceptance", 100)))
        self.kinds_tree = self._table(
            "Kinds", (("kind", "Kind", 160), ("changes", "Changes", 90), ("accepted", "Accepted", 90),
                      ("rejected", "Rejected", 90), ("pending", "Pending", 90), ("rate", "Acceptance", 100)))
        self.frequent_tree = self._table(
            "Frequent corrections", (("original", "Original", 220), ("proposed", "Proposed", 220),
                                     ("count", "Count", 70), ("check", "Check", 100), ("accepted", "Accepted", 80),
                                     ("rejected", "Rejected", 80)))
        self.frequent_tree.bind("<Double-1>", self._jump)
        self.frequent_tree.bind("<Return>", self._jump)
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(buttons, text="Double-click a frequent correction to open its first occurrence.",
                  style="Muted.TLabel", font=font(9)).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Close", style="Ghost.TButton", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(buttons, text="Copy as Markdown", command=self.copy_markdown).pack(side=tk.RIGHT, padx=(0, 6))
        self.bind("<Escape>", lambda event: self.destroy())
        self.refresh()

    def _table(self, title, columns):
        tab = ttk.Frame(self.notebook)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.notebook.add(tab, text=title)
        tree = ttk.Treeview(tab, columns=[key for key, _, _ in columns], show="headings", selectmode="browse")
        for position, (key, heading, width) in enumerate(columns):
            numeric = key not in ("title", "chapter", "check", "kind", "original", "proposed")
            tree.heading(key, text=heading, anchor="e" if numeric else "w")
            tree.column(key, width=width, anchor="e" if numeric else "w",
                        stretch=(key in ("title", "original", "proposed") or position == 0 and key in ("check", "kind")))
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        return tree

    @staticmethod
    def _percent(rate):
        return "{0:.0f}%".format(rate * 100)

    def refresh(self):
        stats = self.stats = project_statistics(self.project)
        totals = stats["totals"]
        self.summary_var.set(
            "{0:,} words · {1} changes ({2} per 1,000 words) · {3} accepted, {4} rejected, {5} pending "
            "· acceptance rate {6} (accepted share of the decided changes)".format(
                totals["words"], totals["changes"], totals["per_1000"], totals["accepted"], totals["rejected"],
                totals["pending"], self._percent(totals["acceptance_rate"])))
        for tree in (self.chapters_tree, self.checks_tree, self.kinds_tree, self.frequent_tree):
            tree.delete(*tree.get_children())
        for row in stats["chapters"]:
            self.chapters_tree.insert("", tk.END, values=(
                row["index"], row["title"], "{0:,}".format(row["words"]), row["changes"], row["per_1000"],
                row["accepted"], row["rejected"], row["pending"], self._percent(row["acceptance_rate"])))
        for check, counter in stats["checks"].items():
            if not counter["changes"] and check not in self.project.options.enabled_checks():
                continue
            self.checks_tree.insert("", tk.END, values=(
                CHECK_LABELS[check], counter["changes"], counter["per_1000"], counter["accepted"],
                counter["rejected"], counter["pending"], self._percent(counter["acceptance_rate"])))
        for kind, counter in stats["kinds"].items():
            self.kinds_tree.insert("", tk.END, values=(
                CHANGE_KIND_LABELS[kind], counter["changes"], counter["accepted"], counter["rejected"],
                counter["pending"], self._percent(counter["acceptance_rate"])))
        self.frequent_rows = {}
        for position, item in enumerate(stats["frequent"]):
            iid = "f{0}".format(position)
            self.frequent_tree.insert("", tk.END, iid=iid, values=(
                item["original"] or "∅", item["proposed"] or "∅", item["count"],
                CHECK_LABELS.get(item["check"], item["check"]), item["accepted"], item["rejected"]))
            self.frequent_rows[iid] = item["first"]

    def copy_markdown(self):
        markdown = statistics_markdown(self.stats or project_statistics(self.project))
        self.clipboard_clear()
        self.clipboard_append(markdown)
        self.summary_var.set("Copied the statistics as Markdown to the clipboard.")

    def _jump(self, event=None):
        selection = self.frequent_tree.selection()
        target = self.frequent_rows.get(selection[0]) if selection else None
        if target is not None:
            self.on_jump(*target)
        return "break"


def _one_line(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def state_text(state):
    """A decision state as glyph plus word, e.g. "✓ Applied" (never colour alone)."""
    return "{0} {1}".format(DECISION_GLYPHS.get(state, ""), STATE_LABELS.get(state, state)).strip()


def status_text(status, detail=""):
    """A segment status as glyph plus word with an optional detail, e.g. "● To review · 2 pending"."""
    label = STATUS_LABELS.get(status, status)
    if detail:
        label = "{0} · {1}".format(label, detail)
    return "{0} {1}".format(STATUS_GLYPHS.get(status, ""), label).strip()


def mark_spans(text, spans, markers=SPAN_MARKERS):
    """Insert a text marker in front of every span; returns ``(text, spans)`` in the new coordinates.

    ``spans`` are ``render_chapter_annotated`` dicts; the returned copies carry
    shifted ``start``/``end`` so tags and clicks keep lining up. Spans starting
    at the same offset share one marker each (both are inserted, in order).
    """
    inserts = sorted(
        ((span["start"], number, markers[span["state"]]) for number, span in enumerate(spans)
         if span["state"] in markers),
        key=lambda item: (item[0], item[1]),
    )
    if not inserts:
        return text, [dict(span) for span in spans]
    pieces = []
    position = 0
    for offset, _, marker in inserts:
        pieces.append(text[position:offset])
        pieces.append(marker)
        position = offset
    pieces.append(text[position:])
    marked = "".join(pieces)

    def shift(offset, inclusive):
        added = 0
        for insert_at, _, marker in inserts:
            if insert_at < offset or (inclusive and insert_at == offset):
                added += len(marker)
            else:
                break
        return offset + added

    shifted = []
    for number, span in enumerate(spans):
        copy = dict(span)
        copy["start"] = shift(span["start"], inclusive=True)
        copy["end"] = max(copy["start"], shift(span["end"], inclusive=False))
        shifted.append(copy)
    return marked, shifted


class WorkflowScreen(ttk.Frame):
    """Container that switches between the start view and the project view."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host
        self.project = None
        self.runner = None
        self.running_tasks = set()
        self._save_after = None
        self._status_after = None  # pending relay status poll (after id)
        self.start_view = StartView(self, on_start=self.start_project, on_open=self.open_project)
        self.project_view = ProjectView(self, on_close=self.close_project, on_pause=self.toggle_pause,
                                        on_export=self.export_project, on_retry=self.resume_runner,
                                        on_checks_changed=self.checks_changed, on_options=self.edit_options,
                                        on_add_to_glossary=self.add_to_glossary, on_reevaluate=self.reevaluate)
        self.decision_dialog = None
        self.statistics_dialog = None
        self.consistency_runner = None
        self.start_view.pack(fill=tk.BOTH, expand=True)

    # ----------------------------------------------------------- lifecycle
    @property
    def active(self):
        return self.project is not None

    @property
    def evaluating(self):
        return self.runner is not None and self.runner.active

    def show_start(self):
        self.project_view.pack_forget()
        self.start_view.pack(fill=tk.BOTH, expand=True)
        self.start_view.refresh_defaults(self.host)

    def show_project(self):
        self.start_view.pack_forget()
        self.project_view.pack(fill=tk.BOTH, expand=True)

    def start_project(self, path, options):
        try:
            document = load_document(path)
        except (OSError, DocumentError) as exc:
            messagebox.showerror("Cannot read file", str(exc))
            return
        text = document.text
        if not text.strip():
            messagebox.showinfo("Empty file", "The selected file contains no text.")
            return
        model = self.host.get_model()
        if not model:
            messagebox.showerror("No model", "Select a model in the toolbar before starting a review.")
            return
        project = create_project(path, document, options, model=model, backend=self.host.backend_id())
        self.host.remember_style_guide(options.style_guide)
        self.host.remember_glossary(options.glossary)
        self.host.remember_evaluation_mode(options.evaluation_mode)
        if project.root.exists() and (project.root / PROJECT_FILE).exists():
            if not messagebox.askyesno(
                "Replace previous review?",
                "A previous review of this file exists in\n{0}\n\nStart over and discard it?".format(project.root),
            ):
                return
        if options.chapter_mode == "model":
            self.host.set_status("Asking the model where the chapters start...")
            self._detect_chapters_with_model(project, text)
            return
        self._launch(project)

    def _detect_chapters_with_model(self, project, text):
        service = self.host.get_service()
        model = project.model
        cancel = threading.Event()

        def worker():
            try:
                answer = service.generate(model, build_outline_messages(text), cancel, max_tokens=2048)
                starts, titles = parse_outline(answer)
                self.host.events.put(("workflow_outline", project, text, starts, titles, None))
            except Exception as exc:
                self.host.events.put(("workflow_outline", project, text, [], [], str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _launch(self, project):
        self.project = project
        project.write_chapter_files()
        project.save()
        self.project_view.load_project(project)
        self.show_project()
        self.host.lock_controls(True)
        self.resume_runner()

    def open_project(self, path):
        try:
            project = Project.load(path)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("Cannot open project", str(exc))
            return
        self.project = project
        self.project_view.load_project(project)
        self.show_project()
        if project.source_changed() and self._offer_resync():
            return
        pending = project.pending_tasks()
        if pending:
            if messagebox.askyesno(
                "Resume evaluation?",
                "{0} check(s) are still missing. Continue the automatic evaluation now?".format(len(pending)),
            ):
                self.host.lock_controls(True)
                self.resume_runner()
                return
        self.host.set_status("Project opened. {0}".format(self.project_view.summary_text()))

    # -------------------------------------------------------------- source
    def check_source(self):
        """The "Check source" button: compare the manuscript file with the project and offer a re-sync."""
        if self.project is None:
            return
        project = self.project
        if project.source_changed():
            self._offer_resync()
            return
        reason = project.source_check_reason
        if reason == "missing":
            messagebox.showwarning("Manuscript not found",
                                   "The manuscript file was not found:\n{0}".format(project.source_path))
        elif reason == "unknown":
            if messagebox.askyesno(
                "Re-sync?",
                "This project was created before source tracking existed, so changes to the manuscript cannot be "
                "detected automatically.\n\nRe-sync with the current file now? Segments with identical text keep "
                "their results and decisions; new or edited segments are evaluated again.",
            ):
                self.resync()
        else:
            self.host.set_status("The manuscript has not changed since the project was created.")

    def _offer_resync(self):
        """Ask whether to re-sync a changed manuscript; returns whether a re-sync was done."""
        if not messagebox.askyesno(
            "Manuscript changed",
            "The manuscript changed since the project was created. Re-sync?\n\nSegments with identical text keep "
            "their results and decisions; new or edited segments are evaluated again. Pending changes of "
            "segments that no longer exist are written to a resync-*.md file in the project folder.",
        ):
            self.host.set_status("The manuscript file changed; use “Check source” to re-sync later.")
            return False
        return self.resync()

    def resync(self):
        """Re-split the current manuscript file and carry decisions over; returns whether it happened."""
        if self.project is None:
            return False
        if self.evaluating:
            messagebox.showinfo("Evaluation running", "Pause the evaluation before re-syncing the manuscript.")
            return False
        try:
            document = load_document(self.project.source_path)
        except (OSError, DocumentError) as exc:
            messagebox.showerror("Cannot read manuscript", str(exc))
            return False
        if not document.text.strip():
            messagebox.showinfo("Empty file", "The manuscript file contains no text; nothing was changed.")
            return False
        summary = resync_project(self.project, document)
        self.running_tasks = set()
        if self.project.consistency:
            self.project.consistency = collect_candidates(self.project, previous=self.project.consistency)
        try:
            self.project.write_chapter_files()
        except OSError:
            pass
        self._save_now()
        self.project_view.load_project(self.project)
        self._refresh_decision_dialog()
        message = "Re-synced: {0} segment(s) kept, {1} new, {2} removed, {3} chapter(s).".format(
            summary["kept"], summary["new"], summary["removed"], summary["chapters"])
        if summary["report"]:
            message += " Dropped changes were written to {0}.".format(summary["report"].name)
        self.host.set_status(message)
        pending = self.project.pending_tasks()
        if pending and messagebox.askyesno(
                "Evaluate now?", "{0}\n\n{1} check(s) are missing for the new or edited segments. Evaluate them now?"
                .format(message, len(pending))):
            self.host.lock_controls(True)
            self.resume_runner()
        return True

    def resume_runner(self):
        if self.project is None or self.evaluating:
            return
        service = self.host.get_service()
        model = self.host.get_model() or self.project.model
        self.project.model = model
        self.project.backend = self.host.backend_id()
        self.runner = ProjectRunner(self.project, service, model, self.host.events)
        self.running_tasks = set()
        queued = self.runner.start()
        self.host.lock_controls(queued > 0)
        self.project_view.set_running(queued > 0)
        if queued:
            self.host.set_status(
                "Evaluating {0} check(s) with {1} on {2} parallel request(s)...".format(
                    queued, model, self.runner.parallelism
                )
            )
            self.project_view.update_progress(0, queued, 0, None)
            self._schedule_status_poll(1000)

    def reevaluate(self, chapter_index, segment_index=None, checks=None):
        """Drop results of a segment (or chapter) and evaluate them again right away."""
        if self.project is None:
            return 0
        checks = list(checks) if checks else self.project.options.enabled_checks()
        removed = self.project.invalidate(chapter_index, segment_index, checks)
        self.project_view.refresh_all()
        self.schedule_save()
        self._refresh_decision_dialog()
        if not removed:
            self.host.set_status("Nothing to evaluate again there.")
            return 0
        busy = {(chapter, segment) for chapter, segment, _ in self.running_tasks}  # in-flight ones return anyway
        if self.project.options.combined:
            # one request answers every missing check of a segment
            tasks = [(chapter, segment, None) for chapter, segment in
                     sorted({(chapter, segment) for chapter, segment, _ in removed}) if (chapter, segment) not in busy]
        else:
            tasks = [task for task in removed if task[:2] not in busy]
        if self.evaluating:
            queued = self.runner.enqueue(tasks)
            self.project_view.set_running(True)
            self.host.lock_controls(True)
            self.project_view.update_progress(self.runner.done, self.runner.total, self.runner.running,
                                              self.runner.eta_seconds())
        else:
            self.host.lock_controls(True)
            self.resume_runner()
            queued = len(tasks)
        self.host.set_status("Evaluating {0} check(s) again...".format(queued))
        return queued

    def toggle_pause(self):
        if self.evaluating:
            self.runner.cancel()
            self.project_view.set_pausing()
            self.host.set_status("Pausing after the running requests finish...")
        else:
            self.resume_runner()

    def checks_changed(self):
        """A check was enabled/disabled on the project screen."""
        if self.project is None:
            return
        self.project.save()
        self.project_view.refresh_all()
        if self.project.pending_tasks() and not self.evaluating:
            if messagebox.askyesno(
                "Evaluate now?",
                "The newly enabled check has not been evaluated for every segment yet. Start now?",
            ):
                self.host.lock_controls(True)
                self.resume_runner()

    def edit_options(self):
        """Open the review options (author's instructions) of the current project."""
        if self.project is None:
            return
        if self.evaluating:
            messagebox.showinfo("Evaluation running", "Pause the evaluation before changing the instructions.")
            return
        StyleGuideDialog(self, self.project.options.style_guide, on_save=self._apply_review_options,
                         glossary=self.project.options.glossary)

    def add_to_glossary(self, term):
        """Protect ``term`` from now on (called from a change card); returns whether it was new."""
        if self.project is None:
            return False
        term = " ".join((term or "").split())
        if not term or term in self.project.options.glossary:
            return False
        self.project.options.glossary.append(term)
        self.host.remember_glossary(self.project.options.glossary)
        self.schedule_save()
        self.host.set_status("Added \u201c{0}\u201d to the protected terms; it applies to segments evaluated from now on.".format(term))
        return True

    def _apply_review_options(self, style_guide, glossary):
        if self.project is None:
            return
        options = self.project.options
        unchanged = (
            normalise_style_guide(style_guide) == normalise_style_guide(options.style_guide)
            and list(glossary) == list(options.glossary)
        )
        options.style_guide = style_guide
        options.glossary = list(glossary)
        self.project.save()
        if unchanged:
            return
        self.host.remember_style_guide(style_guide)
        self.host.remember_glossary(options.glossary)
        has_results = any(segment.results for _, segment in self.project.all_segments())
        if has_results and messagebox.askyesno(
            "Re-evaluate?",
            "Re-evaluate all segments with the new instructions? "
            "(existing decisions on unchanged text are lost)",
        ):
            for _, segment in self.project.all_segments():
                segment.results.clear()
            self.running_tasks = set()
            self.project.save()
            self.project_view.refresh_all()
            self.project_view.render_segment()
            self.host.lock_controls(True)
            self.resume_runner()
            return
        self.project_view.refresh_all()
        self.host.set_status("Instructions saved; they apply to segments evaluated from now on.")

    def export_project(self):
        if self.project is None:
            return
        stats = self.project.progress()
        if stats["pending"]:
            if not messagebox.askyesno(
                "Pending changes",
                "{0} change(s) have no decision yet and will be left out (the original text is kept). "
                "Export anyway?".format(stats["pending"]),
            ):
                return
        try:
            paths = self.project.export()
            self.project.save()
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self.host.set_status("Exported to {0}".format(paths.get("formatted") or paths["document"]))
        message = "Reviewed manuscript:\n{0}\n\nChapter files:\n{1}\n\nReport:\n{2}".format(
            paths["document"], paths["document"].parent / "reviewed", paths["report"]
        )
        if paths.get("formatted"):
            message = "{0} ({1}):\n{2}\n\n{3}\n\n{4}".format(
                KIND_LABELS.get(self.project.document_kind, "Document"), paths["formatted"].suffix,
                paths["formatted"], FORMATTING_NOTE, message,
            )
        if paths["warnings"]:
            message += "\n\nLimitations:\n- " + "\n- ".join(paths["warnings"])
        messagebox.showinfo("Export complete", message)

    # -------------------------------------------------------- relay status
    def _schedule_status_poll(self, delay=RELAY_STATUS_INTERVAL_MS):
        self._cancel_status_poll()
        self._status_after = self.after(delay, self._poll_relay_status)

    def _cancel_status_poll(self):
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except tk.TclError:
                pass
            self._status_after = None

    def _poll_relay_status(self):
        """Fetch the relay's /status on a background thread while the runner is active.

        Only the remote backend has a status document; with Ollama nothing is
        shown. The answer arrives as ``("workflow_relay_status", status)``.
        """
        self._status_after = None
        if not self.evaluating:
            return
        service = self.host.get_service()
        fetch = getattr(service, "fetch_status", None)
        if fetch is None or getattr(service, "backend_id", "") != "remote":
            return
        events = self.host.events

        def worker():
            try:
                status = fetch()
            except Exception:
                status = None
            events.put(("workflow_relay_status", status))

        threading.Thread(target=worker, name="teai-relay-status", daemon=True).start()
        self._schedule_status_poll()

    def close_project(self):
        if self.project is None:
            return
        if self.evaluating:
            if not messagebox.askyesno("Stop evaluation?", "The evaluation is still running. Stop it and close the project?"):
                return
            self.runner.cancel()
        if self.checking_consistency:
            self.consistency_runner.cancel()
        self.consistency_runner = None
        self._save_now()
        self._cancel_status_poll()
        self.project = None
        self.runner = None
        for dialog in (self.decision_dialog, self.statistics_dialog):
            if dialog is not None and dialog.winfo_exists():
                dialog.destroy()
        self.decision_dialog = None
        self.statistics_dialog = None
        self.host.lock_controls(False)
        self.show_start()
        self.host.set_status("Project closed. Progress was saved; open it again any time.")

    def shutdown(self):
        """Called when the application closes."""
        if self.runner is not None:
            self.runner.cancel()
        if self.consistency_runner is not None:
            self.consistency_runner.cancel()
        self._save_now()

    # --------------------------------------------------------- consistency
    @property
    def checking_consistency(self):
        return self.consistency_runner is not None and self.consistency_runner.active

    def run_consistency(self):
        """Collect the deterministic candidates at once and ask the model about the doubtful ones."""
        if self.project is None:
            return
        if self.checking_consistency:
            self.host.set_status("The consistency check is already running.")
            return
        if self.evaluating or self.project.pending_tasks():
            messagebox.showinfo("Evaluation first",
                                "Run the consistency check once every segment has been evaluated; it reads the "
                                "manuscript with the accepted corrections applied.")
            return
        self.project.consistency = collect_candidates(self.project, previous=self.project.consistency)
        self.project_view.refresh_consistency()
        self.project_view.notebook.select(self.project_view.consistency_tab)
        self.schedule_save()
        service = self.host.get_service()
        model = self.host.get_model() or self.project.model
        self.consistency_runner = ConsistencyRunner(self.project, self.project.consistency, service, model,
                                                    self.host.events)
        requests = self.consistency_runner.start()
        undecided = sum(1 for f in self.project.consistency if f.kind in MODEL_TRIAGED and f.status == STATUS_UNVERIFIED)
        if requests:
            self.host.set_status("Found {0} candidate group(s); asking {1} about {2} of them in {3} request(s)...".format(
                len(self.project.consistency), model, undecided, requests))
            self.project_view.set_consistency_progress(0, requests)
        else:
            self.host.set_status("Consistency check: {0} finding(s), nothing left to ask the model.".format(
                len(self.project.consistency)))

    def cancel_consistency(self):
        if self.checking_consistency:
            self.consistency_runner.cancel()
            self.host.set_status("Stopping the consistency check after the running request...")

    # -------------------------------------------------------------- saving
    def schedule_save(self):
        if self.project is None:
            return
        if self._save_after is not None:
            try:
                self.after_cancel(self._save_after)
            except tk.TclError:
                pass
        self._save_after = self.after(1500, self._save_now)

    def _save_now(self):
        self._save_after = None
        if self.project is not None:
            try:
                self.project.save()
            except OSError as exc:
                self.host.set_status("Could not save project: {0}".format(exc))

    # -------------------------------------------------------------- events
    def handle_event(self, event):
        kind = event[0]
        if kind == "workflow_outline":
            project, text, starts, titles, error = event[1:]
            if error or len(starts) < 2:
                self.host.set_status(
                    "The model did not propose usable chapter boundaries{0}; using the automatic split."
                    .format(" (" + error + ")" if error else "")
                )
            else:
                apply_model_outline(project, text, starts, titles)
            self._launch(project)
            return True
        if self.project is None:
            return kind.startswith("workflow_")
        if kind == "workflow_started":
            _, chapter_index, segment_index, check = event
            self.running_tasks.add((chapter_index, segment_index, check))
            self.project_view.running = {(c, s) for c, s, _ in self.running_tasks}
            self.project_view.segment_updated(chapter_index, segment_index)
            return True
        if kind == "workflow_result":
            _, chapter_index, segment_index, check, result = event
            segment = apply_result(self.project, chapter_index, segment_index, check, result)
            self.running_tasks.discard((chapter_index, segment_index, check))
            self.project_view.running = {(c, s) for c, s, _ in self.running_tasks}
            if segment is not None:
                self.project_view.segment_updated(chapter_index, segment_index)
            self.schedule_save()
            return True
        if kind == "consistency_verdicts":
            updated = apply_verdicts(self.project.consistency, event[1])
            if updated:
                self.project_view.refresh_consistency()
                self.schedule_save()
            return True
        if kind == "consistency_progress":
            self.project_view.set_consistency_progress(event[1], event[2])
            return True
        if kind == "consistency_finished":
            _, verdicts, error = event
            self.consistency_runner = None
            self.project_view.set_consistency_progress(None, None)
            self.project_view.refresh_consistency()
            self._save_now()
            issues = sum(1 for finding in self.project.consistency if finding.status == STATUS_ISSUE)
            if error:
                self.host.set_status("Consistency check stopped: {0}. Unverified candidates stay listed.".format(error))
            else:
                self.host.set_status("Consistency check finished: {0} issue(s) among {1} finding(s).".format(
                    issues, len(self.project.consistency)))
            return True
        if kind == "workflow_progress":
            _, done, total, running = event
            eta = self.runner.eta_seconds() if self.runner else None
            self.project_view.update_progress(done, total, running, eta)
            return True
        if kind == "workflow_usage":
            self.project.add_usage(event[1])
            self.schedule_save()
            return True
        if kind == "workflow_relay_status":
            self.project_view.set_throughput(relay_throughput(event[1]) if self.evaluating else None)
            return True
        if kind == "workflow_finished":
            cancelled = event[1]
            if self.runner is not None and self.runner.has_work():
                return True  # stale: tasks were enqueued after this batch of workers retired
            self._cancel_status_poll()
            self.project_view.set_throughput(None)
            self.project_view.set_running(False)
            self.host.lock_controls(False)
            self._save_now()
            stats = self.project.progress()
            if cancelled:
                self.host.set_status("Evaluation paused. {0}".format(self.project_view.summary_text()))
            elif stats["error"]:
                self.host.set_status(
                    "Evaluation finished with {0} segment(s) in error. Use Retry to evaluate them again."
                    .format(stats["error"])
                )
            else:
                self.host.set_status("Evaluation complete. {0}".format(self.project_view.summary_text()))
            self.project_view.refresh_all()
            return True
        return False

    # ---------------------------------------------------------- shortcuts
    def accept_current(self, event=None):
        if self.active:
            self.project_view.decide_selected(ACCEPTED)
        return "break" if event else None

    def reject_current(self, event=None):
        if self.active:
            self.project_view.decide_selected(REJECTED)
        return "break" if event else None

    def undo(self, event=None):
        """Revert the last decision (Alt+Z) and show the change it belonged to."""
        if self.active:
            undone = self.project_view.undo()
            if undone is None:
                self.host.set_status("Nothing to undo.")
            else:
                self.host.set_status("Undid {0} decision(s).".format(len(undone)))
                self._refresh_decision_dialog()
        return "break" if event else None

    def edit_current(self, event=None):
        """F2: reword the selected suggestion."""
        if self.active:
            self.project_view.edit_selected()
        return "break" if event else None

    def add_author_correction(self, event=None):
        """Ctrl+E: turn the selected text of the segment into an author's correction."""
        if self.active:
            self.project_view.add_author_correction()
        return "break" if event else None

    def show_decisions(self):
        """Open (or raise) the decision log window."""
        if not self.active:
            return
        if self.decision_dialog is not None and self.decision_dialog.winfo_exists():
            self.decision_dialog.project = self.project
            self.decision_dialog.refresh()
            self.decision_dialog.lift()
            return
        self.decision_dialog = DecisionLogDialog(self, self.project, on_jump=self.project_view.show_change)

    def _refresh_decision_dialog(self):
        if self.decision_dialog is not None and self.decision_dialog.winfo_exists():
            self.decision_dialog.refresh()

    def show_statistics(self):
        """Open (or raise and refresh) the statistics window."""
        if not self.active:
            return
        if self.statistics_dialog is not None and self.statistics_dialog.winfo_exists():
            self.statistics_dialog.project = self.project
            self.statistics_dialog.refresh()
            self.statistics_dialog.lift()
            return
        self.statistics_dialog = StatisticsDialog(self, self.project, on_jump=self.project_view.show_change)

    def previous(self, event=None):
        if self.active:
            self.project_view.step_segment(-1)
        return "break" if event else None

    def next(self, event=None):
        if self.active:
            self.project_view.step_segment(1)
        return "break" if event else None

    def select_previous_change(self, event=None):
        if self.active:
            self.project_view.step_change(-1)
        return "break" if event else None

    def select_next_change(self, event=None):
        if self.active:
            self.project_view.step_change(1)
        return "break" if event else None


# ============================================================ start view
class StartView(ttk.Frame):
    """Choose the manuscript, the checks and the splitting options."""

    def __init__(self, parent, on_start, on_open):
        super().__init__(parent, padding=(24, 18))
        self.on_start = on_start
        self.on_open = on_open
        self.path = None
        self.text = ""
        self._build()

    def _card(self, parent, title, subtitle=None):
        card = ttk.Frame(parent, style="Card.TFrame", padding=14)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(card, text=subtitle, style="SurfaceMuted.TLabel", wraplength=640).pack(anchor="w", pady=(2, 8))
        return card

    def _build(self):
        ttk.Label(self, text="Automatic review", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text=("Choose a manuscript (.txt, .md, .docx or .odt). It is split into chapters and short segments, "
                  "each segment is checked for spelling, grammar and expression, and every proposed change comes "
                  "with a short explanation for you to accept or reject. Word and OpenDocument files are written "
                  "back in their format on export."),
            style="Muted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(2, 14))

        scroller = ScrollableFrame(self)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.inner
        body.columnconfigure(0, weight=1)

        # --- manuscript
        manuscript = self._card(body, "1. Manuscript")
        manuscript.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        row = ttk.Frame(manuscript, style="Surface.TFrame")
        row.pack(fill=tk.X)
        ttk.Button(row, text="Choose manuscript...", style="Accent.TButton", command=self.choose_file).pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value="No file selected")
        ttk.Label(row, textvariable=self.path_var, style="Surface.TLabel").pack(side=tk.LEFT, padx=12)
        self.info_var = tk.StringVar(value="")
        ttk.Label(manuscript, textvariable=self.info_var, style="SurfaceMuted.TLabel", wraplength=700,
                  justify=tk.LEFT).pack(anchor="w", pady=(8, 0))
        self.resume_frame = ttk.Frame(manuscript, style="Surface.TFrame")
        ttk.Label(self.resume_frame, text="A previous review of this file exists.", style="Surface.TLabel").pack(side=tk.LEFT)
        ttk.Button(self.resume_frame, text="Resume previous review", command=self.resume_existing).pack(side=tk.LEFT, padx=10)

        # --- checks
        checks = self._card(body, "2. Checks", "Each check runs independently on the original text; disable what you do not need. "
                            "Auto-accept applies a check's changes without asking (you can still undo them per change).")
        checks.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.check_vars = {}
        self.auto_vars = {}
        for check in CHECKS:
            row = ttk.Frame(checks, style="Surface.TFrame")
            row.pack(fill=tk.X, pady=2)
            self.check_vars[check] = tk.BooleanVar(value=True)
            ttk.Checkbutton(row, text=CHECK_LABELS[check], variable=self.check_vars[check],
                            style="Surface.TCheckbutton", width=12).pack(side=tk.LEFT)
            ttk.Label(row, text=CHECK_DESCRIPTIONS[check], style="SurfaceMuted.TLabel", wraplength=420).pack(side=tk.LEFT)
            self.auto_vars[check] = tk.BooleanVar(value=False)
            ttk.Checkbutton(row, text="auto-accept", variable=self.auto_vars[check],
                            style="Surface.TCheckbutton").pack(side=tk.RIGHT)

        # --- splitting & model
        options = self._card(body, "3. Splitting and evaluation")
        options.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        grid = ttk.Frame(options, style="Surface.TFrame")
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Chapters:", style="Surface.TLabel").grid(row=0, column=0, sticky="nw", pady=2)
        modes = ttk.Frame(grid, style="Surface.TFrame")
        modes.grid(row=0, column=1, sticky="w")
        self.chapter_mode = tk.StringVar(value="auto")
        for value, label in (
            ("auto", "Detect headings and scene breaks (fall back to size)"),
            ("size", "Split by size only"),
            ("model", "Ask the model to find chapter boundaries"),
            ("single", "Keep as one chapter"),
        ):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.chapter_mode,
                            style="Surface.TRadiobutton", command=self._update_preview).pack(anchor="w")

        ttk.Label(grid, text="Segment size:", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        size_row = ttk.Frame(grid, style="Surface.TFrame")
        size_row.grid(row=1, column=1, sticky="w", pady=(8, 2))
        self.target_var = tk.StringVar(value="1800")
        spin = ttk.Spinbox(size_row, from_=400, to=6000, increment=200, textvariable=self.target_var, width=7,
                           command=self._update_preview)
        spin.pack(side=tk.LEFT)
        spin.bind("<FocusOut>", lambda event: self._update_preview())
        ttk.Label(size_row, text="characters per segment (≈ 300 words at 1800). Smaller segments give more precise "
                  "explanations, larger ones need fewer requests.", style="SurfaceMuted.TLabel",
                  wraplength=460).pack(side=tk.LEFT, padx=8)

        ttk.Label(grid, text="Evaluation:", style="Surface.TLabel").grid(row=2, column=0, sticky="nw", pady=2)
        modes_row = ttk.Frame(grid, style="Surface.TFrame")
        modes_row.grid(row=2, column=1, sticky="w", pady=2)
        self.evaluation_mode = tk.StringVar(value=EVALUATION_COMBINED)
        for value in EVALUATION_MODES:
            ttk.Radiobutton(modes_row, text=EVALUATION_LABELS[value], value=value, variable=self.evaluation_mode,
                            style="Surface.TRadiobutton", command=self._update_preview).pack(anchor="w")
        ttk.Label(modes_row, text="Combined: one JSON answer per segment lists every category's edits with reasons. "
                  "Separate: each check edits the whole segment on its own and explanations are a second "
                  "request; also the automatic fallback when a combined answer cannot be used.",
                  style="SurfaceMuted.TLabel", wraplength=460).pack(anchor="w", pady=(2, 0))

        ttk.Label(grid, text="Parallel requests:", style="Surface.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        par_row = ttk.Frame(grid, style="Surface.TFrame")
        par_row.grid(row=3, column=1, sticky="w", pady=2)
        self.parallel_var = tk.StringVar(value="2")
        ttk.Spinbox(par_row, from_=1, to=8, textvariable=self.parallel_var, width=5).pack(side=tk.LEFT)
        ttk.Label(par_row, text="(1 for local Ollama, 2–4 for a GPU server behind the relay)",
                  style="SurfaceMuted.TLabel").pack(side=tk.LEFT, padx=8)

        ttk.Label(grid, text="Explanations:", style="Surface.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        expl_row = ttk.Frame(grid, style="Surface.TFrame")
        expl_row.grid(row=4, column=1, sticky="w", pady=2)
        self.explain_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(expl_row, text="Ask the model to explain each change", variable=self.explain_var,
                        style="Surface.TCheckbutton").pack(side=tk.LEFT)
        ttk.Label(expl_row, text="in", style="Surface.TLabel").pack(side=tk.LEFT, padx=(12, 4))
        self.language_var = tk.StringVar(value=SAME_LANGUAGE)
        ttk.Combobox(expl_row, textvariable=self.language_var, values=LANGUAGES, width=14).pack(side=tk.LEFT)

        # --- author's instructions and protected terms
        guide = self._card(body, "4. Author's instructions", STYLE_GUIDE_HINT)
        guide.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.style_guide_box = StyleGuideBox(guide, height=4)
        self.style_guide_box.pack(fill=tk.X)
        ttk.Label(guide, text="Protected terms", style="CardTitle.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Label(guide, text=GLOSSARY_HINT, style="SurfaceMuted.TLabel", wraplength=640).pack(anchor="w", pady=(2, 8))
        self.glossary_box = StyleGuideBox(guide, height=4, placeholder=GLOSSARY_PLACEHOLDER)
        self.glossary_box.pack(fill=tk.X)

        # --- actions
        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.start_button = ttk.Button(actions, text="Start automatic review", style="Accent.TButton",
                                       command=self.start, state=tk.DISABLED)
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="Open existing project...", command=self.open_existing).pack(side=tk.LEFT, padx=8)
        self.estimate_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.estimate_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=12)

    def refresh_defaults(self, host):
        """Suggest a parallelism that suits the backend and pre-fill the author's instructions."""
        try:
            self.parallel_var.set("1" if host.backend_id() == "ollama" else "2")
        except Exception:
            pass
        try:
            if not self.style_guide_box.get_text():
                self.style_guide_box.set(host.default_style_guide())
            if not self.glossary_box.get_text():
                self.glossary_box.set("\n".join(host.default_glossary()))
            mode = host.default_evaluation_mode()
            if mode in EVALUATION_MODES:
                self.evaluation_mode.set(mode)
        except Exception:
            pass

    # ------------------------------------------------------------ actions
    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a manuscript",
            filetypes=[("Manuscripts", MANUSCRIPT_PATTERNS), ("Text files", "*.txt *.md *.text *.markdown"),
                       ("Word documents", "*.docx"), ("OpenDocument text", "*.odt"), ("All files", "*.*")],
        )
        if path:
            self.set_file(path)

    def set_file(self, path):
        """Select a manuscript (also used by the quick editor's "Send to automatic review")."""
        try:
            self.text = load_document(path).text
        except (OSError, DocumentError) as exc:
            messagebox.showerror("Cannot read file", str(exc))
            return
        self.path = Path(path)
        self.path_var.set(self.path.name)
        self.start_button.configure(state=tk.NORMAL if self.text.strip() else tk.DISABLED)
        project_dir = self.path.with_name(self.path.stem + PROJECT_DIR_SUFFIX)
        if (project_dir / PROJECT_FILE).exists():
            self.resume_frame.pack(anchor="w", pady=(8, 0))
        else:
            self.resume_frame.pack_forget()
        self._update_preview()

    def options(self):
        try:
            target = max(400, min(6000, int(self.target_var.get())))
        except ValueError:
            target = 1800
        try:
            parallel = max(1, min(8, int(self.parallel_var.get())))
        except ValueError:
            parallel = 2
        return ProjectOptions(
            checks={check: bool(var.get()) for check, var in self.check_vars.items()},
            auto_accept={check: bool(var.get()) for check, var in self.auto_vars.items()},
            target_chars=target,
            max_chars=int(target * 1.7),
            chapter_mode=self.chapter_mode.get(),
            explain=bool(self.explain_var.get()),
            language=self.language_var.get().strip() or SAME_LANGUAGE,
            parallelism=parallel,
            style_guide=self.style_guide_box.get_text(),
            glossary=parse_glossary(self.glossary_box.get_text()),
            evaluation_mode=self.evaluation_mode.get(),
        )

    def _update_preview(self):
        if not self.text:
            self.info_var.set("")
            self.estimate_var.set("")
            return
        options = self.options()
        mode = "auto" if options.chapter_mode == "model" else options.chapter_mode
        result = split_document(self.text, mode=mode, target_chars=options.target_chars, max_chars=options.max_chars)
        segments = sum(len(chapter.segments) for chapter in result.chapters)
        words = word_count(self.text)
        method = {
            "single": "one chapter", "size": "split by size", "separators": "scene breaks", "model": "model",
        }.get(result.method, "headings")
        titles = ", ".join(chapter.title for chapter in result.chapters[:6])
        if len(result.chapters) > 6:
            titles += ", …"
        self.info_var.set(
            "{0:,} words · {1:,} characters · {2} chapter(s) ({3}) · {4} segment(s)\n{5}".format(
                words, len(self.text), len(result.chapters), method, segments, titles
            )
        )
        checks = len(options.enabled_checks())
        if options.combined:
            requests = segments
        else:
            requests = segments * checks * (2 if options.explain else 1)
        self.estimate_var.set("≈ {0} model requests".format(requests) if checks else "No check enabled")

    def start(self):
        if not self.path:
            return
        options = self.options()
        if not options.enabled_checks():
            messagebox.showinfo("No checks", "Enable at least one check.")
            return
        self.on_start(self.path, options)

    def resume_existing(self):
        if self.path:
            self.on_open(self.path.with_name(self.path.stem + PROJECT_DIR_SUFFIX))

    def open_existing(self):
        path = filedialog.askopenfilename(
            title="Open a review project", filetypes=[("TextEnhanceAI project", PROJECT_FILE), ("All files", "*.*")]
        )
        if path:
            self.on_open(path)


# ========================================================== project view
class AuthorFixDialog(tk.Toplevel):
    """Ask for the author's replacement of a selected span of the segment text."""

    def __init__(self, parent, original, on_save):
        super().__init__(parent)
        self.title("Add my correction")
        self.transient(parent.winfo_toplevel())
        self.resizable(True, False)
        self.on_save = on_save
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Selected text:", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=original.replace("\n", "↵") or "∅", wraplength=520, justify=tk.LEFT,
                  font=font(10, "bold")).grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Replace it with:", style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        self.var = tk.StringVar(value=original)
        self.entry = ttk.Entry(frame, textvariable=self.var, width=70)
        self.entry.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        ttk.Label(frame, text="Leave it empty to delete the selected text. The correction is applied with top "
                             "priority and counts as accepted; Alt+Z removes it again.",
                  style="Muted.TLabel", wraplength=520, justify=tk.LEFT).grid(row=4, column=0, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Add correction", style="Accent.TButton", command=self.save).pack(side=tk.RIGHT, padx=(0, 6))
        self.bind("<Return>", lambda event: self.save())
        self.bind("<Escape>", lambda event: self.destroy())
        self.entry.focus_set()
        self.entry.selection_range(0, tk.END)
        self.grab_set()

    def save(self):
        text = self.var.get()
        self.destroy()
        self.on_save(text)


class ChangeCard(ttk.Frame):
    """One proposed change with explanation and accept/reject buttons."""

    def __init__(self, parent, change, segment_text, state, on_select, on_decide, on_add_to_glossary=None,
                 on_edit=None):
        super().__init__(parent, style="Card.TFrame", padding=(10, 8), takefocus=1)
        self.change = change
        self.on_select = on_select
        self.on_decide = on_decide
        self.on_add_to_glossary = on_add_to_glossary
        self.on_edit = on_edit
        self.editor = None
        self.selected = False

        top = ttk.Frame(self, style="Surface.TFrame")
        top.pack(fill=tk.X)
        self.badge = ttk.Label(top, text=CHECK_LABELS[change.check], style="{0}.Badge.TLabel".format(change.check.title()))
        self.badge.pack(side=tk.LEFT)
        self.state_label = ttk.Label(top, text=state_text(state), style="{0}.State.TLabel".format(state.title()))
        self.state_label.pack(side=tk.LEFT, padx=8)
        self.kind_tag = ttk.Label(top, text=CHANGE_KIND_LABELS.get(change.kind, change.kind), style="Kind.Badge.TLabel")
        self.kind_tag.pack(side=tk.LEFT, padx=(0, 8))
        self.edited_tag = ttk.Label(top, text="{0} edited".format(DECISION_GLYPHS["edited"]), style="Kind.Badge.TLabel")
        if change.edited:
            self.edited_tag.pack(side=tk.LEFT, padx=(0, 8))
            Tooltip(self.edited_tag, "The model proposed: {0}".format(change.model_proposed_text or "∅"))
        self.flag_badge = None
        if change.flagged:
            # Hallucination guard: the reason ids explain themselves in the tooltip.
            self.flag_badge = ttk.Label(top, text="\u26a0 check this", style="Flag.Badge.TLabel")
            self.flag_badge.pack(side=tk.LEFT, padx=(0, 8))
            Tooltip(self.flag_badge, flag_tooltip(change))
        self.glossary_link = None
        if on_add_to_glossary is not None and change.original_text.strip() and not change.is_author:
            # A link-styled button (reachable with Tab): reject this change and protect the original wording.
            self.glossary_link = ttk.Button(top, text="Add to glossary", style="Link.TButton",
                                            command=self._add_to_glossary)
            self.glossary_link.pack(side=tk.LEFT, padx=(4, 0))
        self.reject_button = ttk.Button(top, text="Reject", style="Small.Danger.TButton",
                                        command=lambda: self.on_decide(self.change, REJECTED))
        self.reject_button.pack(side=tk.RIGHT)
        self.accept_button = ttk.Button(top, text="Accept", style="Small.Success.TButton",
                                        command=lambda: self.on_decide(self.change, ACCEPTED))
        self.accept_button.pack(side=tk.RIGHT, padx=(0, 6))
        self.edit_button = None
        if on_edit is not None:
            self.edit_button = ttk.Button(top, text="Edit…", style="Small.TButton", command=self.begin_edit)
            self.edit_button.pack(side=tk.RIGHT, padx=(0, 6))
            Tooltip(self.edit_button, "Reword this suggestion before accepting it (F2 on the selected card).")

        self.diff = tk.Text(self, height=2, cursor="arrow")
        style_text(self.diff, size=10)
        self.diff.configure(padx=6, pady=4, highlightthickness=0, spacing1=0, spacing3=0)
        self.diff.tag_configure("context", foreground=PALETTE["muted"])
        self.diff.tag_configure("removed", foreground=PALETTE["danger"], background=PALETTE["danger_soft"], overstrike=True)
        self.diff.tag_configure("added", foreground=PALETTE["success"], background=PALETTE["success_soft"], underline=True)
        self.diff.tag_configure("arrow", foreground=PALETTE["faint"])
        self._render_diff(segment_text)
        self.diff.pack(fill=tk.X, pady=(6, 4))

        self.explanation = ttk.Label(self, text=change.explanation or "", style="Explanation.TLabel",
                                     wraplength=520, justify=tk.LEFT)
        self.explanation.pack(anchor="w")

        for widget in (self, top, self.badge, self.state_label, self.diff, self.explanation):
            widget.bind("<Button-1>", self._clicked, add="+")
        self.bind("<FocusIn>", self._focused)  # Tab reaches the card; Alt+A / Alt+R then act on it
        self.refresh(state)

    def _render_diff(self, segment_text):
        change = self.change
        radius = 36
        before = segment_text[max(0, change.start - radius):change.start].replace("\n", " ")
        after = segment_text[change.end:change.end + radius].replace("\n", " ")
        if change.start - radius > 0:
            before = "…" + before
        if change.end + radius < len(segment_text):
            after = after + "…"
        self.diff.configure(state=tk.NORMAL)
        self.diff.delete("1.0", tk.END)
        self.diff.insert(tk.END, before, "context")
        if change.original_text:
            self.diff.insert(tk.END, change.original_text.replace("\n", "↵"), "removed")
        if change.original_text and change.proposed_text:
            self.diff.insert(tk.END, " → ", "arrow")
        if change.proposed_text:
            self.diff.insert(tk.END, change.proposed_text.replace("\n", "↵"), "added")
        elif not change.original_text:
            self.diff.insert(tk.END, "∅", "arrow")
        self.diff.insert(tk.END, after, "context")
        length = len(before) + len(change.original_text) + len(change.proposed_text) + len(after) + 3
        self.diff.configure(height=min(4, max(1, length // 70 + 1)), state=tk.DISABLED)

    def _clicked(self, event=None):
        self.on_select(self.change)
        try:
            self.focus_set()
        except tk.TclError:
            pass
        return "break"

    def _focused(self, event=None):
        if not self.selected:
            self.on_select(self.change)

    # ---- inline editing of the proposed text
    def begin_edit(self):
        """Show an entry with the proposed text; Enter saves through on_edit, Escape cancels."""
        if self.on_edit is None:
            return
        self.on_select(self.change)
        if self.editor is not None and self.editor.winfo_exists():
            self.edit_entry.focus_set()
            return
        self.editor = ttk.Frame(self, style="Selected.TFrame" if self.selected else "Surface.TFrame")
        self.editor.pack(fill=tk.X, pady=(2, 4), after=self.diff)
        self.edit_var = tk.StringVar(value=self.change.proposed_text)
        self.edit_entry = ttk.Entry(self.editor, textvariable=self.edit_var)
        self.edit_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(self.editor, text="Save", style="Small.Success.TButton", command=self._save_edit).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(self.editor, text="Cancel", style="Small.TButton", command=self.cancel_edit).pack(side=tk.LEFT, padx=(4, 0))
        self.edit_entry.bind("<Return>", lambda event: self._save_edit())
        self.edit_entry.bind("<Escape>", lambda event: self.cancel_edit())
        self.edit_entry.focus_set()
        self.edit_entry.selection_range(0, tk.END)

    def _save_edit(self):
        if self.editor is None:
            return
        text = self.edit_var.get()
        self.cancel_edit()
        self.on_edit(self.change, text)

    def cancel_edit(self):
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.destroy()
        self.editor = None

    def _add_to_glossary(self, event=None):
        if self.on_add_to_glossary is not None:
            self.on_add_to_glossary(self.change)
        return "break"

    def refresh(self, state, selected=None):
        if selected is not None:
            self.selected = selected
        self.state_label.configure(text=state_text(state), style="{0}.State.TLabel".format(state.title()))
        self.configure(style="Selected.Card.TFrame" if self.selected else "Card.TFrame")
        surface = PALETTE["selection"] if self.selected else PALETTE["surface"]
        self.diff.configure(background=surface)
        self.explanation.configure(style="SelectedExplanation.TLabel" if self.selected else "Explanation.TLabel")
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                child.configure(style="Selected.TFrame" if self.selected else "Surface.TFrame")
        self.accept_button.configure(state=tk.DISABLED if self.change.decision == ACCEPTED else tk.NORMAL)
        self.reject_button.configure(state=tk.DISABLED if self.change.decision == REJECTED else tk.NORMAL)


class ProjectView(ttk.Frame):
    """Chapter/segment navigation, highlighted text and change cards."""

    def __init__(self, parent, on_close, on_pause, on_export, on_retry, on_checks_changed, on_options=None,
                 on_add_to_glossary=None, on_reevaluate=None):
        super().__init__(parent, padding=(12, 8))
        self.on_close = on_close
        self.on_reevaluate = on_reevaluate or (lambda *args, **kwargs: 0)
        self.on_pause = on_pause
        self.on_export = on_export
        self.on_retry = on_retry
        self.on_checks_changed = on_checks_changed
        self.on_options = on_options or (lambda: None)
        self.on_add_to_glossary = on_add_to_glossary
        self.project = None
        self.current = None  # (chapter_index, segment_index)
        self.running = set()  # (chapter_index, segment_index) currently being evaluated
        self.cards = {}
        self.selected_change_id = None
        self.filter_vars = {check: tk.BooleanVar(value=True) for check in CHECKS}
        self.kind_vars = {kind: tk.BooleanVar(value=True) for kind in CHANGE_KINDS}  # True = shown
        self.preview_var = tk.BooleanVar(value=False)
        self.chapter_spans = []  # spans of the chapter tab, see render_chapter_annotated
        self.chapter_shown = None  # chapter index rendered in the chapter tab
        self._chapter_after = None
        self._list_after = None
        self.listed = {}  # all-changes tab: row iid -> (chapter_index, segment_index, change_id)
        self.throughput = None  # relay chunk rate / queue depth shown in the progress line, see set_throughput
        self._last_progress = None  # (done, total, running, eta) of the last update_progress call
        self.markers_var = tk.BooleanVar(value=False)  # chapter tab: [+]/[~]/[−] before every change span
        self._build()
        bind_restyle(self, self.restyle)

    def restyle(self):
        """Re-apply palette colours to the text areas and tree tags after a theme change."""
        style_text(self.text, size=11, readonly=True)
        style_text(self.chapter_text, size=11, readonly=True)
        self._configure_tags()
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)
        self.tree.column("#0", width=scaled(200))
        self.tree.column("status", width=scaled(170))
        self.after_idle(self._place_sash)
        self.list_tree.tag_configure("flagged", foreground=PALETTE["warning"])
        self.consistency_tree.tag_configure(STATUS_ISSUE, foreground=PALETTE["danger"])
        self.consistency_tree.tag_configure(STATUS_OK, foreground=PALETTE["success"])
        self.consistency_tree.tag_configure(STATUS_UNVERIFIED, foreground=PALETTE["warning"])
        self.consistency_tree.tag_configure(STATUS_DISMISSED, foreground=PALETTE["faint"])
        self.consistency_tree.tag_configure("occurrence", foreground=PALETTE["muted"])
        if self.project is not None:
            self.render_segment()
            self.schedule_chapter_render()

    def _place_sash(self):
        """Give the tree enough room for both columns at the current scale."""
        try:
            self.paned.sashpos(0, scaled(340))
        except tk.TclError:
            pass

    def _configure_tags(self):
        for check, (fg, bg) in CHECK_COLORS.items():
            self.text.tag_configure(check, background=bg, foreground=fg, underline=True)
        self.text.tag_configure("selected", background=PALETTE["accent_soft"], foreground=PALETTE["accent_dark"],
                                underline=True)
        self.text.tag_configure("rejected", background=PALETTE["surface"], underline=False, overstrike=False,
                                foreground=PALETTE["muted"])
        self.text.tag_raise("selected")
        self.text.tag_raise("sel")
        widget = self.chapter_text
        widget.tag_configure(STATE_APPLIED, foreground=PALETTE["success"], underline=True)
        widget.tag_configure(STATE_PENDING, background=PALETTE["warning_soft"], foreground=PALETTE["text"])
        widget.tag_configure(STATE_REJECTED, foreground=PALETTE["muted"], overstrike=True)
        widget.tag_configure(STATE_SUPERSEDED, foreground=PALETTE["muted"], underline=True)
        widget.tag_configure("flagged", foreground=PALETTE["warning"], underline=True)
        widget.tag_configure("current", background=PALETTE["accent_soft"])
        widget.tag_configure("marker", foreground=PALETTE["accent_dark"], font=font(11, "bold"))
        widget.tag_raise("current")
        widget.tag_raise("flagged")
        widget.tag_raise("marker")

    # ---------------------------------------------------------------- build
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.title_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.title_var, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.summary_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.summary_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(header)
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        self.pause_button = ttk.Button(buttons, text="Pause", command=self.on_pause)
        self.pause_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Options...", command=self.on_options).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="Decisions...", command=lambda: self.master.show_decisions()).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="Statistics...", command=lambda: self.master.show_statistics()).pack(side=tk.LEFT, padx=(6, 0))
        self.consistency_button = ttk.Button(buttons, text="Consistency...", command=lambda: self.master.run_consistency())
        self.consistency_button.pack(side=tk.LEFT, padx=(6, 0))
        Tooltip(self.consistency_button, "Look for names spelled two ways, hyphenation and number-style variants, "
                                         "mixed quotation marks and POV/tense drift across chapters. Available once "
                                         "every segment has been evaluated.")
        check_source = ttk.Button(buttons, text="Check source", command=lambda: self.master.check_source())
        check_source.pack(side=tk.LEFT, padx=(6, 0))
        Tooltip(check_source, "Compare the manuscript file with this project and re-sync when it changed: "
                              "unchanged segments keep their results and decisions.")
        ttk.Button(buttons, text="Export...", style="Accent.TButton", command=self.on_export).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Close project", style="Ghost.TButton", command=self.on_close).pack(side=tk.LEFT)

        progress_row = ttk.Frame(self)
        progress_row.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_var = tk.StringVar(value="")
        ttk.Label(progress_row, textvariable=self.progress_var, style="Muted.TLabel").grid(row=0, column=1, padx=(10, 0))

        checks_row = ttk.Frame(progress_row)
        checks_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(checks_row, text="Checks:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.check_vars = {}
        self.check_buttons = {}
        for check in CHECKS:
            self.check_vars[check] = tk.BooleanVar(value=True)
            button = ttk.Checkbutton(checks_row, text=CHECK_LABELS[check], variable=self.check_vars[check],
                                     command=lambda c=check: self._toggle_check(c))
            button.pack(side=tk.LEFT, padx=(8, 0))
            self.check_buttons[check] = button
        ttk.Label(checks_row, text="   Show:", style="Muted.TLabel").pack(side=tk.LEFT)
        for check in CHECKS:
            ttk.Checkbutton(checks_row, text=CHECK_LABELS[check], variable=self.filter_vars[check],
                            command=self.render_segment).pack(side=tk.LEFT, padx=(8, 0))
        self.kinds_button = ttk.Menubutton(checks_row, text="Kinds ▾", style="Small.TMenubutton")
        self.kinds_menu = tk.Menu(self.kinds_button, tearoff=False, postcommand=self._fill_kinds_menu)
        self.kinds_button.configure(menu=self.kinds_menu)
        self.kinds_button.pack(side=tk.LEFT, padx=(12, 0))
        Tooltip(self.kinds_button, "Hide kinds of edits from the review, or accept/reject every pending "
                                   "change of one kind across the project (Alt+Z reverts).")
        self.hint_var = tk.StringVar(value="Alt+A accept · Alt+R reject · Alt+Z undo · F2 edit · Ctrl+E my correction · "
                                           "Alt+↑/↓ change · Alt+←/→ segment")
        ttk.Label(checks_row, textvariable=self.hint_var, style="Muted.TLabel", font=font(9)).pack(side=tk.RIGHT)

        paned = self.paned = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=2, column=0, sticky="nsew")

        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("status",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Chapters and segments", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=200, stretch=True)
        self.tree.column("status", width=170, stretch=False)
        tree_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)
        self.tree.tag_configure("chapter", font=font(10, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.tree_menu = tk.Menu(self.tree, tearoff=False)
        self.tree.bind("<Button-3>", self._tree_context)
        self.tree.bind("<Button-2>", self._tree_context)  # macOS secondary click
        self.tree.bind("<App>", self._tree_context_key)
        self.tree.bind("<Menu>", self._tree_context_key)
        self.tree.bind("<Shift-F10>", self._tree_context_key)

        self.notebook = ttk.Notebook(paned)
        paned.add(self.notebook, weight=3)
        right = ttk.Frame(self.notebook)
        self.segment_tab = right
        self.notebook.add(right, text="Segment")
        self.chapter_tab = self._build_chapter_tab()
        self.notebook.add(self.chapter_tab, text="Chapter")
        self.list_tab = self._build_list_tab()
        self.notebook.add(self.list_tab, text="All changes")
        self.consistency_tab = self._build_consistency_tab()
        self.notebook.add(self.consistency_tab, text="Consistency")
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=2)
        right.rowconfigure(3, weight=3)

        seg_header = ttk.Frame(right)
        seg_header.grid(row=0, column=0, sticky="ew", padx=(10, 0))
        self.segment_var = tk.StringVar(value="Select a segment")
        ttk.Label(seg_header, textvariable=self.segment_var, style="TLabel", font=font(11, "bold")).pack(side=tk.LEFT)
        self.segment_status = ttk.Label(seg_header, text="", style="Status.TLabel")
        self.segment_status.pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(seg_header, text="Preview result", variable=self.preview_var, command=self.render_segment).pack(side=tk.RIGHT)
        self.reevaluate_button = ttk.Button(seg_header, text="Re-evaluate", style="Small.TButton",
                                            command=self.reevaluate_current)
        self.reevaluate_button.pack(side=tk.RIGHT, padx=(0, 10))
        Tooltip(self.reevaluate_button, "Discard this segment's results and decisions and run the enabled checks "
                                        "on it again. Right-click a row in the tree for one check or a whole chapter.")

        self.text = tk.Text(right, height=9)
        style_text(self.text, size=11, readonly=True)
        self.text.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(4, 6))
        self._configure_tags()  # both text areas exist now
        self.text_menu = tk.Menu(self.text, tearoff=False, postcommand=self._fill_text_menu)
        self.text.bind("<Button-3>", self._text_context)
        self.text.bind("<Button-2>", self._text_context)

        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, sticky="ew", padx=(10, 0), pady=(0, 4))
        ttk.Button(actions, text="◀ Previous", style="Small.TButton", command=lambda: self.step_segment(-1)).pack(side=tk.LEFT)
        ttk.Button(actions, text="Next ▶", style="Small.TButton", command=lambda: self.step_segment(1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(actions, text="Next to review", style="Small.TButton", command=self.jump_to_next_pending).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Reject all shown", style="Small.Danger.TButton", command=lambda: self.decide_all(REJECTED)).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Accept all shown", style="Small.Success.TButton", command=lambda: self.decide_all(ACCEPTED)).pack(side=tk.RIGHT, padx=(0, 6))
        self.undo_button = ttk.Button(actions, text="Undo", style="Small.TButton", command=lambda: self.master.undo())
        self.undo_button.pack(side=tk.RIGHT, padx=(0, 6))
        Tooltip(self.undo_button, "Revert the last accept/reject (a bulk action is reverted as a whole). Alt+Z")
        self.reject_flagged_button = ttk.Button(actions, text="Reject flagged \u26a0", style="Small.TButton",
                                                command=self.reject_flagged)
        self.reject_flagged_button.pack(side=tk.RIGHT, padx=(0, 6))
        self.retry_button = ttk.Button(actions, text="Retry failed", style="Small.TButton", command=self.on_retry)

        self.cards_frame = ScrollableFrame(right)
        self.cards_frame.grid(row=3, column=0, sticky="nsew", padx=(10, 0))

    def _build_chapter_tab(self):
        """Read-only rendering of the whole chapter with one tag per change state."""
        tab = ttk.Frame(self.notebook)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        header = ttk.Frame(tab)
        header.grid(row=0, column=0, sticky="ew", padx=(10, 0), pady=(4, 4))
        self.chapter_title_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.chapter_title_var, style="Heading.TLabel").pack(side=tk.LEFT)
        markers = ttk.Checkbutton(header, text="Show markers", variable=self.markers_var,
                                  command=self.render_chapter_view)
        markers.pack(side=tk.RIGHT)
        Tooltip(markers, "Put [+] before applied, [~] before pending and [−] before rejected or superseded "
                         "changes so the states can be told apart without colour.")
        legend = ttk.Frame(tab)
        legend.grid(row=1, column=0, sticky="w", padx=(10, 0), pady=(0, 4))
        for state, style in ((STATE_APPLIED, "Applied.State.TLabel"), (STATE_PENDING, "Pending.State.TLabel"),
                             (STATE_REJECTED, "Rejected.State.TLabel"), (STATE_SUPERSEDED, "Superseded.State.TLabel")):
            ttk.Label(legend, text="{0} {1}".format(SPAN_MARKERS[state], STATE_LABELS[state].lower()),
                      style=style).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(legend, text="{0} flagged".format(STATUS_GLYPHS["flagged"]), style="Flag.Badge.TLabel").pack(
            side=tk.LEFT)
        Tooltip(legend, "Click a highlighted passage to open its change in the Segment tab; "
                        "Alt+A / Alt+R then decide on it.")
        self.chapter_text = tk.Text(tab, height=20)
        style_text(self.chapter_text, size=11, readonly=True)
        self.chapter_text.grid(row=2, column=0, sticky="nsew", padx=(10, 0))
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.chapter_text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.chapter_text.configure(yscrollcommand=scroll.set)
        self.chapter_text.bind("<Button-1>", self._chapter_clicked)
        self.chapter_text.configure(cursor="hand2")
        return tab

    def _build_list_tab(self):
        """Every pending change of the project in one table with filters and bulk buttons."""
        tab = ttk.Frame(self.notebook)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        filters = ttk.Frame(tab)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=(4, 4))
        ttk.Label(filters, text="Check:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.list_check_var = tk.StringVar(value="All")
        ttk.Combobox(filters, textvariable=self.list_check_var, state="readonly", width=12,
                     values=["All"] + [CHECK_LABELS[check] for check in CHECKS] + [CHECK_LABELS[CHECK_AUTHOR]]
                     ).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(filters, text="Kind:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.list_kind_var = tk.StringVar(value="All")
        ttk.Combobox(filters, textvariable=self.list_kind_var, state="readonly", width=14,
                     values=["All"] + [CHANGE_KIND_LABELS[kind] for kind in CHANGE_KINDS]).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(filters, text="Text:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.list_text_var = tk.StringVar(value="")
        ttk.Entry(filters, textvariable=self.list_text_var, width=18).pack(side=tk.LEFT, padx=(4, 10))
        for var in (self.list_check_var, self.list_kind_var, self.list_text_var):
            var.trace_add("write", lambda *args: self.schedule_list_refresh())
        self.list_count_var = tk.StringVar(value="")
        ttk.Label(filters, textvariable=self.list_count_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        columns = ("chapter", "segment", "check", "kind", "change")
        self.list_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="extended")
        for key, heading, width, stretch in (("chapter", "Chapter", 140, False), ("segment", "Seg.", 50, False),
                                             ("check", "Check", 90, False), ("kind", "Kind", 100, False),
                                             ("change", "Original → proposed", 360, True)):
            self.list_tree.heading(key, text=heading, anchor="w")
            self.list_tree.column(key, width=width, stretch=stretch, anchor="w")
        self.list_tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0))
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.list_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.list_tree.configure(yscrollcommand=scroll.set)
        self.list_tree.tag_configure("flagged", foreground=PALETTE["warning"])
        self.list_tree.bind("<Double-1>", self._list_jump)
        self.list_tree.bind("<Return>", self._list_jump)
        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=(4, 0))
        ttk.Label(actions, text="Alt+A / Alt+R decide on the selected rows; double-click opens the segment.",
                  style="Muted.TLabel", font=font(9)).pack(side=tk.LEFT)
        ttk.Button(actions, text="Reject selected", style="Small.Danger.TButton",
                   command=lambda: self.decide_listed(REJECTED)).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Accept selected", style="Small.Success.TButton",
                   command=lambda: self.decide_listed(ACCEPTED)).pack(side=tk.RIGHT, padx=(0, 6))
        return tab

    def _build_consistency_tab(self):
        """Findings of the chapter-spanning consistency check with their occurrences."""
        tab = ttk.Frame(self.notebook)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        header = ttk.Frame(tab)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=(4, 4))
        self.consistency_summary_var = tk.StringVar(value="No consistency check has run yet.")
        ttk.Label(header, textvariable=self.consistency_summary_var, style="Muted.TLabel").pack(side=tk.LEFT)
        self.show_dismissed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="Show dismissed", variable=self.show_dismissed_var,
                        command=self.refresh_consistency).pack(side=tk.RIGHT)
        self.consistency_tree = ttk.Treeview(tab, columns=("kind", "preferred", "status", "reason"),
                                             show="tree headings", selectmode="browse")
        self.consistency_tree.heading("#0", text="Variants (count) / occurrences", anchor="w")
        self.consistency_tree.column("#0", width=300, stretch=True)
        for key, heading, width in (("kind", "Type", 110), ("preferred", "Preferred", 110),
                                    ("status", "Status", 90), ("reason", "Reason", 260)):
            self.consistency_tree.heading(key, text=heading, anchor="w")
            self.consistency_tree.column(key, width=width, stretch=(key == "reason"), anchor="w")
        self.consistency_tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0))
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.consistency_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.consistency_tree.configure(yscrollcommand=scroll.set)
        self.consistency_tree.tag_configure(STATUS_ISSUE, foreground=PALETTE["danger"])
        self.consistency_tree.tag_configure(STATUS_OK, foreground=PALETTE["success"])
        self.consistency_tree.tag_configure(STATUS_UNVERIFIED, foreground=PALETTE["warning"])
        self.consistency_tree.tag_configure(STATUS_DISMISSED, foreground=PALETTE["faint"])
        self.consistency_tree.tag_configure("occurrence", foreground=PALETTE["muted"])
        self.consistency_tree.bind("<Double-1>", self._consistency_jump)
        self.consistency_tree.bind("<Return>", self._consistency_jump)
        self.consistency_tree.bind("<<TreeviewSelect>>", lambda event: self._update_consistency_buttons())
        self.consistency_rows = {}  # iid -> ("finding", finding) or ("occurrence", finding, occurrence)
        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(10, 0), pady=(4, 0))
        ttk.Button(actions, text="Run check", style="Small.TButton",
                   command=lambda: self.master.run_consistency()).pack(side=tk.LEFT)
        self.consistency_stop = ttk.Button(actions, text="Stop", style="Small.TButton",
                                           command=lambda: self.master.cancel_consistency())
        self.consistency_progress_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.consistency_progress_var, style="Muted.TLabel",
                  font=font(9)).pack(side=tk.LEFT, padx=(10, 0))
        self.dismiss_button = ttk.Button(actions, text="Dismiss", style="Small.TButton", command=self.dismiss_finding)
        self.dismiss_button.pack(side=tk.RIGHT)
        self.apply_button = ttk.Button(actions, text="Apply preferred everywhere", style="Small.Success.TButton",
                                       command=self.apply_finding)
        self.apply_button.pack(side=tk.RIGHT, padx=(0, 6))
        Tooltip(self.apply_button, "Replace every other variant by the preferred form as author corrections "
                                   "(one undo step). Double-click an occurrence to look at it first.")
        self._update_consistency_buttons()
        return tab

    # ------------------------------------------------------- consistency
    def set_consistency_progress(self, done, total):
        if done is None:
            self.consistency_progress_var.set("")
            self.consistency_stop.pack_forget()
        else:
            self.consistency_progress_var.set("Asking the model... {0}/{1} request(s)".format(done, total))
            self.consistency_stop.pack(side=tk.LEFT, padx=(6, 0))

    def refresh_consistency(self):
        tree = self.consistency_tree
        tree.delete(*tree.get_children())
        self.consistency_rows = {}
        if self.project is None:
            return
        findings = self.project.consistency
        show_dismissed = self.show_dismissed_var.get()
        counts = {}
        for finding in findings:
            counts[finding.status] = counts.get(finding.status, 0) + 1
        if not findings:
            self.consistency_summary_var.set("No consistency check has run yet." if not self.project.pending_tasks()
                                             else "Available once every segment has been evaluated.")
        else:
            self.consistency_summary_var.set(
                "{0} finding(s): {1} issue(s), {2} intentional, {3} unverified, {4} dismissed".format(
                    len(findings), counts.get(STATUS_ISSUE, 0), counts.get(STATUS_OK, 0),
                    counts.get(STATUS_UNVERIFIED, 0), counts.get(STATUS_DISMISSED, 0)))
        order = {STATUS_ISSUE: 0, STATUS_UNVERIFIED: 1, STATUS_OK: 2, STATUS_DISMISSED: 3}
        for number, finding in enumerate(sorted(findings, key=lambda f: (order.get(f.status, 9), -f.total))):
            if finding.status == STATUS_DISMISSED and not show_dismissed:
                continue
            iid = "f{0}".format(number)
            label = ", ".join("{0} ({1})".format(variant, finding.counts.get(variant, 0)) for variant in finding.variants)
            tree.insert("", tk.END, iid=iid, text=label, open=False, tags=(finding.status,), values=(
                FINDING_LABELS.get(finding.kind, finding.kind), finding.preferred,
                FINDING_STATUS_LABELS.get(finding.status, finding.status),
                finding.reason or finding.detail))
            self.consistency_rows[iid] = ("finding", finding)
            for variant in finding.variants:
                for position, occurrence in enumerate(finding.occurrences.get(variant, [])):
                    chapter, _ = self.project.find(occurrence["chapter"], occurrence["segment"])
                    child = "{0}-{1}-{2}".format(iid, variant, position)
                    tree.insert(iid, tk.END, iid=child, tags=("occurrence",),
                                text="{0} · {1} · segment {2}".format(
                                    variant, chapter.title if chapter else occurrence["chapter"], occurrence["segment"]),
                                values=("", "", "", _one_line(occurrence.get("text", ""), 80)))
                    self.consistency_rows[child] = ("occurrence", finding, occurrence)
        self._update_consistency_buttons()

    def _selected_finding(self):
        selection = self.consistency_tree.selection()
        row = self.consistency_rows.get(selection[0]) if selection else None
        return row[1] if row else None

    def _update_consistency_buttons(self):
        finding = self._selected_finding()
        self.apply_button.configure(state=tk.NORMAL if finding is not None and finding.applicable
                                    and finding.status != STATUS_DISMISSED else tk.DISABLED)
        if finding is None:
            self.dismiss_button.configure(text="Dismiss", state=tk.DISABLED)
        else:
            self.dismiss_button.configure(text="Restore" if finding.status == STATUS_DISMISSED else "Dismiss",
                                          state=tk.NORMAL)

    def _consistency_jump(self, event=None):
        selection = self.consistency_tree.selection()
        row = self.consistency_rows.get(selection[0]) if selection else None
        if row is None:
            return "break"
        if row[0] == "occurrence":
            occurrence = row[2]
        else:
            finding = row[1]
            occurrence = next((items[0] for variant in finding.variants
                               for items in [finding.occurrences.get(variant, [])] if items), None)
        if occurrence is not None:
            self.show_span(occurrence["chapter"], occurrence["segment"], occurrence["start"], occurrence["end"])
        return "break"

    def show_span(self, chapter_index, segment_index, start, end):
        """Open a segment in the Segment tab and select ``[start, end)`` of its text."""
        if self.project is None or self.project.find(chapter_index, segment_index)[1] is None:
            return
        self.preview_var.set(False)
        self.select_segment(chapter_index, segment_index)
        self.notebook.select(self.segment_tab)
        self.text.tag_remove("sel", "1.0", tk.END)
        self.text.tag_add("sel", "1.0+{0}c".format(start), "1.0+{0}c".format(max(end, start + 1)))
        self.text.see("1.0+{0}c".format(start))

    def dismiss_finding(self):
        finding = self._selected_finding()
        if finding is None:
            return
        finding.status = STATUS_UNVERIFIED if finding.status == STATUS_DISMISSED else STATUS_DISMISSED
        self.refresh_consistency()
        self.master.schedule_save()

    def apply_finding(self):
        finding = self._selected_finding()
        if finding is None or not finding.applicable:
            return
        others = [variant for variant in finding.variants if variant != finding.preferred]
        total = sum(finding.counts.get(variant, 0) for variant in others)
        if not messagebox.askyesno(
                "Apply preferred spelling?",
                "Replace {0} occurrence(s) of {1} by “{2}” as your own corrections?\n\nAlt+Z reverts them all."
                .format(total, " / ".join("“{0}”".format(v) for v in others), finding.preferred)):
            return
        count, _ = apply_preferred(self.project, finding)
        self.project.consistency = collect_candidates(self.project, previous=self.project.consistency)
        self.refresh_all()
        self.refresh_consistency()
        self._after_decision()
        self.master.host.set_status("Wrote {0} author correction(s) for “{1}”. Alt+Z reverts them.".format(
            count, finding.preferred))

    # -------------------------------------------------------- chapter tab
    def _tab_changed(self, event=None):
        if self.project is None:
            return
        tab = self.notebook.select()
        if tab == str(self.chapter_tab):
            self.render_chapter_view()
        elif tab == str(self.list_tab):
            self.refresh_list()
        elif tab == str(self.consistency_tab):
            self.refresh_consistency()

    def _tab_visible(self, tab):
        try:
            return self.notebook.select() == str(tab)
        except tk.TclError:
            return False

    def schedule_chapter_render(self):
        """Re-render the chapter tab shortly (coalesces bursts of decisions and results)."""
        if self._chapter_after is not None:
            try:
                self.after_cancel(self._chapter_after)
            except tk.TclError:
                pass
        self._chapter_after = self.after(150, self.render_chapter_view)

    def render_chapter_view(self):
        self._chapter_after = None
        if self.project is None or not self._tab_visible(self.chapter_tab):
            return
        chapter, segment = self._current_segment()
        if chapter is None:
            chapter = self.project.chapters[0] if self.project.chapters else None
        if chapter is None:
            return
        text, spans = render_chapter_annotated(self.project, chapter)
        counts = {}
        for span in spans:
            counts[span["state"]] = counts.get(span["state"], 0) + 1
        self.chapter_title_var.set("{0}. {1} · {2} words · {3}".format(
            chapter.index, chapter.title, word_count(text),
            ", ".join("{0} {1}".format(counts[state], STATE_LABELS[state].lower())
                      for state in (STATE_PENDING, STATE_APPLIED, STATE_REJECTED, STATE_SUPERSEDED) if counts.get(state))
            or "no changes"))
        marked = bool(self.markers_var.get())
        if marked:
            text, spans = mark_spans(text, spans)
        self.chapter_spans = spans  # in the coordinates of the shown text (markers included)
        self.chapter_shown = chapter.index
        widget = self.chapter_text
        top = widget.yview()[0]
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        for span in spans:
            start = "1.0+{0}c".format(span["start"])
            end = "1.0+{0}c".format(span["end"] if span["end"] > span["start"] else span["start"] + 1)
            widget.tag_add(span["state"], start, end)
            if span["flags"]:
                widget.tag_add("flagged", start, end)
            if segment is not None and span["segment"] == segment.index and span["change_id"] == self.selected_change_id:
                widget.tag_add("current", start, end)
            if marked and span["state"] in SPAN_MARKERS:
                marker = SPAN_MARKERS[span["state"]]
                widget.tag_add("marker", "1.0+{0}c".format(span["start"] - len(marker)), start)
        widget.configure(state=tk.DISABLED)
        widget.yview_moveto(top)

    def _chapter_clicked(self, event):
        if self.project is None or not self.chapter_spans:
            return "break"
        index = self.chapter_text.index("@{0},{1}".format(event.x, event.y))
        counted = self.chapter_text.count("1.0", index, "chars")
        offset = counted[0] if counted else 0
        hit = None
        for span in self.chapter_spans:
            end = span["end"] if span["end"] > span["start"] else span["start"] + 1
            if span["start"] <= offset < end:
                hit = span
                if span["state"] == STATE_PENDING:
                    break  # prefer the change that still needs a decision
        if hit is None:
            return "break"
        self.show_change(self.chapter_shown, hit["segment"], hit["change_id"])
        self.notebook.select(self.segment_tab)
        return "break"

    # --------------------------------------------------- all-changes tab
    def schedule_list_refresh(self):
        if self._list_after is not None:
            try:
                self.after_cancel(self._list_after)
            except tk.TclError:
                pass
        self._list_after = self.after(150, self.refresh_list)

    def _list_filters(self):
        check = self.list_check_var.get()
        kind = self.list_kind_var.get()
        labels_to_check = {label: key for key, label in CHECK_LABELS.items()}
        labels_to_kind = {label: key for key, label in CHANGE_KIND_LABELS.items()}
        return labels_to_check.get(check), labels_to_kind.get(kind), self.list_text_var.get().strip().casefold()

    def refresh_list(self):
        self._list_after = None
        if self.project is None or not self._tab_visible(self.list_tab):
            return
        check, kind, needle = self._list_filters()
        keep = {iid for iid in self.list_tree.selection()}
        self.list_tree.delete(*self.list_tree.get_children())
        self.listed = {}
        total = 0
        for chapter, segment, change in pending_changes(self.project):
            total += 1
            if check and change.check != check:
                continue
            if kind and change.kind != kind:
                continue
            if needle and needle not in (change.original_text + " " + change.proposed_text).casefold():
                continue
            iid = "l{0}-{1}-{2}".format(chapter.index, segment.index, change.change_id)
            self.list_tree.insert("", tk.END, iid=iid, tags=("flagged",) if change.flagged else (), values=(
                chapter.title, segment.index, CHECK_LABELS[change.check], CHANGE_KIND_LABELS[change.kind],
                "{0} → {1}{2}".format(_one_line(change.original_text) or "∅",
                                          _one_line(change.proposed_text) or "∅",
                                          "  ⚠" if change.flagged else ""),
            ))
            self.listed[iid] = (chapter.index, segment.index, change.change_id)
        shown = len(self.listed)
        self.list_count_var.set("{0} of {1} pending change(s)".format(shown, total) if shown != total
                                else "{0} pending change(s)".format(total))
        still = [iid for iid in keep if iid in self.listed]
        if still:
            self.list_tree.selection_set(still)

    def _list_jump(self, event=None):
        selection = self.list_tree.selection()
        if selection and selection[0] in self.listed:
            chapter_index, segment_index, change_id = self.listed[selection[0]]
            self.show_change(chapter_index, segment_index, change_id)
            self.notebook.select(self.segment_tab)
        return "break"

    def decide_listed(self, decision):
        """Accept or reject the rows selected in the all-changes tab as one undo group."""
        if self.project is None:
            return 0
        targets = [self.listed[iid] for iid in self.list_tree.selection() if iid in self.listed]
        if not targets:
            self.master.host.set_status("Select one or more rows in the list first.")
            return 0
        group = new_decision_group() if len(targets) > 1 else None
        count = 0
        for chapter_index, segment_index, change_id in targets:
            _, segment = self.project.find(chapter_index, segment_index)
            change = segment.find_change(change_id) if segment is not None else None
            if change is not None and self.project.decide(chapter_index, segment_index, change, decision, group=group):
                count += 1
        self.refresh_all()
        self._after_decision()
        self.master.host.set_status("{0} change(s) {1}. Alt+Z reverts them.".format(
            count, "accepted" if decision == ACCEPTED else "rejected"))
        return count

    # ------------------------------------------------------------- loading
    def load_project(self, project):
        self.project = project
        self.title_var.set(project.name)
        for check in CHECKS:
            self.check_vars[check].set(bool(project.options.checks.get(check)))
        for kind in CHANGE_KINDS:
            self.kind_vars[kind].set(kind not in project.options.hidden_kinds)
        self.tree.delete(*self.tree.get_children())
        for chapter in project.chapters:
            chapter_id = "c{0}".format(chapter.index)
            self.tree.insert("", tk.END, iid=chapter_id, text="{0}. {1}".format(chapter.index, chapter.title),
                             values=("",), open=True, tags=("chapter",))
            for segment in chapter.segments:
                self.tree.insert(chapter_id, tk.END, iid=self._segment_iid(chapter.index, segment.index),
                                 text="Segment {0} · {1} words".format(segment.index, word_count(segment.text)),
                                 values=("",))
        self.current = None
        self.chapter_shown = None
        self.chapter_spans = []
        self.notebook.select(self.segment_tab)
        self.set_consistency_progress(None, None)
        self.refresh_consistency()
        self.refresh_all()
        first = self._first_segment_with(lambda status: status == STATUS_READY) or self._first_segment_with(lambda status: True)
        if first:
            self.select_segment(*first)

    @staticmethod
    def _segment_iid(chapter_index, segment_index):
        return "s{0}-{1}".format(chapter_index, segment_index)

    def _first_segment_with(self, predicate, after=None):
        found_start = after is None
        for chapter, segment in self.project.all_segments():
            key = (chapter.index, segment.index)
            if not found_start:
                if key == after:
                    found_start = True
                continue
            if predicate(segment.status(self.project.enabled)):
                return key
        return None

    # -------------------------------------------------------------- state
    def set_running(self, running):
        if not running:
            self.running = set()
        if running:
            self.pause_button.configure(text="Pause", state=tk.NORMAL)
        else:
            pending = bool(self.project and self.project.pending_tasks())
            self.pause_button.configure(text="Resume" if pending else "Evaluation complete",
                                        state=tk.NORMAL if pending else tk.DISABLED)
        self.refresh_all()

    def set_pausing(self):
        self.pause_button.configure(text="Pausing...", state=tk.DISABLED)

    def update_progress(self, done, total, running, eta):
        self._last_progress = (done, total, running, eta)
        if total:
            self.progress.configure(value=100.0 * done / total)
        else:
            self.progress.configure(value=100)
        parts = ["{0}/{1} checks evaluated".format(done, total)]
        if running:
            parts.append("{0} running".format(running))
        eta_text = _format_eta(eta)
        if eta_text and done < total:
            parts.append(eta_text)
        throughput = _format_throughput(self.throughput)
        if throughput:
            parts.append(throughput)
        self.progress_var.set(" · ".join(parts))

    def set_throughput(self, throughput):
        """Show (or clear, with None) the relay's chunk rate and queue depth in the progress line."""
        self.throughput = throughput
        if self._last_progress is not None:
            self.update_progress(*self._last_progress)

    def summary_text(self):
        stats = self.project.progress()
        return "{0} segments · {1} changes: {2} accepted, {3} rejected, {4} pending{5}".format(
            stats["segments"], stats["changes"], stats["accepted"], stats["rejected"], stats["pending"],
            _suppressed_note(stats),
        )

    def _summary_line(self, stats):
        return (
            "{0} chapters · {1} segments · {2:,} words   |   {3} changes proposed · {4} accepted · {5} rejected · "
            "{6} pending{7}"
        ).format(len(self.project.chapters), stats["segments"], stats["words"], stats["changes"],
                 stats["accepted"], stats["rejected"], stats["pending"],
                 _flagged_note(stats) + _suppressed_note(stats))

    def refresh_all(self):
        if self.project is None:
            return
        stats = self.project.progress()
        self.summary_var.set(self._summary_line(stats))
        for check in CHECKS:
            per = stats["per_check"][check]
            self.check_buttons[check].configure(text="{0} ({1})".format(CHECK_LABELS[check], per["changes"]))
        for chapter, segment in self.project.all_segments():
            self._refresh_tree_row(chapter, segment)
        if stats["error"]:
            self.retry_button.pack(side=tk.LEFT, padx=(8, 0))
        else:
            self.retry_button.pack_forget()
        self.consistency_button.configure(
            state=tk.NORMAL if not self.running and not self.project.pending_tasks() else tk.DISABLED)
        if self.current:
            self.render_segment()
        self.schedule_chapter_render()
        self.schedule_list_refresh()

    def _segment_status(self, chapter, segment):
        status = segment.status(self.project.enabled)
        if status == STATUS_QUEUED and (chapter.index, segment.index) in self.running:
            return "running"
        return status

    def _refresh_tree_row(self, chapter, segment):
        status = self._segment_status(chapter, segment)
        iid = self._segment_iid(chapter.index, segment.index)
        changes = segment.changes(self.project.enabled)
        pending = sum(1 for change in changes if change.decision == PENDING)
        detail = ""
        if status == STATUS_READY:
            detail = "{0} pending".format(pending)
        elif status == STATUS_REVIEWED:
            detail = "{0} changes".format(len(changes))
        label = status_text(status, detail)
        if any(change.flagged and change.decision == PENDING for change in changes):
            label = STATUS_GLYPHS["flagged"] + " " + label  # pending changes the hallucination guard flagged
        self.tree.item(iid, values=(label,), tags=(status,))

    def segment_updated(self, chapter_index, segment_index):
        chapter, segment = self.project.find(chapter_index, segment_index)
        if segment is None:
            return
        self._refresh_tree_row(chapter, segment)
        stats = self.project.progress()
        for check in CHECKS:
            per = stats["per_check"][check]
            self.check_buttons[check].configure(text="{0} ({1})".format(CHECK_LABELS[check], per["changes"]))
        if self.current == (chapter_index, segment_index):
            self.render_segment()
        if self.current and self.current[0] == chapter_index:
            self.schedule_chapter_render()
        self.schedule_list_refresh()

    def _toggle_check(self, check):
        if self.project is None:
            return
        self.project.options.checks[check] = bool(self.check_vars[check].get())
        self.on_checks_changed()

    # -------------------------------------------------------- re-evaluate
    def _tree_context(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return "break"
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._post_tree_menu(iid, event.x_root, event.y_root)
        return "break"

    def _tree_context_key(self, event=None):
        iid = self.tree.focus() or (self.tree.selection() or [None])[0]
        if not iid:
            return "break"
        x, y, width, height = self.tree.bbox(iid) or (0, 0, 0, 0)
        self._post_tree_menu(iid, self.tree.winfo_rootx() + x + width // 3, self.tree.winfo_rooty() + y + height)
        return "break"

    def _post_tree_menu(self, iid, x, y):
        if self.project is None:
            return
        menu = self.tree_menu
        menu.delete(0, tk.END)
        if iid.startswith("s"):
            chapter_index, segment_index = (int(part) for part in iid[1:].split("-"))
            _, segment = self.project.find(chapter_index, segment_index)
            if segment is None or segment.is_blank:
                return
            submenu = tk.Menu(menu, tearoff=False)
            submenu.add_command(label="All checks", command=lambda: self.on_reevaluate(chapter_index, segment_index))
            submenu.add_separator()
            for check in self.project.options.enabled_checks():
                submenu.add_command(label=CHECK_LABELS[check],
                                    command=lambda c=check: self.on_reevaluate(chapter_index, segment_index, [c]))
            submenu.add_separator()
            submenu.add_command(label="Whole chapter", command=lambda: self.on_reevaluate(chapter_index))
            menu.add_cascade(label="Evaluate again", menu=submenu)
        else:
            chapter_index = int(iid[1:])
            menu.add_command(label="Evaluate chapter again", command=lambda: self.on_reevaluate(chapter_index))
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def reevaluate_current(self):
        chapter, segment = self._current_segment()
        if segment is None or segment.is_blank:
            return
        if self.project.decision_log and any(
                change.decision != PENDING for change in segment.changes(self.project.enabled)):
            if not messagebox.askyesno("Evaluate again?",
                                       "Discard the results and decisions of this segment and run the checks again?"):
                return
        self.on_reevaluate(chapter.index, segment.index)

    # -------------------------------------------------------------- kinds
    def _fill_kinds_menu(self):
        """Rebuild the Kinds menu with current counts every time it opens."""
        menu = self.kinds_menu
        menu.delete(0, tk.END)
        if self.project is None:
            return
        stats = self.project.progress()
        menu.add_command(label="Show kinds", state=tk.DISABLED)
        for kind in CHANGE_KINDS:
            menu.add_checkbutton(
                label="{0} ({1})".format(CHANGE_KIND_LABELS[kind], stats["per_kind"][kind]["changes"]),
                variable=self.kind_vars[kind], onvalue=True, offvalue=False,
                command=lambda k=kind: self._toggle_kind(k),
            )
        menu.add_separator()
        for decision, label in ((ACCEPTED, "Accept all…"), (REJECTED, "Reject all…")):
            submenu = tk.Menu(menu, tearoff=False)
            for kind in CHANGE_KINDS:
                pending = len(self.project.changes_by_kind(kind, decision=PENDING))
                submenu.add_command(
                    label="{0} ({1} pending)".format(CHANGE_KIND_LABELS[kind], pending),
                    state=tk.NORMAL if pending else tk.DISABLED,
                    command=lambda k=kind, d=decision: self.decide_kind_everywhere(k, d),
                )
            menu.add_cascade(label=label, menu=submenu)

    def _toggle_kind(self, kind):
        if self.project is None:
            return
        hidden = [k for k in CHANGE_KINDS if not self.kind_vars[k].get()]
        self.project.options.hidden_kinds = hidden
        self.kinds_button.configure(text="Kinds ▾" if not hidden else "Kinds ({0} hidden) ▾".format(len(hidden)))
        self.master.schedule_save()
        self.render_segment()

    def decide_kind_everywhere(self, kind, decision):
        """Accept or reject every pending change of ``kind`` in the project (one undo group)."""
        if self.project is None:
            return
        pending = self.project.changes_by_kind(kind, decision=PENDING)
        label = CHANGE_KIND_LABELS[kind].lower()
        verb = "Accept" if decision == ACCEPTED else "Reject"
        if not pending:
            self.master.host.set_status("No pending {0} changes.".format(label))
            return
        if not messagebox.askyesno(
            "{0} all {1} changes?".format(verb, label),
            "{0} {1} pending {2} change(s) across the whole project?\n\nAlt+Z (Undo) reverts them all at once."
            .format(verb, len(pending), label),
        ):
            return
        entries = self.project.decide_kind(kind, decision)
        self.refresh_all()
        self._after_decision()
        self.master.host.set_status("{0}ed {1} {2} change(s). Alt+Z reverts them.".format(
            verb, len(entries), label))

    # ------------------------------------------------------------ segment
    def _tree_selected(self, event=None):
        selection = self.tree.selection()
        if not selection:
            return
        iid = selection[0]
        if iid.startswith("s"):
            chapter_index, segment_index = (int(part) for part in iid[1:].split("-"))
            if (chapter_index, segment_index) == self.current:
                # Either a click on the row already shown or the echo of our own
                # selection_set(): re-rendering would drop the selected change.
                return
            self.select_segment(chapter_index, segment_index, from_tree=True)

    def select_segment(self, chapter_index, segment_index, from_tree=False, change_id=None):
        self.current = (chapter_index, segment_index)
        self.selected_change_id = change_id
        if self.chapter_shown != chapter_index:
            self.schedule_chapter_render()
        if not from_tree:
            iid = self._segment_iid(chapter_index, segment_index)
            try:
                self.tree.selection_set(iid)
                self.tree.see(iid)
            except tk.TclError:
                pass
        self.render_segment()

    def _current_segment(self):
        if self.project is None or self.current is None:
            return None, None
        return self.project.find(*self.current)

    def _visible_changes(self, segment):
        enabled = dict(self.project.enabled)
        for check in CHECKS:
            if not self.filter_vars[check].get():
                enabled[check] = False
        return segment.changes(enabled, hidden_kinds=self.project.options.hidden_kinds)

    def render_segment(self):
        chapter, segment = self._current_segment()
        if segment is None:
            return
        enabled = self.project.enabled
        status = self._segment_status(chapter, segment)
        changes = self._visible_changes(segment)
        states = change_states(segment, enabled)
        pending = sum(1 for change in changes if change.decision == PENDING)
        suppressed = sum(
            len(result.suppressed) for check, result in segment.results.items()
            if enabled.get(check) and result.status == "done"
        )
        self.segment_var.set("{0} · Segment {1} of {2} · {3} words".format(
            chapter.title, segment.index, len(chapter.segments), word_count(segment.text)))
        self.segment_status.configure(
            text="{0}{1}".format(
                status_text(status, "{0} pending".format(pending) if pending else ""),
                " · {0} suppressed by glossary".format(suppressed) if suppressed else "",
            ),
            style="{0}.Status.TLabel".format(status.title()),
        )
        if self.selected_change_id and not any(c.change_id == self.selected_change_id for c in changes):
            self.selected_change_id = None
        if self.selected_change_id is None:
            first_pending = next((c for c in changes if c.decision == PENDING), None)
            self.selected_change_id = first_pending.change_id if first_pending else (changes[0].change_id if changes else None)

        # --- text with highlights
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        if self.preview_var.get():
            self.text.insert("1.0", render_segment(segment, enabled))
        else:
            self.text.insert("1.0", segment.text)
            for change in changes:
                start = "1.0+{0}c".format(change.start)
                end = "1.0+{0}c".format(change.end if change.end > change.start else change.start + 1)
                tag = "rejected" if states[change.change_id] == STATE_REJECTED else change.check
                self.text.tag_add(tag, start, end)
                if change.change_id == self.selected_change_id:
                    self.text.tag_add("selected", start, end)
                    self.text.see(start)
        self.text.configure(state=tk.DISABLED)

        self.reject_flagged_button.configure(
            state=tk.NORMAL if any(c.flagged and c.decision == PENDING for c in changes) else tk.DISABLED
        )

        # --- cards
        self.cards_frame.clear()
        self.cards = {}
        if not changes:
            message = {
                STATUS_QUEUED: "Waiting for the evaluation of this segment...",
                "running": "Evaluating...",
                STATUS_ERROR: "The evaluation failed for this segment:\n" + "\n".join(
                    "• {0}: {1}".format(CHECK_LABELS[c], r.error)
                    for c, r in segment.results.items() if r.status == "error" and enabled.get(c)),
                STATUS_CLEAN: "No changes proposed for this segment." if not segment.is_blank else "Blank segment.",
            }.get(status, "No changes to show with the current filters.")
            ttk.Label(self.cards_frame.inner, text=message, style="Muted.TLabel", wraplength=560,
                      justify=tk.LEFT, padding=(12, 16)).pack(anchor="w")
            return
        author_changes = [change for change in changes if change.is_author]
        model_changes = [change for change in changes if not change.is_author]
        if author_changes:
            ttk.Label(self.cards_frame.inner, text="Author's corrections", style="Muted.TLabel",
                      font=font(9, "bold")).pack(anchor="w", pady=(2, 4))
        for change in author_changes + model_changes:
            if model_changes and author_changes and change is model_changes[0]:
                ttk.Label(self.cards_frame.inner, text="Suggested by the checks", style="Muted.TLabel",
                          font=font(9, "bold")).pack(anchor="w", pady=(6, 4))
            card = ChangeCard(self.cards_frame.inner, change, segment.text, states[change.change_id],
                              on_select=self._card_selected, on_decide=self.decide,
                              on_add_to_glossary=self.add_to_glossary if self.on_add_to_glossary else None,
                              on_edit=None if change.is_author else self.edit_change)
            card.pack(fill=tk.X, padx=(0, 4), pady=(0, 6))
            card.refresh(states[change.change_id], selected=change.change_id == self.selected_change_id)
            self.cards[change.change_id] = card
        selected = self.cards.get(self.selected_change_id)
        if selected is not None:
            self.after_idle(lambda: self.cards_frame.scroll_to_widget(selected))

    def _card_selected(self, change):
        self.selected_change_id = change.change_id
        self._refresh_cards_only()

    def _refresh_cards_only(self):
        chapter, segment = self._current_segment()
        if segment is None:
            return
        states = change_states(segment, self.project.enabled)
        for change_id, card in self.cards.items():
            card.refresh(states.get(change_id, STATE_PENDING), selected=change_id == self.selected_change_id)
        self.text.configure(state=tk.NORMAL)
        self.text.tag_remove("selected", "1.0", tk.END)
        for change_id, card in self.cards.items():
            change = card.change
            start = "1.0+{0}c".format(change.start)
            end = "1.0+{0}c".format(change.end if change.end > change.start else change.start + 1)
            if states.get(change_id) == STATE_REJECTED:
                self.text.tag_remove(change.check, start, end)
                self.text.tag_add("rejected", start, end)
            else:
                self.text.tag_remove("rejected", start, end)
                self.text.tag_add(change.check, start, end)
            if change_id == self.selected_change_id:
                self.text.tag_add("selected", start, end)
                self.text.see(start)
        self.text.configure(state=tk.DISABLED)
        self._refresh_tree_row(chapter, segment)
        pending = sum(1 for change in self._visible_changes(segment) if change.decision == PENDING)
        status = self._segment_status(chapter, segment)
        self.segment_status.configure(
            text=status_text(status, "{0} pending".format(pending) if pending else ""),
            style="{0}.Status.TLabel".format(status.title()),
        )

    # ---------------------------------------------------------- decisions
    def decide(self, change, decision, group=None):
        chapter, segment = self._current_segment()
        if segment is None:
            return
        self.project.decide(chapter.index, segment.index, change, decision, group=group)
        self.selected_change_id = change.change_id
        self._refresh_cards_only()
        self._after_decision()
        # move on to the next pending change for a fast keyboard flow
        self.step_change(1, pending_only=True)

    def edit_change(self, change, new_text):
        """Store the author's wording for ``change`` and accept it (from the card's Edit box)."""
        chapter, segment = self._current_segment()
        if segment is None:
            return
        entry = self.project.edit_change(chapter.index, segment.index, change, new_text)
        self.selected_change_id = change.change_id
        self.render_segment()
        if entry is not None:
            self._after_decision()
            self.master.host.set_status("Suggestion reworded and accepted. Alt+Z restores the model's wording.")

    def edit_selected(self):
        """F2: open the inline editor of the selected card."""
        card = self.cards.get(self.selected_change_id)
        if card is not None and not card.change.is_author:
            card.begin_edit()

    # ---- the author's own corrections
    def _fill_text_menu(self):
        menu = self.text_menu
        menu.delete(0, tk.END)
        has_selection = bool(self.text.tag_ranges("sel"))
        menu.add_command(label="Add my correction…   Ctrl+E", command=self.add_author_correction,
                         state=tk.NORMAL if has_selection and not self.preview_var.get() else tk.DISABLED)
        menu.add_command(label="Copy", command=self._copy_selection, state=tk.NORMAL if has_selection else tk.DISABLED)

    def _text_context(self, event):
        try:
            self.text_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.text_menu.grab_release()
        return "break"

    def _copy_selection(self):
        if self.text.tag_ranges("sel"):
            self.clipboard_clear()
            self.clipboard_append(self.text.get("sel.first", "sel.last"))

    def selected_span(self):
        """Return (start, end) offsets of the selection in the segment text, or None."""
        if not self.text.tag_ranges("sel"):
            return None
        counted = self.text.count("1.0", "sel.first", "chars")
        start = counted[0] if counted else 0
        counted = self.text.count("sel.first", "sel.last", "chars")
        end = start + (counted[0] if counted else 0)
        return start, end

    def add_author_correction(self):
        """Ctrl+E: replace the selected span of the ORIGINAL segment text with the author's wording."""
        chapter, segment = self._current_segment()
        if segment is None:
            return
        if self.preview_var.get():
            self.master.host.set_status("Switch off “Preview result” to add a correction to the original text.")
            return
        span = self.selected_span()
        if span is None or span[0] == span[1]:
            self.master.host.set_status("Select the text to correct first, then press Ctrl+E.")
            return
        start, end = span
        original = segment.text[start:end]

        def save(new_text, chapter_index=chapter.index, segment_index=segment.index):
            change = self.project.add_author_change(chapter_index, segment_index, start, end, new_text)
            if change is None:
                self.master.host.set_status("The correction equals the original text; nothing added.")
                return
            self.selected_change_id = change.change_id
            self.render_segment()
            self._after_decision()
            self.master.host.set_status("Your correction was added and applied. Alt+Z removes it.")

        AuthorFixDialog(self, original, on_save=save)

    def add_to_glossary(self, change):
        """Reject ``change`` and protect its original wording (the "Add to glossary" link)."""
        self.on_add_to_glossary(change.original_text)
        self.decide(change, REJECTED)

    def decide_selected(self, decision):
        if self._tab_visible(self.list_tab):
            self.decide_listed(decision)
            return
        card = self.cards.get(self.selected_change_id)
        if card is not None:
            self.decide(card.change, decision)

    def decide_all(self, decision):
        chapter, segment = self._current_segment()
        if segment is None:
            return
        group = new_decision_group()
        for change in self._visible_changes(segment):
            self.project.decide(chapter.index, segment.index, change, decision, group=group)
        self.render_segment()
        self._after_decision()

    def reject_flagged(self):
        """Reject every shown change the hallucination guard flagged that is still pending."""
        chapter, segment = self._current_segment()
        if segment is None:
            return
        flagged = [c for c in self._visible_changes(segment) if c.flagged and c.decision == PENDING]
        group = new_decision_group()
        for change in flagged:
            self.project.decide(chapter.index, segment.index, change, REJECTED, group=group)
        if flagged:
            self.render_segment()
            self._after_decision()

    def undo(self):
        """Revert the last decision (group) and show the first change it touched.

        Returns the reverted log entries, or None when the log was empty.
        """
        if self.project is None:
            return None
        undone = self.project.undo()
        if not undone:
            return None
        first = undone[0]
        self.show_change(first["chapter"], first["segment"], first["change_id"])
        self._after_decision()
        return undone

    def show_change(self, chapter_index, segment_index, change_id):
        """Select ``change_id`` in its segment and scroll it into view."""
        if self.project is None or self.project.find(chapter_index, segment_index)[1] is None:
            return
        self.select_segment(chapter_index, segment_index, change_id=change_id)
        card = self.cards.get(change_id) if change_id else None
        if card is not None:
            self.cards_frame.scroll_to_widget(card)

    def _after_decision(self):
        self.summary_var.set(self._summary_line(self.project.progress()))
        chapter, segment = self._current_segment()
        if segment is not None:
            self._refresh_tree_row(chapter, segment)
        self.master.schedule_save()
        self.schedule_chapter_render()
        self.schedule_list_refresh()

    def step_change(self, delta, pending_only=False):
        ids = list(self.cards.keys())
        if not ids:
            return
        if self.selected_change_id not in ids:
            self.selected_change_id = ids[0]
        else:
            index = ids.index(self.selected_change_id)
            candidates = ids[index + 1:] if delta > 0 else list(reversed(ids[:index]))
            if pending_only:
                candidates = [cid for cid in candidates if self.cards[cid].change.decision == PENDING]
            if not candidates:
                if pending_only:
                    return
                candidates = [ids[index]]
            self.selected_change_id = candidates[0]
        self._refresh_cards_only()
        card = self.cards.get(self.selected_change_id)
        if card is not None:
            self.cards_frame.scroll_to_widget(card)

    def step_segment(self, delta):
        if self.project is None:
            return
        keys = [(chapter.index, segment.index) for chapter, segment in self.project.all_segments()]
        if not keys:
            return
        if self.current not in keys:
            target = keys[0]
        else:
            index = keys.index(self.current) + delta
            if not 0 <= index < len(keys):
                return
            target = keys[index]
        self.select_segment(*target)

    def jump_to_next_pending(self):
        if self.project is None:
            return
        target = self._first_segment_with(lambda status: status == STATUS_READY, after=self.current)
        if target is None:
            target = self._first_segment_with(lambda status: status == STATUS_READY)
        if target is None:
            self.master.host.set_status("No segment is waiting for a decision.")
            return
        self.select_segment(*target)
