# JobMailDesk 项目记忆：产品演进、故障复盘与防回归约束

更新时间：2026-08-13

## Windows 本地候选版补充：2026-09-19 宣传过滤

- 后续反馈确认首次扫描兼容缺陷：`_prepare_uid_source_migration` 在空状态也执行“48 小时前静默建立基线”，造成 fetched 覆盖 30 天但识别只剩近期邮件。`2026.09.19.2` 只对存在旧 Message-ID outcome 且没有 UID 命名空间的消息建立兼容基线；空白安装完整识别 30 天，版本重放修正旧 noncandidate。服务回归必须同时覆盖新安装与升级补识别，不得用读取数充当已识别数。
- 新增 `include_onsite_sessions` 偏好，默认 false；开启时只放行具备开始时间与线下场地的宣讲。公开宣传仍为“招聘通知”，不因“直通 offer”成为个人 Offer。偏好参与 parser replay version，切换后复查 30 天。
- 自动过滤改为独立 `filtered` 状态（可重算），与永久 `ignored`（用户操作）分开；上一个 RC 仅在 reason/version 精确匹配且无确认关联时迁移自动忽略记录。已确认、手动忽略和墓碑保持最高优先级，自动恢复增加 revision，历史未读活动仍去重。
- `scan_runs` 向后兼容新增回看范围、文件夹、跳过／过滤／解析失败数量，UI 显示读取数与候选数、保留首次读取统计；未记录的历史范围保持 NULL，不伪造 30 天。诊断只读聚合元数据，不读取邮箱正文或授权码。

- 用户要求宣讲会、智联推荐、邀投简历及校园招聘宣传不进入待处理。这取代第 22 节“线下有场地即放行”的旧产品策略；具体时间、地点、个人称呼或“现场面试直通 Offer”不构成个人招聘进度。
- `2026.09.19.1` 解析器区分宣传信号和已投递／个人环节证据，保护带营销页脚的回执、笔试和面试通知；“若您已投递，请忽略”等条件句不是投递证据。
- 规则升级按原有最近 30 天窗口重放。只对完整读取且尚未确认的旧宣传记录自动忽略，保留稳定来源 ID、增加复核 revision，并将对应未读活动标为已过滤；活动序号与去重历史保留，其他未读不清空。已确认、手动忽略和墓碑的优先级不变；截断正文先重试，不据此撤下旧记录。
- 回归使用虚构邮件及既有脱敏 golden，真实用户邮箱与授权码不进入测试。旧宣讲向量标记 filtered，个人宣讲暨笔试邀请和技术面试向量继续验证时间提取。

本文记录从 macOS 产品优化到 Android 原生移植期间已经发生的问题、根因、修复方式和永久约束。它不是发布说明；开发前应与 `PROJECT_RULES.md` 一并阅读。

## 1. 产品与数据模型演进

### 邮件读取与扫描

- 163/126/yeah 登录后要求 IMAP `ID` 命令。普通登录成功不代表可以直接检索；网易提供商必须发送 RFC 2971 ID。
- IMAP 永远只读：文件夹以 `READ_ONLY` 打开，正文使用 `BODY.PEEK`，禁止移动、删除、回复或隐式标记已读。
- 曾因 `state.db` 将 39 封历史邮件标记为 processed，手动扫描全部 skipped。手动重扫和解析器版本升级必须支持受控重放，但仍按消息哈希幂等合并。
- 正文和授权码不得落入 Room、SQLite、DataStore、日志或导出文件。只保存结构化事实、脱敏证据和邮箱定位符。
- 授权码使用系统安全存储；Android 使用 Keystore，并尽量以 `CharArray`/buffer 处理，所有异常路径都清零。

### 申请链身份与生命周期

- 申请链身份不能只靠公司名。稳定身份至少综合公司、岗位、项目/批次、职位编号和 attempt；地点只能辅助新链指纹，不能单独触发合并。
- 同公司不同岗位必须分链；同岗位结束后出现新的投递事件应创建新 attempt，不能复活旧链。
- 旧台账可能没有 `application_key`。编辑必须优先通过稳定 ID、legacy ID 和迁移映射定位，不能因为缺 key 进入“新建申请”。
- 人工设为 `ENDED`/“已结束”必须优先于自动推导并持久化；普通邮件重放不得重新激活。只有明确人工操作或新的投递 attempt 才能激活。
- 删除申请链必须二次确认并保留 tombstone/来源哈希，防止历史邮件重放后复活。
- Room 更新使用 `@Upsert`，不能用 SQLite `REPLACE`，后者是 DELETE + INSERT，可能破坏关联数据。

### 解析、归属与时间线

- 高置信度自动归属；中低置信度、身份冲突和缺少岗位的信息进入 unresolved，不能猜测合并。
- 归属对话框必须严格按标准化“公司 + 岗位”唯一匹配：新公司和旧公司新岗位默认新建链；只有旧公司旧岗位唯一命中才默认更新旧链。
- 候选申请列表必须随公司/岗位输入变化重新过滤；新建模式不得保留或显示全局旧申请 selector，也不能让多个 option 同时 selected。
- 归属、节点更新和可选待办使用一次确认写入。未勾选待办时仍保留原邮件时间线节点，但不得进入待办、Markdown、日历、通知或 `current_task_id`。
- unresolved 转任务必须保留原始 `record.id` 作为 `source_message_hash`，不能再对 `unresolved:<id>` 二次哈希；失败不得消费 unresolved 或遗留空申请链。
- 阶段、状态、进度和下一步使用受控选项；公司、岗位、地点、职位编号等详情允许编辑。
- 岗位地点需解析“工作地点/岗位地点/办公城市”等显式标签并保存来源；地点不改变旧主键。
- 中文时间必须覆盖 `24:00`、同日时间窗、中文“14点00分”、时长补全和“48小时内”等相对截止时间。
- Golden vectors 是解析器契约。新增真实邮件句式时先加向量，再改规则；例如“感谢您投递产品经理岗位”必须提取“产品经理”。
- 时间线按稳定事件键去重、按事件时间排序；同一邮件/节点不能重复展示。保留可安全打开的原邮件定位。

### UI 与交互

