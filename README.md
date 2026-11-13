# CAO 项目群 —— 多 agent 群聊式 GUI

把 [CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator) 包装成“项目群”：

- **你是群主**：建项目群、决定邀请哪些模型（Claude Code、Codex、Kiro、Gemini、Copilot……）
- **分配权限与职责**：每个成员可以设角色（统筹 / 开发 / 评审）或自定义 CAO 工具权限，并写明职责
- **群聊对话**：各模型的回复像群消息一样出现；回复里 `@某成员` 会自动转给他，形成接力
- **共享行为日志**：所有行为都写到工作目录下的 `GROUP_LOG.md`，有写权限的 agent 会直接往里追加，没有写权限的用 `[日志] …` 由系统代写

## 运行

需要：Python 3.9+、tmux、已安装并登录的 CAO 和各个模型 CLI。

```bash
# 1. 安装 CAO（只需一次）
uv tool install cli-agent-orchestrator

# 2. 启动 CAO 服务（保持运行）
cao-server

# 3. 另开一个终端，启动群聊 GUI（只用标准库，不需要 pip install）
cd cao-groupchat
python3 server.py
# 浏览器打开 http://localhost:8765
```

如果直接双击打开 `index.html`（没有后端），会进入**演示模式**，回复是模拟的，用来体验界面。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CAO_URL` | `http://localhost:9889` | cao-server 地址 |
| `CAO_TOKEN` | 空 | 若 CAO 开了本地 token 鉴权，填 `CAO_AUTH_LOCAL_TOKEN` 的值 |
| `GROUPCHAT_PORT` | `8765` | 本程序端口（注意 CAO 自带 Web UI 占用 9889） |
| `GROUPCHAT_DATA` | `~/.cao-groupchat` | 群、消息记录的保存位置 |

## 工作原理

| 群聊概念 | CAO 中对应 |
|---|---|
| 一个项目群 | 一个 CAO session |
| 一位成员 | 一个 terminal（独立运行的 CLI agent 进程，在 tmux 里） |
| 成员的角色/权限/职责 | 自动生成的 agent profile（`role` / `allowedTools` / 系统提示词），通过 `POST /agents/profiles` 安装，失败时退回 `cao install` |
| 群主发消息 | `POST /terminals/{id}/input`；`@谁` 就发给谁，不 @ 或 `@所有人` 则发给全体 |
| 成员回复 | 后台每 2 秒查一次 terminal 状态，从 processing 变为 completed/idle 后读取 `output?mode=last` |
| 成员之间接力 | 回复中出现 `@成员名` → 自动转发给该成员；每条群主消息最多转 6 次，防止 agent 互相聊个不停 |
| 共享文件 | `<工作目录>/GROUP_LOG.md` |

### 权限说明

权限由 CAO 执行，强度因 CLI 而异：

- **硬限制**（CLI 真的会拦截）：Claude Code、Kiro CLI、Copilot CLI、OpenCode
- **软约束**（只靠提示词）：Codex、Kimi、Antigravity(Gemini)
- **不执行**：Cursor CLI

所以涉及写文件、执行命令的敏感工作，建议交给硬限制的 CLI。界面上会对软约束和不执行限制的成员给出提示。

### 修改成员

在右侧“群成员”里点“编辑权限/职责”，保存后会关闭该成员的终端，用新 profile 重新启动（新会话，之前的上下文不保留）。
新成员入群时，系统会静默通知现有成员，这条通知的回复不会出现在群聊里。

## 已知限制

- 没有在真实的 cao-server 上联调过，只用一个模拟的 CAO 测试了完整流程。如果你的 CAO 版本的接口参数不同（比如 `output` 的 `mode` 取值），改 `server.py` 里的 `cao()` 调用即可，接口以本机 `http://localhost:9889/docs` 为准。
- 回复靠读取终端输出获取，个别 CLI 的输出里可能夹带界面字符。
- 当某个 agent 弹出权限确认（waiting_user_answer）时，群里会出现提示，你可以 `@它 1` 或 `@它 同意` 来回复。
- 服务只监听 127.0.0.1，没有做登录鉴权，请勿暴露到公网。
