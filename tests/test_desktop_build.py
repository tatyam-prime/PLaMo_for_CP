import re
import tomllib
from pathlib import Path

from plamo_cp import __version__
from scripts.build_macos_app import bundle_info, minimum_version_from_otool


def test_release_metadata_uses_the_same_application_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == project["project"]["name"])
    installer = (root / "packaging/windows/installer.iss").read_text(encoding="utf-8-sig")
    windows_version = re.search(r"(?m)^AppVersion=(.+)$", installer).group(1).strip()
    assert project["project"]["version"] == package["version"] == __version__
    assert bundle_info("27.0")["CFBundleShortVersionString"] == windows_version == __version__


def test_bundle_requirement_uses_highest_embedded_binary_minimum():
    output = """Python:
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 27.0
      sdk 27.0
MLX:
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 26.2
      sdk 26.5
"""
    assert minimum_version_from_otool(output) == "27.0"


def test_legacy_macho_requirement_and_launcher_floor():
    assert (
        minimum_version_from_otool(
            "cmd LC_VERSION_MIN_MACOSX\n cmdsize 16\n version 15.0\n sdk 15.0"
        )
        == "15.0"
    )
    assert minimum_version_from_otool("minos 11.0") == "14.0"
