"""TextEnhanceAI launcher.

Run this file directly to start the editor (local Ollama or a self-hosted
relay). Settings and scratchpads live in the per-user data directory (see
``core/paths.py``); set ``TEAI_DATA_DIR=.`` to keep them next to this script
as older versions did. A settings file found next to the script is copied
to the data directory once.
"""

import sys
import tkinter as tk
from pathlib import Path

from core.prompts import PROMPTS
from ui.app import EditorApp

__all__ = ["EditorApp", "PROMPTS", "main"]


def script_directory():
    """The directory of this script, or of the executable in a PyInstaller build."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main():
    """Start the TextEnhanceAI desktop application."""
    root = tk.Tk()
    EditorApp(root, app_directory=script_directory())
    root.mainloop()


if __name__ == "__main__":
    main()
