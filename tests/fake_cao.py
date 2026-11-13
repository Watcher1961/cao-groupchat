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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, obj):
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
                ("id", "session_name", "status", "status_generation")}

    def default_reply(self, terminal, message):
        if "只需回复“收到”" in message:
            return "收到"
        profile = self.server.profiles[terminal["profile"]]
        if "**统筹**" in profile:
            return "收到，@开发 请实现登录接口。\n[日志] 分派任务"
        if "**开发**" in profile:
            return "已实现 \x1b[32mauth.ts\x1b[0m，@评审 请看。"
        return f"回复：{message[:40]}\n[日志] 审查完成"

    def route(self, method):
        url = parse.urlparse(self.path)
        query = {k: v[0] for k, v in parse.parse_qs(url.query).items()}
        path = url.path
        with self.server.lock:
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
                tid = uuid.uuid4().hex[:8]
                terminal = {
                    "id": tid, "session_name": query.get("session_name") or path.split("/")[2],
                    "status": "idle", "status_generation": 0,
                    "profile": query["agent_profile"], "out": "", "inputs": [],
                    "pending": None, "next_turn": {}, "output_reads": 0, "status_reads": 0,
                }
                self.server.terminals[tid] = terminal
                return self.respond(201, self.public(terminal))
            # Test-only controls/observations do not advance terminal status.
            if path == "/_test/state" and method == "GET":
                return self.respond(200, {"terminals": self.server.terminals})
            control = re.fullmatch(r"/_test/terminals/(\w+)/(next-turn|release)", path)
            if control and method == "POST":
                terminal = self.server.terminals.get(control[1])
                if terminal is None:
                    return self.respond(404, {"detail": "terminal missing"})
                if control[2] == "next-turn":
                    terminal["next_turn"] = self.body()
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
                    if turn:
                        if turn["held"] or turn["stale_remaining"] > 0:
                            turn["stale_remaining"] = max(0, turn["stale_remaining"] - 1)
                            if (terminal["status"] == "completed" and
                                    terminal["status_generation"] <= turn["dispatch_boundary"]):
                                turn["stale_reports"] += 1
                        else:
                            terminal["status_generation"] += 1
                            terminal["status"] = "completed"
                            terminal["out"] = turn["reply"]
                            terminal["pending"] = None
                    return self.respond(200, self.public(terminal))
                if not sub and method == "DELETE":
                    del self.server.terminals[terminal["id"]]
                    return self.respond(200, {"ok": True})
                if sub == "/exit" and method == "POST":
                    return self.respond(200, {"success": True})
                if sub == "/input" and method == "POST":
                    message = query["message"]
                    boundary = terminal["status_generation"]
                    options = terminal["next_turn"]
                    terminal["next_turn"] = {}
                    stale = terminal["status"] == "completed"
                    turn = {
                        "message": message, "dispatch_boundary": boundary,
                        "reply": options.get("reply", self.default_reply(terminal, message)),
                        "held": options.get("hold", False),
                        "stale_remaining": options.get("stale_polls", 2) if stale else 0,
                        "stale_reports": 0,
                    }
                    terminal["inputs"].append(turn)
                    terminal["pending"] = turn
                    if not stale:
                        terminal["status"] = "processing"
                        terminal["status_generation"] += 1
                    return self.respond(200, {"success": True, "dispatch_boundary": boundary})
                if sub == "/output" and method == "GET":
                    terminal["output_reads"] += 1
                    return self.respond(200, {"output": terminal["out"], "mode": query.get("mode")})
            if path.startswith("/sessions/") and method == "DELETE":
                session = path.split("/")[2]
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