- macOS 窗口必须保留关闭、最小化、缩放按钮，不强制置顶；应用保持单实例。
- Spotlight 多个应用通常来自遗留构建/安装包。发布清理只保留当前安装版本和最新 ZIP，不删除用户数据目录。
- 公司卡按公司聚合。多申请时显示二级申请卡；单申请时直接展开详情，避免无意义嵌套。
- 折叠卡必须显示岗位、地点、节点、节点状态胶囊；窄屏保持横向信息关系，允许弹性缩放/换行但不能重叠。
- 展开卡的操作按钮对齐、字号一致；常用状态支持快速编辑，详细字段进入编辑页。
- 破坏性操作必须确认；终止申请显示“重新激活”，不能把“推进状态”误用于已结束链。

## 2. Android 原生移植的已知问题

### 固定工具链

- 当前组合：AGP 9.3.0、Gradle 9.5.0、Kotlin 2.3.21、KSP 2.3.11、compile/target SDK 37.0、Build Tools 36.0.0。
- AGP 9.3 lint 的 `JavaDocParser` 会调用 JDK 21 的 `List.removeLast()`；在 JDK 17 运行 lint 会 `NoSuchMethodError`。Gradle 运行时固定 JDK 21，Java/Kotlin 字节码目标仍为 17。
- Android 37 是 minor SDK，包名和路径必须写作 `platforms;android-37.0` / `platforms/android-37.0`。
- `compileSdk` 使用 AGP 9.3 DSL：`release(37) { minorApiLevel = 0 }`。
- `libs.versions.toml` 的 version、library、plugin alias 不得重名。Protobuf 编译器版本使用 `protoc`，插件版本使用 `protobuf-plugin`。

### 工具链安装故障

- Homebrew `/opt/homebrew/Cellar` 不可写时，不再反复修复系统目录；工具链安装到 `android/.toolchains` 和 `android/.android-sdk`。
- VPN/代理或沙箱可能让 Adoptium/Google 下载返回 403。优先使用官方 GitHub release 直链并校验版本/哈希。
- `sdkmanager --licenses` 必须先完成。手工解压平台后再运行 sdkmanager 可能产生 `android-37.0-2`；只保留 sdkmanager 注册的规范目录。
- Cursor 沙箱中 Gradle 可能因 `FileLockContentionHandler` 无法枚举 wildcard IP 而启动失败。这是执行环境限制，不是源码错误；最终构建在用户终端或 CI 运行。

### 编译与 API 易错点

- Kotlin nullable 值不要依赖含糊的方法引用推断；DAO 查询使用显式 null 分支。
- `PlatformResult` 的错误文本字段是 `detail`；WorkManager 的 `WorkInfo` 查询结果允许为 null。
- Compose `LazyColumn` 传递内容时使用 `content = { content() }`。
- 旧 WebView 开关如 `databaseEnabled`、`saveFormData`、file-URL access 已废弃且在当前安全配置下冗余，不要重新加入。
- WebView 禁用 JavaScript、文件/内容访问和网络加载；外链仅允许无 user-info、host 非空的分层 HTTPS URI。
- 通知 action 使用 WorkManager，不在 BroadcastReceiver 内直接执行 suspend 数据操作。
- Android 权限在功能首次使用时请求，不在应用启动时集中请求。拒绝日历/通知权限不能破坏任务保存。

### 数据与后台任务

- Room 是 Android 独立事实层；Android 第一版不与 macOS 数据库实时同步，邮箱重扫建立本机数据。
- 扫描持久化必须区分 parsed 与 processing failure；部分写入失败不能把邮件永久 tombstone。
- 任务更新要传入已有任务集合，确保延期、取消和重复邮件合并，而不是每次创建新任务。
- WorkManager 扫描间隔不得低于系统 15 分钟；用户手动扫描需要显示最终 output message。
- 日历必须让用户选择可写日历。已保存的 calendar ID 不存在时明确降级，不静默写入其他日历。
- SAF 只修改受管 block，保留用户手写区域与尾部空白；读取和分享设置字符上限，稳定 ID 排除 block marker。

## 3. 构建与发布的单次验收流程

1. 运行 `android/scripts/verify-build.sh`；不得只看 `compileDebugKotlin` 成功。
2. 依次通过 `testDebugUnitTest`、`lintDebug`、`assembleDebug`。
3. 单测失败先读 `app/build/test-results/**/TEST-*.xml` 的 expected/actual，不凭行号猜原因。
4. lint 自身崩溃先核对 JDK/AGP 兼容矩阵；不要用全局 `abortOnError=false` 掩盖真实 lint。
5. 校验 APK 存在、可由 `apksigner verify` 验证，并生成 SHA-256。
6. 在 API 29 和当前 Android 版本验证：首次启动、权限拒绝、邮箱配置、手动扫描、后台扫描、通知、日历、原邮件、导出。
7. 用真实样例验证：重复邮件不重复建链、结束状态不复活、同公司多岗位不误合并、删除链不重生。
8. 所有闸门通过前只称为测试构建，不宣称 release-ready。

### macOS 打包注意事项

- py2app 0.28 与现代 setuptools 组合时，`pyproject.toml` 的依赖会被映射成 `install_requires` 并触发拒绝。专用 command class 必须在 `finalize_options()` 前清空该旧字段；运行依赖应先安装进构建环境。
- 仓库根目录的 `android/` 会被 modulegraph 当作 Python namespace package。macOS 打包必须显式 exclude `android`，否则可能把本地 Android SDK 整体复制进应用。
- uv standalone CPython 3.12 将 zlib 静态链接，`zlib.__file__` 为空，而 py2app 0.28 仍无条件读取它。打包脚本需保留静态 zlib 兼容处理，并验证归档后的应用可通过严格 codesign 校验。
- 发布前依次运行完整 pytest、JavaScriptCore 语法校验、py2app、ad-hoc 深度签名、ZIP 解包复验和源码/包内 UI 资源哈希比对；最后删除临时解包与构建工作目录。

## 4. 开发方法与防回归原则

- 先确认唯一事实层、稳定 ID 和生命周期优先级，再改 UI；不要用展示层补丁修复数据模型问题。
- 每次 schema 变化必须有向后兼容迁移和旧数据测试。
- 每个解析 bug 固化为 golden vector；每个生命周期 bug 固化为服务/DAO 回归测试。
- 依赖升级前同时核对 JDK、Gradle、AGP、Kotlin、KSP、SDK 和 CI 镜像，不单独升级某一项。
- 先运行受影响的快速测试，再运行完整验收；发布前不得省略 lint、APK 签名和真实设备检查。
- 不把环境故障误报为代码故障，也不因环境故障跳过最终验证。
- 不在未验证时连续打多个“修复版”包；集中修复、一次验收、一次交付。

