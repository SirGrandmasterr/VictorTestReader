"""Dialog for choosing the backend and configuring the remote relay profiles."""

import copy
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core.remote_service import RemoteService, normalise_api_key
from core.secrets import INSTALL_HINT, keyring_available
from core.settings import BACKEND_LABELS, BACKEND_OLLAMA, BACKEND_REMOTE, UI_LANGUAGES
from .i18n import LANGUAGE_LABELS, tr
from .theme import PALETTE, style_text


class ConnectionDialog(tk.Toplevel):
    """Edit backend/relay settings with a live connection test, plus UI preferences.

    Relay connections are profiles: the combobox at the top of the relay box
    picks the one the fields below edit; Add/Rename/Delete manage the list.
    All profile edits happen on a draft copy of the settings and reach the
    real object only through Save, so Cancel reverts them.

    Strings in this dialog are wrapped in ``tr()`` as the worked example for
    localisation; the other screens follow later.
    """

    def __init__(self, parent, settings, on_save, remote_factory=None):
        super().__init__(parent)
        self.settings = settings
        self.draft = copy.deepcopy(settings)  # profiles and model memory are edited here until Save
        self.on_save = on_save
        self.remote_factory = remote_factory or self._default_remote_factory
        self._test_token = 0
        self.title(tr("Connection settings"))
        self.transient(parent)
        self.resizable(False, False)
        self._build_widgets()
        self._update_remote_state()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Return>", self._save_shortcut)
        self.grab_set()
        self.url_entry.focus_set()
        self._center_on(parent)

    @staticmethod
    def _default_remote_factory(url, api_key, max_tokens, enable_thinking):
        return RemoteService(
            url, api_key, max_tokens=max_tokens, enable_thinking=enable_thinking
        )

    def _center_on(self, parent):
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
            self.geometry("+{0}+{1}".format(max(x, 0), max(y, 0)))
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- layout
    def _build_widgets(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        backend_box = ttk.LabelFrame(body, text=tr("Where should the model run?"), padding=8)
        backend_box.pack(fill=tk.X)
        self.backend_var = tk.StringVar(value=self.settings.backend)
        ttk.Radiobutton(
            backend_box,
            text=tr("{backend} — models installed on this computer", backend=tr(BACKEND_LABELS[BACKEND_OLLAMA])),
            value=BACKEND_OLLAMA,
            variable=self.backend_var,
            command=self._update_remote_state,
        ).pack(anchor="w")
        ttk.Radiobutton(
            backend_box,
            text=tr("{backend} — a GPU server reached through your relay", backend=tr(BACKEND_LABELS[BACKEND_REMOTE])),
            value=BACKEND_REMOTE,
            variable=self.backend_var,
            command=self._update_remote_state,
        ).pack(anchor="w")

        self.remote_box = ttk.LabelFrame(body, text=tr("Remote relay"), padding=8)
        self.remote_box.pack(fill=tk.X, pady=(10, 0))
        self.remote_box.columnconfigure(1, weight=1)

        ttk.Label(self.remote_box, text=tr("Profile:")).grid(row=0, column=0, sticky="w")
        profile_row = ttk.Frame(self.remote_box)
        profile_row.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(6, 0))
        self.profile_var = tk.StringVar(value=self.draft.active_profile)
        self.profile_combo = ttk.Combobox(profile_row, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self.add_button = ttk.Button(profile_row, text=tr("Add..."), command=self.add_profile, width=8)
        self.add_button.pack(side=tk.LEFT, padx=(6, 0))
        self.rename_button = ttk.Button(profile_row, text=tr("Rename..."), command=self.rename_profile, width=9)
        self.rename_button.pack(side=tk.LEFT, padx=(4, 0))
        self.delete_button = ttk.Button(profile_row, text=tr("Delete"), command=self.delete_profile, width=7)
        self.delete_button.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(self.remote_box, text=tr("Relay URL:")).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.url_var = tk.StringVar(value="")
        self.url_entry = ttk.Entry(self.remote_box, textvariable=self.url_var, width=46)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=(8, 0))

        ttk.Label(self.remote_box, text=tr("API key:")).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.key_var = tk.StringVar(value="")
        self.key_entry = ttk.Entry(self.remote_box, textvariable=self.key_var, show="•", width=36)
        self.key_entry.grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.remote_box,
            text=tr("Show"),
            variable=self.show_key_var,
            command=self._toggle_key_visibility,
        ).grid(row=2, column=2, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self.remote_box, text=tr("Max output tokens:")).grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )
        self.max_tokens_var = tk.StringVar(value="")
        self.max_tokens_spin = ttk.Spinbox(
            self.remote_box,
            from_=256,
            to=65536,
            increment=512,
            textvariable=self.max_tokens_var,
            width=10,
        )
        self.max_tokens_spin.grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        self.thinking_var = tk.BooleanVar(value=False)
        self.thinking_check = ttk.Checkbutton(
            self.remote_box,
            text=tr("Allow the model to think before answering (slower, may improve quality)"),
            variable=self.thinking_var,
        )
        self.thinking_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._refresh_profile_list()
        self._load_profile(self.draft.active_profile)

        keyring_row = ttk.Frame(self.remote_box)
        keyring_row.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.keyring_available = keyring_available()
        self.keyring_var = tk.BooleanVar(value=bool(self.draft.use_keyring))
        self.keyring_check = ttk.Checkbutton(
            keyring_row,
            text=tr("Store keys in the system keyring (recommended)"),
            variable=self.keyring_var,
            command=self._update_storage_note,
        )
        self.keyring_check.pack(side=tk.LEFT)
        if not self.keyring_available:
            self.keyring_check.configure(state=tk.DISABLED)
            ttk.Label(keyring_row, text=tr("(not available: {hint})", hint=INSTALL_HINT),
                      style="Muted.TLabel").pack(side=tk.LEFT, padx=(6, 0))

        test_row = ttk.Frame(self.remote_box)
        test_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.test_button = ttk.Button(test_row, text=tr("Test connection"), command=self.test_connection)
        self.test_button.pack(side=tk.LEFT)
        self.test_status_var = tk.StringVar(value="")
        self.test_status_label = ttk.Label(test_row, textvariable=self.test_status_var, wraplength=360)
        self.test_status_label.pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)

        self.details = tk.Text(self.remote_box, height=5, width=60)
        style_text(self.details, size=9, background=PALETTE["surface_alt"], readonly=True, mono=True)
        self.details.configure(padx=6, pady=4, highlightthickness=0)
        self.details.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        preferences = ttk.LabelFrame(body, text=tr("Preferences"), padding=8)
        preferences.pack(fill=tk.X, pady=(10, 0))
        preferences.columnconfigure(1, weight=1)
        ttk.Label(preferences, text=tr("Language:")).grid(row=0, column=0, sticky="w")
        self._language_codes = [code for code in UI_LANGUAGES if code in LANGUAGE_LABELS]
        self.language_var = tk.StringVar(value=self._language_label(self.settings.ui_language))
        self.language_combo = ttk.Combobox(
            preferences,
            textvariable=self.language_var,
            values=[self._language_label(code) for code in self._language_codes],
            state="readonly",
            width=14,
        )
        self.language_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.language_combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        self.language_status_var = tk.StringVar(value="")
        ttk.Label(preferences, textvariable=self.language_status_var, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        self.storage_note_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.storage_note_var, wraplength=480, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )
        self._update_storage_note()

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text=tr("Cancel"), command=self.destroy).pack(side=tk.RIGHT)
        self.save_button = ttk.Button(buttons, text=tr("Save"), command=self.save, style="Primary.TButton")
        self.save_button.pack(side=tk.RIGHT, padx=(0, 6))

    # -------------------------------------------------------------- profiles
    def _refresh_profile_list(self):
        names = self.draft.profile_names()
        self.profile_combo.configure(values=names)
        self.profile_var.set(self.draft.active_profile)
        self.delete_button.configure(state=tk.NORMAL if len(names) > 1 else tk.DISABLED)

    def _load_profile(self, name):
        """Show ``name``'s fields; it becomes the draft's active profile."""
        self.draft.set_active_profile(name)
        profile = self.draft.profile
        self.url_var.set(profile["url"])
        self.key_var.set(profile["api_key"])
        self.max_tokens_var.set(str(profile["max_tokens"]))
        self.thinking_var.set(profile["enable_thinking"])
        self.profile_var.set(profile["name"])

    def _store_fields(self):
        """Write the fields into the draft's active profile."""
        self.draft.remote_url = self.url_var.get().strip()
        self.draft.remote_api_key = self._current_api_key()
        self.draft.remote_max_tokens = self._current_max_tokens()
        self.draft.remote_enable_thinking = bool(self.thinking_var.get())

    def _on_profile_selected(self, event=None):
        self._store_fields()
        self._load_profile(self.profile_var.get())
        self.test_status_var.set("")
        self._set_details("")

    def _ask_name(self, title, prompt, initial=""):
        name = simpledialog.askstring(title, prompt, initialvalue=initial, parent=self)
        if name is None:
            return None
        name = " ".join(name.split())
        if not name:
            messagebox.showerror(title, tr("Enter a name for the profile."), parent=self)
            return None
        return name

    def add_profile(self):
        name = self._ask_name(tr("Add profile"), tr("Name of the new relay profile:"))
        if name is None:
            return
        self._store_fields()
        if self.draft.add_profile(name) is None:
            messagebox.showerror(tr("Add profile"), tr("A profile called {name} already exists.", name=name), parent=self)
            return
        self._refresh_profile_list()
        self._load_profile(name)
        self._refresh_profile_list()
        self.url_entry.focus_set()

    def rename_profile(self):
        current = self.draft.active_profile
        name = self._ask_name(tr("Rename profile"), tr("New name for {name}:", name=current), initial=current)
        if name is None or name == current:
            return
        if not self.draft.rename_profile(current, name):
            messagebox.showerror(tr("Rename profile"), tr("A profile called {name} already exists.", name=name), parent=self)
            return
        self._refresh_profile_list()

    def delete_profile(self):
        current = self.draft.active_profile
        if len(self.draft.remote_profiles) <= 1:
            return
        if not messagebox.askyesno(
            tr("Delete profile"), tr("Delete the relay profile {name}?", name=current), parent=self
        ):
            return
        self.draft.delete_profile(current)
        self._refresh_profile_list()
        self._load_profile(self.draft.active_profile)
        self.test_status_var.set("")
        self._set_details("")

    # --------------------------------------------------------------- actions
    def _language_label(self, code):
        # Language names are shown in their own language, so they are not translated.
        return LANGUAGE_LABELS.get(code) or LANGUAGE_LABELS[self._language_codes[0]]

    def _selected_language(self):
        label = self.language_var.get()
        for code in self._language_codes:
            if self._language_label(code) == label:
                return code
        return self.settings.ui_language

    def _on_language_selected(self, event=None):
        """Persist the language immediately; it takes effect after a restart."""
        code = self._selected_language()
        if code == self.settings.ui_language:
            return
        self.settings.ui_language = code
        error = self.settings.save()
        self.language_status_var.set(error or tr("Restart TextEnhanceAI to apply the language."))

    def _toggle_key_visibility(self):
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _keyring_selected(self):
        return self.keyring_available and bool(self.keyring_var.get())

    def _update_storage_note(self):
        """The plain-text warning is shown only while the keys are going to live in the file."""
        path = self.settings.path or "TextEnhanceAI-settings.json"
        if self._keyring_selected():
            self.storage_note_var.set(tr(
                "Settings are saved in {path}; the relay API keys are kept in the system keyring.", path=path
            ))
        else:
            self.storage_note_var.set(tr(
                "Settings are saved in {path} (the API key is stored in plain text).", path=path
            ))

    def _update_remote_state(self):
        remote = self.backend_var.get() == BACKEND_REMOTE
        state = tk.NORMAL if remote else tk.DISABLED
        for widget in (
            self.url_entry,
            self.key_entry,
            self.max_tokens_spin,
            self.thinking_check,
            self.test_button,
            self.add_button,
            self.rename_button,
        ):
            widget.configure(state=state)
        self.keyring_check.configure(state=state if self.keyring_available else tk.DISABLED)
        self.profile_combo.configure(state="readonly" if remote else tk.DISABLED)
        self.delete_button.configure(
            state=tk.NORMAL if remote and len(self.draft.remote_profiles) > 1 else tk.DISABLED
        )

    def _set_details(self, text):
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state=tk.DISABLED)

    MAX_OUTPUT_TOKENS = 65536

    def _current_max_tokens(self):
        try:
            value = int(self.max_tokens_var.get().strip())
        except ValueError:
            return self.draft.remote_max_tokens
        value = min(self.MAX_OUTPUT_TOKENS, max(256, value))
        self.max_tokens_var.set(str(value))
        return value

    def _current_api_key(self):
        key = normalise_api_key(self.key_var.get())
        self.key_var.set(key)
        return key

    def _build_remote(self):
        return self.remote_factory(
            self.url_var.get().strip(),
            self._current_api_key(),
            self._current_max_tokens(),
            self.thinking_var.get(),
        )

    def test_connection(self):
        url = self.url_var.get().strip()
        if not url:
            self.test_status_var.set(tr("Enter the relay URL first."))
            return
        self._test_token += 1
        token = self._test_token
        self.test_button.configure(state=tk.DISABLED)
        self.test_status_var.set(tr("Connecting to {url} ...", url=url))
        self._set_details("")

        def worker():
            try:
                service = self._build_remote()
                models = service.list_models()
                summary = service.connection_summary()
                details = describe_status(service.last_status, models)
                outcome = ("ok", summary, details)
            except Exception as exc:  # surfaced to the user, never raised in Tk
                outcome = ("error", str(exc), "")
            try:
                self.after(0, lambda: self._show_test_result(token, outcome))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_test_result(self, token, outcome):
        if token != self._test_token:
            return
        kind, message, details = outcome
        try:
            self.test_button.configure(state=tk.NORMAL)
        except tk.TclError:
            return
        if kind == "ok":
            self.test_status_var.set("\u25cf " + message)
            self.test_status_label.configure(foreground=PALETTE["success"])
        else:
            self.test_status_var.set("\u2716 " + message)
            self.test_status_label.configure(foreground=PALETTE["danger"])
        self._set_details(details)

    def _save_shortcut(self, event=None):
        if self.focus_get() is not self.details:
            self.save()
        return "break"

    def save(self):
        backend = self.backend_var.get()
        url = self.url_var.get().strip()
        if backend == BACKEND_REMOTE and not url:
            self.test_status_var.set("\u2716 " + tr("Enter the relay URL before saving."))
            self.test_status_label.configure(foreground=PALETTE["danger"])
            return
        self._store_fields()
        self.settings.backend = backend
        self.settings.remote_profiles = [dict(profile) for profile in self.draft.remote_profiles]
        self.settings.active_profile = self.draft.active_profile  # the selected profile becomes active
        self.settings.models = dict(self.draft.models)  # renamed/deleted profiles took their model memory along
        if self.keyring_available:
            self.settings.use_keyring = bool(self.keyring_var.get())  # save() migrates the keys either way
        self.destroy()
        self.on_save(self.settings)


