"""Small themed dialogs shared by the screens (the Tk ``simpledialog`` ones ignore the palette and scale)."""

import tkinter as tk
from tkinter import ttk

from .i18n import N_, tr
from .theme import font


def center_on(window, parent):
    """Place ``window`` over the middle of ``parent``'s toplevel."""
    window.update_idletasks()
    try:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - window.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - window.winfo_height()) // 2
        window.geometry("+{0}+{1}".format(max(x, 0), max(y, 0)))
    except tk.TclError:
        pass


class NameDialog(tk.Toplevel):
    """Ask for one line of text; ``result`` is the stripped text, or None when cancelled."""

    def __init__(self, parent, title, prompt, initial="", ok_label=None, width=40):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        body = ttk.Frame(self, padding=(16, 14))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=prompt, wraplength=360, justify=tk.LEFT).pack(anchor="w")
        self.var = tk.StringVar(value=initial or "")
        self.entry = ttk.Entry(body, textvariable=self.var, width=width)
        self.entry.pack(fill=tk.X, pady=(8, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text=tr("Cancel"), style="Ghost.TButton", command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(buttons, text=ok_label or tr("OK"), style="Primary.TButton", command=self._ok).pack(
            side=tk.RIGHT, padx=(0, 6))
        self.bind("<Return>", lambda event: self._ok())
        self.bind("<Escape>", lambda event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        center_on(self, parent)
        self.grab_set()
        self.entry.focus_set()
        self.entry.selection_range(0, tk.END)

    def _ok(self):
        self.result = " ".join(self.var.get().split())
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def ask_name(parent, title, prompt, initial="", ok_label=None):
    """Modal one-line prompt; returns the text (whitespace collapsed) or None when cancelled."""
    dialog = NameDialog(parent, title, prompt, initial=initial, ok_label=ok_label)
    dialog.wait_window()
    return dialog.result


# (heading, [(keys, what it does)]) - the source of truth for the shortcut list, see EditorApp._bind_shortcuts
SHORTCUT_GROUPS = (
    (N_("Everywhere"), (
        ("Ctrl+O", N_("Open a text file")),
        ("Ctrl+S", N_("Save the text")),
        ("Ctrl+Shift+S", N_("Save the text under a new name")),
        ("Ctrl+= / Ctrl+-", N_("Larger / smaller text")),
        ("Ctrl+0", N_("Normal text size")),
        ("Ctrl+Shift+T", N_("Watch the model's reasoning")),
        ("F1", N_("This list")),
    )),
    (N_("Quick edit"), (
        ("Ctrl+Enter", N_("Suggest edits; apply them once every suggestion is decided")),
        ("Alt+A / Alt+R", N_("Accept / reject the shown sentence")),
        ("Alt+← / Alt+→", N_("Previous / next suggestion")),
        ("Alt+↑ / Alt+↓", N_("Select the previous / next individual change")),
    )),
    (N_("Automatic review"), (
        ("Alt+A / Alt+R", N_("Accept / reject the selected change (or the selected rows of the list)")),
        ("Alt+Z", N_("Undo the last decision, bulk actions as a whole")),
        ("F2", N_("Reword the selected suggestion")),
        ("Ctrl+E", N_("Turn the selected text into your own correction")),
        ("Alt+← / Alt+→", N_("Previous / next segment")),
        ("Alt+↑ / Alt+↓", N_("Select the previous / next change")),
        ("Right click", N_("Evaluate a segment or chapter again (tree); add a correction (text)")),
    )),
)


class ShortcutsDialog(tk.Toplevel):
    """Every keyboard shortcut of the app in three columns (Help menu, F1)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title(tr("Keyboard shortcuts"))
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        body = ttk.Frame(self, padding=(18, 14))
        body.pack(fill=tk.BOTH, expand=True)
        columns = ttk.Frame(body)
        columns.pack(fill=tk.BOTH, expand=True)
        for column, (heading, entries) in enumerate(SHORTCUT_GROUPS):
            group = ttk.Frame(columns, style="Card.TFrame", padding=(14, 10))
            group.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
            ttk.Label(group, text=tr(heading), style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                              pady=(0, 6))
            for row, (keys, what) in enumerate(entries, 1):
                ttk.Label(group, text=keys, style="Surface.TLabel", font=font(10, "bold")).grid(
                    row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
                ttk.Label(group, text=tr(what), style="SurfaceMuted.TLabel", wraplength=220, justify=tk.LEFT).grid(
                    row=row, column=1, sticky="w", pady=2)
        ttk.Button(body, text=tr("Close"), command=self.destroy).pack(side=tk.RIGHT, pady=(12, 0))
        self.bind("<Escape>", lambda event: self.destroy())
        center_on(self, parent)
        self.focus_set()
