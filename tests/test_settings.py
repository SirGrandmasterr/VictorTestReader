"""Tests for persisted settings and environment seeding."""

import json

from core.settings import (
    BACKEND_OLLAMA,
    BACKEND_REMOTE,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_PROFILE_NAME,
    RECENT_FILES_LIMIT,
    UI_SCALE_MAX,
    UI_SCALE_MIN,
    AppSettings,
    clamp_ui_scale,
    make_profile,
    remote_model_key,
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


def test_ui_scale_and_high_contrast_are_clamped_seeded_and_persisted(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.ui_scale == 1.0
    assert settings.high_contrast is False

    assert clamp_ui_scale(1.24) == 1.2  # one decimal
    assert clamp_ui_scale(0.1) == UI_SCALE_MIN
    assert clamp_ui_scale(9) == UI_SCALE_MAX
    assert clamp_ui_scale("abc") == 1.0
    assert clamp_ui_scale(None) == 1.0

    seeded = AppSettings.load(path, environ={"TEAI_UI_SCALE": "1.6", "TEAI_HIGH_CONTRAST": "yes"})
    assert seeded.ui_scale == 1.6
    assert seeded.high_contrast is True
    assert AppSettings.load(path, environ={"TEAI_UI_SCALE": "huge"}).ui_scale == 1.0

    settings.ui_scale = 3.5  # the setter is not clamped, the file round trip is
    settings.high_contrast = True
    assert settings.save() is None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["ui_scale"] == 3.5 and stored["high_contrast"] is True
    reloaded = AppSettings.load(path, environ={"TEAI_UI_SCALE": "1.2", "TEAI_HIGH_CONTRAST": "no"})
    assert reloaded.ui_scale == UI_SCALE_MAX  # file wins over the environment, then clamped
    assert reloaded.high_contrast is True

    path.write_text(json.dumps({"ui_scale": "not a number", "high_contrast": 0}), encoding="utf-8")
    damaged = AppSettings.load(path, environ={})
    assert damaged.ui_scale == 1.0
    assert damaged.high_contrast is False


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


def test_recent_files_keep_order_cap_and_prune_missing_files(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.recent_files == []
    files = []
    for number in range(RECENT_FILES_LIMIT + 2):
        file = tmp_path / "doc{0}.txt".format(number)
        file.write_text("x", encoding="utf-8")
        files.append(str(file))
        settings.remember_file(file)
    assert len(settings.recent_files) == RECENT_FILES_LIMIT
    assert settings.recent_files[0] == files[-1] and files[0] not in settings.recent_files

    settings.remember_file(files[3])  # re-opening moves a file to the front without duplicating it
    assert settings.recent_files[0] == files[3] and settings.recent_files.count(files[3]) == 1
    assert settings.save() is None
    assert json.loads(path.read_text(encoding="utf-8"))["recent_files"] == settings.recent_files

    (tmp_path / "doc3.txt").unlink()
    reloaded = AppSettings.load(path, environ={})
    assert files[3] not in reloaded.recent_files
    assert reloaded.recent_files == [entry for entry in settings.recent_files if entry != files[3]]
    reloaded.forget_file(files[-1])
    assert files[-1] not in reloaded.recent_files

    path.write_text(json.dumps({"recent_files": "not a list"}), encoding="utf-8")
    assert AppSettings.load(path, environ={}).recent_files == []


def test_quick_explanations_default_off_seeded_by_env_and_persisted(tmp_path):
    path = tmp_path / "settings.json"
    assert AppSettings.load(path, environ={}).quick_explanations is False
    assert AppSettings.load(path, environ={"TEAI_QUICK_EXPLAIN": "yes"}).quick_explanations is True
    settings = AppSettings.load(path, environ={})
    settings.quick_explanations = True
    settings.save()
    assert json.loads(path.read_text(encoding="utf-8"))["quick_explanations"] is True
    assert AppSettings.load(path, environ={"TEAI_QUICK_EXPLAIN": "no"}).quick_explanations is True  # file wins


def test_custom_instruction_history_is_capped_deduplicated_and_persisted(tmp_path):
    from core.settings import INSTRUCTION_HISTORY_LIMIT

    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    assert settings.instruction_history == [] and settings.translation_language == ""
    for number in range(INSTRUCTION_HISTORY_LIMIT + 2):
        settings.remember_instruction("Instruction {0}".format(number))
    settings.remember_instruction("  Instruction 3 ")  # moves to the front, whitespace stripped
    settings.remember_instruction("")  # ignored
    assert settings.instruction_history[0] == "Instruction 3"
    assert len(settings.instruction_history) == INSTRUCTION_HISTORY_LIMIT
    assert "Instruction 0" not in settings.instruction_history
    settings.translation_language = " Brazilian  Portuguese "
    settings.save()
    loaded = AppSettings.load(path, environ={})
    assert loaded.instruction_history == settings.instruction_history
    assert loaded.translation_language == "Brazilian Portuguese"

    path.write_text(json.dumps({"instruction_history": ["a", "", "b", "a", 7] + ["x{0}".format(n) for n in range(9)]}),
                    encoding="utf-8")
    assert AppSettings.load(path, environ={}).instruction_history == ["a", "b", "7", "x0", "x1"]


# ------------------------------------------------------------- profiles
def test_flat_remote_fields_migrate_to_a_default_profile(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "backend": "remote", "remote_url": "https://relay.example.com", "remote_api_key": "k1",
        "remote_max_tokens": 2048, "remote_enable_thinking": True, "models": {"remote": "qwen-27b"},
    }), encoding="utf-8")

    settings = AppSettings.load(path, environ={"TEAI_REMOTE_URL": "https://ignored.example.com"})

    assert settings.profile_names() == [DEFAULT_PROFILE_NAME]
    assert settings.active_profile == DEFAULT_PROFILE_NAME
    assert settings.remote_url == "https://relay.example.com"
    assert settings.remote_api_key == "k1"
    assert settings.remote_max_tokens == 2048
    assert settings.remote_enable_thinking is True
    assert settings.preferred_model(BACKEND_REMOTE) == "qwen-27b"  # the old single-relay memory still counts
    assert settings.remote_profiles[0] == make_profile("Default", "https://relay.example.com", "k1", 2048, True)


def test_environment_seeds_the_default_profile_only_without_profiles(tmp_path):
    path = tmp_path / "settings.json"
    environ = {"TEAI_REMOTE_URL": "https://env.example.com", "TEAI_REMOTE_API_KEY": "env-key"}
    fresh = AppSettings.load(path, environ=environ)
    assert fresh.remote_url == "https://env.example.com" and fresh.remote_api_key == "env-key"

    path.write_text(json.dumps({
        "remote_profiles": [{"name": "Office", "url": "https://office.example.com", "api_key": "o"}],
        "active_profile": "Office",
    }), encoding="utf-8")
    saved = AppSettings.load(path, environ=environ)
    assert saved.profile_names() == ["Office"]
    assert saved.remote_url == "https://office.example.com"
    assert saved.remote_api_key == "o"
    assert saved.remote_max_tokens == 4096 and saved.remote_enable_thinking is False  # defaults filled in


def test_profiles_round_trip_and_the_active_one_is_written_flat(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings.load(path, environ={})
    settings.remote_url = "https://one.example.com"
    settings.remote_api_key = "k-one"
    assert settings.add_profile("Two", url="https://two.example.com", api_key="k-two", max_tokens=8192) is not None
    assert settings.add_profile("two") is None  # names are unique, case-insensitively
    assert settings.add_profile("   ") is None
    assert settings.set_active_profile("Two") is True
    assert settings.set_active_profile("Nope") is False
    settings.remember_model(BACKEND_REMOTE, "big-model")
    settings.set_active_profile(DEFAULT_PROFILE_NAME)
    settings.remember_model(BACKEND_REMOTE, "small-model")
    assert settings.save() is None

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [p["name"] for p in stored["remote_profiles"]] == [DEFAULT_PROFILE_NAME, "Two"]
    assert stored["active_profile"] == DEFAULT_PROFILE_NAME
    assert stored["remote_url"] == "https://one.example.com"  # flat fields mirror the active profile
    assert stored["remote_api_key"] == "k-one"
    assert stored["models"] == {
        "ollama": DEFAULT_OLLAMA_MODEL, remote_model_key("Two"): "big-model",
        remote_model_key(DEFAULT_PROFILE_NAME): "small-model",
    }

    reloaded = AppSettings.load(path, environ={})
    assert reloaded.remote_profiles == settings.remote_profiles
    assert reloaded.preferred_model(BACKEND_REMOTE) == "small-model"
    reloaded.set_active_profile("Two")
    assert reloaded.remote_url == "https://two.example.com"
    assert reloaded.remote_max_tokens == 8192
    assert reloaded.preferred_model(BACKEND_REMOTE) == "big-model"
    assert reloaded.preferred_model(BACKEND_OLLAMA) == DEFAULT_OLLAMA_MODEL


def test_deleting_and_renaming_profiles(tmp_path):
    settings = AppSettings.load(tmp_path / "settings.json", environ={})
    assert settings.delete_profile(DEFAULT_PROFILE_NAME) is False  # the last profile stays
    settings.add_profile("Two", url="https://two.example.com")
    settings.add_profile("Three")
    settings.set_active_profile("Two")
    settings.remember_model(BACKEND_REMOTE, "m2")

    assert settings.rename_profile("Two", "Zwei") is True
    assert settings.active_profile == "Zwei"
    assert settings.preferred_model(BACKEND_REMOTE) == "m2"  # model memory follows the rename
    assert settings.rename_profile("Zwei", "three") is False  # taken
    assert settings.rename_profile("Zwei", "") is False
    assert settings.rename_profile("Zwei", "zwei") is True  # only the casing changes

    assert settings.delete_profile("zwei") is True  # deleting the active profile
    assert settings.active_profile == DEFAULT_PROFILE_NAME  # ...falls back to the first
    assert settings.profile_names() == [DEFAULT_PROFILE_NAME, "Three"]
    assert remote_model_key("zwei") not in settings.models
    assert settings.delete_profile("Missing") is False


def test_damaged_profile_entries_are_dropped(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "remote_profiles": [5, {"url": "no name"}, {"name": "A", "url": "https://a"}, {"name": "a", "url": "dup"}],
        "active_profile": "Missing",
    }), encoding="utf-8")
    settings = AppSettings.load(path, environ={})
    assert settings.profile_names() == ["A"]
    assert settings.active_profile == "A"

    path.write_text(json.dumps({"remote_profiles": "nonsense"}), encoding="utf-8")
    settings = AppSettings.load(path, environ={})
    assert settings.profile_names() == [DEFAULT_PROFILE_NAME]
    assert settings.remote_configured is False
