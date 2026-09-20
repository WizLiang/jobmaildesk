"""Generate the Windows VERSIONINFO resource for PyInstaller from pyproject.toml.

Windows wants four unsigned 16-bit components. The fourth component reserves
ordered ranges for alpha (0-16383), beta (16384-32767), RC (32768-49151), and
final (65535), so moving from RC to final never lowers the resource version.
Each prerelease counter is limited to 0-16383; unsupported formats and overflow
are rejected instead of silently clamped. Human-readable strings stay intact.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_NUMBER = r"(0|[1-9][0-9]*)"
_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:(a|b|rc){_NUMBER})?")


def parse_version(version: str) -> tuple[int, int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported version string for a Windows resource: {version!r}")
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    if max(major, minor, patch) > 65535:
        raise ValueError("version components must be between 0 and 65535")
    stage, counter = match.group(4), int(match.group(5) or 0)
    if counter > 16383:
        raise ValueError("prerelease counter must be between 0 and 16383")
    fourth = {"a": 0, "b": 16384, "rc": 32768}.get(stage, 65535) + counter
    return major, minor, patch, fourth


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
    flags={"0x2" if numbers[3] != 65535 else "0x0"},
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