def describe_status(status, models):
    """Render a compact multi-line description of a relay status document."""
    lines = []
    if models:
        lines.append(tr("Models: {models}", models=", ".join(models)))
    else:
        lines.append(tr("Models: none available yet"))
    agents = (status or {}).get("agents") or []
    if not agents:
        if status is None:
            lines.append(tr("The server did not expose /status (plain OpenAI-compatible endpoint)."))
        else:
            lines.append(tr("GPU agents: none connected"))
    for agent in agents:
        parts = [agent.get("name", "agent"), agent.get("state", "unknown")]
        in_flight = agent.get("in_flight")
        if in_flight is not None:
            parts.append(tr("{busy}/{limit} busy", busy=in_flight, limit=agent.get("max_concurrency", "?")))
        agent_models = agent.get("models") or []
        if agent_models:
            parts.append(", ".join(str(m.get("id", m)) if isinstance(m, dict) else str(m) for m in agent_models))
        lines.append(tr("Agent: {details}", details=" · ".join(str(part) for part in parts)))
    relay = (status or {}).get("relay") or {}
    if relay.get("version"):
        lines.append(tr(
            "Relay version {version}, {count} requests served",
            version=relay.get("version"), count=relay.get("requests_total", 0),
        ))
    return "\n".join(lines)
