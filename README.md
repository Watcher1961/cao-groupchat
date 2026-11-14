# CAO 项目群

为 [CLI Agent Orchestrator（CAO）](https://github.com/awslabs/cli-agent-orchestrator) 提供本机群聊式 GUI：群主建项目群，配置成员的 provider、模型、推理强度、角色、工具和职责，成员在群中回复并通过提及接力。

支持成员管理、共享日志、从本机 CLI 日志读取用量，以及浅色/深色主题。后端使用 Python 标准库；前端为单个 `index.html`，无需构建。界面文案为中文。窄窗口通过顶栏打开群列表或成员/日志抽屉，支持遮罩与 Esc 关闭；选群后左抽屉关闭。

## 本地运行

需要 Python、tmux、已安装并登录的 CAO 和所用 provider CLI。原有运行要求为 Python 3.9+；最低版本兼容性尚未专项验证。

```bash
uv tool install cli-agent-orchestrator  # 尚未安装 CAO 时使用
cao-server                             # 终端 1，默认 http://localhost:9889
```

另开终端，在项目目录运行（把占位路径换成你的项目目录）：

```bash
cd ~/projects/cao-groupchat
python3 server.py
```

浏览器打开 `http://localhost:8765`。GUI 仅监听 `127.0.0.1`，没有应用层登录鉴权，供本机使用。

直接打开 `index.html`，或前端初始化时无法访问后端，会进入演示模式；回复由浏览器模拟。仅 CAO 不可用、GUI 后端仍可用时会显示连接提示，不代表进入演示模式。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CAO_URL` | `http://localhost:9889` | CAO 服务地址 |
| `CAO_TOKEN` | 空 | 对接启用本地 token 鉴权的 CAO，由运行环境提供，不写入文档或日志 |
| `GROUPCHAT_PORT` | `8765` | GUI 后端端口 |
| `GROUPCHAT_DATA` | `~/.cao-groupchat` | 本机群与消息数据目录 |
| `GROUPCHAT_POLL_SECONDS` | `2` | 后台轮询间隔（秒） |
| `GROUPCHAT_READY_SECONDS` | `0.75` | CLI 正向就绪特征至少稳定的秒数，且须两次观察 |
| `GROUPCHAT_STARTUP_TIMEOUT` | `180` | 队列等待启动的秒数，超时保留消息并提供重试 |
| `GROUPCHAT_DELIVERY_TIMEOUT` | `120` | 已投递后无本轮进展的秒数，超时提示可能未送达 |

## 工作方式

- 一个群对应一个 CAO session，一位成员对应一个 terminal。角色、工具和职责写入生成的 profile，优先经 HTTP 安装，失败时退回 `cao install`。
- 群主提及已匹配成员时定向发送；无匹配提及或广播时投递给所有成员，尚未取得 terminal 或就绪证据的消息先排队保存。成员回复中的提及继续接力，默认上限 6 跳，可按群配置。
- 后台每 2 秒轮询 terminal；其他 provider 用 dispatch_boundary/status_generation 区分本轮状态。Codex 解析原始 `OSC 7501;state=…:app=codex`（ST 结束，兼容 BEL 和无 app 旧格式），使用本轮最后状态，仅 done 完成；没有新标记、仍 working 或输出读取失败继续等待，不相信中途 idle。发送前记录快照指纹和标记边界，识别 CAO input 清空；首次轮询中同时出现本轮 working/done 可完成；若 raw 已重置、发送时非 working 且输出代际前进，只剩 done 也可完成。发送时旧轮仍 working 则继续要求新 working，完成后才取最终回复。
- 邀请和编辑可选推理强度：Codex 为 low/medium/high/xhigh；Claude Code 另有 max；留空用 CLI 默认，其他 provider 不显示。配置随成员保存，卡片和演示模式同步；编辑后重启生成对应 provider 的 profile，群主表单/卡片抽查通过，真实 CLI 参数生效尚待联调；编辑日志记录强度值或 CLI 默认。
- 初次投递须观察 idle/completed 及稳定的 CLI 提示特征：Codex 页脚、Claude Code 空/带 `Try "…"` 占位提示的输入框或旧空提示符。当前 HTTP 只能取得 raw full，不能完全替代 CAO 的渲染屏幕检查；群主在 afaa1ab 真实冷启动复测通过：Codex 7 秒、Claude Code 8 秒回复进群且无 TUI 残留，第 3 项已结案，具体依据见任务清单。
- 消息按成员排队处理；投递无本轮进展时显示“可能未送达”，可在成员卡片重发，后台继续等待迟到回复。不会自动重发；人工重发可能重复执行，界面要求先核对终端。启动超时则显示“尚未投递”；人工确认 CLI 已加载且可输入后，可按稳定的 idle/completed 状态重试，即使输入框特征不匹配。自动启动不会退化为仅凭状态投递。四项秒数配置须为有限正数；测试独立缩短时限。
- 编辑成员配置会停止旧 terminal 并启动新 terminal，不保证保留旧 CLI 上下文。
- 共享日志位于群工作目录的 `GROUP_LOG.md`。有写权限的成员直接追加，无写权限者在行首使用 `[日志]` 由后端代写；该文件含本机运行信息，保持 Git 忽略。
- 工具权限最终由 CAO 和 provider 执行。界面会标注部分 provider 的软约束或不执行限制；这些标注不等于安全隔离已验证。

### 按群配置自主运行（仅后端 API）

默认群保持接力 6 跳、不限投递、不发空闲提醒；只读取 API 不会开启新功能。仅给另建的实验群配置，不要误用开发群 ID。直接打开 HTML 的演示模式不支持此 API。

`GET /api/groups/{id}/automation` 返回 `{settings, runtime}`，`PUT` 部分更新 settings；未知字段/非法类型返回中文 400，runtime 累计计数不可通过 PUT 清零。

| 配置键 | 默认 | 含义 |
| --- | --- | --- |
| relay_limit | 6 | 非负整数，0 禁止成员自动接力 |
| delivery_budget | null | 从首次配置起的累计投递上限；非负整数，0 暂停，null 不限 |
| idle_minutes | 0 | 持续空闲几分钟后提醒；0 关闭，支持有限小数 |
| reminder_cooldown_minutes | 5 | 两条自动提醒至少间隔的正数分钟数 |
| reminder_limit | 3 | 累计提醒次数上限，0 禁止；重启不清零 |
| coordinator_id | null | 本群协调成员 ID；未指定/成员离线时用首个在线就绪成员 |

以下仅为配置示例，先替换实验群 ID 和实际成员 ID；Python 标准库即可调用：

```python
import json
from urllib.request import Request, urlopen

url = "http://127.0.0.1:8765/api/groups/替换为实验群ID/automation"
with urlopen(url) as response:
    print(json.load(response))
settings = {"relay_limit": 30, "delivery_budget": 100, "idle_minutes": 3,
            "reminder_cooldown_minutes": 5, "reminder_limit": 3,
            "coordinator_id": None}  # 可改为 GET 群详情中的成员 id
req = Request(url, data=json.dumps(settings).encode(), method="PUT",
              headers={"Content-Type": "application/json"})
with urlopen(req) as response:
    print(json.load(response))
```

runtime 提供 deliveries_used、budget_exhausted、reminders_sent、last_reminder_at、idle_since、awaiting_owner。广播 N 人扣 N 次，静默通知、自动提醒与人工重发也扣；只有明确配置后的尝试计数，不追溯历史。额度在 input 前原子预留落盘；发送前失败/明确拒绝返还，响应丢失等可能送达的尝试保守扣减。进程崩溃后的未决预留也保留，避免重启绕过预算。

预算耗尽时系统提示一次，后续消息保留各成员 FIFO 队列；将 delivery_budget 提高到大于 deliveries_used（或设 null），下次轮询恢复。预算暂停不消耗启动超时；不同成员仍可并发，不保证跨成员全局顺序。实际更改设置会重开空闲观察窗口，但不清累计计数；同值重复 PUT 不重置观察期。

成员任务回复须在最后独立一行写一个标记，例如 `状态：等群主`；允许 0–3 个前导空格，并识别 Codex 的 `• ` 首行及两空格续行显示边距；可选进行中、等对方、等群主、阻塞、已交付。无标记视为进行中；引用符、正文提及和围栏代码不认，静默通知不改变工作状态。解析只对状态识别去除显示边距，保留相对 Markdown 缩进，聊天正文不改。解析以 CAO 返回文本为准：仅能保留仍存在的缩进；真实 Claude last 模式逐行 strip 后，原缩进代码中的状态行可能被识别为标记，不能还原，示例请用围栏代码。群详情的成员 work_state 可查询。工作状态不等于 CAO 的 CLI status。

配置过自主运行的群中，非静默回复提及 `@群主`（按现有提及边界）或接力被上限截断，会置 runtime.awaiting_owner=true，暂停提醒；其他成员回复、改配置和重启不清除。仅有效的群主新消息解除群级暂停并重开观察期；不会覆盖成员明确报告的等群主/阻塞/已交付状态。

只有全群无在途或排队任务且持续空闲才提醒；等群主/阻塞/已交付、待用户应答（含额度弹窗）、忙碌/启动/未知状态和观察失败均排除。提醒带 `[自动提醒 idle:<群ID>:<序号>]`，作为系统消息只发协调成员；无预算或无可用成员不生成，冷却和累计次数也限制发送。重启后重新观察空闲，原累计计数/冷却/队列不变。提醒不授予额外权限，不替代额度交接、独立审查或真实 CLI 安全边界。

### 用量与费用

`GET /api/groups/{id}/usage` 解析本机 CLI 日志，支持 Claude Code 与 Codex；不是调用 CAO 用量接口。Claude Code 日志可提供 token，不能提供订阅剩余额度；Codex 可提供账号共享的额度窗口，不能当作成员独享额度。

API 金额按内置价格和数据目录下的 `prices.json` 估算；未知价格不会得到确定总价。日志缺失或格式变化可能造成无数据，内置价格与模型推荐没有在本轮核验为最新值。

## 验证范围与已知限制

- **已验证（群主确认）**：2026-11-14 在真实 cao-server 2.5.4 上，使用 codex、claude_code 成员跑通建群、加成员、提及接力、日志代写。以上为历史联调结果；任务 3、4 分别增加隔离单元与集成验收，未重新进行真实 provider 联调。
- **待验证**：kiro 等其他 provider，以及 waiting_user_answer 权限确认后的回复和恢复交互。代码有相关分支，不代表实测通过。
- 成员启动按群协调会话查询、创建和加入：全离线仍复用存活会话，404/terminated 才创建；GET 200 的失败记录不能当成存活，加入 404 后直接尝试重建一次；不同群各用独立锁，失败释放锁。第 4 项已由群主在 9f59def 真实验证同群两成员并发重启及再次收发；故意 CLI 失败后的恢复因影响本机登录未做，仍待真实验证。
- 单测已覆盖Claude 可变动词结束行和 Codex `Worked for …` 结束行的清除，其他 CLI 格式仍可能残留界面字符；消息与日志可能含正文和私人路径，运行数据不提交、不公开。

## 开发与测试

共享规则见 [AGENTS.md](AGENTS.md)，Claude Code 通过 [CLAUDE.md](CLAUDE.md) 导入。产品需求、接口和串行任务分别见 [PRD](docs/PRD.md)、[架构](docs/ARCHITECTURE.md)、[任务清单](docs/TASKS.md)。

```bash
python3 -B -m unittest discover -s tests         # 自动单元与集成测试
python3 -m py_compile server.py tests/fake_cao.py  # 仅语法检查
python3 -B tests/fake_cao.py --port 0             # 独立模拟 CAO，输出随机端口；非自动断言
```

模拟服务支持 `--port 0`（系统分配端口），省略参数仍默认 9889；不要占用或切换当前真实会话。导入不会启动服务。模拟状态按状态查询推进，不依赖固定延迟；`/_test/state` 提供观察，`POST /_test/terminals/{id}/next-turn` 可指定下一轮 `reply`、`hold`、`stale_polls`，`POST /_test/terminals/{id}/release` 释放本轮输出。另可用 `idle_stream` 和 `POST /_test/terminals/{id}/advance` 控制无标记/working/done 阶段；raw full 输出与 last 回复分离。Codex 的 last/full 及流式回复默认采用真实 TUI 排版：首行 `• `、非空续行前两空格、空行保留；自定义 reply 仍传逻辑正文，模拟器负责显示格式。模拟器的 input 默认清空 raw full 缓冲；`append_output=true` 选择只追加兼容路径，`complete_on_input=true` 模拟首次轮询前完成。另用 `/_test/config` 的 `hold_create/startup_hold` 控制创建与启动，`ready` 放出输入提示；`drop=true` 丢弃下一次投递，`padding_bytes` 模拟滚动 FIFO（上限 32768 字节）。会话单独保存，重复同名创建严格返回真实 400；`hold_sessions` 与 `fail_profiles` 配置可按群阻塞创建或按成员注入失败，供并发隔离验证。这些仅用于本机测试。

自动套件含 102 个隔离后端单测、29 个 HTTP 集成和 10 个可选前端行为测试。单测另覆盖按群自主运行的默认隔离、配置校验、6 线程预算原子性、暂停/恢复、引用状态误判及提醒冷却/上限/排除；HTTP 增加广播预算、队列重启恢复与定向自动提醒。单测覆盖隔离导入、提及、接力上限、静默回复、日志幂等、输出清洗、effort 默认/校验/profile 及 Codex 本轮标记判定。集成测试通过 HTTP 验证建群、邀请入群、群主消息、自动转发、残留 completed 防误判和后端重启后的数据恢复，effort 编辑、成员重启和清空配置，以及 Codex 全程 idle 时无新标记/working 不提前发言、done 后最终回复可接力，还覆盖清空后快速完成、连续相同短回复和迟到旧 done 的保护。还覆盖两种 CLI 误报 idle 的启动等待、未取得 terminal 的队列、超时/手动重试/并发防重、迟到完成、启动任务的重启恢复，以及 32KB 滚出 working 后的完成。主样本采用群主提供的 Codex 0.164.0 原生 app 后缀/ST 字节，并覆盖 BEL/旧格式兼容、未知状态及 full 起点。还覆盖两种 provider 首次轮询前完成、此前未见 working 的 32KB done 完成及无误告警、dim 占位输入框、未知特征人工兜底及发送/轮询/写盘异常恢复。另覆盖同群线程并发只创建一次、全离线复用/丢失重建、跨群不阻塞和启动失败恢复，以及 GET 200 terminated 失败记录下的并发重建。集成测试以独立子进程运行原始 `server.py`，使用随机端口、临时 HOME/数据/工作目录和不继承凭据的环境；不连接 9889，不访问真实 `~/.cao-groupchat`，不启动 provider CLI。等待使用有超时的轮询，失败也回收子进程和目录。

前端测试通过可选的本机 Node 执行真实内联脚本，使用最小 DOM 夹具验证强度选项、provider 切换提交、演示邀请编辑和卡片，以及投递不确定时的状态/重发、抽屉/焦点/缩放/选群与手机演示工作流；不安装 npm 包，不增加应用依赖。未安装 Node 时这 10 项会明确跳过，不能记为前端已验证。浏览器排版及真实 provider 联调需单独验收。

中文界面和演示模式的逐步人工结果见[任务清单](docs/TASKS.md)：2026-11-14 群主第 1–6、8 步通过；第 7 步主题正常，但宽度小于 1100px 时右侧成员/日志面板不可达，700px 时左侧群列表也不可达，第二轮第 5 项 `0c3d33b` 已审查通过，群主在 375×812、375×667、800/1100px、深浅色下完成抽屉/建群/编辑/日志等验收并结案；媒体查询变化收尾修复待复审，实际 Tab 循环及故意 CLI 启动失败恢复待验证；Node 最小 DOM 不能验证布局或横向溢出。模拟集成通过不代表真实 provider 联调通过。文档修改使用链接、隐私、范围和 Git 差异检查验证。
