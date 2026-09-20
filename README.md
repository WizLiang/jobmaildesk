# JobMailDesk · 求职纸片

把招聘邮件整理为本地待处理卡片、求职进展、待办和日历。邮箱通过只读 IMAP 连接，核心功能不依赖大模型或 API Key。

这是在原项目基础上持续开发的 Windows 定制版。[下载 Windows 试用版](https://github.com/WizLiang/jobmaildesk/releases)。当前发布均为未签名预发布，尚需真实使用反馈；macOS 不作为当前支持目标。

当前源码版本：**0.7.0rc4**。已发布版本以 [Releases](https://github.com/WizLiang/jobmaildesk/releases) 为准；“设置 → 程序更新”会显示本机正在运行的版本。

## 当前功能

- 公司、岗位、笔试、面试和截止时间识别；信息不足时显示确认原因和识别来源，支持人工归属。
- 每个邮箱首次扫描最近 30 天，日常默认回看 3 天，停用后自动补齐缺口；可单次补扫 7／30／90 天，持续显示扫描结果与失败数量。
- 默认过滤宣讲会、岗位推荐和邀投简历；可选择保留具有明确时间和线下地点的宣讲会。
- 已忽略和自动过滤的邮件可分别查看并手动恢复；人工确认、忽略和删除状态优先于自动扫描。
- Windows 首次启动选择统一的数据目录，设置中可迁移；支持清除应用管理的个人信息。
- 本地 Markdown 存储，Obsidian 同步可选；当前没有 Notion 或语雀自动同步接口。

从 0.7.0rc2 开始，设置中的“程序更新”可检查此仓库的 GitHub 发布、下载并校验新版，确认后重启更新。“每天检查一次新版”默认关闭，只在用户开启后定期检查；下载和替换仍需主动点击。主界面不常驻显示版本号。

0.7.0rc1 及更早版本需要手动安装一次支持更新的版本。源码推送本身不会推送到用户电脑，只有发布了完整 Windows ZIP 与校验文件的 Release 才能成为更新。

## Windows 使用

获得经过验证的便携包后，解压完整文件夹并运行其中的 `JobMailDesk.exe`，不要只复制 EXE。首次打开选择数据存放位置，程序会创建 `JobMailDeskData`。

在设置中选择邮箱服务商，填写邮箱及客户端授权码，测试只读连接后保存。授权码存入 Windows 凭据管理器。扫描结果先进入待处理，确认后再进入正式进展和待办。

更换邮箱时必须填写新账号的授权码；同一账号保留原授权码才可留空。设置显示当前扫描账号，保存后才生效。误过滤的宣传或活动邮件可在“待处理 → 自动过滤”中移回待处理；升级前未留摘要的邮件可先补扫。

不连接 Obsidian 也能使用。关闭窗口会隐藏到托盘，完全退出需使用托盘菜单。更新便携版时先退出旧程序，再运行新文件夹中的程序，沿用原数据位置。

清除个人信息的范围和外部文档处理方式见 [隐私清除说明](docs/PRIVACY_RESET.md) 与 [隐私政策](PRIVACY.md)。

## 从源码开发

需要 Python 3.12 和 uv。在仓库根目录运行：

```powershell
uv sync --frozen --group dev
uv run pytest
./scripts/secret-scan.ps1
```

Windows 构建：

```powershell
./scripts/build.ps1 -OutputRoot "$PWD/release"
```

构建与发布约束见 [项目规则](PROJECT_RULES.md)，协作方式见 [贡献说明](CONTRIBUTING.md)。运行开发版可能连接已配置邮箱，测试请使用隔离的数据目录。

日常修改、版本升级和发布步骤见 [维护手册](docs/MAINTENANCE.md)。版本校验使用 `python scripts/version.py check`；准备下一版本时用 `python scripts/version.py set <版本号>` 同步源码和锁文件，随后更新日志、Release Notes 和验收记录。该命令不会自动推送或发布。

遇到问题可提交 [问题反馈](https://github.com/WizLiang/jobmaildesk/issues/new/choose)，附程序版本、复现步骤和脱敏截图；请勿上传邮箱授权码、原始邮件、配置文件或完整数据目录。

## GitHub 同步

本定制版仓库目标为 `WizLiang/jobmaildesk`。Windows SSH 配置、首次导入和日常同步见 [GitHub 使用说明](docs/GITHUB_SETUP.md)。

仓库只管理源码、测试和文档；个人配置、邮件记录、授权码、私钥和本地构建产物不应提交。`.gitignore` 是基础防护，提交前仍需审阅文件和执行秘密扫描。

## 许可与来源

本项目基于 [Chpeeeeea/job-mail-desk](https://github.com/Chpeeeeea/job-mail-desk) 二次开发，保留原作者版权与 MIT 许可。参见 [LICENSE](LICENSE.md)、[第三方声明](THIRD_PARTY_NOTICES.md) 和 [更新日志](CHANGELOG.md)。历史版本文档记录上游当时状态，不代表本定制版已完成相同验收。
