"""Tests for persisted settings and environment seeding."""

import json

from core.settings import (
    BACKEND_OLLAMA,
    BACKEND_REMOTE,
    DEFAULT_OLLAMA_MODEL,
    AppSettings,
)


def test_defaults_when_no_file_and_no_environment(tmp_path):
    settings = AppSettings.load(tmp_path / "settings.json", environ={})

    assert settings.backend == BACKEND_OLLAMA
    assert settings.preferred_model() == DEFAULT_OLLAMA_MODEL
    assert settings.remote_url == ""
    assert settings.remote_configured is False
    assert settings.remote_max_tokens == 4096
    assert settings.remote_enable_thinking is False
    assert settings.load_error == ""


def test_environment_seeds_missing_values(tmp_path):
    environ = {
        "TEAI_BACKEND": "remote",
        "TEAI_REMOTE_URL": "https://relay.example.com",
        "TEAI_REMOTE_API_KEY": "abc",
        "TEAI_REMOTE_MAX_TOKENS": "8192",
        "TEAI_REMOTE_THINKING": "yes",
        "TEAI_MODEL": "qwen-27b",
    }

    settings = AppSettings.load(tmp_path / "settings.json", environ=environ)

    assert settings.backend == BACKEND_REMOTE
    assert settings.remote_url == "https://relay.example.com"
    assert settings.remote_api_key == "abc"
    assert settings.remote_max_tokens == 8192
    assert settings.remote_enable_thinking is True
    assert settings.preferred_model(BACKEND_REMOTE) == "qwen-27b"
    assert settings.preferred_model(BACKEND_OLLAMA) == DEFAULT_OLLAMA_MODEL


def test_default_style_guide_is_persisted(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.default_style_guide == ""

    settings.default_style_guide = "British spelling\nnever touch quotations"
    assert settings.save() is None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["default_style_guide"] == "British spelling\nnever touch quotations"
    assert AppSettings.load(path, environ={}).default_style_guide == "British spelling\nnever touch quotations"

    path.write_text(json.dumps({"default_style_guide": None}), encoding="utf-8")
    assert AppSettings.load(path, environ={}).default_style_guide == ""


def test_default_glossary_is_persisted_as_a_list(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.default_glossary == []

    settings.default_glossary = ["Thalbrück", "hyper*"]
    assert settings.save() is None
    assert json.loads(path.read_text(encoding="utf-8"))["default_glossary"] == ["Thalbrück", "hyper*"]
    assert AppSettings.load(path, environ={}).default_glossary == ["Thalbrück", "hyper*"]

    path.write_text(json.dumps({"default_glossary": "a\n b \n"}), encoding="utf-8")
    assert AppSettings.load(path, environ={}).default_glossary == ["a", "b"]
    path.write_text(json.dumps({"default_glossary": 5}), encoding="utf-8")
    assert AppSettings.load(path, environ={}).default_glossary == []


def test_default_evaluation_mode_is_validated_and_persisted(tmp_path):
    path = tmp_path / "settings.json"
    assert AppSettings.load(path, environ={}).default_evaluation_mode == "combined"
    assert AppSettings.load(path, environ={"TEAI_EVALUATION_MODE": "separate"}).default_evaluation_mode == "separate"
    assert AppSettings.load(path, environ={"TEAI_EVALUATION_MODE": "turbo"}).default_evaluation_mode == "combined"
    settings = AppSettings.load(path, environ={})
    settings.default_evaluation_mode = "separate"
    settings.save()
    assert AppSettings.load(path, environ={}).default_evaluation_mode == "separate"


def test_saved_file_wins_over_environment_and_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    settings.backend = BACKEND_REMOTE
    settings.remote_url = "https://relay.example.com"
    settings.remote_api_key = "file-key"
    settings.remember_model(BACKEND_REMOTE, "qwen-27b")
    assert settings.save() is None

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["remote_api_key"] == "file-key"
    assert "path" not in stored and "load_error" not in stored

    reloaded = AppSettings.load(path, environ={"TEAI_REMOTE_API_KEY": "env-key"})
    assert reloaded.remote_api_key == "file-key"
    assert reloaded.backend == BACKEND_REMOTE
    assert reloaded.preferred_model() == "qwen-27b"


def test_corrupt_file_falls_back_to_defaults_with_message(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    settings = AppSettings.load(path, environ={})

    assert settings.backend == BACKEND_OLLAMA
    assert "could not be read" in settings.load_error


def test_unknown_backend_and_models_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"backend": "cloud", "models": {"cloud": "x", "ollama": "phi"}}),
        encoding="utf-8",
    )

    settings = AppSettings.load(path, environ={})

    assert settings.backend == BACKEND_OLLAMA
    assert settings.models == {"ollama": "phi"}


def test_custom_modes_and_chains_round_trip_and_invalid_entries_are_dropped(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.custom_modes == [] and settings.chains == []
    settings.custom_modes = [{"name": "House style", "instruction": "Apply it."}]
    settings.chains = [{"name": "Tidy", "steps": ["Grammar", "House style"]}]
    assert settings.save() is None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["custom_modes"] == [{"name": "House style", "instruction": "Apply it."}]
    assert stored["chains"] == [{"name": "Tidy", "steps": ["Grammar", "House style"]}]

    reloaded = AppSettings.load(path, environ={})
    assert reloaded.custom_modes == settings.custom_modes and reloaded.chains == settings.chains
    assert reloaded.custom_mode_names() == ["House style"] and reloaded.chain_names() == ["Tidy"]
    assert reloaded.find_chain("Tidy") == ["Grammar", "House style"] and reloaded.find_chain("x") is None
    assert reloaded.load_error == ""

    path.write_text(json.dumps({
        "custom_modes": [{"name": "Grammar", "instruction": "x"}, {"name": "Ok", "instruction": "y"}, 5],
        "chains": [{"name": "Broken", "steps": ["Ok", "Translate"]}, {"name": "Fine", "steps": ["Ok", "Polish"]}],
    }), encoding="utf-8")
    damaged = AppSettings.load(path, environ={})
    assert damaged.custom_modes == [{"name": "Ok", "instruction": "y"}]
    assert damaged.chains == [{"name": "Fine", "steps": ["Ok", "Polish"]}]
    assert "Ignored 3 invalid custom mode/chain entries" in damaged.load_error
