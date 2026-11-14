# 架构与接口

## 模块与数据

| 模块 | 职责 |
| --- | --- |
| `index.html` | 中文单页面工作台、成员与权限表单、消息/状态/用量刷新、主题和浏览器 Mock |
| `server.py` HTTP 层 | 绑定 `127.0.0.1`，提供首页及 `/api/*`，默认端口 8765 |
| 群与成员逻辑 | 校验成员、生成 profile、启停 terminal、处理提及与接力 |
| CAO 适配与轮询 | 标准库 HTTP 请求、可选 Bearer token、每 2 秒轮询 terminal 状态和完成输出 |
| 持久化与共享日志 | 群和消息写入数据目录的 `groups.json`；先写临时文件再替换；工作目录内追加 `GROUP_LOG.md` |
| 用量解析 | 读取本机 Claude Code/Codex CLI JSONL，按成员标记认领并估算费用；可选读取 Claude 状态栏过滤快照 |
| `claude_statusline.py` | 标准库静默 stdin 采集器，只原子保存额度窗口及更新时间；项目本地 statusLine 调用 |
| `tests/test_claude_limits.py` | 19 项脚本、项目设置所有权/回滚、生命周期容错、额度缓存及真实输入框字节回归 |
| `tests/test_server.py` | 74 个标准库 unittest：子进程导入隔离、临时 HOME/数据/日志、模拟 CAO 的群聊行为、输出清洗、effort/profile 和 Codex 完成判定 |
| `tests/test_permission_mode.py`、`tests/test_autonomy.py` | 6 项权限模式和 22 项自主运行隔离回归，含真实线程及注入时钟 |
| `tests/fake_cao.py` | 可导入、可选随机端口的 HTTP 模拟服务，按查询推进状态代际，input 默认清空 raw FIFO，可显式追加，支持同步快速完成及 idle_stream/advance 分阶段输出 7501，Codex last/full 回复均模拟 bullet/两空格续行；不包含验收断言 |
| `tests/test_integration.py` | 31 个真实子进程 HTTP 集成测试：入群、收发接力、残留状态、恢复、effort 编辑/重启的 profile 安装以及 Codex idle 流式回复/接力 |
| `tests/test_frontend.py`、`tests/frontend_effort.js`、`tests/frontend_drawers.js`、`tests/frontend_limits.js`（共用 `frontend_dom.js`） | 可选 Node VM 的 16 个前端行为回归，执行页面真实内联脚本；无 npm 包，不替代浏览器排版验收 |

默认数据目录为 `~/.cao-groupchat`，可用 `GROUPCHAT_DATA` 覆盖。profile 安装降级文件在数据目录的 `profiles/`，自定义价格在 `prices.json`。以上均为本机运行数据；共享日志保持 Git 忽略。消息和日志可能含正文与私人路径，当前并未实现自动脱敏。

## 前端与交互约定

- 前端颜色全部走 `:root` 设计令牌；主题支持浅色、深色与跟随系统，选择存入 localStorage 的 `theme` 键。
- 成员 `effort` 按 provider 校验并持久化；表单提供 CLI 默认及指定选项，切换 provider 清除不兼容值；卡片显示配置或 CLI 默认，Mock 使用相同选择与校验语义。
- 对话区采用 Telegram 式气泡；同一人 5 分钟内连续发言合并为一组，系统消息和日期分隔单独显示。
- 成员 `plan` 决定用量展示：订阅计划显示可取得的账号额度与本群 token（可附 API 价折算，订阅不另收费）；`api` 显示估算金额与 token，未知价格提示补充；未选计划时提供可取得的额度与费用参考。
- 选目录通过 `osascript` 打开 macOS 原生对话框；非 macOS 或缺少该命令、原生选择失败时，前端退回页面内目录浏览。用户取消时不打开降级浏览器。

窄窗口保留 1100px/760px 边界：小于 1100px 右侧抽屉、小于 760px 左侧也为抽屉，375px 顶栏入口常驻。S.drawer 只允许一侧展开，控制遮罩、ARIA、背景 inert 和焦点；Esc 优先关闭目录/设置窗口，随后关闭抽屉。两个断点的 matchMedia change 与 resize 兜底共用同步路径，恢复常驻布局时清理遮罩和禁用状态，选群成功关闭左侧；头部动态内容与入口分开，轮询不销毁入口。单文件前端使用现有颜色令牌；布局/无横向滚动由群主浏览器验收，Node 只验证行为。过期日志响应校验当前群/页签后才写入视图。

