#!/usr/bin/env python
"""List UI source strings that have no translation in a locale file.

Scans ``ui/`` for ``tr("...")`` and ``N_("...")`` calls (the string must be
a literal, adjacent literals count as one), adds the English label tables of
``core/`` that the screens show (``ui.i18n.core_source_strings``) and prints
every source string missing from ``locales/<code>.json``. Exit status is 1
when something is missing, so the script doubles as a check.

    python scripts/extract_strings.py            # against locales/de.json
    python scripts/extract_strings.py fr         # against locales/fr.json
    python scripts/extract_strings.py de --json  # missing strings as a JSON skeleton
    python scripts/extract_strings.py de --update  # add the missing keys ("" = untranslated)
"""

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
LOCALES_DIR = ROOT / "locales"


MARKERS = ("tr", "N_")  # N_ marks constants that are tr()'d where they are shown


def source_strings(path):
    """Return the literal first arguments of ``tr(...)`` / ``N_(...)`` calls in one Python file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in MARKERS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append(first.value)
    return found


def collect_source_strings(directory=UI_DIR, include_core=None):
    """Return the sorted set of source strings used by every ``tr()`` / ``N_()`` call under ``directory``.

    ``include_core`` (default: only for the real ``ui/`` directory) adds the
    English label tables of ``core/`` that the screens translate at render time.
    """
    strings = set()
    for path in sorted(Path(directory).rglob("*.py")):
        strings.update(source_strings(path))
    if include_core is None:
        include_core = Path(directory).resolve() == UI_DIR.resolve()
    if include_core:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from ui.i18n import core_source_strings

        strings.update(core_source_strings())
    return sorted(strings)


def load_locale(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def missing_strings(strings, table):
    """Return the source strings without a non-empty translation in ``table``."""
    return [text for text in strings if not table.get(text)]


def _display_path(path):
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("locale", nargs="?", default="de", help="language code (default: de)")
    parser.add_argument("--json", action="store_true", help="print the missing strings as a JSON object")
    parser.add_argument(
        "--update", action="store_true",
        help="append the missing keys with empty values to the locale file",
    )
    args = parser.parse_args(argv)

    locale_path = LOCALES_DIR / "{0}.json".format(args.locale)
    strings = collect_source_strings()
    table = load_locale(locale_path)
    missing = missing_strings(strings, table)

    if args.update and missing:
        for text in missing:
            table.setdefault(text, "")
        locale_path.parent.mkdir(parents=True, exist_ok=True)
        locale_path.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({text: "" for text in missing}, ensure_ascii=False, indent=2))
    else:
        for text in missing:
            print(text)
        print(
            "{0} source string(s) in ui/, {1} missing from {2}".format(
                len(strings), len(missing), _display_path(locale_path)
            ),
            file=sys.stderr,
        )
    return 1 if missing and not args.update else 0


if __name__ == "__main__":
    sys.exit(main())
