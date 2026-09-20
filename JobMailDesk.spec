# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build (one directory, two executables).

``JobMailDesk.exe``     windowed tray application (the desktop UI)
``JobMailDesk-cli.exe`` console twin for ``doctor`` / ``smoke`` / ``configure`` /
                        ``import-snapshot`` and the agent commands, whose
                        output must be visible; a windowed EXE has no stdout.

Both share one ``_internal`` tree. The onedir layout starts faster than a
self-extracting single file, is friendlier to antivirus scanners, and lets
``JobMailDesk.ico`` sit next to the EXE where ``ui_app._tray_icon_path`` and
the shortcut scripts expect it. Build with ``scripts/build.ps1``.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

PROJECT = Path(SPECPATH)  # noqa: F821 - provided by PyInstaller
sys.path.insert(0, str(PROJECT / "packaging" / "windows"))
from version_info import write_version_info  # noqa: E402

ICON = str(PROJECT / "packaging" / "windows" / "JobMailDesk.ico")
VERSION_FILE = write_version_info(PROJECT)

hiddenimports = (
    collect_submodules("keyring.backends")
    + collect_submodules("webview.platforms")
    + collect_submodules("pystray")
    + collect_submodules("win32ctypes")
    + [
        "_cffi_backend",  # cryptography/Fernet; missing this crashed the macOS bundle
        "clr",
        "clr_loader",
        "winsound",
        "winreg",
        "msvcrt",
        "zoneinfo",
        "tzdata",
        "apscheduler.executors.pool",
        "apscheduler.jobstores.memory",
        "apscheduler.triggers.cron",
        "apscheduler.triggers.interval",
    ]
)

datas = (
    [
        ("src/job_mail_desk/ui", "job_mail_desk/ui"),
        ("src/job_mail_desk/identity_data", "job_mail_desk/identity_data"),
        # Shipped so ``JobMailDesk-cli.exe smoke`` can replay the identity
        # golden vectors inside the frozen bundle (redacted test data).
        ("tests/golden/mail_identity.json", "job_mail_desk/golden"),
        ("tests/golden/mail_non_candidates.json", "job_mail_desk/golden"),
        ("tests/golden/mail_filter_policy.json", "job_mail_desk/golden"),
    ]
    + collect_data_files("tzdata")      # Windows has no IANA database
    + collect_data_files("webview")     # WebView2 / WinForms assemblies (webview/lib)
    + collect_data_files("clr_loader")  # ClrLoader.dll
    + collect_data_files("pythonnet")   # Python.Runtime.dll
    + copy_metadata("apscheduler")
    + copy_metadata("keyring")
    + copy_metadata("pywebview")
)

EXCLUDES = ["android", "PyInstaller", "pytest", "_pytest", "setuptools", "py2app", "tkinter"]

# pywebview ships every platform's native helper; only the x64 Windows ones
# can ever load here (WebView2Loader lives in runtimes/win-x64).
_UNUSED_WEBVIEW_PAYLOAD = ("pywebview-android.jar", "WebBrowserInterop.x86.dll", "win-x86", "win-arm64")
datas = [
    entry
    for entry in datas
    if not (
        entry[1].replace("\\", "/").startswith("webview/lib")
        and any(marker in entry[0].replace("\\", "/") for marker in _UNUSED_WEBVIEW_PAYLOAD)
    )
]

# Make every ``open()`` default to UTF-8 (same as ``python -X utf8``); the
# facts are UTF-8 Markdown/YAML and Windows would otherwise default to GBK.
RUNTIME_OPTIONS = [("X utf8", None, "OPTION")]

a = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

common = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed EXEs trip antivirus heuristics and slow start-up
    icon=ICON,
    version=VERSION_FILE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

desktop = EXE(
    pyz,
    a.scripts,
    RUNTIME_OPTIONS,
    exclude_binaries=True,
    name="JobMailDesk",
    console=False,
    **common,
)
cli = EXE(
    pyz,
    a.scripts,
    RUNTIME_OPTIONS,
    exclude_binaries=True,
    name="JobMailDesk-cli",
    console=True,
    **common,
)
coll = COLLECT(
    desktop,
    cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JobMailDesk",
)