## 按群自主运行控制

`GET/PUT /api/groups/{id}/automation` 读取设置及运行摘要，PUT 为部分更新。缺省 `relay_limit=6`、`delivery_budget=null`、`idle_minutes=0`、`reminder_cooldown_minutes=5`、`reminder_limit=3`、`coordinator_id=null`、`claude_limits=false`；GET 不激活旧群配置。数值拒绝 bool、负数及非有限值，协调 ID 必须属于当前群；空对象无操作，相同配置 PUT 保持空闲观察期。未提供界面和浏览器 Mock API。

明确配置后，`automation` 保存设置，`automation_state` 保存 deliveries_used、budget_notified、reminders_sent、last_reminder_at、idle_since、awaiting_owner。预算是累计上限，不是剩余额度；成员广播、静默通知、自动提醒、人工重发都经同一 input 路径。各成员 FIFO 仍可并发，跨成员不保证全局顺序。超额在原 outbox 中保留，delivery_state=budget_paused，不设置 pending；增额/null 后下一轮自动继续，启动等待时钟从预算暂停恢复处续开，不把暂停时间算成启动超时。

发送持成员投递锁；原始输出基线等网络读取在全局锁外。input 前持全局锁检查剩余、预留一个名额，与 dispatching 一起持久化，再释放全局锁调用 CAO。多个成员不能同时抢最后一个名额。发送前失败及明确拒绝返还；可能已送达的异常保留计数，人工重试按新尝试计数；崩溃留下的预留保守保留，沿用 uncertain 恢复。预算到达上限或首次阻塞时仅一条系统提示，加额恢复可开启下一次耗尽提示。没有 CAO/本地文件联合事务，不承诺恰好一次。

工作状态与 CLI 状态分开：每次非静默完成回复解析最后非空行的 `状态：进行中|等对方|等群主|阻塞|已交付`，每行只能取其中一个值，允许 0–3 个前导空格；无合法标记回到进行中。先去 ANSI/真实结束行并保留 CAO 返回文本中仍存在的缩进；若首个非空行带 Codex `• `，仅对状态识别移除该首行前缀和其后每行两空格显示边距，保留相对 Markdown 缩进，再解析。聊天正文仍按原逻辑显示；围栏代码、引用符、列表及正文提及不认，显示正文保持原清洗结果。这个缩进保护不能还原上游丢失的信息：真实 CAO Claude 的 extract_last_message_from_script 对每行 strip，last 模式中原缩进代码的状态行仍可能被认成标记；full/其他模式只有保留了缩进时才可排除，围栏代码不受此限制。静默入群确认不覆盖工作状态。

群级等待与成员 work_state 分开：配置过自主运行的群中，非静默回复提及 @群主（沿用提及的后缀边界）或因 relay_limit 截断成员转发，在保存该回复时同步置 awaiting_owner=true、idle_since=null；静默通知不触发，@群主助手不匹配。该标志阻止提醒把接力从 0 跳重新启动，其他成员回复/设置变更不清除，重启也保留。只有非空、有效的群主 post_message 清除，并与群主消息同次保存；随后仍须满足原成员工作状态和连续空闲条件。

轮询所有成员后按群检查提醒：必须无 active_delivery/pending/outbox，CLI 不忙、不等待用户输入、状态已知且观察正常；任一成员等群主/阻塞/已交付则重置连续空闲观察。目标优先配置的在线就绪成员，否则首个可用成员，无可用者不提醒。先在全局锁选目标，释放后获取该成员锁，再在全局锁重验，原子登记提醒计数、系统消息及队列；释放全局锁后才尝试 input。固定标识 `[自动提醒 idle:<群ID>:<累计序号>]`，不冒充群主；间隔、正数冷却、总次数及预算同时约束，无额度不生成提醒。