## 5. 2026-08-10 Android 验收基线

- Temurin JDK 21 下 `testDebugUnitTest`、`lintDebug`、`assembleDebug` 全部通过，共 64 个 Gradle tasks。
- APK 元数据：`com.jobmaildesk.android` v1.0、minSdk 29、targetSdk 37，覆盖 Android 10+。
- 测试 APK 已通过 `apksigner` 验证，使用 APK Signature Scheme v2 和 Android Debug RSA 2048 证书。
- 交付物：`build/JobMailDesk-Android-v1.0-debug-signed.apk`。
- SHA-256：`f293cc740b8997a8f1965d52685df45f43ab54628d8155620cb10e171e7803e5`。
- 验收时没有连接 Android 设备；真实设备首次启动、权限弹窗和后台调度仍应作为安装后的 smoke test 执行。

## 6. 2026-08-10 macOS 归属工作流 RC 基线

- 六个重点回归文件：44 passed、1 skipped；完整 pytest 套件全部通过，仅 1 项平台相关跳过。
- JavaScriptCore 语法校验通过；归档内 `app.js` 与源码 SHA-256 一致。
- arm64 应用通过 `codesign --verify --deep --strict`，ZIP 解包后再次通过签名校验。
- 交付物沿用既有修复包命名：`build/JobMailDesk-Core-v0.6.0-macos-arm64-fixed-v13.zip`；应用包名为 `JobMailDesk.app`。
- macOS 图标必须通过 py2app 的 `iconfile` 显式配置；归档内 `CFBundleIconFile` 和 `.icns` 资源均已回读验证。
- SHA-256：`defe24dcada8e55d8bf0b10961ab7c47a9428f80aa4bcfdb91d96d03d2957fdf`。

## 7. 首页、通知、待办删除与申请链合并约束

- 本地提醒和系统日历不能依赖 IMAP 凭据。曾因 UI 只在 `load_credential()` 成功后启动整个 scheduler，导致纯手工待办没有 macOS 通知；现在 scheduler 始终启动，只有 `ScheduledJobs.scan()` 在无凭据时跳过邮件扫描。
- 新建或改期待办后必须清除旧提醒阈值并立即检查一次提醒资格。待办移入回收站后立即停止提醒、退出常规视图和日历；恢复时按删除前状态恢复；永久删除仅保留稳定 ID、原始来源哈希和最小墓碑，邮件重放不得复活。
- 首页速览只显示未来事项：按“72 小时重点 → 本日 → 本周其余”去重，排除 done、snoozed、非 actionable、已删除和已过期事项。待归属邮件按收件时间倒序，常规宽度最多 3 封、窄屏最多 2 封。
- 日历状态不能只靠颜色：有任务日期同时使用底色、边框和数量徽标；today、selected、urgent 的组合状态必须分别验证，深色选中态要重设文字、徽标和事件点对比度。
- 合并选择器的 Registry choice 是 `same_company` 等权威匹配元数据来源。与 dashboard progress 合并展示字段时，禁止用缺少匹配标志的快照覆盖 choice；同公司不同岗位必须保留为可选目标。
- 跨公司合并必须在预览和最终写入两层校验显式确认。冲突值只能从来源或目标中选择；合并需迁移任务、unresolved 引用、人工时间线、aliases、双方 legacy ID 和删除来源哈希，来源链保留 `merged_into` 墓碑。
- 多文件合并在任何事实层写入失败时必须恢复来源/目标申请、任务和 unresolved 原记录。生成视图可重建，不能反过来成为事实层。
- 展开申请卡操作保持 3+2 等宽布局；用户字体缩放限制为 90%–125%。公司、岗位、地点、胶囊和按钮使用 `min-width: 0`、单行省略或受控换行，禁止因放大字体造成横向溢出或破坏原有布局。

## 8. 2026-08-10 首页与合并优化 macOS RC 基线

- 完整 pytest：`200 passed, 1 skipped`；JavaScriptCore 语法、`git diff --check` 和源码秘密扫描通过。
- 480px/窄屏视觉契约已由 UI 源码回归覆盖：速览分区、最新 2–3 封待归属邮件、3+2 按钮、长文本省略、日历组合状态和 90%–125% 字体缩放均有稳定 class/结构断言。
- py2app 必须直接生成 `JobMailDesk.app`，且 `CFBundleName`、`CFBundleDisplayName`、`CFBundleExecutable` 和 `Contents/MacOS/JobMailDesk` 全部同名。只在构建后重命名 `.app` 目录会留下不一致的 LaunchServices 元数据。
- arm64 bundle 的内置 Python 运行时与新 dashboard 合约 smoke test 通过；沙箱内直接启动已进入应用代码，但因禁止写入 `~/Library/Application Support/JobMailDesk` 而退出，因此该包仍按 RC 交付，安装后的真实窗口与通知权限需在正常用户环境 smoke test。
- ad-hoc 深度签名、ZIP 解包后严格签名、图标/Info.plist、源码与包内 UI/Python 资源哈希、Android namespace 排除和 arm64 架构全部通过。
- 交付物：`build/JobMailDesk-Core-v0.6.0-macos-arm64-fixed-v14.zip`（34 MB）。
- SHA-256：`184fb6150b6db92177d19da796affd967713ba1e0e9c549abbff1f0ea727aca3`。

## 9. macOS 退出与身份识别约束

