# JobMailDesk Core v0.7.0 RC1 验收记录（Windows 版候选）

## 自动化事实层（Linux 开发机 + Wine 下的 Windows 解释器）

- [x] 全量 Python 3.12 测试通过（Linux：共 476 项；不设 `JOBMAILDESK_SNAPSHOT_DIR` 时 474 通过 + 2 skip，
  设为真实快照目录时 475 通过 + 1 skip；skip 为 Windows-only 命名互斥体测试）。
- [x] Windows 解释器（python-build-standalone 3.12，Wine 7.0 内置 CRT，非微软 ucrtbase/msvcrt）下运行测试套件全绿
  （476 项，0 失败，1 skip 为需要真实快照目录的可选测试）；`os.replace`、路径与编码语义仅在 Wine 重实现下通过。msvcrt 锁的争用/超时/同范围解锁回归
  使用 Python 假 `msvcrt`（tests/test_windows_platform.py），未在任何真实 CRT 上验证；WMI、CopyFile2、.NET 由 Wine 缺失。
  以上均需真机复验（见下）。
- [x] 新增回归：目录 fsync/fchmod 保护、msvcrt 锁超时与同范围解锁、Windows 凭据库后端绑定、
  文件事务可写句柄 fsync 与回滚重试、可见通知契约、托盘角标契约、ICS 导出、快照导入、自检命令；
  第二轮 26 项待办 ↔ 申请链一致性 / 原位更新 / 后台通知 / 性能回归（`tests/test_task_chain_consistency.py`）。
- [x] 日程训练集 49/49，身份 golden 17/17，非候选 4/4，词典计数 533/129/2829/4。
- [x] 数据快照导入后数量为 94 待办 / 59 申请链 / 126 待处理，`config.toml` 无残留 macOS 路径。
- [x] 所有 PowerShell 脚本通过 PowerShell 7 语法解析；Inno Setup 脚本与运行时常量（互斥体名、
  WebView2 注册表键、AppUserModelID）一致。

## 打包闸门（由 Install-JobMailDesk.ps1 在 Windows 真机执行并写入 build-output\）

- [ ] `uv sync --frozen` 或离线 wheels 安装成功，pytest 全绿。
- [ ] PyInstaller onedir 产物含 `JobMailDesk.exe`、`JobMailDesk-cli.exe`、`_internal`、`JobMailDesk.ico`、`BUILD_INFO.txt`。
- [ ] `JobMailDesk-cli.exe smoke --keyring` 全部 PASS（含 `clr`、`webview.platforms.edgechromium`、tzdata、`_cffi_backend`、凭据库往返）。
- [ ] `Get-AuthenticodeSignature` 对两个 EXE 与 Setup.exe 均为 Valid。
- [ ] Setup.exe 静默安装成功，安装后自检含 94/59/126 数量核对通过。

## 安装包人工验收（真机）

- [ ] 首次启动出现托盘图标；窗口关闭/最小化/最大化可用；480×740 与 90%/108%/125% 字号无溢出。
- [ ] 单实例：重复启动只激活既有窗口；`JobMailDesk-cli.exe show` 同效。
- [ ] 托盘八项菜单可用；未读角标随确认清零；悬浮提示显示未读数。
- [ ] 手工待办到点收到可见通知，同一阈值不重复。
- [ ] 启用日历后 `%LOCALAPPDATA%\JobMailDesk\JobMailDesk.ics` 出现，Outlook/日历可打开；保存待办秒级返回。
- [ ] 设置页保存授权码后 `JobMailDesk-cli.exe doctor` 的「邮箱凭据」为已配置，「IMAP 只读连接」为 ok 且 UNSEEN/UIDVALIDITY/UIDNEXT 不变。
- [ ] 手动扫描 → 待处理卡 → 人工确认 → 申请链 + 进展节点 + 可选待办一次写入。
- [ ] 在周历 / 月历 / 待办任一页面把面试待办标记完成：进展卡节点状态变为已完成、申请链记录 `manual_stage_status: completed`、日历导出移除该事件；恢复后反向成立。
- [ ] 收到改期邮件 → 复核窗口默认「更新已有待办」并选中同阶段待办（包括邮件没有推荐链、只靠公司 + 岗位精确匹配到链的情况）→ 确认后仍只有一张卡、时间为新时间、时间线单行；改场地类无时间邮件确认后原时间与备注保留。
- [ ] 点窗口「×」后托盘图标仍在、定时扫描继续；托盘「退出」6 秒内结束进程。窗口有最小化 / 最大化按钮并出现在任务栏。
- [ ] 后台定时扫描或托盘「立即扫描」识别到新邮件时右下角出现通知并进入通知中心；点击通知打开主窗口（最小化时恢复）并切到「待处理」；待办提醒气泡点击只显示窗口。
- [ ] 标记完成 / 确认归属在 1 秒内返回（真实数据量）。
- [ ] 退出：托盘「退出」6 秒内结束、无残留托盘图标、无 `msedgewebview2.exe` 孤儿进程；窗口「×」只隐藏（设置页显示「关闭窗口只隐藏到托盘」提示）。设 `JOBMAILDESK_CLOSE_EXITS=1` 启动时「×」退回为退出且提示消失。
- [ ] 数据锁争用：GUI 运行中用 `JobMailDesk-cli.exe task-update` 写入，或两个进程同时写；第二个进程在约 20 秒内
  得到「另一个 JobMailDesk 正在写入本地数据」且 `.data.lock` 句柄释放，不留死锁。
- [ ] 事实文件被另一进程（编辑器/资源管理器预览）打开时保存待办：`os.replace` 失败路径给出明确错误且原文件完整；
  GBK 代码页的控制台/管道里 `JobMailDesk-cli.exe task-list` 的 JSON 仍为 UTF-8。
- [ ] 72 小时试运行，无卡死、无重复建链、无结束状态复活。
