# JobMailDesk Core v0.6.1 RC5 验收记录

## 自动化事实层

- [x] 全量 Python 3.12 测试通过，仅保留 Windows mutex 平台跳过。
- [x] JavaScriptCore 校验主窗口和独立复核窗口脚本。
- [x] Python compileall、`git diff --check` 与源码秘密扫描。
- [x] progress-only、零任务、同公司双岗位与最后任务删除后申请仍可见。
- [x] mail outcome、parser replay、重复 Message-ID/不同 UID、UIDVALIDITY 与
  INTERNALDATE 迁移。
- [x] 理想汽车、江波龙、翱捷及公司/岗位负例使用生产词典 golden。
- [x] confirmation revision、事务故障恢复、CLI/桌面一致性、merge preview token。
- [x] ENDED/ignored/tombstone、任务解绑/重绑、Calendar end-only 正时长。
- [x] activity ACK、Dock 唯一计数、重启持久化与派生 outbox 重试。
- [x] Obsidian/进展缺少受管标记时 fail closed，dashboard cache 竞态重试。

## 打包闸门

- [x] 无 Git 提交模式的 exact source revision 与资源哈希清单已记录。
- [x] py2app arm64 构建、内置运行时 imports 与多窗口资源 smoke。
- [x] 包内源码资源哈希、无个人绝对路径、依赖 allowlist。
- [x] ad-hoc 深度签名与 ZIP 解包后严格签名。
- [x] 图标、Bundle/可执行文件名称、版本、source revision 和 arm64 架构。
- [ ] 只保留当前候选 ZIP、checksum 与当前安装应用。

## 安装包人工验收

- [ ] 独立复核窗口可多开、缩放、最小化和关闭；主窗口/胶囊几何不变。
- [ ] 左侧原邮件、右侧四种申请模式和可选待办可同时操作。
- [ ] 480×740 主窗口及 90%/108%/125% 字号、长中英文无重叠。
- [ ] 键盘、焦点返回、错误播报与 VoiceOver 基础流程。
- [ ] 扫描时切页/编辑不丢草稿、不抢页签；合并换目标必须重新预览。
- [ ] Dock 红点、公司逐卡 ACK、Calendar 键盘与 capsule 快速切换。
- [ ] Cmd-Q/关闭期间取消 scan 与原邮件读取，4 秒内退出并可立即重启。

## 真实账号只读 canary

- [ ] dedicated folder 的 UIDVALIDITY、UIDNEXT、FLAGS、UNSEEN 扫描前后不变。
- [ ] 翱捷两封分别生成两个 pending review，江波龙/理想汽车原位升级。
- [ ] progress-only、可选待办和忽略邮件各走一遍，重扫/重启不重复。
- [ ] 日志/导出不含正文、凭据、完整发件人、locator 或私人 URL。

## 分阶段试运行

- [ ] 隔离合成数据至少 24 小时。
- [ ] 真实账号只读 canary 至少 24–48 小时。
- [ ] Apple Silicon 与 Intel 正常使用至少 72 小时。
- [ ] 零数据丢失、重复申请链、邮件 flag 改动和未关闭 blocker/high。

## 发布边界

- 本记录未全部勾选前只能称为 RC。
- macOS 未完成 Developer ID 签名、公证与 stapling，不能称正式稳定版。
- 有限测试不能证明绝对零 bug；release-ready 仅表示 exact source/artifact
  通过上述风险闸门且没有已知 blocker/high。

## 当前候选构建

- 版本：`0.6.1rc5`；Bundle 版本：`0.6.1.5`。
- source revision：
  `8ded59f48d8477406d5a6a8db12f22e37b75cc8648b7dcbec0901558bd72a5f7`。
- arm64 ZIP：`JobMailDesk-Core-v0.6.1-rc5-macos-arm64-fixed-v22.zip`。
- ZIP SHA-256：
  `c7e02e8100f3c0ad1a0fe43bb1dce01035328f5c3728819b54b8d48d71e3f6ad`。
- 自动化：368 项测试，367 passed、1 个 Windows-only skip；Python compileall、
  JavaScriptCore、IDE lints、内置 imports/parser、archive privacy/dependency、
  ad-hoc 深度签名和解包后严格签名均通过。
- UID `1709047101/1709047102` 从旧 parser 的
  `noncandidate` 升级重放至待处理，并断言公司为翱捷科技股份有限公司、岗位为
  数字后端工程师。
- 最近 30 天真实邮箱影子审计：搜索 48、读取 48、安全重试 0、解析失败 0、
  硬冲突 0；所有邮件均有逐条结构化判断。
- 当前沙箱无权替换 `/Applications/JobMailDesk.app`；RC4 仍需手动替换。
  原生窗口/TCC/VoiceOver/长期试运行闸门仍未完成。
