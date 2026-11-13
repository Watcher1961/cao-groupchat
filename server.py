#!/usr/bin/env python3
"""
CAO 群聊 GUI 后端 —— 把 CLI Agent Orchestrator (CAO) 包装成"项目群"。

- 群主（你）建群、邀请模型、分配角色/权限/职责
- 每个群 = 一个 CAO session；每个成员 = 一个 CAO terminal（独立的 CLI agent 进程）
- 成员回复以群聊消息形式出现；回复里 @某成员 会自动转给该成员
- 所有行为记录到工作目录下的共享文件 GROUP_LOG.md

只用 Python 标准库，无需额外安装。用法：
    cao-server                 # 先启动 CAO（默认 http://localhost:9889）
    python3 server.py          # 再启动本程序，浏览器打开 http://localhost:8765

环境变量：
    CAO_URL          CAO 服务地址，默认 http://localhost:9889
    CAO_TOKEN        若 CAO 开启了本地 token 鉴权，填 CAO_AUTH_LOCAL_TOKEN 的值
    GROUPCHAT_PORT   本程序端口，默认 8765
    GROUPCHAT_DATA   群数据保存目录，默认 ~/.cao-groupchat
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse, request

CAO_URL = os.environ.get("CAO_URL", "http://localhost:9889").rstrip("/")
CAO_TOKEN = os.environ.get("CAO_TOKEN", "")
PORT = int(os.environ.get("GROUPCHAT_PORT", "8765"))
DATA_DIR = Path(os.environ.get("GROUPCHAT_DATA", "~/.cao-groupchat")).expanduser()
STATE_FILE = DATA_DIR / "groups.json"
HERE = Path(__file__).resolve().parent

POLL_SECONDS = 2.0
MAX_RELAY_HOPS = 6          # 一条群主消息最多引发多少次 agent 之间的 @ 转发，防止无限对话
LOG_FILENAME = "GROUP_LOG.md"

PROVIDERS = [
    {"id": "claude_code", "label": "Claude Code"},
    {"id": "codex", "label": "Codex CLI"},
    {"id": "kiro_cli", "label": "Kiro CLI"},
    {"id": "antigravity_cli", "label": "Antigravity CLI (Gemini)"},
    {"id": "copilot_cli", "label": "GitHub Copilot CLI"},
    {"id": "kimi_cli", "label": "Kimi CLI"},
    {"id": "opencode_cli", "label": "OpenCode CLI"},
    {"id": "cursor_cli", "label": "Cursor CLI"},
    {"id": "hermes", "label": "Hermes"},
]
ROLES = {
    "supervisor": ["@cao-mcp-server", "fs_read", "fs_list"],
    "developer": ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"],
    "reviewer": ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"],
}
# 按订阅计划给出的模型选项（仅作推荐，实际可用以账号为准；界面里始终可以手填）
# Claude Code 的 opus/sonnet/haiku 是别名，会自动指向该档最新模型
CLAUDE_PLANS = [
    {"id": "pro", "label": "Claude Pro", "models": [
        {"id": "sonnet", "label": "Sonnet（推荐）"},
        {"id": "haiku", "label": "Haiku（快、省额度）"},
        {"id": "opus", "label": "Opus（额度消耗快，Pro 可能受限）"},
    ]},
    {"id": "max", "label": "Claude Max", "models": [
        {"id": "opus", "label": "Opus（推荐）"},
        {"id": "opusplan", "label": "Opus 规划 + Sonnet 执行"},
        {"id": "sonnet", "label": "Sonnet"},
        {"id": "haiku", "label": "Haiku"},
    ]},
    {"id": "team", "label": "Team / Enterprise", "models": [
        {"id": "opus", "label": "Opus"},
        {"id": "sonnet", "label": "Sonnet（推荐）"},
        {"id": "haiku", "label": "Haiku"},
    ]},
    {"id": "api", "label": "API Key（按量计费）", "models": [
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
        {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
        {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
        {"id": "claude-fable-5-2", "label": "Claude Fable 5.2"},
    ]},
]


def codex_models() -> list[dict]:
    """读 Codex CLI 本机缓存的模型列表（按登录账号下发，所以就是该账号能用的模型）。"""
    f = Path("~/.codex/models_cache.json").expanduser()
    try:
        ms = json.loads(f.read_text(encoding="utf-8")).get("models", [])
    except (OSError, ValueError, AttributeError):
        return []
    ms = [m for m in ms if m.get("visibility") == "list" and m.get("slug")]
    ms.sort(key=lambda m: m.get("priority", 99))
    return [{"id": m["slug"], "label": m.get("display_name") or m["slug"],
             "note": m.get("description", "")} for m in ms]


def model_catalog() -> dict:
    cm = codex_models()
    return {
        "claude_code": CLAUDE_PLANS,
        "codex": [
            {"id": "chatgpt", "label": "ChatGPT 订阅（Plus / Pro / Business）", "models": cm},
            {"id": "api", "label": "OpenAI API Key", "models": cm},
        ] if cm else [],
    }


def providers_with_install() -> list[dict]:
    """在 PROVIDERS 上标出本机是否装了对应 CLI（来自 CAO 的 /agents/providers）。"""
    try:
        inst = {p["name"]: p for p in cao("GET", "/agents/providers", timeout=3)}
    except (CaoError, TypeError, KeyError):
        inst = {}
    out = []
    for p in PROVIDERS:
        i = inst.get(p["id"])
        out.append({**p, "installed": i.get("installed") if i else None,
                    "binary": i.get("binary") if i else None})
    return out


BUSY = {"processing", "running", "busy"}
DONE = {"completed", "idle"}

lock = threading.RLock()
groups: dict[str, dict] = {}


# ───────────────────────── 工具函数 ─────────────────────────
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()
    return s or uuid.uuid4().hex[:6]


def save_state() -> None:
    with lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def load_state() -> None:
    global groups
    if STATE_FILE.exists():
        try:
            groups = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            groups = {}


class CaoError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"CAO {status}: {msg}")
        self.status = status


def cao(method: str, path: str, query: dict | None = None, body=None, timeout=30):
    q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
    url = CAO_URL + path + ("?" + parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if CAO_TOKEN:
        req.add_header("Authorization", f"Bearer {CAO_TOKEN}")
    try:
        with request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "null"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urlerror.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise CaoError(e.code, detail) from None
    except (urlerror.URLError, OSError) as e:
        raise CaoError(0, f"无法连接 CAO ({CAO_URL}): {e}") from None


def cao_alive() -> bool:
    try:
        cao("GET", "/health", timeout=3)
        return True
    except CaoError:
        return False


# ───────────────────────── 共享日志 ─────────────────────────
def log_path(g: dict) -> Path:
    return Path(g["workdir"]).expanduser() / LOG_FILENAME


def append_log(g: dict, who: str, what: str) -> None:
    p = log_path(g)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                f"# 项目群「{g['name']}」共享行为日志\n\n"
                "> 所有成员（包括群主）的关键行为都记录在这里。agent 也会直接往这里追加。\n\n",
                encoding="utf-8",
            )
        with p.open("a", encoding="utf-8") as f:
            for line in what.strip().splitlines() or [""]:
                f.write(f"- [{now_iso()}] [{who}] {line}\n")
    except OSError as e:
        add_msg(g, "system", "系统", f"写共享日志失败：{e}", log=False)


# ───────────────────────── 消息 ─────────────────────────
def add_msg(g: dict, kind: str, sender: str, text: str, to=None, log=True, mid=None) -> dict:
    with lock:
        m = {
            "seq": len(g["messages"]) + 1,
            "ts": now_iso(),
            "kind": kind,          # owner | agent | system
            "sender": sender,
            "member_id": mid,
            "text": text,
            "to": to or [],
        }
        g["messages"].append(m)
    if log and kind != "system":
        target = f"（→ {', '.join(to)}）" if to else ""
        first = text.strip().splitlines()[0][:200] if text.strip() else ""
        append_log(g, sender, f"发言{target}：{first}")
    save_state()
    return m


# ───────────────────────── Profile ─────────────────────────
def member_tools(m: dict) -> list[str]:
    if m.get("tools"):
        return m["tools"]
    return ROLES.get(m.get("role", "developer"), ROLES["developer"])


EFFORTS = {
    "codex": ("low", "medium", "high", "xhigh"),
    "claude_code": ("low", "medium", "high", "xhigh", "max"),
}


def clean_effort(provider: str, value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("推理强度必须是字符串")
    effort = value.strip()
    if effort and effort not in EFFORTS.get(provider, ()):
        raise ValueError("该模型 CLI 不支持所选推理强度")
    return effort


def profile_name(g: dict, m: dict) -> str:
    return f"grp_{g['id']}_{m['id']}"


def build_profile(g: dict, m: dict) -> str:
    tools = member_tools(m)
    roster = "\n".join(
        f"- @{x['name']}（{x['provider']}，角色 {x.get('role','developer')}）：{x.get('duty') or '未指定'}"
        for x in g["members"]
    )
    # 注意：@builtin 只是"该 provider 自带工具集"的选择器，本身不授予写权限
    can_write = any(t in tools for t in ("fs_write", "fs_*", "*"))
    fm = [
        "---",
        f"name: {profile_name(g, m)}",
        f"description: \"项目群 {g['name']} 成员 {m['name']}\"",
        f"provider: {m['provider']}",
    ]
    if m.get("model"):
        fm.append(f"model: {m['model']}")
    effort = clean_effort(m["provider"], m.get("effort"))
    if effort:
        if m["provider"] == "codex":
            fm += ["codexConfig:", f"  model_reasoning_effort: {effort}"]
        elif m["provider"] == "claude_code":
            fm += ["claudeConfig:", f"  effort: {effort}"]
    if m.get("role") in ROLES and not m.get("tools"):
        fm.append(f"role: {m['role']}")
    fm.append("allowedTools:")
    fm += [f"  - \"{t}\"" for t in tools]
    if m["provider"] == "claude_code" and m.get("permission_mode"):
        fm.append(f"permissionMode: {m['permission_mode']}")
    fm.append("---")
    log_rule = (
        f"每完成一个实际动作（修改文件、运行命令、做出决定），立即在共享日志 `{log_path(g)}` 末尾追加一行，格式：\n"
        f"`- [YYYY-MM-DD HH:MM:SS] [{m['name']}] 动作描述`。不要改动别人写的行。"
        if can_write else
        "你没有写文件权限。需要记录的动作，请在回复末尾单独成行写 `[日志] 动作描述`，系统会代你写入共享日志。"
    )
    body = f"""
