"""Real HTTP/subprocess acceptance tests using only disposable local resources."""

import json
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib import error, parse, request


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
            "GROUPCHAT_POLL_SECONDS": "0.03", "GROUPCHAT_READY_SECONDS": "0.06",
            "GROUPCHAT_STARTUP_TIMEOUT": "2", "GROUPCHAT_DELIVERY_TIMEOUT": "0.6",
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
            creates = path == "/api/groups" or (path.startswith("/api/groups/") and
                      path.rsplit("/",1)[-1] in ("members","messages"))
            self.assertEqual(response.status, 201 if method == "POST" and creates else 200)
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

    def invite(self, name, provider, role, **extra):
        member = self.api("POST", "/members", {"name": name, "provider": provider, "role": role, **extra})
        def joined():
            group = self.api("GET")
            current = next(m for m in group["members"] if m["id"] == member["id"])
            return current if current["terminal_id"] and current.get("ready_terminal")==current["terminal_id"] and any(
                f"{name} 已入群" in msg["text"] for msg in group["messages"]) else None
        return self.until(f"{name} joins", joined)

    def replies(self):
        return [msg for msg in self.api("GET")["messages"] if msg["kind"] == "agent"]

    def wait_replies(self, count):
        def received():
            replies = self.replies()
            return replies if len(replies) >= count else None
        return self.until(f"{count} agent replies", received)

    def fake_state(self):
        return self.http(self.cao_url,"GET","/_test/state")

    def concurrent_posts(self, requests):
        gate=threading.Barrier(len(requests))
        def post(item):
            gate.wait(timeout=3)
            return self.http(self.server_url,"POST",item[0],item[1])
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            return list(pool.map(post,requests))

    def startup_settled(self, path, count, timeout=15):
        def settled():
            group=self.http(self.server_url,"GET",path)
            members=group["members"]
            return group if len(members)==count and all(m.get("status")=="failed" or
                (m.get("terminal_id") and m.get("ready_terminal")==m["terminal_id"]) for m in members) else None
        return self.until("all startup workers settle",settled,timeout=timeout)

    def offline_group(self, path):
        group=self.http(self.server_url,"GET",path)
        old={m["terminal_id"] for m in group["members"]}
        for tid in old:
            self.http(self.cao_url,"DELETE",f"/terminals/{tid}")
        self.until("all members observed offline",lambda:all(not m["terminal_id"] for m in
            self.http(self.server_url,"GET",path)["members"]))
        return old

    def test_relay_storm_circuit_stops_real_http_chain_and_owner_resumes(self):
        self.create_group()
        a = self.invite("合成甲", "codex", "developer")
        b = self.invite("合成乙", "claude_code", "reviewer")
        self.until("intro turns complete", lambda: all(not m.get("pending") for m in self.api("GET")["members"]))
        self.api("PUT", "/automation", {"relay_limit": 30, "delivery_budget": 40})
        baseline = {m["id"]: len(self.terminal(m["terminal_id"])["inputs"]) for m in (a, b)}
        self.http(self.cao_url, "POST", "/_test/config", {"provider_replies": {
            "codex": "@合成乙 请检查合成任务。", "claude_code": "@合成甲 请继续合成步骤。"}})
        self.api("POST", "/messages", {"text": "@合成甲 begin synthetic chain"})
        group = self.until("relay circuit pauses", lambda: (g if (g := self.api("GET")).get("relay_control", {}).get("paused") else None))
        self.assertEqual(len([x for x in group["messages"] if x["kind"] == "agent"]), 12)
        self.assertEqual(sum("已暂停自动转发" in x["text"] for x in group["messages"]), 1)
        totals = [len(self.terminal(m["terminal_id"])["inputs"]) for m in (a, b)]
        reads = self.terminal(a["terminal_id"])["status_reads"]
        self.until("paused polling continues", lambda: self.terminal(a["terminal_id"])["status_reads"] >= reads + 4)
        self.assertEqual([len(self.terminal(m["terminal_id"])["inputs"]) for m in (a, b)], totals)
        self.assertEqual(sum(totals) - sum(baseline.values()), 12)
        self.http(self.cao_url, "POST", "/_test/config", {"provider_replies": {"codex": "新指令已处理。", "claude_code": "新指令已处理。"}})
        self.api("POST", "/messages", {"text": "@合成甲 owner priority"})
        self.until("owner input bypasses circuit", lambda: len(self.terminal(a["terminal_id"])["inputs"]) == totals[0] + 1)
        self.assertEqual(self.terminal(a["terminal_id"])["inputs"][-1]["message"], "[群主] @合成甲 owner priority")
        self.assertFalse(self.api("GET")["relay_control"]["paused"])

    def test_styled_composers_and_hermes_stale_busy_complete_then_drain(self):
        self.create_group()
        for name, provider in (("合成甲", "claude_code"), ("合成乙", "hermes")):
            member = self.invite(name, provider, "developer")
            tid = member["terminal_id"]
            self.until("intro complete", lambda: not next(m for m in self.api("GET")["members"] if m["id"] == member["id"]).get("pending"))
            before = len(self.terminal(tid)["inputs"])
            self.next_turn(tid, "独立合成答案", complete_on_input=True,
                           completion_status="processing" if provider == "hermes" else "completed")
            self.api("POST", "/messages", {"text": "@" + name + " first"})
            self.until("first input accepted", lambda: len(self.terminal(tid)["inputs"]) == before + 1)
            self.next_turn(tid, "第二轮合成答案", complete_on_input=True)
            self.api("POST", "/messages", {"text": "@" + name + " second"})
            self.until("both inputs drain", lambda: len(self.terminal(tid)["inputs"]) == before + 2)
            self.until("completed after placeholder", lambda: not next(m for m in self.api("GET")["members"] if m["id"] == member["id"]).get("pending"))
            self.assertEqual(len([x for x in self.replies() if x["sender"] == name and x["text"] == "独立合成答案"]), 1)

    def test_hermes_fallback_extracts_only_final_answer_and_suppresses_failed_extraction(self):
        self.create_group()
        member = self.invite("合成成员", "hermes", "developer")
        tid = member["terminal_id"]
        self.until("initial Hermes turn", lambda: not self.api("GET")["members"][0].get("pending"))
        answer = "合成最终答复。\n状态：已交付"
        frame = "☤ Hermes\n\n" + answer + "\n─ 3.5s · read 2 files · 02:03 PM\n❯ \x1b[0;38;5;240;3mSynthetic hint?\x1b[0m\n"
        huge = "PRIVATE_TERMINAL_NOISE\n" * 7000 + frame
        self.next_turn(tid, answer, full_output=frame, last_output="[NO RESPONSE - agent completed without producing a text response (999 lines in buffer)]\n" + huge)
        self.api("POST", "/messages", {"text": "synthetic task"})
        replies = self.wait_replies(1)
        self.assertEqual(replies[0]["text"], answer)
        self.assertNotIn("PRIVATE_TERMINAL_NOISE", json.dumps(self.api("GET")))
        self.next_turn(tid, "unused", full_output="PRIVATE_BAD_SCREEN\n", last_output="[NO RESPONSE - agent completed without producing a text response (999 lines in buffer)]\n" + "PRIVATE_BAD_SCREEN\n" * 7000)
        self.api("POST", "/messages", {"text": "second synthetic task"})
        self.until("short extraction notice", lambda: any("未能可靠提取" in x["text"] for x in self.api("GET")["messages"]))
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(sum("未能可靠提取" in x["text"] for x in self.api("GET")["messages"]), 1)
        self.assertNotIn("PRIVATE_BAD_SCREEN", (self.data / "groups.json").read_text())

    def test_chinese_owner_input_preserves_twelve_kb_and_bounds_oversize(self):
        self.create_group()
        members = [self.invite("开发", "codex", "developer"),
                   self.invite("评审", "claude_code", "reviewer")]
        self.until("initial turns", lambda: all(not m.get("pending") for m in self.api("GET")["members"]))
        # Synthetic request-target cap, not a measurement of h11 or real CAO.
        self.http(self.cao_url, "POST", "/_test/config", {"max_input_target_bytes": 15000})
        self.api("PUT", "/automation", {"delivery_budget": 20})
        used = self.api("GET", "/automation")["runtime"]["deliveries_used"]
        for text in ("汉" * 1400, "中文😀 +%\n" * 1200):
            for member in members:
                with self.subTest(provider=member["provider"], chars=len(text)):
                    tid = member["terminal_id"]
                    expected = "[群主] @" + member["name"] + " " + text.strip()
                    before = len(self.terminal(tid)["inputs"])
                    self.next_turn(tid, "synthetic complete", complete_on_input=True)
                    self.api("POST", "/messages", {"text": "@" + member["name"] + " " + text})
                    self.until("one new input", lambda: len(self.terminal(tid)["inputs"]) == before + 1)
                    wire = self.terminal(tid)["inputs"][-1]
                    self.assertLessEqual(wire["target_bytes"], 14000)
                    if len(text) == 1400:
                        self.assertGreater(wire["target_bytes"], 12500)
                        self.assertEqual(wire["message"], expected)
                    else:
                        self.assertGreater(wire["target_bytes"], 13900)
                        self.assertIn("已截断", wire["message"])
                        self.assertIn(str(len(expected)), wire["message"])
                        prefix = wire["message"].split("\n（已截断")[0]
                        self.assertTrue(expected.startswith(prefix))
                        wire["message"].encode("utf-8").decode("utf-8")
                    self.until("turn finished", lambda: not next(m for m in self.api("GET")["members"] if m["id"] == member["id"]).get("pending"))
                    self.assertEqual(len(self.terminal(tid)["inputs"]), before + 1)
                    used += 1
                    self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], used)

    def test_legacy_oversize_failed_and_uncertain_jobs_retry_with_safe_query(self):
        self.create_group()
        member = self.invite("重试成员", "claude_code", "developer")
        tid = member["terminal_id"]
        self.until("initial turn", lambda: not self.api("GET")["members"][0].get("pending"))
        self.http(self.cao_url, "POST", "/_test/config", {"max_input_target_bytes": 15000})
        for index, state in enumerate(("failed", "uncertain")):
            with self.subTest(state=state):
                self.stopped_server = self.server
                self.stop_process(self.server)
                self.processes = [(proc, log) for proc, log in self.processes if proc is not self.server]
                stored = json.loads((self.data / "groups.json").read_text())
                group = stored[self.group_path.rsplit("/", 1)[1]]
                current = group["members"][0]
                original = "合成旧任务😀+" * 1000
                job = {"id": f"legacy-{index}", "attempt": 1, "text": original,
                       "hops": 2, "silent": False, "queued_at": time.time()}
                current.update(active_delivery=job, outbox=[], pending=state == "uncertain", delivery_state=state,
                               status="idle", reply_handled=False, dispatch_confirmed=False, boundary=None,
                               delivery_started=False, saw_busy=False, progress_generation=None,
                               sent_at=time.time(), last_progress_at=time.time(), delivery_warned=True)
                (self.data / "groups.json").write_text(json.dumps(stored))
                self.start_server()
                before = len(self.terminal(tid)["inputs"])
                self.next_turn(tid, "retry completed", complete_on_input=True)
                self.api("POST", f"/members/{member['id']}/retry", {"delivery_id": job["id"], "delivery_attempt": 1})
                def attempted():
                    item = self.api("GET")["members"][0]
                    return item if item.get("delivery_attempt") == 2 and item.get("delivery_state") in ("sent", "failed", "completed") or len(self.terminal(tid)["inputs"]) > before else None
                self.until("legacy retry attempt", attempted)
                self.assertEqual(len(self.terminal(tid)["inputs"]), before + 1)
                wire = self.terminal(tid)["inputs"][-1]
                self.assertLessEqual(wire["target_bytes"], 14000)
                self.assertGreater(wire["target_bytes"], 13900)
                self.assertIn("已截断", wire["message"])
                self.assertIn(str(len(original)), wire["message"])
                self.until("retry finished", lambda: not self.api("GET")["members"][0].get("active_delivery") and not self.api("GET")["members"][0].get("pending"))

    def test_gemini_echo_tail_and_long_reply_private_retention(self):
        self.create_group()
        member = self.invite("回显成员", "antigravity_cli", "developer")
        tid = member["terminal_id"]
        self.until("initial turn", lambda: not self.api("GET")["members"][0].get("pending"))
        task = "first synthetic line\n" + "SYNTHETIC_INPUT_BANNER " * 5 + "\ninput tail"
        echo = "SYNTHETIC_INPUT_BANNER " * 5 + "\ninput tail"
        answer = "合成自己的回答。" * 1200 + "\n状态：已交付"
        self.next_turn(tid, answer, last_output=echo + "\n○ Thought for 1.2s (synthetic title)\n" + answer)
        self.api("POST", "/messages", {"text": task})
        reply = self.wait_replies(1)[0]
        self.assertLessEqual(len(reply["text"]), 8000)
        self.assertNotIn("SYNTHETIC_INPUT_BANNER", reply["text"])
        self.assertNotIn("full_text", reply)
        groups = self.http(self.server_url, "GET", "/api/groups")
        self.assertNotIn("full_text", groups[0]["last"])
        private = json.loads((self.data / "groups.json").read_text())[self.group_path.rsplit("/", 1)[1]]
        self.assertEqual(private["messages"][-1]["full_text"], answer)

    def test_quota_structured_pause_recovery_fifo_and_restart(self):
        self.create_group()
        member = self.invite("额度成员", "codex", "developer")
        tid = member["terminal_id"]
        self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
        settings = self.api("PUT", "/automation", {"quota_enabled": True,
            "quota_reset_grace_seconds": 0, "quota_probe_interval_seconds": 0.1})
        self.assertTrue(settings["settings"]["quota_enabled"])
        import datetime
        def limits(percent):
            path = self.home / ".codex/sessions/synthetic/11/17/rollout-quota.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "event_msg", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "payload": {"type": "token_count", "rate_limits": {"primary": {
                    "used_percent": percent, "resets_at": time.time() + 0.5, "window_minutes": 300}}}}) + "\n")
        limits(100)
        self.until("structured quota pause", lambda: self.api("GET")["members"][0].get("quota_state") == "exhausted")
        baseline = len(self.terminal(tid)["inputs"])
        self.api("POST", "/messages", {"text": "第一条"})
        self.api("POST", "/messages", {"text": "第二条"})
        self.assertEqual(len(self.terminal(tid)["inputs"]), baseline)
        self.until("two queued messages", lambda: self.api("GET")["members"][0]["queued_count"] == 2)
        self.stopped_server = self.server
        self.stop_process(self.server)
        self.start_server()
        limits(0)
        self.wait_replies(2)
        delivered = [x["message"] for x in self.terminal(tid)["inputs"][baseline:]]
        self.assertEqual(delivered, ["[群主] 第一条", "[群主] 第二条"])
        runtime = self.api("GET", "/automation")["runtime"]
        self.assertGreater(runtime["quota_wait_seconds"][member["id"]], 0)

    def test_quota_hard_stop_auto_continuation_precedes_later_queue(self):
        self.create_group()
        member = self.invite("续接成员", "claude_code", "developer")
        tid = member["terminal_id"]
        self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
        self.api("PUT", "/automation", {"quota_enabled": True, "delivery_budget": 3,
            "quota_probe_interval_seconds": 0.15})
        baseline = len(self.terminal(tid)["inputs"])
        self.next_turn(tid, "unused", quota_prompt="You've hit your session limit")
        self.api("POST", "/messages", {"text": "当前任务"})
        self.until("quota interrupted", lambda: self.api("GET")["members"][0].get("quota_interrupted_delivery"))
        self.api("POST", "/messages", {"text": "后续任务"})
        self.until("later queued", lambda: self.api("GET")["members"][0]["queued_count"] == 1)
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/quota-clear", {})
        self.wait_replies(2)
        delivered = [item["message"] for item in self.terminal(tid)["inputs"][baseline:]]
        self.assertEqual(len(delivered), 3, delivered)
        self.assertEqual(delivered[0], "[群主] 当前任务")
        self.assertTrue(delivered[1].startswith("[自动续接 quota:"))
        self.assertNotIn("当前任务", delivered[1])
        self.assertEqual(delivered[2], "[群主] 后续任务")
        self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], 3)
        self.assertFalse(any("可能未送达" in item["text"] for item in self.api("GET")["messages"]))

    def test_quota_claude_structured_cache_pause_and_recovery(self):
        group = self.create_group()
        member = self.invite("缓存成员", "claude_code", "developer")
        tid = member["terminal_id"]
        self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
        self.api("PUT", "/automation", {"quota_enabled": True, "claude_limits": True,
            "quota_reset_grace_seconds": 0, "quota_probe_interval_seconds": 0.1})
        cache = self.data / "claude_limits" / f"{group['id']}.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        reset = time.time() + 0.6
        def limits(percent, resets):
            cache.write_text(json.dumps({"updated_at": time.time(), "rate_limits": {
                "five_hour": {"used_percentage": percent, "resets_at": resets}}}))
        limits(100, reset)
        self.until("Claude structured pause", lambda: self.api("GET")["members"][0].get("quota_state") == "exhausted")
        count = len(self.terminal(tid)["inputs"])
        self.api("POST", "/messages", {"text": "等窗口恢复"})
        self.until("queued", lambda: self.api("GET")["members"][0]["queued_count"] == 1)
        self.assertEqual(len(self.terminal(tid)["inputs"]), count)
        limits(0, reset + 100)
        self.wait_replies(1)
        self.assertEqual(len(self.terminal(tid)["inputs"]), count + 1)
        self.assertEqual(self.api("GET")["members"][0]["quota_state"], "ok")

    def test_quota_codex_menu_keys_exact_and_unknown_is_read_only(self):
        self.create_group()
        member = self.invite("菜单成员", "codex", "developer")
        tid = member["terminal_id"]
        self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
        self.api("PUT", "/automation", {"quota_enabled": True})
        # Labels from installed 0.164.0; these rendered numbers are synthetic.
        menu = "Approaching rate limits\n  1. Switch to another model\n› 2. Keep current model\n  3. Keep current model (never show again)"
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/quota", {"prompt": menu, "status": "waiting_user_answer"})
        self.until("exact menu keys", lambda: self.terminal(tid).get("keys") == ["2", "Enter"])
        # /key leaves FIFO history; normal readiness only appends a fresh prompt.
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/ready", {})
        self.api("POST", "/messages", {"text": "菜单关闭后继续"})
        self.wait_replies(1)
        self.assertEqual(self.terminal(tid)["keys"], ["2", "Enter"])
        budget = self.api("GET", "/automation")["runtime"]["deliveries_used"]
        unknown = "PRIVATE_MENU_SNIPPET\n" + menu.replace("2. Keep current model", "2. Keep the current model")
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/quota", {"prompt": unknown, "status": "waiting_user_answer"})
        self.until("unknown menu waits for owner", lambda: self.api("GET")["members"][0].get("quota_needs_owner"))
        self.assertEqual(self.terminal(tid)["keys"], ["2", "Enter"])
        self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], budget)
        self.assertNotIn("PRIVATE_MENU_SNIPPET", self.api("GET", "/log")["content"])
        self.assertNotIn("PRIVATE_MENU_SNIPPET", (self.data / "groups.json").read_text())

    def test_quota_recovered_history_then_new_rejection_in_appended_output(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.create_group()
                member = self.invite("边界成员", provider, "developer")
                tid = member["terminal_id"]
                self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
                self.api("PUT", "/automation", {"quota_enabled": True})
                # Existing installed-CLI literals; retain this hint as terminal history.
                hint = "You've hit your session limit" if provider == "claude_code" else "■ You’ve hit your usage limit."
                prompt = "────────────\n❯ \n────────────\n? for shortcuts" if provider == "claude_code" else "codex>\n? for shortcuts"
                self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/quota", {"prompt": hint + "\n" + prompt, "status": "idle"})
                self.until("quota pause", lambda: self.api("GET")["members"][0].get("quota_state") == "exhausted")
                self.api("POST", f"/members/{member['id']}/quota", {"action": "resume"})
                before = len(self.terminal(tid)["inputs"])
                self.next_turn(tid, "unused", hold=True, append_output=True)
                self.api("POST", "/messages", {"text": "new task"})
                self.until("new input accepted", lambda: len(self.terminal(tid)["inputs"]) == before + 1)
                work = "\n> new task\nPRIVATE_HISTORY_WORK"
                if provider == "codex":
                    work += chr(27) + "]7501;state=working:app=codex" + chr(27) + chr(92)
                polls = self.terminal(tid)["status_reads"]
                self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/progress", {"text": work})
                self.until("observe normal work", lambda: self.terminal(tid)["status_reads"] >= polls + 3)
                current = self.api("GET")["members"][0]
                self.assertEqual(current["quota_state"], "ok")
                self.assertTrue(current["pending"])
                self.assertIsNone(current.get("quota_interrupted_delivery"))
                self.assertIn(hint, self.terminal(tid)["full_out"])
                self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/progress", {"text": "\n" + hint})
                self.until("new quota rejection", lambda: self.api("GET")["members"][0].get("quota_state") == "exhausted")
                current = self.api("GET")["members"][0]
                self.assertEqual(current["quota_interrupted_delivery"], current["delivery_id"])
                self.assertEqual(len(self.terminal(tid)["inputs"]), before + 1)
                self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], 1)
                self.assertNotIn("PRIVATE_HISTORY_WORK", (self.data / "groups.json").read_text())
                self.assertNotIn("PRIVATE_HISTORY_WORK", self.api("GET", "/log")["content"])
                self.api("DELETE")

    def test_quota_terminal_unknown_manual_and_private_output(self):
        self.create_group()
        member = self.invite("额度成员", "claude_code", "developer")
        tid = member["terminal_id"]
        self.until("initial silent turn", lambda: not self.api("GET")["members"][0].get("pending"))
        self.api("PUT", "/automation", {"quota_enabled": True,
            "quota_probe_interval_seconds": 0.08, "quota_probe_max_failures": 2})
        # Claude Code 2.1.326 Zn/ZTe; no reset is also a supported rendering.
        self.next_turn(tid, "unused", quota_prompt="PRIVATE_SYNTHETIC_BODY\nYou've hit your session limit")
        self.api("POST", "/messages", {"text": "开始"})
        self.until("hard quota", lambda: self.api("GET")["members"][0].get("quota_state") == "exhausted")
        self.api("POST", "/messages", {"text": "随后"})
        self.until("owner escalation", lambda: self.api("GET")["members"][0].get("quota_needs_owner"))
        group = self.api("GET")
        self.assertEqual(sum("@群主" in x["text"] for x in group["messages"]), 1)
        self.assertFalse(any("可能未送达" in x["text"] for x in group["messages"]))
        self.assertNotIn("PRIVATE_SYNTHETIC_BODY", json.dumps(group))
        self.assertNotIn("PRIVATE_SYNTHETIC_BODY", self.api("GET", "/log")["content"])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/quota-clear", {})
        self.api("POST", f"/members/{member['id']}/quota", {"action": "resume"})
        self.until("quota resumed", lambda: self.api("GET")["members"][0].get("quota_state") == "ok")
        self.api("POST", f"/members/{member['id']}/quota", {"action": "pause"})
        self.assertEqual(self.api("GET")["members"][0]["quota_state"], "exhausted")

    def test_fake_rejects_duplicate_session_creation_with_the_real_400(self):
        self.create_group()
        member=self.invite("开发","codex","developer")
        terminal=self.terminal(member["terminal_id"])
        query=parse.urlencode({
            "session_name":terminal["session_name"],"agent_profile":terminal["profile"],"provider":"codex"})
        with self.assertRaises(error.HTTPError) as duplicate:
            self.http(self.cao_url,"POST","/sessions?"+query,{})
        with duplicate.exception as response:
            self.assertEqual(response.code,400)
            self.assertEqual(json.load(response)["detail"],f"Session '{terminal['session_name']}' already exists")
        state=self.fake_state()
        self.assertEqual(len(state["sessions"]),1)
        self.assertEqual(len(state["terminals"]),1)
        self.assertIn(next(iter(state["sessions"].values()))["status"],("active","detached"))

    def test_concurrent_new_members_create_one_session_and_all_join(self):
        self.create_group()
        self.http(self.cao_url,"POST","/_test/config",{"hold_create":True})
        try:
            self.concurrent_posts([(self.group_path+"/members",{"name":name,"provider":provider})
                for name,provider in (("开发","codex"),("评审","claude_code"),("统筹","codex"))])
            self.until("concurrent profiles installed",lambda:len(self.fake_state()["profiles"])==3)
            self.until("session creation held",lambda:self.fake_state()["create_waiting"])
        finally:
            self.http(self.cao_url,"POST","/_test/config",{"hold_create":False})
        group=self.startup_settled(self.group_path,3)
        self.assertTrue(all(m["terminal_id"] for m in group["members"]),group["messages"])
        state=self.fake_state()
        self.assertEqual([r["status"] for r in state["create_requests"] if r["path"]=="/sessions"],[201])
        self.assertEqual(len(state["sessions"]),1)
        self.assertEqual({t["session_name"] for t in state["terminals"].values()},{group["session_name"]})

    def test_concurrent_offline_restarts_reuse_or_rebuild_one_session(self):
        self.create_group()
        for name,provider in (("开发","codex"),("评审","claude_code"),("统筹","codex")):
            self.invite(name,provider,"developer")
        group=self.startup_settled(self.group_path,3)
        for missing in (False,True):
            old=self.offline_group(self.group_path)
            if missing:
                self.http(self.cao_url,"DELETE",f"/sessions/{group['session_name']}")
            before=len([r for r in self.fake_state()["create_requests"] if r["path"]=="/sessions"])
            self.http(self.cao_url,"POST","/_test/config",{"hold_create":True})
            try:
                self.concurrent_posts([(self.group_path+f"/members/{m['id']}/restart",{}) for m in group["members"]])
                self.until("restart creation held",lambda:self.fake_state()["create_waiting"])
            finally:
                self.http(self.cao_url,"POST","/_test/config",{"hold_create":False})
            group=self.startup_settled(self.group_path,3)
            self.assertTrue(all(m["terminal_id"] and m["terminal_id"] not in old for m in group["members"]),group["messages"])
            state=self.fake_state()
            creates=[r for r in state["create_requests"] if r["path"]=="/sessions"]
            self.assertEqual(len(creates)-before,int(missing))
            self.assertTrue(all(r["status"]==201 for r in creates))
            self.assertEqual(len(state["sessions"]),1)
            self.assertEqual({t["session_name"] for t in state["terminals"].values()},{group["session_name"]})

    def test_terminated_session_with_failure_records_is_recreated_on_restart(self):
        group=self.create_group()
        members=[self.invite(name,provider,"developer") for name,provider in
                 (("开发","codex"),("评审","claude_code"),("统筹","codex"))]
        self.until("silent notices consumed before provider failure",lambda:
                   all(not m.get("pending") and not m.get("queued_count") for m in self.api("GET")["members"]))
        session=group["session_slug"]
        old={m["terminal_id"] for m in members}
        self.http(self.cao_url,"POST",f"/_test/sessions/{session}/terminate",{})
        record=self.http(self.cao_url,"GET",f"/sessions/{session}")
        self.assertEqual(record["session"]["status"],"terminated")
        self.assertEqual(len(record["terminals"]),3)
        self.assertTrue(all(t["status"]=="error" and t["deferred_init_failure"] for t in record["terminals"]))
        query=parse.urlencode({"agent_profile":self.terminal(members[0]["terminal_id"])["profile"],"provider":"codex"})
        with self.assertRaises(error.HTTPError) as cannot_join:
            self.http(self.cao_url,"POST",f"/sessions/{session}/terminals:operator?"+query,{})
        self.assertEqual(cannot_join.exception.code,404)
        cannot_join.exception.close()
        self.until("GUI observes failed provider records",lambda:
                   all(m["status"]=="error" for m in self.api("GET")["members"]))
        before=sum(r["path"]=="/sessions" and r["status"]==201 for r in self.fake_state()["create_requests"])
        # Keep one failure record so GET must remain 200/terminated while the
        # other two members concurrently stop their old records and restart.
        self.concurrent_posts([(self.group_path+f"/members/{m['id']}/restart",{}) for m in members[:2]])
        recovered=self.startup_settled(self.group_path,3)
        self.assertTrue(all(m["terminal_id"] and m["terminal_id"] not in old
                            for m in recovered["members"][:2]),recovered["messages"])
        self.assertEqual(self.http(self.cao_url,"GET",f"/sessions/{session}")["session"]["status"],"detached")
        self.api("POST",f"/members/{members[2]['id']}/restart",{})
        self.until("all failed members join new backend",lambda:
                   all(m["terminal_id"] and m["terminal_id"] not in old and m.get("ready_terminal")==m["terminal_id"]
                       for m in self.api("GET")["members"]))
        state=self.fake_state()
        self.assertEqual(sum(r["path"]=="/sessions" and r["status"]==201 for r in state["create_requests"])-before,1)
        self.assertEqual(len(state["sessions"]),1)
        self.assertEqual({t["session_name"] for t in state["terminals"].values()},{session})

    def test_a_blocked_group_does_not_block_another_groups_startup(self):
        first=self.create_group();first_path=self.group_path
        second=self.create_group();second_path=self.group_path
        self.http(self.cao_url,"POST","/_test/config",{"hold_sessions":[first["session_slug"]]})
        release_client=threading.Event()
        def start_first():
            result=self.http(self.server_url,"POST",first_path+"/members",{"name":"甲","provider":"codex"})
            release_client.wait(timeout=5)
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_call=pool.submit(start_first)
            try:
                self.until("first group held before starting second",lambda:
                    self.fake_state()["create_waiting_sessions"].get(first["session_slug"]))
                second_call=pool.submit(self.http,self.server_url,"POST",second_path+"/members",
                                       {"name":"乙","provider":"claude_code"})
                second_call.result(timeout=3)
                ready=self.startup_settled(second_path,1,timeout=3)
                self.assertTrue(ready["members"][0]["terminal_id"])
                self.assertGreater(self.fake_state()["create_waiting_sessions"][first["session_slug"]],0)
                self.assertIsNone(self.http(self.server_url,"GET",first_path)["members"][0]["terminal_id"])
            finally:
                release_client.set()
                self.http(self.cao_url,"POST","/_test/config",{"hold_sessions":[]})
            first_call.result(timeout=3)
        self.assertTrue(self.startup_settled(first_path,1)["members"][0]["terminal_id"])
        self.assertEqual(set(self.fake_state()["sessions"]),{first["session_slug"],second["session_slug"]})

    def test_a_failed_creator_releases_its_group_for_other_members_and_retry(self):
        group=self.create_group()
        self.http(self.cao_url,"POST","/_test/config",{"hold_sessions":[group["session_slug"]]})
        bad=self.api("POST","/members",{"name":"失败成员","provider":"codex"})
        held=self.until("first creator held",lambda:self.fake_state()["create_requests"])
        failed_profile=held[0]["profile"]
        try:
            self.http(self.cao_url,"POST","/_test/config",{"fail_profiles":[failed_profile]})
            self.concurrent_posts([(self.group_path+"/members",{"name":name,"provider":"claude_code"})
                                   for name in ("成员甲","成员乙")])
            self.until("all profiles installed before failure",lambda:len(self.fake_state()["profiles"])==3)
        finally:
            self.http(self.cao_url,"POST","/_test/config",{"hold_sessions":[]})
        settled=self.startup_settled(self.group_path,3)
        self.assertEqual(next(m for m in settled["members"] if m["id"]==bad["id"])["status"],"failed")
        self.assertTrue(all(m["terminal_id"] for m in settled["members"] if m["id"]!=bad["id"]))
        self.assertEqual(len(self.fake_state()["sessions"]),1)
        self.http(self.cao_url,"POST","/_test/config",{"fail_profiles":[]})
        self.api("POST",f"/members/{bad['id']}/restart",{})
        self.until("failed creator can retry",lambda:all(m["terminal_id"] and m.get("ready_terminal")==m["terminal_id"]
                   for m in self.api("GET")["members"]))
        creates=[r for r in self.fake_state()["create_requests"] if r["path"]=="/sessions"]
        self.assertEqual(sum(r["status"]==201 for r in creates),1)
        self.assertEqual({t["session_name"] for t in self.fake_state()["terminals"].values()},{group["session_slug"]})

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
                         [("开发", dev["id"], "• " + dev_reply), ("评审", review["id"], review_reply)])
        dev_state, review_state = self.terminal(dev_tid), self.terminal(review_tid)
        self.assertEqual(dev_state["inputs"][-1]["message"], "[群主] " + owner_text)
        self.assertEqual([turn["message"] for turn in review_state["inputs"]],
                         ["[来自 开发] • " + dev_reply])
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

        # Exercise a new turn through the restored group and its existing terminal.
        resumed_text = "@开发 重启后再处理一轮"
        resumed_reply = "重启后的本轮回复已送达"
        self.next_turn(dev_tid, resumed_reply)
        resumed_owner = self.api("POST", "/messages", {"text": resumed_text})
        self.assertEqual((resumed_owner["kind"], resumed_owner["sender"], resumed_owner["to"]),
                         ("owner", "群主", ["开发"]))
        resumed_replies = self.wait_replies(3)
        self.assertEqual(resumed_replies[:2], replies)
        self.assertEqual((resumed_replies[2]["sender"], resumed_replies[2]["member_id"],
                          resumed_replies[2]["text"]), ("开发", dev["id"], "• " + resumed_reply))
        self.assertEqual(self.api("GET")["messages"],
                         before["messages"] + [resumed_owner, resumed_replies[2]])
        self.assertEqual(resumed_owner["seq"], before["messages"][-1]["seq"] + 1)
        self.assertEqual(resumed_replies[2]["seq"], resumed_owner["seq"] + 1)
        resumed_state = self.terminal(dev_tid)
        self.assertEqual(len(resumed_state["inputs"]), len(dev_state["inputs"]) + 1)
        self.assertEqual(resumed_state["inputs"][-1]["message"], "[群主] " + resumed_text)
        self.assertGreater(resumed_state["status_generation"],
                           resumed_state["inputs"][-1]["dispatch_boundary"])

    def test_codex_idle_stream_waits_for_new_done_then_relays_final_reply(self):
        self.create_group()
        dev = self.invite("开发", "codex", "developer")
        review = self.invite("评审", "claude_code", "reviewer")
        tid, review_tid = dev["terminal_id"], review["terminal_id"]
        self.until("silent notice dispatched", lambda: self.terminal(tid)["inputs"])
        self.until("silent notice consumed", lambda: self.api("GET")["members"][0].get("reply_handled"))
        previous = self.terminal(tid)
        self.assertIn("state=done", previous["full_out"])
        self.assertEqual(self.replies(), [])
        interim = "仅中途文字，@评审 不应收到这条"
        final = "本轮最终回复，@评审 请审查。"
        reviewed = "已收到本轮最终回复。"
        self.next_turn(tid, final, idle_stream=True, interim=interim, append_output=True)
        self.next_turn(review_tid, reviewed)
        self.api("POST", "/messages", {"text": "@开发 处理本轮任务"})

        def observed(phase):
            state = self.terminal(tid)
            turn = state["inputs"][-1]
            return turn["idle_stream"] and turn["phase_reads"][phase] >= 2

        for phase in (0, 1):
            self.until(f"idle phase {phase} observed twice", lambda: observed(phase))
            state = self.terminal(tid)
            self.assertEqual(state["status"], "idle")
            self.assertGreater(state["status_generation"], state["inputs"][-1]["dispatch_boundary"])
            self.assertTrue(self.api("GET")["members"][0]["pending"])
            self.assertEqual(self.replies(), [])
            self.assertEqual(state["last_reads"], previous["last_reads"])
            self.assertEqual(self.terminal(review_tid)["inputs"], [])
            self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")

        replies = self.wait_replies(2)
        self.assertEqual([(m["sender"], m["text"]) for m in replies],
                         [("开发", "• " + final), ("评审", reviewed)])
        self.assertFalse(any(interim in m["text"] for m in self.api("GET")["messages"]))
        self.assertEqual([turn["message"] for turn in self.terminal(review_tid)["inputs"]],
                         ["[来自 开发] • " + final])
        self.assertEqual(set(self.terminal(tid)["inputs"][-1]["reported_statuses"]), {"idle"})
        self.assertEqual(self.terminal(tid)["last_reads"], previous["last_reads"] + 1)

    def test_codex_long_output_remembers_working_after_fifo_eviction(self):
        self.create_group()
        member=self.invite("开发","codex","developer")
        tid=member["terminal_id"]
        final="长任务最终回复"
        self.next_turn(tid,final,idle_stream=True,interim="长任务进行中",padding_bytes=40000)
        self.api("POST","/messages",{"text":"@开发 长任务"})
        self.until("long task input dispatched",lambda:self.terminal(tid)["inputs"])
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/advance")
        self.until("working remembered",lambda:self.api("GET")["members"][0].get("codex_saw_working"))
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/advance")
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["• " + final])
        state=self.http(self.cao_url,"GET","/_test/state")
        raw=state["terminals"][tid]["full_out"]
        self.assertEqual(len(raw.encode()),state["buffer_limit"])
        self.assertEqual(state["buffer_limit"],32768)
        self.assertNotIn("state=working",raw)
        self.assertEqual(raw.count("state=done"),1)

    def test_startup_messages_wait_for_both_cli_prompts_and_deliver_once(self):
        self.http(self.cao_url,"POST","/_test/config",{"hold_create":True,"startup_hold":True})
        group=self.http(self.server_url,"POST","/api/groups",{
            "name":"启动验收","workdir":str(self.root/"project"),"members":[
                {"name":"开发","provider":"codex"},{"name":"评审","provider":"claude_code"}]})
        self.group_path=f"/api/groups/{group['id']}"
        self.until("create blocked before terminal exists",lambda:
                   self.http(self.cao_url,"GET","/_test/state")["create_waiting"])
        text="@开发 @评审 启动后执行"
        self.api("POST","/messages",{"text":text})
        def queued():
            members=self.api("GET")["members"]
            return members if all(m.get("queued_count")==1 for m in members) else None
        members=self.until("both messages queued before terminal creation",queued)
        self.assertTrue(all(m["terminal_id"] is None for m in members))
        self.http(self.cao_url,"POST","/_test/config",{"hold_create":False})
        self.until("terminals allocated",lambda:all(m["terminal_id"] for m in self.api("GET")["members"]))
        members=self.api("GET")["members"]
        self.until("false idle loading observed",lambda:all(
            self.terminal(m["terminal_id"])["status_reads"]>=2 for m in members))
        for m in members:
            self.assertEqual(self.terminal(m["terminal_id"])["inputs"],[])
            self.next_turn(m["terminal_id"],m["name"]+"启动后完成")
            self.http(self.cao_url,"POST",f"/_test/terminals/{m['terminal_id']}/ready")
        replies=self.wait_replies(2)
        self.assertEqual({m["text"] for m in replies},{"• 开发启动后完成","评审启动后完成"})
        for m in members:
            state=self.terminal(m["terminal_id"])
            self.assertEqual(state["early_inputs"],0)
            self.assertEqual([x["message"] for x in state["inputs"]],["[群主] "+text])

    def test_startup_timeout_and_restart_preserve_the_job_for_manual_retry(self):
        self.http(self.cao_url,"POST","/_test/config",{"startup_hold":True})
        self.create_group()
        member=self.api("POST","/members",{"name":"开发","provider":"codex"})
        original="@开发 启动超时后恢复原消息"
        self.api("POST","/messages",{"text":original})
        self.until("startup timeout visible",lambda:self.api("GET")["members"][0].get("delivery_state")=="not_ready")
        blocked=self.api("GET")["members"][0]
        self.assertEqual(blocked["queued_count"],1)
        self.assertTrue(blocked["can_retry"])
        self.assertEqual(self.terminal(blocked["terminal_id"])["inputs"],[])
        self.stopped_server=self.server
        self.stop_process(self.stopped_server)
        self.start_server()
        restored=self.api("GET")["members"][0]
        self.assertEqual((restored["delivery_id"],restored["queued_count"]),(blocked["delivery_id"],1))
        tid=restored["terminal_id"]
        self.next_turn(tid,"启动恢复后完成",complete_on_input=True)
        reads=self.terminal(tid)["status_reads"]
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready")
        self.until("ready observed without automatic retry",lambda:self.terminal(tid)["status_reads"]>=reads+2)
        self.assertEqual(self.terminal(tid)["inputs"],[])
        self.api("POST",f"/members/{member['id']}/retry",{
            "delivery_id":restored["delivery_id"],"delivery_attempt":restored["delivery_attempt"]})
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["• 启动恢复后完成"])
        self.assertEqual([x["message"] for x in self.terminal(tid)["inputs"]],["[群主] "+original])
        self.assertEqual(sum(m["kind"]=="owner" for m in self.api("GET")["messages"]),1)

    def test_late_working_and_done_recover_after_warning_without_resend(self):
        self.create_group()
        member=self.invite("开发","codex","developer")
        tid=member["terminal_id"]
        self.next_turn(tid,"迟到的本轮回复",idle_stream=True,interim="尚无本轮工作标记")
        self.api("POST","/messages",{"text":"@开发 等待迟到进展"})
        self.until("no progress warning",lambda:self.api("GET")["members"][0].get("can_retry"))
        self.assertEqual(self.replies(),[])
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/advance")
        self.until("late progress clears warning state",lambda:not self.api("GET")["members"][0].get("can_retry"))
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/advance")
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["• 迟到的本轮回复"])
        self.assertEqual(len(self.terminal(tid)["inputs"]),1)

    def test_unrecognized_prompt_has_an_explicit_manual_retry_exit(self):
        self.http(self.cao_url,"POST","/_test/config",{"startup_hold":True})
        self.create_group()
        member=self.api("POST","/members",{"name":"评审","provider":"claude_code"})
        original="@评审 未知输入框的原消息"
        self.api("POST","/messages",{"text":original})
        tid=self.until("unknown prompt terminal allocated",lambda:self.api("GET")["members"][0]["terminal_id"])
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready",{"prompt":"unrecognized localized input"})
        self.until("unknown feature startup timeout",lambda:self.api("GET")["members"][0].get("delivery_state")=="not_ready")
        blocked=self.api("GET")["members"][0]
        self.assertEqual(self.terminal(tid)["inputs"],[])
        self.next_turn(tid,"人工兜底完成",complete_on_input=True)
        token={"delivery_id":blocked["delivery_id"],"delivery_attempt":blocked["delivery_attempt"]}
        self.api("POST",f"/members/{member['id']}/retry",token)
        with self.assertRaises(error.HTTPError) as stale:
            self.api("POST",f"/members/{member['id']}/retry",token)
        self.assertEqual(stale.exception.code,400)
        stale.exception.close()
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["人工兜底完成"])
        self.assertEqual([turn["message"] for turn in self.terminal(tid)["inputs"]],["[群主] "+original])
        self.assertEqual(sum(m["kind"]=="owner" for m in self.api("GET")["messages"]),1)

    def test_dropped_delivery_warns_once_and_manual_retry_is_idempotent(self):
        self.create_group()
        self.invite("开发","codex","developer")
        self.invite("评审","claude_code","reviewer")
        self.until("silent notice consumed",lambda:self.api("GET")["members"][0].get("reply_handled"))
        members=self.api("GET")["members"]
        for m in members:
            self.next_turn(m["terminal_id"],"不会到达",drop=True)
        original="@开发 @评审 这轮投递被丢弃"
        self.api("POST","/messages",{"text":original})
        self.until("both deliveries visibly uncertain",lambda:all(
            m.get("delivery_state")=="uncertain" for m in self.api("GET")["members"]))
        warned=self.api("GET")
        self.assertEqual(sum("可能未送达" in m["text"] for m in warned["messages"]),2)
        self.assertTrue(all(m["can_retry"] for m in warned["members"]))
        for m in warned["members"]:
            before=len(self.terminal(m["terminal_id"])["inputs"])
            self.next_turn(m["terminal_id"],m["name"]+"重发后完成",complete_on_input=True)
            token={"delivery_id":m["delivery_id"],"delivery_attempt":m["delivery_attempt"]}
            gate=threading.Barrier(2)
            def retry_once():
                gate.wait(timeout=2)
                try:
                    self.api("POST",f"/members/{m['id']}/retry",token)
                    return 200
                except error.HTTPError as exc:
                    with exc:
                        return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:retry_once(),range(2)))
            self.assertEqual(sorted(results),[200,400])
            with self.assertRaises(error.HTTPError) as failed:
                self.api("POST",f"/members/{m['id']}/retry",token)
            self.assertEqual(failed.exception.code,400)
            failed.exception.close()
            self.until("retry dispatched after prompt stability", lambda: len(
                self.terminal(m["terminal_id"])["inputs"])==before+1)
            self.assertEqual(self.terminal(m["terminal_id"])["inputs"][-1]["message"],"[群主] "+original)
        self.assertEqual({m["text"] for m in self.wait_replies(2)}, {"• 开发重发后完成","评审重发后完成"})
        settled=self.api("GET")
        self.assertTrue(all(not m["pending"] and not m["can_retry"] for m in settled["members"]))
        self.assertEqual(sum("可能未送达" in m["text"] for m in settled["messages"]),2)

    def assert_fast_replies_remain_complete(self, expected):
        def terminal_states():
            members=self.api("GET")["members"]
            return members if all(m.get("delivery_state") in ("completed","uncertain") for m in members) else None
        members=self.until("fast completion or erroneous watchdog warning",terminal_states)
        self.assertEqual({m["text"] for m in self.replies()},set(expected))
        self.assertTrue(all(m["delivery_state"]=="completed" and not m["pending"] and
                            m["delivery_started"] and not m["can_retry"] for m in members))
        reads={m["terminal_id"]:self.terminal(m["terminal_id"])["status_reads"] for m in members}
        # Observe polling beyond the actual dispatch deadlines, without a fixed sleep.
        deadline=max(m["sent_at"] for m in members)+float(self.env["GROUPCHAT_DELIVERY_TIMEOUT"])
        self.until("completed deliveries remain settled beyond watchdog deadline",lambda:
                   time.time()>deadline and all(self.terminal(tid)["status_reads"]>=count+2 for tid,count in reads.items()))
        self.assertFalse(any("可能未送达" in m["text"] for m in self.api("GET")["messages"]))
        self.assertEqual({m["text"] for m in self.replies()},set(expected))

    def test_both_providers_complete_before_first_poll_during_cold_start(self):
        self.http(self.cao_url,"POST","/_test/config",{"hold_create":True,"startup_hold":True})
        group=self.http(self.server_url,"POST","/api/groups",{
            "name":"冷启动快回复","workdir":str(self.root/"project"),"members":[
                {"name":"cx","provider":"codex","effort":"low"},
                {"name":"cc","provider":"claude_code","model":"sonnet","effort":"low"}]})
        self.group_path=f"/api/groups/{group['id']}"
        self.until("cold creation blocked",lambda:self.http(self.cao_url,"GET","/_test/state")["create_waiting"])
        text="@所有人 只回复：收到，我是<名字>"
        self.api("POST","/messages",{"text":text})
        self.until("broadcast queued before allocation",lambda:all(m.get("queued_count")==1 for m in self.api("GET")["members"]))
        self.http(self.cao_url,"POST","/_test/config",{"hold_create":False})
        self.until("both cold terminals allocated",lambda:all(m["terminal_id"] for m in self.api("GET")["members"]))
        members=self.api("GET")["members"]
        for m in members:
            tid=m["terminal_id"]
            self.assertEqual(self.terminal(tid)["inputs"],[])
            self.next_turn(tid,"收到，我是"+m["name"],complete_on_input=True,
                           padding_bytes=6600 if m["provider"]=="codex" else 0)
            self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready")
        self.assert_fast_replies_remain_complete(["• 收到，我是cx","收到，我是cc"])
        for m in members:
            state=self.terminal(m["terminal_id"])
            self.assertEqual([turn["message"] for turn in state["inputs"]],["[群主] "+text])
            self.assertTrue(state["inputs"][0]["complete_on_input"])
            self.assertGreater(state["output_generation"],state["inputs"][0]["dispatch_boundary"])
            if m["provider"]=="codex":
                # Independently assert the owner's literal bytes, not fake constants.
                self.assertIn(b"\x1b]7501;state=working:app=codex\x1b\\",state["full_out"].encode())
                self.assertIn(b"\x1b]7501;state=done:app=codex\x1b\\",state["full_out"].encode())
                self.assertLess(len(state["full_out"].encode()),32768)
                self.assertTrue(self.api("GET")["members"][0]["codex_saw_working"])

    def test_fast_codex_done_survives_eviction_before_the_first_poll(self):
        self.http(self.cao_url,"POST","/_test/config",{"startup_hold":True})
        self.create_group()
        self.api("POST","/members",{"name":"cx","provider":"codex","effort":"low"})
        self.api("POST","/messages",{"text":"@所有人 只回复：收到，我是cx"})
        tid=self.until("cold codex terminal allocated",lambda:self.api("GET")["members"][0]["terminal_id"])
        self.next_turn(tid,"收到，我是cx",complete_on_input=True,padding_bytes=40000)
        previous="\x1b]7501;state=working:app=codex\x1b\\旧回复\x1b]7501;state=done:app=codex\x1b\\\x1b]7501;state=done:app=codex\x1b\\"
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready",{"prompt":previous+"\ncodex>\n? for shortcuts"})
        self.assert_fast_replies_remain_complete(["• 收到，我是cx"])
        member=self.api("GET")["members"][0]
        state=self.terminal(tid)
        self.assertEqual(member["codex_dispatch_markers"],3)
        self.assertTrue(member["codex_buffer_reset"])
        self.assertFalse(member["codex_saw_working"])
        self.assertEqual(len(state["full_out"].encode()),32768)
        self.assertNotIn("state=working",state["full_out"])
        self.assertEqual(state["full_out"].count("state=done"),1)
        self.assertTrue(state["inputs"][0]["complete_on_input"])
        self.assertEqual(len(state["inputs"]),1)

    def test_codex_buffer_clear_allows_fast_and_identical_short_replies(self):
        self.create_group()
        member = self.invite("开发", "codex", "developer")
        tid = member["terminal_id"]
        warmup = "上一轮足够长的正文，作为发送前缓冲区"
        self.next_turn(tid, warmup)
        self.api("POST", "/messages", {"text": "@开发 准备上一轮"})
        self.assertEqual(self.wait_replies(1)[0]["text"], "• " + warmup)
        for count in (2, 3):
            previous = self.terminal(tid)
            self.assertIn("state=done", previous["full_out"])
            self.next_turn(tid, "收到", complete_on_input=True)
            self.api("POST", "/messages", {"text": f"@开发 第{count}轮短回复"})
            replies = self.wait_replies(count)
            self.assertEqual([m["text"] for m in replies], ["• " + warmup] + ["• 收到"] * (count - 1))
            state = self.terminal(tid)
            self.assertTrue(state["inputs"][-1]["complete_on_input"])
            self.assertFalse(state["inputs"][-1]["append_output"])
            self.assertNotIn(warmup, state["full_out"])
            self.assertEqual(state["full_out"].count("state=working"), 1)
            self.assertEqual(state["full_out"].count("state=done"), 1)
            if count == 3:
                self.assertEqual(state["full_out"], previous["full_out"])
            self.assertFalse(self.api("GET")["members"][0]["pending"])

    def test_codex_cleared_late_old_done_waits_for_new_working_and_done(self):
        self.create_group()
        member = self.invite("开发", "codex", "developer")
        tid = member["terminal_id"]
        self.next_turn(tid, "旧轮不应入群", idle_stream=True, interim="旧轮执行中")
        # Simulate work begun outside the GUI. New GUI jobs now queue while its
        # own delivery is pending, so they must not overwrite that delivery.
        self.http(self.cao_url, "POST", f"/terminals/{tid}/input?message=external-old-turn")
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")
        self.assertIn("state=working", self.terminal(tid)["full_out"])
        final = "只有新轮最终回复可以入群"
        self.next_turn(tid, final, idle_stream=True, interim="旧轮收尾文字",
                       raw_prefix="\x1b]7501;state=done:app=codex\x1b\\")
        self.api("POST", "/messages", {"text": "@开发 新任务"})
        self.until("old done polled twice after buffer clear", lambda:
                   len(self.terminal(tid)["inputs"]) == 2 and
                   self.terminal(tid)["inputs"][-1]["phase_reads"][0] >= 2)
        self.assertEqual(self.replies(), [])
        self.assertTrue(self.api("GET")["members"][0]["pending"])
        self.assertNotIn("state=working", self.terminal(tid)["full_out"])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")
        self.assertEqual([m["text"] for m in self.wait_replies(1)], ["• " + final])

    def test_claude_limits_opt_in_conflict_capture_usage_and_member_restart(self):
        group = self.create_group()
        member = self.invite("额度成员", "claude_code", "developer", effort="low", plan="pro")
        settings_file = Path(group["workdir"]) / ".claude" / "settings.local.json"
        self.assertFalse(settings_file.exists())
        settings_file.parent.mkdir()
        original = '{"statusLine":{"type":"command","command":"user-bar"},"permissions":{"allow":["Read"]}}'
        settings_file.write_text(original)
        with self.assertRaises(error.HTTPError) as caught:
            self.api("PUT", "/automation", {"claude_limits": True})
        with caught.exception as response:
            self.assertEqual(response.code, 400)
            self.assertIn("statusLine", json.load(response)["error"])
        self.assertEqual(settings_file.read_text(), original)
        self.assertEqual(self.api("GET")["members"][0]["terminal_id"], member["terminal_id"])
        self.assertFalse(self.api("GET", "/automation")["settings"]["claude_limits"])
        settings_file.write_text('{"permissions":{"allow":["Read"]}}')
        self.api("PUT", "/automation", {"claude_limits": True})
        settings = json.loads(settings_file.read_text())
        argv = shlex.split(settings["statusLine"]["command"])
        future = time.time() + 7200
        capture = subprocess.run(argv, input=json.dumps({"rate_limits": {
            "seven_day": {"used_percentage": 24, "resets_at": future}}, "cwd": "must-not-save",
            "transcript_path": "must-not-save", "messages": ["must-not-save"]}),
            capture_output=True, text=True, cwd=self.root, env=self.env, timeout=5)
        self.assertEqual((capture.returncode, capture.stdout, capture.stderr), (0, "", ""))
        self.assertNotIn("must-not-save", Path(argv[-1]).read_text())
        usage = self.api("GET", "/usage")["members"][member["id"]]
        self.assertIsNone(usage["usage"])
        self.assertEqual([(w["window_minutes"], w["used_percent"]) for w in usage["limits"]["windows"]], [(10080, 24)])
        self.assertIsNotNone(usage["limits"]["updated_at"])
        self.api("POST", f"/members/{member['id']}/restart", {})
        current = self.until("Claude restarted with project-local quota collector", lambda:
                   next((m for m in self.api("GET")["members"] if m["terminal_id"] and
                         m["terminal_id"] != member["terminal_id"] and m.get("ready_terminal") == m["terminal_id"]), None))
        self.assertEqual(current["effort"], "low")
        self.assertEqual(json.loads(settings_file.read_text()), settings)
        self.api("PUT", "/automation", {"claude_limits": False})
        self.assertEqual(json.loads(settings_file.read_text()), {"permissions": {"allow": ["Read"]}})
        self.assertIsNone(self.api("GET", "/usage")["members"][member["id"]]["limits"])

    def test_claude_quota_settings_failures_do_not_block_restart_disable_or_dissolve(self):
        group = self.create_group()
        member = self.invite("额度容错", "claude_code", "developer", effort="low", plan="pro")
        usage = self.api("GET", "/usage")["members"][member["id"]]
        self.assertFalse(usage["limits_available"])
        self.assertIsNone(usage["limits"])
        settings_file = Path(group["workdir"]) / ".claude" / "settings.local.json"
        for raw in ('{"statusLine":{"type":"command","command":"user-bar"}}', '{broken'):
            settings_file.parent.mkdir(exist_ok=True)
            settings_file.write_text('{}')
            self.api("PUT", "/automation", {"claude_limits": True})
            settings_file.write_text(raw)
            old_tid = self.api("GET")["members"][0]["terminal_id"]
            self.api("POST", f"/members/{member['id']}/restart", {})
            def settled():
                current = self.api("GET")["members"][0]
                return current if current["status"] == "failed" or (
                    current["terminal_id"] and current["terminal_id"] != old_tid and
                    current.get("ready_terminal") == current["terminal_id"]) else None
            current = self.until("quota failure does not block member restart", settled)
            self.assertNotEqual(current["status"], "failed")
            self.assertEqual(current["effort"], "low")
            self.next_turn(current["terminal_id"], "配置冲突仍能回复")
            previous = len(self.replies())
            self.api("POST", "/messages", {"text": "只回复测试内容"})
            self.assertEqual(self.wait_replies(previous + 1)[-1]["text"], "配置冲突仍能回复")
            result = self.api("PUT", "/automation", {"claude_limits": False})
            self.assertFalse(result["settings"]["claude_limits"])
            self.assertEqual(len(result["warnings"]), 1)
            self.assertEqual(settings_file.read_text(), raw)
            self.assertFalse(self.api("GET", "/usage")["members"][member["id"]]["limits_available"])
        settings_file.write_text('{}')
        self.api("PUT", "/automation", {"claude_limits": True})
        settings_file.write_text('{broken')
        result = self.api("DELETE")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(settings_file.read_text(), '{broken')
        self.assertEqual(self.http(self.server_url, "GET", "/api/groups"), [])

    def check_rendered_work_states_stop_idle_reminders(self, provider):
        self.create_group()
        member = self.invite("协调", provider, "supervisor")
        self.api("PUT", "/automation", {"idle_minutes": 1, "reminder_cooldown_minutes": 0.002,
                                        "reminder_limit": 1, "coordinator_id": member["id"]})
        for index, state in enumerate(("等群主", "阻塞", "已交付"), 1):
            self.api("PUT", "/automation", {"idle_minutes": 1})
            logical = "已完成两条回复指令，等待群主安排下一步。\n\n状态：" + state
            self.next_turn(member["terminal_id"], logical)
            self.api("POST", "/messages", {"text": "@协调 本轮检查"})
            replies = self.wait_replies(index)
            expected = "• 已完成两条回复指令，等待群主安排下一步。\n\n  状态：" + state if provider == "codex" else logical
            self.assertEqual(replies[-1]["text"], expected)
            if provider == "codex":
                self.assertEqual(self.terminal(member["terminal_id"])["out"], expected)
                self.assertIn(expected, self.terminal(member["terminal_id"])["full_out"])
            self.assertEqual(self.api("GET")["members"][0]["work_state"], state)
            self.assertFalse(self.api("GET", "/automation")["runtime"]["awaiting_owner"])
            self.api("PUT", "/automation", {"idle_minutes": 0.002})
            reads = self.terminal(member["terminal_id"])["status_reads"]
            self.until("blocked work state persists across idle interval", lambda:
                       self.terminal(member["terminal_id"])["status_reads"] >= reads + 12)
            self.assertEqual(self.api("GET", "/automation")["runtime"]["reminders_sent"], 0)
            self.assertEqual(len(self.terminal(member["terminal_id"])["inputs"]), index)

    def test_codex_real_rendered_work_states_suppress_idle_reminders(self):
        self.check_rendered_work_states_stop_idle_reminders("codex")

    def test_claude_last_work_states_still_suppress_idle_reminders(self):
        self.check_rendered_work_states_stop_idle_reminders("claude_code")

    def check_automation_waits_for_owner(self, relay_limit):
        self.create_group()
        member = self.invite("执行", "claude_code", "developer")
        peer = self.invite("复核", "claude_code", "reviewer")
        self.until("silent notices completed before owner-wait case", lambda:
                   all(not m.get("pending") and not m.get("queued_count") for m in self.api("GET")["members"]))
        peer_inputs = len(self.terminal(peer["terminal_id"])["inputs"])
        reply = "@复核 请审查" if relay_limit == 0 else "@群主 需要你决定范围"
        self.next_turn(member["terminal_id"], reply)
        self.api("PUT", "/automation", {"idle_minutes": 1, "reminder_cooldown_minutes": 0.002,
                                        "reminder_limit": 1, "relay_limit": relay_limit})
        self.api("POST", "/messages", {"text": "@执行 本轮任务"})
        self.wait_replies(1)
        self.assertTrue(self.api("GET", "/automation")["runtime"].get("awaiting_owner", False))
        self.api("PUT", "/automation", {"idle_minutes": 0.002})
        self.assertEqual(len(self.terminal(peer["terminal_id"])["inputs"]), peer_inputs)
        self.stopped_server = self.server
        self.stop_process(self.stopped_server)
        self.start_server()
        reads = self.terminal(member["terminal_id"])["status_reads"]
        self.until("owner-wait survives restart and many idle observations", lambda:
                   self.terminal(member["terminal_id"])["status_reads"] >= reads + 20)
        runtime = self.api("GET", "/automation")["runtime"]
        self.assertTrue(runtime["awaiting_owner"])
        self.assertEqual((runtime["reminders_sent"], runtime["deliveries_used"]), (0, 1))
        self.api("PUT", "/automation", {"relay_limit": 10, "idle_minutes": 1})
        self.assertTrue(self.api("GET", "/automation")["runtime"]["awaiting_owner"])
        self.next_turn(member["terminal_id"], "继续推进\n状态：进行中")
        self.api("POST", "/messages", {"text": "@执行 群主决定继续"})
        self.wait_replies(2)
        self.assertFalse(self.api("GET", "/automation")["runtime"]["awaiting_owner"])
        self.api("PUT", "/automation", {"idle_minutes": 0.002})
        self.wait_replies(3)
        self.assertEqual(self.api("GET", "/automation")["runtime"]["reminders_sent"], 1)

    def test_automation_owner_mention_pauses_reminders_until_owner_resumes(self):
        self.check_automation_waits_for_owner(relay_limit=6)

    def test_automation_relay_limit_pauses_reminders_until_owner_resumes(self):
        self.check_automation_waits_for_owner(relay_limit=0)

    def test_automation_budget_queue_restore_and_increase_preserve_fifo(self):
        self.create_group()
        member = self.invite("预算成员", "claude_code", "developer")
        self.assertEqual(self.api("GET", "/automation")["settings"]["relay_limit"], 6)
        self.api("PUT", "/automation", {"delivery_budget": 1, "relay_limit": 10})
        for text in ("第一条", "第二条", "第三条"):
            self.api("POST", "/messages", {"text": text})
        self.until("one input and two budget-paused messages", lambda:
                   len(self.terminal(member["terminal_id"])["inputs"]) == 1 and
                   self.api("GET")["members"][0]["queued_count"] == 2 and len(self.replies()) == 1)
        self.assertEqual(sum("投递预算" in msg["text"] for msg in self.api("GET")["messages"]), 1)
        self.stopped_server = self.server
        self.stop_process(self.stopped_server)
        self.start_server()
        self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], 1)
        self.assertEqual(self.api("GET")["members"][0]["queued_count"], 2)
        self.api("PUT", "/automation", {"delivery_budget": 3})
        self.wait_replies(3)
        inputs = self.terminal(member["terminal_id"])["inputs"]
        self.assertEqual([turn["message"] for turn in inputs], ["[群主] 第一条", "[群主] 第二条", "[群主] 第三条"])
        self.assertEqual(self.api("GET", "/automation")["runtime"]["deliveries_used"], 3)

    def test_automation_broadcast_counts_recipients_and_rejects_invalid_configuration(self):
        self.create_group()
        members = [self.invite(name, "claude_code", "developer") for name in ("成员甲", "成员乙", "成员丙")]
        self.until("silent notices completed", lambda: all(not m.get("pending") and not m.get("queued_count")
                                                          for m in self.api("GET")["members"]))
        before = {m["terminal_id"]: len(self.terminal(m["terminal_id"])["inputs"]) for m in members}
        defaults = self.api("GET", "/automation")
        for invalid in ({"relay_limit": True}, {"delivery_budget": -1}, {"coordinator_id": "missing"}):
            with self.assertRaises(error.HTTPError) as caught:
                self.api("PUT", "/automation", invalid)
            with caught.exception as response:
                self.assertEqual(response.code, 400)
                self.assertTrue(json.load(response)["error"])
            self.assertEqual(self.api("GET", "/automation"), defaults)
        self.api("PUT", "/automation", {"delivery_budget": 1})
        self.api("POST", "/messages", {"text": "@所有人 预算广播"})
        self.until("broadcast paused at exactly one recipient", lambda:
                   sum(self.api("GET")["members"][i]["queued_count"] for i in range(3)) == 2)
        self.assertEqual(sum(len(self.terminal(tid)["inputs"]) - count for tid, count in before.items()), 1)
        self.api("PUT", "/automation", {"delivery_budget": 3})
        self.wait_replies(3)
        self.assertEqual(sum(len(self.terminal(tid)["inputs"]) - count for tid, count in before.items()), 3)
        other = self.http(self.server_url, "POST", "/api/groups", {
            "name": "默认群", "workdir": str(self.root / "other"), "members": []})
        self.assertIsNone(self.http(self.server_url, "GET", f"/api/groups/{other['id']}/automation")["settings"]["delivery_budget"])

    def test_automation_idle_reminder_is_identified_targeted_and_budgeted(self):
        self.create_group()
        member = self.invite("协调", "claude_code", "supervisor")
        self.next_turn(member["terminal_id"], "请群主决定\n状态：等群主")
        self.api("PUT", "/automation", {"idle_minutes": 0.004, "reminder_cooldown_minutes": 0.004,
                                        "reminder_limit": 2, "delivery_budget": 5, "coordinator_id": member["id"]})
        self.wait_replies(1)
        self.assertEqual(self.api("GET")["members"][0]["work_state"], "等群主")
        reminders = [msg for msg in self.api("GET")["messages"] if "[自动提醒 idle:" in msg["text"]]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["kind"], "system")
        self.assertEqual(reminders[0]["to"], ["协调"])
        self.assertIn("[自动提醒 idle:", self.terminal(member["terminal_id"])["inputs"][0]["message"])
        # Observe many actual poll cycles across the interval, without fixed sleeps.
        reads = self.terminal(member["terminal_id"])["status_reads"]
        self.until("waiting-owner excludes reminders across many polls", lambda:
                   self.terminal(member["terminal_id"])["status_reads"] >= reads + 20)
        runtime = self.api("GET", "/automation")["runtime"]
        self.assertEqual((runtime["reminders_sent"], runtime["deliveries_used"]), (1, 1))

    def test_invalid_permission_modes_do_not_disrupt_running_member(self):
        self.create_group()
        member = self.invite("权限测试", "claude_code", "developer",
                             permission_mode="acceptEdits", effort="low")
        before = self.fake_state()
        groups = self.http(self.server_url, "GET", "/api/groups")
        invalid = {"name": "权限测试", "provider": "claude_code",
                   "permission_mode": "bogus", "effort": "low"}
        requests = [
            ("POST", "/api/groups", {"name": "非法权限群", "workdir": str(self.root / "invalid"),
                                     "members": [{"name": "合法成员", "provider": "claude_code"}, invalid]}),
            ("POST", self.group_path + "/members", {**invalid, "name": "非法成员"}),
            ("PUT", self.group_path + f"/members/{member['id']}", invalid),
        ]
        for method, path, body in requests:
            with self.assertRaises(error.HTTPError) as caught:
                self.http(self.server_url, method, path, body)
            with caught.exception as response:
                self.assertEqual(response.code, 400)
                self.assertIn("权限模式", json.load(response)["error"])
            self.assertEqual(self.http(self.server_url, "GET", "/api/groups"), groups)
            self.assertEqual(self.api("GET")["members"], [member])
            state = self.fake_state()
            for key in ("profiles", "create_requests", "sessions"):
                self.assertEqual(state[key], before[key])
            self.assertEqual(set(state["terminals"]), {member["terminal_id"]})
        self.next_turn(member["terminal_id"], "非法配置已拒绝，原成员仍可回复")
        self.api("POST", "/messages", {"text": "@权限测试 继续回复"})
        self.assertEqual([m["text"] for m in self.wait_replies(1)], ["非法配置已拒绝，原成员仍可回复"])

    def test_legacy_permission_modes_default_on_load_and_member_restart(self):
        self.create_group()
        codex = self.invite("编码", "codex", "developer", permission_mode="bogus", effort="xhigh")
        self.assertEqual(codex["permission_mode"], "")
        claude = self.invite("权限测试", "claude_code", "reviewer", permission_mode="plan", effort="low")
        state = self.fake_state()
        self.assertNotIn("permissionMode:", state["profiles"][state["terminals"][codex["terminal_id"]]["profile"]])
        self.assertIn("permissionMode: plan\n", state["profiles"][state["terminals"][claude["terminal_id"]]["profile"]])
        self.until("silent notices finish", lambda: all(not m.get("pending") and not m.get("queued_count")
                                                       for m in self.api("GET")["members"]))
        self.stopped_server = self.server
        self.stop_process(self.stopped_server)
        saved = json.loads((self.data / "groups.json").read_text())
        saved_group = saved[self.group_path.rsplit("/", 1)[1]]
        for member in saved_group["members"]:
            member["permission_mode"] = "bogus" if member["provider"] == "claude_code" else "plan"
        (self.data / "groups.json").write_text(json.dumps(saved))
        self.start_server()
        restored = self.api("GET")["members"]
        self.assertEqual([(m["id"], m["permission_mode"], m["effort"]) for m in restored],
                         [(codex["id"], "", "xhigh"), (claude["id"], "", "low")])
        self.api("POST", f"/members/{claude['id']}/restart", {})
        def restarted():
            current = next(m for m in self.api("GET")["members"] if m["id"] == claude["id"])
            return current if current["terminal_id"] and current["terminal_id"] != claude["terminal_id"] and \
                current.get("ready_terminal") == current["terminal_id"] else None
        current = self.until("legacy member restarts with default permissions", restarted)
        state = self.fake_state()
        profile = state["profiles"][state["terminals"][current["terminal_id"]]["profile"]]
        self.assertNotIn("permissionMode:", profile)
        self.assertIn("claudeConfig:\n  effort: low\n", profile)

    def test_effort_survives_edit_member_restart_and_server_restore(self):
        self.create_group()
        member = self.invite("强度测试", "codex", "developer", effort="xhigh")
        self.assertEqual(member["effort"], "xhigh")

        def profile(current):
            state = self.http(self.cao_url, "GET", "/_test/state")
            terminal = state["terminals"][current["terminal_id"]]
            return state["profiles"][terminal["profile"]]

        def replacement(previous):
            def ready():
                current = self.api("GET")["members"][0]
                return current if current["terminal_id"] and current["terminal_id"] != previous else None
            return self.until("replacement terminal ready", ready)

        self.assertIn("codexConfig:\n  model_reasoning_effort: xhigh\n", profile(member))
        updated = self.api("PUT", f"/members/{member['id']}", {
            "name": "强度测试", "provider": "claude_code", "role": "reviewer", "effort": "max"})
        self.assertEqual(updated["effort"], "max")
        updated = replacement(member["terminal_id"])
        self.assertIn("claudeConfig:\n  effort: max\n", profile(updated))
        self.assertNotIn("codexConfig:", profile(updated))
        self.assertIn("推理强度=max", self.api("GET", "/log")["content"])
        saved = json.loads((self.data / "groups.json").read_text())
        self.assertEqual(saved[self.group_path.rsplit("/", 1)[1]]["members"][0]["effort"], "max")
        self.stopped_server = self.server
        self.stop_process(self.stopped_server)
        self.start_server()
        restored = self.api("GET")["members"][0]
        self.assertEqual((restored["id"], restored["effort"]), (member["id"], "max"))
        self.api("POST", f"/members/{member['id']}/restart")
        restarted = replacement(restored["terminal_id"])
        self.assertEqual(restarted["effort"], "max")
        self.assertIn("claudeConfig:\n  effort: max\n", profile(restarted))
        self.api("PUT", f"/members/{member['id']}", {
            "name": "强度测试", "provider": "claude_code", "role": "reviewer", "effort": ""})
        defaulted = replacement(restarted["terminal_id"])
        self.assertEqual(defaulted["effort"], "")
        self.assertNotIn("claudeConfig:", profile(defaulted))
        self.assertIn("推理强度=CLI 默认", self.api("GET", "/log")["content"])

    def test_stale_completed_waits_for_current_turn_output(self):
        self.create_group()
        dev = self.invite("开发", "codex", "developer")
        tid = dev["terminal_id"]
        old_reply, new_reply = "上一轮的唯一内容", "本轮的新内容，不能复用上一轮"
        self.next_turn(tid, old_reply)
        self.api("POST", "/messages", {"text": "@开发 第一轮"})
        self.assertEqual([m["text"] for m in self.wait_replies(1)], ["• " + old_reply])
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
                         ("completed", boundary, "• " + old_reply))
        member = self.api("GET")["members"][0]
        self.assertEqual(member["boundary"], boundary)
        self.assertTrue(member["pending"])
        self.assertEqual(stale["last_reads"], previous["last_reads"])
        self.assertEqual([m["text"] for m in self.replies()], ["• " + old_reply])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/release")
        self.assertEqual([m["text"] for m in self.wait_replies(2)], ["• " + old_reply, "• " + new_reply])
        current = self.terminal(tid)
        self.assertGreater(current["status_generation"], boundary)
        self.assertEqual(current["last_reads"], previous["last_reads"] + 1)
        self.assertFalse(self.api("GET")["members"][0]["pending"])


if __name__ == "__main__":
    unittest.main()
