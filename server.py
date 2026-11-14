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
    GROUPCHAT_POLL_SECONDS       轮询间隔，默认 2 秒
    GROUPCHAT_READY_SECONDS      就绪特征稳定时长，默认 0.75 秒
    GROUPCHAT_STARTUP_TIMEOUT    启动等待时限，默认 180 秒
    GROUPCHAT_DELIVERY_TIMEOUT   投递无进展时限，默认 120 秒
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
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

def seconds_setting(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是正数秒数")
    return value


POLL_SECONDS = seconds_setting("GROUPCHAT_POLL_SECONDS", 2.0)
READY_SECONDS = seconds_setting("GROUPCHAT_READY_SECONDS", 0.75)
STARTUP_TIMEOUT = seconds_setting("GROUPCHAT_STARTUP_TIMEOUT", 180)
DELIVERY_TIMEOUT = seconds_setting("GROUPCHAT_DELIVERY_TIMEOUT", 120)
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
delivery_locks: dict[tuple[str, str], threading.RLock] = {}
session_start_locks: dict[str, threading.RLock] = {}


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
            for group in groups.values():
                for member in group["members"]:
                    member["permission_mode"] = clean_permission_mode(
                        member.get("provider") or "claude_code", member.get("permission_mode"), strict=False)
                    if member.get("delivery_state") == "dispatching":
                        # The process may have died after CAO accepted the input.
                        # Keep polling and permit manual recovery; never auto-resend.
                        member.update(delivery_state="uncertain", pending=True, delivery_warned=False)
                if "automation_state" in group:
                    # Downtime is not an observation of continuous group idleness.
                    group["automation_state"]["idle_since"] = None
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


def clean_permission_mode(provider: str, value, *, strict: bool = True) -> str:
    if provider != "claude_code":
        return ""
    if isinstance(value, str) and value in ("", "acceptEdits", "plan"):
        return value
    if strict:
        raise ValueError("Claude Code 权限模式只支持默认（空字符串）、acceptEdits 或 plan")
    # Previously saved or in-memory legacy values must not break CLI startup.
    return ""


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
    permission_mode = clean_permission_mode(m["provider"], m.get("permission_mode"), strict=False)
    if permission_mode:
        fm.append(f"permissionMode: {permission_mode}")
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
def session_start_lock(g: dict):
    # Never hold the global state lock across CAO I/O; different groups run apart.
    with lock:
        return session_start_locks.setdefault(g["id"], threading.RLock())


def start_group_terminal(g: dict, m: dict, query: dict) -> dict:
    """Called under the group's startup lock, through terminal-id publication."""
    session = g.get("session_name") or g["session_slug"]
    force_create = False
    for attempt in range(2):
        exists = False
        if not force_create:
            try:
                response = cao("GET", f"/sessions/{session}")
                # CAO preserves deferred-init failure records after tmux exits.
                # Its real response wraps metadata in "session"; accept flat
                # metadata too for older adapters.
                info = response.get("session") or response
                exists = str(info.get("status", "")).lower() != "terminated"
            except CaoError as e:
                if e.status != 404:
                    raise
        if not exists:
            try:
                terminal = cao("POST", "/sessions", {**query, "session_name": session},
                               body={"description": f"{g['name']} / {m['name']}"}, timeout=180)
            except CaoError as e:
                # Another process may have created it after our existence probe.
                if e.status != 400 or f"Session '{session}' already exists" not in str(e):
                    raise
            else:
                g["session_name"] = terminal.get("session_name") or session
                return terminal
        g["session_name"] = session
        path = f"/sessions/{session}/terminals"
        live = [x for x in g["members"] if x is not m and x.get("terminal_id")]
        try:
            try:
                return cao("POST", path + ":operator", query, body={"description": m["name"]}, timeout=180)
            except CaoError as e:
                if e.status == 404 and not attempt:
                    raise  # Rebuild first, even if local terminal IDs are stale.
                if e.status not in (404, 405) or not live:
                    raise
                # Older CAO versions require a live caller instead of :operator.
                return cao("POST", path, {**query, "caller_id": live[0]["terminal_id"]},
                           body={"description": m["name"]}, timeout=180)
        except CaoError as e:
            if e.status != 404 or attempt:
                raise
            # A successful GET may keep returning a terminated registry shell.
            # After join 404, try creation directly once instead of querying again.
            force_create = True


def start_member(g: dict, m: dict) -> None:
    p = next((x for x in providers_with_install() if x["id"] == m["provider"]), None)
    if p and p.get("installed") is False:
        raise RuntimeError(f"本机没有找到 `{p['binary']}` 命令（{p['label']} 未安装或不在 PATH 里）。"
                           f"请先安装并登录，然后在成员面板里重启该成员。")
    if m["provider"] == "claude_code":
        with lock:
            if automation_settings(g)["claude_limits"]:
                optional_claude_statusline(g, True)
    install_profile(g, m)
    q = {
        "agent_profile": profile_name(g, m),
        "provider": m["provider"],
        "working_directory": str(Path(g["workdir"]).expanduser()),
        "allowed_tools": ",".join(member_tools(m)),
        "model": m.get("model") or None,
    }
    with session_start_lock(g):
        t = start_group_terminal(g, m, q)
        with delivery_lock(g, m):
            m["terminal_id"] = t.get("id") or t.get("terminal_id")
            m["status"] = t.get("status", "starting")
            m["ready_terminal"] = None
            m.pop("ready_candidate", None)
            m["pending"] = False
            if m.get("active_delivery"):
                m["delivery_state"] = "failed"
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
    m.update(status="stopped", pending=False, ready_terminal=None)
    if m.get("active_delivery"):
        m["delivery_state"] = "failed"


def delivery_lock(g: dict, m: dict):
    # Serializes only this member's queue/input/retry, not group session creation.
    with lock:
        return delivery_locks.setdefault((g["id"], m["id"]), threading.RLock())


# CAO's composer extractor treats Try "..." as empty input, including dim ANSI.
CLAUDE_INPUT_BOX = re.compile(
    r'─{8,}[^\n]*\n(?:[ \t\xa0]*\n){0,2}[ \t]*[>❯]'
    r'(?:[ \t\xa0]+Try "[^"\n]*")?[ \t\xa0]*\n'
    r'(?:[ \t\xa0]*\n){0,2}[ \t]*─{8,}', re.M)
CODEX_READY_FOOTER = re.compile(r"(?:\?\s+for shortcuts|context left|\d{1,3}%\s+left|·\s+[~/])")


def member_ready(m: dict, terminal: dict, allow_status_only: bool = False) -> bool:
    tid = m.get("terminal_id")
    if not tid:
        return False
    if m.get("ready_terminal") == tid:
        return True
    status = str(terminal.get("status", "")).lower()
    if status not in DONE:
        m.pop("ready_candidate", None)
        return False
    if allow_status_only:
        # Only an explicit retry of a startup timeout may waive the feature gate.
        # Keep two observations, stable status and the minimum elapsed interval.
        fingerprint = "manual-status:" + status
    else:
        raw = get_full_output(m)
        tail = "\n".join(ANSI.sub("", raw).replace("\r", "\n").splitlines()[-12:])
        provider = m.get("provider")
        states = codex_states(raw)
        if provider == "codex":
            positive = bool(CODEX_READY_FOOTER.search(tail)) and not (states and states[-1] == "working")
        elif provider == "claude_code":
            legacy = (not re.search(r"─{8,}", tail) and
                      re.search(r"(?:^|\n)[>❯][ \t\xa0]*$", tail))
            positive = bool(CLAUDE_INPUT_BOX.search(tail) or legacy)
        else:
            positive = bool(tail.strip())
        if not positive:
            m.pop("ready_candidate", None)
            return False
        fingerprint = hashlib.sha256(tail.encode()).hexdigest()
    candidate = m.get("ready_candidate", {})
    now = time.time()
    if candidate.get("terminal") != tid or candidate.get("hash") != fingerprint:
        m["ready_candidate"] = {"terminal": tid, "hash": fingerprint, "since": now}
        return False
    if now - candidate["since"] < READY_SECONDS:
        return False
    m["ready_terminal"] = tid
    m.pop("ready_candidate", None)
    return True


def runtime_notice(g: dict, m: dict, text: str, exc: Exception) -> None:
    # add_msg appends in memory before saving; a broken disk must not hide the
    # recovery state or kill polling. Do not log exception bodies or chat text.
    print(f"group={g['id']} member={m['id']} error={type(exc).__name__}", flush=True)
    try:
        add_msg(g, "system", "系统", text, log=False)
    except Exception as persist_error:
        print(f"group={g['id']} member={m['id']} persistence_error={type(persist_error).__name__}", flush=True)


def send_to(g: dict, m: dict, text: str, hops: int, silent: bool = False) -> None:
    with delivery_lock(g, m):
        with lock:
            queue_delivery(g, m, new_delivery(text, hops, silent))
            save_state()
        flush_outbox(g, m)


def new_delivery(text: str, hops: int, silent: bool = False) -> dict:
    return {"id": uuid.uuid4().hex, "attempt": 0, "queued_at": time.time(),
            "text": text, "hops": hops, "silent": silent}


def queue_delivery(g: dict, m: dict, job: dict) -> None:
    # Caller holds this member's delivery lock, then the global state lock.
    if m.get("pending") and m.get("status") == "waiting_user_answer" and job["hops"] == 0 and not job["silent"]:
        m.update(active_delivery=None, pending=False, delivery_state="waiting_ready")
        m.setdefault("outbox", []).insert(0, job)
    else:
        m.setdefault("outbox", []).append(job)
    if not m.get("active_delivery") and m.get("delivery_state") not in ("not_ready", "budget_paused"):
        m["delivery_state"] = "waiting_ready"
    if "automation_state" in g:
        g["automation_state"]["idle_since"] = None


def flush_outbox(g: dict, m: dict, terminal: dict | None = None) -> bool:
    with delivery_lock(g, m):
        queue = m.get("outbox", [])
        if not queue or m.get("active_delivery") or m.get("delivery_state") == "not_ready":
            return False
        with lock:
            if not budget_available(g):
                m["delivery_state"] = "budget_paused"
                budget_notice(g)
                save_state()
                return False
            if m.get("delivery_state") == "budget_paused":
                # A budget pause must not consume the CLI startup timeout.
                queue[0]["queued_at"] = time.time()
                m["delivery_state"] = "waiting_ready"
        if time.time() - queue[0]["queued_at"] >= STARTUP_TIMEOUT and not m.get("terminal_id"):
            ready = False
        else:
            try:
                if m.get("ready_terminal") == m.get("terminal_id") and m.get("terminal_id"):
                    ready = True
                elif m.get("terminal_id"):
                    observation = terminal if terminal is not None else cao("GET", f"/terminals/{m['terminal_id']}", timeout=5)
                    ready = member_ready(m, observation, allow_status_only=queue[0].get("status_only_retry", False))
                else:
                    ready = False
            except CaoError:
                ready = False
        if not ready:
            if time.time() - queue[0]["queued_at"] >= STARTUP_TIMEOUT:
                m["delivery_state"] = "not_ready"
                add_msg(g, "system", "系统", f"{m['name']} 启动尚未就绪，消息尚未投递；处理终端后可重试投递。", log=False)
            save_state()
            return False
        with lock:
            job = queue.pop(0)
            m["active_delivery"] = job
        return dispatch_delivery(g, m, job)


def dispatch_delivery(g: dict, m: dict, job: dict) -> bool:
    with lock:
        job["attempt"] += 1
        m.update(active_delivery=job, pending=True, delivery_state="dispatching", delivery_warned=False)
    attempted = False
    charged = False
    try:
        raw = get_full_output(m) if m.get("provider") == "codex" else None
        with lock:
            m.update(boundary=None, reply_handled=False, sent_at=time.time(), saw_busy=False,
                     hops=job["hops"], silent=job["silent"], dispatch_confirmed=False,
                     delivery_started=False, last_progress_at=time.time(), progress_generation=None)
            m.pop("progress_hash", None)
            if raw is not None:
                states = codex_states(raw)
                m.update(codex_dispatch_markers=len(states), codex_dispatch_state=states[-1] if states else None,
                         codex_saw_working=False, codex_snapshot_len=len(raw),
                         codex_snapshot_hash=hashlib.sha256(raw.encode()).hexdigest(),
                         codex_buffer_reset=False, full_len=len(raw))
            # Reserve globally across member locks, immediately before input.
            if not budget_available(g):
                job["attempt"] -= 1
                m.setdefault("outbox", []).insert(0, job)
                m.update(active_delivery=None, pending=False, delivery_state="budget_paused")
                budget_notice(g)
                save_state()
                return False
            if "automation" in g:
                automation_state(g)["deliveries_used"] += 1
                charged = True
            save_state()  # Persist the reservation and in-flight job before input.
        attempted = True
        r = cao("POST", f"/terminals/{m['terminal_id']}/input", {"message": job["text"]})
        # Fresh statuses belong to this dispatch only above CAO's boundary.
        with lock:
            m["boundary"] = r.get("dispatch_boundary") if isinstance(r, dict) else None
            m.update(delivery_state="sent", dispatch_confirmed=True, progress_generation=m["boundary"])
            save_state()
    except Exception as e:
        ambiguous = attempted and (not isinstance(e, CaoError) or e.status == 0 or e.status >= 500)
        if charged and not ambiguous:
            with lock:
                automation_state(g)["deliveries_used"] -= 1
                charged = False
        m.update(pending=ambiguous, delivery_state="uncertain" if ambiguous else "failed",
                 delivery_warned=True, sent_at=time.time(), last_progress_at=time.time(),
                 delivery_started=False)
        note = "可能未送达，可重发（也可能已送达，请先核对终端）" if ambiguous else "投递失败，可重试"
        detail = str(e) if isinstance(e, CaoError) else type(e).__name__
        runtime_notice(g, m, f"发给 {m['name']} 失败：{detail}；{note}。", e)
    if charged:
        budget_notice(g)
    return True


def watch_delivery(g: dict, m: dict, terminal: dict) -> None:
    if not m.get("active_delivery") or not m.get("pending") or m.get("delivery_state") == "dispatching":
        return
    now = time.time()
    st = str(terminal.get("status", "")).lower()
    gen, boundary = terminal.get("status_generation"), m.get("boundary")
    fresh = gen is None or boundary is None or gen > boundary
    started = (m.get("codex_saw_working") if m.get("provider") == "codex" else fresh and st in BUSY)
    if fresh and st == "waiting_user_answer":
        m.update(delivery_started=True, last_progress_at=now, delivery_state="waiting_user")
        return
    output_gen = terminal.get("output_generation")
    progressed = False
    if started and not m.get("delivery_started"):
        progressed = True
        m.update(delivery_started=True, last_progress_at=now)
    if m.get("delivery_started"):
        if isinstance(output_gen, int) and (boundary is None or output_gen > boundary):
            if output_gen != m.get("progress_generation"):
                progressed = True
                m.update(progress_generation=output_gen, last_progress_at=now)
        elif output_gen is None and m.get("terminal_id"):
            try:
                digest = hashlib.sha256(get_full_output(m).encode()).hexdigest()
                if digest != m.get("progress_hash"):
                    progressed = True
                    m.update(progress_hash=digest, last_progress_at=now)
            except CaoError:
                pass
    if now - m.get("last_progress_at", m.get("sent_at", now)) >= DELIVERY_TIMEOUT:
        m["delivery_state"] = "uncertain"
        if not m.get("delivery_warned"):
            m["delivery_warned"] = True
            add_msg(g, "system", "系统", f"{m['name']} 长时间没有本轮进展，消息可能未送达。可在成员卡片重发；也可能仍在执行，请先核对终端，避免重复操作。", log=False)
    elif progressed and m.get("delivery_state") in ("uncertain", "waiting_user"):
        m.update(delivery_state="sent", delivery_warned=False)


def retry_delivery(g: dict, m: dict, token: dict) -> None:
    with delivery_lock(g, m):
        job = m.get("active_delivery") or next(iter(m.get("outbox", [])), None)
        if (m.get("delivery_state") not in ("uncertain", "failed", "not_ready") or not job
                or token.get("delivery_id") != job["id"] or token.get("delivery_attempt") != job["attempt"]):
            raise ValueError("投递状态已改变，请刷新后再试")
        if m.get("delivery_state") == "not_ready":
            job["status_only_retry"] = True
        if m.get("active_delivery"):
            m.setdefault("outbox", []).insert(0, job)
        job["queued_at"] = time.time()
        m.update(active_delivery=None, pending=False, delivery_state="waiting_ready", delivery_warned=False)
        # An uncertain terminal may still be working; require a new idle prompt.
        m["ready_terminal"] = None
        m.pop("ready_candidate", None)
        save_state()
        flush_outbox(g, m)


def find_mentions(g: dict, text: str, exclude: str | None = None) -> list[dict]:
    names = sorted((x["name"] for x in g["members"]), key=len, reverse=True)
    if not names:
        return []
    # Resolve the longest known name before checking its suffix, including dev/dev-team.
    pattern = r"@(" + "|".join(re.escape(name) for name in names) + r")"
    mentioned = {match.group(1) for match in re.finditer(pattern, text)
                 if not re.match(r"[\w一-鿿]", text[match.end():match.end() + 1])}
    return [x for x in g["members"] if x["id"] != exclude and x["name"] in mentioned]


def get_full_output(m: dict) -> str:
    result = cao("GET", f"/terminals/{m['terminal_id']}/output", {"mode": "full"})
    raw = result.get("output") if isinstance(result, dict) else result
    if not isinstance(raw, str):
        raise CaoError(0, "无法读取终端原始输出")
    return raw


CODEX_STATE = re.compile(r"state=(\w+)(?=[:;\x07\x1b\r\n]|$)")


def codex_markers(raw: str) -> list[tuple[int, str | None]]:
    # Codex 0.164.0 uses state=...:app=codex + ST. Keep no-app/BEL support.
    # Retain incomplete/unknown final markers so an earlier done cannot win.
    markers = []
    for marker in re.finditer(re.escape("\x1b]7501;"), raw):
        state = CODEX_STATE.match(raw, marker.end())
        markers.append((marker.start(), state.group(1) if state else None))
    return markers


def codex_states(raw: str) -> list[str | None]:
    return [state for _, state in codex_markers(raw)]


def get_codex_reply(m: dict, output_generation: int | None = None) -> str | None:
    boundary = m.get("codex_dispatch_markers")
    if boundary is None:
        return None  # A pending turn from before this protocol has no proven boundary.
    raw = get_full_output(m)
    markers = codex_markers(raw)
    states = [state for _, state in markers]
    snapshot_len, snapshot_hash = m.get("codex_snapshot_len"), m.get("codex_snapshot_hash")
    fresh_bytes = (isinstance(output_generation, int) and isinstance(m.get("boundary"), int)
                   and output_generation > m["boundary"])
    if snapshot_len is not None and snapshot_hash is not None:
        same_prefix = (len(raw) >= snapshot_len and
                       hashlib.sha256(raw[:snapshot_len].encode()).hexdigest() == snapshot_hash)
        # CAO clears its FIFO before input. A byte-identical fast reply is also
        # new when the output counter advanced; status_generation is not used.
        identical_new_turn = (len(raw) == snapshot_len and same_prefix and fresh_bytes
                              and states and states[-1] == "done" and "working" in states[:-1])
        if not same_prefix or identical_new_turn:
            m["codex_buffer_reset"] = True
    reset = m.get("codex_buffer_reset", False)
    dispatch_state = m.get("codex_dispatch_state")  # Immutable evidence of an already-active old turn.
    if reset:
        boundary = 0
        m["full_len"] = 0
    if not states:
        return None
    if states[-1] == "working":
        if len(states) > boundary or dispatch_state != "working":
            m["codex_saw_working"] = True
        return None
    if states[-1] != "done":
        return None
    if (dispatch_state == "working" and not m.get("codex_saw_working")
            and "working" not in states[boundary:]):
        # Discard the completion of the turn already working at dispatch time.
        if not reset:
            m["codex_dispatch_markers"] = len(states)
            m["full_len"] = len(raw)
        return None
    # Count new markers in an append-only buffer; a working->done observation also
    # handles a rolling terminal window that has discarded the old prefix.
    if len(states) <= boundary and not m.get("codex_saw_working"):
        return None
    current_working = "working" in states[boundary:-1]
    # A fast turn can redraw >32KB before the first poll and evict working.
    # A cleared buffer plus fresh bytes proves its remaining done is new, unless
    # the preceding turn was still working when we sent the input.
    fresh_reset_done = reset and fresh_bytes and dispatch_state != "working"
    if not m.get("codex_saw_working") and not current_working and not fresh_reset_done:
        return None
    if current_working:
        m["codex_saw_working"] = True
    if reset:
        starts = [start for start, state in markers if state == "working"]
        m["full_len"] = starts[-1] if starts else 0
    return get_reply(m)


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
        with lock:
            m["_reply_work_state"] = reply_work_state(clean_output(text, preserve_indent=True))
        return clean_output(text)
    return ""


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
TUI_FOOTER = re.compile(
    r"(?:✻ [^\W\d_]+ for (?:\d+[hms]\s*)+· done \d{1,2}:\d{2} (?:AM|PM)"
    r"|Worked for (?:\d+[hms]\s*)+• \d{2}:\d{2})"
)


def clean_output(s: str, *, preserve_indent: bool = False) -> str:
    s = ANSI.sub("", s).replace("\r", "")
    lines = (s.rstrip() if preserve_indent else s.strip()).splitlines()
    # Only remove complete footer lines at the end, preserving prose and code examples.
    while lines and (not lines[-1].strip() or TUI_FOOTER.fullmatch(lines[-1].strip())):
        lines.pop()
    s = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return s.rstrip() if preserve_indent else s.strip()


def handle_reply(g: dict, m: dict, text: str) -> None:
    if m.get("reply_handled"):
        return
    m.update(reply_handled=True, pending=False, active_delivery=None, delivery_state="completed",
             delivery_started=True, last_progress_at=time.time())
    reported_state = m.pop("_reply_work_state", None)
    if m.pop("silent", False):          # 系统通知（如新成员入群）的回复不进群聊
        return
    if not text:
        text = "（无文字输出）"
    hops = m.get("hops", 0)
    targets = find_mentions(g, text, exclude=m["id"])
    relay_limit = automation_settings(g)["relay_limit"]
    with lock:
        m["work_state"] = reported_state if reported_state is not None else reply_work_state(text)
        if "automation" in g and (re.search(r"@群主(?![\w一-鿿])", text) or (targets and hops >= relay_limit)):
            automation_state(g).update(awaiting_owner=True, idle_since=None)
    add_msg(g, "agent", m["name"], text, mid=m["id"])
    for line in re.findall(r"^[^\S\r\n]*\[日志\][^\S\r\n]*(\S[^\r\n]*)\r?$", text, flags=re.M):
        append_log(g, m["name"], line)
    if targets and hops < relay_limit:
        for t in targets:
            send_to(g, t, f"[来自 {m['name']}] {text}", hops + 1)
    elif targets:
        add_msg(g, "system", "系统",
                f"已达到自动转发上限（{relay_limit} 次），请群主决定下一步。", log=False)


def poll_member(g: dict, m: dict) -> None:
    if not m.get("terminal_id"):
        flush_outbox(g, m)
        return
    try:
        t = cao("GET", f"/terminals/{m['terminal_id']}", timeout=5)
    except CaoError as e:
        if automation_settings(g)["idle_minutes"]:
            m["autonomy_poll_ok"] = False
        if e.status == 404:
            m.update(status="gone", terminal_id=None, ready_terminal=None, pending=False)
            if m.get("active_delivery"):
                m["delivery_state"] = "failed"
            add_msg(g, "system", "系统", f"{m['name']} 的终端已不存在。", log=False)
        else:
            watch_delivery(g, m, {})
        return
    if automation_settings(g)["idle_minutes"]:
        m["autonomy_poll_ok"] = True
    st = str(t.get("status", "")).lower()
    prev = m.get("status")
    gen, boundary = t.get("status_generation"), m.get("boundary")
    fresh = gen is None or boundary is None or gen > boundary
    m["status"] = st
    if not m.get("pending"):
        if m.get("outbox"):
            flush_outbox(g, m, t)
        elif m.get("ready_terminal") != m.get("terminal_id"):
            try:
                member_ready(m, t)
            except CaoError:
                pass
        return  # This observation predates any new dispatch.
    if m.get("delivery_state") == "dispatching":
        # Polling owns this member's lock, so a live dispatch cannot be running.
        m.update(delivery_state="uncertain", delivery_warned=True)
        runtime_notice(g, m, f"{m['name']} 的投递未正常结束，消息可能未送达。请核对终端后重发。",
                       RuntimeError("interrupted dispatch"))
        return
    if m.get("provider") == "codex" and st not in ("waiting_user_answer", "error"):
        m["status"] = "processing"
        try:
            reply = get_codex_reply(m, output_generation=t.get("output_generation"))
        except CaoError:
            reply = None  # A failed probe is not evidence of completion.
        if reply is not None:
            m["status"] = "completed"
            handle_reply(g, m, reply)
    elif st in BUSY:
        m["saw_busy"] = True
    elif st == "waiting_user_answer" and prev != st and fresh:
        try:
            tail = get_reply(m)[-800:]
        except CaoError:
            tail = ""
        add_msg(g, "system", "系统",
                f"{m['name']} 在等待确认。可以 @{m['name']} 回复它（例如“同意”或“1”）。\n\n{tail}", log=False)
    elif (st in DONE and fresh and (m.get("dispatch_confirmed") is not False or m.get("delivery_started"))
          and (m.get("saw_busy") or boundary is not None or time.time() - m.get("sent_at", 0) > 8)):
        try:
            reply = get_reply(m)
        except CaoError:
            watch_delivery(g, m, t)
            return
        handle_reply(g, m, reply)
    elif st == "error" and fresh:
        m.update(pending=False, delivery_state="failed")
        add_msg(g, "system", "系统", f"{m['name']} 出错了，可在终端里查看。", log=False)
    watch_delivery(g, m, t)
    if not m.get("pending"):
        flush_outbox(g, m)


def poll_loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        with lock:
            group_snapshot = list(groups.values())
            snapshot = [(g, m) for g in groups.values() for m in g["members"]
                        if m.get("terminal_id") or m.get("outbox") or m.get("active_delivery")]
        for g, m in snapshot:
            with delivery_lock(g, m):
                try:
                    poll_member(g, m)
                    m.pop("poll_error_type", None)
                except Exception as e:
                    if m.get("active_delivery"):
                        m["delivery_state"] = "uncertain"
                    elif m.get("outbox"):
                        m.update(delivery_state="not_ready", pending=False)
                    if m.get("poll_error_type") != type(e).__name__:
                        m["poll_error_type"] = type(e).__name__
                        runtime_notice(g, m, f"{m['name']} 的状态更新失败（{type(e).__name__}）；"
                                       "投递可能未送达，请核对终端后重试。", e)
        for g in group_snapshot:
            try:
                check_idle_reminder(g)
            except Exception as e:
                print(f"group={g['id']} reminder_error={type(e).__name__}", flush=True)
        if snapshot:
            try:
                save_state()
            except Exception as e:
                print(f"state_persistence_error={type(e).__name__}", flush=True)


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
        "permission_mode": clean_permission_mode(provider, raw.get("permission_mode", "")),
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
        if any(automation_settings(other)["claude_limits"] and Path(other["workdir"]).expanduser().resolve() == wd.resolve()
               for other in groups.values()):
            raise ValueError("该工作目录已有群开启 Claude 额度采集，请为新群使用独立目录")
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
        targets = list(g["members"])
    with lock:
        if "automation_state" in g:
            g["automation_state"].update(awaiting_owner=False, idle_since=None)
        msg = add_msg(g, "owner", "群主", text, to=[t["name"] for t in targets])
    for t in targets:
        run_bg(send_to, g, t, f"[群主] {text}", 0)
    return msg


def public_group(g: dict, since: int = 0) -> dict:
    out = {k: v for k, v in g.items() if k != "messages"}
    out["messages"] = [m for m in g["messages"] if m["seq"] > since]
    out["log_file"] = str(log_path(g))
    out["members"] = []
    for m in g["members"]:
        member = {k: v for k, v in m.items() if k not in ("outbox", "active_delivery", "_reply_work_state")}
        job = m.get("active_delivery") or next(iter(m.get("outbox", [])), None)
        member.update(effective_tools=member_tools(m), queued_count=len(m.get("outbox", [])),
                      delivery_id=job["id"] if job else None, delivery_attempt=job["attempt"] if job else None,
                      can_retry=bool(job and m.get("delivery_state") in ("uncertain", "failed", "not_ready")))
        out["members"].append(member)
    return out


# ───────────────────────── 按群自主运行（缺省保持原行为） ─────────────────────────
AUTOMATION_DEFAULTS = {
    "relay_limit": MAX_RELAY_HOPS, "delivery_budget": None, "idle_minutes": 0,
    "reminder_cooldown_minutes": 5, "reminder_limit": 3, "coordinator_id": None, "claude_limits": False,
}


def automation_settings(g: dict) -> dict:
    return {**AUTOMATION_DEFAULTS, **g.get("automation", {})}


def automation_state(g: dict) -> dict:
    return g.setdefault("automation_state", {"deliveries_used": 0, "budget_notified": False,
                                           "reminders_sent": 0, "last_reminder_at": None, "idle_since": None,
                                           "awaiting_owner": False})


def budget_available(g: dict) -> bool:
    limit = automation_settings(g)["delivery_budget"]
    return limit is None or g.get("automation_state", {}).get("deliveries_used", 0) < limit


def automation_view(g: dict) -> dict:
    with lock:
        state = {"deliveries_used": 0, "reminders_sent": 0, "last_reminder_at": None,
                 "idle_since": None, "awaiting_owner": False, **g.get("automation_state", {})}
        state.pop("budget_notified", None)
        state["budget_exhausted"] = not budget_available(g)
        return {"settings": automation_settings(g), "runtime": state}


def update_automation(g: dict, body: dict) -> dict:
    with lock:
        if not isinstance(body, dict) or any(key not in AUTOMATION_DEFAULTS for key in body):
            raise ValueError("自主运行设置必须是对象，且只能包含支持的配置字段")
        settings = {**automation_settings(g), **body}
        if type(settings["claude_limits"]) is not bool:
            raise ValueError("claude_limits 必须是布尔值")
        for key in ("relay_limit", "delivery_budget", "reminder_limit"):
            value = settings[key]
            if key == "delivery_budget" and value is None:
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} 必须是非负整数")
        for key in ("idle_minutes", "reminder_cooldown_minutes"):
            value = settings[key]
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or (key == "reminder_cooldown_minutes" and value == 0)):
                raise ValueError(f"{key} 必须是有限的{'正数' if key == 'reminder_cooldown_minutes' else '非负数'}分钟数")
        coordinator = settings["coordinator_id"]
        if ("coordinator_id" in body and coordinator is not None
                and not any(m["id"] == coordinator for m in g["members"])):
            raise ValueError("协调成员不存在，请使用本群成员 ID 或 null")
        restore_project, warning = lambda: None, None
        if "claude_limits" in body:
            if settings["claude_limits"]:
                restore_project = configure_claude_statusline(g, True)
            else:
                restore_project, warning = optional_claude_statusline(g, False)
        previous_settings, previous_state = g.get("automation"), g.get("automation_state")
        previous_state = dict(previous_state) if previous_state is not None else None
        try:
            if body and ("automation" not in g or settings != automation_settings(g)):
                g["automation"] = settings
                state = automation_state(g)
                state["idle_since"] = None
                if budget_available(g):
                    state["budget_notified"] = False
                save_state()
                budget_notice(g)
        except Exception:
            if previous_settings is None:
                g.pop("automation", None)
            else:
                g["automation"] = previous_settings
            if previous_state is None:
                g.pop("automation_state", None)
            else:
                g["automation_state"] = previous_state
            restore_project()
            raise
        result = automation_view(g)
        if warning:
            result["warnings"] = [warning]
        return result


