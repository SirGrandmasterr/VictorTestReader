"""Main Tkinter application for TextEnhanceAI."""

import platform
import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from core import RELEASES_URL, __version__
from core.backend import EditCancelled, OutputTruncated, add_usage, empty_usage, format_usage
from core.diff_engine import build_edit_session, render_reviewed_text
from core.documents import (
    FORMATTED_KINDS,
    KIND_SUFFIXES,
    MANUSCRIPT_PATTERNS,
    DocumentError,
    kind_for,
    load_document,
    save_document,
    text_document,
)
from core.editing import explain_session, run_chain
from core.ollama_service import OllamaService
from core.paths import ensure_dir, migrate_legacy_settings, user_data_dir
from core.prompts import EDITING_MODES, PROMPTS, build_chain, build_instruction, describe_chain, validate_custom_modes
from core.scratchpad import ScratchpadLogger
from core.services import build_service
from core.settings import (
    BACKEND_LABELS,
    BACKEND_OLLAMA,
    BACKEND_REMOTE,
    SETTINGS_FILENAME,
    UI_SCALE_STEP,
    AppSettings,
    clamp_ui_scale,
)
from core.text_positions import char_offset, normalise_span, tk_index
from core.workflow import CHECK_LABELS, STREAM_EVENT
from .connection_dialog import ConnectionDialog
from .dialogs import ShortcutsDialog, ask_name
from .i18n import N_, current_language, format_number, resolve_language, set_language, tr
from .mode_dialog import ManageModesDialog
from .review_panel import ReviewPanel
from .theme import Toast, Tooltip, apply_theme, font, style_text, subscribe
from .thinking_window import StreamLog, ThinkingWindow
from .workflow_screen import WorkflowScreen

# connection label states (colour plus a glyph, see _set_connection)
COLOR_OK = "ok"
COLOR_WARN = "warn"
COLOR_ERROR = "error"
COLOR_NEUTRAL = "neutral"
CONNECTION_GLYPHS = {COLOR_OK: "\u25cf", COLOR_WARN: "\u25c6", COLOR_ERROR: "\u25a0", COLOR_NEUTRAL: "\u25cc"}
MODE_QUICK = "quick"
MODE_AUTO = "auto"
APP_TITLE = "TextEnhanceAI - V {0}".format(__version__)
APPLIED_HISTORY_LIMIT = 20  # applied reviews that can be undone in the quick editor
# (label, pattern) pairs of the file dialogs; the labels are translated by file_types()
FILE_TYPES = [
    (N_("Documents"), MANUSCRIPT_PATTERNS),
    (N_("Text files"), "*.txt *.md *.text *.markdown"),
    (N_("Word documents"), "*.docx"),
    (N_("OpenDocument text"), "*.odt"),
    (N_("All files"), "*.*"),
]
# Built-in editing modes are shown translated; the PROMPTS keys stay the identifiers (see _mode_key).
MODE_LABELS = {
    "Grammar": N_("Grammar"),
    "Proofread": N_("Proofread"),
    "Natural": N_("Natural"),
    "Streamline": N_("Streamline"),
    "Awkward": N_("Awkward"),
    "Rewrite": N_("Rewrite"),
    "Concise": N_("Concise"),
    "Polish": N_("Polish"),
    "Improve": N_("Improve"),
    "Translate": N_("Translate"),
    "Custom": N_("Custom"),
}


def file_types():
    return [(tr(label), pattern) for label, pattern in FILE_TYPES]


