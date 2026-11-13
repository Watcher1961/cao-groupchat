"""Real HTTP/subprocess acceptance tests using only disposable local resources."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="groupchat-integration-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        self.env = {
            "HOME": str(self.home), "PATH": str(self.root / "empty-bin"),
            "CAO_TOKEN": "", "GROUPCHAT_DATA": str(self.data),
            "PYTHONIOENCODING": "utf-8", "NO_PROXY": "*",
        }
        self.opener = request.build_opener(request.ProxyHandler({}))
        self.processes = []
        ready = self.root / "fake-ready.json"
        self.fake = self.spawn("fake", [str(ROOT / "tests/fake_cao.py"),
                                       "--port", "0", "--ready-file", str(ready)], self.env)
        port = self.until("fake CAO port", lambda: json.loads(ready.read_text())["port"])
        self.assertNotEqual(port, 9889)
        self.cao_url = f"http://127.0.0.1:{port}"
        self.until("fake CAO health", lambda: self.http(self.cao_url, "GET", "/health"))
        self.start_server()

    def spawn(self, name, args, env):
        output = (self.root / f"{name}-{len(self.processes)}.log").open("w+")
        self.addCleanup(output.close)
        proc = subprocess.Popen([sys.executable, "-I", "-B", *args],
                                cwd=self.root, env=env, stdout=output, stderr=output)
        self.processes.append((proc, output))
        self.addCleanup(self.stop_process, proc)
        return proc

    def stop_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        self.assertIsNotNone(proc.poll(), "child process was not reaped")

    def until(self, label, probe, timeout=15):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            for proc, output in self.processes:
                if proc.poll() is not None and proc is not getattr(self, "stopped_server", None):
                    output.seek(0)
                    self.fail(f"Child exited while waiting for {label}: {output.read()}")
            try:
                last = probe()
                if last:
                    return last
            except (OSError, error.URLError, json.JSONDecodeError) as exc:
                last = type(exc).__name__
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        self.fail(f"Timed out waiting for {label}; last result: {last!r}")

    def http(self, base, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(base + path, data=data, method=method,
                              headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=1) as response:
            self.assertEqual(response.status, 201 if method == "POST" and
                             path in ("/api/groups", getattr(self, "group_path", "") + "/members",
                                      getattr(self, "group_path", "") + "/messages") else 200)
            return json.load(response)

    def start_server(self):
        # The OS chooses the port. Never probe or connect to the real CAO port.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.assertNotEqual(port, 9889)
        self.assertNotEqual(port, int(self.cao_url.rsplit(":", 1)[1]))
        self.server_url = f"http://127.0.0.1:{port}"
        env = {**self.env, "CAO_URL": self.cao_url, "GROUPCHAT_PORT": str(port)}
        self.server = self.spawn("server", [str(ROOT / "server.py")], env)
        health = self.until("GUI health", lambda: self.http(self.server_url, "GET", "/api/health"))
        self.assertTrue(health["cao"])
        self.assertEqual(health["cao_url"], self.cao_url)
        self.assertFalse((self.home / ".cao-groupchat").exists())

    def api(self, method, suffix="", body=None):
        return self.http(self.server_url, method, self.group_path + suffix, body)

    def terminal(self, tid):
        return self.http(self.cao_url, "GET", "/_test/state")["terminals"][tid]

    def next_turn(self, tid, reply, **options):
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/next-turn",
                  {"reply": reply, **options})

    def create_group(self):
        group = self.http(self.server_url, "POST", "/api/groups", {
            "name": "集成验收群", "workdir": str(self.root / "project"), "members": []})
        self.group_path = f"/api/groups/{group['id']}"
        self.assertEqual(group["members"], [])
        return group

    def invite(self, name, provider, role):
        member = self.api("POST", "/members", {"name": name, "provider": provider, "role": role})
        def joined():
            group = self.api("GET")
            current = next(m for m in group["members"] if m["id"] == member["id"])
            return current if current["terminal_id"] and any(
                f"{name} 已入群" in msg["text"] for msg in group["messages"]) else None
        return self.until(f"{name} joins", joined)

    def replies(self):
        return [msg for msg in self.api("GET")["messages"] if msg["kind"] == "agent"]

    def wait_replies(self, count):
        def received():
            replies = self.replies()
            return replies if len(replies) >= count else None
        return self.until(f"{count} agent replies", received)

    def test_group_message_relay_and_restart_restore(self):
        group = self.create_group()
        dev = self.invite("开发", "codex", "developer")
        review = self.invite("评审", "claude_code", "reviewer")
        dev_tid, review_tid = dev["terminal_id"], review["terminal_id"]
        # Joining a second member triggers a silent notification to the first.
        self.until("join notification dispatch", lambda: self.terminal(dev_tid)["inputs"])
        self.until("join notification consumed", lambda: all(
            not m.get("pending") and m.get("reply_handled")
            for m in self.api("GET")["members"] if m["id"] == dev["id"]))
        self.assertEqual(self.replies(), [])
        dev_reply = "本轮实现已完成，@评审 请审查。"
        review_reply = "本轮审查通过。\n[日志] 已审查本轮实现"
        self.next_turn(dev_tid, dev_reply)
        self.next_turn(review_tid, review_reply)
        owner_text = "@开发 实现本轮接口"
        owner = self.api("POST", "/messages", {"text": owner_text})
        self.assertEqual((owner["kind"], owner["sender"], owner["to"]),
                         ("owner", "群主", ["开发"]))
        replies = self.wait_replies(2)
        self.assertEqual([(m["sender"], m["member_id"], m["text"]) for m in replies],
                         [("开发", dev["id"], dev_reply), ("评审", review["id"], review_reply)])
        dev_state, review_state = self.terminal(dev_tid), self.terminal(review_tid)
        self.assertEqual(dev_state["inputs"][-1]["message"], "[群主] " + owner_text)
        self.assertEqual([turn["message"] for turn in review_state["inputs"]],
                         ["[来自 开发] " + dev_reply])
        self.assertEqual(dev_state["session_name"], review_state["session_name"])
        self.assertGreater(review_state["status_generation"],
                           review_state["inputs"][-1]["dispatch_boundary"])
        self.until("relay log written", lambda: "[评审] 已审查本轮实现" in
                   self.api("GET", "/log")["content"])
        def settled_group():
            current = self.api("GET")
            return current if all(not m.get("pending") for m in current["members"]) else None
        before = self.until("all turns consumed", settled_group)
        state_file = self.data / "groups.json"
        def persisted():
            saved = json.loads(state_file.read_text())[group["id"]]
            return saved if saved["messages"] == before["messages"] else None
        saved = self.until("messages on disk", persisted)
        self.assertEqual(saved["name"], before["name"])
        # Keep fake CAO alive while replacing only the GUI process.
        self.stopped_server = self.server
        self.stop_process(self.stopped_server)
        self.assertIsNone(self.fake.poll())
        stopped_state = self.terminal(review_tid)
        self.start_server()
        listed = self.http(self.server_url, "GET", "/api/groups")
        self.assertEqual([g["id"] for g in listed], [group["id"]])
        restored = self.api("GET")
        for key in ("id", "name", "workdir", "created", "session_name", "messages", "members"):
            self.assertEqual(restored[key], before[key], key)
        # Wait for an actual post-restart poll, so replay/duplication cannot hide at startup.
        self.until("poll after restart", lambda: self.terminal(review_tid)["status_reads"] >
                   stopped_state["status_reads"])
        self.assertEqual(self.api("GET")["messages"], before["messages"])
        self.assertEqual(self.terminal(review_tid)["output_reads"], review_state["output_reads"])

    def test_stale_completed_waits_for_current_turn_output(self):
        self.create_group()
        dev = self.invite("开发", "codex", "developer")
        tid = dev["terminal_id"]
        old_reply, new_reply = "上一轮的唯一内容", "本轮的新内容，不能复用上一轮"
        self.next_turn(tid, old_reply)
        self.api("POST", "/messages", {"text": "@开发 第一轮"})
        self.assertEqual([m["text"] for m in self.wait_replies(1)], [old_reply])
        previous = self.terminal(tid)
        self.assertEqual(previous["status"], "completed")
        self.next_turn(tid, new_reply, hold=True, stale_polls=0)
        self.api("POST", "/messages", {"text": "@开发 第二轮"})
        self.until("second dispatch", lambda: len(self.terminal(tid)["inputs"]) == 2)
        self.until("backend observes stale completed twice", lambda:
                   self.terminal(tid)["inputs"][-1]["stale_reports"] >= 2)
        stale = self.terminal(tid)
        boundary = stale["inputs"][-1]["dispatch_boundary"]
        self.assertEqual(boundary, previous["status_generation"])
        self.assertGreater(boundary, previous["inputs"][0]["dispatch_boundary"])
        self.assertEqual((stale["status"], stale["status_generation"], stale["out"]),
                         ("completed", boundary, old_reply))
        member = self.api("GET")["members"][0]
        self.assertEqual(member["boundary"], boundary)
        self.assertTrue(member["pending"])
        self.assertEqual(stale["output_reads"], previous["output_reads"])
        self.assertEqual([m["text"] for m in self.replies()], [old_reply])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/release")
        self.assertEqual([m["text"] for m in self.wait_replies(2)], [old_reply, new_reply])
        current = self.terminal(tid)
        self.assertGreater(current["status_generation"], boundary)
        self.assertEqual(current["output_reads"], previous["output_reads"] + 1)
        self.assertFalse(self.api("GET")["members"][0]["pending"])


if __name__ == "__main__":
    unittest.main()