- `BackgroundScheduler.shutdown(wait=False)` 只停止后续调度，不会终止正在执行的线程。2026-08-13 曾有一次无超时 IMAP 扫描持续超过 12 小时；关闭窗口后 Python 等待非守护 worker，最终被 macOS 判定为无响应。
- IMAP 连接必须有有限超时，并注册到应用级退出协调器；退出时先停止新调度，再设置停止事件并中断活动连接。手工扫描与定时扫描必须共享同一个门闩，设置重载不得留下第二个并发扫描。
- macOS `closing`、`closed` 与最终清理必须走同一个幂等 shutdown；退出验收需覆盖活动登录/抓取，并要求进程 4 秒内结束且可立即重启。
- 公司和岗位不能由“第一个正则命中”直接决定。标题“您已应聘思特威电子科技，快来Get最新招聘信息查询方式”会让宽泛的“文字 + 招聘”规则误取“快来Get最新”；营销 CTA、平台名和动作短语必须先过滤。
- 身份提取固定为“多来源候选 → 规范化 → 噪声过滤 → 字段级证据/置信度 → 冲突决策”。发件人、显式标签、完整申请句式、审核词典和模板是独立证据；弱标题前缀不能覆盖强证据。
- 年份、地点、批次、阶段和轮次从岗位名拆出单独保存。公司加年份、岗位子串只能排序候选，不能独立触发自动归属或新建。
- 本机纠错学习只使用人工确认的待归属操作，或通过 `source_message_hash/mail_locator → task → stable application_key` 验证的近期邮件与已锁定申请链。禁止按公司名相似度跨链学习。
- 学习库只保存纠正前后 token、HMAC 发件签名、HMAC 标题形状、时间和启用状态；不保存邮件正文、完整邮箱、URL 或凭据。冲突规则自动停用，历史人工锁定申请不被静默重写。
- 每次解析修复必须先增加脱敏 golden vector；低置信度首封新公司邮件进入待归属并预填候选，宁可增加人工确认，也不能生成错误申请链。

## 10. 2026-08-13 退出与身份识别 macOS RC 基线

- 完整 pytest：`207 passed, 1 skipped`；Python compileall、JavaScriptCore 语法、`git diff --check` 和源码秘密扫描通过。
- 阻塞 IMAP 回归证明退出协调器会中断活动连接；手工和定时扫描共享同一门闩。真实 Cmd-Q/Dock 退出仍需安装后的窗口 smoke test。
- 思特威电子科技 golden vector、词典校验、低置信度 fail-closed、本机学习命中/冲突/停用，以及“近期邮件来源哈希 → 已确认申请链”的监督学习均有回归测试。
- py2app 内置运行时完成思特威样例与退出协调器 smoke test；源码与包内 16 项 Python/UI/词典资源哈希一致。
- ad-hoc 深度签名、ZIP 解包后严格签名、图标/Bundle 名称、无 `__pycache__`、Android namespace 排除和 arm64 架构全部通过。
- 交付物：`build/JobMailDesk-Core-v0.6.0-macos-arm64-fixed-v15.zip`（33 MB）。
- SHA-256：`03d41df4a30082d24d68a3ff3b69a5492cc2d7675df6aa4a848fcde7eec55f8b`。

## 11. 邮件复核、待办与未读约束

- 招聘邮件扫描固定为 review-first：身份解析只生成预填、候选和建议，任何 matched/new/conflict/unresolved 都先进入待处理。确认前不得创建申请、任务、进展节点、日历或提醒。
- 来源哈希是永久去重事实；解析器升级不得删除 processed hashes。相同待处理记录只有语义哈希变化时才增加 revision 和未读。
- 邮件确认必须校验请求 ID、待处理 revision 和申请 revision，并通过可恢复文件事务一次提交申请、确定性进展节点、可选任务及待处理结果。导出、日历、提醒和 Dock 是提交后派生操作。
- 仅更新进展时不得创建假的非 actionable 任务。邮件进展节点 ID 由来源哈希稳定生成；任务必须由用户明确勾选。
- 阶段推进由后端统一层级判断；低阶段邮件不能回退当前申请，结束/归档链不得自动复活。
- “考试开始/结束时间 + 考试时长”表示可作答窗口和预计时长：结束标签写入 deadline，时长单独保存，不能用 `start + duration` 覆盖明确截止。
- 手工待办只要 start/end/deadline 任一存在即为 planned；无时间手工待办仍显示在待办，同时进入待处理补信息。done/cancelled/irrelevant/expired 不得因编辑或重复创建复活。
- 待办弹窗不得改变原生窗口几何；mutation 和定时刷新必须恢复页签、展开状态与滚动位置。表单错误必须在弹窗内明确显示。
- 未读活动只保存安全 ID、序号、类型、公司范围和六页签路由，不保存正文、发件人、URL、定位符或行动文本。普通页签按渲染快照 ACK；进展按公司一次清当前全部更新。
- Dock 数字按仍有任一未读投递的唯一活动计数，不累加跨页签副本。升级首次建立空基线，禁止把全部历史记录标为未读。
- 待处理阶段的行动 URL 只保存加密引用，密钥放系统凭据库；确认创建任务后才进入任务结构化来源字段。

## 12. 2026-08-14 复核、待办与未读 macOS RC 基线

- 完整 pytest：`240 passed, 1 skipped`；Python compileall、JavaScriptCore 语法、`git diff --check` 和源码秘密扫描通过。
- 思特威 golden vector确认岗位为“数字后端工程师”，作答窗口为 8 月 14 日 00:00 至 8 月 17 日 23:55，时长 90 分钟，行动链接优先于退订/页脚链接。
- review-first、语义 revision、永久来源去重、事务回滚/启动恢复、进展-only、可选待办、阶段不回退、加密链接和现有思特威任务迁移均有回归测试。
- 六页签 ACK 水位、进展逐公司清除、跨页签唯一 Dock 计数、重启持久化、隐私序列化和竞态均有独立测试。
- py2app 首次 smoke 捕获到 Fernet 运行时缺少 `_cffi_backend`；最终包显式包含 `cryptography`、`cffi`、`_cffi_backend`、AppKit 和 PyObjCTools，内置 Python 导入/解析/未读 smoke 通过。
- 最终 ZIP 解包后通过 ad-hoc 深度签名、Bundle/图标、25 项源码资源哈希、CFFI 文件、无 Android namespace、无 `__pycache__` 和 arm64 架构校验。
- 当前执行环境策略禁止使用 LaunchServices `open`，直接执行 `.app/Contents/MacOS` 在 macOS 26 会由 `_RegisterApplication` 主动中止，因此真实窗口仍需用户安装后 smoke test；本包保持 RC，不称 release-ready。
- 交付物：`build/JobMailDesk-Core-v0.6.0-macos-arm64-fixed-v16.zip`（36 MB）。
- SHA-256：`51ddc21a36ca5f288bbd7c9b2890a823a86b08bfcdece0bb1cc60ae1b20f9f00`。

## 13. IMAP 来源身份与明确标题优先级

