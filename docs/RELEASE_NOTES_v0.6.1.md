# JobMailDesk Core v0.6.1 RC5

> 发布类型：内部候选版。macOS 当前使用 ad-hoc 签名，尚未 Developer ID
> 签名、公证或完成 72 小时双架构试运行，因此不作为正式稳定版发布。

## 邮件与身份

- 所有招聘邮件先进入待处理，自动识别只负责预填和候选建议。
- 邮件状态区分 fetch/parse/noncandidate/pending/resolved/ignored/tombstoned，
  parser 升级可安全重试未形成事实的邮件。
- IMAP 来源使用账号、文件夹、UIDVALIDITY 与 UID；重复 Message-ID 不再吞掉
  不同邮件。
- 公司识别采用审核词典精确匹配与一次安全招聘后缀归一化，不使用模糊公司合并。
- 公司、岗位、项目、地点、职位编号与 attempt 分开参与身份判断。
- RC4 使用 parser version `2026.08.15.5`，使此前误记为
  `noncandidate`、但尚未形成事实的翱捷邮件自动安全重放。
- 明确的网申回执标题现在优先于正文中的“云宣讲/邀你赴约/抢先拿 offer”
  营销页脚，不再整封提前丢弃。
- 对真实邮箱最近 30 天的 48 封邮件执行逐封只读影子审计；最终结果为
  48/48 读取、0 安全重试、0 MIME/解析失败、0 硬冲突。
- 修复 MIME HTML 拼接、附件子树、未知字符集、超大邮件有界前缀、
  RFC822.SIZE 差异、UID 校验和逐邮件 parser replay debt。
- 补齐字节跳动、中兴微电子、小米、芯动、小鹏、沐曦、兆易等公司/岗位/
  地点/批次/阶段字段，并过滤验证码、面试问卷、空宣及安全邮件。
- 新增海光信息审核别名，清理岗位前缀并识别七日截止；复核目标默认只按公司
  搜索，岗位名称不同也能人工选择同公司的现有申请链。

## 申请与待办

- progress-only、零任务申请仍进入公司卡、时间线、计数和进展 Markdown。
- 确认申请、可选待办和进展节点使用 revision 与可恢复文件事务。
- CLI 与桌面使用相同确认服务；合并预览绑定来源、目标和 revision。
- ENDED/archived 停止任务提醒、日历和待办投影；irrelevant 结果不再终止申请。
- 删除、合并与 UID 重放保留任务/申请来源墓碑。

## 复核与交互

- 每封待处理邮件可打开独立原生复核窗口；左侧原邮件、右侧申请/节点/待办。
- 支持同时打开多个复核窗口，并用 session/revision 拒绝过期提交。
- 提供新公司/岗位、新 attempt、更新 active、明确重新激活四种模式。
- 修复异步目标错配、dirty refresh、合并换目标、页面跳动、未读 ACK、
  Calendar/设置/capsule 竞态和主要可访问性问题。

## 数据与派生输出

- 台账 canonical marker 保留原 application key；同步台账真实写回 Registry。
- Obsidian/进展受管标记缺失时拒绝覆盖用户文件。
- dashboard cache 在事实变化时重试，不缓存混合快照。
- 导出、日历、提醒与未读活动使用可重试 outbox。

## 隐私

- IMAP 仍为只读与 `BODY.PEEK`，不会移动、删除、回复或标记已读。
- 正文只在内存展示；活动状态不保存正文、发件人、URL 或 locator。
- 私人行动链接使用系统凭据库密钥加密。

## 候选版限制

- 独立原生窗口、VoiceOver、Dock badge、Calendar TCC 和真实通知仍需在安装包上
  完成最终人工验收。
- Intel 构建与至少 72 小时正常使用试运行完成前，禁止正式发布。
