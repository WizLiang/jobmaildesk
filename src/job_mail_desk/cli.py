from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_bridge import apply_task_update, list_tasks, sync_outputs
from .config import (
    APPLICATIONS_DIR,
    DICTIONARIES_DIR,
    DASHBOARD_FILE,
    DIGESTS_DIR,
    LOCAL_ROOT,
    TASKS_DIR,
    UNRESOLVED_DIR,
    ensure_config,
    load_settings,
)
from .application_registry import ApplicationRegistry, preview_progress_applications
from .identity_dictionaries import (
    DictionaryValidationError,
    load_identity_dictionaries,
)
from .dictionary_compiler import compile_workbook
from .identity_preview import export_identity_preview
from .file_transaction import recover_file_transactions
from .credentials import configure_interactively
from .data_lock import data_directory_lease
from .digest import generate_digest
from .doctor import run_doctor
from .exporter import export_dashboard, import_checked_states
from .logging_setup import configure_logging
from .markdown_store import MarkdownTaskStore
from .research import pending_requests
from .progress import export_progress
from .scanner import scan_once
from .scheduler import run_forever
from .selfcheck import run_selfcheck
from .snapshot_import import SnapshotImportError, import_snapshot
from .ui_app import DesktopApi, run_ui, show_existing_window
from .unresolved_store import UnresolvedStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobmaildesk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("configure", help="配置 IMAP 凭据和本地配置")
    doctor = subparsers.add_parser("doctor", help="检查本地环境")
    doctor.add_argument("--offline", action="store_true")
    scan = subparsers.add_parser("scan", help="扫描邮件")
    scan.add_argument("--once", action="store_true")
    scan.add_argument("--days", type=int)
    scan.add_argument("--shadow", action="store_true")
    scan.add_argument(
        "--identity-preview",
        action="store_true",
        help="重放邮箱窗口并预览申请归属，不修改任务、状态或 unresolved",
    )
    scan.add_argument(
        "--preview-output",
        type=Path,
        help="将 identity preview 导出为本地脱敏 Markdown",
    )
    subparsers.add_parser("run", help="运行后台调度器")
    digest = subparsers.add_parser("digest", help="生成简报")
    digest.add_argument("period", choices=("morning", "noon", "evening"))
    export = subparsers.add_parser("export", help="导出 Markdown 总览")
    export.add_argument("--obsidian", action="store_true")
    subparsers.add_parser(
        "sync-ledger",
        help="读取岗位投递决策台账并刷新本地卡片，不扫描邮箱",
    )
    subparsers.add_parser("research-queue", help="查看待处理研究请求")
    subparsers.add_parser(
        "application-preview",
        help="只读预览人工台账可生成的申请身份，不写入本地数据",
    )
    application_import = subparsers.add_parser(
        "application-import",
        help="从人工进展台账导入并锁定申请身份",
    )
    application_import.add_argument("--from-progress", action="store_true")
    dictionary_check = subparsers.add_parser(
        "dictionary-check",
        help="校验内置身份词典和本地覆盖，不修改任务或申请数据",
    )
    dictionary_check.add_argument(
        "--user-dir",
        type=Path,
        default=DICTIONARIES_DIR,
        help="本地词典覆盖目录",
    )
    dictionary_compile = subparsers.add_parser(
        "dictionary-compile",
        help="将用户持有的秋招 XLSX 编译成本地覆盖词典",
    )
    dictionary_compile.add_argument("--xlsx", type=Path, required=True)
    dictionary_compile.add_argument("--output", type=Path, required=True)
    dictionary_compile.add_argument(
        "--sheet",
        default="2027秋招信息表",
        help="包含公司及岗位列的工作表名称",
    )
    subparsers.add_parser("unresolved-list", help="列出待人工归属的脱敏邮件")
    unresolved_resolve = subparsers.add_parser(
        "unresolved-resolve",
        help="将待归属邮件绑定到现有申请并生成任务",
    )
    unresolved_resolve.add_argument("source_hash")
    unresolved_resolve.add_argument("--application-key", required=True)
    unresolved_ignore = subparsers.add_parser(
        "unresolved-ignore",
        help="忽略一条待归属邮件",
    )
    unresolved_ignore.add_argument("source_hash")
    task_list = subparsers.add_parser(
        "task-list",
        help="为本地 Agent 列出可更新任务及稳定 ID",
    )
    task_list.add_argument("--company", default="")
    task_list.add_argument("--role", default="")
    task_list.add_argument("--stage", default="")
    task_list.add_argument("--include-irrelevant", action="store_true")
    task_update = subparsers.add_parser(
        "task-update",
        help="按稳定 ID 更新任务并同步 Markdown、进展和 Obsidian",
    )
    task_update.add_argument("task_id")
    task_update.add_argument(
        "--status",
        choices=(
            "needs_review",
            "confirmed",
            "planned",
            "done",
            "cancelled",
            "irrelevant",
        ),
    )
    task_update.add_argument("--start-at")
    task_update.add_argument("--end-at")
    task_update.add_argument("--deadline-at")
    task_update.add_argument("--company")
    task_update.add_argument("--role")
    task_update.add_argument("--stage")
    task_update.add_argument("--round")
    task_update.add_argument("--action-summary")
    task_update.add_argument("--manual-notes")
    subparsers.add_parser("ui", help="启动桌面组件")
    subparsers.add_parser("show", help="显示现有桌面组件；未运行时启动")
    smoke = subparsers.add_parser(
        "smoke",
        help="打包后自检：导入、词典、golden 向量、事实层往返；不连接邮箱",
    )
    smoke.add_argument("--report", type=Path, help="将 JSON 报告写入此文件（无控制台的 EXE 必需）")
    smoke.add_argument("--expect-tasks", type=int)
    smoke.add_argument("--expect-applications", type=int)
    smoke.add_argument("--expect-unresolved", type=int)
    smoke.add_argument("--keyring", action="store_true", help="同时往返一次系统凭据库")
    smoke.add_argument("--skip-dotnet", action="store_true", help="跳过 .NET/WebView2 导入检查")
    snapshot = subparsers.add_parser(
        "import-snapshot",
        help="把另一台机器导出的数据目录导入本机数据目录，并改写 config.toml 路径",
    )
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("--replace", action="store_true", help="目标已有数据时先备份再替换")
    snapshot.add_argument("--report", type=Path, help="将 JSON 报告写入此文件")
    snapshot.add_argument("--expect-tasks", type=int)
    snapshot.add_argument("--expect-applications", type=int)
    snapshot.add_argument("--expect-unresolved", type=int)
    return parser


