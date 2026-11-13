import json, re, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

T = {}; P = {}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def j(self, c, o):
        d = json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(d)))
        self.end_headers(); self.wfile.write(d)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0); return json.loads(self.rfile.read(n)) if n else {}
    def r(self, m):
        u = parse.urlparse(self.path); q = {k: v[0] for k, v in parse.parse_qs(u.query).items()}; p = u.path
        if p == "/health": return self.j(200, {"status": "ok"})
        if p == "/agents/profiles" and m == "POST":
            b = self.body(); P[b["name"]] = b["content"]; return self.j(201, {"ok": True})
        if (p == "/sessions" or re.match(r"/sessions/[^/]+/terminals:operator$", p)) and m == "POST":
            self.body()
            assert q["agent_profile"] in P, "profile missing"
            tid = uuid.uuid4().hex[:8]
            s = q.get("session_name") or p.split("/")[2]
            T[tid] = {"id": tid, "session_name": s, "status": "idle", "profile": q["agent_profile"], "out": ""}
            return self.j(201, T[tid])
        mm = re.match(r"/terminals/(\w+)(/.*)?$", p)
        if mm:
            t = T.get(mm.group(1)); sub = mm.group(2) or ""
            if not t: return self.j(404, {"detail": "nf"})
            if sub == "" and m == "GET": return self.j(200, t)
            if sub == "" and m == "DELETE": T.pop(t["id"]); return self.j(200, {"ok": True})
            if sub == "/exit": return self.j(200, {"success": True})
            if sub == "/input":
                msg = q["message"]; t["status"] = "processing"
                def done():
                    time.sleep(1.5)
                    c=P[t["profile"]]
                    if "**统筹**" in c:
                        out = "收到，@开发 请实现登录接口。\n[日志] 分派任务"
                    elif "**开发**" in c:
                        out = "已实现 \x1b[32mauth.ts\x1b[0m，@评审 请看。"
                    else:
                        out = f"回复：{msg[:40]}\n[日志] 审查完成"
                    t["out"] = out; t["status"] = "completed"
                threading.Thread(target=done, daemon=True).start()
                return self.j(200, {"success": True, "dispatch_boundary": 1})
            if sub == "/output": return self.j(200, {"output": t["out"], "mode": q.get("mode")})
        if p.startswith("/sessions/") and m == "DELETE": return self.j(200, {"success": True})
        return self.j(404, {"detail": "nf " + p})
    def do_GET(self): self.r("GET")
    def do_POST(self): self.r("POST")
    def do_DELETE(self): self.r("DELETE")
    def do_PUT(self): self.r("PUT")

P_FIRST = ["__none__"]
ThreadingHTTPServer(("127.0.0.1", 9889), H).serve_forever()
