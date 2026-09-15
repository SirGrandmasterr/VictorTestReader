# Repository Guidelines

## Project Structure & Module Organization
- Root files: `TextEnhanceAI.py` (main app), `README.md`, `LICENSE`, screenshots `TextEnhanceAI-*.png`.
- Runtime artifacts: the settings file `TextEnhanceAI-settings.json` and the scratchpads `TextEnhanceAI-scratchpad_*.md` live in the per-user data directory (`core/paths.py`: `%APPDATA%\TextEnhanceAI`, `~/Library/Application Support/TextEnhanceAI`, `$XDG_CONFIG_HOME/TextEnhanceAI`). Set `TEAI_DATA_DIR=.` when running from a checkout to keep them next to the script as before; a settings file found next to the script is copied to the data directory once at start-up.
- `packaging/` holds the PyInstaller spec, the icon source and `make_icon.py`; `.github/workflows/build.yml` runs the tests on every push and builds Windows/macOS archives for `v*` tags (see `packaging/README.md`). The version is `core/__init__.py: __version__`.
- `core/` holds editing logic and model backends (`backend.py` contract, `ollama_service.py`, `remote_service.py`, `settings.py` with relay profiles, `secrets.py` for keys in the OS keyring via the optional `keyring` package), manuscript splitting (`chunking.py`), manuscript import/export for `.txt`/`.md`/`.docx`/`.odt` (`documents.py`, paragraph-level round trip; `python-docx` is optional), the automatic review model, runner and persistence (`workflow.py`) and the edit-kind heuristics (`change_kinds.py`); `ui/` holds Tkinter screens (`app.py`, `review_panel.py`, `connection_dialog.py`, `workflow_screen.py`) and the shared `theme.py`. Keep Tk out of `core/` so it stays testable.
- `locales/<code>.json` holds UI translations (flat English-source → translation maps).
- `scripts/` holds developer tools that are not part of the app (`record_responses.py` records model answers for the recorded-response tests, `extract_strings.py` lists untranslated UI strings).
- `remote/` holds the self-hosted relay (`remote/relay`, runs on a public server) and the GPU stack (`remote/gpu-agent`, vLLM + agent). Each is a self-contained Docker Compose deployment; the wire protocol is in `remote/PROTOCOL.md`.

## Build, Test, and Development Commands
- Run locally: `python TextEnhanceAI.py`
- Create venv (optional):
  - Windows: `python -m venv .venv && .venv\\Scripts\\activate`
  - Unix: `python -m venv .venv && source .venv/bin/activate`
- Install deps: `pip install -r requirements.txt` (Tkinter and difflib are stdlib).
- Ollama model: `ollama pull llama3.1:8b` (ensure Ollama is installed and running).

## Coding Style & Naming Conventions
- Python 3.8+; follow PEP 8 with 4‑space indents.
- Functions/variables: `snake_case`; classes: `PascalCase`; constants: `UPPER_CASE`.
- Docstrings: short summary + key args/returns where useful.
- UI labeling: keep button text concise; tooltips explain behavior.
- Prompts: extend the `PROMPTS` dict; avoid duplicating strings across the UI.
- UI strings: wrap user-visible text in `tr()` from `ui/i18n.py` with named placeholders (`tr("Connecting to {url} ...", url=url)`), never positional `{0}`. `ui/connection_dialog.py` is the worked example; the other screens are still unwrapped. `python scripts/extract_strings.py de` lists strings missing from `locales/de.json` (`--update` adds empty keys). The language comes from `AppSettings.ui_language` (`auto|en|de`, env `TEAI_LANG`) and is applied once at start-up, so changes need a restart.

## Testing Guidelines
- Run `pytest -q` from the repository root; it collects `tests/` (desktop), `remote/relay/tests`, `remote/gpu-agent/tests`, and `remote/tests` (end-to-end with a fake vLLM).
- Long-run tests (`tests/test_long_run.py`: a 3,000-segment project with injected failures, cancelled and resumed five times, both evaluation modes; `remote/tests/test_long_run_relay.py`: 200 streaming requests through relay and agent while the agent's socket is dropped twice) carry the `slow` marker and are deselected by `pytest.ini`. Run them with `pytest -m slow -q` (a few minutes) after touching `core/workflow.py: ProjectRunner`, the relay or the agent. Test file basenames must be unique across the test directories (they are not packages).
- Name tests `test_*.py` (pytest style). Backend tests use fake servers, never a live model. For UI changes, still provide manual steps (what you typed, which button you clicked, expected behavior).
- Recorded model answers (`tests/test_recorded.py`): the automatic review pipeline (`core/workflow.py: run_check`) is exercised against real model output replayed from *cassettes*, `tests/recorded/<sample>.<model-slug>.json`. `tests/recorded_service.py` provides `RecordedService` (replays a cassette, backend contract) and `RecordingService` (captures a live backend); each request is keyed by the SHA-256 of `(model, messages, max_tokens)`, so the tests exercise exactly the prompts the app sends.
  - Sample texts live in `tests/recorded/samples/*.txt` (German and English, 300–900 chars, planted typos, umlaut mistakes, missing commas, tense slips, awkward phrases and a proper noun that must stay). The `PLANTED` table in `tests/test_recorded.py` names each sample's typo and proper noun; keep both in sync when adding a sample.
  - Re-record with `python scripts/record_responses.py` (uses `TextEnhanceAI-settings.json`; `--backend`, `--model`, `--samples` override; `--mode combined|both` also records the single combined request into `<sample>.<model-slug>.combined.json`) whenever a prompt in `core/backend.py` or `core/workflow.py` changes (`SYSTEM_PROMPT`, `build_messages`, `CHECK_INSTRUCTIONS`, `build_explanation_messages`), a sample is added or edited, or you want cassettes for another model. A changed prompt makes replay fail with `KeyError: No recorded answer for the <check> request ... run scripts/record_responses.py to re-record` rather than passing silently. The script is never run in CI.
  - Cassettes are committed. They contain only the sample texts, the prompts built from them and the model's answers — never a user manuscript. Samples without a cassette are skipped, so `pytest -q` passes offline either way.

## Commit & Pull Request Guidelines
- Commits: imperative mood, present tense (e.g., "Fix grammar prompt", "Update README"). Keep focused and small.
- PRs: include a clear description, linked issues (if any), and before/after screenshots for UI changes.
- Checklists: note local run results and any edge cases tested.

## Security & Configuration Tips
- The app uses a local LLM via the `ollama` Python client, or a self-hosted relay (`core/remote_service.py`, standard library only). No third-party cloud calls are made; the relay and agent must never log prompt or output text.
- Do not commit runtime artifacts (e.g., `TextEnhanceAI-scratchpad_*.md`, `TextEnhanceAI-settings.json`) or deployment secrets (`remote/**/.env`).
- If you introduce config, prefer environment variables with safe defaults.
