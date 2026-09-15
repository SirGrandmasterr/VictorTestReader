"""Relay keys in the OS keyring: settings round trip, migration and fallbacks (fake keyring)."""

import json

import pytest

from core import secrets
from core.secrets import KEYRING_PLACEHOLDER
from core.settings import BACKEND_REMOTE, AppSettings


class FakeKeyring:
    """Dict-backed stand-in for the keyring functions of ``core.secrets``."""

    def __init__(self, available=True, refuse=False):
        self.available = available
        self.refuse = refuse
        self.store = {}
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(secrets, "keyring_available", lambda: self.available)
        monkeypatch.setattr(secrets, "store_key", self.store_key)
        monkeypatch.setattr(secrets, "load_key", self.load_key)
        monkeypatch.setattr(secrets, "delete_key", self.delete_key)
        return self

    def store_key(self, profile, key):
        self.calls.append(("store", profile))
        if not self.available or self.refuse:
            return False
        self.store[profile] = key
        return True

    def load_key(self, profile):
        self.calls.append(("load", profile))
        return self.store.get(profile) if self.available else None

    def delete_key(self, profile):
        self.calls.append(("delete", profile))
        return self.store.pop(profile, None) is not None


@pytest.fixture
def fake(monkeypatch):
    return FakeKeyring().install(monkeypatch)


def stored(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_keys_round_trip_through_the_keyring(tmp_path, fake):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    settings.backend = BACKEND_REMOTE
    settings.remote_url = "https://one.example.com"
    settings.remote_api_key = "key-one"
    settings.add_profile("Two", url="https://two.example.com", api_key="key-two")
    settings.use_keyring = True
    assert settings.keyring_in_use is True
    assert settings.save() is None

    data = stored(path)
    assert data["use_keyring"] is True
    assert [p["api_key"] for p in data["remote_profiles"]] == [KEYRING_PLACEHOLDER, KEYRING_PLACEHOLDER]
    assert data["remote_api_key"] == KEYRING_PLACEHOLDER  # the flat mirror does not leak either
    assert "key-one" not in path.read_text(encoding="utf-8")
    assert fake.store == {"Default": "key-one", "Two": "key-two"}
    assert settings.remote_api_key == "key-one"  # in memory the real key stays

    fake.calls.clear()
    settings.save()  # unchanged keys are not written again
    assert ("store", "Default") not in fake.calls

    reloaded = AppSettings.load(path, environ={})
    assert reloaded.use_keyring is True
    assert reloaded.remote_api_key == "key-one"
    reloaded.set_active_profile("Two")
    assert reloaded.remote_api_key == "key-two"
    assert reloaded.load_error == ""


def test_placeholder_without_keyring_package_is_explained_and_preserved(tmp_path, monkeypatch):
    fake = FakeKeyring(available=False).install(monkeypatch)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "use_keyring": True,
        "remote_profiles": [{"name": "Default", "url": "https://r", "api_key": KEYRING_PLACEHOLDER}],
    }), encoding="utf-8")

    settings = AppSettings.load(path, environ={})
    assert settings.remote_api_key == ""
    assert "keyring package is not installed" in settings.load_error
    assert "pip install keyring" in settings.load_error
    assert settings.use_keyring is True and settings.keyring_in_use is False
    assert fake.calls == []  # nothing is asked of a missing keyring

    settings.remember_model(BACKEND_REMOTE, "m")
    settings.save()  # e.g. after choosing a model: the unread key must survive
    assert stored(path)["remote_profiles"][0]["api_key"] == KEYRING_PLACEHOLDER
    assert stored(path)["remote_api_key"] == KEYRING_PLACEHOLDER

    settings.remote_api_key = "typed-again"  # the user enters it on this machine: plain text here
    settings.save()
    assert stored(path)["remote_profiles"][0]["api_key"] == "typed-again"


