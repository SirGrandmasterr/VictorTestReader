# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TextEnhanceAI (one directory, windowed).

    pip install pyinstaller pillow python-docx keyring
    python packaging/make_icon.py
    pyinstaller --noconfirm --clean packaging/TextEnhanceAI.spec

Produces dist/TextEnhanceAI/ (Windows, Linux) or dist/TextEnhanceAI.app
(macOS). The version comes from core/__init__.py; locales/ ships as data.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # SPECPATH: directory of this file (set by PyInstaller)
sys.path.insert(0, str(ROOT))

from core import __version__  # noqa: E402

PACKAGING = ROOT / "packaging"
if sys.platform == "win32":
    ICON = PACKAGING / "icon.ico"
elif sys.platform == "darwin":
    ICON = PACKAGING / "icon.icns"
else:
    ICON = PACKAGING / "icon.png"
icon = str(ICON) if ICON.exists() else None

block_cipher = None

a = Analysis(
    [str(ROOT / "TextEnhanceAI.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "locales"), "locales")],
    hiddenimports=[
        # optional runtime packages: bundled when installed in the build environment
        "docx",
        "keyring",
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "keyring.backends.chainer",
        "keyring.backends.fail",
        "keyring.backends.null",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "aiohttp"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TextEnhanceAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TextEnhanceAI",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TextEnhanceAI.app",
        icon=icon,
        bundle_identifier="com.textenhanceai.desktop",
        info_plist={
            "CFBundleName": "TextEnhanceAI",
            "CFBundleDisplayName": "TextEnhanceAI",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "TextEnhanceAI contributors",
        },
    )