设置改动和重启后重新观察持续空闲，累计预算/提醒次数/冷却及待发队列保留；相同 PUT 不改变观察期。任一待投递/在途任务，包括预算暂停和 uncertain，都禁止空闲提醒。单成员轮询和单群提醒错误分别兜底，不中止其他群。配置仅影响本实例经 GUI 发起的 input，不限制 CLI 自己的内部工具或外部直接 CAO 输入；不能替代只读角色的实际授权和隔离。

## 数据流

1. 浏览器经 `/api/groups` 创建群，提交工作目录与成员配置；后端保存群并异步启动成员。
2. 每位成员生成独立 agent profile；非空 effort 对 Codex 写入 `codexConfig.model_reasoning_effort`，对 Claude Code 写入 `claudeConfig.effort`，由 CAO 转为对应 CLI 配置；缺字段或空值不写配置。一个群对应一个 CAO session，一位成员对应一个 terminal。权限与职责写入 profile，实际执行由 provider 决定。
3. 群主消息经 `/api/groups/{id}/messages` 进入后端。匹配成员提及则定向发送；无匹配提及或广播时覆盖所有成员。每位成员有持久化 outbox 和 active_delivery，以及进程内的投递锁；未取得 terminal 或未证明初次就绪前不 POST input，按成员队列顺序处理。自动就绪须 idle/completed、provider 正向提示特征及两次至少间隔 READY_SECONDS 的稳定观察（Claude 输入框可为空或带 Try 占位文字，先清除 ANSI）；terminal 更换或手动重试重新检查。另有按群的启动锁，覆盖会话查询/创建/加入直到发布 terminal_id；profile 安装在锁外，不同群不互相等待。读取原生 session.status：会话存活就加入，即使全体离线；404 或 terminated 才创建。GET 200 可仅为失败记录，并不证明后端存活。精确同名重复 400 转加入，加入 404 后最多直接创建一次，异常释放群锁。
4. 其他 provider 将 input 响应的 `dispatch_boundary` 记为边界，轮询按 `status_generation` 判断；字段缺失时仍降级为 busy 或发送后等待时间。Codex 原生 OSC 标记为 state=working/done:app=codex，以 ST 结束；解析兼容 BEL 和无 app 旧格式，未知/拆分末状态保留。状态与 full 起点共用同一解析规则。Codex 发送前读取 raw full 标记基线，排除旧轮 done；只有本轮最后一个 OSC 7501 为 done 才读取最终回复，无标记、working 或探测失败继续等待，不回退相信 idle。发送前只存长度/哈希和标记元数据；前缀改变或缩短时将有效边界归零，同一轮首次读取的 working+done 可完成。相同字节的短回复结合新 output_generation 确认新输出；若 raw 已重置、发送时非 working 且 output_generation > dispatch_boundary，首次轮询只剩 done 也能完成（working 可能已被 32KB 重绘挤出）；发送时旧轮 working 的保护仍要求本轮 working 证据，并保持发送时原状态不变。显式追加兼容仍以原前缀及标记数区分旧轮，full 降级使用当前轮起点；原等待确认/错误路径保留。
5. 读取并清洗回复：去除 ANSI/OSC 控制序列，末尾整行匹配 Claude `✻ <单个字母词> for <时长> · done <时:分> AM/PM` 或 Codex `Worked for <时长> • <时:分>` 格式时去除；Claude 动词支持带重音字母，不写死具体词；引用、代码和非尾部示例保留。同轮回复只处理一次，成功发送下一轮时重置标记；静默通知回复直接丢弃。收下回复时即标 delivery_started=True，即使首次轮询前已完成、未见中间 busy/working 状态。普通回复写入群消息，行首非空 `[日志]` 仅在同一行代写；成员提及按最长已知名字解析，最多接力 6 跳。
6. 启动等待超时保留队列并暂停投递；从 not_ready 人工重试时，在用户确认 CLI 可输入后允许仅凭稳定 idle/completed 状态投递，仍需两次观察、最小时长，busy 等状态重置候选。其他自动投递及未开启此兜底的 uncertain/failed 重试仍走原特征检查。已投递后记录本轮工作证据（Codex working；其他 provider 的 fresh busy/等待确认），只有实际新输出刷新无进展时钟，重复状态不续期；超时标 uncertain、系统提示“可能未送达”，仍轮询迟到完成，不自动重发。input 网络响应丢失也属 uncertain：Codex 保留发送前基线，其他 provider 未获 dispatch 确认须先有工作证据，不复用旧完成。重试校验投递 ID/次数，同一成员锁内只接受一次，保留原文、静默属性和接力跳数，不重写群主消息。
7. 在 POST input 前持久化 dispatching；进程中断后加载为 uncertain，保留任务并允许人工恢复，避免重启自动重复投递。发送阶段捕获所有 Exception：input 前失败为 failed，可能已 POST 的失败为 uncertain；保留任务并提示。系统消息先在内存追加，写盘失败仅诊断错误类型、不抛出到轮询；成员轮询及批量保存独立兜底，一个成员失败不终止其他成员。轮询发现无正常结束的 dispatching 也转 uncertain。磁盘不可写期间无法保证状态持久化，仍须修复存储。群详情只公开 queued_count、delivery_id/attempt、can_retry 等摘要，不重复公开队列/在途原文。浏览器为 waiting_ready/uncertain 显示相应状态及恢复按钮，不一直显示思考中。
8. 浏览器获取增量消息、共享日志和成员用量。持久化可加载群记录，但真实 terminal 是否仍存活需后续轮询判断；任务 4 已通过模拟 CAO 验证后端重启后群、成员、会话标识和消息从数据文件恢复，恢复后的轮询不重复发消息；真实 provider 的存活和重连不在该验证范围。

