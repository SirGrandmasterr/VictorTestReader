"""Tests for packaging scripts and workflow definitions."""

import importlib.util
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = ROOT / "packaging" / "build.py"

spec = importlib.util.spec_from_file_location("build_script", str(BUILD_PY))
build_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_mod)

format_size = build_mod.format_size
tar_directory = build_mod.tar_directory
zip_directory = build_mod.zip_directory
package_distribution = build_mod.package_distribution


def test_format_size():
    assert format_size(500) == "500 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size(1024 * 1024 * 1024) == "1.0 GB"


def test_zip_directory():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src_dir = tmp_path / "app_folder"
        src_dir.mkdir()
        (src_dir / "app.exe").write_text("dummy binary", encoding="utf-8")
        sub = src_dir / "_internal"
        sub.mkdir()
        (sub / "lib.dll").write_text("dummy dll", encoding="utf-8")

        target_zip = tmp_path / "output.zip"
        zip_directory(src_dir, target_zip)

        assert target_zip.exists()
        with zipfile.ZipFile(target_zip, "r") as zf:
            namelist = zf.namelist()
            assert any("app.exe" in name for name in namelist)
            assert any("lib.dll" in name for name in namelist)


def test_tar_directory():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src_dir = tmp_path / "app_folder"
        src_dir.mkdir()
        (src_dir / "app").write_text("dummy elf", encoding="utf-8")

        target_tar = tmp_path / "output.tar.gz"
        tar_directory(src_dir, target_tar)

        assert target_tar.exists()
        with tarfile.open(target_tar, "r:gz") as tf:
            names = tf.getnames()
            assert any("app" in name for name in names)


def test_package_distribution_windows():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dist_dir = tmp_path / "dist"
        app_dir = dist_dir / "TextEnhanceAI"
        app_dir.mkdir(parents=True)
        (app_dir / "TextEnhanceAI.exe").write_text("bin", encoding="utf-8")
        out_dir = tmp_path / "out"

        with patch.object(sys, "platform", "win32"):
            target = package_distribution(dist_dir, out_dir, "0.14")
            assert target.name == "TextEnhanceAI-v0.14-windows.zip"
            assert target.exists()


def test_package_distribution_linux():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dist_dir = tmp_path / "dist"
        app_dir = dist_dir / "TextEnhanceAI"
        app_dir.mkdir(parents=True)
        (app_dir / "TextEnhanceAI").write_text("elf", encoding="utf-8")
        out_dir = tmp_path / "out"

        with patch.object(sys, "platform", "linux"):
            target = package_distribution(dist_dir, out_dir, "0.14")
            assert target.name == "TextEnhanceAI-v0.14-linux.tar.gz"
            assert target.exists()


def test_package_distribution_macos():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dist_dir = tmp_path / "dist"
        app_dir = dist_dir / "TextEnhanceAI.app"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<plist/>", encoding="utf-8")
        out_dir = tmp_path / "out"

        with patch.object(sys, "platform", "darwin"), patch("shutil.which", return_value=None):
            # When hdiutil is not available (e.g. non-macOS test runner), falls back to zip
            target = package_distribution(dist_dir, out_dir, "0.14")
            assert target.name == "TextEnhanceAI-v0.14-macos.zip"
            assert target.exists()


def test_workflow_has_three_platforms_and_deploy():
    workflow_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml"
    assert workflow_path.exists()
    content = workflow_path.read_text(encoding="utf-8")

    # Verify 3 platforms in matrix
    assert "windows-latest" in content
    assert "macos-latest" in content
    assert "ubuntu-latest" in content
    assert "TextEnhanceAI-${{ github.ref_name }}-linux.tar.gz" in content
    assert "TextEnhanceAI-${{ github.ref_name }}-windows.zip" in content
    assert "TextEnhanceAI-${{ github.ref_name }}-macos.dmg" in content

    # Verify deploy job
    assert "deploy-server:" in content
    assert "DEPLOY_SSH_KEY" in content
    assert "DEPLOY_HOST_KEY" in content
    assert "85.215.233.90" in content
    assert "fokus.voglerprojekte.com" in content