- 招聘平台可能复用或错误生成 `Message-ID`；不能将其单独作为 IMAP 邮件的永久主键。稳定来源身份使用“账号指纹 + 文件夹 + UIDVALIDITY + UID”，`Message-ID` 只保留为兼容证据。
- 从旧 Message-ID 主键迁移时，已有任务/待处理的 mail locator 直接建立 UID 基线；无事实记录的历史邮件静默建立基线，最近 48 小时的未知 UID 必须重新解析，以回收刚到达但曾因 ID 冲突被跳过的邮件。
- 待处理文件可能仍使用旧来源哈希。重放时先按 mail locator 找到原记录并原位增加 revision，禁止因主键升级生成第二张待处理卡。
- 标题开头明确的非通用 `【公司】` 在招聘通知中强于未审核发件人显示名。两者文字不同不能仅因分数接近就把公司清空；只有两个同等级强证据冲突时才 fail closed。
- “已成功申请【岗位】职位”和“感谢您投递某有限公司公司的某岗位”是正式网申回执句式，必须分别提取公司、岗位、职位编号和网申阶段，并加入 golden vectors。

## 14. 2026-08-15 匹配与 UID 恢复 macOS RC 基线

- 完整 pytest：`241 passed, 1 skipped`；新增江波龙、翱捷科技双岗位及重复 Message-ID/不同 UID 回归。
- 11:27 江波龙邮件已确认由标题公司与发件人近似分数冲突导致空公司；修复后提取“江波龙｜IC实现工程师（上海）(J11374)｜网申”。
- 11:14、11:15 两封翱捷回执已固化为“翱捷科技股份有限公司｜数字后端工程师/数字中端实现工程师｜网申”。
- UID 来源迁移只静默基线化已有 locator 或 48 小时前的历史邮件；最近 48 小时未知 UID 会重新解析，待处理旧哈希按 locator 原位升级，不重复建卡。
- py2app 内置 Python 对三条真实句式 smoke 通过；ZIP 解包后签名、图标、源码资源、CFFI、无 Android namespace、无 `__pycache__` 与 arm64 架构校验通过。
- 交付物：`build/JobMailDesk-Core-v0.6.0-macos-arm64-fixed-v17.zip`（35 MB）。
- SHA-256：`65c33ce6880403c7dde9bdc5651eae807d726dc07be7621e03a9ed672fd3cc98`。

## 15. macOS 全面交互与数据完整性约束

- 主事实源是 `ApplicationRegistry`、任务文件和 unresolved；公司卡、台账、
  Obsidian、dashboard cache、Calendar 与活动未读均为派生结果。零任务申请不得
  从任何申请投影消失。
- 邮件结果必须区分 fetch/parse/noncandidate/pending/resolved/ignored/tombstoned。
  parser 升级与人工重试只允许重放尚未形成事实的结果；事实状态不可降级。
- 主 source identity 是账号指纹、mailbox、UIDVALIDITY 与 UID。Message-ID 只能
  辅助，不可作为唯一主键；缺少 UIDVALIDITY 时应停止而非猜测。
- 公司 exact alias/dictionary 是高层证据；仅允许一次受审核招聘后缀归一化。
  不得通过包含、编辑距离或相似度合并公司。岗位、项目、地点、job code、
  recruiting year、business unit 与 attempt 必须分字段判断。
- 所有业务写入口共享跨进程数据锁。多文件确认与合并使用可恢复事务；应用和
  任务编辑使用 revision，合并确认绑定 preview token。
- 已结束/归档申请必须暂停关联任务的待办、提醒、日历和导出投影；明确重新激活
  要写 lifecycle 证据。ignored、deleted、tombstoned 永远优先于邮件重放。
- 申请合并不得丢失 progress nodes、manual history、legacy IDs、aliases、
  source tombstones 或 task tombstones；永久删除后同一来源不得复活。
- 每封待处理邮件使用独立原生复核窗口和窗口私有 session/revision。旧链已结束
  时默认创建新 attempt，更新旧链与明确重新激活是两个不同操作。
- 主窗口刷新不得覆盖 dirty quick edit；扫描不得抢页签。异步选择、merge、
  unread ACK、Calendar、设置与 capsule 均需 generation/token 防止过期回调。
- 未读事件只保存安全元数据；progress ACK 使用稳定 company key，Dock badge
  使用唯一事件数而不是各页签计数之和。
- 进展与 Obsidian 文件缺少受管标记时 fail closed。外部复选框必须先导入，再
  执行业务写入和导出；dashboard cache 只接受同一输入签名下的完整快照。
- 事实提交后的导出、日历、提醒和活动写入使用可幂等重试的 derived outbox。
  Calendar 同名冲突必须报错，不能静默写到任意日历。
- 无 Git 提交的 macOS 候选包必须记录 exact source revision、依赖锁、资源哈希、
  架构、签名与 checksum。自动测试不能替代原生窗口、TCC、VoiceOver、真实
  IMAP flag canary、双架构与至少 72 小时试运行。

## 16. 2026-08-15 全面整改 macOS RC1 基线

- 版本提高到 `0.6.1rc1`，不创建 Git commit；包内记录 exact source revision
  `b4f593cbdc2ca2de0dbb4e6f29e612ad7e000d51fa3dbc86fdf8d0c52fa7f41e`，
  并单独生成源码资源 SHA-256 清单。
- 完整 pytest：304 项，303 passed、1 个 Windows-only skip；Python compileall、
  JavaScriptCore、IDE lints 与 whitespace 检查通过。
- py2app 内置 imports、江波龙/翱捷 dictionary-backed parser 和主/复核窗口脚本
  smoke 通过；包内含六个主/复核 HTML/CSS/JS 资源。
- 构建后移除所有 `__pycache__`、本机绝对路径、pytest/setuptools 包和非 Cocoa
  pywebview 平台；archive privacy/dependency 扫描通过。
