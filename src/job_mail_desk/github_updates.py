"""Opt-in public GitHub updates. No mailbox data or credentials are sent."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from . import __version__

REPOSITORY = "WizLiang/jobmaildesk"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MAX_DOWNLOAD = 300 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
REQUIRED = {"JobMailDesk.exe", "JobMailDesk-cli.exe", "JobMailDesk.ico", "_internal"}
VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?\Z")


class UpdateError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, ...]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise UpdateError("发布版本格式无效。")
    major, minor, patch, pre, number = match.groups()
    return (int(major), int(minor), int(patch), {"a": 0, "b": 1, "rc": 2, None: 3}[pre], int(number or 0))


def allowed_url(url: str, *, redirected: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return False
        if parsed.hostname == "api.github.com":
            return parsed.path == f"/repos/{REPOSITORY}/releases"
        if parsed.hostname == "github.com":
            return parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
        return redirected and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    except ValueError:
        return False


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl, redirected=True):
            raise UpdateError("更新下载被重定向到未受信任的地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_url(url: str, limit: int, *, destination: Path | None = None, progress=None) -> bytes:
    if not allowed_url(url):
        raise UpdateError("更新地址不属于此项目。")
    request = Request(url, headers={"User-Agent": "JobMailDesk-Updater", "Accept": "application/vnd.github+json" if url == API_URL else "application/octet-stream"})
    started = time.monotonic()
    chunks = []
    total = 0
    stream = None
    try:
        with build_opener(_SafeRedirect()).open(request, timeout=20) as response:
            if not allowed_url(response.geturl(), redirected=True):
                raise UpdateError("更新响应地址无效。")
            expected = int(response.headers.get("Content-Length", "0"))
            if expected > limit:
                raise UpdateError("更新文件超过允许大小。")
            if destination:
                stream = destination.open("xb")
            while block := response.read(128 * 1024):
                total += len(block)
                if total > limit or time.monotonic() - started > 600:
                    raise UpdateError("下载过大或超时，请重试。")
                if stream:
                    stream.write(block)
                else:
                    chunks.append(block)
                if progress:
                    progress(total, expected)
            if expected and total != expected:
                raise UpdateError("下载不完整，请重试。")
    finally:
        if stream:
            stream.close()
    return b"".join(chunks)


@dataclass(frozen=True)
class Release:
    version: str
    page: str
    archive_name: str
    archive_url: str
    checksum_url: str
    notes: str
    preview: bool


def select_release(payload: object, current: str, channel: str) -> Release | None:
    if not isinstance(payload, list) or channel not in {"stable", "preview"}:
        raise UpdateError("更新信息格式无效。")
    candidates = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        tag = str(entry.get("tag_name", ""))
        try:
            key = version_key(tag)
        except UpdateError:
            continue
        preview = bool(entry.get("prerelease")) or key[3] < 3
        if (preview and channel == "stable") or key <= version_key(current):
            continue
        version = tag.removeprefix("v")
        name = f"JobMailDesk-Core-v{version}-win-x64.zip"
        assets = entry.get("assets", [])
        if not isinstance(assets, list):
            continue
        matches = [a for a in assets if isinstance(a, dict) and a.get("name") in {name, name + ".sha256"} and a.get("state", "uploaded") == "uploaded"]
        by_name = {a["name"]: a for a in matches}
        if len(matches) != 2 or set(by_name) != {name, name + ".sha256"}:
            continue
        base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
        if any(by_name[n].get("browser_download_url") != base + n for n in by_name):
            continue
        candidates.append((key, Release(version, f"{RELEASES_URL}/tag/{tag}", name, base + name, base + name + ".sha256", str(entry.get("body") or "")[:6000], preview)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def verify_checksum(archive: Path, checksum: bytes, filename: str) -> None:
    try:
        lines = checksum.decode("ascii").strip().splitlines()
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", lines[0]) if len(lines) == 1 else None
    except UnicodeError:
        match = None
    if not match or match[2] != filename:
        raise UpdateError("SHA-256 校验文件格式或文件名不匹配。")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != match[1].lower():
        raise UpdateError("更新包校验失败，未安装。")


def unpack_bundle(archive: Path, destination: Path) -> None:
    """Extract only JobMailDesk/; reject Windows traversal, aliases and links."""
    with zipfile.ZipFile(archive) as bundle:
        seen: set[str] = set()
        members = []
        total = 0
        if len(bundle.infolist()) > 20000:
            raise UpdateError("更新包文件过多。")
        for entry in bundle.infolist():
            raw_name = entry.orig_filename
            path = PurePosixPath(raw_name)
            parts = raw_name.rstrip("/").split("/")
            if ("\\" in raw_name or "\x00" in raw_name or path.is_absolute() or not parts
                    or any(p in {"", ".", ".."} or ":" in p or p.endswith((" ", "."))
                           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise UpdateError("更新包含有不安全的文件路径。")
            canonical = "/".join(parts).casefold()
            if canonical in seen:
                raise UpdateError("更新包包含重复文件。")
            seen.add(canonical)
            total += entry.file_size
            if total > MAX_UNPACKED:
                raise UpdateError("解压大小超过限制。")
            if parts[0] == "JobMailDesk" and len(parts) > 1:
                members.append((entry, parts[1:]))
        destination.mkdir()
        for entry, parts in members:
            target = destination.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    if not all((destination / name).exists() for name in REQUIRED) or not (destination / "_internal").is_dir():
        raise UpdateError("更新包缺少必要的 Windows 程序文件。")
    for name in ("JobMailDesk.exe", "JobMailDesk-cli.exe"):
        with (destination / name).open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise UpdateError("更新包内的程序格式无效。")


def validate_installation(install: Path, data_root: Path) -> None:
    if (not install.is_absolute() or install.resolve() != install or install.parent == install
            or install == Path.home().resolve() or data_root.resolve().is_relative_to(install)
            or install.is_relative_to(data_root.resolve())):
        raise UpdateError("程序目录与数据目录重叠或不是独立目录，请手动更新。")
    if not all((install / name).exists() for name in REQUIRED):
        raise UpdateError("当前不是完整的 Windows 程序目录，请手动更新。")
    for child in install.iterdir():
        if child.is_symlink() or child.is_junction() or (child.is_dir() and child.name != "_internal"):
            raise UpdateError("程序目录含有额外文件夹或链接，请手动更新以保护这些文件。")


def prepare_install(archive: Path, install: Path, data_root: Path) -> tuple[Path, Path]:
    validate_installation(install, data_root)
    token = uuid.uuid4().hex
    stage = install.parent / f".jobmaildesk-update-{token}"
    backup = install.parent / f".jobmaildesk-backup-{token}"
    try:
        unpack_bundle(archive, stage)
        # Keep user-added regular files, old license/README, and installer metadata.
        for child in install.iterdir():
            if child.is_file() and not (stage / child.name).exists():
                shutil.copy2(child, stage / child.name)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return stage, backup


def _release_cache(release: Release | None) -> list[dict]:
    """Persist only the public fields already validated by select_release."""
    if release is None:
        return []
    return [{"tag_name": f"v{release.version}", "prerelease": release.preview,
             "body": release.notes,
             "assets": [{"name": release.archive_name, "browser_download_url": release.archive_url},
                        {"name": release.archive_name + ".sha256", "browser_download_url": release.checksum_url}]}]


def _clean_downloads(folder: Path, *, keep: Path | None = None) -> None:
    """Remove only our obsolete ZIPs; never follow links or touch backups."""
    try:
        if folder.is_symlink() or folder.is_junction() or folder.resolve() != folder.absolute():
            return
        for path in folder.iterdir():
            if (path != keep and re.fullmatch(r"[a-f0-9]{32}\.zip", path.name)
                    and not path.is_symlink() and not path.is_junction() and path.is_file()):
                try:
                    path.unlink()
                except OSError:
                    pass  # A locked cache must not prevent another update attempt.
    except OSError:
        pass


class UpdateService:
    def __init__(self, root: Path, current: str = __version__):
        self.root = root
        self.current = current
        self._lock = threading.RLock()
        self._busy = False
        self._release: Release | None = None
        self._archive: Path | None = None
        self._channel = "preview"
        self._state = {"status": "idle", "message": "尚未检查更新", "current_version": current, "latest_version": "", "notes": "", "downloaded": 0, "total": 0}
        try:
            result = json.loads((root / "updates/install-result.json").read_text(encoding="utf-8-sig"))
            if isinstance(result, dict) and result.get("status") == "failed":
                self._state["message"] = "上次替换失败，已尝试恢复旧程序。可重试或打开发布页手动更新。"
            elif isinstance(result, dict) and result.get("status") == "installed":
                self._state["message"] = f"当前已安装 {current}；旧程序备份保留在程序目录旁。"
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self):
        with self._lock:
            return dict(self._state, busy=self._busy)

    def _set(self, **fields):
        with self._lock:
            self._state.update(fields)

    def _start(self, operation):
        with self._lock:
            if self._busy:
                return self.snapshot()
            self._busy = True
        def run():
            try:
                operation()
            except Exception as exc:
                self._set(status="error", message=str(exc) if isinstance(exc, UpdateError) else "无法完成更新操作，请检查网络、磁盘空间和目录权限后重试。")
            finally:
                with self._lock:
                    self._busy = False
        threading.Thread(target=run, name="jobmaildesk-updates", daemon=True).start()
        return self.snapshot()

    def check(self, channel: str = "preview", *, automatic=False):
        if channel not in {"preview", "stable"}:
            raise UpdateError("更新通道无效。")
        marker = self.root / "updates" / "last-check.json"
        if automatic:
            with self._lock:
                if self._busy or self._state["status"] == "ready":
                    return self.snapshot()
            try:
                cached = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("channel") == channel and 0 <= time.time() - float(cached["at"]) < 86400:
                    release = select_release(cached["releases"], self.current, channel)
                    with self._lock:
                        if not self._busy and self._state["status"] != "ready":
                            self._release = release
                            self._channel = channel
                            self._set(status="available" if release else "current", message=f"发现新版 {release.version}" if release else "当前通道暂无可用新版", latest_version=release.version if release else "", notes=release.notes if release else "")
                        return self.snapshot()
            except (OSError, ValueError, KeyError, TypeError, UpdateError):
                pass
        def check():
            self._release = None
            self._archive = None
            self._channel = channel
            self._set(status="checking", message="正在检查 GitHub 发布…", latest_version="", notes="")
            payload = json.loads(read_url(API_URL, 2 * 1024 * 1024))
            self._release = select_release(payload, self.current, channel)
            release = self._release
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                temporary = marker.with_suffix(".tmp")
                temporary.write_text(json.dumps({"at": time.time(), "channel": channel, "releases": _release_cache(release)}), encoding="utf-8")
                temporary.replace(marker)
            except OSError:
                pass  # Caching is optional; a successful check remains usable.
            self._set(status="available" if release else "current", message=f"发现新版 {release.version}" if release else "当前通道暂无可用新版", latest_version=release.version if release else "", notes=release.notes if release else "")
        return self._start(check)

    def download(self):
        release = self._release
        if not release:
            raise UpdateError("请先检查可用新版。")
        def download():
            previous = self._archive
            self._set(status="downloading", message="正在下载更新包…", downloaded=0, total=0)
            folder = self.root / "updates"
            folder.mkdir(parents=True, exist_ok=True)
            _clean_downloads(folder, keep=previous)
            target = folder / f"{uuid.uuid4().hex}.zip"
            try:
                checksum = read_url(release.checksum_url, 4096)
                read_url(release.archive_url, MAX_DOWNLOAD, destination=target, progress=lambda count, total: self._set(downloaded=count, total=total))
                verify_checksum(target, checksum, release.archive_name)
            except Exception:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                if previous and previous.is_file() and not previous.is_symlink() and not previous.is_junction():
                    self._set(status="ready", message="重新下载失败，已保留之前校验通过的更新包，可重启更新。")
                    return
                self._archive = None
                raise
            self._archive = target
            _clean_downloads(folder, keep=target)
            self._set(status="ready", message="下载与 SHA-256 校验完成，可重启更新")
        return self._start(download)

    def install(self, confirmed: bool, quit_callback):
        if confirmed is not True or sys.platform != "win32" or not getattr(sys, "frozen", False):
            raise UpdateError("仅支持 Windows 打包版，请确认后重启更新。")
        if not quit_callback:
            raise UpdateError("当前无法安全退出，请手动更新。")
        with self._lock:
            if self._busy or self._state["status"] != "ready" or not self._archive:
                raise UpdateError("请先下载并校验更新。")
            self._busy = True
        stage = None
        try:
            install = Path(sys.executable).absolute().parent
            release = self._release
            if not release:
                raise UpdateError("更新信息已过期，请重新检查。")
            # Recheck before executing: a cached ZIP may have changed since download.
            verify_checksum(self._archive, read_url(release.checksum_url, 4096), release.archive_name)
            stage, backup = prepare_install(self._archive, install, self.root)
            folder = self.root / "updates"
            probe_env = dict(os.environ, JOBMAILDESK_LOCAL_ROOT=str(folder / "probe"))
            probe = subprocess.run([str(stage / "JobMailDesk-cli.exe"), "--help"], env=probe_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            if probe.returncode != 0:
                raise UpdateError("新版启动自检失败，旧程序未替换。")
            token = uuid.uuid4().hex
            helper = folder / f"apply-{token}.ps1"
            shutil.copyfile(Path(__file__).with_name("apply_update.ps1"), helper)
            job = folder / f"apply-{token}.json"
            job.write_text(json.dumps({"install": str(install), "stage": str(stage), "backup": str(backup), "parent_pid": os.getpid(), "result": str(folder / "install-result.json")}), encoding="utf-8")
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper), "-JobFile", str(job)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self._set(status="installing", message="正在退出并更新，旧程序备份会保留在程序目录旁。")
            threading.Timer(0.5, quit_callback).start()
            return self.snapshot()
        except Exception:
            if stage and stage.exists():
                shutil.rmtree(stage)
            with self._lock:
                self._busy = False
            raise
