"""Dialog for editing custom mode presets and chains of modes."""

import tkinter as tk
from tkinter import messagebox, ttk

from core.prompts import PROMPTS, describe_chain, validate_chains, validate_custom_modes
from .i18n import tr
from .theme import style_text


class ManageModesDialog(tk.Toplevel):
    """List, add, edit and delete presets and chains; ``on_save(modes, chains)`` gets validated lists."""

    def __init__(self, parent, custom_modes, chains, on_save, select_chain=None):
        super().__init__(parent)
        self.on_save = on_save
        self.modes = [{"name": entry["name"], "instruction": entry["instruction"]} for entry in custom_modes]
        self.chains = [{"name": entry["name"], "steps": list(entry["steps"])} for entry in chains]
        self._mode_index = None
        self._chain_index = None
        self.title(tr("Manage modes"))
        self.transient(parent)
        self.minsize(720, 460)
        self._build()
        self._fill_mode_list()
        self._fill_chain_list()
        if select_chain is not None:
            self.notebook.select(1)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda event: self.destroy())
        self.grab_set()
        self._center_on(parent)

    def _center_on(self, parent):
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
            self.geometry("+{0}+{1}".format(max(x, 0), max(y, 0)))
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- layout
    def _build(self):
        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self._build_modes_tab(self.notebook), text=tr("Custom modes"))
        self.notebook.add(self._build_chains_tab(self.notebook), text=tr("Chains"))

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text=tr("Cancel"), command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text=tr("Save"), style="Accent.TButton", command=self.save).pack(side=tk.RIGHT, padx=6)

    def _build_modes_tab(self, parent):
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text=tr("A preset is a named editing instruction that appears in the mode list."),
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        left = ttk.Frame(tab)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 12))
        self.mode_list = tk.Listbox(left, width=24, exportselection=False, activestyle="none")
        self.mode_list.pack(fill=tk.BOTH, expand=True)
        self.mode_list.bind("<<ListboxSelect>>", self._mode_selected)
        mode_buttons = ttk.Frame(left)
        mode_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(mode_buttons, text=tr("Add"), style="Small.TButton", command=self.add_mode).pack(side=tk.LEFT)
        ttk.Button(mode_buttons, text=tr("Delete"), style="Small.TButton", command=self.delete_mode).pack(side=tk.LEFT, padx=4)

        right = ttk.Frame(tab)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text=tr("Name:")).grid(row=0, column=0, sticky="w")
        self.mode_name_var = tk.StringVar()
        self.mode_name_entry = ttk.Entry(right, textvariable=self.mode_name_var)
        self.mode_name_entry.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(right, text=tr("Instruction:")).grid(row=1, column=0, sticky="nw", pady=(4, 0))
        self.mode_text = tk.Text(right, height=8, wrap=tk.WORD, undo=True)
        style_text(self.mode_text, size=10)
        self.mode_text.grid(row=1, column=1, sticky="nsew", pady=(4, 0))
        self.mode_name_var.trace_add("write", lambda *args: self._store_mode())
        self.mode_text.bind("<KeyRelease>", lambda event: self._store_mode())
        self._set_mode_editor_state(False)
        return tab

    def _build_chains_tab(self, parent):
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text=tr("A chain runs several modes in a row; each step edits the previous step's result and "
                               "you review the final text against the original."),
                  style="Muted.TLabel", wraplength=640).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        left = ttk.Frame(tab)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 12))
        self.chain_list = tk.Listbox(left, width=24, exportselection=False, activestyle="none")
        self.chain_list.pack(fill=tk.BOTH, expand=True)
        self.chain_list.bind("<<ListboxSelect>>", self._chain_selected)
        chain_buttons = ttk.Frame(left)
        chain_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(chain_buttons, text=tr("Add"), style="Small.TButton", command=self.add_chain).pack(side=tk.LEFT)
        ttk.Button(chain_buttons, text=tr("Delete"), style="Small.TButton", command=self.delete_chain).pack(side=tk.LEFT, padx=4)

        right = ttk.Frame(tab)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text=tr("Name:")).grid(row=0, column=0, sticky="w")
        self.chain_name_var = tk.StringVar()
        self.chain_name_entry = ttk.Entry(right, textvariable=self.chain_name_var)
        self.chain_name_entry.grid(row=0, column=1, sticky="ew", pady=2)
        self.chain_name_var.trace_add("write", lambda *args: self._store_chain_name())
        ttk.Label(right, text=tr("Steps:")).grid(row=1, column=0, sticky="nw", pady=(4, 0))
        steps = ttk.Frame(right)
        steps.grid(row=1, column=1, sticky="nsew", pady=(4, 0))
        steps.columnconfigure(0, weight=1)
        steps.rowconfigure(0, weight=1)
        self.step_list = tk.Listbox(steps, height=8, exportselection=False, activestyle="none")
        self.step_list.grid(row=0, column=0, sticky="nsew")
        side = ttk.Frame(steps)
        side.grid(row=0, column=1, sticky="n", padx=(6, 0))
        self.step_buttons = [
            ttk.Button(side, text=tr("Remove"), style="Small.TButton", command=self.remove_step),
            ttk.Button(side, text=tr("Up"), style="Small.TButton", command=lambda: self.move_step(-1)),
            ttk.Button(side, text=tr("Down"), style="Small.TButton", command=lambda: self.move_step(1)),
        ]
        for button in self.step_buttons:
            button.pack(fill=tk.X, pady=(0, 4))
        add_row = ttk.Frame(steps)
        add_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.step_choice_var = tk.StringVar()
        self.step_choice = ttk.Combobox(add_row, textvariable=self.step_choice_var, state="readonly", width=26)
        self.step_choice.pack(side=tk.LEFT)
        self.add_step_button = ttk.Button(add_row, text=tr("Add step"), style="Small.TButton", command=self.add_step)
        self.add_step_button.pack(side=tk.LEFT, padx=6)
        self.chain_preview_var = tk.StringVar()
        ttk.Label(right, textvariable=self.chain_preview_var, style="Muted.TLabel", wraplength=420).grid(
            row=2, column=1, sticky="w", pady=(6, 0))
        self._set_chain_editor_state(False)
        return tab

    # ------------------------------------------------------------- presets
    def _fill_mode_list(self, select=None):
        self.mode_list.delete(0, tk.END)
        for entry in self.modes:
            self.mode_list.insert(tk.END, entry["name"] or tr("(unnamed)"))
        if select is not None and self.modes:
            self.mode_list.selection_set(select)
            self.mode_list.see(select)
        self._mode_selected()
        self.step_choice.configure(values=self._step_choices())
        if self.step_choice_var.get() not in self._step_choices():
            self.step_choice_var.set(self._step_choices()[0])

    def _step_choices(self):
        return list(PROMPTS) + [entry["name"] for entry in self.modes if entry["name"]]

    def _set_mode_editor_state(self, enabled):
        self.mode_name_entry.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        self.mode_text.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _mode_selected(self, event=None):
        selection = self.mode_list.curselection()
        self._mode_index = None
        if not selection:
            self._set_mode_editor_state(True)
            self.mode_name_var.set("")
            self.mode_text.delete("1.0", tk.END)
            self._set_mode_editor_state(False)
            return
        entry = self.modes[selection[0]]
        self._set_mode_editor_state(True)
        self.mode_name_var.set(entry["name"])
        self.mode_text.delete("1.0", tk.END)
        self.mode_text.insert("1.0", entry["instruction"])
        self._mode_index = selection[0]

    def _store_mode(self):
        if self._mode_index is None:
            return
        entry = self.modes[self._mode_index]
        entry["name"] = self.mode_name_var.get().strip()
        entry["instruction"] = self.mode_text.get("1.0", "end-1c").strip()
        self.mode_list.delete(self._mode_index)
        self.mode_list.insert(self._mode_index, entry["name"] or tr("(unnamed)"))
        self.mode_list.selection_set(self._mode_index)
        self.step_choice.configure(values=self._step_choices())

    def add_mode(self):
        self.modes.append({"name": "", "instruction": ""})
        self._fill_mode_list(select=len(self.modes) - 1)
        self.mode_name_entry.focus_set()

    def delete_mode(self):
        if self._mode_index is None:
            return
        name = self.modes[self._mode_index]["name"]
        del self.modes[self._mode_index]
        for chain in self.chains:
            chain["steps"] = [step for step in chain["steps"] if step != name]
        self._fill_mode_list()
        self._fill_chain_list(select=self._chain_index)

    # -------------------------------------------------------------- chains
    def _fill_chain_list(self, select=None):
        self.chain_list.delete(0, tk.END)
        for entry in self.chains:
            self.chain_list.insert(tk.END, entry["name"] or tr("(unnamed)"))
        if select is not None and 0 <= select < len(self.chains):
            self.chain_list.selection_set(select)
            self.chain_list.see(select)
        self._chain_selected()

    def _set_chain_editor_state(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.chain_name_entry.configure(state=state)
        self.step_list.configure(state=state)
        self.add_step_button.configure(state=state)
        self.step_choice.configure(state="readonly" if enabled else tk.DISABLED)
        for button in self.step_buttons:
            button.configure(state=state)

    def _chain_selected(self, event=None):
        selection = self.chain_list.curselection()
        self._chain_index = None
        if not selection:
            self._set_chain_editor_state(True)
            self.chain_name_var.set("")
            self.step_list.delete(0, tk.END)
            self.chain_preview_var.set("")
            self._set_chain_editor_state(False)
            return
        entry = self.chains[selection[0]]
        self._set_chain_editor_state(True)
        self.chain_name_var.set(entry["name"])
        self._fill_steps(entry)
        self._chain_index = selection[0]

    def _fill_steps(self, entry, select=None):
        self.step_list.delete(0, tk.END)
        for step in entry["steps"]:
            self.step_list.insert(tk.END, step)
        if select is not None and 0 <= select < len(entry["steps"]):
            self.step_list.selection_set(select)
        self.chain_preview_var.set(describe_chain(entry["steps"]))

    def _store_chain_name(self):
        if self._chain_index is None:
            return
        entry = self.chains[self._chain_index]
        entry["name"] = self.chain_name_var.get().strip()
        self.chain_list.delete(self._chain_index)
        self.chain_list.insert(self._chain_index, entry["name"] or tr("(unnamed)"))
        self.chain_list.selection_set(self._chain_index)

    def add_chain(self):
        self.chains.append({"name": "", "steps": []})
        self._fill_chain_list(select=len(self.chains) - 1)
        self.chain_name_entry.focus_set()

    def delete_chain(self):
        if self._chain_index is None:
            return
        del self.chains[self._chain_index]
        self._fill_chain_list()

    def add_step(self):
        if self._chain_index is None or not self.step_choice_var.get():
            return
        entry = self.chains[self._chain_index]
        entry["steps"].append(self.step_choice_var.get())
        self._fill_steps(entry, select=len(entry["steps"]) - 1)

    def _selected_step(self):
        selection = self.step_list.curselection()
        return selection[0] if selection else None

    def remove_step(self):
        index = self._selected_step()
        if self._chain_index is None or index is None:
            return
        entry = self.chains[self._chain_index]
        del entry["steps"][index]
        self._fill_steps(entry, select=min(index, len(entry["steps"]) - 1))

    def move_step(self, delta):
        index = self._selected_step()
        if self._chain_index is None or index is None:
            return
        steps = self.chains[self._chain_index]["steps"]
        target = index + delta
        if not 0 <= target < len(steps):
            return
        steps[index], steps[target] = steps[target], steps[index]
        self._fill_steps(self.chains[self._chain_index], select=target)

    # ---------------------------------------------------------------- save
    def save(self):
        modes, problems = validate_custom_modes(self.modes)
        chains, chain_problems = validate_chains(self.chains, modes)
        problems += chain_problems
        if problems:
            messagebox.showerror(
                tr("Cannot save"),
                tr("Please fix these entries first:") + "\n\n- " + "\n- ".join(problems),
                parent=self,
            )
            return
        self.on_save(modes, chains)
        self.destroy()