你是项目群「{g['name']}」里的成员 **{m['name']}**。

## 你的职责
{m.get('duty') or '协助完成项目。'}

## 群成员
{roster}

## 群聊规则
1. 群主是人类项目负责人，他的消息以 `[群主]` 开头；其他成员转来的消息以 `[来自 某某]` 开头。
2. 你的每次回复都会原样显示在群聊窗口里，请简洁、有条理，先给结论。
3. 需要其他成员配合时，在回复中写 `@成员名` 并说明需要他做什么，系统会把你的回复转给他。不需要别人时不要 @。
4. 只做职责范围内的事；超出权限或职责的事，@群主 请示。
5. 共享日志：{log_rule}
"""
    return "\n".join(fm) + "\n" + body.strip() + "\n"


def install_profile(g: dict, m: dict) -> None:
    name = profile_name(g, m)
    content = build_profile(g, m)
    # 1) 先走 HTTP API
    try:
        cao("POST", "/agents/profiles", body={"name": name, "content": content})
        return
    except CaoError as e:
        if e.status == 409:
            try:
                cao("PUT", f"/agents/profiles/{name}", body={"content": content})
                return
            except CaoError:
                pass
        api_err = e
    # 2) 退回到命令行 `cao install <file>`
    if shutil.which("cao"):
        d = DATA_DIR / "profiles"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{name}.md"
        f.write_text(content, encoding="utf-8")
        r = subprocess.run(["cao", "install", str(f), "--provider", m["provider"]],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return
        raise RuntimeError(f"cao install 失败：{r.stderr or r.stdout}")
    raise RuntimeError(f"安装 profile 失败：{api_err}")


# ───────────────────────── 成员生命周期 ─────────────────────────
def start_member(g: dict, m: dict) -> None:
    p = next((x for x in providers_with_install() if x["id"] == m["provider"]), None)
    if p and p.get("installed") is False:
        raise RuntimeError(f"本机没有找到 `{p['binary']}` 命令（{p['label']} 未安装或不在 PATH 里）。"
                           f"请先安装并登录，然后在成员面板里重启该成员。")
    install_profile(g, m)
    q = {
        "agent_profile": profile_name(g, m),
        "provider": m["provider"],
        "working_directory": str(Path(g["workdir"]).expanduser()),
        "allowed_tools": ",".join(member_tools(m)),
        "model": m.get("model") or None,
    }
    live = [x for x in g["members"] if x.get("terminal_id") and x is not m]
    if not g.get("session_name") or not live:
        t = cao("POST", "/sessions", {**q, "session_name": g["session_slug"]},
                body={"description": f"{g['name']} / {m['name']}"}, timeout=180)
        g["session_name"] = t.get("session_name") or g["session_slug"]
    else:
        path = f"/sessions/{g['session_name']}/terminals"
        try:
            t = cao("POST", path + ":operator", q, body={"description": m["name"]}, timeout=180)
        except CaoError as e:
            if e.status not in (404, 405):
                raise
            # 老版本没有 :operator 路由，以群里第一个成员的身份创建
            t = cao("POST", path, {**q, "caller_id": live[0]["terminal_id"]},
                    body={"description": m["name"]}, timeout=180)
    m["terminal_id"] = t.get("id") or t.get("terminal_id")
    m["status"] = t.get("status", "starting")
    m["pending"] = False
    save_state()


def stop_member(m: dict) -> None:
    tid = m.get("terminal_id")
    if not tid:
        return
    for method, path in (("POST", f"/terminals/{tid}/exit"), ("DELETE", f"/terminals/{tid}")):
        try:
            cao(method, path, timeout=15)
        except CaoError:
            pass
    m["terminal_id"] = None
    m["status"] = "stopped"


def send_to(g: dict, m: dict, text: str, hops: int, silent: bool = False) -> None:
    if not m.get("terminal_id"):
        add_msg(g, "system", "系统", f"{m['name']} 尚未启动，消息未送达。", log=False)
        return
    try:
        r = cao("POST", f"/terminals/{m['terminal_id']}/input", {"message": text})
        with lock:
            # CAO 会把上一轮的 completed 状态保留着；只有 status_generation > dispatch_boundary
            # 的状态才属于这次发送（见 POST /terminals/{id}/input 的说明，#735/#407）
            m["boundary"] = r.get("dispatch_boundary") if isinstance(r, dict) else None
            m["pending"] = True
            m["reply_handled"] = False
            m["sent_at"] = time.time()
            m["saw_busy"] = False
            m["hops"] = hops
            m["silent"] = silent
        save_state()
    except CaoError as e:
        add_msg(g, "system", "系统", f"发给 {m['name']} 失败：{e}", log=False)


def find_mentions(g: dict, text: str, exclude: str | None = None) -> list[dict]:
    names = sorted((x["name"] for x in g["members"]), key=len, reverse=True)
    if not names:
        return []
    # Resolve the longest known name before checking its suffix, including dev/dev-team.
    pattern = r"@(" + "|".join(re.escape(name) for name in names) + r")"
    mentioned = {match.group(1) for match in re.finditer(pattern, text)
                 if not re.match(r"[\w一-鿿]", text[match.end():match.end() + 1])}
    return [x for x in g["members"] if x["id"] != exclude and x["name"] in mentioned]


def get_reply(m: dict) -> str:
    tid = m["terminal_id"]
    for mode in ("last", "full"):
        try:
            r = cao("GET", f"/terminals/{tid}/output", {"mode": mode})
        except CaoError as e:
            if e.status == 422 and mode == "last":
                continue
            raise
        text = r.get("output", "") if isinstance(r, dict) else str(r)
        if mode == "full":   # 退化：只取新增部分
            prev = m.get("full_len", 0)
            m["full_len"] = len(text)
            text = text[prev:]
        return clean_output(text)
    return ""


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
TUI_FOOTER = re.compile(
    r"(?:✻ [^\W\d_]+ for (?:\d+[hms]\s*)+· done \d{1,2}:\d{2} (?:AM|PM)"
    r"|Worked for (?:\d+[hms]\s*)+• \d{2}:\d{2})"
)


def clean_output(s: str) -> str:
    s = ANSI.sub("", s).replace("\r", "")
    lines = s.strip().splitlines()
    # Only remove complete footer lines at the end, preserving prose and code examples.
    while lines and (not lines[-1].strip() or TUI_FOOTER.fullmatch(lines[-1].strip())):
        lines.pop()
    s = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return s.strip()


def handle_reply(g: dict, m: dict, text: str) -> None:
    if m.get("reply_handled"):
        return
    m["reply_handled"] = True
    if m.pop("silent", False):          # 系统通知（如新成员入群）的回复不进群聊
        return
    if not text:
        text = "（无文字输出）"
    add_msg(g, "agent", m["name"], text, mid=m["id"])
    for line in re.findall(r"^[^\S\r\n]*\[日志\][^\S\r\n]*(\S[^\r\n]*)\r?$", text, flags=re.M):
        append_log(g, m["name"], line)
    hops = m.get("hops", 0)
    targets = find_mentions(g, text, exclude=m["id"])
    if targets and hops < MAX_RELAY_HOPS:
        for t in targets:
            send_to(g, t, f"[来自 {m['name']}] {text}", hops + 1)
    elif targets:
        add_msg(g, "system", "系统",
                f"已达到自动转发上限（{MAX_RELAY_HOPS} 次），请群主决定下一步。", log=False)


def poll_loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        with lock:
            snapshot = [(g, m) for g in groups.values() for m in g["members"] if m.get("terminal_id")]
        for g, m in snapshot:
            try:
                t = cao("GET", f"/terminals/{m['terminal_id']}", timeout=5)
            except CaoError as e:
                if e.status == 404:
                    m["status"] = "gone"
                    m["terminal_id"] = None
                    add_msg(g, "system", "系统", f"{m['name']} 的终端已不存在。", log=False)
                continue
            st = str(t.get("status", "")).lower()
            prev = m.get("status")
            gen, boundary = t.get("status_generation"), m.get("boundary")
            fresh = gen is None or boundary is None or gen > boundary
            m["status"] = st
            if not m.get("pending"):
                continue
            if st in BUSY:
                m["saw_busy"] = True
            elif st == "waiting_user_answer" and prev != st and fresh:
                try:
                    tail = get_reply(m)[-800:]
                except CaoError:
                    tail = ""
                add_msg(g, "system", "系统",
                        f"{m['name']} 在等待确认。可以 @{m['name']} 回复它（例如“同意”或“1”）。\n\n{tail}",
                        log=False)
            elif st in DONE and fresh and (m.get("saw_busy") or boundary is not None
                                           or time.time() - m.get("sent_at", 0) > 8):
                m["pending"] = False
                try:
                    reply = get_reply(m)
                except CaoError as e:
                    reply = f"（读取输出失败：{e}）"
                handle_reply(g, m, reply)
            elif st == "error" and fresh:
                m["pending"] = False
                add_msg(g, "system", "系统", f"{m['name']} 出错了，可在终端里查看。", log=False)
        if snapshot:
            save_state()


# ───────────────────────── 业务操作 ─────────────────────────
def clean_member(raw: dict, existing: list[dict]) -> dict:
    name = (raw.get("name") or "").strip()
    if not name:
        raise ValueError("成员需要名字")
    if any(x["name"] == name for x in existing if x.get("id") != raw.get("id")):
        raise ValueError(f"群里已经有叫 {name} 的成员")
    tools = [t for t in raw.get("tools") or [] if isinstance(t, str) and t.strip()]
    provider = raw.get("provider") or "claude_code"
    effort = clean_effort(provider, raw.get("effort"))
    return {
        "id": raw.get("id") or slug(name) + "_" + uuid.uuid4().hex[:4],
        "name": name,
        "provider": provider,
        "effort": effort,
        "model": (raw.get("model") or "").strip(),
        "plan": (raw.get("plan") or "").strip(),
        "role": raw.get("role") or "developer",
        "tools": tools,
        "duty": (raw.get("duty") or "").strip(),
        "permission_mode": raw.get("permission_mode") or "",
        "color": raw.get("color") or "",
        "terminal_id": None,
        "status": "new",
    }


def run_bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def bootstrap_members(g: dict, members: list[dict], announce: bool) -> None:
    for m in members:
        add_msg(g, "system", "系统", f"正在启动 {m['name']}（{m['provider']}）…", log=False)
        try:
            start_member(g, m)
            add_msg(g, "system", "系统", f"{m['name']} 已入群，终端 {m['terminal_id']}。", log=False)
            append_log(g, "群主", f"邀请 {m['name']} 入群：provider={m['provider']}，角色={m['role']}，"
                                  f"权限={','.join(member_tools(m))}，职责={m['duty'] or '未指定'}")
        except Exception as e:  # noqa: BLE001
            m["status"] = "failed"
            add_msg(g, "system", "系统", f"{m['name']} 启动失败：{e}", log=False)
    if announce:
        names = "、".join(x["name"] for x in members)
        for x in g["members"]:
            if x not in members and x.get("terminal_id"):
                info = "；".join(f"@{m['name']}：{m['duty'] or m['role']}" for m in members)
                send_to(g, x, f"[群主] 新成员 {names} 入群。{info}。只需回复“收到”。", MAX_RELAY_HOPS, silent=True)
    save_state()


def create_group(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    workdir = (body.get("workdir") or "").strip()
    if not name or not workdir:
        raise ValueError("群名和工作目录必填")
    wd = Path(workdir).expanduser()
    wd.mkdir(parents=True, exist_ok=True)
    gid = uuid.uuid4().hex[:8]
    g = {
        "id": gid, "name": name, "workdir": str(wd), "created": now_iso(),
        "session_slug": f"grp-{slug(name)[:20]}-{gid[:4]}", "session_name": None,
        "members": [], "messages": [],
    }
    for raw in body.get("members") or []:
        g["members"].append(clean_member(raw, g["members"]))
    with lock:
        groups[gid] = g
    append_log(g, "群主", f"创建项目群「{name}」，工作目录 {wd}")
    save_state()
    run_bg(bootstrap_members, g, list(g["members"]), False)
    return g


def post_message(g: dict, text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("消息为空")
    targets = find_mentions(g, text)
    if re.search(r"@(所有人|all)\b", text, flags=re.I) or not targets:
        targets = [x for x in g["members"] if x.get("terminal_id")]
    msg = add_msg(g, "owner", "群主", text, to=[t["name"] for t in targets])
    for t in targets:
        run_bg(send_to, g, t, f"[群主] {text}", 0)
    return msg


def public_group(g: dict, since: int = 0) -> dict:
    out = {k: v for k, v in g.items() if k != "messages"}
    out["messages"] = [m for m in g["messages"] if m["seq"] > since]
    out["log_file"] = str(log_path(g))
    out["members"] = [{**m, "effective_tools": member_tools(m)} for m in g["members"]]
    return out


# ───────────────────────── 用量与开销 ─────────────────────────
# 数据全部来自各 CLI 写在本机的日志，不联网、不需要密钥：
#   Claude Code：~/.claude/projects/<工作目录编码>/*.jsonl，每条回复带 token 用量（没有订阅剩余额度）
#   Codex：~/.codex/sessions/**/rollout-*.jsonl，带 token 用量和账号的额度窗口（rate_limits）
# 用系统提示词里的「项目群「群名」里的成员 **名字**」认领日志文件，所以同一目录下多个成员也分得开。
#
# 价格：美元 / 百万 token。Claude 为 Anthropic 一方 API 标价；缓存写入按 5 分钟 1.25 倍、
# 1 小时 2 倍输入价估算。其他模型（如 Codex 的 gpt-*）没有内置价格，可在
# ~/.cao-groupchat/prices.json 里补充，格式同下：{"模型": {"in":..,"out":..,"cache_read":..}}
PRICES = {
    "claude-fable-5-2": {"in": 8.0, "out": 40.0, "cache_read": 0.20},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0},
    "claude-fable-5": {"in": 10.0, "out": 50.0},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"in": 5.0, "out": 25.0},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0},
    "claude-opus-4-7": {"in": 5.0, "out": 25.0},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_read": 0.10},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
    # Haiku 5.5：单次输入超过 10 万 token 时按 $0.50 / $2.50 计
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "long": {"over": 100_000, "in": 0.50, "out": 2.50}},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
}
SUBSCRIPTION_PLANS = {"pro", "max", "team", "chatgpt"}
_usage_cache: dict[str, tuple] = {}      # 文件路径 → (mtime, size, 解析结果)


def all_prices() -> dict:
    out = dict(PRICES)
    f = DATA_DIR / "prices.json"
    try:
        out.update(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return out


def price_of(model: str | None, prices: dict) -> dict | None:
    if not model:
        return None
    if model in prices:
        return prices[model]
    # 带日期后缀或厂商前缀的模型名，取最长的匹配前缀
    hits = [k for k in prices if model.startswith(k) or model.endswith(k)]
    return prices[max(hits, key=len)] if hits else None


def cost_of(model: str, u: dict, prices: dict) -> float | None:
    """u: in / cache_read / cache_w5m / cache_w1h / out（都是 token 数）。"""
    pr = price_of(model, prices)
    if not pr:
        return None
    prompt = u["in"] + u["cache_read"] + u["cache_w5m"] + u["cache_w1h"]
    long = pr.get("long")
    p_in, p_out = (long["in"], long["out"]) if long and prompt > long["over"] else (pr["in"], pr["out"])
    p_cr = pr.get("cache_read", p_in * 0.1)
    return (u["in"] * p_in + u["cache_w5m"] * p_in * 1.25 + u["cache_w1h"] * p_in * 2
            + u["cache_read"] * p_cr + u["out"] * p_out) / 1e6


def _cached_parse(path: Path, parser):
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _usage_cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        res = parser(path)
    except OSError:
        res = None
    _usage_cache[key] = (st.st_mtime, st.st_size, res)
    return res


def _member_marker(g: dict, m: dict) -> str:
    return f"项目群「{g['name']}」里的成员 **{m['name']}**"


def _parse_claude_log(path: Path) -> dict:
    """返回 {text 前缀用于认领, per_model: {model: tokens}}；同一条回复会分多行记录，按 message.id 去重。"""
    seen, per_model, head = set(), {}, []
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < 40:
                head.append(line)
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict) or not msg.get("usage"):
                continue
            mid = msg.get("id") or d.get("requestId") or d.get("uuid")
            if mid in seen:
                continue
            seen.add(mid)
            u, cc = msg["usage"], msg["usage"].get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
            w5m = cc.get("ephemeral_5m_input_tokens")
            if w5m is None:
                w5m = max((u.get("cache_creation_input_tokens") or 0) - w1h, 0)
            t = per_model.setdefault(msg.get("model") or "unknown",
                                     {"in": 0, "cache_read": 0, "cache_w5m": 0, "cache_w1h": 0, "out": 0, "turns": 0,
                                      "cost": 0.0, "priced": True})
            one = {"in": u.get("input_tokens") or 0, "cache_read": u.get("cache_read_input_tokens") or 0,
                   "cache_w5m": w5m or 0, "cache_w1h": w1h, "out": u.get("output_tokens") or 0}
            for k, v in one.items():
                t[k] += v
            t["turns"] += 1
            t["_raw"] = t.get("_raw", []) + [one]
    return {"head": "".join(head), "per_model": per_model}


def _parse_codex_log(path: Path) -> dict:
    cwd = model = None
    total = limits = limits_ts = None
    marker_text = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < 12:
                marker_text.append(line)
            if '"session_meta"' in line or '"turn_context"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pl = d.get("payload") or {}
                cwd = pl.get("cwd") or cwd
                model = pl.get("model") or model
            elif '"token_count"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pl = d.get("payload") or {}
                info = pl.get("info") or {}
                total = info.get("total_token_usage") or total
                if pl.get("rate_limits"):
                    limits, limits_ts = pl["rate_limits"], d.get("timestamp")
    return {"head": "".join(marker_text), "cwd": cwd, "model": model, "total": total,
            "limits": limits, "limits_ts": limits_ts}


def _since(g: dict) -> float:
    try:
        return datetime.strptime(g.get("created", ""), "%Y-%m-%d %H:%M:%S").timestamp() - 60
    except ValueError:
        return 0.0


def claude_usage(g: dict, m: dict, prices: dict) -> dict | None:
    wd = str(Path(g["workdir"]).expanduser().resolve())
    d = Path("~/.claude/projects").expanduser() / re.sub(r"[^A-Za-z0-9]", "-", wd)
    if not d.is_dir():
        return None
    marker, since = _member_marker(g, m), _since(g)
    agg: dict[str, dict] = {}
    for f in d.glob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        res = _cached_parse(f, _parse_claude_log)
        if not res or marker not in res["head"]:
            continue
        for model, t in res["per_model"].items():
            a = agg.setdefault(model, {"in": 0, "cache_read": 0, "cache_w5m": 0, "cache_w1h": 0, "out": 0,
                                       "turns": 0, "cost": 0.0, "priced": True})
            for k in ("in", "cache_read", "cache_w5m", "cache_w1h", "out", "turns"):
                a[k] += t[k]
            for one in t.get("_raw", []):
                c = cost_of(model, one, prices)
                if c is None:
                    a["priced"] = False
                else:
                    a["cost"] += c
    if not agg:
        return None
    return _summarize(agg)


def _codex_files(since: float) -> list[Path]:
    root = Path("~/.codex/sessions").expanduser()
    if not root.is_dir():
        return []
    out = []
    for f in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            if f.stat().st_mtime >= since:
                out.append(f)
        except OSError:
            pass
    return out


def codex_usage(g: dict, m: dict, prices: dict) -> dict | None:
    wd = str(Path(g["workdir"]).expanduser().resolve())
    marker = _member_marker(g, m)
    agg: dict[str, dict] = {}
    for f in _codex_files(_since(g)):
        res = _cached_parse(f, _parse_codex_log)
        if not res or not res["total"] or marker not in res["head"]:
            continue
        if res["cwd"] and str(Path(res["cwd"]).resolve()) != wd:
            continue
        t, model = res["total"], res["model"] or m.get("model") or "unknown"
        cached = t.get("cached_input_tokens") or 0
        one = {"in": max((t.get("input_tokens") or 0) - cached, 0), "cache_read": cached,
               "cache_w5m": t.get("cache_write_input_tokens") or 0, "cache_w1h": 0,
               "out": t.get("output_tokens") or 0}
        a = agg.setdefault(model, {"in": 0, "cache_read": 0, "cache_w5m": 0, "cache_w1h": 0, "out": 0,
                                   "turns": 0, "cost": 0.0, "priced": True})
        for k, v in one.items():
            a[k] += v
        a["turns"] += 1
        c = cost_of(model, one, prices)
        if c is None:
            a["priced"] = False
        else:
            a["cost"] += c
    return _summarize(agg) if agg else None


def codex_limits() -> dict | None:
    """Codex 账号当前的额度窗口：取最近一次记录。额度是整个账号共享的，不分成员。"""
    best = None
    for f in sorted(_codex_files(time.time() - 7 * 86400), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
        res = _cached_parse(f, _parse_codex_log)
        if res and res["limits"] and (not best or (res["limits_ts"] or "") > (best["limits_ts"] or "")):
            best = res
    if not best:
        return None
    lim, now = best["limits"], time.time()
    windows = []
    for key in ("primary", "secondary"):
        w = lim.get(key)
        if not w:
            continue
        resets = w.get("resets_at")
        windows.append({"used_percent": w.get("used_percent"), "window_minutes": w.get("window_minutes"),
                        "resets_at": resets, "resets_in": max(int(resets - now), 0) if resets else None,
                        "stale": bool(resets and resets < now)})
    cr = lim.get("credits") or {}
    return {"windows": windows, "observed_at": best["limits_ts"],
            "credits": {"has": cr.get("has_credits"), "unlimited": cr.get("unlimited"), "balance": cr.get("balance")}}


def _summarize(agg: dict) -> dict:
    models = []
    tot = {"in": 0, "cache_read": 0, "cache_write": 0, "out": 0, "turns": 0}
    cost, priced = 0.0, True
    for model, a in agg.items():
        models.append({"model": model, "cost": round(a["cost"], 6) if a["priced"] else None,
                       "tokens": {"in": a["in"], "cache_read": a["cache_read"],
                                  "cache_write": a["cache_w5m"] + a["cache_w1h"], "out": a["out"]},
                       "turns": a["turns"]})
        tot["in"] += a["in"]
        tot["cache_read"] += a["cache_read"]
        tot["cache_write"] += a["cache_w5m"] + a["cache_w1h"]
        tot["out"] += a["out"]
        tot["turns"] += a["turns"]
        cost += a["cost"]
        priced = priced and a["priced"]
    return {"models": models, "tokens": tot, "cost": round(cost, 6) if priced else None,
            "partial_cost": round(cost, 6)}


def group_usage(g: dict) -> dict:
    prices = all_prices()
    members, total, priced = {}, 0.0, True
    limits = None
    for m in g["members"]:
        prov, plan = m.get("provider"), m.get("plan") or ""
        if prov == "claude_code":
            u = claude_usage(g, m, prices)
        elif prov == "codex":
            u = codex_usage(g, m, prices)
            if limits is None:
                limits = codex_limits()
        else:
            u = None
        # 订阅（包月）不按次计费：显示额度；API 显示花费；没选计划时两者都给，前端标“参考”
        mode = "subscription" if plan in SUBSCRIPTION_PLANS else "api" if plan == "api" else "unknown"
        members[m["id"]] = {"provider": prov, "plan": plan, "mode": mode, "usage": u,
                            "limits": limits if prov == "codex" else None,
                            "limits_available": prov == "codex"}
        if u and mode == "api":
            if u["cost"] is None:
                priced = False
            total += u["partial_cost"]
    return {"members": members, "api_cost": round(total, 6), "api_cost_complete": priced,
            "prices_file": str(DATA_DIR / "prices.json")}


# ───────────────────────── 选择文件夹 ─────────────────────────
PICK_SCRIPT = """
on run argv
    activate
    set startDir to POSIX file (item 1 of argv)
    set p to choose folder with prompt "选择项目群的工作目录（可点“新建文件夹”）" default location startDir
    return POSIX path of p
