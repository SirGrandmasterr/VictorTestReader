# Packaging TextEnhanceAI for Windows, macOS, and Linux

Authors who do not run `pip` get a folder (Windows/Linux) or an app bundle (macOS)
built with [PyInstaller](https://pyinstaller.org). `TextEnhanceAI.spec` in
this directory describes the build; `.github/workflows/build.yml` runs it on
`windows-latest`, `macos-latest`, and `ubuntu-latest` whenever changes merge into
`main` (uploaded as workflow artifacts) and whenever a tag `v*` is pushed
(attached to the GitHub release). The version shown in the window title, the
About dialog and the bundle comes from `core/__init__.py` (`__version__`).

## Build locally

### Option 1: One-step build script (recommended)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt pyinstaller pillow python-docx keyring
python packaging/build.py
```

This automatically generates icons (`icon.ico`, `icon.icns`, `icon.png`), runs
PyInstaller with `TextEnhanceAI.spec`, and creates the platform distribution archive:
- **Windows**: `dist/TextEnhanceAI-v<version>-windows.zip`
- **macOS**: `dist/TextEnhanceAI-v<version>-macos.dmg` (or `.zip`)
- **Linux**: `dist/TextEnhanceAI-v<version>-linux.tar.gz`

Use `python packaging/build.py --clean` to clean previous build caches, or
`--no-archive` to leave only the raw folder in `dist/`.

### Option 2: Manual steps

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt pyinstaller pillow python-docx keyring
python packaging/make_icon.py                      # icon.ico / icon.icns / icon.png from icon.svg
pyinstaller --noconfirm --clean packaging/TextEnhanceAI.spec
```

Result:

- Windows: `dist/TextEnhanceAI/TextEnhanceAI.exe` plus its `_internal/`
  folder. Zip the whole `dist/TextEnhanceAI` directory to distribute it.
- macOS: `dist/TextEnhanceAI.app`. Wrap it in a disk image with
  `hdiutil create -volname TextEnhanceAI -srcfolder dist/TextEnhanceAI.app -ov -format UDZO TextEnhanceAI.dmg`.
- Linux: `dist/TextEnhanceAI/TextEnhanceAI` plus libraries. Package with
  `tar -czf TextEnhanceAI-linux.tar.gz -C dist TextEnhanceAI`.

`python-docx` and `keyring` are optional at runtime; installing them before
the build bundles `.docx` support and the OS keyring backends.

## Release

```bash
# bump __version__ in core/__init__.py, commit, then:
git tag v0.14
git push origin v0.14
```

The workflow builds all three platforms, runs the test suite on Ubuntu, and
uploads `TextEnhanceAI-v0.14-windows.zip`, `TextEnhanceAI-v0.14-macos.dmg`, and
`TextEnhanceAI-v0.14-linux.tar.gz` to the release for that tag. On merges to `main`,
build artifacts are also uploaded to the GitHub Actions run for testing.

## Notes and limitations

- **Unsigned.** Builds are not code-signed or notarised. Windows shows the
  SmartScreen "unknown publisher" prompt on first start; macOS Gatekeeper
  refuses the app until the user right-clicks it and chooses *Open* (or runs
  `xattr -dr com.apple.quarantine TextEnhanceAI.app`). Signing needs a
  developer certificate and is deliberately out of scope.
- **Settings location.** Packaged builds keep settings and scratchpads in the
  per-user data directory (`%APPDATA%\TextEnhanceAI`,
  `~/Library/Application Support/TextEnhanceAI`, `~/.config/TextEnhanceAI`);
  see `core/paths.py`. A `TextEnhanceAI-settings.json` next to the executable
  is copied there once on first start.
- **Ollama** must be installed separately; the app talks to it over HTTP.
  The relay backend needs nothing else.
- The icon is generated from `icon.svg`: with `cairosvg` installed the SVG
  is rasterised, otherwise `make_icon.py` draws the same design with Pillow.