def budget_notice(g: dict) -> None:
    with lock:
        if budget_available(g) or automation_state(g)["budget_notified"]:
            return
        state = automation_state(g)
        state["budget_notified"] = True
        add_msg(g, "system", "系统", f"本群投递预算已用完（{state['deliveries_used']}/"
                f"{automation_settings(g)['delivery_budget']}）；新消息保留在队列，调高预算后按成员队列顺序继续投递。", log=False)


def reply_work_state(text: str) -> str:
    lines = text.rstrip().splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    if first < len(lines) and lines[first].startswith("• "):
        # Codex renders the response with a bullet and two-space continuations.
        # Remove only that display margin; retain any relative Markdown indent.
        lines = lines[:first] + [lines[first][2:]] + [line[2:] if line.startswith("  ") else line for line in lines[first + 1:]]
    fence = None
    for line in lines[:-1]:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
    match = re.fullmatch(r" {0,3}状态：(进行中|等对方|等群主|阻塞|已交付)", lines[-1]) if lines and not fence else None
    return match.group(1) if match else "进行中"


def idle_coordinator(g: dict, settings: dict) -> dict | None:
    if g.get("automation_state", {}).get("awaiting_owner"):
        return None
    members = g["members"]
    for m in members:
        if (m.get("pending") or m.get("active_delivery") or m.get("outbox")
                or m.get("work_state", "进行中") in ("等群主", "阻塞", "已交付")
                or m.get("status") not in DONE | {"gone", "failed", "stopped", "error"}
                or m.get("poll_error_type") or (m.get("terminal_id") and m.get("autonomy_poll_ok") is False)):
            return None
    available = [m for m in members if m.get("terminal_id") and m.get("ready_terminal") == m["terminal_id"]
                 and m.get("status") in DONE]
    return next((m for m in available if m["id"] == settings["coordinator_id"]), next(iter(available), None))


