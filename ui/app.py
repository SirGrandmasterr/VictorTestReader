"""Main Tkinter application for TextEnhanceAI v0.13."""

import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from core.backend import EditCancelled, OutputTruncated
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
from core.prompts import EDITING_MODES, PROMPTS, build_chain, build_instruction, describe_chain, validate_custom_modes
from core.scratchpad import ScratchpadLogger
from core.services import build_service
from core.settings import (
    BACKEND_LABELS,
    BACKEND_OLLAMA,
    BACKEND_REMOTE,
    SETTINGS_FILENAME,
    AppSettings,
)
from core.text_positions import char_offset, normalise_span, tk_index
from .connection_dialog import ConnectionDialog
from .i18n import current_language, resolve_language, set_language, tr
from .mode_dialog import ManageModesDialog
from .review_panel import ReviewPanel
from .theme import PALETTE, apply_theme, font, style_text
from .workflow_screen import WorkflowScreen

COLOR_OK = PALETTE["success"]
COLOR_WARN = PALETTE["warning"]
COLOR_ERROR = PALETTE["danger"]
COLOR_NEUTRAL = PALETTE["header_muted"]
MODE_QUICK = "quick"
MODE_AUTO = "auto"
SEPARATOR_CUSTOM = "\u2014 Custom modes \u2014"  # unselectable headings in the mode list
SEPARATOR_CHAINS = "\u2014 Chains \u2014"
APP_TITLE = "TextEnhanceAI - V 0.13"
FILE_TYPES = [
    ("Documents", MANUSCRIPT_PATTERNS),
    ("Text files", "*.txt *.md *.text *.markdown"),
    ("Word documents", "*.docx"),
    ("OpenDocument text", "*.odt"),
    ("All files", "*.*"),
]


