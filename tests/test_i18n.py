"""Tests for the translation helper, the ui_language setting and string extraction."""

import importlib.util
import json
import string
import sys
from pathlib import Path

import pytest

from core.settings import UI_LANGUAGE_AUTO, AppSettings
from ui import i18n
from ui.i18n import current_language, resolve_language, set_language, tr

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def english_after_each_test():
    yield
    set_language("en")


@pytest.fixture
def locales(tmp_path):
    (tmp_path / "de.json").write_text(json.dumps({
        "Save": "Speichern",
        "Connecting to {url} ...": "Verbinde mit {url} ...",
        "Models: {models}": "Modelle: {model}",  # broken placeholder on purpose
        "Cancel": "",
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "xx.json").write_text("not json", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------- tr()
def test_english_is_the_default_and_returns_the_source_string():
    assert current_language() == "en"
    assert tr("Save") == "Save"
    assert tr("Connecting to {url} ...", url="https://relay") == "Connecting to https://relay ..."


def test_translation_is_used_when_present_and_falls_back_otherwise(locales):
    assert set_language("de", locales) == "de"
    assert current_language() == "de"
    assert tr("Save") == "Speichern"
    assert tr("Test connection") == "Test connection"  # not in the table
    assert tr("Cancel") == "Cancel"  # empty translation counts as missing


def test_format_kwargs_apply_to_the_translation(locales):
    set_language("de", locales)
    assert tr("Connecting to {url} ...", url="x") == "Verbinde mit x ..."


def test_broken_placeholders_fall_back_to_the_formatted_source(locales):
    set_language("de", locales)
    assert tr("Models: {models}", models="a, b") == "Models: a, b"


def test_unknown_language_missing_file_and_bad_json_mean_english(locales):
    assert set_language("fr", locales) == "en"
    assert current_language() == "en"
    assert set_language("xx", locales) == "en"  # unreadable file
    assert set_language("de", locales / "nowhere") == "en"  # missing file
    assert set_language("", locales) == "en"
    assert tr("Save") == "Save"


def test_language_codes_are_normalised(locales):
    assert set_language("DE", locales) == "de"
    assert set_language(" en ", locales) == "en"


def test_auto_follows_the_system_locale(monkeypatch, locales):
    monkeypatch.setattr(i18n.locale, "getdefaultlocale", lambda: ("de_AT", "UTF-8"))
    assert resolve_language(UI_LANGUAGE_AUTO) == "de"
    assert set_language("auto", locales) == "de"
    assert tr("Save") == "Speichern"

    monkeypatch.setattr(i18n.locale, "getdefaultlocale", lambda: ("en_GB", "cp65001"))
    assert set_language("auto", locales) == "en"

    monkeypatch.setattr(i18n.locale, "getdefaultlocale", lambda: (None, None))
    assert resolve_language("auto") == "en"
    monkeypatch.setattr(i18n.locale, "getdefaultlocale", lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert resolve_language("auto") == "en"


def test_shipped_german_locale_translates_ui_and_core_labels():
    from core.change_kinds import CHANGE_KIND_LABELS
    from core.workflow import CHECK_LABELS, FALLBACK_EXPLANATIONS

    assert (i18n.LOCALES_DIR / "de.json").exists()
    assert set_language("de") == "de"
    assert tr("Save") == "Speichern"
    assert tr("Accept") == "\u00dcbernehmen" and tr("Reject") == "Verwerfen"
    assert tr(CHECK_LABELS["spelling"]) == "Rechtschreibung"
    assert tr(CHECK_LABELS["grammar"]) == "Grammatik"
    assert tr(CHECK_LABELS["expression"]) == "Ausdruck"
    assert tr(CHANGE_KIND_LABELS["word_choice"]) == "Wortwahl"
    assert tr(FALLBACK_EXPLANATIONS["spelling"]) == "Rechtschreibkorrektur."
    assert tr("Suggestion {number} of {total}", number=2, total=5) == "Vorschlag 2 von 5"
    assert tr("Model output that is not in the table") == "Model output that is not in the table"


def test_marker_returns_the_source_and_format_number_follows_the_language():
    assert i18n.N_("Queued") == "Queued"
    assert i18n.format_number(1234567) == "1,234,567"
    assert i18n.format_number(999) == "999"
    assert i18n.format_number("x") == "x"
    set_language("de")
    assert i18n.format_number(1234567) == "1.234.567"
    assert i18n.format_number(0) == "0"
    set_language("en")
    assert i18n.format_number(12345) == "12,345"


def test_core_source_strings_list_the_english_label_tables():
    from core.settings import BACKEND_LABELS
    from core.workflow import CHECK_DESCRIPTIONS, CHECK_LABELS, EVALUATION_LABELS

    strings = i18n.core_source_strings()
    assert strings == sorted(set(strings))
    for table in (CHECK_LABELS, CHECK_DESCRIPTIONS, EVALUATION_LABELS, BACKEND_LABELS):
        assert set(table.values()) <= set(strings)
    assert "Ollama" in strings and "remote model" in strings


# ------------------------------------------------------------- settings
def test_ui_language_is_seeded_by_environment_and_persisted(tmp_path):
    path = tmp_path / "settings.json"
    assert AppSettings.load(path, environ={}).ui_language == UI_LANGUAGE_AUTO
    assert AppSettings.load(path, environ={"TEAI_LANG": "de"}).ui_language == "de"
    assert AppSettings.load(path, environ={"TEAI_LANG": "EN"}).ui_language == "en"
    assert AppSettings.load(path, environ={"TEAI_LANG": "klingon"}).ui_language == UI_LANGUAGE_AUTO

    settings = AppSettings.load(path, environ={"TEAI_LANG": "en"})
    settings.ui_language = "de"
    assert settings.save() is None
    assert json.loads(path.read_text(encoding="utf-8"))["ui_language"] == "de"
    assert AppSettings.load(path, environ={"TEAI_LANG": "en"}).ui_language == "de"  # file wins


# ------------------------------------------------------------ extraction
def _extract_module():
    spec = importlib.util.spec_from_file_location("extract_strings", ROOT / "scripts" / "extract_strings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_strings_finds_literal_tr_calls(tmp_path):
    extract = _extract_module()
    (tmp_path / "screen.py").write_text(
        'from .i18n import tr\n'
        'a = tr("Save")\n'
        'b = tr("Connecting to {url} ...", url=u)\n'
        'c = tr("two " "parts")\n'
        'd = i18n.tr("Dotted")\n'
        'e = tr(dynamic)\n'
        'f = other("Not me")\n'
        'LABELS = {"x": N_("Marked constant")}\n',
        encoding="utf-8",
    )
    strings = extract.collect_source_strings(tmp_path)
    assert strings == ["Connecting to {url} ...", "Dotted", "Marked constant", "Save", "two parts"]
    assert extract.missing_strings(strings, {"Save": "Speichern", "Dotted": ""}) == [
        "Connecting to {url} ...", "Dotted", "Marked constant", "two parts"
    ]


def test_extract_strings_covers_the_connection_dialog():
    extract = _extract_module()
    strings = extract.collect_source_strings(ROOT / "ui")
    for expected in ("Connection settings", "Save", "Cancel", "Restart TextEnhanceAI to apply the language.",
                     "Connecting to {url} ...", "{busy}/{limit} busy"):
        assert expected in strings
    # Placeholders are named so translators can reorder them.
    assert not any("{0}" in text for text in strings)


def test_every_ui_source_string_has_a_matching_german_translation():
    """New tr()/N_() strings (and core labels the UI shows) cannot land without a German entry."""
    extract = _extract_module()
    strings = extract.collect_source_strings(ROOT / "ui")
    table = extract.load_locale(ROOT / "locales" / "de.json")
    assert extract.missing_strings(strings, table) == []
    formatter = string.Formatter()
    for source in strings:
        fields = {field for _, field, _, _ in formatter.parse(source) if field}
        translated = {field for _, field, _, _ in formatter.parse(table[source]) if field}
        assert fields == translated, "placeholders differ for {0!r}".format(source)
        assert not any("{0}" in text for text in (source, table[source]))
    stale = sorted(set(table) - set(strings))
    assert stale == [], "locales/de.json has entries without a source string"


def test_extract_strings_cli_reports_missing_and_can_update(tmp_path, monkeypatch, capsys):
    extract = _extract_module()
    monkeypatch.setattr(extract, "LOCALES_DIR", tmp_path)
    monkeypatch.setattr(extract, "collect_source_strings", lambda directory=None: ["Save", "Cancel"])
    (tmp_path / "de.json").write_text(json.dumps({"Save": "Speichern"}), encoding="utf-8")

    assert extract.main(["de"]) == 1
    out = capsys.readouterr()
    assert out.out.strip() == "Cancel"
    assert "1 missing" in out.err

    assert extract.main(["de", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"Cancel": ""}

    assert extract.main(["de", "--update"]) == 0
    table = json.loads((tmp_path / "de.json").read_text(encoding="utf-8"))
    assert table == {"Save": "Speichern", "Cancel": ""}
    assert extract.main(["de"]) == 1  # an empty value is still untranslated
