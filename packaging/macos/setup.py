from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import tomllib
import zlib

from py2app.build_app import py2app
from setuptools import find_packages, setup


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path("src")
APP = ["launcher.py"]
# uv's standalone CPython links zlib statically. py2app 0.28 still
# unconditionally copies zlib.__file__; point that obsolete copy step at this
# harmless build script while the bundled interpreter supplies the real module.
if not getattr(zlib, "__file__", None):
    zlib.__file__ = str(Path(__file__).resolve())
with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
    VERSION = str(tomllib.load(stream)["project"]["version"])
SHORT_VERSION = VERSION.split("rc", 1)[0]
BUNDLE_VERSION = VERSION.replace("rc", ".")


def _source_revision() -> str:
    digest = hashlib.sha256()
    candidates = [
        PROJECT_ROOT / "launcher.py",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "uv.lock",
        *sorted((PROJECT_ROOT / "src" / "job_mail_desk").rglob("*")),
        *sorted((PROJECT_ROOT / "packaging" / "macos").glob("*")),
    ]
    for path in candidates:
        if (
            not path.is_file()
            or path.suffix in {".pyc", ".pyo"}
            or "__pycache__" in path.parts
        ):
            continue
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


SOURCE_REVISION = (
    os.environ.get("JOBMAILDESK_SOURCE_REVISION") or _source_revision()
)
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "packaging/macos/JobMailDesk.icns",
    "packages": [
        "job_mail_desk",
        "keyring.backends.macOS",
        "webview",
        "cryptography",
        "cffi",
    ],
    "includes": ["AppKit", "PyObjCTools.AppHelper", "_cffi_backend"],
    # The repository also contains an Android source tree. Without this
    # exclusion modulegraph treats it as a Python namespace package and copies
    # the entire local Android SDK into the macOS bundle.
    "excludes": ["PyInstaller", "android", "pytest", "setuptools"],
    "plist": {
        "CFBundleName": "JobMailDesk",
        "CFBundleDisplayName": "JobMailDesk",
        "CFBundleIdentifier": "io.github.chpeeeeea.jobmaildesk",
        "CFBundleShortVersionString": SHORT_VERSION,
        "CFBundleVersion": BUNDLE_VERSION,
        "JobMailDeskSourceRevision": SOURCE_REVISION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": (
            "JobMailDesk 需要访问日历，以同步您确认的求职安排。"
        ),
    },
}


class ProjectPy2App(py2app):
    """Ignore metadata mirrored from pyproject; dependencies are preinstalled."""

    def finalize_options(self) -> None:
        self.distribution.install_requires = []
        super().finalize_options()

    def run(self) -> None:
        super().run()
        for app_path in Path(self.dist_dir).glob("*.app"):
            info_path = app_path / "Contents" / "Info.plist"
            with info_path.open("rb") as stream:
                payload = plistlib.load(stream)
            python_info = payload.get("PythonInfoDict")
            if isinstance(python_info, dict):
                python_info.pop("PythonExecutable", None)
            with info_path.open("wb") as stream:
                plistlib.dump(payload, stream, sort_keys=False)
            resources = app_path / "Contents" / "Resources"
            (resources / "setup.py").unlink(missing_ok=True)
            cache_directories = sorted(
                resources.rglob("__pycache__"),
                key=lambda item: len(item.parts),
                reverse=True,
            )
            for cache_dir in cache_directories:
                shutil.rmtree(cache_dir, ignore_errors=True)
            platforms = (
                resources
                / "lib"
                / "python3.12"
                / "webview"
                / "platforms"
            )
            shutil.rmtree(platforms / "android", ignore_errors=True)
            for platform_name in (
                "cef.py",
                "edgechromium.py",
                "gtk.py",
                "mshtml.py",
                "qt.py",
                "win32.py",
                "winforms.py",
            ):
                (platforms / platform_name).unlink(missing_ok=True)


setup(
    name="JobMailDesk",
    version=VERSION,
    description="Privacy-first, Markdown-native job email desk.",
    app=APP,
    package_dir={"": SOURCE_ROOT.as_posix()},
    packages=find_packages(SOURCE_ROOT.as_posix()),
    package_data={
        "job_mail_desk": [
            "ui/*.html",
            "ui/*.css",
            "ui/*.js",
            "identity_data/*.yml",
        ]
    },
    cmdclass={"py2app": ProjectPy2App},
    options={"py2app": OPTIONS},
)