- 解包后 ad-hoc 深度签名、图标、Bundle/可执行文件名称、source revision、
  arm64 架构及严格签名均通过。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc1-macos-arm64-fixed-v18.zip`
  （34,750,166 bytes）。
- SHA-256：`911be27245be6d6a2779931077153e21cbd1bbe8b01802fd331eb68628757f49`。
- RC1 构建时执行沙箱不能替换 `/Applications/JobMailDesk.app`，随后由用户手动
  安装。仍需执行原生窗口、TCC、VoiceOver、真实账号 canary、Intel 构建与
  24–72 小时试运行，因此候选包不称 release-ready。

## 17. 翱捷 parser-version 重放根因与 RC2

- 现场状态确认 UID `1709047101`（11:14:52）与 `1709047102`（11:15:12）均为
  `noncandidate | 2026.08.15.2 | retry_eligible=1`；13:47 的 RC1 扫描
  `fetched=48, skipped=48`，说明正确解析规则没有执行。
- 根因不是翱捷公司正则再次失败，而是规则修复后仍沿用 parser version `.2`。
  `StateStore.should_process` 正确禁止同版本重复解析，因此旧 noncandidate 被保留。
- parser version 提高到 `2026.08.15.3`。新增生产回归：从 `.2` 的翱捷
  noncandidate 记录升级扫描后，必须生成 pending review，并识别
  “翱捷科技股份有限公司｜数字后端工程师”；不得创建业务事实。
- 完整 pytest：305 项，304 passed、1 个 Windows-only skip；包内 RC2 解析、
  JavaScript、隐私依赖边界、ad-hoc 深度签名与解包后严格签名通过。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc2-macos-arm64-fixed-v19.zip`
  （34,750,675 bytes）。
- source revision：
  `8d0ae53154a3e020fb9e87778dd0a32a5c246f8f63732af9a4ce4a503e1e3514`。
- SHA-256：`50efc0c8122fbc0b45fb786ae5e660b71e4e5a0a7cdce0e3445d87eb62d9ff74`。

## 18. 2026-08-15 真实邮箱全量审计与 RC4

- 使用只读 `BODY.PEEK` 对最近 30 天全部 48 封邮件执行结构化影子审计；最终
  结果为搜索 48、读取 48、安全重试 0、MIME/解析失败 0、硬冲突 0。
- 修复真实 MIME 与直接 `MailRecord` golden 的差异：HTML 相邻节点、危险标签、
  空白 plain fallback、message/rfc822 附件、未知字符集、链接上下文、
  FETCH UID/SIZE、超大邮件有界前缀和逐 UID parser replay debt。
- 修复验证码、面试问卷、招聘空宣、安全提醒误报；修复翱捷、字节跳动、
  中兴微电子、小米、芯动、小鹏、沐曦、兆易、DJI、理想汽车等真实模板的
  公司、岗位、地点、批次、轮次、阶段和时间字段。
- 语义重复邮件在审计中标记来源，确认后合并为同一 progress node 并保留全部
  source hashes；缺少岗位/job code 的下游事件不得自动创建申请。
- parser version 为 `2026.08.15.5`；完整 pytest 367 项，366 passed、
  1 个 Windows-only skip；compileall、JavaScriptCore 和 IDE lints 通过。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc4-macos-arm64-fixed-v21.zip`
  （34,769,606 bytes）。
- source revision：
  `6b05a40d94eb3da295ad6863ec5c4b844a304aca3a3fb8ed14542634daf51ccb`。
- SHA-256：`d153991fe9085b2ef20c81a1a2ff073a7c9601d44bab4f7f0e208a79537598d1`。
- ZIP 解包后通过 arm64、图标、资源、隐私依赖边界、ad-hoc 深度签名与严格
  签名校验。安装后原生窗口/TCC/VoiceOver 与长期试运行仍属 RC 闸门。

## 19. 2026-08-18 海光信息复核目标修复与 RC5

- 真实邮件“海光信息招聘｜2027校招-物理设计与实现工程师｜在线笔试”未能命中
  已手工建立的“海光信息｜数字后端”申请链。根因同时包含公司别名缺失、岗位
  cohort 前缀未清理，以及复核搜索默认要求公司和岗位 token 全部命中。
- 新增审核别名“海光信息招聘/海光 → 海光信息”，岗位归一为
  “物理设计与实现工程师”，招聘项目为 2027校园招聘，并解析收到邮件七日内
  的截止时间。
- 复核目标初始搜索改为公司-only；岗位名不同仍显示同公司已有链，选择后可人工
  更新 active 链或创建新 attempt。后端搜索同样使用审核词典归一公司别名。
- parser version 为 `2026.08.18.1`；完整 pytest 368 项，367 passed、
  1 个 Windows-only skip；包内海光邮件与目标搜索 smoke 通过。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc5-macos-arm64-fixed-v22.zip`
  （34,768,845 bytes）。
- source revision：
  `8ded59f48d8477406d5a6a8db12f22e37b75cc8648b7dcbec0901558bd72a5f7`。
- SHA-256：`c7e02e8100f3c0ad1a0fe43bb1dce01035328f5c3728819b54b8d48d71e3f6ad`。

## 20. 2026-08-25 退出卡死根因与有界退出

- 现象：点击退出后窗口先转圈无响应，进程长时间不结束，只能强制退出。真实
  日志最后一行停在 `Scheduler has been shut down`，之后再无输出。
- 复现方法：`build/exit-probe/probe.py` 用隔离的 `JOBMAILDESK_LOCAL_ROOT`
  启动 UI，在窗口显示后由 `AppHelper.callAfter` 调用真实的
  `NSWindow.performClose_`，走完整关闭路径；守护线程按 `sys._current_frames()`
  打印线程栈，外部 `sample` 在 Python 收尾阶段抓取原生栈。
- 根因有三层，互相叠加：
  1. pywebview 的 `closing` 事件是 `Event(self, True)`，回调在 AppKit 主线程
     同步执行。原 `shutdown_runtime` 在该回调里做阻塞工作，界面必然转圈。
  2. 该回调里的三件事都可能长时间阻塞：`BackgroundScheduler.shutdown` 无论
     `wait=False` 都会 join 调度线程；`Window.destroy` 带 `@_shown_call`，每个
     复核窗口最多等 20 秒 `shown`；`runtime_control.stop()` 会就地关闭 IMAP
     socket。
  3. 即使窗口关掉，进程仍可能不退出：pywebview 为每个非锁定事件创建
     **非守护**线程，且 `concurrent.futures` 的 `_python_exit` 在解释器收尾时
     无条件 join 线程池 worker。对正在运行的应用采样可见 2 个空闲 worker，
     它们只要卡在 `osascript` 或 TLS 关闭上就会拖死退出。
- 另有一个数据风险：`closing` 在 Cmd+Q 时也会触发，但脏的复核窗口可以取消
  退出。旧实现已经先停掉调度器并强制关闭复核窗口，等于丢掉用户选择保留的
  未保存修改。
