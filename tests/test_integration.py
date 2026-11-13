"""Real HTTP/subprocess acceptance tests using only disposable local resources."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
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
                          resumed_replies[2]["text"]), ("开发", dev["id"], resumed_reply))
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
                         [("开发", final), ("评审", reviewed)])
        self.assertFalse(any(interim in m["text"] for m in self.api("GET")["messages"]))
        self.assertEqual([turn["message"] for turn in self.terminal(review_tid)["inputs"]],
                         ["[来自 开发] " + final])
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
        self.assertEqual([m["text"] for m in self.wait_replies(1)],[final])
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
        self.assertEqual({m["text"] for m in replies},{"开发启动后完成","评审启动后完成"})
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
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["启动恢复后完成"])
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
        self.assertEqual([m["text"] for m in self.wait_replies(1)],["迟到的本轮回复"])
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
        self.assertEqual({m["text"] for m in self.wait_replies(2)}, {"开发重发后完成","评审重发后完成"})
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
            self.next_turn(tid,"收到，我是"+m["name"],complete_on_input=True)
            self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready")
        self.assert_fast_replies_remain_complete(["收到，我是cx","收到，我是cc"])
        for m in members:
            state=self.terminal(m["terminal_id"])
            self.assertEqual([turn["message"] for turn in state["inputs"]],["[群主] "+text])
            self.assertTrue(state["inputs"][0]["complete_on_input"])
            self.assertGreater(state["output_generation"],state["inputs"][0]["dispatch_boundary"])

    def test_fast_codex_done_survives_eviction_before_the_first_poll(self):
        self.http(self.cao_url,"POST","/_test/config",{"startup_hold":True})
        self.create_group()
        self.api("POST","/members",{"name":"cx","provider":"codex","effort":"low"})
        self.api("POST","/messages",{"text":"@所有人 只回复：收到，我是cx"})
        tid=self.until("cold codex terminal allocated",lambda:self.api("GET")["members"][0]["terminal_id"])
        self.next_turn(tid,"收到，我是cx",complete_on_input=True,padding_bytes=40000)
        previous="\x1b]7501;state=working\x07旧回复\x1b]7501;state=done\x07\x1b]7501;state=done\x07"
        self.http(self.cao_url,"POST",f"/_test/terminals/{tid}/ready",{"prompt":previous+"\ncodex>\n? for shortcuts"})
        self.assert_fast_replies_remain_complete(["收到，我是cx"])
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
        self.assertEqual(self.wait_replies(1)[0]["text"], warmup)
        for count in (2, 3):
            previous = self.terminal(tid)
            self.assertIn("state=done", previous["full_out"])
            self.next_turn(tid, "收到", complete_on_input=True)
            self.api("POST", "/messages", {"text": f"@开发 第{count}轮短回复"})
            replies = self.wait_replies(count)
            self.assertEqual([m["text"] for m in replies], [warmup] + ["收到"] * (count - 1))
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
                       raw_prefix="\x1b]7501;state=done\x07")
        self.api("POST", "/messages", {"text": "@开发 新任务"})
        self.until("old done polled twice after buffer clear", lambda:
                   len(self.terminal(tid)["inputs"]) == 2 and
                   self.terminal(tid)["inputs"][-1]["phase_reads"][0] >= 2)
        self.assertEqual(self.replies(), [])
        self.assertTrue(self.api("GET")["members"][0]["pending"])
        self.assertNotIn("state=working", self.terminal(tid)["full_out"])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/advance")
        self.assertEqual([m["text"] for m in self.wait_replies(1)], [final])

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
        self.assertEqual(stale["last_reads"], previous["last_reads"])
        self.assertEqual([m["text"] for m in self.replies()], [old_reply])
        self.http(self.cao_url, "POST", f"/_test/terminals/{tid}/release")
        self.assertEqual([m["text"] for m in self.wait_replies(2)], [old_reply, new_reply])
        current = self.terminal(tid)
        self.assertGreater(current["status_generation"], boundary)
        self.assertEqual(current["last_reads"], previous["last_reads"] + 1)
        self.assertFalse(self.api("GET")["members"][0]["pending"])


if __name__ == "__main__":
    unittest.main()
