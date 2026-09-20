# JobMailDesk Core v0.7.0 RC1（Windows 版候选）

> 发布类型：内部候选版。Windows 包由用户在本机用一键脚本构建，并用本机自签名证书签名；
> 未做 Authenticode 商业签名，SmartScreen 可能提示来源未知。macOS 包本轮不变。

## Windows 平台

- 首次提供可安装的 Windows 版本：`JobMailDesk-Setup-v0.7.0rc1-win-x64.exe`（Inno Setup，
  按用户安装到 `%LOCALAPPDATA%\Programs\JobMailDesk`，含开始菜单、可选桌面图标与登录自启）。
- 同时保留便携 ZIP `JobMailDesk-Core-v0.7.0rc1-win-x64.zip` 与 `.sha256`，供程序内更新检查识别。
- 安装目录内有两个程序：`JobMailDesk.exe`（托盘桌面程序）与 `JobMailDesk-cli.exe`
  （命令行：`doctor`、`smoke`、`configure`、`import-snapshot`、`task-list`、`task-update` 等）。
- 提醒：托盘气泡通知，回退 WinRT Toast；托盘图标显示唯一未读数角标。
- 系统日历：导出 `%LOCALAPPDATA%\JobMailDesk\JobMailDesk.ics`，在 Outlook 或 Windows 日历中打开/订阅。
- 启动前检测 Edge WebView2 运行时，缺失时给出下载入口。
- 数据迁移：`jobmaildesk import-snapshot <目录>` 一次导入 macOS 数据目录并改写路径。

## 待办与申请链一致性、复核改期、性能（全部平台）

- 完成 / 恢复 / 改期 / 改阶段一个待办会同步申请链节点与当前进度；进展卡保存不再误把已完成待办改回未完成。
- 复核新增「更新已有待办」模式：改期 / 改场地邮件原位写入既有待办，不再重复建卡；自动按阶段推荐目标待办，带乐观锁。
- 时间线不再重复显示同阶段的复核节点；已回收待办的节点不再复活。
- 事实层解析改用 libyaml 并按文件缓存，事务只刷新标记文件：单次操作从数秒降到半秒以内。

## Windows 后台运行

- 关闭窗口隐藏到托盘并继续扫描 / 提醒；托盘菜单「退出」才退出（环境变量 `JOBMAILDESK_CLOSE_EXITS=1` 可恢复关闭即退出）。窗口默认带任务栏按钮与最小化 / 最大化（可在设置关闭）。
- 定时或手动扫描到新的待确认邮件时弹出可点击的系统通知，点击直达「待处理」（PowerShell Toast 回退不可点击）。

## 事实层与稳定性（全部平台）

- 修复 Windows 上所有本地写入因目录 fsync 抛异常的问题；文件事务在 Windows 上可提交、可回滚、可恢复。
- 修复 Windows Python 3.12 上活动未读状态写入崩溃（`os.fchmod` 缺失）。
- 文件锁争用给出明确中文错误；IMAP 搜索日期不再依赖 locale。

## 已知限制

- Windows 上胶囊模式宽度受系统最小窗口宽度限制，可能比 macOS 宽；功能不受影响。
- 私人行动链接的加密密钥留在 macOS Keychain：迁移时仍待处理的记录，其链接在本机无法解密，普通重新扫描不会重新获取（已去重的邮件不再处理），可在复核窗口按需只读查看原邮件；已确认任务的链接保存在任务文件中，不受影响。
- 自签名证书仅在本机受信任；把安装包复制到另一台机器需要重新签名或信任证书。
- 真机人工验收（窗口、托盘、通知、退出 6 秒内、72 小时试运行）尚未由维护者完成，见 `docs/ACCEPTANCE_v0.7.0.md`。
