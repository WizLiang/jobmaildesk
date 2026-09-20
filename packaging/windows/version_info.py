"""Generate the Windows VERSIONINFO resource for PyInstaller from pyproject.toml.

Windows wants four numeric components. ``MAJOR.MINOR.PATCH`` map directly and
a pre-release suffix (``rc1``/``b2``/``a3``) becomes the fourth component, so
``0.7.0rc1`` -> ``0.7.0.1`` and a final ``0.7.0`` -> ``0.7.0.0``. The human
readable ``FileVersion``/``ProductVersion`` strings keep the original text.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(?:a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev\d+)?$")


def parse_version(version: str) -> tuple[int, int, int, int]:
    match = _VERSION.match(version.strip())
    if match is None:
        raise ValueError(f"unsupported version string for a Windows resource: {version!r}")
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    fourth = int(match.group(4) or match.group(5) or 0)
    return major, minor, patch, min(fourth, 65535)


def project_version(project_root: Path) -> str:
    payload = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def render_version_info(version: str) -> str:
    numbers = parse_version(version)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'JobMailDesk'),
        StringStruct('FileDescription', 'JobMailDesk - local-first job mail desk'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'JobMailDesk'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', 'JobMailDesk.exe'),
        StringStruct('ProductName', 'JobMailDesk'),
        StringStruct('ProductVersion', '{version}'),
      ]),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def write_version_info(project_root: Path, output: Path | None = None) -> str:
    version = project_version(project_root)
    target = output or project_root / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_version_info(version), encoding="utf-8")
    return str(target)


if __name__ == "__main__":
    print(write_version_info(Path(__file__).resolve().parents[2]))