## GUI 后端接口

| 接口 | 行为 |
| --- | --- |
| `GET /api/health` | CAO 连通性、provider 安装状态、角色与模型目录 |
| `GET/POST /api/groups` | 列群或创建群 |
| `GET/DELETE /api/groups/{id}` | 群详情及 `since` 增量消息，或解散群 |
| `POST /api/groups/{id}/messages` | 发送群主消息 |
| `POST /api/groups/{id}/members` | 邀请成员 |
| `PUT/DELETE /api/groups/{id}/members/{mid}` | 编辑并重启或移出成员 |
| `POST /api/groups/{id}/members/{mid}/restart` | 重启成员 |
| `POST /api/groups/{id}/members/{mid}/retry` | 人工重试原任务；body 为 delivery_id/ delivery_attempt，过期或重复请求 400 |
| `GET/PUT /api/groups/{id}/automation` | 读设置及累计摘要，部分更新本群自主运行设置 |
| `GET /api/groups/{id}/log`、`GET /api/groups/{id}/usage` | 共享日志、用量汇总 |
| `POST /api/pick-folder` | macOS 原生选目录；前端在失败时降级为页面浏览 |
| `GET /api/fs`、`POST /api/fs/mkdir` | 本机目录浏览和创建 |

## CAO 接口与兼容分支

默认 `CAO_URL=http://localhost:9889`；若配置 `CAO_TOKEN`，请求带 Bearer 鉴权。下表来自现有代码，兼容分支存在不等于所有分支均已实测。

| 操作 | CAO HTTP 调用或降级 |
| --- | --- |
| 连通性与 provider | `GET /health`、`GET /agents/providers` |
| 安装 profile | `POST /agents/profiles`，body 为 `{name, content}`；409 时 `PUT /agents/profiles/{name}`，body 仅 `{content}`；失败退回 `cao install <file> --provider <provider>` |
| 会话探测 | `GET /sessions/{name}`；读取嵌套 session.status；存活则加入，404/terminated 才创建；精确重复 400 表示外部创建竞态，转为加入 |
| 创建会话及首位成员 | `POST /sessions`；query 含 profile、provider、工作目录、工具、模型和 session_name；body 含 description |
| 后续成员 | `POST /sessions/{s}/terminals:operator`；404/405 时退回 `/sessions/{s}/terminals` 并带首个存活成员的 caller_id |
| 发消息 | `POST /terminals/{id}/input?message=...`，读取 dispatch_boundary |
| 状态与输出 | `GET /terminals/{id}`；`GET /terminals/{id}/output?mode=last`，422 时退回 mode=full 并截取新增内容 |
| 停止与解散 | `POST /terminals/{id}/exit`、`DELETE /terminals/{id}`、`DELETE /sessions/{s}` |

