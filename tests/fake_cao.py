"""Local CAO simulator; progression is driven by status polls, never by sleeps."""

import argparse
import json
from pathlib import Path
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse


class FakeCAO(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, Handler)
        self.lock = threading.RLock()
        self.profiles = {}
        self.terminals = {}
        self.sessions = {}
        self.create_requests = []
        self.config = {"startup_hold": False, "hold_create": False, "hold_sessions": [], "fail_profiles": []}
        self.creation_gates = {}
        self.create_waiting = 0
        self.create_waiting_sessions = {}
        self.buffer_limit = 32768


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, obj):
        if getattr(self, "creation_event", None) is not None:
            with self.server.lock:
                self.creation_event["status"] = code
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(size)) if size else {}

    @staticmethod
    def public(terminal):
        return {key: terminal[key] for key in
                ("id", "session_name", "status", "status_generation", "output_generation")}

    def default_reply(self, terminal, message):
        if "只需回复“收到”" in message:
            return "收到"
        fixed = self.server.config.get("provider_replies", {}).get(terminal["provider"])
        if fixed is not None:
            return fixed
        profile = self.server.profiles[terminal["profile"]]
        if "**统筹**" in profile:
            return "收到，@开发 请实现登录接口。\n[日志] 分派任务"
        if "**开发**" in profile:
            return "已实现 \x1b[32mauth.ts\x1b[0m，@评审 请看。"
        return f"回复：{message[:40]}\n[日志] 审查完成"

    @staticmethod
    def prompt(provider):
        if provider == "codex":
            return "codex>\n? for shortcuts"
        if provider == "claude_code":
            return '────────────\n❯ \x1b[2mTry "fix typecheck errors"\x1b[22m\n────────────\n? for shortcuts'
        if provider == "hermes":
            return "────────────\n❯ \x1b[0;38;5;240;3mSynthetic next action?\x1b[0m\n────────────"
        return "Ready\n> "

    @staticmethod
    def model_menu(terminal):
        sim = terminal["model_menu_sim"]
        labels = ("Switch to another model", "Keep current model", "Keep current model (never show again)")
        rows = [("› " if i == sim.get("selected", 1) else "  ") + f"{i}. {label}" for i, label in enumerate(labels, 1)]
        if sim.get("partial"):
            rows = rows[1:]
        else:
            rows.insert(0, "Approaching rate limits")
        return "\n".join(rows) + "\nenter select · esc back"

    def write_raw(self, terminal, content):
        # Hermes CAO captures are LF-only; retain that path in the fake.
        if terminal["provider"] == "claude_code":
            content = re.sub(r"(?<!\r)\n", "\r\n", content)
        raw = (terminal["full_out"] + content).encode()
        terminal["full_out"] = raw[-self.server.buffer_limit:].decode(errors="ignore")

    @staticmethod
    def rendered_reply(terminal, text):
        if terminal["provider"] != "codex":
            return text
        lines = text.split("\n")
        return "• " + lines[0] + "".join("\n" + ("  " + line if line else "") for line in lines[1:])

    def complete(self, terminal, turn):
        terminal["output_generation"] += 1
        terminal["status_generation"] = terminal["output_generation"]
        terminal["status"] = turn.get("completion_status") or "completed"
        terminal["out"] = self.rendered_reply(terminal, turn["reply"])
        if terminal["provider"] == "hermes":
            frame = ("☤ Hermes\n\n" + turn["reply"] + f"\n─ {terminal['output_generation']}.1s · read 1 file · 01:02 PM\n" + self.prompt("hermes"))
            self.write_raw(terminal, frame)
            terminal["out"] = "[NO RESPONSE - agent completed without producing a text response (99 lines in buffer)]\n" + terminal["full_out"]
        elif terminal["provider"] == "codex":
            self.write_raw(terminal, "\x1b]7501;state=working:app=codex\x1b\\" + "x" * turn["padding_bytes"] + terminal["out"] +
                           "\x1b]7501;state=done:app=codex\x1b\\\n" + self.prompt(terminal["provider"]))
        else:
            self.write_raw(terminal, turn["reply"] + "\n" + self.prompt(terminal["provider"]))
        if turn["full_output"] is not None:
            terminal["full_out"] = ""
            self.write_raw(terminal, turn["full_output"])
        if turn["last_output"] is not None:
            terminal["out"] = turn["last_output"]
        terminal["pending"] = None

    def route(self, method):
        url = parse.urlparse(self.path)
        query = {k: v[0] for k, v in parse.parse_qs(url.query).items()}
        path = url.path
        maximum = self.server.config.get("max_input_target_bytes", 0)
        if maximum and method == "POST" and re.fullmatch(r"/terminals/[^/]+/input", path) and len(self.path.encode("ascii")) > maximum:
            return self.respond(400, {"detail": "Invalid HTTP request received."})
        creating = method == "POST" and (path == "/sessions" or
                   re.fullmatch(r"/sessions/[^/]+/terminals:operator", path))
        self.creation_event = None
        if creating:
            session = query.get("session_name") if path == "/sessions" else path.split("/")[2]
            with self.server.lock:
                self.creation_event = {"path": path, "session": session,
                                       "profile": query.get("agent_profile"), "status": None}
                self.server.create_requests.append(self.creation_event)
                blocked = self.server.config["hold_create"] or session in self.server.config["hold_sessions"]
                if blocked:
                    gate = self.server.creation_gates.setdefault(session, threading.Event())
                    gate.clear()
                    self.server.create_waiting += 1
                    self.server.create_waiting_sessions[session] = self.server.create_waiting_sessions.get(session, 0) + 1
            if blocked:
                released = gate.wait(timeout=10)
                with self.server.lock:
                    self.server.create_waiting -= 1
                    self.server.create_waiting_sessions[session] -= 1
                if not released:
                    return self.respond(503, {"detail": "create gate timed out"})
        with self.server.lock:
            if path == "/_test/config" and method == "POST":
                self.server.config.update(self.body())
                for session, gate in self.server.creation_gates.items():
                    if self.server.config["hold_create"] or session in self.server.config["hold_sessions"]:
                        gate.clear()
                    else:
                        gate.set()
                return self.respond(200, {"ok": True})
            if path == "/health" and method == "GET":
                return self.respond(200, {"status": "ok"})
            if path == "/agents/providers" and method == "GET":
                return self.respond(200, [
                    {"name": name, "installed": True, "binary": "simulated"}
                    for name in ("codex", "claude_code", "hermes", "antigravity_cli")])
            if path == "/agents/profiles" and method == "POST":
                body = self.body()
                self.server.profiles[body["name"]] = body["content"]
                return self.respond(201, {"ok": True})
            if method == "POST" and (path == "/sessions" or re.fullmatch(
                    r"/sessions/[^/]+/terminals:operator", path)):
                self.body()
                if query.get("agent_profile") not in self.server.profiles:
                    return self.respond(400, {"detail": "profile missing"})
                if (path == "/sessions" and session in self.server.sessions
                        and self.server.sessions[session]["status"] != "terminated"):
                    return self.respond(400, {"detail": f"Session '{session}' already exists"})
                if (path != "/sessions" and (session not in self.server.sessions
                        or self.server.sessions[session]["status"] == "terminated")):
                    return self.respond(404, {"detail": f"Session '{session}' not found"})
                if query["agent_profile"] in self.server.config["fail_profiles"]:
                    return self.respond(503, {"detail": "simulated terminal start failure"})
                if path == "/sessions":
                    self.server.sessions[session] = {"id": session, "name": session, "status": "detached"}
                tid = uuid.uuid4().hex[:8]
                terminal = {
                    "id": tid, "session_name": query.get("session_name") or path.split("/")[2],
                    "status": "idle", "status_generation": 0, "output_generation": 0,
                    "profile": query["agent_profile"], "provider": query["provider"],
                    "out": "", "full_out": "", "inputs": [], "full_reads": 0, "last_reads": 0,
                    "pending": None, "next_turn": {}, "output_reads": 0, "status_reads": 0,
                    "startup_ready": not self.server.config["startup_hold"], "early_inputs": 0,
                }
                terminal["full_out"] = (self.prompt(terminal["provider"]) if terminal["startup_ready"]
                                        else "CLI loading")
                terminal["output_generation"] = 1
                terminal["status_generation"] = 1 if terminal["startup_ready"] else 0
                self.server.terminals[tid] = terminal
                return self.respond(201, self.public(terminal))
            # Test-only controls/observations do not advance terminal status.
            if path == "/_test/state" and method == "GET":
                return self.respond(200, {"terminals": self.server.terminals,
                                          "profiles": self.server.profiles,
                                          "sessions": self.server.sessions,
                                          "create_requests": self.server.create_requests,
                                          "create_waiting": self.server.create_waiting,
                                          "create_waiting_sessions": self.server.create_waiting_sessions,
                                          "buffer_limit": self.server.buffer_limit})
            session_control = re.fullmatch(r"/_test/sessions/([^/]+)/terminate", path)
            if session_control and method == "POST":
                session = self.server.sessions.get(session_control[1])
                if session is None:
                    return self.respond(404, {"detail": "session missing"})
                session["status"] = "terminated"
                for terminal in self.server.terminals.values():
                    if terminal["session_name"] == session_control[1]:
                        terminal["status"] = "error"
                        terminal["deferred_init_failure"] = {"error_type": "simulated_provider_start_failure"}
                return self.respond(200, {"ok": True})
            if re.fullmatch(r"/sessions/[^/]+", path) and method == "GET":
                name = path.split("/")[2]
                session = self.server.sessions.get(name)
                terminals = [{**self.public(t), **({"deferred_init_failure": t["deferred_init_failure"]}
                              if t.get("deferred_init_failure") else {})}
                             for t in self.server.terminals.values() if t["session_name"] == name]
                if not session or (session["status"] == "terminated" and
                                   not any(t.get("deferred_init_failure") for t in terminals)):
                    return self.respond(404, {"detail": "session missing"})
                return self.respond(200, {"session": session, "terminals": terminals})
            control = re.fullmatch(r"/_test/terminals/(\w+)/(next-turn|release|advance|ready|progress|quota|quota-clear|model-menu|arm-menu)", path)
            if control and method == "POST":
                terminal = self.server.terminals.get(control[1])
                if terminal is None:
                    return self.respond(404, {"detail": "terminal missing"})
                if control[2] == "arm-menu":
                    terminal["menu_after_full_read"] = self.body()
                elif control[2] == "model-menu":
                    terminal["model_menu_sim"] = self.body()
                    terminal["full_out"] = self.model_menu(terminal)
                    terminal["out"] = terminal["full_out"]
                    terminal["status"] = terminal["model_menu_sim"].get("status", "idle")
                    terminal["output_generation"] += 1
                    terminal["status_generation"] = terminal["output_generation"]
                elif control[2] in ("quota", "quota-clear"):
                    options = self.body()
                    if control[2] == "quota-clear":
                        terminal["model_menu_sim"] = None
                    terminal["full_out"] = options.get("prompt", "") if control[2] == "quota" else self.prompt(terminal["provider"])
                    terminal["out"] = ""
                    terminal["status"] = options.get("status", "error") if control[2] == "quota" else "idle"
                    terminal["pending"] = None
                    terminal["output_generation"] += 1
                    terminal["status_generation"] = terminal["output_generation"]
                elif control[2] == "next-turn":
                    terminal["next_turn"] = self.body()
                elif control[2] == "ready":
                    terminal["startup_ready"] = True
                    terminal["output_generation"] += 1
                    terminal["status_generation"] = terminal["output_generation"]
                    terminal["status"] = "idle"
                    prompt = self.body().get("prompt", self.prompt(terminal["provider"]))
                    self.write_raw(terminal, "\n" + prompt)
                elif control[2] == "progress":
                    self.write_raw(terminal, self.body()["text"])
                    terminal["output_generation"] += 1
                elif control[2] == "advance" and terminal["pending"]:
                    turn = terminal["pending"]
                    if turn["idle_stream"]:
                        turn["phase"] += 1
                        terminal["output_generation"] += 1
                        if turn["phase"] == 1:
                            self.write_raw(terminal, "\x1b]7501;state=working:app=codex\x1b\\" + self.rendered_reply(terminal, turn["interim"]))
                        else:
                            terminal["out"] = self.rendered_reply(terminal, turn["reply"])
                            self.write_raw(terminal, "x" * turn["padding_bytes"] + terminal["out"] +
                                           "\x1b]7501;state=done:app=codex\x1b\\\n" + self.prompt(terminal["provider"]))
                            terminal["pending"] = None
                elif terminal["pending"]:
                    terminal["pending"]["held"] = False
                return self.respond(200, {"ok": True})
            match = re.fullmatch(r"/terminals/(\w+)(/.*)?", path)
            if match:
                terminal = self.server.terminals.get(match[1])
                sub = match[2] or ""
                if terminal is None:
                    return self.respond(404, {"detail": "terminal missing"})
                if not sub and method == "GET":
                    terminal["status_reads"] += 1
                    turn = terminal["pending"]
                    latest = terminal["inputs"][-1] if terminal["inputs"] else None
                    if latest:
                        latest["polls"] += 1
                    if latest and latest["idle_stream"]:
                        latest["phase_reads"][latest["phase"]] += 1
                        latest["reported_statuses"].append(terminal["status"])
                    elif turn:
                        if turn["held"] or turn["stale_remaining"] > 0:
                            turn["stale_remaining"] = max(0, turn["stale_remaining"] - 1)
                            if (terminal["status"] == "completed" and
                                    terminal["status_generation"] <= turn["dispatch_boundary"]):
                                turn["stale_reports"] += 1
                        else:
                            self.complete(terminal, turn)
                    return self.respond(200, self.public(terminal))
                if not sub and method == "DELETE":
                    del self.server.terminals[terminal["id"]]
                    return self.respond(200, {"ok": True})
                if sub == "/exit" and method == "POST":
                    return self.respond(200, {"success": True})
                if sub == "/key" and method == "POST":
                    key = query["key"]
                    terminal.setdefault("keys", []).append(key)
                    sim = terminal.get("model_menu_sim")
                    if sim is not None:
                        terminal.setdefault("selection_checks", []).append({"key": key, "selected": sim.get("selected", 1)})
                        if key == "Enter":
                            if sim.get("selected", 1) != 2:
                                return self.respond(400, {"detail": "synthetic wrong selection"})
                            terminal["model_menu_sim"] = None
                            terminal["full_out"] = "\x1b]7501;state=working:app=codex\x1b\\working"
                            terminal["out"] = ""
                            terminal["status"] = "processing"
                        elif key in ("Down", "Up"):
                            if not sim.get("ignore_navigation"):
                                sim["selected"] = sim.get("selected", 1) + (1 if key == "Down" else -1)
                            terminal["full_out"] = self.model_menu(terminal)
                            terminal["out"] = terminal["full_out"]
                        terminal["output_generation"] += 1
                        terminal["status_generation"] = terminal["output_generation"]
                    return self.respond(200, {"success": True})
                if sub == "/input" and method == "POST":
                    message = query["message"]
                    if terminal.get("model_menu_sim") is not None:
                        # Simulate the real failure: attached Enter consumes the message.
                        terminal["menu_input_violations"] = terminal.get("menu_input_violations", 0) + 1
                        terminal["synthetic_model_switched"] = terminal["model_menu_sim"].get("selected", 1) == 1
                        return self.respond(200, {"dispatch_boundary": terminal["output_generation"]})
                    boundary = terminal["output_generation"]
                    options = terminal["next_turn"]
                    terminal["next_turn"] = {}
                    if not options.get("append_output", False):
                        terminal["full_out"] = ""
                    stale = terminal["status"] == "completed"
                    turn = {
                        "message": message, "dispatch_boundary": boundary, "target_bytes": len(self.path.encode("ascii")),
                        "full_output": options.get("full_output"), "last_output": options.get("last_output"),
                        "completion_status": options.get("completion_status"),
                        "reply": options.get("reply", self.default_reply(terminal, message)),
                        "held": options.get("hold", False),
                        "stale_remaining": options.get("stale_polls", 2) if stale else 0,
                        "stale_reports": 0, "idle_stream": options.get("idle_stream", False),
                        "interim": options.get("interim", "正在处理"), "phase": 0,
                        "phase_reads": [0, 0, 0], "reported_statuses": [], "polls": 0,
                        "append_output": options.get("append_output", False),
                        "complete_on_input": options.get("complete_on_input", False),
                        "padding_bytes": options.get("padding_bytes", 0), "drop": options.get("drop", False),
                    }
                    terminal["inputs"].append(turn)
                    terminal["pending"] = turn
                    if turn["drop"] or not terminal["startup_ready"]:
                        if not terminal["startup_ready"]:
                            terminal["early_inputs"] += 1
                        terminal["pending"] = None
                        if terminal["startup_ready"]:
                            terminal["status"] = "idle"
                            terminal["output_generation"] += 1
                            self.write_raw(terminal, self.prompt(terminal["provider"]))
                    elif options.get("quota_prompt"):
                        terminal["status"] = "error"
                        terminal["pending"] = None
                        terminal["output_generation"] += 1
                        terminal["status_generation"] = terminal["output_generation"]
                        terminal["full_out"] = options["quota_prompt"]
                    elif turn["complete_on_input"]:
                        self.complete(terminal, turn)
                    elif turn["idle_stream"]:
                        terminal["status"] = "idle"
                        terminal["output_generation"] += 1
                        terminal["status_generation"] = terminal["output_generation"]
                        terminal["out"] = self.rendered_reply(terminal, turn["interim"])
                        self.write_raw(terminal, options.get("raw_prefix", "") + terminal["out"])
                    elif not stale:
                        terminal["status"] = "processing"
                        # No raw bytes have arrived yet; a status value is not output.
                        terminal["status_generation"] = terminal["output_generation"]
                    return self.respond(200, {"success": True, "dispatch_boundary": boundary})
                if sub == "/output" and method == "GET":
                    terminal["output_reads"] += 1
                    mode = query.get("mode")
                    terminal["full_reads" if mode == "full" else "last_reads"] += 1
                    output = terminal["full_out"] if mode == "full" else terminal["out"]
                    armed = terminal.pop("menu_after_full_read", None) if mode == "full" else None
                    if armed is not None:
                        terminal["model_menu_sim"] = armed
                        terminal["full_out"] = self.model_menu(terminal)
                        terminal["out"] = terminal["full_out"]
                        terminal["status"] = "idle"
                        terminal["output_generation"] += 1
                        terminal["status_generation"] = terminal["output_generation"]
                    return self.respond(200, {"output": output, "mode": mode})
            if path.startswith("/sessions/") and method == "DELETE":
                session = path.split("/")[2]
                self.server.sessions.pop(session, None)
                self.server.terminals = {tid: t for tid, t in self.server.terminals.items()
                                         if t["session_name"] != session}
                return self.respond(200, {"success": True})
            return self.respond(404, {"detail": "not found"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9889, help="0 selects a random port")
    parser.add_argument("--ready-file", type=Path, help="write the bound port after startup")
    args = parser.parse_args()
    with FakeCAO(("127.0.0.1", args.port)) as server:
        if args.ready_file:
            args.ready_file.write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
        print(f"模拟 CAO 已启动：http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
