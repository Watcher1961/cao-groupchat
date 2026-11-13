# CLAUDE.md —— CAO 项目群 GUI（交接说明）

本项目从 claude.ai 的对话迁移过来，以下是当时的上下文，供 Claude Code 继续开发。

## 目标（用户原始需求）
给 AWS 开源的 CLI Agent Orchestrator（CAO，https://github.com/awslabs/cli-agent-orchestrator）做一个**群聊式可视化 GUI**：
- 用户是"群主"，可以建立项目群，决定邀请哪些模型（Claude Code、Codex、Kiro、Gemini…）进群
- 给每个成员分配权限与职责
- 各模型的发言像群聊对话一样出现在窗口里
- 各自的行为记录到一个共享修改的文件中

## 当前实现
- `server.py`：纯 Python 标准库后端（端口 8765），通过 HTTP 调 cao-server（默认 http://localhost:9889）
  - 群 = CAO session；成员 = CAO terminal
  - 成员的角色/权限/职责 → 生成 agent profile（frontmatter：provider、model、role、allowedTools、permissionMode；正文是系统提示词，含群规则和共享日志规则），通过 `POST /agents/profiles`（409 时 `PUT`）安装，失败退回 `cao install <file>`
  - 首个成员 `POST /sessions?agent_profile=&provider=&session_name=&working_directory=&allowed_tools=&model=`；之后的成员 `POST /sessions/{s}/terminals:operator`，不存在时退回 `POST /sessions/{s}/terminals?caller_id=<首个成员终端id>`
  - 发消息 `POST /terminals/{id}/input?message=`；后台线程每 2 秒 `GET /terminals/{id}` 查 status，processing → completed/idle 后读 `GET /terminals/{id}/output?mode=last`（422 时退回 mode=full 并取增量）
  - 回复里 `@成员名` 自动转发，每条群主消息最多 6 跳（MAX_RELAY_HOPS）
  - 共享日志：`<工作目录>/GROUP_LOG.md`。有写权限的 agent 自己追加；没有的在回复里写 `[日志] …`，后端代写
  - 新成员入群时静默通知老成员（silent 标记，回复不进群聊）
  - 状态持久化在 `~/.cao-groupchat/groups.json`
  - 用量与开销：`GET /api/groups/{id}/usage`，读本机 CLI 日志（CAO 不提供用量）。Claude Code 读 `~/.claude/projects/<工作目录把非字母数字换成 ->/*.jsonl`（按 message.id 去重，只有 token、没有订阅额度）；Codex 读 `~/.codex/sessions/**/rollout-*.jsonl`（取最后一次 `token_count` 的累计用量，`rate_limits` 是账号级额度窗口）。用系统提示词里「项目群「群名」里的成员 **名字**」认领日志文件，只看群创建之后的文件。价格表 `PRICES`（美元/百万 token，仅内置 Claude），其他模型由用户在 `~/.cao-groupchat/prices.json` 补充。成员 `plan` 决定显示方式：订阅显示额度条，`api` 显示估算金额，未选两者都给
  - 选工作目录：`POST /api/pick-folder` 用 osascript 弹 macOS 原生"选择文件夹"对话框（阻塞到用户选完）；非 macOS 或失败时前端退回页面内浏览器（`GET /api/fs?path=`、`POST /api/fs/mkdir`）
- `index.html`：单文件前端（无构建）。视觉为 Linear 式工作台（靛蓝主色、头像状态点），对话区借鉴 Telegram：群主气泡靠右、成员靠左带彩色名字，同一人 5 分钟内连续发言合并且头像只在最后一条，气泡尾巴用 `clip-path` 画，时间嵌在气泡右下角，系统消息/日期是居中半透明胶囊，背景是自绘点阵；颜色全部走 `:root` 设计令牌，支持浅色/深色/跟随系统（左下角切换，存 localStorage `theme`）。左侧群列表、中间聊天、右侧成员面板/共享日志；建群与成员编辑弹窗。后端不可用时自动进入**演示模式**（浏览器内 Mock 类模拟回复）

## 测试情况
- 已用一个模拟 CAO（`tests/fake_cao.py`，监听 9889）跑通：建群、@ 发言、agent 间 @ 接力、邀请/编辑/移出成员、解散群、共享日志
- 前端用 Playwright 截图检查过演示模式
- 2026-11-14 已在真实 cao-server 2.5.4 上联调通过（两个 codex 成员：建群、`terminals:operator` 加成员、@ 接力、`[日志]` 代写、agent 自写日志）。claude_code/kiro 等 provider 本机未安装，尚未验证
- 对照 /docs 的修正：PUT profile 只发 `{content}`；`@builtin` 不算写权限；用 input 返回的 `dispatch_boundary` 与 terminal 的 `status_generation` 判断"本轮完成"，避免上轮残留的 completed 被误判

## 待办 / 下一步建议
1. 在本机真实 CAO 上联调：先 `cao-server`，打开 http://localhost:9889/docs 核对接口（尤其是 output 的 mode 枚举值、`terminals:operator` 路由、profile 安装接口的鉴权与 body 格式、terminal status 取值）
2. 验证各 provider 的输出清洗（ANSI/TUI 字符），必要时按 provider 定制 `clean_output`
3. 处理 `waiting_user_answer`（agent 请求权限确认）的交互是否顺畅
4. 可选：用 CAO 的 WebSocket `/terminals/{id}/ws` 或 `/events` 替代轮询；给每个成员加"查看原始终端"按钮

## 运行
```bash
cao-server                      # 终端 1
python3 server.py               # 终端 2，浏览器打开 http://localhost:8765
python3 tests/fake_cao.py       # 没有真实 CAO 时，用它代替 cao-server 做测试
```

## 约定
- 后端保持零第三方依赖；前端保持单文件、无构建
- 界面文案用中文