- 修复：新增 `ShutdownCoordinator`。`request()` 立即返回，只做
  `RuntimeControl.begin_stop()`；清理在工作线程执行；看门狗先等清理，再等
  `scan_lock` 空出（即事实写入到达事务边界），随后 flush 日志并
  `os._exit(0)`。所有本地存储都走 `_atomic_write` 或可回滚的
  `FileTransaction`，因此有界强杀不会留下半写数据。
- 退出改绑 `closed` 而非 `closing`，被取消的退出不再拆掉运行时。
- 验证：完整 pytest 375 项通过（1 个 Windows-only skip）；探测脚本正常关闭
  约 1.5 秒退出；注入“`close_all` 挂死 300 秒”后仍在约 6 秒退出，即
  `drain_timeout` 上限。
- 包内验证：`build/exit-probe/in-bundle-smoke.py` 用 bundle 自带解释器复跑
  “清理挂死仍按时退出”，并断言包内 `ui_app.py` 绑定的是 `closed`；ZIP 解包后
  ad-hoc 深度签名严格校验通过。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc6-macos-arm64-fixed-v23.zip`
  （34,777,889 bytes）。
- source revision：
  `959335035efead03dd00c1e5edb815d77fbc4dce167175ec90a616d3ba19155c`。
- SHA-256：`fe3964aa7dedce70ea5a0fd13dc1d9c999c5e3a6e9601424afabf62eaa017391`。
- 安装后真机点击退出、TCC 与长期试运行仍属 RC 闸门。

## 21. 2026-08-25 退出中断扫描的 IMAP 会话撕裂

- 真机日志（RC5，21:36:30 点退出）给出了第 20 节缺失的一手证据：退出把
  `client.shutdown()` 打在正在 `uid FETCH` 的扫描上，标准库先抛
  `ValueError: PyMemoryView_FromBuffer(): info->buf must not be NULL`，
  随后 `with client:` 的 `__exit__` 调 `logout()`，在死掉的 socket 上得到
  `OSError: [Errno 9]`，被包装成 `imaplib.IMAP4.abort` 抛出。
- CPython 3.12 的 `IMAP4.__exit__` 只吞 `OSError`，不吞 `IMAP4.abort`；而
  `logout()` 在 `_simple_command` 失败时**不会**执行 `self.shutdown()`。因此
  这次中断同时造成两个后果：取消被伪装成“扫描失败”一路抛到 pywebview JS
  桥（用户会看到报错），并且 socket 描述符没有释放。
- 修复：`_connected_client` 不再依赖 `with client:`。围绕 yield 捕获
  `(IMAP4.error, OSError, ValueError)`，若运行时正在停止则还原为
  `RuntimeStopping`，否则原样上抛；`_close_session` 在 finally 里静默 logout，
  失败时补一次 `shutdown()` 释放描述符，并且自身异常绝不覆盖调用方的结果。
- 回归：`test_quitting_mid_fetch_cancels_instead_of_raising_a_socket_abort`
  按真机异常序列构造替身，断言得到 `RuntimeStopping`、logout 调用一次、
  shutdown 调用两次（中断 + 补救）；
  `test_a_real_imap_failure_is_not_disguised_as_cancellation` 守住“真实故障
  不得被伪装成取消”。完整 pytest 377 项通过（1 个 Windows-only skip）。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc7-macos-arm64-fixed-v24.zip`
  （34,779,462 bytes）。
- source revision：
  `5cc2e3fbc60a750668963ff0f12d272a1508be728cc791810738551a67f1a828`。
- SHA-256：`2dc48c1340ad65d6069e732069834cfd711ac72d42bc6272a06a43d9b5f2faa7`。
- 打包遗留导致 Spotlight 出现两个同名应用：`dist/JobMailDesk.app` 没有在构建
  结束时删除，被一并索引。`build/exit-probe/build-and-install.sh` 现在用
  `trap ... EXIT` 清理构建树、只保留最新安装包，并在收尾核对
  `mdfind -name JobMailDesk.app` 只剩一条；该约束已写入 PROJECT_RULES 发布节。

## 22. 2026-09-12 日程训练集与识别整改

- 起因：壁仞科技、中兴微两封面试邀请建出的待办没有时间，日历与三日内重点
  因此都不提醒。
- 一手证据：只读拉取近 120 天全部 81 封邮件逐封核对。这两封的正文只剩 3 个
  字加链接——**面试时间从未进入解析器**。
- 根因一（正文提取）：`_message_contents` 只要 `text/plain` 非空就采信它。
  MokaHR 系 ATS 发的 multipart/alternative 里，plain 半边只有「新面试」三个
  字的占位内容，时间表格只在 HTML 半边。改为比较两半去除 URL 后的信息量，
  plain 不足 HTML 一半即视为占位副本。全量语料命中 5 封真实招聘邮件。
- 根因二（时间正则）：`--` 双短横线区间不识别；带秒时间戳把后续「失效」挤出
  上下文窗口；相对截止只认「收到邮件/通知」加「请在/须在/务必在」。补齐
  `[-–—]{1,2}` 区间、可选秒、`收到本链接`、`请于/请您于`、`将于N小时后失效`、
  `N个工作日内`（按自然日计，用户确认不跳周末）。
- 根因三（公司名）：发件人显示名常是 HR 个人姓名或 ATS 产品名。改为
  `find_company_mention` 扫描标题与正文里的已审核公司标签，并按「常见姓氏
  开头的 2–3 字且不含企业实体词」判定人名；词典命中优先于形状判断，
  `小鹏汽车` 这类短公司名不受影响。公司词典补 10 家（寒武纪、曦望、星宸、
  芯海、圣邦微、硅谷数模、芯源系统、中芯国际、合肥君正等），并把重复的
  local-company 条目合并、保留原 id 以免影响既有申请链。
- 宣讲会：线下（有具体场地）进入待处理，线上直播空宣继续排除，既有
  `alibaba-campus-livestream` 非候选样本不受影响。
- 训练集：`tests/golden/schedule_vectors.json`，49 条脱敏真实邮件，保留发件
  域名（公司信号）但抹去个人姓名、手机号、邮箱本地部分与链接令牌，每条带
  `note` 说明判定依据。`tests/test_schedule_vectors.py` 一次性报出全部偏差。
