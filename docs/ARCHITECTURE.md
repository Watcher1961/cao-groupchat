# 架构与接口

## 模块与数据

| 模块 | 职责 |
| --- | --- |
| `index.html` | 中文单页面工作台、成员与权限表单、消息/状态/用量刷新、主题和浏览器 Mock |
| `server.py` HTTP 层 | 绑定 `127.0.0.1`，提供首页及 `/api/*`，默认端口 8765 |
| 群与成员逻辑 | 校验成员、生成 profile、启停 terminal、处理提及与接力 |
| CAO 适配与轮询 | 标准库 HTTP 请求、可选 Bearer token、每 2 秒轮询 terminal 状态和完成输出 |
| 持久化与共享日志 | 群和消息写入数据目录的 `groups.json`；先写临时文件再替换；工作目录内追加 `GROUP_LOG.md` |
| 用量解析 | 读取本机 Claude Code/Codex CLI JSONL，按成员标记认领，缓存解析并估算费用 |
| `tests/fake_cao.py` | 固定端口 9889 的简化模拟服务；尚未完整模拟状态代际，也不包含自动验收断言 |

默认数据目录为 `~/.cao-groupchat`，可用 `GROUPCHAT_DATA` 覆盖。profile 安装降级文件在数据目录的 `profiles/`，自定义价格在 `prices.json`。以上均为本机运行数据；共享日志保持 Git 忽略。消息和日志可能含正文与私人路径，当前并未实现自动脱敏。

## 数据流

1. 浏览器经 `/api/groups` 创建群，提交工作目录与成员配置；后端保存群并异步启动成员。
2. 每位成员生成独立 agent profile；一个群对应一个 CAO session，一位成员对应一个 terminal。权限与职责写入 profile，实际执行由 provider 决定。
3. 群主消息经 `/api/groups/{id}/messages` 进入后端。匹配成员提及则定向发送；无匹配提及或广播时发送给所有有 terminal 的成员。
4. CAO input 响应的 `dispatch_boundary` 记为本轮边界。轮询读取 `status_generation`，只有大于边界的完成状态才属于本轮；字段缺失时降级为忙碌状态或发送后等待时间判定。
5. 读取并清洗回复，写入群消息；行首 `[日志]` 逐行代写，成员提及触发接力，最多 6 跳。静默通知的回复在这些处理之前直接丢弃。
6. 浏览器获取增量消息、共享日志和成员用量。持久化可加载群记录，但真实 terminal 是否仍存活需后续轮询判断；完整重启恢复待任务 4 验收。

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
| `GET /api/groups/{id}/log`、`GET /api/groups/{id}/usage` | 共享日志、用量汇总 |
| `POST /api/pick-folder` | macOS 原生选目录；前端在失败时降级为页面浏览 |
| `GET /api/fs`、`POST /api/fs/mkdir` | 本机目录浏览和创建 |

## CAO 接口与兼容分支

默认 `CAO_URL=http://localhost:9889`；若配置 `CAO_TOKEN`，请求带 Bearer 鉴权。下表来自现有代码，兼容分支存在不等于所有分支均已实测。

| 操作 | CAO HTTP 调用或降级 |
| --- | --- |
| 连通性与 provider | `GET /health`、`GET /agents/providers` |
| 安装 profile | `POST /agents/profiles`，body 为 `{name, content}`；409 时 `PUT /agents/profiles/{name}`，body 仅 `{content}`；失败退回 `cao install <file> --provider <provider>` |
| 首位成员 | `POST /sessions`；query 含 profile、provider、工作目录、工具、模型和 session_name；body 含 description |
| 后续成员 | `POST /sessions/{s}/terminals:operator`；404/405 时退回 `/sessions/{s}/terminals` 并带首个存活成员的 caller_id |
| 发消息 | `POST /terminals/{id}/input?message=...`，读取 dispatch_boundary |
| 状态与输出 | `GET /terminals/{id}`；`GET /terminals/{id}/output?mode=last`，422 时退回 mode=full 并截取新增内容 |
| 停止与解散 | `POST /terminals/{id}/exit`、`DELETE /terminals/{id}`、`DELETE /sessions/{s}` |

## 用量来源与限制

- Claude Code：`~/.claude/projects/<工作目录编码>/*.jsonl`，按 message.id 等标识去重并汇总 token；日志不提供订阅剩余额度。
- Codex：`~/.codex/sessions/**/rollout-*.jsonl`，取 token_count 中累计 token，结合工作目录与成员标记认领；rate_limits 取最近可用记录，属于账号共享额度，不属于单个成员。
- 依据文件修改时间筛选候选日志，门槛为群创建时间减 60 秒；不等于逐条事件严格按时间过滤。日志格式和标记缺失可能导致无数据或归属不完整。
- 费用来自内置 `PRICES` 与本机 `prices.json`，仅为估算；未知价格返回空值，部分金额另列。内置价格与模型推荐不代表已核验的最新定价或可用性。

## 关键取舍与验证边界

- 标准库后端与单文件前端降低部署成本；保持无构建，不增加框架依赖。
- HTTP 轮询便于兼容和调试；回复有轮询延迟，未使用 WebSocket，部分 CLI 输出仍可能残留 TUI 字符。
- 角色和工具列表交由 CAO/provider 执行。前端对 Codex 等标软约束、Cursor 标不执行；这不是本轮验证过的安全隔离保证，`@builtin` 本身也不授予写文件权限。
- 状态临时文件替换降低部分写入风险，但不提供数据库事务或跨进程并发保证。成员并发重启时可能争抢创建同一 session，后到者报 Session already exists，本轮不修复。
- 仅监听本机回环，无 GUI 登录鉴权，目录接口可访问本机目录，不适合公开部署。
- 2026-11-14 真实 cao-server 2.5.4 上 codex、claude_code 的建群、加成员、接力、日志代写已获群主确认。其他 provider 与 waiting_user_answer 交互待验证；自动回归由任务 3、4 补齐。