## 用量来源与限制

- Claude Code：`~/.claude/projects/<工作目录编码>/*.jsonl`，按 message.id 等标识去重并汇总 token；日志不提供订阅剩余额度。按群开启的状态栏另提供过滤后的额度快照，独立于 token 日志缺失。
- Codex：`~/.codex/sessions/**/rollout-*.jsonl`，取 token_count 中累计 token，结合工作目录与成员标记认领；rate_limits 取最近可用记录，属于账号共享额度，不属于单个成员。
- 日志用系统提示词中的 `项目群「群名」里的成员 **名字**` 标记认领，区分同一项目目录的不同成员；标记需出现在解析器读取的日志头部。
- 依据文件修改时间筛选候选日志，门槛为群创建时间减 60 秒；不等于逐条事件严格按时间过滤。日志格式和标记缺失可能导致无数据或归属不完整。
- 费用来自内置 `PRICES` 与本机 `prices.json`，仅为估算；未知价格返回空值，部分金额另列。内置价格与模型推荐不代表已核验的最新定价或可用性。

## Claude 状态栏额度采集

群主批准方案 B：不改 CAO；仅显式 `PUT /api/groups/{id}/automation` 中 `claude_limits=true` 的群配置项目本地状态栏，默认 false。已安装 CAO 的 claudeConfig 只转接 effort/fallback_model，未提供成员独立 `--settings` 透传；不使用全局设置或 CLAUDE_CONFIG_DIR。开关只改变后续启动的 CLI，已有成员须由群主重启；成员启动前再次检查/补齐本程序设置；可选配置失败时保留原文件，只发一条固定中文系统提示并继续创建终端，不绕过冲突覆盖。显式开启 API 仍严格返回 400；不能代替 Claude CLI 对用户设置自身的校验。

- 配置阶段持全局状态锁，严格检查 bool 和全部设置后读取 `<workdir>/.claude/settings.local.json`，要求合法 JSON 对象。只合并 statusLine；完整 `{type, command}` 与本群预期一致才属于本程序（命令用 shlex 引用当前 Python、仓库脚本和本群缓存路径）。用户状态栏存在时返回中文 400，不改文件、不停止终端。禁用只删仍归本程序所有的键，空对象删文件，外部替换的状态栏保留；解散已开启的群也尝试清理自有项。关闭/解散时遇到非自有、损坏、迁移后命令不符或写入异常，捕获后保留原文件、记录中文系统提示并返回 warnings，仍关闭开关或解散群；前端解散后展示此警告。警告不附设置内容、命令路径或任意异常文本。默认关闭的群重复关闭不读取项目设置。
- 写入采用同目录临时文件 + os.replace，已有文件权限保留；设置持久化失败会恢复 automation/runtime 和原文件字节。此回滚是进程内补偿，不保证跨进程编辑或崩溃的多文件事务。不开启采集时不创建 `.claude/`。拒绝 HOME/全局 Claude 目录、设置符号链接以及缓存落在全局 Claude 目录。已注册多个群共用目录时拒绝开启；已开启目录也不允许新群复用，防止未开启的群被间接配置。不能隔离人在同一项目另开的 Claude CLI，需专用实验目录。
- 命令以 `-I -B` 调用标准库采集器，限制输入 JSON 长度。只白名单 rate_limits 的 five_hour/seven_day/spend_limit，各窗口只留有限数值 used_percentage（0–100）和未来 resets_at；另存 updated_at。缺失窗口不继承上一快照；不保存 cwd、transcript 路径、正文或任何鉴权字段。异常全部吞掉且 stdout/stderr 为空；临时文件原子替换到 `<GROUPCHAT_DATA>/claude_limits/<群ID>.json`。不发送 `/usage`，不读取 OAuth/钥匙串，不更改用户或项目的忽略规则。
- 后端再次逐窗校验，已过 resets_at 的窗口不返回；开启但坏文件/不存在时 windows=[]、updated_at=null、spend_limit=null；未开启时成员 limits=null、limits_available=false，完全跳过缓存读取。有效快照更新时间不因窗口过期抹除。usage 的每个 Claude 成员共享本群最新 windows（300/10080 分钟，used_percent、resets_at、resets_in、stale=false）和 updated_at；spend_limit 独立返回，不画第三条额度条。额度反映登录账号，不能按成员拆分。关闭后不读取旧缓存，在途旧 CLI 仍可能写入过滤快照，须重启后才彻底停止采集。
- 前端仅对 limits_available=true 的 Claude 显示额度，未开启时保留原订阅 /usage 提示和 API 行，且不画额度条/更新时间（即使响应残留旧 windows）。开启后复用现有 qrow/qbar 及颜色令牌，固定 5 小时/7 天，缺失或过期显示“暂无数据”；显示“更新于 X 分钟前”，刷新签名包含当前分钟，空快照显示更新时间暂无数据。即使 usage=null 也显示窗口；原 API 费用/token 行保留。