def window_title(path, modified):
    """Title bar text: the file name (or Untitled) with a bullet while there are unsaved changes."""
    if path is None and not modified:
        return APP_TITLE
    name = Path(path).name if path else "Untitled"
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
    ):
        self.root = root
        self.app_directory = Path(app_directory or Path.cwd())
        self.settings = settings or AppSettings.load(self.app_directory / SETTINGS_FILENAME)
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
        self.generation_verb = "Generating review"  # or "Editing selection (N words)"
        self._progress_chars = 0
        self.current_session = None
        self.current_logger = None
        self.last_applied_source = None
        self._suppress_modified = False
        self.mode = MODE_QUICK
        self._controls_locked = False
        self._previous_mode = "Grammar"
        self.last_custom_instruction = ""  # offered by "Save as preset..." after a Custom request
        self.current_path = None  # quick-editor file (Path) or None while untitled
        self.current_document = None  # documents.LoadedDocument the editor text came from
        self.modified = False

        self.root.title(APP_TITLE)
        self.root.geometry("1120x780")
        self.root.minsize(900, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._configure_style()
        self._build_menu()
        self._build_interface()
        self._bind_shortcuts()
        if self.settings.load_error:
            self.set_status(self.settings.load_error)
        self.root.after(100, self._poll_events)
        self.root.after(150, self.refresh_models)

    # --------------------------------------------------------------- services
    def _remote_from_settings(self):
        return build_service(self.settings, BACKEND_REMOTE)

    def _configure_style(self):
        apply_theme(self.root)

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.file_menu = tk.Menu(menubar, tearoff=False)
        self.file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_file)
        self.file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_file)
        self.file_menu.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_file_as)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=False)
        self.file_menu.add_cascade(label="Recent", menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Send to automatic review", command=self.send_to_review)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Quit", command=self.close)
        menubar.add_cascade(label="File", menu=self.file_menu)
        self.root.config(menu=menubar)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self.recent_menu.delete(0, tk.END)
        for entry in self.settings.recent_files:
            self.recent_menu.add_command(label=self._recent_label(entry), command=lambda p=entry: self.open_file(p))
        if self.settings.recent_files:
            self.recent_menu.add_separator()
            self.recent_menu.add_command(label="Clear list", command=self._clear_recent)
        else:
            self.recent_menu.add_command(label="(no recent files)", state=tk.DISABLED)

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
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="TextEnhanceAI", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, text="local & remote LLM editing", style="HeaderMuted.TLabel").pack(
            side=tk.LEFT, padx=(8, 18), pady=(3, 0)
        )
        self.mode_buttons = {}
        for mode, label in ((MODE_QUICK, "Quick edit"), (MODE_AUTO, "Automatic review")):
            button = ttk.Button(
                header, text=label, style="Nav.TButton", command=lambda m=mode: self.switch_mode(m)
            )
            button.pack(side=tk.LEFT, padx=(0, 4))
            self.mode_buttons[mode] = button
        self.connection_var = tk.StringVar(value="Checking...")
        self.connection_label = tk.Label(
            header,
            textvariable=self.connection_var,
            anchor="e",
            font=font(9, "bold"),
            background=PALETTE["header"],
            foreground=PALETTE["header_muted"],
        )
        self.connection_label.pack(side=tk.RIGHT)

        top_bar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(12, 6))
        top_bar.grid(row=1, column=0, sticky="ew")
        ttk.Label(top_bar, text="Backend", style="Toolbar.TLabel").pack(side=tk.LEFT)
        self.backend_var = tk.StringVar(value=BACKEND_LABELS[self.settings.backend])
        self.backend_combo = ttk.Combobox(
            top_bar,
            textvariable=self.backend_var,
            state="readonly",
            width=18,
            values=[BACKEND_LABELS[key] for key in (BACKEND_OLLAMA, BACKEND_REMOTE)],
        )
        self.backend_combo.pack(side=tk.LEFT, padx=(6, 14))
        self.backend_combo.bind("<<ComboboxSelected>>", self._on_backend_selected)

        ttk.Label(top_bar, text="Model", style="Toolbar.TLabel").pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=self.settings.preferred_model())
        self.model_combo = ttk.Combobox(
            top_bar,
            textvariable=self.model_var,
            state="readonly",
            width=30,
            values=(self.model_var.get(),) if self.model_var.get() else (),
        )
        self.model_combo.pack(side=tk.LEFT, padx=(6, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.refresh_button = ttk.Button(
            top_bar, text="Refresh models", command=self.refresh_models
        )
        self.refresh_button.pack(side=tk.LEFT)
        self.connection_button = ttk.Button(
            top_bar, text="Connection...", command=self.open_connection_dialog
        )
        self.connection_button.pack(side=tk.LEFT, padx=(6, 0))

        self.content = ttk.Frame(self.root)
        self.content.grid(row=2, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self.editor_frame = ttk.Frame(self.content, padding=(12, 10, 12, 4))
        self.editor_frame.pack(fill=tk.BOTH, expand=True)
        editor_heading = ttk.Label(
            self.editor_frame,
            text="Text to improve",
            style="Title.TLabel",
        )
        editor_heading.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.text_area = scrolledtext.ScrolledText(self.editor_frame, undo=True)
        style_text(self.text_area, size=11)
        self.text_area.grid(row=1, column=0, sticky="nsew")
        self.text_area.bind("<<Modified>>", self._on_text_modified)
        self.editor_frame.columnconfigure(0, weight=1)
        self.editor_frame.rowconfigure(1, weight=1)

        editor_meta = ttk.Frame(self.editor_frame)
        editor_meta.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.count_var = tk.StringVar(value="0 words · 0 characters")
        ttk.Label(editor_meta, textvariable=self.count_var).pack(side=tk.RIGHT)

        controls = ttk.LabelFrame(self.editor_frame, text="Editing request", padding=7)
        controls.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        ttk.Label(controls, text="Editing mode:").grid(row=0, column=0, sticky="w")
        self.mode_var = tk.StringVar(value="Grammar")
        self.mode_combo = ttk.Combobox(
            controls,
            textvariable=self.mode_var,
            state="readonly",
            values=self._mode_values(),
            width=24,
        )
        self.mode_combo.grid(row=0, column=1, padx=6, sticky="w")
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_selected)
        self.review_button = ttk.Button(
            controls,
            text="Review changes",
            style="Primary.TButton",
            command=self.start_review,
        )
        self.review_button.grid(row=0, column=2, padx=(8, 4))
        self.undo_button = ttk.Button(
            controls,
            text="Undo applied review",
            command=self.undo_applied_review,
            state=tk.DISABLED,
        )
        self.undo_button.grid(row=0, column=3, padx=4)
        self.explain_var = tk.BooleanVar(value=self.settings.quick_explanations)
        ttk.Checkbutton(
            controls, text="Explain changes", variable=self.explain_var, command=self._on_explain_toggled
        ).grid(row=0, column=4, padx=(10, 4), sticky="w")
        controls.columnconfigure(4, weight=1)
        self.save_preset_button = ttk.Button(
            controls, text="Save as preset...", command=self.save_custom_preset
        )
        self.save_preset_button.grid(row=0, column=5, padx=4)
        self.save_preset_button.grid_remove()  # shown once a Custom instruction was entered
        ttk.Button(controls, text="Manage modes...", command=self.open_mode_dialog).grid(row=0, column=6)
        self.mode_description_var = tk.StringVar(value=PROMPTS["Grammar"])
        ttk.Label(
            controls,
            textvariable=self.mode_description_var,
            wraplength=720,
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(5, 0))

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
            bottom, text="Cancel", command=self.cancel_generation, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=6)
        self.status_var = tk.StringVar(
            value="Paste text, choose an editing mode, then review suggestions."
        )
        self.status_label = ttk.Label(bottom, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        self.status_label.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )
        ttk.Button(bottom, text="Quit", command=self.close).pack(side=tk.RIGHT)
        self.progress.pack_forget()
        self.cancel_button.pack_forget()

    def _bind_shortcuts(self):
        self.root.bind_all("<Control-Return>", self._primary_shortcut)
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
        return None

    def _down_shortcut(self, event=None):
        if self._in_auto():
            return self.workflow_screen.select_next_change(event)
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
        self.model_combo.configure(state="disabled" if busy else "readonly")
        self.backend_combo.configure(state="disabled" if busy else "readonly")
        self.refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.connection_button.configure(state=tk.DISABLED if busy else tk.NORMAL)

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

    def _update_counts(self):
        text = self.text_area.get("1.0", "end-1c")
        words = len(re.findall(r"\S+", text))
        self.count_var.set(
            "{0} words · {1} characters".format(words, len(text))
        )

    # ------------------------------------------------------------- modes
    def _mode_values(self):
        """Built-in modes, then the custom presets and chains under unselectable headings."""
        values = list(EDITING_MODES)
        custom = self.settings.custom_mode_names()
        if custom:
            values += [SEPARATOR_CUSTOM] + custom
        chains = self.settings.chain_names()
        if chains:
            values += [SEPARATOR_CHAINS] + chains
        return values

    def _refresh_mode_values(self, select=None):
        values = self._mode_values()
        self.mode_combo.configure(values=values)
        if select in values:
            self.mode_var.set(select)
        elif self.mode_var.get() not in values:
            self.mode_var.set("Grammar")
        self._previous_mode = self.mode_var.get()
        self._update_mode_description()

    def _on_mode_selected(self, event=None):
        mode = self.mode_var.get()
        if mode in (SEPARATOR_CUSTOM, SEPARATOR_CHAINS):
            self.mode_var.set(self._previous_mode)  # headings cannot be chosen
            return
        self._previous_mode = mode
        self._update_mode_description()

    def _update_mode_description(self, event=None):
        mode = self.mode_var.get()
        descriptions = {
            "Translate": "Translate the complete text into a language you choose.",
            "Custom": "Enter a custom editing instruction before generation.",
        }
        if mode in descriptions or mode in PROMPTS:
            self.mode_description_var.set(descriptions.get(mode, PROMPTS.get(mode, "")))
            return
        steps = self.settings.find_chain(mode)
        if steps is not None:
            self.mode_description_var.set("Chain: {0}".format(describe_chain(steps)))
            return
        self.mode_description_var.set(build_instruction(mode, custom_modes=self.settings.custom_modes)
                                      if mode in self.settings.custom_mode_names() else "")

    def _on_explain_toggled(self):
        self.settings.quick_explanations = bool(self.explain_var.get())
        self._save_settings()

    def save_custom_preset(self):
        """Store the last Custom instruction under a name of the user's choice."""
        instruction = self.last_custom_instruction.strip()
        if not instruction:
            return
        name = simpledialog.askstring("Save as preset", "Name for this instruction:", parent=self.root)
        if not name or not name.strip():
            return
        modes, problems = validate_custom_modes(self.settings.custom_modes + [{"name": name, "instruction": instruction}])
        if problems:
            messagebox.showerror("Cannot save preset", "\n".join(problems))
            return
        self.settings.custom_modes = modes
        self._save_settings()
        self._refresh_mode_values(select=modes[-1]["name"])
        self.set_status("Preset \u201c{0}\u201d saved.".format(modes[-1]["name"]))

    def open_mode_dialog(self):
        ManageModesDialog(self.root, self.settings.custom_modes, self.settings.chains, on_save=self._apply_modes)

    def _apply_modes(self, modes, chains):
        self.settings.custom_modes = modes
        self.settings.chains = chains
        self._save_settings()
        self._refresh_mode_values()
        self.set_status("{0} custom mode(s) and {1} chain(s) saved.".format(len(modes), len(chains)))

    def set_status(self, message):
        self.status_var.set(message)

    def _set_connection(self, message, color):
        self.connection_var.set(message)
        header_colors = {
            COLOR_OK: "#7ee2a8",
            COLOR_WARN: "#ffd27a",
            COLOR_ERROR: "#ff9b8f",
        }
        self.connection_label.configure(foreground=header_colors.get(color, PALETTE["header_muted"]))

    # ------------------------------------------------------- backend switching
    def _save_settings(self):
        error = self.settings.save()
        if error:
            self.set_status(error)

    def _on_backend_selected(self, event=None):
        label = self.backend_var.get()
        backend = next(
            (key for key, value in BACKEND_LABELS.items() if value == label),
            BACKEND_OLLAMA,
        )
        self._switch_backend(backend)

    def _switch_backend(self, backend):
        if self.generating or self._controls_locked:
            self.backend_var.set(BACKEND_LABELS[self.settings.backend])
            return
        self.settings.backend = backend
        self.service = self.services[backend]
        self.backend_var.set(BACKEND_LABELS[backend])
        self.model_var.set(self.settings.preferred_model(backend))
        self.model_combo.configure(values=(self.model_var.get(),) if self.model_var.get() else ())
        self._save_settings()
        if backend == BACKEND_REMOTE and not self.settings.remote_configured:
            self._set_connection("Not configured", COLOR_WARN)
            self.set_status("Choose Connection... to enter the relay address and API key.")
            return
        self.refresh_models()

    def _on_model_selected(self, event=None):
        model = self.model_var.get().strip()
        if model:
            self.settings.remember_model(self.settings.backend, model)
            self._save_settings()

    def open_connection_dialog(self):
        if self.generating or self._controls_locked:
            return
        ConnectionDialog(self.root, self.settings, on_save=self._apply_connection_settings)

    def _apply_connection_settings(self, settings):
        self.settings = settings
        self.services[BACKEND_REMOTE] = self._remote_from_settings()
        self._save_settings()
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
            self._set_connection("Not configured", COLOR_WARN)
            self.set_status("Choose Connection... to enter the relay address and API key.")
            return
        self.refresh_button.configure(state=tk.DISABLED)
        self._set_connection("Checking {0}...".format(service.display_name), COLOR_NEUTRAL)

        def worker():
            try:
                models = service.list_models()
                summary = service.connection_summary()
                self.events.put(("models", backend, models, summary))
            except Exception as exc:
                self.events.put(("model_error", backend, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_models(self, backend, models, summary):
        self.refresh_button.configure(state=tk.NORMAL)
        if backend != self.settings.backend:
            return  # the user switched backends while this request was running
        self.model_combo.configure(values=models)
        if not models:
            self.model_var.set("")
            self._set_connection("Model missing", COLOR_WARN)
            self.set_status(self.service.no_models_hint())
            return
        preferred = self.settings.preferred_model(backend) or self.model_var.get()
        chosen = preferred if preferred in models else models[0]
        self.model_var.set(chosen)
        self.settings.remember_model(backend, chosen)
        self._save_settings()
        self._set_connection(summary or "Connected", COLOR_OK)
        self.set_status(
            "{0} ready with {1} model{2}. Paste text, choose an editing mode, then "
            "review suggestions.".format(
                self.service.display_name.capitalize(),
                len(models),
                "" if len(models) == 1 else "s",
            )
        )

    def _handle_model_error(self, backend, error):
        self.refresh_button.configure(state=tk.NORMAL)
        if backend != self.settings.backend:
            return
        self.model_combo.configure(values=())
        self._set_connection("Unavailable", COLOR_ERROR)
        self.set_status(str(error))

    # -------------------------------------------------------------- generation
    def _get_instruction(self):
        """Return the ``(name, instruction)`` steps for the chosen mode, or None when the user backed out."""
        mode = self.mode_var.get()
        if mode == "Translate":
            language = simpledialog.askstring(
                "Translate", "Target language:", parent=self.root
            )
            if not language or not language.strip():
                return None
            return [(mode, build_instruction(mode, language.strip()))]
        if mode == "Custom":
            custom = simpledialog.askstring(
                "Custom instruction", "Editing instruction:", parent=self.root,
                initialvalue=self.last_custom_instruction or None,
            )
            if not custom or not custom.strip():
                return None
            self.last_custom_instruction = custom.strip()
            self.save_preset_button.grid()
            return [(mode, build_instruction(mode, custom.strip()))]
        steps = self.settings.find_chain(mode)
        if steps is not None:
            try:
                return list(zip(steps, build_chain(steps, self.settings.custom_modes)))
            except ValueError as exc:
                messagebox.showerror("Chain not usable", str(exc))
                return None
        try:
            return [(mode, build_instruction(mode, custom_modes=self.settings.custom_modes))]
        except ValueError as exc:
            messagebox.showerror("Unknown mode", str(exc))
            return None

    @staticmethod
    def _describe_steps(mode, steps):
        """What the scratchpad and review record as the instruction."""
        if len(steps) == 1:
            return steps[0][1]
        return "Chain \u201c{0}\u201d: {1}".format(mode, describe_chain(name for name, _ in steps))

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
                messagebox.showinfo("TextEnhanceAI", "The selected passage contains no text. Select text or clear the selection.")
            else:
                messagebox.showinfo("TextEnhanceAI", "Enter or paste text to edit.")
            return
        model = self.model_var.get().strip()
        if not model:
            messagebox.showerror(
                "Model missing",
                "No model is available on the selected backend. "
                + self.service.no_models_hint(),
            )
            return
        steps = self._get_instruction()
        if steps is None:
            return
        mode = self.mode_var.get()
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
        self.generation_label = "{0} via {1}".format(model, service.display_name)
        scope = " \u00b7 selection ({0} words)".format(len(re.findall(r"\S+", source))) if selection else ""
        if selection:
            self.generation_verb = "Editing selection ({0} words)".format(len(re.findall(r"\S+", source)))
        else:
            self.generation_verb = "Generating review"
        self._progress_chars = 0
        self._set_generating_state(True)
        cancel_event = self.cancel_event

        def on_progress(received):
            self._progress_chars = received

        def on_step(index, total, name):
            self._progress_chars = 0
            if total > 1:
                self.generation_verb = "Step {0}/{1}: {2}{3}".format(index, total, name, scope)

        def worker():
            try:
                chain = run_chain(
                    service, model, steps, source, cancel_event, on_step=on_step, on_progress=on_progress
                )
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
                    self.generation_verb = "Explaining changes"
                    self._progress_chars = 0
                    try:
                        explain_session(service, model, session, explain_instruction, cancel_event)
                    except EditCancelled:
                        pass  # the edit itself is done: open the review without explanations
                    except Exception:
                        pass  # explanations are best effort
                self.events.put(("generation_result", request_id, revision_id, session))
            except EditCancelled as exc:
                self.events.put(("generation_cancelled", request_id, exc))
            except Exception as exc:
                self.events.put(("generation_error", request_id, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _set_generating_state(self, generating):
        self.generating = generating
        editor_state = tk.DISABLED if generating else tk.NORMAL
        self.text_area.configure(state=editor_state)
        self.mode_combo.configure(state="disabled" if generating else "readonly")
        self._update_control_states()
        self.review_button.configure(state=tk.DISABLED if generating else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if generating else tk.DISABLED)
        if generating:
            self.progress.pack(side=tk.LEFT, before=self.status_label)
            self.cancel_button.pack(side=tk.LEFT, padx=6, before=self.status_label)
            self.progress.start(12)
            self.set_status("{0} with {1}...".format(self.generation_verb, self.generation_label))
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.cancel_button.pack_forget()

    def cancel_generation(self):
        if self.generating and self.cancel_event:
            self.cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED)
            self.set_status("Cancelling generation...")

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
                "The text changed; the stale result from {0} was discarded.".format(model)
            )
            return

        self.current_logger = ScratchpadLogger(self.app_directory)
        self.current_logger.log_proposal(session)
        if not session.review_items:
            self.current_logger.log_outcome(session, "no changes", session.original_text)
            self.current_logger = None
            self.set_status("{0} did not suggest any changes.".format(model))
            messagebox.showinfo("Review complete", "No changes were suggested.")
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
            title = "Response truncated"
        else:
            title = "{0} error".format(self.service.display_name.capitalize())
            self._set_connection("Unavailable", COLOR_ERROR)
        self.set_status(str(error))
        messagebox.showerror(title, str(error))

    def _handle_generation_cancelled(self, request_id):
        if request_id != self.active_request_id:
            return
        self._finish_generation()
        self.set_status("Generation cancelled. Your text was not changed.")

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
                elif kind.startswith("workflow_"):
                    self.workflow_screen.handle_event(event)
        except queue.Empty:
            pass

        if self.generating and self.generation_started_at and not (
            self.cancel_event and self.cancel_event.is_set()
        ):
            elapsed = int(time.time() - self.generation_started_at)
            received = self._progress_chars
            if received:
                progress = " · {0} characters received".format(received)
            else:
                progress = " · waiting for the first tokens"
            self.set_status(
                "{0} with {1}... {2}s elapsed{3}".format(
                    self.generation_verb, self.generation_label, elapsed, progress
                )
            )
        try:
            self.root.after(100, self._poll_events)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ review
    def apply_review(self, session):
        if session.pending_count:
            self.set_status("Review every pending change before applying.")
            return
        final_text = render_reviewed_text(session)
        current = self.text_area.get("1.0", "end-1c")
        span = None
        if session.selection:
            span = self._locate_selection(current, session)
            if span is None:
                self.set_status("The editor text changed; the selected passage could not be found, so nothing was applied.")
                return
        self.last_applied_source = current
        session.state = "applied"
        if self.current_logger:
            self.current_logger.log_outcome(session, "applied", final_text)
        if span:
            start, end = span
            self._set_editor_text(current[:start] + final_text + current[end:])
            self._select_span(start, start + len(final_text))
        else:
            self._set_editor_text(final_text)
        self.undo_button.configure(state=tk.NORMAL)
        self._leave_review("Reviewed changes applied.")

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
        self._leave_review("Review discarded. The original text was kept.")

    def _leave_review(self, status):
        self.review_panel.pack_forget()
        self.editor_frame.pack(fill=tk.BOTH, expand=True)
        self.current_session = None
        self.current_logger = None
        self.set_status(status)
        self.text_area.focus_set()

    def _set_editor_text(self, text, modified=True):
        """Replace the editor text; ``modified=False`` when it now matches the file on disk."""
        self._suppress_modified = True
        self.text_area.configure(state=tk.NORMAL)
        self.text_area.delete("1.0", tk.END)
        self.text_area.insert("1.0", text)
        self.text_area.edit_modified(False)
        self._suppress_modified = False
        self.revision_id += 1
        self._update_counts()
        self._set_modified(modified)

    def undo_applied_review(self):
        if self.last_applied_source is None:
            return
        source = self.last_applied_source
        self.last_applied_source = None
        self._set_editor_text(source)
        self.undo_button.configure(state=tk.DISABLED)
        self.set_status("The last applied review was undone.")

    # ------------------------------------------------------------------- files
    def _confirm_discard(self):
        """Offer to save unsaved editor changes; False when the user cancels the action."""
        if not self.modified:
            return True
        name = self.current_path.name if self.current_path else "Untitled"
        answer = messagebox.askyesnocancel("Unsaved changes", "Save the changes to {0}?".format(name))
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
            path = filedialog.askopenfilename(title="Open", filetypes=FILE_TYPES, parent=self.root)
            if not path:
                return False
        try:
            document = load_document(path)
        except (OSError, DocumentError) as exc:
            messagebox.showerror("Cannot open file", str(exc))
            self.settings.forget_file(path)
            self._rebuild_recent_menu()
            return False
        if self.current_session:
            self.discard_review(self.current_session)  # a review of the old text makes no sense any more
        self.current_path = Path(path)
        self.current_document = document
        self.last_applied_source = None
        self.undo_button.configure(state=tk.DISABLED)
        self._set_editor_text(document.text, modified=False)
        self.settings.remember_file(self.current_path)
        self._save_settings()
        self._rebuild_recent_menu()
        if self.mode != MODE_QUICK:
            self.switch_mode(MODE_QUICK)
        self.set_status("Opened {0}.".format(self.current_path))
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
            title="Save As", defaultextension=suffix, filetypes=FILE_TYPES, parent=self.root,
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
            messagebox.showerror("Cannot save file", str(exc))
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
        message = "Saved {0}.".format(self.current_path)
        if warnings:
            message += " " + " ".join(warnings) + "."
            messagebox.showwarning("Saved with limitations", "\n".join(warnings))
        self.set_status(message)
        return True

    def send_to_review(self):
        """Hand the editor's file to the automatic review (saving first, since it works on files)."""
        if self.generating:
            return
        if self.current_session:
            messagebox.showinfo("Review in progress", "Apply or discard the current review first.")
            return
        if not self.text_area.get("1.0", "end-1c").strip():
            messagebox.showinfo("TextEnhanceAI", "Enter or open text first.")
            return
        if self.current_path is None or self.modified:
            if not messagebox.askyesno(
                "Save first?",
                "The automatic review works on files. Save the text {0} now?".format(
                    "to a file" if self.current_path is None else "to {0}".format(self.current_path.name)),
            ):
                return
            if not self.save_file():
                return
        path = self.current_path
        self.switch_mode(MODE_AUTO)
        if self.workflow_screen.active:
            messagebox.showinfo(
                "Review project open",
                "Close the current review project to start a new one with {0}.".format(path.name),
            )
            return
        self.workflow_screen.start_view.set_file(str(path))
        self.set_status("{0} is ready for the automatic review; check the options and start.".format(path.name))

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