- 结果：训练集从 19 处偏差降到 0（49/49 命中公司与三个时间字段）；全量
  pytest 379 项通过；真实邮箱 72 个会话复扫，33 个改善、0 个变差、无新增
  company=None（唯一 None 是 Postmaster 退信，本就不该归属）。
- parser version 提升到 `2026.09.12.1` 以触发 30 天窗口内的重放；已产生事实
  的邮件不会重放，需单独回填。
- 待办编辑器合并为单一「硬截止」：日期用 date 选择器保留日历弹窗，时间改为
  可自由输入的文本框，接受 `1400`/`14:00`/`14.00` 并在保存时归一。原生
  datetime-local 的分段输入在 WKWebView 上逐位跳变，是用户反馈的输入 bug。
  保存时显式清空 start_at/end_at，避免历史窗口值盖过刚填的时刻；模型字段保留
  以兼容旧数据，`critical_time` 对两者都成立。
- 回填必须经 `task-update` 唯一写入口，使用稳定任务 ID 原位修复时间。
  已产生事实的邮件不参与 parser 重放。本公开记录不保留用户的任务编号、
  公司与具体面试日程对应关系。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc8-macos-arm64-fixed-v25.zip`
  （34,783,175 bytes）。
- source revision：
  `95124717ac9ea44d9bcf142213207471a585e4c8a46f0ce708b0336113090659`。
- SHA-256：`9661faff0ddeb0bd318c64612148cd17d72c3d6ce2b2201dfc40d15aed1e4e0f`。

## 23. 2026-09-12 保存待办卡顿与待处理库读取

- 保存待办要等几十秒：`_refresh_task_runtime` 在 JS 桥调用内同步执行
  `sync_macos_calendar(store.all())`，把全部 70+ 条待办逐条推给日历，每条一次
  osascript；首次还阻塞在 TCC 授权弹窗。改为后台线程 + 请求合并，退出时跳过。
- 待处理库整体读不出来：`UnresolvedStore.all()` 用 `content.split("---")` 取
  frontmatter，而海光宣讲会邮件页脚的 `----------------` 被收进 requirements，
  quoted scalar 被从中间截断。改为只在整行恰为 `---` 处切分；本地 104 条恢复。
- 待办编辑器时间比卡片少 8 小时：预填用字符串截取而非本地时间换算。
- 时分拆成两个输入框（点小时打 16、自动跳到分钟打 00，失焦补零），
  待办编辑器、复核窗口、归属弹窗三处统一，界面内已无 datetime-local。
- 交付物：`build/JobMailDesk-Core-v0.6.1-rc10-macos-arm64-fixed-v27.zip`
  （34,784,739 bytes）。
- source revision：
  `2b8bc70b20373647a518e547960e70612673128f58f668617b4661b23147e8a7`。
- SHA-256：`8ebbb1f2a3dd4302d98074b02276b5dbe9e1582c3944698bcc37144ce63b1fc4`。
- 真机验证：点退出后进程 6 秒内结束，未触发卡死采样。


## 2026-09-20 本地版扫描进度及更新入口移除

- RuntimeControl 持有仅内存中的 ScanProgress；扫描入口统一处理完成、部分失败、错误和取消。进度桥接不获取扫描锁，前端独立轮询，不因设置弹窗或未保存卡片而停止。
- 读取阶段计入失败及窗口外被跳过的尝试；识别阶段以实际进入解析循环的记录为分母，不能当作整个任务的百分比。
- 按用户要求删除 updates 模块及其不再适用的旧测试，增加旧开关无法启用、无前端/桥接入口的验证。保留历史源码和第三方许可归属；不进行上游发布。
- sync-ledger 命令保留兼容；界面名称改为“导入台账修改”，明确这是本地 Markdown 导入。

## 2026-09-20 公司与岗位漏识别修复

- 本地待确认元数据表明，两类标题已带公司全称但旧规则没有识别：请参加某公司的某岗位在线考试；带括号地点的公司全称-线上笔试通知。正文的简历已通过我司“岗位-城市岗”筛选也未命中旧岗位规则。
- 为明确邀请句式与合法公司全称增加独立候选，拆分岗位中的城市岗后缀；仅在完整合法公司名称后移除招聘标签，不批量重命名申请。
- “来自”句式必须沿用既有规则，不能把“来自”吃进公司名；“AI／的 AI”、系统发件名和“线上”等泛称不能成为身份。引用岗位与正文岗位标签冲突时继续待确认。
- parser 版本改为 2026.09.20.1。增加虚构 golden 样例与升级重放测试，验证 pending 原位补全、ignored/resolved 保留、重复扫描不复制卡片。没有连接真实邮箱或读取授权码。
- 2026-09-20 待处理工具：确认原因由结构化 reason/source 元数据生成，不保存原邮件正文。单次补扫显式传入范围并允许窗口内 pending 重识别，不能只扩大 IMAP 下载窗口；人工终态优先。
- 手动恢复通过 ignored→pending 的显式服务入口，提升 revision，清理旧 ID 和定位符 ID 对应的 ignored 索引；遇 resolved/tombstoned 索引拒绝恢复。先更新可重建索引，再保存 Markdown；保存失败时 ignored 事实仍优先，下次扫描修正索引。manual_restore 标记防止后续自动过滤或重放覆盖，且不制造永久的解析升级重放债务。
- 新工具栏放在卡片滚动区内，避免改变主窗口 grid 行分配；跨页签复用同一 DOM 节点保留事件绑定。

## 2026-09-20 本仓库 Windows 更新

- 用户重新授权添加 GitHub 更新；独立 github_updates 开关默认关闭，历史上游开关仍无效。只读取 WizLiang/jobmaildesk 公开 Release，不需要客户端 GitHub 凭据。
- RC 与正式通道分别筛选版本，资产必须是匹配版本的 Windows ZIP 和 SHA-256。检查、下载在后台执行；说明用 textContent 展示。
- Windows ZipInfo 会规范化反斜杠，路径检查必须使用 orig_filename。拒绝穿越、设备名、大小写重复、链接和超限解压。
- 先校验、解压并自检，再等待旧进程退出后替换同级目录。Directory.Move 避免把目录意外嵌套到已有目标；失败尝试回退，保留备份。
- 与清除个人信息、迁移数据操作互斥；程序与数据重叠或有额外文件夹时拒绝自动替换。原生 helper 测试实际执行文件移动，进程启动使用替身。