def check_idle_reminder(g: dict, now: float | None = None) -> bool:
    with lock:
        settings = automation_settings(g)
        if not settings["idle_minutes"] or not settings["reminder_limit"]:
            return False
        target = idle_coordinator(g, settings)
        if target is None:
            automation_state(g)["idle_since"] = None
            return False
    # Never acquire a member lock while holding the global state lock.
    with delivery_lock(g, target):
        with lock:
            if groups.get(g["id"]) is not g:
                return False
            settings = automation_settings(g)
            state = automation_state(g)
            if (not settings["idle_minutes"] or not settings["reminder_limit"]
                    or idle_coordinator(g, settings) is not target):
                state["idle_since"] = None
                return False
            now = time.time() if now is None else now
            since, last = state["idle_since"], state["last_reminder_at"]
            if since is None or now < since:
                state["idle_since"] = now
                return False
            if (now - since < settings["idle_minutes"] * 60
                    or (last is not None and now - last < settings["reminder_cooldown_minutes"] * 60)
                    or state["reminders_sent"] >= settings["reminder_limit"]):
                return False
            if not budget_available(g):
                budget_notice(g)
                return False
            number = state["reminders_sent"] + 1
            text = (f"[自动提醒 idle:{g['id']}:{number}] 群内已持续空闲，请协调下一步或说明等待原因。"
                    "必要时定向提及其他成员；回复最后请独立一行标明状态，取值为进行中、等对方、等群主、阻塞或已交付，格式为‘状态：取值’。")
            state.update(reminders_sent=number, last_reminder_at=now)
            queue_delivery(g, target, new_delivery(text, 0))
            add_msg(g, "system", "系统", text, to=[target["name"]], log=False)
        flush_outbox(g, target)
    return True


