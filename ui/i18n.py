"""Minimal translation helper for the Tkinter screens.

Translations are flat JSON dictionaries in ``locales/<code>.json`` (next to
the ``ui`` package) mapping the English source string to its translation::

    {"Save": "Speichern", "Connecting to {url} ...": "Verbinde mit {url} ..."}

Usage in a screen::

    from .i18n import tr
    ttk.Button(frame, text=tr("Save"))
    status.set(tr("Connecting to {url} ...", url=url))

``tr`` falls back to the English source string whenever no translation is
loaded, the string is missing from the table, or the translation's
placeholders do not match. ``set_language`` is called once at start-up from
the ``ui_language`` setting; ``scripts/extract_strings.py`` lists source
strings that still lack a translation.

Constants that are defined once but shown later (label tables) are marked
with ``N_`` and passed through ``tr`` where they are rendered. English label
tables that live in ``core/`` (check names, change kinds, ...) stay there;
``core_source_strings`` lists them so the extraction script and the
completeness test cover them too. ``format_number`` writes thousands
separators the way the active language does, without touching ``locale``.
"""

import json
import locale
import warnings
from pathlib import Path

from core.settings import UI_LANGUAGE_AUTO, UI_LANGUAGES

DEFAULT_LANGUAGE = "en"
LANGUAGE_LABELS = {
    UI_LANGUAGE_AUTO: "Auto",
    "en": "English",
    "de": "Deutsch",
}
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_active = {"code": DEFAULT_LANGUAGE, "table": {}}


THOUSANDS_SEPARATORS = {"en": ",", "de": "."}


def N_(text):
    """Mark a constant for extraction without translating it yet (``tr`` it where it is shown)."""
    return text


def format_number(value):
    """Format an integer with the active language's thousands separator (no ``locale.setlocale``)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    text = "{0:,}".format(number)
    separator = THOUSANDS_SEPARATORS.get(current_language(), ",")
    return text if separator == "," else text.replace(",", separator)


def core_source_strings():
    """English constants from ``core/`` that the screens show verbatim (translated with ``tr`` at render time)."""
    from core.change_kinds import CHANGE_KIND_LABELS
    from core.consistency import FINDING_LABELS, STATUS_LABELS as FINDING_STATUS_LABELS
    from core.documents import FORMATTING_NOTE, KIND_LABELS
    from core.ollama_service import OllamaService
    from core.remote_service import RemoteService
    from core.settings import BACKEND_LABELS
    from core.workflow import (
        AUTHOR_EXPLANATION,
        CHECK_DESCRIPTIONS,
        CHECK_LABELS,
        EVALUATION_LABELS,
        FALLBACK_EXPLANATIONS,
        FLAG_LABELS,
    )

    strings = set()
    for table in (CHANGE_KIND_LABELS, FINDING_LABELS, FINDING_STATUS_LABELS, KIND_LABELS, BACKEND_LABELS,
                  CHECK_DESCRIPTIONS, CHECK_LABELS, EVALUATION_LABELS, FALLBACK_EXPLANATIONS, FLAG_LABELS):
        strings.update(table.values())
    strings.update((FORMATTING_NOTE, AUTHOR_EXPLANATION, OllamaService.display_name, RemoteService.display_name))
    return sorted(strings)


def tr(text, **kwargs):
    """Return ``text`` translated into the active language, formatted with ``kwargs``."""
    translated = _active["table"].get(text) or text
    if not kwargs:
        return translated
    try:
        return translated.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # A translation with broken placeholders must never crash the UI.
        return text.format(**kwargs)


def current_language():
    """Return the active language code (``"en"`` or ``"de"``)."""
    return _active["code"]


def system_language():
    """Return ``"de"`` when the OS locale is German, else ``"en"``."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            code = locale.getdefaultlocale()[0] or ""
    except (ValueError, TypeError):
        code = ""
    return "de" if code.lower().startswith("de") else DEFAULT_LANGUAGE


def resolve_language(setting):
    """Map a ``ui_language`` setting (``auto|en|de``) to a concrete language code."""
    code = (setting or UI_LANGUAGE_AUTO).strip().lower()
    if code == UI_LANGUAGE_AUTO:
        return system_language()
    return code


def load_table(code, locales_dir=None):
    """Return the translation dictionary for ``code``, or ``None`` when its file is missing or unreadable."""
    path = Path(locales_dir or LOCALES_DIR) / "{0}.json".format(code)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(value, str)
    }


def set_language(code, locales_dir=None):
    """Activate a language; unknown codes and missing files silently mean English.

    ``code`` may be ``"auto"`` (resolved from the OS locale) or a concrete code
    such as ``"de"``. Returns the code that is now active.
    """
    resolved = resolve_language(code)
    table = None
    if resolved != DEFAULT_LANGUAGE and resolved in UI_LANGUAGES:
        table = load_table(resolved, locales_dir)
    if table is None:
        resolved, table = DEFAULT_LANGUAGE, {}
    _active["code"] = resolved
    _active["table"] = table
    return resolved
