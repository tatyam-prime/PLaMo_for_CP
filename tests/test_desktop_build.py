from scripts.build_macos_app import minimum_version_from_otool


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
