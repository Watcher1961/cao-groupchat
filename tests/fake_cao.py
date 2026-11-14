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
        return "Ready\n> "

    def write_raw(self, terminal, content):
        raw = (terminal["full_out"] + content).encode()
        terminal["full_out"] = raw[-self.server.buffer_limit:].decode(errors="ignore")

    def complete(self, terminal, turn):
        terminal["output_generation"] += 1
        terminal["status_generation"] = terminal["output_generation"]
        terminal["status"] = "completed"
        terminal["out"] = turn["reply"]
        if terminal["provider"] == "codex":
            self.write_raw(terminal, "\x1b]7501;state=working:app=codex\x1b\\" + "x" * turn["padding_bytes"] + turn["reply"] +
                           "\x1b]7501;state=done:app=codex\x1b\\\n" + self.prompt(terminal["provider"]))
        else:
            self.write_raw(terminal, turn["reply"] + "\n" + self.prompt(terminal["provider"]))
        terminal["pending"] = None

    def route(self, method):
        url = parse.urlparse(self.path)
        query = {k: v[0] for k, v in parse.parse_qs(url.query).items()}
        path = url.path
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
                    for name in ("codex", "claude_code")])
            if path == "/agents/profiles" and method == "POST":
                body = self.body()
                self.server.profiles[body["name"]] = body["content"]
                return self.respond(201, {"ok": True})
            if method == "POST" and (path == "/sessions" or re.fullmatch(
                    r"/sessions/[^/]+/terminals:operator", path)):
                self.body()
                if query.get("agent_profile") not in self.server.profiles:
                    return self.respond(400, {"detail": "profile missing"})
                if path == "/sessions" and session in self.server.sessions:
                    return self.respond(400, {"detail": f"Session '{session}' already exists"})
                if path != "/sessions" and session not in self.server.sessions:
                    return self.respond(404, {"detail": f"Session '{session}' not found"})
                if query["agent_profile"] in self.server.config["fail_profiles"]:
                    return self.respond(503, {"detail": "simulated terminal start failure"})
                if path == "/sessions":
                    self.server.sessions[session] = {"session_name": session}
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
            if re.fullmatch(r"/sessions/[^/]+", path) and method == "GET":
                session = self.server.sessions.get(path.split("/")[2])
                return self.respond(200, session) if session else self.respond(404, {"detail": "session missing"})
            control = re.fullmatch(r"/_test/terminals/(\w+)/(next-turn|release|advance|ready|progress)", path)
            if control and method == "POST":
                terminal = self.server.terminals.get(control[1])
                if terminal is None:
                    return self.respond(404, {"detail": "terminal missing"})
                if control[2] == "next-turn":
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
                            self.write_raw(terminal, "\x1b]7501;state=working:app=codex\x1b\\" + turn["interim"])
                        else:
                            self.write_raw(terminal, "x" * turn["padding_bytes"] + turn["reply"] +
                                           "\x1b]7501;state=done:app=codex\x1b\\\n" + self.prompt(terminal["provider"]))
                            terminal["out"] = turn["reply"]
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
                if sub == "/input" and method == "POST":
                    message = query["message"]
                    boundary = terminal["output_generation"]
                    options = terminal["next_turn"]
                    terminal["next_turn"] = {}
                    if not options.get("append_output", False):
                        terminal["full_out"] = ""
                    stale = terminal["status"] == "completed"
                    turn = {
                        "message": message, "dispatch_boundary": boundary,
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
                    elif turn["complete_on_input"]:
                        self.complete(terminal, turn)
                    elif turn["idle_stream"]:
                        terminal["status"] = "idle"
                        terminal["output_generation"] += 1
                        terminal["status_generation"] = terminal["output_generation"]
                        terminal["out"] = turn["interim"]
                        self.write_raw(terminal, options.get("raw_prefix", "") + turn["interim"])
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
