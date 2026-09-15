"""Per-user data directory resolution and the one-time settings migration."""

import json
from pathlib import Path

from core.paths import APP_DIR_NAME, SETTINGS_FILENAME, ensure_dir, migrate_legacy_settings, user_data_dir
from core.scratchpad import ScratchpadLogger
from core.settings import AppSettings


def test_data_dir_follows_the_platform_conventions(tmp_path):
    home = tmp_path / "home"
    assert user_data_dir({"APPDATA": str(tmp_path / "Roaming")}, "win32", home) == tmp_path / "Roaming" / APP_DIR_NAME
    assert user_data_dir({}, "win32", home) == home / "AppData" / "Roaming" / APP_DIR_NAME
    assert user_data_dir({}, "darwin", home) == home / "Library" / "Application Support" / APP_DIR_NAME
    assert user_data_dir({}, "linux", home) == home / ".config" / APP_DIR_NAME
    assert user_data_dir({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, "linux", home) == tmp_path / "xdg" / APP_DIR_NAME


def test_teai_data_dir_overrides_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert user_data_dir({"TEAI_DATA_DIR": "."}, "win32", tmp_path / "home") == tmp_path.resolve()
    custom = tmp_path / "custom"
    assert user_data_dir({"TEAI_DATA_DIR": str(custom)}, "darwin") == custom.resolve()
    assert AppSettings.default_path({"TEAI_DATA_DIR": str(custom)}) == custom.resolve() / SETTINGS_FILENAME


def test_settings_and_scratchpads_default_to_the_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TEAI_DATA_DIR", str(data_dir))
    settings = AppSettings.load()
    assert settings.path == data_dir.resolve() / SETTINGS_FILENAME
    assert settings.save() is None  # the directory is created on demand
    assert settings.path.is_file()

    logger = ScratchpadLogger()
    assert logger.directory == data_dir.resolve()
    assert ensure_dir(tmp_path / "a" / "b").is_dir()


def test_legacy_settings_are_migrated_once(tmp_path):
    legacy = ensure_dir(tmp_path / "checkout")
    data_dir = tmp_path / "data"
    assert migrate_legacy_settings(legacy, data_dir) is None  # nothing to migrate

    (legacy / SETTINGS_FILENAME).write_text(json.dumps({"backend": "remote", "remote_url": "https://r"}), "utf-8")
    migrated = migrate_legacy_settings(legacy, data_dir)
    assert migrated == data_dir / SETTINGS_FILENAME
    assert AppSettings.load(migrated, environ={}).remote_url == "https://r"
    assert (legacy / SETTINGS_FILENAME).exists()  # the original stays for older versions

    (legacy / SETTINGS_FILENAME).write_text(json.dumps({"remote_url": "https://changed"}), "utf-8")
    assert migrate_legacy_settings(legacy, data_dir) is None  # only once: the data dir file wins now
    assert AppSettings.load(migrated, environ={}).remote_url == "https://r"
    assert migrate_legacy_settings(legacy, legacy) is None  # TEAI_DATA_DIR=.: same file, nothing to do


def test_version_is_the_single_source():
    from core import RELEASES_URL, __version__
    from core.remote_service import USER_AGENT
    from ui.app import APP_TITLE

    assert USER_AGENT == "TextEnhanceAI/" + __version__
    assert __version__ in APP_TITLE
    assert RELEASES_URL.startswith("https://github.com/") and RELEASES_URL.endswith("/releases")
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "TextEnhanceAI.spec").read_text("utf-8")
    assert "from core import __version__" in spec
