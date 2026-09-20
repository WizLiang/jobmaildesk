"""Check or update the three source version fields without publishing anything.

Run ``python scripts/version.py check`` or ``python scripts/version.py set
0.7.0rc3``. Success prints only the version, for use by build scripts. Accepted
versions are M.m.p with an optional a/b/rc counter, using the Windows resource
limits: each base component <= 65535 and prerelease counter <= 16383.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packaging" / "windows"))
from version_info import parse_version  # noqa: E402

VERSION_FILES = ("pyproject.toml", "src/job_mail_desk/__init__.py", "uv.lock")
_HEADERS = re.compile(r"(?m)^[ \t]*(\[\[?[^\r\n]*?\]\]?)[ \t]*(?:#[^\r\n]*)?\r?$")


def _field_span(text: str, field: str, expected: str, offset: int = 0) -> tuple[int, int]:
    pattern = re.compile(
        rf"(?m)^[ \t]*{re.escape(field)}[ \t]*=[ \t]*(['\"])([^'\"\r\n]+)\1[ \t]*(?:#[^\r\n]*)?\r?$"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1 or matches[0].group(2) != expected:
        raise ValueError(f"expected exactly one simple {field} string assignment")
    start, end = matches[0].span(2)
    return offset + start, offset + end


def _toml_version(text: str, filename: str) -> tuple[str, tuple[int, int]]:
    payload = tomllib.loads(text)  # Also rejects duplicate TOML fields/tables.
    headers = list(_HEADERS.finditer(text))
    if filename == "pyproject.toml":
        project = payload.get("project", {})
        if project.get("name") != "job-mail-desk":
            raise ValueError("pyproject.toml must describe job-mail-desk")
        value = project.get("version")
        selected = [index for index, header in enumerate(headers) if header.group(1) == "[project]"]
    else:
        packages = payload.get("package", [])
        if not isinstance(packages, list) or not all(isinstance(package, dict) for package in packages):
            raise ValueError("uv.lock must contain package tables")
        own = [index for index, package in enumerate(packages) if package.get("name") == "job-mail-desk"]
        if len(own) != 1 or packages[own[0]].get("source") != {"editable": "."}:
            raise ValueError("uv.lock must contain exactly one editable job-mail-desk package")
        value = packages[own[0]].get("version")
        package_headers = [index for index, header in enumerate(headers) if header.group(1) == "[[package]]"]
        if len(package_headers) != len(packages):
            raise ValueError("uv.lock package table layout is unsupported")
        selected = [package_headers[own[0]]]
    if not isinstance(value, str) or len(selected) != 1:
        raise ValueError(f"{filename} must contain one project version")
    index = selected[0]
    start = headers[index].end()
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    return value, _field_span(text[start:end], "version", value, start)


def _module_version(text: str) -> tuple[str, tuple[int, int]]:
    tree = ast.parse(text)
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                   and node.id == "__version__" and isinstance(node.ctx, ast.Store)]
    statements = [node for node in tree.body if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == "__version__"]
    if len(assignments) != 1 or len(statements) != 1 or not isinstance(statements[0].value, ast.Constant):
        raise ValueError("__init__.py must contain one literal __version__ assignment")
    value = statements[0].value.value
    if not isinstance(value, str):
        raise ValueError("__version__ must be a string")
    return value, _field_span(text, "__version__", value)


def _inspect(contents: dict[str, bytes]) -> tuple[str, dict[str, tuple[int, int]]]:
    versions, spans = {}, {}
    for filename, raw in contents.items():
        text = raw.decode("utf-8")
        try:
            value, span = _module_version(text) if filename.endswith(".py") else _toml_version(text, filename)
            parse_version(value)
        except (ValueError, SyntaxError, AttributeError) as exc:
            raise ValueError(f"{filename}: {exc}") from exc
        versions[filename], spans[filename] = value, span
    if len(set(versions.values())) != 1:
        raise ValueError("version mismatch: " + ", ".join(f"{name}={value}" for name, value in versions.items()))
    return next(iter(versions.values())), spans


def check(root: Path = PROJECT_ROOT) -> str:
    return _inspect({name: (root / name).read_bytes() for name in VERSION_FILES})[0]


def _stage(path: Path, content: bytes) -> Path:
    """Write alongside the original before making any replacement."""
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.version-", dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(path.stat().st_mode)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def set_version(version: str, root: Path = PROJECT_ROOT) -> str:
    target_key = parse_version(version)
    originals = {name: (root / name).read_bytes() for name in VERSION_FILES}
    current, spans = _inspect(originals)
    if target_key < parse_version(current):
        raise ValueError(f"version rollback is not allowed: {current} -> {version}")
    if current == version:
        return current
    updated = {}
    for name, raw in originals.items():
        text = raw.decode("utf-8")
        start, end = spans[name]
        updated[name] = (text[:start] + version + text[end:]).encode("utf-8")
    _inspect(updated)  # Validate all results before writing any original.
    staged, backups, replaced = {}, {}, []
    retained: set[Path] = set()
    try:
        for name in VERSION_FILES:
            staged[name] = _stage(root / name, updated[name])
            backups[name] = _stage(root / name, originals[name])
        if any((root / name).read_bytes() != raw for name, raw in originals.items()):
            raise ValueError("version files changed during preparation; retry after the other edit finishes")
        try:
            for name in VERSION_FILES:
                os.replace(staged[name], root / name)
                replaced.append(name)
        except OSError as failure:
            for name in reversed(replaced):
                try:
                    os.replace(backups[name], root / name)
                except OSError:
                    retained.add(backups[name])
            if retained:
                locations = ", ".join(str(path) for path in sorted(retained))
                raise OSError(f"replacement and rollback failed; original backups retained at: {locations}") from failure
            raise
    finally:
        for path in (*staged.values(), *backups.values()):
            if path not in retained:
                path.unlink(missing_ok=True)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="check all three source versions and print the version")
    update = commands.add_parser("set", help="update only the three version fields; never publish")
    update.add_argument("version")
    args = parser.parse_args(argv)
    try:
        print(check() if args.command == "check" else set_version(args.version))
    except (OSError, ValueError) as exc:
        print(f"version: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
