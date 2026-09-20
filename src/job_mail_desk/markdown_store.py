from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import frontmatter_cache
from .frontmatter_cache import load_document
from .fs_utils import fsync_directory
from .models import JobTask


FRONTMATTER = "---"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))
        fsync_directory(path.parent)
        frontmatter_cache.invalidate(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def render_task(task: JobTask) -> str:
    payload = task.to_dict()
    frontmatter = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    checkbox = "x" if task.status == "done" else " "
    time_bits = []
    if task.start_at:
        time_bits.append(f"开始：{task.start_at:%Y-%m-%d %H:%M}")
    if task.end_at:
        time_bits.append(f"结束：{task.end_at:%Y-%m-%d %H:%M}")
    if task.deadline_at:
        time_bits.append(f"截止：{task.deadline_at:%Y-%m-%d %H:%M}")
    if task.completed_at:
        inferred = "（由旧记录更新时间推定）" if task.completed_at_inferred else ""
        time_bits.append(
            f"完成：{task.completed_at:%Y-%m-%d %H:%M}{inferred}"
        )
    requirements = (
        "\n".join(f"- {item}" for item in task.requirements)
        if task.requirements
        else "- 暂无结构化要求，请人工核对原邮件。"
    )
    source_link = (
        f"[在本机打开通知链接]({task.source_url})"
        if task.source_url
        else "无可用链接"
    )
    return (
        f"{FRONTMATTER}\n{frontmatter}\n{FRONTMATTER}\n\n"
        f"# {task.company}｜{task.stage}\n\n"
        f"- [{checkbox}] {task.action_summary} <!-- jobmaildesk:{task.id} -->\n\n"
        "## 内容摘要\n\n"
        f"{task.title}\n\n"
        "## 下一步行动\n\n"
        f"{task.action_summary}\n\n"
        "## 时间与提醒\n\n"
        + f"- 岗位地点：{task.location or '待确认'}\n"
        + ("\n".join(f"- {item}" for item in time_bits) if time_bits else "- 时间待确认")
        + "\n\n## 邮件要求\n\n"
        + requirements
        + "\n\n## 本地通知链接\n\n"
        + source_link
        + "\n\n## 研究进度\n\n"
        + f"- 状态：{task.research_status}\n\n"
        + "## 手动补充\n\n"
        + (task.manual_notes or "_暂无_")
        + "\n\n"
        + "## 来源与事实边界\n\n"
        + "- 邮件原文未落盘；本页只保存结构化字段和脱敏摘要。\n"
        + "- 网络经验只能作为准备参考，不等同于本人真题或企业官方事实。\n"
    )


def _task_frontmatter(path: Path) -> "callable":
    def extract(content: str) -> str:
        if not content.startswith(f"{FRONTMATTER}\n"):
            raise ValueError(f"任务文件缺少 frontmatter：{path}")
        _, frontmatter, _ = content.split(FRONTMATTER, maxsplit=2)
        return frontmatter

    return extract


def parse_task(path: Path) -> JobTask:
    content, payload = load_document(path, _task_frontmatter(path))
    task = JobTask.from_dict(payload or {})
    marker = f"<!-- jobmaildesk:{task.id} -->"
    if marker in content:
        line = next((item for item in content.splitlines() if marker in item), "")
        if "- [x]" in line.lower():
            task.status = "done"
    return task


class MarkdownTaskStore:
    def __init__(self, tasks_dir: Path) -> None:
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.md"

    def save(self, task: JobTask) -> Path:
        task.updated_at = task.updated_at or datetime.now().astimezone()
        path = self.path_for(task.id)
        _atomic_write(path, render_task(task))
        return path

    def load(self, task_id: str) -> JobTask | None:
        path = self.path_for(task_id)
        return parse_task(path) if path.exists() else None

    def all(self) -> list[JobTask]:
        tasks = []
        for path in sorted(self.tasks_dir.glob("*.md")):
            try:
                tasks.append(parse_task(path))
            except (KeyError, TypeError, ValueError, yaml.YAMLError):
                continue
        return tasks

    def backfill_completed_times(self) -> int:
        """Backfill legacy completions without pretending they are exact times."""
        updates = 0
        for task in self.all():
            if task.status != "done" or task.completed_at:
                continue
            task.completed_at = task.updated_at or task.received_at
            task.completed_at_inferred = True
            self.save(task)
            updates += 1
        return updates

    def update_status(
        self,
        task_id: str,
        status: str,
        snoozed_until: datetime | None = None,
    ) -> JobTask:
        task = self.load(task_id)
        if task is None:
            raise KeyError(task_id)
        current = datetime.now().astimezone()
        task.status = status  # type: ignore[assignment]
        task.snoozed_until = snoozed_until
        if status == "done":
            task.completed_at = task.completed_at or current
            task.completed_at_inferred = False
        else:
            task.completed_at = None
            task.completed_at_inferred = False
        task.updated_at = current
        self.save(task)
        return task

    def trash(self, task_id: str, *, now: datetime | None = None) -> JobTask:
        task = self.load(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.tombstoned:
            raise ValueError("任务已永久删除。")
        if task.deleted_at:
            return task
        current = now or datetime.now().astimezone()
        task.deleted_status = task.status
        task.deleted_at = current
        task.status = "cancelled"
        task.snoozed_until = None
        task.updated_at = current
        self.save(task)
        return task

    def restore(self, task_id: str, *, now: datetime | None = None) -> JobTask:
        task = self.load(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.tombstoned:
            raise ValueError("永久删除的任务不能恢复。")
        if not task.deleted_at:
            return task
        current = now or datetime.now().astimezone()
        task.status = (task.deleted_status or (
            "planned"
            if task.start_at or task.end_at or task.deadline_at
            else "needs_review"
        ))  # type: ignore[assignment]
        task.deleted_at = None
        task.deleted_status = None
        task.updated_at = current
        self.save(task)
        return task

    def permanently_delete(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> JobTask:
        task = self.load(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.tombstoned:
            return task
        if not task.deleted_at:
            raise ValueError("请先将任务移入回收站。")
        current = now or datetime.now().astimezone()
        # Retain only stable replay identity and non-sensitive lifecycle facts.
        task.company = "已永久删除"
        task.role = None
        task.recruiting_project = None
        task.location = None
        task.stage = "已永久删除"
        task.round = None
        task.start_at = None
        task.end_at = None
        task.deadline_at = None
        task.status = "cancelled"
        task.title = ""
        task.action_summary = ""
        task.requirements = []
        task.manual_notes = ""
        task.source_sender = None
        task.source_url = None
        task.mail_locator = None
        task.snoozed_until = None
        task.completed_at = None
        task.completed_at_inferred = False
        task.deleted_status = None
        task.deleted_at = task.deleted_at or current
        task.tombstoned = True
        task.updated_at = current
        self.save(task)
        return task