def _format_duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return tr("{seconds} s", seconds=seconds)
    if seconds < 3600:
        return tr("{minutes} min {seconds:02d} s", minutes=seconds // 60, seconds=seconds % 60)
    return tr("{hours} h {minutes:02d} min", hours=seconds // 3600, minutes=(seconds % 3600) // 60)


def window_title(path, modified):
    """Title bar text: the file name (or Untitled) with a bullet while there are unsaved changes."""
    if path is None and not modified:
        return APP_TITLE
    name = Path(path).name if path else tr("Untitled")
    return "{0}{1} \u2014 TextEnhanceAI".format(name, " \u2022" if modified else "")


class EditorApp:
    """Coordinate editing, local or remote generation, and structured review."""

    def __init__(
        self,
        root,
        app_directory=None,
        ollama_service=None,
        remote_service=None,
        settings=None,
        data_dir=None,
    ):
        """``app_directory`` is where the script lives (legacy settings location);
        ``data_dir`` is where settings and scratchpads go (default: ``core.paths.user_data_dir``)."""
        self.root = root
        self.app_directory = Path(app_directory or Path.cwd())
        self.data_dir = ensure_dir(Path(data_dir) if data_dir is not None else user_data_dir())
        self.startup_notice = ""
        if settings is None:
            migrated = migrate_legacy_settings(self.app_directory, self.data_dir)
            if migrated is not None:
                self.startup_notice = tr("Settings migrated to {migrated}", migrated=migrated)
            settings = AppSettings.load(self.data_dir / SETTINGS_FILENAME)
        self.settings = settings
        set_language(self.settings.ui_language)  # before any widget text is built
        self.services = {
            BACKEND_OLLAMA: ollama_service or OllamaService(),
            BACKEND_REMOTE: remote_service or self._remote_from_settings(),
        }
        self.service = self.services[self.settings.backend]
        self.events = queue.Queue()
        self.cancel_event = None
        self.active_request_id = 0
        self.active_revision_id = None
        self.revision_id = 0
        self.generating = False
        self.generation_started_at = None
        self.generation_label = ""
        self.generation_verb = tr("Generating review")  # or "Editing selection (N words)"
        self._progress_chars = 0
        self.current_session = None
        self.current_logger = None
        self.applied_history = []  # (before, after, mode label) of every applied review, oldest first
        self.redo_history = []
        self._suppress_modified = False
        self.mode = MODE_QUICK
        self._controls_locked = False
        self.last_custom_instruction = ""  # offered by "Save as preset..." after a Custom request
        self.current_path = None  # quick-editor file (Path) or None while untitled
        self.current_document = None  # documents.LoadedDocument the editor text came from
        self.modified = False
        self.session_usage = empty_usage()  # token counts of every request since the app started
        self._connection = ("", COLOR_NEUTRAL)  # last message and state of the connection label
        self.thinking_log = StreamLog()  # reasoning streamed by the last requests (View > Model thinking)
        self.thinking_window = None
        self.shortcuts_window = None

        self.root.title(APP_TITLE)
        self.root.geometry("1120x780")
        self.root.minsize(900, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._configure_style()
        self.toast = Toast(self.root)
        self._build_menu()
        self._build_interface()
        self._bind_shortcuts()
        if self.settings.load_error:
            self.set_status(self.settings.load_error)
        elif self.startup_notice:
            self.set_status(self.startup_notice)
        self.root.after(100, self._poll_events)
        self.root.after(150, self.refresh_models)

    # --------------------------------------------------------------- services
    def _remote_from_settings(self):
        return build_service(self.settings, BACKEND_REMOTE)

    def _configure_style(self):
        apply_theme(self.root, high_contrast=self.settings.high_contrast, scale=self.settings.ui_scale)
        subscribe(self._restyle)

    def _restyle(self):
        """Re-apply palette colours to the plain Tk widgets after a theme change."""
        if not hasattr(self, "text_area"):
            return
        style_text(self.text_area, size=12, serif=True)
        self.text_area.configure(state=tk.DISABLED if self.generating else tk.NORMAL)
        self._set_connection(*self._connection)

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.file_menu = tk.Menu(menubar, tearoff=False)
        self.file_menu.add_command(label=tr("Open..."), underline=0, accelerator="Ctrl+O", command=self.open_file)
        self.file_menu.add_command(label=tr("Save"), underline=0, accelerator="Ctrl+S", command=self.save_file)
        self.file_menu.add_command(label=tr("Save As..."), underline=5, accelerator="Ctrl+Shift+S", command=self.save_file_as)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=False)
        self.file_menu.add_cascade(label=tr("Recent"), underline=0, menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label=tr("Send to automatic review"), underline=8, command=self.send_to_review)
        self.file_menu.add_separator()
        self.file_menu.add_command(label=tr("Quit"), underline=0, command=self.close)
        menubar.add_cascade(label=tr("File"), underline=0, menu=self.file_menu)
        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(label=tr("Larger text"), underline=0, accelerator="Ctrl++", command=lambda: self.zoom(1))
        view_menu.add_command(label=tr("Smaller text"), underline=0, accelerator="Ctrl+-", command=lambda: self.zoom(-1))
        view_menu.add_command(label=tr("Normal text size"), underline=0, accelerator="Ctrl+0", command=lambda: self.zoom(0))
        view_menu.add_separator()
        view_menu.add_command(label=tr("Model thinking..."), underline=6, accelerator="Ctrl+Shift+T",
                              command=self.show_thinking)
        view_menu.add_separator()
        self.high_contrast_var = tk.BooleanVar(value=bool(self.settings.high_contrast))
        view_menu.add_checkbutton(label=tr("High contrast"), underline=0, variable=self.high_contrast_var,
                                  command=self.toggle_high_contrast)
        menubar.add_cascade(label=tr("View"), underline=0, menu=view_menu)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label=tr("Keyboard shortcuts..."), underline=0, accelerator="F1", command=self.show_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label=tr("Releases on GitHub"), underline=0, command=self.open_releases)
        help_menu.add_separator()
        help_menu.add_command(label=tr("About TextEnhanceAI"), underline=0, command=self.show_about)
        menubar.add_cascade(label=tr("Help"), underline=0, menu=help_menu)
        self.root.config(menu=menubar)
        self._rebuild_recent_menu()

    # ------------------------------------------------------------ view menu
    def zoom(self, direction):
        """Scale every font up (1), down (-1) or back to 100 % (0); persisted as ``ui_scale``."""
        if direction == 0:
            scale = 1.0
        else:
            scale = clamp_ui_scale(self.settings.ui_scale + direction * UI_SCALE_STEP)
        if scale == self.settings.ui_scale and direction != 0:
            self.set_status(tr("Text size is already at {scale:.0f} %.", scale=scale * 100))
            return "break"
        self.settings.ui_scale = scale
        apply_theme(self.root, scale=scale)
        self._save_settings()
        self.set_status(tr("Text size {scale:.0f} %.", scale=scale * 100))
        return "break"

    def toggle_high_contrast(self, enabled=None):
        """Switch between the normal and the high-contrast palette (View menu)."""
        if enabled is None:
            enabled = bool(self.high_contrast_var.get())
        else:
            self.high_contrast_var.set(bool(enabled))
        self.settings.high_contrast = bool(enabled)
        apply_theme(self.root, high_contrast=self.settings.high_contrast)
        self._save_settings()
        self.set_status(tr("High contrast on.") if enabled else tr("High contrast off."))
        return "break"

    # ------------------------------------------------------------------ help
    @staticmethod
    def open_releases():
        webbrowser.open(RELEASES_URL)

    def about_text(self):
        return tr(
            "TextEnhanceAI {version}\n\n"
            "Local and self-hosted LLM editing for authors.\n\n"
            "Python {python} \u00b7 Tk {tk}\n"
            "Settings and scratchpads: {directory}\n\n"
            "New versions are published on the Releases page; this app does not update itself.",
            version=__version__, python=platform.python_version(), tk=self.root.tk.call("info", "patchlevel"),
            directory=self.data_dir,
        )

    def show_shortcuts(self, event=None):
        if self.shortcuts_window is not None and self.shortcuts_window.winfo_exists():
            self.shortcuts_window.lift()
        else:
            self.shortcuts_window = ShortcutsDialog(self.root)
        return "break" if event else None

    def show_about(self):
        dialog = tk.Toplevel(self.root)
        dialog.title(tr("About TextEnhanceAI"))
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="TextEnhanceAI", font=font(16, serif=True)).pack(anchor="w")
        ttk.Label(body, text=self.about_text(), justify=tk.LEFT, wraplength=420).pack(anchor="w", pady=(6, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text=tr("Close"), command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text=tr("Releases"), command=self.open_releases, style="Primary.TButton").pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.grab_set()
        return dialog

    def _rebuild_recent_menu(self):
        self.recent_menu.delete(0, tk.END)
        for entry in self.settings.recent_files:
            self.recent_menu.add_command(label=self._recent_label(entry), command=lambda p=entry: self.open_file(p))
        if self.settings.recent_files:
            self.recent_menu.add_separator()
            self.recent_menu.add_command(label=tr("Clear list"), command=self._clear_recent)
        else:
            self.recent_menu.add_command(label=tr("(no recent files)"), state=tk.DISABLED)

    @staticmethod
    def _recent_label(entry):
        path = Path(entry)
        parent = str(path.parent)
        if len(parent) > 40:
            parent = "\u2026" + parent[-39:]
        return "{0}  ({1})".format(path.name, parent)

    def _clear_recent(self):
        self.settings.recent_files = []
        self._save_settings()
        self._rebuild_recent_menu()

    def _build_interface(self):
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="TextEnhanceAI", style="Header.TLabel").pack(side=tk.LEFT)
        # The two workflows as a segmented toggle; the tooltips say what each one is for.
        segment = ttk.Frame(header, style="Segment.TFrame", padding=2)
        segment.pack(side=tk.LEFT, padx=(20, 0))
        self.mode_buttons = {}
        for mode, label, hint in (
            (MODE_QUICK, tr("Quick edit"), tr("Fix or rewrite a text in one pass and review the suggestions.")),
            (MODE_AUTO, tr("Automatic review"),
             tr("Check a whole manuscript chapter by chapter, decision by decision, and export the result.")),
        ):
            button = ttk.Button(segment, text=label, style="Nav.TButton", command=lambda m=mode: self.switch_mode(m))
            button.pack(side=tk.LEFT)
            Tooltip(button, hint)
            self.mode_buttons[mode] = button
        # Backend, model and connection state live in one pill; its menu switches them.
        self.backend_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value=self.settings.preferred_model())
        self._models = [self.model_var.get()] if self.model_var.get() else []
        self.connection_var = tk.StringVar(value="")
        self.connection_pill = ttk.Menubutton(header, textvariable=self.connection_var, style="Pill.TMenubutton",
                                              direction="below")
        self.connection_menu = tk.Menu(self.connection_pill, tearoff=False, postcommand=self._fill_connection_menu)
        self.connection_pill.configure(menu=self.connection_menu)
        self.connection_pill.pack(side=tk.RIGHT)
        self.connection_hint = tr("Where the model runs and which one answers. Click to switch or to open "
                                  "the connection settings.")
        self.connection_tooltip = Tooltip(self.connection_pill, self.connection_hint)
        self._refresh_backend_values()
        self._set_connection(tr("Checking..."), COLOR_NEUTRAL)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).grid(row=1, column=0, sticky="ew")

        self.content = ttk.Frame(self.root)
        self.content.grid(row=2, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self.editor_frame = ttk.Frame(self.content, padding=(16, 10, 16, 4))
        self.editor_frame.pack(fill=tk.BOTH, expand=True)
        heading = ttk.Frame(self.editor_frame)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.document_var = tk.StringVar(value=tr("Untitled"))
        ttk.Label(heading, textvariable=self.document_var, style="Title.TLabel").pack(side=tk.LEFT)
        self.document_note_var = tk.StringVar(value=tr("not saved yet"))
        ttk.Label(heading, textvariable=self.document_note_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(10, 0),
                                                                                           pady=(6, 0))
        self.count_var = tk.StringVar(value=tr("{words} words · {count} characters", words=0, count=0))
        ttk.Label(heading, textvariable=self.count_var, style="Muted.TLabel").pack(side=tk.RIGHT, pady=(6, 0))
        self.text_area = scrolledtext.ScrolledText(self.editor_frame, undo=True)
        style_text(self.text_area, size=12, serif=True)
        self.text_area.grid(row=1, column=0, sticky="nsew")
        self.text_area.bind("<<Modified>>", self._on_text_modified)
        self.text_area.bind("<<Selection>>", self._on_selection_changed, add="+")
        self.editor_frame.columnconfigure(0, weight=1)
        self.editor_frame.rowconfigure(1, weight=1)
        self._build_empty_state()

        # The composer: "Ask the model to [mode]" with the instruction box for Custom/Translate underneath.
        controls = ttk.Frame(self.editor_frame, style="Card.TFrame", padding=(12, 10))
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.controls = controls
        row = ttk.Frame(controls, style="Surface.TFrame")
        row.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        ttk.Label(row, text=tr("Ask the model to"), style="SurfaceMuted.TLabel").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=tr(MODE_LABELS["Grammar"]))
        self.mode_button = ttk.Menubutton(row, text=self.mode_var.get(), style="Mode.TMenubutton", direction="below")
        self.mode_menu = tk.Menu(self.mode_button, tearoff=False, postcommand=self._fill_mode_menu)
        self.mode_button.configure(menu=self.mode_menu)
        self.mode_button.pack(side=tk.LEFT, padx=(8, 10))
        Tooltip(self.mode_button, tr("Built-in modes, your own presets and chains of modes; the last entry manages them."))
        self.explain_var = tk.BooleanVar(value=self.settings.quick_explanations)
        explain = ttk.Checkbutton(
            row, text=tr("Explain changes"), variable=self.explain_var, command=self._on_explain_toggled,
            style="Surface.TCheckbutton",
        )
        explain.pack(side=tk.LEFT)
        Tooltip(explain, tr("Ask the model why it changed each passage (one extra request)."))
        self.review_button = ttk.Button(
            row,
            text=tr("Suggest edits"),
            style="Primary.TButton",
            command=self.start_review,
        )
        self.review_button.pack(side=tk.RIGHT)
        ttk.Label(row, text=tr("Ctrl+Enter"), style="Surface.Kbd.TLabel").pack(side=tk.RIGHT, padx=(0, 8))
        whole = ttk.Button(row, text=tr("Review the whole file..."), style="Small.Surface.Ghost.TButton",
                           command=self.send_to_review)
        whole.pack(side=tk.RIGHT, padx=(0, 12))
        Tooltip(whole, tr("Hand this file to the automatic review: chapter by chapter, every change with a reason, "
                          "decisions saved as you go."))
        below = ttk.Frame(controls, style="Surface.TFrame")
        below.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.mode_description_var = tk.StringVar(value=PROMPTS["Grammar"])
        self.mode_description = ttk.Label(
            below,
            textvariable=self.mode_description_var,
            style="Explanation.TLabel",
            justify=tk.LEFT,
        )
        self.mode_description.pack(side=tk.LEFT, fill=tk.X, expand=True)
        history = ttk.Frame(below, style="Surface.TFrame")  # Undo / Redo of applied reviews, shown when there are any
        history.pack(side=tk.RIGHT, padx=(10, 0))
        self.undo_button = ttk.Button(history, text=tr("Undo applied review"), style="Small.Surface.Ghost.TButton",
                                      command=self.undo_applied_review)
        self.undo_tooltip = Tooltip(self.undo_button, "")
        self.redo_button = ttk.Button(history, text=tr("Redo"), style="Small.Surface.Ghost.TButton",
                                      command=self.redo_applied_review)
        Tooltip(self.redo_button, tr("Apply the review you just undid again."))
        controls.bind("<Configure>", lambda event: self.mode_description.configure(
            wraplength=max(240, event.width - 40 - history.winfo_reqwidth())))
        self._build_instruction_box(controls)
        self.scope_var = tk.StringVar(value="")
        self.scope_row = ttk.Frame(controls, style="Surface.TFrame")
        ttk.Label(self.scope_row, textvariable=self.scope_var, style="Surface.TLabel").pack(side=tk.LEFT)
        ttk.Button(self.scope_row, text=tr("Edit the whole text instead"), style="Link.TButton",
                   command=self._clear_selection).pack(side=tk.LEFT, padx=(8, 0))

        self.review_panel = ReviewPanel(
            self.content,
            on_apply=self.apply_review,
            on_back=self.discard_review,
            on_status=self.set_status,
        )
        self.workflow_screen = WorkflowScreen(self.content, host=self)
        self._update_mode_buttons()

        bottom = ttk.Frame(self.root, padding=(12, 6, 12, 8))
        bottom.grid(row=3, column=0, sticky="ew")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.progress.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            bottom, text=tr("Cancel"), command=self.cancel_generation, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=6)
        self.thinking_button = ttk.Button(bottom, text=tr("Thinking..."), command=self.show_thinking)
        self.thinking_button.pack(side=tk.LEFT)
        Tooltip(self.thinking_button, tr("Watch the model's reasoning while it works (View menu, Ctrl+Shift+T)."))
        self.status_var = tk.StringVar(
            value=tr("Paste text, choose an editing mode, then review suggestions.")
        )
        self.status_label = ttk.Label(bottom, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        self.status_label.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )
        ttk.Button(bottom, text=tr("Quit"), command=self.close).pack(side=tk.RIGHT)
        self.usage_var = tk.StringVar(value="")
        self.usage_label = ttk.Label(bottom, textvariable=self.usage_var, anchor="e", style="Status.TLabel")
        self.usage_label.pack(side=tk.RIGHT, padx=(6, 12))
        self.usage_tooltip = Tooltip(self.usage_label, "")
        self.progress.pack_forget()
        self.cancel_button.pack_forget()
        self.thinking_button.pack_forget()

    def _build_empty_state(self):
        """Three lines over the empty editor that say what to do; gone as soon as there is text."""
        self.empty_state = ttk.Frame(self.text_area, style="Surface.TFrame", padding=(24, 18))
        for number, line in enumerate((
            tr("Paste or open a text - a paragraph, an essay, a chapter."),
            tr("Choose what the model should do with it, below."),
            tr("Read every suggestion and keep only what you like."),
        ), 1):
            step = ttk.Frame(self.empty_state, style="Surface.TFrame")
            step.pack(anchor="w", pady=(0, 6))
            ttk.Label(step, text="{0}.".format(number), style="StepNumber.TLabel", width=3).pack(side=tk.LEFT)
            ttk.Label(step, text=line, style="Step.TLabel").pack(side=tk.LEFT)
        actions = ttk.Frame(self.empty_state, style="Surface.TFrame")
        actions.pack(anchor="w", pady=(10, 0))
        ttk.Button(actions, text=tr("Open a file..."), command=self.open_file).pack(side=tk.LEFT)
        self.setup_button = ttk.Button(actions, text=tr("Set up a model..."), style="Primary.TButton",
                                       command=self.open_connection_dialog)
        self.setup_button.pack(side=tk.LEFT, padx=(8, 0))
        self.setup_button.pack_forget()
        self.empty_state.bind("<Button-1>", lambda event: self.text_area.focus_set())
        self.empty_state.place(relx=0.5, rely=0.42, anchor="center")
        self._empty_state_shown = True

    def _update_empty_state(self, has_text):
        """Show the hints only while the editor is empty; offer model setup while none is available."""
        show = not has_text and not self.generating
        if show and not self._empty_state_shown:
            self.empty_state.place(relx=0.5, rely=0.42, anchor="center")
        elif not show and self._empty_state_shown:
            self.empty_state.place_forget()
        self._empty_state_shown = show
        if self._models:
            self.setup_button.pack_forget()
        else:
            self.setup_button.pack(side=tk.LEFT, padx=(8, 0))

    def _build_instruction_box(self, controls):
        """The multi-line instruction of the Custom mode (or the target language of Translate), with history."""
        self.instruction_frame = ttk.Frame(controls, style="Surface.TFrame")
        self.instruction_frame.columnconfigure(1, weight=1)
        self.instruction_label_var = tk.StringVar(value=tr("Instruction:"))
        ttk.Label(self.instruction_frame, textvariable=self.instruction_label_var, style="SurfaceMuted.TLabel").grid(
            row=0, column=0, sticky="nw", pady=(6, 0))
        self.instruction_text = tk.Text(self.instruction_frame, height=3, undo=True)
        style_text(self.instruction_text, size=10)
        self.instruction_text.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.instruction_text.bind("<Control-Return>", self._primary_shortcut)
        self.instruction_text.bind("<KeyRelease>", lambda event: self._instruction_changed())
        side = ttk.Frame(self.instruction_frame, style="Surface.TFrame")
        side.grid(row=0, column=2, sticky="n")
        self.history_button = ttk.Menubutton(side, text=tr("Recent instructions"), style="Small.TMenubutton")
        self.history_menu = tk.Menu(self.history_button, tearoff=False, postcommand=self._fill_history_menu)
        self.history_button.configure(menu=self.history_menu)
        self.history_button.pack(anchor="e")
        self.save_preset_button = ttk.Button(side, text=tr("Save as preset..."), style="Link.TButton",
                                             command=self.save_custom_preset)
        self.save_preset_button.pack(anchor="e", pady=(4, 0))
        self._instruction_mode = None  # "Custom", "Translate" or None while the box is hidden

    def _show_instruction_box(self, mode):
        """Reveal the box for ``mode`` ("Custom" or "Translate") or hide it for any other mode."""
        if mode == self._instruction_mode:
            return
        self._instruction_mode = mode
        if mode is None:
            self.instruction_frame.grid_remove()
            return
        self.instruction_text.delete("1.0", tk.END)
        if mode == "Translate":
            self.instruction_label_var.set(tr("Translate into:"))
            self.instruction_text.configure(height=1)
            self.instruction_text.insert("1.0", self.settings.translation_language)
            self.history_button.pack_forget()
            self.save_preset_button.pack_forget()
        else:
            self.instruction_label_var.set(tr("Instruction:"))
            self.instruction_text.configure(height=3)
            self.instruction_text.insert("1.0", self.last_custom_instruction)
            self.history_button.pack(anchor="e")
            self.save_preset_button.pack(anchor="e", pady=(4, 0))
        self.instruction_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._instruction_changed()
        self.instruction_text.focus_set()

    def _instruction_value(self):
        return " ".join(self.instruction_text.get("1.0", "end-1c").split()) if self._instruction_mode else ""

    def _instruction_changed(self):
        if self._instruction_mode == "Custom":
            self.save_preset_button.configure(state=tk.NORMAL if self._instruction_value() else tk.DISABLED)

    def _fill_history_menu(self):
        menu = self.history_menu
        menu.delete(0, tk.END)
        if not self.settings.instruction_history:
            menu.add_command(label=tr("(no instructions used yet)"), state=tk.DISABLED)
            return
        for entry in self.settings.instruction_history:
            label = entry if len(entry) <= 70 else entry[:69] + "…"
            menu.add_command(label=label, command=lambda text=entry: self._use_history_entry(text))

    def _use_history_entry(self, text):
        self.instruction_text.delete("1.0", tk.END)
        self.instruction_text.insert("1.0", text)
        self._instruction_changed()
        self.instruction_text.focus_set()

    def _on_selection_changed(self, event=None):
        """Say so on the button when only the selected passage would be edited (and offer the whole text)."""
        try:
            ranges = self.text_area.tag_ranges("sel")
        except tk.TclError:
            ranges = ()
        words = len(re.findall(r"\S+", self.text_area.get(*ranges))) if len(ranges) >= 2 else 0
        if words:
            self.review_button.configure(text=tr("Suggest edits for the selection"))
            self.scope_var.set(tr("Only the selected passage ({count} words) will be edited.", count=format_number(words)))
            self.scope_row.grid(row=3, column=0, sticky="w", pady=(6, 0))
        else:
            self.review_button.configure(text=tr("Suggest edits"))
            self.scope_row.grid_remove()

    def _clear_selection(self):
        self.text_area.tag_remove("sel", "1.0", tk.END)
        self._on_selection_changed()
        self.text_area.focus_set()

    def _update_document_heading(self):
        if self.current_path is not None:
            self.document_var.set(self.current_path.name)
            self.document_note_var.set(tr("unsaved changes") if self.modified else str(self.current_path.parent))
        else:
            self.document_var.set(tr("Untitled"))
            self.document_note_var.set(tr("unsaved changes") if self.modified else tr("not saved yet"))

    def _bind_shortcuts(self):
        self.root.bind_all("<Control-Return>", self._primary_shortcut)
        self.root.bind_all("<Control-Shift-KeyPress-T>", self.show_thinking)
        self.root.bind_all("<F1>", self.show_shortcuts)
        self.root.bind_all("<Control-KeyPress-o>", self._open_shortcut)
        self.root.bind_all("<Control-KeyPress-O>", self._open_shortcut)
        self.root.bind_all("<Control-KeyPress-s>", self._save_shortcut)
        self.root.bind_all("<Control-KeyPress-S>", self._save_shortcut)
        self.root.bind_all("<Alt-KeyPress-a>", self._accept_shortcut)
        self.root.bind_all("<Alt-KeyPress-A>", self._accept_shortcut)
        self.root.bind_all("<Alt-KeyPress-r>", self._reject_shortcut)
        self.root.bind_all("<Alt-KeyPress-R>", self._reject_shortcut)
        self.root.bind_all("<Alt-KeyPress-z>", self._undo_shortcut)
        self.root.bind_all("<Alt-KeyPress-Z>", self._undo_shortcut)
        self.root.bind_all("<F2>", self._edit_shortcut)
        self.root.bind_all("<Control-KeyPress-e>", self._author_fix_shortcut)
        self.root.bind_all("<Control-KeyPress-E>", self._author_fix_shortcut)
        self.root.bind_all("<Alt-Left>", self._previous_shortcut)
        self.root.bind_all("<Alt-Right>", self._next_shortcut)
        self.root.bind_all("<Alt-Up>", self._up_shortcut)
        self.root.bind_all("<Alt-Down>", self._down_shortcut)
        for sequence in ("<Control-equal>", "<Control-plus>", "<Control-KP_Add>"):
            self.root.bind_all(sequence, lambda event: self.zoom(1))
        for sequence in ("<Control-minus>", "<Control-underscore>", "<Control-KP_Subtract>"):
            self.root.bind_all(sequence, lambda event: self.zoom(-1))
        for sequence in ("<Control-Key-0>", "<Control-KP_0>"):
            self.root.bind_all(sequence, lambda event: self.zoom(0))

    def _in_review(self):
        return bool(self.current_session)

    def _open_shortcut(self, event=None):
        if self._in_auto():
            return None
        self.open_file()
        return "break"

    def _save_shortcut(self, event=None):
        if self._in_auto():
            return None
        if event is not None and event.state & 0x1:  # Shift held: Save As
            self.save_file_as()
        else:
            self.save_file()
        return "break"

    def _in_auto(self):
        return self.mode == MODE_AUTO

    def _primary_shortcut(self, event=None):
        if self._in_auto():
            return None
        if self._in_review():
            if self.current_session.pending_count == 0:
                self.apply_review(self.current_session)
        elif not self.generating:
            self.start_review()
        return "break"

    def _accept_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.accept_current(event)
        if self._in_review():
            return self.review_panel.accept_current(event)
        return None

    def _reject_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.reject_current(event)
        if self._in_review():
            return self.review_panel.reject_current(event)
        return None

    def _undo_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.undo(event)
        return None

    def _edit_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.edit_current(event)
        return None

    def _author_fix_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.add_author_correction(event)
        return None

    def _previous_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.previous(event)
        if self._in_review():
            return self.review_panel.previous(event)
        return None

    def _next_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.next(event)
        if self._in_review():
            return self.review_panel.next(event)
        return None

    def _up_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.select_previous_change(event)
        if self._in_review():
            return self.review_panel.select_previous_hunk(event)
        return None

    def _down_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.select_next_change(event)
        if self._in_review():
            return self.review_panel.select_next_hunk(event)
        return None

    # ------------------------------------------------------------- modes
    def switch_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == MODE_AUTO:
            self.editor_frame.pack_forget()
            self.review_panel.pack_forget()
            self.workflow_screen.pack(fill=tk.BOTH, expand=True)
            if not self.workflow_screen.active:
                self.workflow_screen.start_view.refresh_defaults(self)
        else:
            self.workflow_screen.pack_forget()
            if self.current_session:
                self.review_panel.pack(fill=tk.BOTH, expand=True)
            else:
                self.editor_frame.pack(fill=tk.BOTH, expand=True)
        self._update_mode_buttons()

    def _update_mode_buttons(self):
        for mode, button in self.mode_buttons.items():
            button.configure(style="NavActive.TButton" if mode == self.mode else "Nav.TButton")

    # ------------------------------------------------ host API for screens
    def get_service(self):
        return self.service

    def get_model(self):
        return self.model_var.get().strip()

    def backend_id(self):
        return self.settings.backend

    def default_style_guide(self):
        """Author's instructions that pre-fill a new review project."""
        return self.settings.default_style_guide

    def remember_style_guide(self, style_guide):
        """Keep the last used author's instructions as the default for the next project."""
        style_guide = style_guide or ""
        if style_guide != self.settings.default_style_guide:
            self.settings.default_style_guide = style_guide
            self._save_settings()

    def default_glossary(self):
        """Protected terms that pre-fill a new review project."""
        return list(self.settings.default_glossary)

    def default_evaluation_mode(self):
        """Evaluation mode ("combined" or "separate") pre-selected for a new review project."""
        return self.settings.default_evaluation_mode

    def remember_evaluation_mode(self, mode):
        if mode and mode != self.settings.default_evaluation_mode:
            self.settings.default_evaluation_mode = mode
            self._save_settings()

    def remember_glossary(self, glossary):
        """Keep the last used protected terms as the default for the next project."""
        glossary = list(glossary or [])
        if glossary != list(self.settings.default_glossary):
            self.settings.default_glossary = glossary
            self._save_settings()

    def lock_controls(self, locked):
        """Freeze backend/model selection while a project evaluation runs."""
        self._controls_locked = bool(locked)
        self._update_control_states()

    def _update_control_states(self):
        busy = self.generating or self._controls_locked
        self.connection_pill.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def _on_text_modified(self, event=None):
        if self._suppress_modified:
            self.text_area.edit_modified(False)
            return
        if self.text_area.edit_modified():
            self.revision_id += 1
            self.text_area.edit_modified(False)
            self._update_counts()
            self._set_modified(True)

    def _set_modified(self, modified):
        self.modified = bool(modified)
        self.root.title(window_title(self.current_path, self.modified))
        self._update_document_heading()

    def _update_counts(self):
        text = self.text_area.get("1.0", "end-1c")
        words = len(re.findall(r"\S+", text))
        self.count_var.set(
            tr("{words} words · {count} characters", words=format_number(words), count=format_number(len(text)))
        )
        self._update_empty_state(bool(text.strip()))

    # ------------------------------------------------------------- modes
    @staticmethod
    def mode_label(mode):
        """The translated display name of a built-in mode (custom modes and chains keep their own name)."""
        label = MODE_LABELS.get(mode)
        return tr(label) if label else mode

    def _mode_values(self):
        """Every selectable mode as a display name: built-in modes, then the custom presets, then the chains."""
        return [self.mode_label(mode) for mode in EDITING_MODES] + self.settings.custom_mode_names() + self.settings.chain_names()

    def _fill_mode_menu(self):
        """The mode picker's menu: built-in modes, then presets and chains in their own sections."""
        menu = self.mode_menu
        menu.delete(0, tk.END)
        for mode in EDITING_MODES:
            menu.add_radiobutton(label=self.mode_label(mode), variable=self.mode_var, value=self.mode_label(mode),
                                 command=self._on_mode_selected)
        for heading, names in ((tr("My presets"), self.settings.custom_mode_names()),
                               (tr("Chains"), self.settings.chain_names())):
            if not names:
                continue
            menu.add_separator()
            menu.add_command(label=heading, state=tk.DISABLED)
            for name in names:
                menu.add_radiobutton(label=name, variable=self.mode_var, value=name, command=self._on_mode_selected)
        menu.add_separator()
        menu.add_command(label=tr("Manage modes..."), command=self.open_mode_dialog)

    def _mode_key(self, label=None):
        """The mode identifier (PROMPTS key, custom mode or chain name) behind a display name."""
        label = self.mode_var.get() if label is None else label
        for mode in EDITING_MODES:
            if self.mode_label(mode) == label:
                return mode
        return label

    def _refresh_mode_values(self, select=None):
        values = self._mode_values()
        select = self.mode_label(select) if select else None
        if select in values:
            self.mode_var.set(select)
        elif self.mode_var.get() not in values:
            self.mode_var.set(self.mode_label("Grammar"))
        self._on_mode_selected()

    def _on_mode_selected(self, event=None):
        self.mode_button.configure(text=self.mode_var.get())
        self._update_mode_description()

    def _update_mode_description(self, event=None):
        mode = self._mode_key()
        descriptions = {
            "Translate": tr("Translate the complete text into the language you name below."),
            "Custom": tr("Tell the model in your own words what to do with the text."),
        }
        self._show_instruction_box(mode if mode in descriptions else None)
        if mode in descriptions or mode in PROMPTS:
            self.mode_description_var.set(descriptions.get(mode, PROMPTS.get(mode, "")))
            return
        steps = self.settings.find_chain(mode)
        if steps is not None:
            self.mode_description_var.set(tr("Chain: {steps}", steps=describe_chain(steps)))
            return
        self.mode_description_var.set(build_instruction(mode, custom_modes=self.settings.custom_modes)
                                      if mode in self.settings.custom_mode_names() else "")

    def _on_explain_toggled(self):
        self.settings.quick_explanations = bool(self.explain_var.get())
        self._save_settings()

    def save_custom_preset(self):
        """Store the Custom instruction in the box under a name of the user's choice."""
        instruction = self._instruction_value() if self._instruction_mode == "Custom" else self.last_custom_instruction
        if not instruction:
            self.set_status(tr("Enter an instruction first."))
            return
        name = ask_name(self.root, tr("Save as preset"), tr("Name for this instruction:"), ok_label=tr("Save"))
        if not name:
            return
        modes, problems = validate_custom_modes(self.settings.custom_modes + [{"name": name, "instruction": instruction}])
        if problems:
            messagebox.showerror(tr("Cannot save preset"), "\n".join(problems))
            return
        self.settings.custom_modes = modes
        self._save_settings()
        self._refresh_mode_values(select=modes[-1]["name"])
        self.notify(tr("Preset “{name}” saved.", name=modes[-1]["name"]))

    def open_mode_dialog(self):
        ManageModesDialog(self.root, self.settings.custom_modes, self.settings.chains, on_save=self._apply_modes)

    def _apply_modes(self, modes, chains):
        self.settings.custom_modes = modes
        self.settings.chains = chains
        self._save_settings()
        self._refresh_mode_values()
        self.set_status(tr("{count} custom mode(s) and {count2} chain(s) saved.", count=len(modes), count2=len(chains)))

    def set_status(self, message):
        self.status_var.set(message)

    def average_request_seconds(self):
        """Mean model time per request in this session (None before the first answer); feeds the start view's estimate."""
        totals = self.session_usage
        if not totals["requests"] or not totals["seconds"]:
            return None
        return totals["seconds"] / float(totals["requests"])

    def notify(self, message, action=None):
        """Show a routine outcome as a toast (with one optional ``(label, callback)`` button)."""
        self.toast.show(message, action=action)

    def record_usage(self, record):
        """Add one request's token counts to the session total shown in the status bar."""
        add_usage(self.session_usage, record)
        totals = self.session_usage
        self.usage_var.set(tr("Session: {usage}", usage=format_usage(totals)))
        self.usage_tooltip.text = tr(
            "Tokens used since the app started (every request in quick edit and automatic review)\n"
            "Prompt tokens: {prompt_tokens}\nCompletion tokens: {completion_tokens}\nRequests: {requests}\n"
            "Model time: {model_time}\nLast request: {last_in} in, {last_out} out ({model})",
            prompt_tokens=format_number(totals["prompt_tokens"]), completion_tokens=format_number(totals["completion_tokens"]),
            requests=totals["requests"], model_time=_format_duration(totals["seconds"]),
            last_in=format_number(record.prompt_tokens), last_out=format_number(record.completion_tokens),
            model=record.model or "?",
        )

    def _set_connection(self, message, color):
        """Show the connection state in the header pill: glyph, message and model, coloured by state."""
        self._connection = (message, color)
        model = self.model_var.get().strip()
        text = "{0} {1}".format(CONNECTION_GLYPHS.get(color, ""), message).strip()
        if model and color == COLOR_OK:
            text = tr("{state} · {model} · {backend}", state=text, model=model, backend=self._backend_label())
        self.connection_var.set(text + "  ▾")
        styles = {COLOR_OK: "Ok.Pill.TMenubutton", COLOR_WARN: "Warn.Pill.TMenubutton",
                  COLOR_ERROR: "Error.Pill.TMenubutton"}
        self.connection_pill.configure(style=styles.get(color, "Pill.TMenubutton"))

    def _fill_connection_menu(self):
        """Rebuild the pill's menu: backends, the models of the current one, refresh and settings."""
        menu = self.connection_menu
        menu.delete(0, tk.END)
        menu.add_command(label=tr("Where the model runs"), state=tk.DISABLED)
        for label, _, _ in self._backend_choices():
            menu.add_radiobutton(label=label, variable=self.backend_var, value=label, command=self._on_backend_selected)
        menu.add_separator()
        menu.add_command(label=tr("Model"), state=tk.DISABLED)
        if self._models:
            for model in self._models:
                menu.add_radiobutton(label=model, variable=self.model_var, value=model, command=self._on_model_selected)
        else:
            menu.add_command(label=tr("(no model available)"), state=tk.DISABLED)
        menu.add_separator()
        menu.add_command(label=tr("Refresh models"), command=self.refresh_models)
        menu.add_command(label=tr("Connection settings..."), command=self.open_connection_dialog)

    # ------------------------------------------------------- backend switching
    def _save_settings(self):
        error = self.settings.save()
        if error:
            self.set_status(error)

    def _backend_choices(self):
        """The backend combobox entries: ``[(label, backend, profile name or None)]``."""
        choices = [(tr(BACKEND_LABELS[BACKEND_OLLAMA]), BACKEND_OLLAMA, None)]
        for name in self.settings.profile_names():
            choices.append((tr("Remote: {name}", name=name), BACKEND_REMOTE, name))
        return choices

    def _backend_label(self, backend=None, profile=None):
        backend = backend or self.settings.backend
        if backend == BACKEND_REMOTE:
            return tr("Remote: {name}", name=profile or self.settings.active_profile)
        return tr(BACKEND_LABELS[BACKEND_OLLAMA])

    def _refresh_backend_values(self):
        """Select the active backend's label in the pill menu (Local Ollama or "Remote: <profile>")."""
        self.backend_var.set(self._backend_label())

    def _on_backend_selected(self, event=None):
        label = self.backend_var.get()
        backend, profile = next(
            ((backend, profile) for choice, backend, profile in self._backend_choices() if choice == label),
            (BACKEND_OLLAMA, None),
        )
        self._switch_backend(backend, profile)

    def _switch_backend(self, backend, profile=None):
        """Activate a backend; for the remote backend ``profile`` picks the relay profile."""
        if self.generating or self._controls_locked:
            self.backend_var.set(self._backend_label())
            return
        self.settings.backend = backend
        if backend == BACKEND_REMOTE and profile and profile != self.settings.active_profile:
            self.settings.set_active_profile(profile)
            self.services[BACKEND_REMOTE] = self._remote_from_settings()
        self.service = self.services[backend]
        self.backend_var.set(self._backend_label(backend))
        self.model_var.set(self.settings.preferred_model(backend))
        self._models = [self.model_var.get()] if self.model_var.get() else []
        self._save_settings()
        if backend == BACKEND_REMOTE and not self.settings.remote_configured:
            self._set_connection(tr("Not configured"), COLOR_WARN)
            self.set_status(tr("Choose Connection... to enter the relay address and API key."))
            return
        self.refresh_models()

    def _on_model_selected(self, event=None):
        model = self.model_var.get().strip()
        if model:
            self.settings.remember_model(self.settings.backend, model)
            self._save_settings()
        self._set_connection(*self._connection)

    def open_connection_dialog(self):
        if self.generating or self._controls_locked:
            return
        ConnectionDialog(self.root, self.settings, on_save=self._apply_connection_settings)

    def _apply_connection_settings(self, settings):
        self.settings = settings
        self.services[BACKEND_REMOTE] = self._remote_from_settings()
        self._save_settings()
        self._refresh_backend_values()
        self._switch_backend(settings.backend)
        if resolve_language(settings.ui_language) != current_language():
            self.set_status(tr("Restart TextEnhanceAI to apply the language."))

    # --------------------------------------------------------- model discovery
    def refresh_models(self):
        if self.generating:
            return
        service = self.service
        backend = self.settings.backend
        if backend == BACKEND_REMOTE and not self.settings.remote_configured:
            self._set_connection(tr("Not configured"), COLOR_WARN)
            self.set_status(tr("Choose Connection... to enter the relay address and API key."))
            return
        self._set_connection(tr("Checking {backend}...", backend=tr(service.display_name)), COLOR_NEUTRAL)

        def worker():
            try:
                models = service.list_models()
                summary = service.connection_summary()
                self.events.put(("models", backend, models, summary))
            except Exception as exc:
                self.events.put(("model_error", backend, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_models(self, backend, models, summary):
        if backend != self.settings.backend:
            return  # the user switched backends while this request was running
        self._models = list(models)
        self.connection_tooltip.text = self.connection_hint
        if not models:
            self.model_var.set("")
            self._set_connection(tr("Model missing"), COLOR_WARN)
            self.set_status(self.service.no_models_hint())
            self._update_counts()
            return
        preferred = self.settings.preferred_model(backend) or self.model_var.get()
        chosen = preferred if preferred in models else models[0]
        self.model_var.set(chosen)
        self.settings.remember_model(backend, chosen)
        self._save_settings()
        self._set_connection(summary or tr("Connected"), COLOR_OK)
        self.set_status(
            tr("{backend} ready with {count} model(s). Paste text, choose an editing mode, then review suggestions.",
               backend=tr(self.service.display_name).capitalize(), count=len(models))
        )
        self._update_counts()

    def _handle_model_error(self, backend, error):
        if backend != self.settings.backend:
            return
        self._models = []
        self._set_connection(tr("Unavailable"), COLOR_ERROR)
        self.set_status(tr("{backend} could not be reached. Open the connection menu in the header to check the "
                           "settings or to switch where the model runs.",
                           backend=tr(self.service.display_name).capitalize()))
        self.connection_tooltip.text = tr("The model list could not be fetched: {error}", error=error)
        self._update_counts()

    # -------------------------------------------------------------- generation
    def _get_instruction(self):
        """Return the ``(name, instruction)`` steps for the chosen mode, or None when something is missing."""
        mode = self._mode_key()
        if mode == "Translate":
            language = self._instruction_value()
            if not language:
                self.set_status(tr("Name the language to translate into first."))
                self.instruction_text.focus_set()
                return None
            self.settings.translation_language = language
            self._save_settings()
            return [(mode, build_instruction(mode, language))]
        if mode == "Custom":
            custom = self._instruction_value()
            if not custom:
                self.set_status(tr("Enter an instruction first."))
                self.instruction_text.focus_set()
                return None
            self.last_custom_instruction = custom
            self.settings.remember_instruction(custom)
            self._save_settings()
            return [(mode, build_instruction(mode, custom))]
        steps = self.settings.find_chain(mode)
        if steps is not None:
            try:
                return list(zip(steps, build_chain(steps, self.settings.custom_modes)))
            except ValueError as exc:
                messagebox.showerror(tr("Chain not usable"), tr("This chain cannot be run: {error}", error=exc))
                return None
        try:
            return [(mode, build_instruction(mode, custom_modes=self.settings.custom_modes))]
        except ValueError as exc:
            messagebox.showerror(tr("Unknown mode"), tr("This mode cannot be run: {error}", error=exc))
            return None

    @staticmethod
    def _describe_steps(mode, steps):
        """What the scratchpad and review record as the instruction."""
        if len(steps) == 1:
            return steps[0][1]
        return "Chain \u201c{0}\u201d: {1}".format(mode, describe_chain(name for name, _ in steps))  # recorded, English

    def _current_selection(self, full_text):
        """The editor selection as ``(start, end)`` character offsets, or None."""
        try:
            ranges = self.text_area.tag_ranges("sel")
        except tk.TclError:
            return None
        if len(ranges) < 2:
            return None
        return normalise_span(full_text, char_offset(full_text, str(ranges[0])), char_offset(full_text, str(ranges[1])))

    def start_review(self):
        if self.generating:
            return
        full_text = self.text_area.get("1.0", "end-1c")
        selection = self._current_selection(full_text)
        source = full_text[selection[0]:selection[1]] if selection else full_text
        if not source.strip():
            if selection:
                messagebox.showinfo("TextEnhanceAI", tr("The selected passage contains no text. Select text or clear the selection."))
            else:
                messagebox.showinfo("TextEnhanceAI", tr("Enter or paste text to edit."))
            return
        model = self.model_var.get().strip()
        if not model:
            messagebox.showerror(
                tr("Model missing"),
                tr("No model is available on the selected backend.") + " " + self.service.no_models_hint(),
            )
            return
        steps = self._get_instruction()
        if steps is None:
            return
        mode = self._mode_key()
        description = self._describe_steps(mode, steps)
        explain = bool(self.explain_var.get())
        # what the explanation prompt quotes as the instruction (every step of a chain)
        explain_instruction = "\n".join(instruction for _, instruction in steps)

        service = self.service
        self.active_request_id += 1
        request_id = self.active_request_id
        revision_id = self.revision_id
        self.active_revision_id = revision_id
        self.cancel_event = threading.Event()
        self.generating = True
        self.generation_started_at = time.time()
        self.generation_label = tr("{model} via {backend}", model=model, backend=tr(service.display_name))
        scope = tr(" · selection ({count} words)", count=len(re.findall(r"\S+", source))) if selection else ""
        if selection:
            self.generation_verb = tr("Editing selection ({count} words)", count=len(re.findall(r"\S+", source)))
        else:
            self.generation_verb = tr("Generating review")
        self._progress_chars = 0
        self._set_generating_state(True)
        cancel_event = self.cancel_event

        def on_progress(received):
            self._progress_chars = received

        # The request the thinking window is told about: one per chain step, one for the
        # explanations (events as in core.workflow.start_stream, all from the worker thread).
        stream = {"key": None}

        def begin_stream(step, info):
            end_stream("done")
            stream["key"] = ("quick", request_id, step)
            self.events.put((STREAM_EVENT, stream["key"], "started", info))

        def end_stream(outcome):
            if stream["key"] is not None:
                self.events.put((STREAM_EVENT, stream["key"], "finished", outcome))
                stream["key"] = None

        def on_stream(kind, text):
            if stream["key"] is not None:
                self.events.put((STREAM_EVENT, stream["key"], kind, text))

        def on_step(index, total, name):
            self._progress_chars = 0
            if total > 1:
                self.generation_verb = tr("Step {index}/{total}: {name}{scope}", index=index, total=total,
                                          name=self.mode_label(name), scope=scope)
            begin_stream(index, {"scope": "quick", "mode": name, "step": index, "total": total})

        def on_usage(record):
            self.events.put(("usage", record))

        def worker():
            try:
                chain = run_chain(
                    service, model, steps, source, cancel_event, on_step=on_step, on_progress=on_progress,
                    on_usage=on_usage, on_stream=on_stream,
                )
                end_stream("done")
                session = build_edit_session(
                    source,
                    chain.text,
                    instruction=description,
                    model=model,
                    revision_id=revision_id,
                    selection=selection,
                    full_text=full_text,
                )
                session.steps = chain.steps
                if explain and session.review_items:
                    self.generation_verb = tr("Explaining changes")
                    self._progress_chars = 0
                    begin_stream("explain", {"scope": "quick_explain"})
                    try:
                        explain_session(
                            service, model, session, explain_instruction, cancel_event, on_usage=on_usage,
                            on_stream=on_stream,
                        )
                        end_stream("done")
                    except EditCancelled:
                        end_stream("cancelled")  # the edit itself is done: open the review without explanations
                    except Exception:
                        end_stream("error")  # explanations are best effort
                self.events.put(("generation_result", request_id, revision_id, session))
            except EditCancelled as exc:
                end_stream("cancelled")
                self.events.put(("generation_cancelled", request_id, exc))
            except Exception as exc:
                end_stream("error")
                self.events.put(("generation_error", request_id, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _set_generating_state(self, generating):
        self.generating = generating
        editor_state = tk.DISABLED if generating else tk.NORMAL
        self.text_area.configure(state=editor_state)
        self.mode_button.configure(state=tk.DISABLED if generating else tk.NORMAL)
        self._update_control_states()
        self.review_button.configure(state=tk.DISABLED if generating else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if generating else tk.DISABLED)
        if generating:
            self.progress.pack(side=tk.LEFT, before=self.status_label)
            self.cancel_button.pack(side=tk.LEFT, padx=6, before=self.status_label)
            self.thinking_button.pack(side=tk.LEFT, padx=(0, 6), before=self.status_label)
            self.progress.start(12)
            self.set_status(tr("{verb} with {target}...", verb=self.generation_verb, target=self.generation_label))
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.cancel_button.pack_forget()
            self.thinking_button.pack_forget()

    def cancel_generation(self):
        if self.generating and self.cancel_event:
            self.cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED)
            self.set_status(tr("Cancelling generation..."))

    def _finish_generation(self):
        self._set_generating_state(False)
        self.cancel_event = None
        self.generation_started_at = None

    def _handle_generation_result(self, event):
        _, request_id, revision_id, session = event
        model = session.model
        if request_id != self.active_request_id:
            return
        self._finish_generation()
        if revision_id != self.revision_id:
            self.set_status(
                tr("The text changed; the stale result from {model} was discarded.", model=model)
            )
            return

        self.current_logger = ScratchpadLogger(self.data_dir)
        self.current_logger.log_proposal(session)
        if not session.review_items:
            self.current_logger.log_outcome(session, "no changes", session.original_text)
            self.current_logger = None
            self.set_status(tr("{model} did not suggest any changes.", model=model))
            self.notify(tr("No changes were suggested."))
            return

        self.current_session = session
        self.editor_frame.pack_forget()
        self.review_panel.pack(fill=tk.BOTH, expand=True)
        self.review_panel.set_session(session)

    def _handle_generation_error(self, request_id, error):
        if request_id != self.active_request_id:
            return
        self._finish_generation()
        if isinstance(error, OutputTruncated):
            title = tr("Response truncated")
        else:
            title = tr("{backend} error", backend=tr(self.service.display_name).capitalize())
            self._set_connection(tr("Unavailable"), COLOR_ERROR)
        self.set_status(str(error))
        messagebox.showerror(title, tr("The request failed: {error}", error=error))

    def _handle_generation_cancelled(self, request_id):
        if request_id != self.active_request_id:
            return
        self._finish_generation()
        self.set_status(tr("Generation cancelled. Your text was not changed."))

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "models":
                    self._handle_models(event[1], event[2], event[3])
                elif kind == "model_error":
                    self._handle_model_error(event[1], event[2])
                elif kind == "generation_result":
                    self._handle_generation_result(event)
                elif kind == "generation_error":
                    self._handle_generation_error(event[1], event[2])
                elif kind == "generation_cancelled":
                    self._handle_generation_cancelled(event[1])
                elif kind == "usage":
                    self.record_usage(event[1])
                elif kind == STREAM_EVENT:
                    self.thinking_log.handle(event[1], event[2], event[3])
                elif kind == "workflow_usage":
                    self.workflow_screen.handle_event(event)  # adds to the project total
                    self.record_usage(event[1])
                elif kind.startswith(("workflow_", "consistency_")):
                    self.workflow_screen.handle_event(event)
        except queue.Empty:
            pass

        if self.generating and self.generation_started_at and not (
            self.cancel_event and self.cancel_event.is_set()
        ):
            elapsed = int(time.time() - self.generation_started_at)
            received = self._progress_chars
            thinking = self._active_thinking_chars()
            if received:
                progress = tr(" · {received} characters received", received=format_number(received))
            elif thinking:
                progress = tr(" · thinking ({count} characters)", count=format_number(thinking))
            else:
                progress = tr(" · waiting for the first tokens")
            self.set_status(
                tr("{verb} with {target}... {elapsed}s elapsed{progress}", verb=self.generation_verb,
                   target=self.generation_label, elapsed=elapsed, progress=progress)
            )
        try:
            self.root.after(100, self._poll_events)
        except tk.TclError:
            pass

    # ---------------------------------------------------------- model thinking
    def _active_thinking_chars(self):
        """Reasoning characters streamed so far by the running quick-edit request."""
        return sum(
            entry.thinking_chars for entry in self.thinking_log.active_entries()
            if entry.key[:2] == ("quick", self.active_request_id)
        )

    def show_thinking(self, event=None):
        """Open (or raise) the window that shows the model's reasoning as it streams (View menu, Ctrl+Shift+T)."""
        if self.thinking_window is not None and self.thinking_window.winfo_exists():
            self.thinking_window.deiconify()
            self.thinking_window.lift()
            self.thinking_window.focus_set()
        else:
            self.thinking_window = ThinkingWindow(
                self.root, self.thinking_log, describe=self.describe_request, hint=self.thinking_hint
            )
        return "break" if event else None

    def describe_request(self, info):
        """Label of one streamed request in the thinking window (``info`` as the producers publish it)."""
        scope = info.get("scope")
        if scope == "quick":
            label = tr("Quick edit · {mode}", mode=self.mode_label(info.get("mode") or ""))
            if (info.get("total") or 1) > 1:
                label += tr(" · step {index}/{total}", index=info.get("step"), total=info.get("total"))
            return label
        if scope == "quick_explain":
            return tr("Quick edit · explanations")
        if scope == "combined":
            return tr("{segment} · all checks", segment=self._describe_segment(info.get("chapter"), info.get("segment")))
        if scope == "evaluate":
            return tr("{segment} · {check}", segment=self._describe_segment(info.get("chapter"), info.get("segment")),
                      check=self._check_label(info.get("check")))
        if scope == "explain":
            segments = info.get("segments") or []
            check = self._check_label(info.get("check"))
            if len(segments) > 1:
                return tr("Explanations · {check} · {segment} and {more} more", check=check,
                          segment=self._describe_segment(*segments[0]), more=len(segments) - 1)
            if segments:
                return tr("Explanations · {check} · {segment}", check=check, segment=self._describe_segment(*segments[0]))
            return tr("Explanations · {check}", check=check)
        if scope == "consistency":
            return tr("Consistency check · batch {batch} of {total}", batch=info.get("batch"), total=info.get("total"))
        if scope == "outline":
            return tr("Chapter outline")
        return tr("Request")

    @staticmethod
    def _check_label(check):
        return tr(CHECK_LABELS[check]) if check in CHECK_LABELS else str(check)

    def _describe_segment(self, chapter_index, segment_index):
        project = self.workflow_screen.project if hasattr(self, "workflow_screen") else None
        chapter, segment = project.find(chapter_index, segment_index) if project is not None else (None, None)
        if chapter is not None and chapter.title:
            return tr("{title} · Segment {index}", title=chapter.title, index=segment.index)
        return tr("Chapter {chapter} · Segment {segment}", chapter=chapter_index, segment=segment_index)

    def thinking_hint(self):
        """Why the current backend may send no reasoning (shown in the thinking window)."""
        if self.settings.backend == BACKEND_REMOTE and not self.settings.remote_enable_thinking:
            return tr("Thinking is switched off for this connection profile: turn on \u201cAllow the model to think "
                      "before answering\u201d in the Connection dialog to see the model's reasoning.")
        if self.settings.backend == BACKEND_OLLAMA:
            return tr("Ollama sends reasoning only for thinking models such as qwen3 or deepseek-r1.")
        return tr("The server has to separate the reasoning (vLLM with a reasoning parser) or the model has to "
                  "write it in <think> blocks.")

    # ------------------------------------------------------------------ review
    def apply_review(self, session):
        if session.pending_count:
            self.set_status(tr("Review every pending change before applying."))
            return
        final_text = render_reviewed_text(session)
        current = self.text_area.get("1.0", "end-1c")
        span = None
        if session.selection:
            span = self._locate_selection(current, session)
            if span is None:
                self.set_status(tr("The editor text changed; the selected passage could not be found, so nothing was applied."))
                return
        session.state = "applied"
        if self.current_logger:
            self.current_logger.log_outcome(session, "applied", final_text)
        if span:
            start, end = span
            new_text = current[:start] + final_text + current[end:]
        else:
            new_text = final_text
        self._remember_applied(current, new_text, self._mode_key())
        self._set_editor_text(new_text)
        if span:
            self._select_span(span[0], span[0] + len(final_text))
        self._leave_review(tr("Reviewed changes applied."))
        self.notify(tr("Reviewed changes applied."), action=(tr("Undo"), self.undo_applied_review))

    # ------------------------------------------------------- applied history
    def _remember_applied(self, before, after, label):
        """Push one applied review onto the undo stack (the newest APPLIED_HISTORY_LIMIT are kept)."""
        self.applied_history.append((before, after, self.mode_label(label)))
        del self.applied_history[:-APPLIED_HISTORY_LIMIT]
        self.redo_history = []
        self._update_history_buttons()

    def _update_history_buttons(self):
        if self.applied_history:
            self.undo_button.pack(side=tk.LEFT)
            self.undo_tooltip.text = tr("Undo the last applied review ({label}); {count} more can be undone.",
                                        label=self.applied_history[-1][2], count=len(self.applied_history) - 1)
        else:
            self.undo_button.pack_forget()
        if self.redo_history:
            self.redo_button.pack(side=tk.LEFT, padx=(6, 0))
        else:
            self.redo_button.pack_forget()

    def _clear_applied_history(self):
        self.applied_history = []
        self.redo_history = []
        self._update_history_buttons()

    @staticmethod
    def _locate_selection(current, session):
        """Where the edited selection sits in the current editor text (None when it is gone)."""
        start, end = session.selection
        if current[start:end] == session.original_text:
            return start, end
        if current.count(session.original_text) == 1:
            start = current.index(session.original_text)
            return start, start + len(session.original_text)
        return None

    def _select_span(self, start, end):
        text = self.text_area.get("1.0", "end-1c")
        self.text_area.tag_remove("sel", "1.0", tk.END)
        self.text_area.tag_add("sel", tk_index(text, start), tk_index(text, end))
        self.text_area.mark_set("insert", tk_index(text, end))
        self.text_area.see(tk_index(text, start))

    def discard_review(self, session):
        session.state = "discarded"
        if self.current_logger:
            self.current_logger.log_outcome(session, "discarded", session.original_text)
        self._leave_review(tr("Review discarded. The original text was kept."))

    def _leave_review(self, status):
        self.review_panel.pack_forget()
        self.editor_frame.pack(fill=tk.BOTH, expand=True)
        self.current_session = None
        self.current_logger = None
        self.set_status(status)
        self.text_area.focus_set()

    def _set_editor_text(self, text, modified=True):
        """Replace the editor text; ``modified=False`` when it now matches the file on disk.

        The replacement is one step of the widget's own undo (Ctrl+Z), not a
        delete and an insert.
        """
        self._suppress_modified = True
        self.text_area.configure(state=tk.NORMAL)
        self.text_area.edit_separator()
        self.text_area.delete("1.0", tk.END)
        self.text_area.insert("1.0", text)
        self.text_area.edit_separator()
        self.text_area.edit_modified(False)
        self._suppress_modified = False
        self.revision_id += 1
        self._update_counts()
        self._set_modified(modified)

    def undo_applied_review(self):
        """Put the text back the way it was before the newest applied review (any number of times)."""
        if not self.applied_history or self.generating:
            return
        before, after, label = self.applied_history.pop()
        self.redo_history.append((before, after, label))
        self._set_editor_text(before)
        self._update_history_buttons()
        self.set_status(tr("Undid the applied review ({label}).", label=label))

    def redo_applied_review(self):
        if not self.redo_history or self.generating:
            return
        before, after, label = self.redo_history.pop()
        self.applied_history.append((before, after, label))
        self._set_editor_text(after)
        self._update_history_buttons()
        self.set_status(tr("Applied the review again ({label}).", label=label))

    # ------------------------------------------------------------------- files
    def _confirm_discard(self):
        """Offer to save unsaved editor changes; False when the user cancels the action."""
        if not self.modified:
            return True
        name = self.current_path.name if self.current_path else tr("Untitled")
        answer = messagebox.askyesnocancel(tr("Unsaved changes"), tr("Save the changes to {name}?", name=name))
        if answer is None:
            return False
        if answer:
            return self.save_file()
        return True

    def open_file(self, path=None):
        """Open a .txt/.md/.docx/.odt file in the quick editor (asks when there are unsaved changes)."""
        if self.generating:
            return False
        if not self._confirm_discard():
            return False
        if path is None:
            path = filedialog.askopenfilename(title=tr("Open"), filetypes=file_types(), parent=self.root)
            if not path:
                return False
        try:
            document = load_document(path)
        except (OSError, DocumentError) as exc:
            messagebox.showerror(tr("Cannot open file"), tr("The file could not be opened: {error}", error=exc))
            self.settings.forget_file(path)
            self._rebuild_recent_menu()
            return False
        if self.current_session:
            self.discard_review(self.current_session)  # a review of the old text makes no sense any more
        self.current_path = Path(path)
        self.current_document = document
        self._clear_applied_history()
        self._set_editor_text(document.text, modified=False)
        self.settings.remember_file(self.current_path)
        self._save_settings()
        self._rebuild_recent_menu()
        if self.mode != MODE_QUICK:
            self.switch_mode(MODE_QUICK)
        self.set_status(tr("Opened {path}.", path=self.current_path))
        self.text_area.focus_set()
        return True

    def save_file(self):
        """Save to the current file (Save As when untitled); returns whether it was written."""
        if self.generating:
            return False
        if self.current_path is None:
            return self.save_file_as()
        return self._write_file(self.current_path)

    def save_file_as(self):
        if self.generating:
            return False
        kind = self.current_document.kind if self.current_document else "txt"
        suffix = KIND_SUFFIXES.get(kind, ".txt")
        path = filedialog.asksaveasfilename(
            title=tr("Save As"), defaultextension=suffix, filetypes=file_types(), parent=self.root,
            initialfile=self.current_path.name if self.current_path else "",
            initialdir=str(self.current_path.parent) if self.current_path else None,
        )
        if not path:
            return False
        return self._write_file(Path(path))

    def _write_file(self, path):
        text = self.text_area.get("1.0", "end-1c")
        document = self.current_document or text_document(text, str(path))
        warnings = []
        try:
            save_document(document, text, path, warnings.append)
        except (OSError, DocumentError) as exc:
            messagebox.showerror(tr("Cannot save file"), tr("The file could not be saved: {error}", error=exc))
            return False
        self.current_path = Path(path)
        try:
            # the saved file is the new baseline (for .docx/.odt the next save edits it paragraph-wise)
            self.current_document = load_document(path) if kind_for(path) in FORMATTED_KINDS else text_document(text, str(path))
        except (OSError, DocumentError):
            self.current_document = text_document(text, str(path))
        self._set_modified(False)
        self.settings.remember_file(self.current_path)
        self._save_settings()
        self._rebuild_recent_menu()
        message = tr("Saved {path}.", path=self.current_path)
        if warnings:
            message += " " + " ".join(warnings) + "."
            messagebox.showwarning(tr("Saved with limitations"), "\n".join(warnings))
        self.set_status(message)
        return True

    def send_to_review(self):
        """Hand the editor's file to the automatic review (saving first, since it works on files)."""
        if self.generating:
            return
        if self.current_session:
            messagebox.showinfo(tr("Review in progress"), tr("Apply or discard the current review first."))
            return
        if not self.text_area.get("1.0", "end-1c").strip():
            messagebox.showinfo("TextEnhanceAI", tr("Enter or open text first."))
            return
        if self.current_path is None or self.modified:
            if self.current_path is None:
                question = tr("The automatic review works on files. Save the text to a file now?")
            else:
                question = tr("The automatic review works on files. Save the text to {name} now?",
                              name=self.current_path.name)
            if not messagebox.askyesno(tr("Save first?"), question):
                return
            if not self.save_file():
                return
        path = self.current_path
        self.switch_mode(MODE_AUTO)
        if self.workflow_screen.active:
            if not messagebox.askyesno(
                tr("Review project open"),
                tr("Close the current review project (its progress is saved) and start a new one with {name}?",
                   name=path.name),
            ):
                return
            self.workflow_screen.close_project()
            if self.workflow_screen.active:
                return  # the user kept a running evaluation
        self.workflow_screen.start_view.set_file(str(path))
        self.set_status(tr("{name} is ready for the automatic review; check the options and start.", name=path.name))

    def close(self):
        if not self._confirm_discard():
            return
        if self.cancel_event:
            self.cancel_event.set()
        try:
            self.workflow_screen.shutdown()
        except Exception:  # never block closing the window
            pass
        self._save_settings()
        self.root.destroy()
