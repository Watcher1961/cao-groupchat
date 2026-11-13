# CAO 项目群

为 [CLI Agent Orchestrator（CAO）](https://github.com/awslabs/cli-agent-orchestrator) 提供本机群聊式 GUI：群主建项目群，配置成员的 provider、模型、角色、工具和职责，成员在群中回复并通过提及接力。

支持成员管理、共享日志、从本机 CLI 日志读取用量，以及浅色/深色主题。后端使用 Python 标准库；前端为单个 `index.html`，无需构建。界面文案为中文。

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

## 工作方式

- 一个群对应一个 CAO session，一位成员对应一个 terminal。角色、工具和职责写入生成的 profile，优先经 HTTP 安装，失败时退回 `cao install`。
- 群主提及已匹配成员时定向发送；无匹配提及或广播时发送给所有已启动成员。成员回复中的提及继续接力，上限 6 跳。
- 后台每 2 秒轮询 terminal；用 dispatch_boundary/status_generation 区分本轮状态，再读取完成输出。该逻辑的隔离自动回归待任务 4。
- 编辑成员配置会停止旧 terminal 并启动新 terminal，不保证保留旧 CLI 上下文。
- 共享日志位于群工作目录的 `GROUP_LOG.md`。有写权限的成员直接追加，无写权限者在行首使用 `[日志]` 由后端代写；该文件含本机运行信息，保持 Git 忽略。
- 工具权限最终由 CAO 和 provider 执行。界面会标注部分 provider 的软约束或不执行限制；这些标注不等于安全隔离已验证。

### 用量与费用

`GET /api/groups/{id}/usage` 解析本机 CLI 日志，支持 Claude Code 与 Codex；不是调用 CAO 用量接口。Claude Code 日志可提供 token，不能提供订阅剩余额度；Codex 可提供账号共享的额度窗口，不能当作成员独享额度。

API 金额按内置价格和数据目录下的 `prices.json` 估算；未知价格不会得到确定总价。日志缺失或格式变化可能造成无数据，内置价格与模型推荐没有在本轮核验为最新值。

## 验证范围与已知限制

- **已验证（群主确认）**：2026-11-14 在真实 cao-server 2.5.4 上，使用 codex、claude_code 成员跑通建群、加成员、提及接力、日志代写。本轮任务 2 不重跑真实会话。
- **待验证**：kiro 等其他 provider，以及 waiting_user_answer 权限确认后的回复和恢复交互。代码有相关分支，不代表实测通过。
- 多成员同时重启时可能争抢新建 session，后到者报 Session already exists；成员推理强度（codexConfig / claudeConfig）尚未支持。本轮不修复这两项。
- 部分 CLI 的回复可能残留界面字符；消息与日志可能含正文和私人路径，运行数据不提交、不公开。

## 开发与测试

共享规则见 [AGENTS.md](AGENTS.md)，Claude Code 通过 [CLAUDE.md](CLAUDE.md) 导入。产品需求、接口和串行任务分别见 [PRD](docs/PRD.md)、[架构](docs/ARCHITECTURE.md)、[任务清单](docs/TASKS.md)。

```bash
python3 -m py_compile server.py tests/fake_cao.py  # 仅语法检查
python3 tests/fake_cao.py                        # 持续运行的模拟 CAO，非自动测试
```

模拟服务固定监听 9889，仅在隔离验证环境替代真实 CAO，不要占用或切换当前真实会话。当前尚无自动断言套件；标准库 unittest 和临时目录、随机端口的集成测试分别在任务 3、4 实施。文档修改使用链接、隐私、范围和 Git 差异检查验证。