def _expected_counts(args: argparse.Namespace) -> dict[str, int]:
    return {
        key: value
        for key, value in (
            ("tasks", args.expect_tasks),
            ("applications", args.expect_applications),
            ("unresolved", args.expect_unresolved),
        )
        if value is not None
    }


def _emit_report(payload: dict[str, object], report_path: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(rendered)


def _force_utf8_stdio() -> None:
    """Windows consoles and pipes default to the ANSI code page (GBK on zh-CN).

    The CLI prints JSON with Chinese company names for the agent bridge, so
    the streams are switched to UTF-8 the same way ``python -X utf8`` would.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def _frozen_default_command() -> list[str]:
    """``JobMailDesk.exe`` with no arguments opens the UI; the console twin prints help."""
    if Path(sys.executable).stem.lower().endswith("-cli"):
        return ["--help"]
    return ["ui"]


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    if argv is None and getattr(sys, "frozen", False) and len(sys.argv) == 1:
        argv = _frozen_default_command()
    args = _parser().parse_args(argv)
    if (LOCAL_ROOT / ".storage-move.json").exists():
        print("数据迁移尚未完成，请使用 JobMailDesk.exe 或 python -m job_mail_desk ui 重新打开程序。")
        return 1
    from .privacy_reset import PENDING, finish_pending_reset, show_reset_error
    if (LOCAL_ROOT / PENDING).exists():
        if args.command not in {"ui", "show"}:
            print("个人信息清除尚未完成，请打开桌面程序完成清除后再执行命令。")
            return 1
        from .ui_app import _claim_single_instance, _close_instance_handle
        try:
            finish_pending_reset(LOCAL_ROOT, _claim_single_instance, _close_instance_handle)
        except Exception:
            # Do not echo credential backend errors, paths, or old record data.
            show_reset_error("个人信息清除未完成，请关闭其他 JobMailDesk 进程并检查凭据库和文件权限。")
            return 1
    if args.command == "import-snapshot":
        # Runs before configure_logging() and ensure_config(): the log file
        # lives under LOCAL_ROOT and an open handle there would make Windows
        # refuse the backup rename, and a default config.toml must not race
        # the snapshot's own config.
        try:
            report = import_snapshot(args.source, LOCAL_ROOT, replace_existing=args.replace)
        except SnapshotImportError as exc:
            _emit_report({"ok": False, "error": str(exc)}, args.report)
            return 1
        except Exception as exc:  # noqa: BLE001 - the report is the installer's interface
            _emit_report({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, args.report)
            return 1
        expected = _expected_counts(args)
        mismatched = {
            key: (report.counts.get(key), value)
            for key, value in expected.items()
            if report.counts.get(key) != value
        }
        payload = report.to_dict()
        payload["expected"] = expected
        payload["ok"] = not mismatched
        if mismatched:
            payload["error"] = f"导入后的数量与预期不一致（实际, 预期）：{mismatched}"
        _emit_report(payload, args.report)
        return 0 if not mismatched else 1
    if args.command == "smoke":
        payload = run_selfcheck(
            expect_counts=_expected_counts(args) or None,
            keyring_roundtrip=args.keyring,
            include_dotnet=not args.skip_dotnet,
        )
        _emit_report(payload, args.report)
        return 0 if payload["ok"] else 1
    configure_logging()
    ensure_config()
    settings = load_settings()
    recover_file_transactions(LOCAL_ROOT / ".transactions")
    if args.command == "configure":
        configure_interactively()
        print("配置完成。请运行 jobmaildesk doctor 验证。")
        return 0
    if args.command == "doctor":
        checks = run_doctor(settings, online=not args.offline)
        for check in checks:
            print(f"{'OK' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
        return 0 if all(check.ok for check in checks) else 1
    if args.command == "scan":
        if args.preview_output and not args.identity_preview:
            raise ValueError("--preview-output 仅能与 --identity-preview 一起使用")
        summary = scan_once(
            settings,
            days=args.days,
            shadow=args.shadow or args.identity_preview,
            identity_preview=args.identity_preview,
        )
        if args.preview_output:
            export_identity_preview(summary, args.preview_output)
        rendered_summary = summary.to_dict()
        if args.identity_preview:
            rendered_summary["preview_count"] = len(summary.preview)
            rendered_summary["preview"] = "[written only to redacted preview file]"
        print(json.dumps(rendered_summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        run_forever(settings)
        return 0
    if args.command == "digest":
        path = generate_digest(
            args.period,
            MarkdownTaskStore(TASKS_DIR),
            DIGESTS_DIR,
        )
        print(path)
        return 0
    if args.command == "export":
        store = MarkdownTaskStore(TASKS_DIR)
        target = settings.obsidian_output if args.obsidian else DASHBOARD_FILE
        if args.obsidian:
            import_checked_states(target, store)
        export_dashboard(store.all(), target, settings)
        if settings.progress_enabled:
            export_progress(
                store.all(),
                settings.progress_output,
                source_path=settings.progress_source,
            )
        print(target)
        return 0
    if args.command == "sync-ledger":
        result = DesktopApi(settings).sync_ledger()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "research-queue":
        print(
            json.dumps(
                pending_requests(settings.research_queue),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "application-preview":
        records = preview_progress_applications(settings.progress_source)
        print(
            json.dumps(
                [record.to_dict() for record in records],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "application-import":
        if not args.from_progress:
            raise ValueError("当前仅支持 --from-progress。")
        with data_directory_lease(TASKS_DIR.parent / ".data.lock"):
            records = ApplicationRegistry(APPLICATIONS_DIR).import_progress(
                settings.progress_source
            )
        print(
            json.dumps(
                [record.to_dict() for record in records],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "dictionary-check":
        try:
            dictionaries = load_identity_dictionaries(args.user_dir)
        except DictionaryValidationError as exc:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "counts": dictionaries.counts(),
                    "sources": dictionaries.sources,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "dictionary-compile":
        report = compile_workbook(
            args.xlsx,
            args.output,
            load_identity_dictionaries(),
            sheet_name=args.sheet,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "unresolved-list":
        records = [
            record.to_dict()
            for record in UnresolvedStore(UNRESOLVED_DIR).all()
            if record.status == "pending"
        ]
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    if args.command == "unresolved-ignore":
        DesktopApi(settings).ignore_unresolved(args.source_hash)
        record = UnresolvedStore(UNRESOLVED_DIR).load(args.source_hash)
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "unresolved-resolve":
        unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
        record = unresolved_store.load(args.source_hash)
        if not record or record.status != "pending":
            raise ValueError("待归属记录不存在或已经处理。")
        result = DesktopApi(settings).resolve_unresolved_workflow(
            record.id,
            {
                "mode": "existing",
                "application_key": args.application_key,
                "company": record.company or "",
                "role": record.role or "",
                "recruiting_project": record.recruiting_project or "",
                "stage": record.stage,
                "manual_stage_status": "pending",
                "create_task": False,
                "expected_review_revision": record.revision,
            },
        )
        resolved = unresolved_store.load(record.id)
        print(
            json.dumps(
                {
                    "application_key": resolved.resolved_application_key
                    if resolved
                    else args.application_key,
                    "progress_node_id": resolved.progress_node_id if resolved else None,
                    "task_id": resolved.resolved_task_id if resolved else None,
                    "dashboard_counts": result.get("counts", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "task-list":
        print(
            json.dumps(
                list_tasks(
                    company=args.company,
                    role=args.role,
                    stage=args.stage,
                    include_irrelevant=args.include_irrelevant,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "task-update":
        changes = {
            key: value
            for key, value in {
                "status": args.status,
                "start_at": args.start_at,
                "end_at": args.end_at,
                "deadline_at": args.deadline_at,
                "company": args.company,
                "role": args.role,
                "stage": args.stage,
                "round": args.round,
                "action_summary": args.action_summary,
                "manual_notes": args.manual_notes,
            }.items()
            if value is not None
        }
        if not changes:
            raise ValueError("至少提供一个更新字段。")
        print(
            json.dumps(
                apply_task_update(settings, args.task_id, changes),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "ui":
        run_ui(settings)
        return 0
    if args.command == "show":
        if not show_existing_window():
            run_ui(settings)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
