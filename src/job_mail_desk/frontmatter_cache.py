"""Fast, cached parsing of the Markdown + YAML fact files.

Every mutation used to re-read and re-parse the whole fact layer several
times (tasks, applications, unresolved) with PyYAML's pure-Python loader,
which on a few hundred files cost seconds per click. Two changes make the
fact layer cheap to read without changing a single byte on disk:

* ``fast_safe_load`` uses libyaml's ``CSafeLoader`` when the wheel ships it
  (Windows and manylinux wheels do) and falls back to ``SafeLoader``;
* ``load_document`` caches the parsed frontmatter per file, keyed by the
  file's ``(mtime_ns, size)``. Atomic writes create a new file every time, so
  any change by another process invalidates the entry; this process also calls
  ``invalidate`` after each write. Callers get a deep copy of the payload, so
  mutating the returned dict never leaks.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Callable

import yaml

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_CACHE: dict[str, tuple[tuple[int, int], str, object]] = {}
_LOCK = threading.Lock()
MAX_ENTRIES = 4096


def fast_safe_load(text: str) -> object:
    """``yaml.safe_load`` semantics with the libyaml parser when available."""
    return yaml.load(text, Loader=_LOADER)  # noqa: S506 - SafeLoader family only


def load_document(
    path: Path,
    extract_frontmatter: Callable[[str], str | None],
) -> tuple[str, object | None]:
    """Return ``(content, payload)`` for a fact file, reusing a cached parse.

    ``extract_frontmatter`` receives the full text and returns the YAML block
    (or ``None`` when the file has no usable frontmatter, in which case the
    payload is ``None``). Exceptions it raises propagate uncached.
    """
    stat = path.stat()
    key = str(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        entry = _CACHE.get(key)
    if entry is not None and entry[0] == signature:
        return entry[1], copy.deepcopy(entry[2])
    content = path.read_text(encoding="utf-8")
    block = extract_frontmatter(content)
    payload = (fast_safe_load(block) or {}) if block is not None else None
    with _LOCK:
        if len(_CACHE) >= MAX_ENTRIES:
            _CACHE.clear()
        _CACHE[key] = (signature, content, copy.deepcopy(payload))
    return content, payload


def invalidate(path: Path) -> None:
    """Drop the entry for a file this process just rewrote.

    The ``(mtime_ns, size)`` key already catches every change made by another
    process; this covers the one hole where a same-size rewrite lands inside
    the file system's timestamp granularity.
    """
    with _LOCK:
        _CACHE.pop(str(path), None)


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
