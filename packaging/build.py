#!/usr/bin/env python3
"""Build and package TextEnhanceAI for the current platform.

Usage:
    python packaging/build.py [--clean] [--no-archive] [--output-dir DIR]

Platforms:
    - Windows: creates dist/TextEnhanceAI-v<version>-windows.zip containing TextEnhanceAI.exe and _internal/
    - macOS: creates dist/TextEnhanceAI-v<version>-macos.dmg (or .zip) containing TextEnhanceAI.app
    - Linux: creates dist/TextEnhanceAI-v<version>-linux.tar.gz containing TextEnhanceAI/ executable and assets

Requirements:
    pip install pyinstaller pillow python-docx keyring
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import __version__  # noqa: E402


def ensure_icons(packaging_dir: Path) -> None:
    """Generate icon.ico, icon.icns, and icon.png if missing or requested."""
    ico = packaging_dir / "icon.ico"
    icns = packaging_dir / "icon.icns"
    png = packaging_dir / "icon.png"
    if ico.exists() and icns.exists() and png.exists():
        return

    print("Generating application icons...")
    make_icon_script = packaging_dir / "make_icon.py"
    subprocess.run([sys.executable, str(make_icon_script), str(packaging_dir)], check=True)


def run_pyinstaller(spec_file: Path, clean: bool = False) -> None:
    """Invoke PyInstaller on TextEnhanceAI.spec."""
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd.append(str(spec_file))
    print(f"Running PyInstaller: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def zip_directory(src_dir: Path, target_zip: Path) -> None:
    """Zip a directory including its top-level folder name."""
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(src_dir.parent)
                zf.write(file_path, arcname)


def tar_directory(src_dir: Path, target_tar: Path) -> None:
    """Tar.gz a directory including its top-level folder name."""
    target_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target_tar, "w:gz") as tf:
        tf.add(str(src_dir), arcname=src_dir.name)


def package_macos(app_dir: Path, target_dmg: Path) -> Path:
    """Create a DMG on macOS with hdiutil, falling back to zip."""
    target_dmg.parent.mkdir(parents=True, exist_ok=True)
    hdiutil = shutil.which("hdiutil")
    if hdiutil:
        cmd = [
            hdiutil, "create",
            "-volname", "TextEnhanceAI",
            "-srcfolder", str(app_dir),
            "-ov",
            "-format", "UDZO",
            str(target_dmg),
        ]
        print(f"Creating DMG: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
        return target_dmg
    else:
        target_zip = target_dmg.with_suffix(".zip")
        print(f"hdiutil not found; creating zip: {target_zip}")
        zip_directory(app_dir, target_zip)
        return target_zip


def package_distribution(dist_dir: Path, output_dir: Path, version: str) -> Path:
    """Package the PyInstaller output according to the current platform."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        src = dist_dir / "TextEnhanceAI"
        if not src.exists():
            raise FileNotFoundError(f"Expected build directory at {src}")
        target = output_dir / f"TextEnhanceAI-v{version}-windows.zip"
        print(f"Packaging Windows distribution into {target.name}...")
        zip_directory(src, target)
        return target
    elif sys.platform == "darwin":
        src = dist_dir / "TextEnhanceAI.app"
        if not src.exists():
            raise FileNotFoundError(f"Expected app bundle at {src}")
        target = output_dir / f"TextEnhanceAI-v{version}-macos.dmg"
        print(f"Packaging macOS distribution into {target.name}...")
        return package_macos(src, target)
    else:
        src = dist_dir / "TextEnhanceAI"
        if not src.exists():
            raise FileNotFoundError(f"Expected build directory at {src}")
        target = output_dir / f"TextEnhanceAI-v{version}-linux.tar.gz"
        print(f"Packaging Linux distribution into {target.name}...")
        tar_directory(src, target)
        return target


def format_size(bytes_size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}" if unit != "B" else f"{bytes_size} B"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and package TextEnhanceAI client")
    parser.add_argument("--clean", action="store_true", help="Clean PyInstaller cache and build artifacts first")
    parser.add_argument("--no-archive", action="store_true", help="Skip creating the release archive (zip/dmg/tar.gz)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist", help="Directory for distribution archive")
    args = parser.parse_args()

    packaging_dir = ROOT / "packaging"
    spec_file = packaging_dir / "TextEnhanceAI.spec"
    dist_dir = ROOT / "dist"

    print(f"Building TextEnhanceAI v{__version__} on {sys.platform}...")
    ensure_icons(packaging_dir)
    run_pyinstaller(spec_file, clean=args.clean)

    if not args.no_archive:
        archive_path = package_distribution(dist_dir, args.output_dir, __version__)
        size_str = format_size(archive_path.stat().st_size)
        print(f"Successfully created: {archive_path} ({size_str})")
    else:
        print("Build complete (--no-archive specified).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