def test_missing_entry_in_an_available_keyring_asks_for_the_key(tmp_path, fake):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "remote_profiles": [{"name": "Default", "url": "https://r", "api_key": KEYRING_PLACEHOLDER}],
    }), encoding="utf-8")

    settings = AppSettings.load(path, environ={})
    assert settings.use_keyring is True  # implied by the placeholder
    assert settings.remote_api_key == ""
    assert "enter it again" in settings.load_error
    settings.save()
    assert stored(path)["remote_profiles"][0]["api_key"] == KEYRING_PLACEHOLDER
    settings.remote_api_key = "fresh"
    settings.save()
    assert fake.store == {"Default": "fresh"}
    assert stored(path)["remote_profiles"][0]["api_key"] == KEYRING_PLACEHOLDER


def test_migration_file_to_keyring_and_back(tmp_path, fake):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"remote_url": "https://r", "remote_api_key": "plain"}), encoding="utf-8")
    settings = AppSettings.load(path, environ={})
    assert settings.use_keyring is False

    settings.use_keyring = True  # the checkbox
    settings.save()
    assert fake.store == {"Default": "plain"}
    assert stored(path)["remote_profiles"][0]["api_key"] == KEYRING_PLACEHOLDER

    settings = AppSettings.load(path, environ={})
    assert settings.remote_api_key == "plain"
    settings.use_keyring = False
    settings.save()
    assert fake.store == {}  # the keyring copy is deleted
    assert stored(path)["remote_profiles"][0]["api_key"] == "plain"
    assert stored(path)["use_keyring"] is False
    assert AppSettings.load(path, environ={}).remote_api_key == "plain"


def test_environment_key_wins_over_the_keyring_and_is_never_stored(tmp_path, fake):
    fake.store["Default"] = "stored"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "use_keyring": True,
        "remote_profiles": [
            {"name": "Default", "url": "https://r", "api_key": KEYRING_PLACEHOLDER},
            {"name": "Other", "url": "https://o", "api_key": KEYRING_PLACEHOLDER},
        ],
    }), encoding="utf-8")
    fake.store["Other"] = "other-key"

    settings = AppSettings.load(path, environ={"TEAI_REMOTE_API_KEY": "from-env"})
    assert settings.remote_api_key == "from-env"
    settings.set_active_profile("Other")
    assert settings.remote_api_key == "other-key"  # only the active profile takes the environment
    settings.set_active_profile("Default")
    settings.save()
    assert fake.store["Default"] == "stored"
    assert stored(path)["remote_profiles"][0]["api_key"] == KEYRING_PLACEHOLDER
    assert stored(path)["remote_api_key"] == KEYRING_PLACEHOLDER


def test_renamed_and_deleted_profiles_take_their_stored_keys_along(tmp_path, fake):
    settings = AppSettings.load(tmp_path / "settings.json", environ={})
    settings.use_keyring = True
    settings.remote_api_key = "k-default"
    settings.add_profile("Two", api_key="k-two")
    settings.add_profile("Three", api_key="k-three")
    settings.save()
    assert set(fake.store) == {"Default", "Two", "Three"}

    settings.rename_profile("Two", "Zwei")
    settings.delete_profile("Three")
    settings.remote_api_key = ""  # cleared key: removed from the keyring too
    settings.save()
    assert fake.store == {"Zwei": "k-two"}
    assert [p["api_key"] for p in stored(settings.path)["remote_profiles"]] == ["", KEYRING_PLACEHOLDER]


def test_a_refusing_keyring_keeps_the_key_in_the_file(tmp_path, monkeypatch):
    FakeKeyring(refuse=True).install(monkeypatch)
    settings = AppSettings.load(tmp_path / "settings.json", environ={})
    settings.use_keyring = True
    settings.remote_api_key = "plain"
    settings.save()
    assert stored(settings.path)["remote_profiles"][0]["api_key"] == "plain"
    assert AppSettings.load(settings.path, environ={}).remote_api_key == "plain"


def test_real_module_degrades_without_raising():
    """Whatever is installed on this machine, the helpers never raise."""
    assert isinstance(secrets.keyring_available(), bool)
    assert secrets.load_key("teai-test-profile-that-does-not-exist") is None
    assert secrets.delete_key("teai-test-profile-that-does-not-exist") is False
    assert secrets.store_key("teai-test-profile", "") is False
