"""Small themed dialogs shared by the screens (the Tk ``simpledialog`` ones ignore the palette and scale)."""

import tkinter as tk
from tkinter import ttk

from .i18n import tr


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