end run
"""


def nearest_dir(raw: str | None) -> Path:
    """把用户输入的路径归一到一个真实存在的目录（不存在就往上找），默认家目录。"""
    p = Path((raw or "~").strip() or "~").expanduser()
    while not p.is_dir() and p != p.parent:
        p = p.parent
    return p if p.is_dir() else Path.home()


def pick_folder_native(start: str | None) -> dict:
    """弹出系统原生的选择文件夹对话框（目前只支持 macOS），会一直阻塞到用户选完。"""
    if not shutil.which("osascript"):
        return {"unsupported": True}
    r = subprocess.run(["osascript", "-e", PICK_SCRIPT, str(nearest_dir(start))],
                       capture_output=True, text=True, timeout=600)
    if r.returncode == 0:
        return {"path": r.stdout.strip().rstrip("/") or "/"}
    if "-128" in r.stderr:            # 用户点了取消
        return {"cancelled": True}
    return {"unsupported": True, "error": r.stderr.strip()[:300]}


def list_dirs(raw: str | None) -> dict:
    d = nearest_dir(raw).resolve()
    try:
        names = sorted((e.name for e in os.scandir(d)
                        if e.is_dir() and not e.name.startswith(".")), key=str.lower)
    except OSError as e:
        raise ValueError(f"无法读取 {d}：{e.strerror}") from None
    return {"path": str(d), "parent": str(d.parent) if d != d.parent else None,
            "home": str(Path.home()), "dirs": names}


def make_dir(parent: str, name: str) -> dict:
    name = (name or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("文件夹名不合法")
    base = Path(parent).expanduser()
    if not base.is_dir():
        raise ValueError(f"上级目录不存在：{parent}")
    d = base / name
    d.mkdir()                          # 已存在时抛 FileExistsError
    return {"path": str(d)}


# ───────────────────────── HTTP ─────────────────────────
class H(BaseHTTPRequestHandler):
    server_version = "CaoGroupChat/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _route(self, method: str):
        u = parse.urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse.parse_qs(u.query)
        try:
            if method == "GET" and (u.path in ("/", "/index.html")):
                data = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parts[:1] != ["api"]:
                return self._json(404, {"error": "not found"})
            p = parts[1:]
            if p == ["health"]:
                return self._json(200, {"ok": True, "cao": cao_alive(), "cao_url": CAO_URL,
                                        "providers": providers_with_install(), "roles": ROLES,
                                        "models": model_catalog()})
            if p == ["pick-folder"] and method == "POST":
                return self._json(200, pick_folder_native(self._body().get("start")))
            if p == ["fs"] and method == "GET":
                return self._json(200, list_dirs((qs.get("path") or [""])[0]))
            if p == ["fs", "mkdir"] and method == "POST":
                b = self._body()
                try:
                    return self._json(201, make_dir(b.get("parent", ""), b.get("name", "")))
                except FileExistsError:
                    return self._json(409, {"error": "已经有同名文件夹"})
            if p == ["groups"] and method == "GET":
                with lock:
                    return self._json(200, [
                        {"id": g["id"], "name": g["name"], "members": len(g["members"]),
                         "last": (g["messages"][-1] if g["messages"] else None)}
                        for g in groups.values()])
            if p == ["groups"] and method == "POST":
                return self._json(201, public_group(create_group(self._body())))
            if len(p) >= 2 and p[0] == "groups":
                g = groups.get(p[1])
                if not g:
                    return self._json(404, {"error": "群不存在"})
                rest = p[2:]
                if not rest and method == "GET":
                    since = int((qs.get("since") or ["0"])[0])
                    with lock:
                        return self._json(200, public_group(g, since))
                if not rest and method == "DELETE":
                    for m in g["members"]:
                        stop_member(m)
                    if g.get("session_name"):
                        try:
                            cao("DELETE", f"/sessions/{g['session_name']}")
                        except CaoError:
                            pass
                    append_log(g, "群主", "解散项目群")
                    with lock:
                        groups.pop(g["id"], None)
                    save_state()
                    return self._json(200, {"ok": True})
                if rest == ["messages"] and method == "POST":
                    return self._json(201, post_message(g, self._body().get("text", "")))
                if rest == ["usage"] and method == "GET":
                    return self._json(200, group_usage(g))
                if rest == ["log"] and method == "GET":
                    lp = log_path(g)
                    txt = lp.read_text(encoding="utf-8") if lp.exists() else ""
                    return self._json(200, {"path": str(lp), "content": txt})
                if rest == ["members"] and method == "POST":
                    m = clean_member(self._body(), g["members"])
                    g["members"].append(m)
                    save_state()
                    run_bg(bootstrap_members, g, [m], True)
                    return self._json(201, m)
                if len(rest) == 2 and rest[0] == "members":
                    m = next((x for x in g["members"] if x["id"] == rest[1]), None)
                    if not m:
                        return self._json(404, {"error": "成员不存在"})
                    if method == "DELETE":
                        stop_member(m)
                        g["members"].remove(m)
                        append_log(g, "群主", f"将 {m['name']} 移出群")
                        add_msg(g, "system", "系统", f"{m['name']} 已被移出群。", log=False)
                        return self._json(200, {"ok": True})
                    if method == "PUT":
                        b = self._body()
                        b["id"] = m["id"]
                        new = clean_member(b, g["members"])
                        stop_member(m)
                        g["members"][g["members"].index(m)] = new
                        append_log(g, "群主", f"调整 {new['name']} 的配置并重启："
                                             f"角色={new['role']}，权限={','.join(member_tools(new))}，"
                                             f"职责={new['duty'] or '未指定'}")
                        run_bg(bootstrap_members, g, [new], False)
                        return self._json(200, new)
                if len(rest) == 3 and rest[0] == "members" and rest[2] == "restart" and method == "POST":
                    m = next((x for x in g["members"] if x["id"] == rest[1]), None)
                    if not m:
                        return self._json(404, {"error": "成员不存在"})
                    stop_member(m)
                    run_bg(bootstrap_members, g, [m], False)
                    return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except CaoError as e:
            return self._json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


def main() -> None:
    load_state()
    threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"CAO 群聊 GUI 已启动：http://localhost:{PORT}")
    print(f"CAO 地址：{CAO_URL}（{'已连接' if cao_alive() else '未连接，请先运行 cao-server'}）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
