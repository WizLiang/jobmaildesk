# Contributing

感谢参与 JobMailDesk。开始前请阅读 [项目规则](PROJECT_RULES.md) 与[维护、更新和发布规则](docs/MAINTENANCE.md)。

## 基本约束

1. 只使用匿名测试样例。
2. 保持 IMAP `readonly=True` 与 `BODY.PEEK`，不得修改邮件状态。
3. 修改解析、任务链、去重或同步逻辑时增加回归测试。
4. 保持公开 Research 数据与私人邮件数据分离。
5. 不直接向 `main` 开发；使用单一目标的短期分支和 Pull Request。
6. 用户可见变化必须更新 `CHANGELOG.md` 和对应使用文档。

## 提交前

```powershell
python scripts/version.py check
uv run --frozen pytest
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\secret-scan.ps1
git diff --check
```

开发依赖使用 Python 3.12 与 `uv sync --frozen --group dev` 安装。系统没有相应 Python 时，版本命令可用 `uv run --frozen python scripts/version.py check`。

Pull Request 应说明变化、原因、用户影响、验证结果、隐私/兼容性影响和回滚方法。当前以 Windows x64 GitHub Actions 通过为合并门禁，macOS 仅手动选跑。正式版本来自已合并的 `main`；授权的 Windows RC 可按维护手册的分支发布例外执行，并明确标为 prerelease。

需要改版本时使用 `python scripts/version.py set 0.7.0rcN`（把 `N` 替换为候选序号），再运行 `check`；历史标签和发布资产不得覆盖。先集中修复和验收，再创建一次新版本，纯文档修改通常不单独发包。