兼容依据（2026-11-14，codex）：只读真实 Claude 屏幕得到 220 个 U+2500 横线包围的 `❯\xa0` 输入行（UTF-8 `e29dafc2a0`），已作为独立就绪单测。提取本机 CAO 纯正则及 extract_last_message_from_script，以该真实框加合成回复验证提取结果只剩回复正文；脚本进程测试确认不输出任何字节。未创建/重启真实成员，未给开发群开启；这不是新状态栏钩子的端到端联调结论，启用后就绪和回复仍待群主真实验证。

## 关键取舍与验证边界

- 标准库后端与单文件前端降低部署成本；保持无构建，不增加框架依赖。
- HTTP 轮询便于兼容和调试；回复有轮询延迟，未使用 WebSocket，部分 CLI 输出仍可能残留 TUI 字符。
- 角色和工具列表交由 CAO/provider 执行。前端对 Codex 等标软约束、Cursor 标不执行；这不是本轮验证过的安全隔离保证，`@builtin` 本身也不授予写文件权限。
- 状态临时文件替换降低部分写入风险，但不提供数据库事务或跨进程并发保证。成员启动现用每群进程内锁协调，不跨进程提供事务保证；同群/跨群/失败隔离已有线程并发回归，群主已在 9f59def 验证真实同群两成员并发重启；故意 CLI 失败再恢复因影响本机登录未做，仍待真实验证。
- 仅监听本机回环，无 GUI 登录鉴权，目录接口可访问本机目录，不适合公开部署。
- 2026-11-14 真实 cao-server 2.5.4 上 codex、claude_code 的建群、加成员、接力、日志代写已获群主确认。其他 provider 与 waiting_user_answer 交互待验证；任务 3 的隔离单测已通过，任务 4 已实现隔离 HTTP 集成验收，群主于 2026-11-14 完成人工验收，第 7 步窄窗口侧栏不可达，已延期，其余步骤通过；恢复后再收发的补充测试也已由群主确认结案。第二轮第 2 项已实现 Codex raw OSC 7501 完成判定并覆盖隔离回归，首版 `116851a` 因 input 清空时短回复卡住未通过审查，独立修复 `092021c` 已由群主确认审查通过。第二轮第 3 项增加就绪队列/无进展恢复及长 FIFO 回归，待独立审查；raw 提示特征与 CAO 渲染屏幕的差异、版本/权限菜单及两种 CLI 实际可输入时机均待真实联调，群主随后在 `aad5a16` 独立实例进行真实冷启动：Claude Code 7 秒内回复，Codex 实际 2 秒内完成但 GUI 漏收，第 3 项重开。32KB 边界修复 5b88949 仍未通过真实复测：6.6KB 内 W/D 都在，真正阻塞为原生 :app=codex 冒号后缀未被解析。原生格式修复 afaa1ab 经群主真实复测：Codex 7 秒、Claude Code 8 秒回复进群且无 TUI 残留，第 3 项已结案。第 4 项 9f59def 已真实并发重启通过并结案；故意故障恢复待验证。第 5 项 `0c3d33b` 审查通过，群主已完成 375/800/1100px 深浅色浏览器验收并将第二轮结案；媒体查询变化收尾修复待复审，实际 Tab 循环待验证，详见任务清单。
