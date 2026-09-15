"""Preferences: language, text size and contrast, quick-edit and review defaults, in one place."""

import copy
import tkinter as tk
from tkinter import ttk

from core.settings import UI_LANGUAGES, UI_SCALE_MAX, UI_SCALE_MIN, UI_SCALE_STEP, clamp_ui_scale
from core.workflow import EVALUATION_LABELS, EVALUATION_MODES, parse_glossary
from .dialogs import center_on
from .i18n import LANGUAGE_LABELS, current_language, resolve_language, tr
from .mode_dialog import ManageModesDialog
from .workflow_screen import GLOSSARY_HINT, GLOSSARY_PLACEHOLDER, STYLE_GUIDE_HINT, StyleGuideBox


class PreferencesDialog(tk.Toplevel):
    """Edit the settings that are not about the connection; ``on_save(settings)`` gets the updated object."""

    def __init__(self, parent, settings, on_save):
        super().__init__(parent)
        self.settings = settings
        self.draft = copy.deepcopy(settings)
        self.on_save = on_save
        self.title(tr("Preferences"))
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda event: self.destroy())
        center_on(self, parent)
        self.grab_set()

    # ---------------------------------------------------------------- layout
    def _build(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self._general_tab(), text=tr("General"))
        self.notebook.add(self._editing_tab(), text=tr("Quick edit"))
        self.notebook.add(self._review_tab(), text=tr("Automatic review"))
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text=tr("Cancel"), command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text=tr("Save"), style="Primary.TButton", command=self.save).pack(side=tk.RIGHT, padx=(0, 6))

    def _general_tab(self):
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text=tr("Language:")).grid(row=0, column=0, sticky="w")
        self._language_codes = [code for code in UI_LANGUAGES if code in LANGUAGE_LABELS]
        self.language_var = tk.StringVar(value=LANGUAGE_LABELS[self.settings.ui_language])
        ttk.Combobox(tab, textvariable=self.language_var, state="readonly", width=14,
                     values=[LANGUAGE_LABELS[code] for code in self._language_codes]).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(tab, text=tr("Takes effect after a restart."), style="Muted.TLabel").grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(2, 10))

        ttk.Label(tab, text=tr("Text size:")).grid(row=2, column=0, sticky="w")
        size_row = ttk.Frame(tab)
        size_row.grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.scale_var = tk.StringVar(value="{0:.0f}".format(self.settings.ui_scale * 100))
        ttk.Spinbox(size_row, from_=int(UI_SCALE_MIN * 100), to=int(UI_SCALE_MAX * 100),
                    increment=int(UI_SCALE_STEP * 100), textvariable=self.scale_var, width=5).pack(side=tk.LEFT)
        ttk.Label(size_row, text="%").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(tab, text=tr("Every screen scales with it; Ctrl+= and Ctrl+- do the same while you work."),
                  style="Muted.TLabel", wraplength=360).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(2, 10))

        self.high_contrast_var = tk.BooleanVar(value=bool(self.settings.high_contrast))
        ttk.Checkbutton(tab, text=tr("High contrast"), variable=self.high_contrast_var).grid(
            row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(tab, text=tr("Black on white, thick borders and a yellow selection, for low vision and Windows "
                               "high-contrast themes."), style="Muted.TLabel", wraplength=420).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
        return tab

    def _editing_tab(self):
        tab = ttk.Frame(self.notebook, padding=14)
        self.explain_var = tk.BooleanVar(value=bool(self.settings.quick_explanations))
        ttk.Checkbutton(tab, text=tr("Explain changes"), variable=self.explain_var).grid(row=0, column=0, sticky="w")
        ttk.Label(tab, text=tr("After every quick edit the model is asked why it changed each passage (one extra "
                               "request); the reasons appear under the suggestion."), style="Muted.TLabel",
                  wraplength=440).grid(row=1, column=0, sticky="w", pady=(2, 14))
        ttk.Label(tab, text=tr("Presets and chains"), style="Heading.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(tab, text=tr("Your own editing instructions and sequences of modes appear in the mode menu of the "
                               "quick editor."), style="Muted.TLabel", wraplength=440).grid(
            row=3, column=0, sticky="w", pady=(2, 6))
        ttk.Button(tab, text=tr("Manage modes..."), command=self._manage_modes).grid(row=4, column=0, sticky="w")
        return tab

    def _review_tab(self):
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(0, weight=1)
        ttk.Label(tab, text=tr("Evaluation of new projects:")).grid(row=0, column=0, sticky="w")
        self.evaluation_var = tk.StringVar(value=self.settings.default_evaluation_mode)
        modes = ttk.Frame(tab)
        modes.grid(row=1, column=0, sticky="w", pady=(2, 12))
        for value in EVALUATION_MODES:
            ttk.Radiobutton(modes, text=tr(EVALUATION_LABELS[value]), value=value, variable=self.evaluation_var).pack(
                anchor="w")
        ttk.Label(tab, text=tr("Author's instructions for new projects"), style="Heading.TLabel").grid(
            row=2, column=0, sticky="w")
        ttk.Label(tab, text=tr(STYLE_GUIDE_HINT), style="Muted.TLabel", wraplength=460).grid(
            row=3, column=0, sticky="w", pady=(2, 6))
        self.style_guide_box = StyleGuideBox(tab, height=4, width=60)
        self.style_guide_box.grid(row=4, column=0, sticky="ew")
        self.style_guide_box.set(self.settings.default_style_guide)
        ttk.Label(tab, text=tr("Protected terms for new projects"), style="Heading.TLabel").grid(
            row=5, column=0, sticky="w", pady=(12, 0))
        ttk.Label(tab, text=tr(GLOSSARY_HINT), style="Muted.TLabel", wraplength=460).grid(
            row=6, column=0, sticky="w", pady=(2, 6))
        self.glossary_box = StyleGuideBox(tab, height=4, width=60, placeholder=GLOSSARY_PLACEHOLDER)
        self.glossary_box.grid(row=7, column=0, sticky="ew")
        self.glossary_box.set("\n".join(self.settings.default_glossary))
        return tab

    # ---------------------------------------------------------------- actions
    def _manage_modes(self):
        ManageModesDialog(self, self.draft.custom_modes, self.draft.chains, on_save=self._store_modes)

    def _store_modes(self, modes, chains):
        self.draft.custom_modes = modes
        self.draft.chains = chains

    def _selected_language(self):
        label = self.language_var.get()
        for code in self._language_codes:
            if LANGUAGE_LABELS[code] == label:
                return code
        return self.settings.ui_language

    def save(self):
        settings = self.settings
        settings.ui_language = self._selected_language()
        settings.ui_scale = clamp_ui_scale(float(self.scale_var.get() or 100) / 100.0)
        settings.high_contrast = bool(self.high_contrast_var.get())
        settings.quick_explanations = bool(self.explain_var.get())
        settings.default_evaluation_mode = self.evaluation_var.get()
        settings.default_style_guide = self.style_guide_box.get_text()
        settings.default_glossary = parse_glossary(self.glossary_box.get_text())
        settings.custom_modes = self.draft.custom_modes
        settings.chains = self.draft.chains
        self.destroy()
        self.on_save(settings, resolve_language(settings.ui_language) != current_language())
