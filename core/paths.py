"""Where the app keeps its per-user files (settings, scratchpads).

The location follows the platform conventions so a packaged build never
writes next to its executable:

- Windows: ``%APPDATA%\\TextEnhanceAI``
- macOS: ``~/Library/Application Support/TextEnhanceAI``
- elsewhere: ``$XDG_CONFIG_HOME/TextEnhanceAI`` (default ``~/.config/TextEnhanceAI``)

``TEAI_DATA_DIR`` overrides all of that; developers running from a checkout
set ``TEAI_DATA_DIR=.`` to keep the settings file and scratchpads next to
the script as before. Review projects are unaffected: they live next to the
manuscript (``<name>.teai/``).
"""

import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "TextEnhanceAI"
SETTINGS_FILENAME = "TextEnhanceAI-settings.json"


def user_data_dir(environ=None, platform=None, home=None):
    """Return the per-user data directory (not created); see the module docstring."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    override = str(environ.get("TEAI_DATA_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    home = Path(home) if home is not None else Path.home()
    if platform.startswith("win"):
        base = str(environ.get("APPDATA", "") or "").strip()
        root = Path(base) if base else home / "AppData" / "Roaming"
    elif platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        base = str(environ.get("XDG_CONFIG_HOME", "") or "").strip()
        root = Path(base).expanduser() if base else home / ".config"
    return root / APP_DIR_NAME


def ensure_dir(path):
    """Create ``path`` (and parents) when missing; returns it."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_settings(legacy_dir, data_dir, filename=SETTINGS_FILENAME):
    """Copy a settings file from next to the script into the data dir, once.

    Returns the destination path when a copy was made, otherwise None (no
    legacy file, or the data dir already has one - which is also the case
    with ``TEAI_DATA_DIR=.``, where both are the same file). The legacy file
    is left in place so an older version or a checkout run still finds it.
    """
    source = Path(legacy_dir) / filename
    destination = Path(data_dir) / filename
    if not source.is_file() or destination.exists():
        return None
    ensure_dir(destination.parent)
    shutil.copy2(str(source), str(destination))
    return destination