def claude_limits_path(g: dict) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", g["id"]):
        raise ValueError("群标识不合法")
    return DATA_DIR.resolve() / "claude_limits" / f"{g['id']}.json"


def claude_statusline_entry(g: dict) -> dict:
    return {"type": "command", "command": shlex.join([
        sys.executable, "-I", "-B", str(HERE / "claude_statusline.py"), str(claude_limits_path(g))])}


def atomic_project_settings(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".groupchat-settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_claude_statusline(g: dict, enabled: bool):
    """Return an undo callback; never write shared/global or unowned settings."""
    if not enabled and not automation_settings(g)["claude_limits"]:
        return lambda: None
    workdir = Path(g["workdir"]).expanduser().resolve()
    path = workdir / ".claude" / "settings.local.json"
    global_dir = (Path.home() / ".claude").resolve()
    if (workdir == Path.home().resolve() or path.resolve().is_relative_to(global_dir)
            or claude_limits_path(g).resolve().is_relative_to(global_dir)
            or path.parent.is_symlink() or path.is_symlink()):
        raise ValueError("只能配置独立项目目录，不能使用全局或符号链接的 Claude 设置")
    if enabled and any(other.get("id") != g["id"] and
                       Path(other["workdir"]).expanduser().resolve() == workdir for other in groups.values()):
        raise ValueError("该工作目录被其他群使用，请使用独立目录开启 Claude 额度采集")
    try:
        original = path.read_bytes() if path.exists() else None
        def invalid_constant(_):
            raise ValueError("invalid JSON constant")
        content = json.loads(original, parse_constant=invalid_constant) if original is not None else {}
        if not isinstance(content, dict):
            raise ValueError("settings must be an object")
    except (OSError, ValueError):
        raise ValueError("Claude 项目本地设置不是可读取的合法 JSON 对象，原文件未修改") from None
    expected = claude_statusline_entry(g)
    owned = content.get("statusLine") == expected
    if enabled:
        if "statusLine" in content and not owned:
            raise ValueError("已有非本程序的 statusLine 状态栏，拒绝覆盖")
        if owned:
            return lambda: None
        content["statusLine"] = expected
    elif owned:
        del content["statusLine"]
    elif "statusLine" in content:
        raise ValueError("statusLine 已不属于本程序，保留原设置")
    else:
        return lambda: None
    replacement = (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode() if content else None
    try:
        atomic_project_settings(path, replacement)
    except OSError:
        raise ValueError("无法原子写入 Claude 项目本地设置，请检查目录权限") from None
    return lambda: atomic_project_settings(path, original)


def optional_claude_statusline(g: dict, enabled: bool):
    """Optional setup/cleanup cannot prevent member or group lifecycle actions."""
    try:
        return configure_claude_statusline(g, enabled), None
    except Exception:
        # Never include settings contents, command paths or arbitrary errors.
        warning = ("Claude 额度采集设置不可用，已跳过本次配置；成员继续启动。请检查项目本地设置。" if enabled else
                   "Claude 额度采集设置未清理（文件不可用或不再归本程序所有），已保留原文件；关闭或解散继续。")
        add_msg(g, "system", "系统", warning, log=False)
        return lambda: None, warning


def claude_limits(g: dict) -> dict:
    result = {"windows": [], "updated_at": None, "spend_limit": None}
    if not automation_settings(g)["claude_limits"]:
        return result
    try:
        data = json.loads(claude_limits_path(g).read_text(encoding="utf-8"))
        updated = data.get("updated_at")
        if type(updated) not in (int, float) or not math.isfinite(updated) or updated <= 0:
            return result
        limits = data.get("rate_limits")
        if not isinstance(limits, dict):
            return result
        now = time.time()
        result["updated_at"] = updated
        for name, minutes in (("five_hour", 300), ("seven_day", 10080), ("spend_limit", None)):
            value = limits.get(name)
            if not isinstance(value, dict):
                continue
            used, resets = value.get("used_percentage"), value.get("resets_at")
            if (type(used) not in (int, float) or type(resets) not in (int, float)
                    or not math.isfinite(used) or not math.isfinite(resets)
                    or not 0 <= used <= 100 or resets <= now):
                continue
            if name == "spend_limit":
                result["spend_limit"] = {"used_percentage": used, "resets_at": resets}
            else:
                result["windows"].append({"window_minutes": minutes, "used_percent": used, "resets_at": resets,
                                          "resets_in": max(int(resets - now), 0), "stale": False})
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return {"windows": [], "updated_at": None, "spend_limit": None}
    return result


# ───────────────────────── 用量与开销 ─────────────────────────
# token/费用来自各 CLI 本机日志；Claude 额度另取上述可选状态栏快照，不联网、不需要密钥：
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
    claude_enabled = automation_settings(g)["claude_limits"]
    claude_quota = claude_limits(g) if claude_enabled else None
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
                            "limits": limits if prov == "codex" else claude_quota if prov == "claude_code" else None,
                            "limits_available": prov == "codex" or (prov == "claude_code" and claude_enabled)}
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
                if rest == ["automation"]:
                    if method == "GET":
                        return self._json(200, automation_view(g))
                    if method == "PUT":
                        return self._json(200, update_automation(g, self._body()))
                if not rest and method == "DELETE":
                    warning = None
                    with lock:
                        if automation_settings(g)["claude_limits"]:
                            _, warning = optional_claude_statusline(g, False)
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
                    return self._json(200, {"ok": True, **({"warnings": [warning]} if warning else {})})
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
                                             f"推理强度={new.get('effort') or 'CLI 默认'}，"
                                             f"职责={new['duty'] or '未指定'}")
                        run_bg(bootstrap_members, g, [new], False)
                        return self._json(200, new)
                if len(rest) == 3 and rest[0] == "members" and rest[2] == "retry" and method == "POST":
                    m = next((x for x in g["members"] if x["id"] == rest[1]), None)
                    if not m:
                        return self._json(404, {"error": "成员不存在"})
                    retry_delivery(g, m, self._body())
                    return self._json(200, {"ok": True})
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
