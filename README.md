# JobMailDesk · 求职纸片

把招聘邮件整理为本地待处理卡片、求职进展、待办和日历。邮箱通过只读 IMAP 连接，核心功能不依赖大模型或 API Key。

这是在原项目基础上持续开发的 Windows 定制版。当前为本地候选版本，尚未完成正式发布验收；本仓库暂不承诺已有可下载的正式安装包。macOS 构建脚本保留，仍需独立验证。

## 当前功能

- 公司、岗位、笔试、面试和截止时间识别；信息不足时显示确认原因和识别来源，支持人工归属。
- 首次扫描最近 30 天，日常默认回看 3 天；可单次补扫 7／30／90 天，显示扫描阶段与结果。
- 默认过滤宣讲会、岗位推荐和邀投简历；可选择保留具有明确时间和线下地点的宣讲会。
- 已忽略邮件可查看并手动恢复；人工确认、忽略和删除状态优先于自动扫描。
- Windows 首次启动选择统一的数据目录，设置中可迁移；支持清除应用管理的个人信息。
- 本地 Markdown 存储，Obsidian 同步可选；当前没有 Notion 或语雀自动同步接口。

本定制版已移除应用内的在线更新检查及版本展示。GitHub 用于源码协作，上传代码不会自动更新已安装程序。

## Windows 使用

获得经过验证的便携包后，解压完整文件夹并运行其中的 `JobMailDesk.exe`，不要只复制 EXE。首次打开选择数据存放位置，程序会创建 `JobMailDeskData`。

在设置中选择邮箱服务商，填写邮箱及客户端授权码，测试只读连接后保存。授权码存入 Windows 凭据管理器。扫描结果先进入待处理，确认后再进入正式进展和待办。

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

## GitHub 同步

本定制版仓库目标为 `WizLiang/jobmaildesk`。Windows SSH 配置、首次导入和日常同步见 [GitHub 使用说明](docs/GITHUB_SETUP.md)。

仓库只管理源码、测试和文档；个人配置、邮件记录、授权码、私钥和本地构建产物不应提交。`.gitignore` 是基础防护，提交前仍需审阅文件和执行秘密扫描。

## 许可与来源

本项目基于 [Chpeeeeea/job-mail-desk](https://github.com/Chpeeeeea/job-mail-desk) 二次开发，保留原作者版权与 MIT 许可。参见 [LICENSE](LICENSE.md)、[第三方声明](THIRD_PARTY_NOTICES.md) 和 [更新日志](CHANGELOG.md)。历史版本文档记录上游当时状态，不代表本定制版已完成相同验收。
