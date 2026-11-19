"""Isolated standard-library unit tests; never use a running CAO instance."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


class ImportSafetyTests(unittest.TestCase):
    def assert_import_safe(self, configured):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = home / ("custom-data" if configured else ".cao-groupchat")
            env = {
                "HOME": str(home),
                "PATH": os.defpath,
                "CAO_URL": "http://127.0.0.1:1",
                "CAO_TOKEN": "",
                "GROUPCHAT_PORT": "8765",
            }
            if configured:
                env["GROUPCHAT_DATA"] = str(expected)
            script = r'''
from contextlib import ExitStack
import http.server
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading
from unittest.mock import patch
import urllib.request

source, expected = map(Path, sys.argv[1:])
protected = {expected, Path.home() / ".cao-groupchat"}

def audit(event, args):
    if event in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove",
                 "os.rmdir", "os.rename"}:
        for value in args:
            if isinstance(value, (str, bytes)):
                path = Path(value.decode() if isinstance(value, bytes) else value)
                if any(path == p or p in path.parents for p in protected):
                    raise AssertionError("import accessed runtime data")

sys.addaudithook(audit)
with ExitStack() as stack:
    # A read attempt or existence probe also violates the import contract.
    for method in ("open", "read_text", "read_bytes", "write_text", "write_bytes",
                   "exists", "mkdir", "replace"):
        stack.enter_context(patch.object(Path, method,
                            side_effect=AssertionError("import performed file I/O")))
    for target in ("http.server.ThreadingHTTPServer", "threading.Thread.start",
                   "urllib.request.urlopen", "socket.socket.connect",
                   "socket.socket.bind", "subprocess.Popen"):
        stack.enter_context(patch(target,
                            side_effect=AssertionError("import started external work")))
    spec = importlib.util.spec_from_file_location("isolated_groupchat", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DATA_DIR == expected
    assert module.STATE_FILE == expected / "groups.json"
    assert module.groups == {}
    assert callable(module.main)
'''
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(SERVER_PATH), str(expected)],
                cwd=home, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(list(home.iterdir()), [])

    def test_default_data_directory_is_not_accessed_on_import(self):
        self.assert_import_safe(configured=False)

    def test_configured_data_directory_is_not_accessed_on_import(self):
        self.assert_import_safe(configured=True)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        env = patch.dict(os.environ, {
            "HOME": str(root), "GROUPCHAT_DATA": str(root / "data"),
            "CAO_URL": "http://127.0.0.1:1", "CAO_TOKEN": "",
            "GROUPCHAT_PORT": "8765",
        })
        env.start()
        self.addCleanup(env.stop)
        for target in ("urllib.request.urlopen", "socket.socket.connect",
                       "socket.socket.bind", "subprocess.Popen", "threading.Thread.start"):
            blocker = patch(target, side_effect=AssertionError("unexpected external work"))
            blocker.start()
            self.addCleanup(blocker.stop)
        spec = importlib.util.spec_from_file_location("unit_groupchat", SERVER_PATH)
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        blocker = patch.object(self.server, "cao", side_effect=AssertionError("unexpected CAO call"))
        self.cao = blocker.start()
        self.addCleanup(blocker.stop)
        self.developer = {"id": "dev", "name": "开发", "terminal_id": "dev-terminal", "ready_terminal": "dev-terminal", "hops": 0}
        self.reviewer = {"id": "review", "name": "评审", "terminal_id": "review-terminal", "ready_terminal": "review-terminal"}
        self.group = {
            "id": "test-group", "name": "测试群", "workdir": str(root / "work"),
            "members": [self.developer, self.reviewer], "messages": [],
        }
        self.server.groups[self.group["id"]] = self.group

    def log_text(self):
        return (Path(self.group["workdir"]) / "GROUP_LOG.md").read_text(encoding="utf-8")


class EffortTests(ServerTestCase):
    def member(self, provider="codex", **extra):
        member = self.server.clean_member({"name": "强度测试", "provider": provider, **extra}, [])
        self.group["members"] = [member]
        return member

    def test_supported_efforts_round_trip_into_provider_profile(self):
        for provider, values, config, key in (
            ("codex", ("low", "medium", "high", "xhigh"), "codexConfig", "model_reasoning_effort"),
            ("claude_code", ("low", "medium", "high", "xhigh", "max"), "claudeConfig", "effort"),
        ):
            for value in values:
                with self.subTest(provider=provider, effort=value):
                    member = self.member(provider, effort=value)
                    self.assertEqual(member["effort"], value)
                    frontmatter = self.server.build_profile(self.group, member).split("---")[1]
                    self.assertIn(f"{config}:\n  {key}: {value}\n", frontmatter)
                    other = "claudeConfig" if provider == "codex" else "codexConfig"
                    self.assertNotIn(other, frontmatter)

    def test_omitted_empty_and_legacy_effort_keep_cli_default(self):
        for provider in ("codex", "claude_code", "kiro_cli"):
            for extra in ({}, {"effort": ""}, {"effort": None}, {"effort": "   "}):
                with self.subTest(provider=provider, extra=extra):
                    member = self.member(provider, **extra)
                    self.assertEqual(member["effort"], "")
                    for legacy in (False, True):
                        if legacy:
                            member.pop("effort")
                        profile = self.server.build_profile(self.group, member)
                        self.assertNotIn("codexConfig:", profile)
                        self.assertNotIn("claudeConfig:", profile)

    def test_invalid_or_unsupported_effort_is_rejected(self):
        for provider, value in (("codex", "max"), ("claude_code", "ultra"),
                                ("kiro_cli", "high"), ("codex", "HIGH"),
                                ("codex", "high\nmodel: injected"), ("codex", []),
                                ("claude_code", {}), ("codex", 1)):
            with self.subTest(provider=provider, value=value):
                with self.assertRaises(ValueError):
                    self.member(provider, effort=value)

    def test_effort_is_trimmed_persisted_and_exposed(self):
        member = self.member(effort=" xhigh ")
        self.group["members"] = [member]
        self.server.save_state()
        self.server.groups.clear()
        self.server.load_state()
        restored = self.server.groups[self.group["id"]]
        self.assertEqual(restored["members"][0]["effort"], "xhigh")
        self.assertEqual(self.server.public_group(restored)["members"][0]["effort"], "xhigh")


class MentionTests(ServerTestCase):
    def test_matches_multiple_members_once_and_preserves_roster_order(self):
        found = self.server.find_mentions(self.group, "@评审 再请 @开发，@评审")
        self.assertEqual([m["id"] for m in found], ["dev", "review"])

    def test_prefix_names_do_not_match_the_longer_name(self):
        for short, long in [("ann", "anna"), ("开发", "开发者"), ("dev", "dev-team")]:
            with self.subTest(short=short, long=long):
                group = {"members": [{"id": "short", "name": short}, {"id": "long", "name": long}]}
                self.assertEqual(self.server.find_mentions(group, f"@{long} 请处理"), [group["members"][1]])
                self.assertEqual(self.server.find_mentions(group, f"@{short} 请处理"), [group["members"][0]])
                self.assertEqual(self.server.find_mentions(group, f"@{long}后缀"), [])

    def test_chinese_or_word_suffix_does_not_match(self):
        for text in ("@开发请处理", "@开发者", "@开发x", "@开发_", "@开发2", "没有提及"):
            with self.subTest(text=text):
                self.assertEqual(self.server.find_mentions(self.group, text), [])
        self.assertEqual(self.server.find_mentions(self.group, "@开发，请处理"), [self.developer])

    def test_excludes_sender(self):
        self.assertEqual(self.server.find_mentions(self.group, "@开发 @评审", exclude="dev"), [self.reviewer])

    def test_names_are_literal_and_empty_roster_is_supported(self):
        member = {"id": "literal", "name": "dev.v2+"}
        group = {"members": [member]}
        self.assertEqual(self.server.find_mentions(group, "@dev.v2+ 请处理"), [member])
        self.assertEqual(self.server.find_mentions(group, "@devXv2+ 请处理"), [])
        self.assertEqual(self.server.find_mentions({"members": []}, "@开发"), [])


class ReplyTests(ServerTestCase):
    def test_final_allowed_relay_uses_hop_six(self):
        self.assertEqual(self.server.MAX_RELAY_HOPS, 6)
        self.developer["hops"] = self.server.MAX_RELAY_HOPS - 1
        with patch.object(self.server, "send_to") as send:
            self.server.handle_reply(self.group, self.developer, "@评审 请看")
        send.assert_called_once_with(self.group, self.reviewer, "[来自 开发] @评审 请看", 6)
        self.assertEqual([m["kind"] for m in self.group["messages"]], ["agent"])

    def test_at_or_above_limit_stops_relay_and_shows_notice(self):
        for hops in (self.server.MAX_RELAY_HOPS, self.server.MAX_RELAY_HOPS + 1):
            with self.subTest(hops=hops):
                member = dict(self.developer, hops=hops)
                self.group["messages"].clear()
                with patch.object(self.server, "send_to") as send:
                    self.server.handle_reply(self.group, member, "@评审 请看")
                send.assert_not_called()
                self.assertEqual([m["kind"] for m in self.group["messages"]], ["agent", "system"])
                self.assertIn("自动转发上限（6 次）", self.group["messages"][-1]["text"])

    def test_reply_does_not_relay_to_sender(self):
        with patch.object(self.server, "send_to") as send:
            self.server.handle_reply(self.group, self.developer, "@开发 已完成")
        send.assert_not_called()

    def test_silent_reply_has_no_messages_logs_or_relays_even_when_reprocessed(self):
        self.developer["silent"] = True
        with patch.object(self.server, "send_to") as send:
            for _ in range(2):
                self.server.handle_reply(self.group, self.developer, "@评审\n[日志] 不应出现")
        self.assertEqual(self.group["messages"], [])
        self.assertFalse((Path(self.group["workdir"]) / "GROUP_LOG.md").exists())
        send.assert_not_called()

    def test_log_marker_is_only_recognized_at_start_of_same_line(self):
        text = ("完成\n正文 [日志] 不应写入\n[日志] 动作一\n  [日志] 动作二\n"
                "[日志]\n后续普通正文\n[日志]   \n下一行普通正文\n[日志]动作三\n[日志] 动作四\r\n")
        self.server.handle_reply(self.group, self.developer, text)
        lines = self.log_text().splitlines()
        actions = [line.split("] [开发] ", 1)[1] for line in lines if "] [开发] " in line]
        self.assertEqual(actions, ["发言：完成", "动作一", "动作二", "动作三", "动作四"])

    def test_reprocessing_same_reply_does_not_duplicate_message_log_or_relay(self):
        text = "@评审 请看\n[日志] 已写单测"
        with patch.object(self.server, "send_to") as send:
            self.server.handle_reply(self.group, self.developer, text)
            first_log = self.log_text()
            self.server.handle_reply(self.group, self.developer, text)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertEqual(self.log_text(), first_log)
        self.assertEqual(first_log.count("] [开发] 已写单测\n"), 1)
        send.assert_called_once()

    def test_identical_reply_is_allowed_after_next_dispatch(self):
        text = "完成\n[日志] 本轮完成"
        self.cao.side_effect = None
        self.cao.return_value = {"dispatch_boundary": 1}
        self.server.send_to(self.group, self.developer, "第一轮", 0)
        self.server.handle_reply(self.group, self.developer, text)
        self.cao.return_value = {"dispatch_boundary": 2}
        self.server.send_to(self.group, self.developer, "第二轮", 0)
        self.server.handle_reply(self.group, self.developer, text)
        self.assertEqual(len(self.group["messages"]), 2)
        self.assertEqual(self.log_text().count("] [开发] 本轮完成\n"), 2)
        self.assertEqual(self.cao.call_count, 2)

    def test_empty_reply_keeps_placeholder(self):
        self.server.handle_reply(self.group, self.developer, "")
        self.assertEqual(self.group["messages"][0]["text"], "（无文字输出）")


class DeliveryTests(ServerTestCase):
    CODEX_READY = "codex>\n? for shortcuts"
    CLAUDE_READY = "────────────\n❯ \n────────────\n? for shortcuts"

    def setUp(self):
        super().setUp()
        self.developer.update(provider="codex", terminal_id=None, ready_terminal=None,
                              outbox=[], status="starting")
        self.group["members"] = [self.developer]

    def active(self, provider="codex", started=False):
        self.developer.update(provider=provider, terminal_id="dev-terminal", ready_terminal="dev-terminal",
            active_delivery={"id": "job", "attempt": 1, "queued_at": 900,
                             "text": "[群主] 原始消息", "hops": 2, "silent": False},
            pending=True, delivery_state="sent", delivery_warned=False,
            delivery_started=started, last_progress_at=1000, progress_generation=2,
            sent_at=1000, boundary=1, codex_saw_working=started)

    def test_messages_without_a_terminal_are_queued_persisted_and_publicly_summarized(self):
        with patch.object(self.server.time, "time", return_value=1000):
            self.server.send_to(self.group, self.developer, "[群主] 启动即发", 0)
        self.cao.assert_not_called()
        self.assertEqual(len(self.developer["outbox"]), 1)
        self.assertEqual(self.developer["outbox"][0]["text"], "[群主] 启动即发")
        self.server.save_state()
        self.server.groups.clear()
        self.server.load_state()
        restored=self.server.groups[self.group["id"]]
        public=self.server.public_group(restored)["members"][0]
        self.assertEqual(public["queued_count"], 1)
        self.assertNotIn("outbox", public)
        self.assertNotIn("active_delivery", public)

    def test_both_providers_need_a_positive_prompt_and_stable_observations(self):
        self.cao.side_effect = None
        for provider, prompt in (("codex", self.CODEX_READY), ("claude_code", self.CLAUDE_READY)):
            with self.subTest(provider=provider):
                self.developer.update(provider=provider, terminal_id="dev-terminal", ready_terminal=None)
                self.cao.return_value={"output":"CLI loading"}
                with patch.object(self.server.time, "time", return_value=1000):
                    self.assertFalse(self.server.member_ready(self.developer, {"status":"idle"}))
                self.cao.return_value={"output":prompt}
                with patch.object(self.server.time, "time", return_value=1001):
                    self.assertFalse(self.server.member_ready(self.developer, {"status":"idle"}))
                with patch.object(self.server.time, "time", return_value=1001.3):
                    self.assertFalse(self.server.member_ready(self.developer, {"status":"idle"}))
                with patch.object(self.server.time, "time", return_value=1001.8):
                    self.assertTrue(self.server.member_ready(self.developer, {"status":"idle"}))

    def test_changing_prompt_or_a_new_terminal_restarts_the_stability_check(self):
        self.cao.side_effect = None
        self.developer.update(terminal_id="dev-terminal")
        for now, raw, expected in ((1000,self.CODEX_READY,False),
                                   (1001,"new banner\n"+self.CODEX_READY,False),
                                   (1001.8,"new banner\n"+self.CODEX_READY,True)):
            self.cao.return_value={"output":raw}
            with patch.object(self.server.time,"time",return_value=now):
                self.assertEqual(self.server.member_ready(self.developer,{"status":"idle"}),expected)
        self.developer["terminal_id"]="replacement"
        self.cao.return_value={"output":"loading"}
        with patch.object(self.server.time,"time",return_value=1002):
            self.assertFalse(self.server.member_ready(self.developer,{"status":"idle"}))

    def test_a_queued_job_is_dispatched_only_once_after_readiness(self):
        with patch.object(self.server.time,"time",return_value=1000):
            self.server.send_to(self.group,self.developer,"原文",3,True)
        self.developer["terminal_id"]="dev-terminal"
        def response(method,path,query=None,**kwargs):
            return {"dispatch_boundary":5} if method=="POST" else {"output":self.CODEX_READY}
        self.cao.side_effect=response
        with patch.object(self.server.time,"time",return_value=1000):
            self.server.flush_outbox(self.group,self.developer,{"status":"idle"})
        self.assertFalse(any(c.args[0]=="POST" for c in self.cao.call_args_list))
        with patch.object(self.server.time,"time",return_value=1001):
            self.server.flush_outbox(self.group,self.developer,{"status":"idle"})
            self.server.flush_outbox(self.group,self.developer,{"status":"idle"})
        posts=[c for c in self.cao.call_args_list if c.args[0]=="POST"]
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0].args[2],{"message":"原文"})
        self.assertEqual((self.developer["hops"],self.developer["silent"]),(3,True))

    def test_startup_timeout_reports_undelivered_once_instead_of_discarding(self):
        with patch.object(self.server.time,"time",return_value=1000):
            self.server.send_to(self.group,self.developer,"尚未投递",0)
        for now in (1181,1190):
            with patch.object(self.server.time,"time",return_value=now):
                self.server.flush_outbox(self.group,self.developer)
        self.assertEqual(self.developer["delivery_state"],"not_ready")
        self.assertEqual(len(self.group["messages"]),1)
        self.assertIn("尚未投递",self.group["messages"][0]["text"])
        self.assertEqual(len(self.developer["outbox"]),1)

    def test_idle_redraws_without_start_evidence_do_not_postpone_warning(self):
        for provider in ("codex","claude_code"):
            with self.subTest(provider=provider):
                self.group["messages"].clear()
                self.active(provider)
                for now,generation in ((1121,99),(1130,100)):
                    with patch.object(self.server.time,"time",return_value=now):
                        self.server.watch_delivery(self.group,self.developer,
                            {"status":"idle","status_generation":1,"output_generation":generation})
                self.assertEqual(self.developer["delivery_state"],"uncertain")
                self.assertEqual(len(self.group["messages"]),1)
                self.assertIn("可能未送达",self.group["messages"][0]["text"])
                self.assertTrue(self.server.public_group(self.group)["members"][0]["can_retry"])

    def test_started_work_only_refreshes_timeout_on_new_output(self):
        self.active(started=True)
        for now,generation in ((1119,3),(1200,3)):
            with patch.object(self.server.time,"time",return_value=now):
                self.server.watch_delivery(self.group,self.developer,
                    {"status":"idle","status_generation":1,"output_generation":generation})
        self.assertEqual(self.group["messages"],[])
        with patch.object(self.server.time,"time",return_value=1240):
            self.server.watch_delivery(self.group,self.developer,
                {"status":"idle","status_generation":1,"output_generation":3})
        self.assertEqual(self.developer["delivery_state"],"uncertain")

    def test_retry_preserves_payload_and_rejects_the_same_attempt_twice(self):
        self.active()
        self.developer.update(delivery_state="uncertain",delivery_warned=True)
        self.cao.side_effect=lambda method,path,*args,**kwargs: (
            {"dispatch_boundary":5} if method=="POST" else
            {"output":self.CODEX_READY} if path.endswith("/output") else {"status":"idle"})
        token={"delivery_id":"job","delivery_attempt":1}
        with patch.object(self.server.time,"time",return_value=1000):
            self.server.retry_delivery(self.group,self.developer,token)
        self.assertIsNone(self.developer["active_delivery"])
        with self.assertRaises(ValueError):
            self.server.retry_delivery(self.group,self.developer,token)
        with patch.object(self.server.time,"time",return_value=1001):
            self.server.flush_outbox(self.group,self.developer)
        self.assertEqual(self.developer["active_delivery"]["attempt"],2)
        self.assertEqual(self.developer["hops"],2)
        with self.assertRaises(ValueError):
            self.server.retry_delivery(self.group,self.developer,token)
        posts=[c for c in self.cao.call_args_list if c.args[0]=="POST"]
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0].args[2],{"message":"[群主] 原始消息"})

    def test_ambiguous_input_failure_cannot_reuse_previous_codex_reply(self):
        self.developer.update(terminal_id="dev-terminal",ready_terminal="dev-terminal",
                              reply_handled=False,codex_dispatch_markers=0,codex_dispatch_state=None,
                              codex_saw_working=True,full_len=0)
        raw="\x1b]7501;state=working:app=codex\x1b\\旧回复\x1b]7501;state=done:app=codex\x1b\\"
        def response(method,path,*args,**kwargs):
            if method=="POST":
                raise self.server.CaoError(0,"input response timed out")
            return {"output":raw} if path.endswith("/output") else {
                "status":"idle","status_generation":10,"output_generation":10}
        self.cao.side_effect=response
        self.server.send_to(self.group,self.developer,"新消息",0)
        self.server.poll_member(self.group,self.developer)
        self.assertTrue(self.developer["pending"])
        self.assertFalse(any(m["kind"]=="agent" for m in self.group["messages"]))
        self.assertTrue(self.server.public_group(self.group)["members"][0]["can_retry"])

    def test_ambiguous_input_failure_cannot_reuse_previous_claude_reply(self):
        self.active(provider="claude_code")
        self.developer.update(active_delivery=None,pending=False)
        def response(method,path,*args,**kwargs):
            if method=="POST":
                raise self.server.CaoError(0,"input response timed out")
            return {"output":"旧回复\n────────────\n❯ \n────────────"} if path.endswith("/output") else {
                "status":"completed","status_generation":10,"output_generation":10}
        self.cao.side_effect=response
        self.server.send_to(self.group,self.developer,"新消息",0)
        self.server.poll_member(self.group,self.developer)
        self.assertTrue(self.developer["pending"])
        self.assertFalse(any(m["kind"]=="agent" for m in self.group["messages"]))

    def test_watchdog_still_expires_when_terminal_or_output_queries_fail(self):
        for failing in ("terminal","output"):
            with self.subTest(failing=failing):
                self.active()
                self.developer["codex_dispatch_markers"]=0
                self.group["messages"].clear()
                def response(method,path,*args,**kwargs):
                    if failing=="terminal" or path.endswith("/output"):
                        raise self.server.CaoError(503,"temporary probe failure")
                    return {"status":"idle","status_generation":1,"output_generation":1}
                self.cao.side_effect=response
                with patch.object(self.server.time,"time",return_value=1121):
                    self.server.poll_member(self.group,self.developer)
                    self.server.poll_member(self.group,self.developer)
                self.assertEqual(self.developer["delivery_state"],"uncertain")
                self.assertEqual(sum("可能未送达" in m["text"] for m in self.group["messages"]),1)

    def test_dispatch_is_persisted_before_any_input_can_be_accepted(self):
        self.developer.update(terminal_id="dev-terminal",ready_terminal="dev-terminal")
        def response(method,path,*args,**kwargs):
            if method=="POST":
                saved=json.loads(self.server.STATE_FILE.read_text())[self.group["id"]]["members"][0]
                self.assertEqual(saved["delivery_state"],"dispatching")
                self.assertEqual(saved["outbox"],[])
                self.assertEqual(saved["active_delivery"]["text"],"原始消息")
                return {"dispatch_boundary":5}
            return {"output":self.CODEX_READY}
        self.cao.side_effect=response
        self.server.send_to(self.group,self.developer,"原始消息",0)
        self.assertEqual(self.developer["delivery_state"],"sent")

    def test_restart_turns_an_interrupted_dispatch_into_a_manual_recovery(self):
        self.active()
        self.developer.update(delivery_state="dispatching",dispatch_confirmed=False)
        self.server.save_state()
        self.server.load_state()
        restored=self.server.groups[self.group["id"]]
        member=restored["members"][0]
        self.assertEqual(member["delivery_state"],"uncertain")
        self.assertTrue(member["pending"])
        self.assertTrue(self.server.public_group(restored)["members"][0]["can_retry"])
        self.cao.assert_not_called()

    def test_claude_placeholder_box_is_ready_in_dim_output_and_empty_box(self):
        self.cao.side_effect=None
        for prompt in ('❯ \x1b[2mTry "fix typecheck errors"\x1b[22m',
                       '❯'):
            with self.subTest(prompt=prompt):
                self.developer.update(provider="claude_code",terminal_id="dev-terminal",ready_terminal=None)
                self.developer.pop("ready_candidate",None)
                self.cao.return_value={"output":"────────────\n"+prompt+"\n────────────"}
                for now,expected in ((1000,False),(1000.3,False),(1000.8,True)):
                    with patch.object(self.server.time,"time",return_value=now):
                        self.assertEqual(self.server.member_ready(self.developer,{"status":"idle"}),expected)
        for raw in ('────────────\n❯ Try "fix typecheck errors"\n────────────',
                    '────────────\n❯ fix typecheck errors\n────────────',
                    '────────────\n❯ Try "fix typecheck errors"\nnot a bottom rail'):
            self.developer.update(ready_terminal=None)
            self.developer.pop("ready_candidate",None)
            self.cao.return_value={"output":raw}
            for now in (2000,2001):
                with patch.object(self.server.time,"time",return_value=now):
                    self.assertFalse(self.server.member_ready(self.developer,{"status":"idle"}))

    def test_not_ready_manual_retry_uses_stable_idle_without_matching_features(self):
        for provider in ("claude_code","codex"):
            with self.subTest(provider=provider):
                self.developer.update(provider=provider,terminal_id="dev-terminal",ready_terminal=None,
                                      active_delivery=None,pending=False,outbox=[],delivery_state="waiting_ready")
                self.developer.pop("ready_candidate",None)
                self.cao.reset_mock()
                self.cao.side_effect=lambda method,path,*args,**kwargs: (
                    {"dispatch_boundary":5} if method=="POST" else
                    {"output":"unrecognized localized input"} if path.endswith("/output") else {"status":"idle"})
                with patch.object(self.server.time,"time",return_value=1000):
                    self.server.send_to(self.group,self.developer,"原文兜底",2,True)
                with patch.object(self.server.time,"time",return_value=1181):
                    self.server.flush_outbox(self.group,self.developer)
                self.assertEqual(self.developer["delivery_state"],"not_ready")
                job=self.developer["outbox"][0]
                token={"delivery_id":job["id"],"delivery_attempt":job["attempt"]}
                with patch.object(self.server.time,"time",return_value=1182):
                    self.server.retry_delivery(self.group,self.developer,token)
                for now,status in ((1182.4,"processing"),(1183,"idle"),(1183.4,"idle")):
                    with patch.object(self.server.time,"time",return_value=now):
                        self.server.flush_outbox(self.group,self.developer,{"status":status})
                self.assertFalse(any(c.args[0]=="POST" for c in self.cao.call_args_list))
                with patch.object(self.server.time,"time",return_value=1184):
                    self.server.flush_outbox(self.group,self.developer,{"status":"idle"})
                    self.server.flush_outbox(self.group,self.developer,{"status":"idle"})
                posts=[c for c in self.cao.call_args_list if c.args[0]=="POST"]
                self.assertEqual(len(posts),1)
                self.assertEqual(posts[0].args[2],{"message":"原文兜底"})
                self.assertEqual((self.developer["hops"],self.developer["silent"]),(2,True))
                with self.assertRaises(ValueError):
                    self.server.retry_delivery(self.group,self.developer,token)

    def test_unexpected_dispatch_failures_keep_a_recoverable_job(self):
        for phase in ("before_input","after_input","input_exception"):
            with self.subTest(phase=phase):
                self.active(provider="claude_code")
                self.group["messages"].clear()
                self.cao.reset_mock()
                self.cao.side_effect=RuntimeError("private payload") if phase=="input_exception" else None
                self.cao.return_value={"dispatch_boundary":5}
                effects=[OSError("private disk detail")] if phase=="before_input" else (
                    [None,OSError("private disk detail"),OSError("private disk detail")] if phase=="after_input" else [None,None])
                def save():
                    effect=effects.pop(0) if len(effects)>1 else effects[0]
                    if effect:raise effect
                job={"id":"exception-job","attempt":0,"queued_at":1000,"text":"original","hops":2,"silent":False}
                with patch.object(self.server,"save_state",side_effect=save),patch("builtins.print") as diagnostic:
                    self.server.dispatch_delivery(self.group,self.developer,job)
                ambiguous=phase!="before_input"
                self.assertEqual(self.developer["delivery_state"],"uncertain" if ambiguous else "failed")
                self.assertEqual(self.developer["pending"],ambiguous)
                self.assertIs(self.developer["active_delivery"],job)
                self.assertEqual(sum(c.args[0]=="POST" for c in self.cao.call_args_list),int(ambiguous))
                self.assertTrue(self.server.public_group(self.group)["members"][0]["can_retry"])
                self.assertTrue(any(("可能未送达" if ambiguous else "投递失败") in m["text"] for m in self.group["messages"]))
                self.assertNotIn("private",str(diagnostic.call_args_list)+str(self.group["messages"]))

    def test_orphan_dispatching_state_does_not_wait_forever(self):
        self.active()
        self.developer["delivery_state"]="dispatching"
        self.cao.side_effect=None
        self.cao.return_value={"status":"idle"}
        self.server.poll_member(self.group,self.developer)
        self.assertEqual(self.developer["delivery_state"],"uncertain")
        self.assertTrue(self.server.public_group(self.group)["members"][0]["can_retry"])
        self.assertTrue(any("可能未送达" in m["text"] for m in self.group["messages"]))

    def test_poll_errors_and_persistence_failures_do_not_stop_other_members(self):
        self.active()
        self.group["members"].append(self.reviewer)
        class StopPolling(Exception):pass
        seen=[]
        def poll(g,m):
            seen.append(m["id"])
            if m is self.developer:raise RuntimeError("private payload")
        with patch.object(self.server,"poll_member",side_effect=poll), \
             patch.object(self.server,"save_state",side_effect=OSError("private disk detail")), \
             patch.object(self.server.time,"sleep",side_effect=[None,None,StopPolling]), \
             patch("builtins.print") as diagnostic:
            with self.assertRaises(StopPolling):self.server.poll_loop()
        self.assertEqual(seen.count(self.reviewer["id"]),2)
        self.assertEqual(self.developer["delivery_state"],"uncertain")
        self.assertTrue(self.server.public_group(self.group)["members"][0]["can_retry"])
        self.assertNotIn("private",str(diagnostic.call_args_list)+str(self.group["messages"]))

    def test_startup_queue_keeps_fifo_order_until_each_reply_finishes(self):
        self.server.send_to(self.group,self.developer,"第一条",0)
        self.server.send_to(self.group,self.developer,"第二条",0)
        self.developer.update(terminal_id="dev-terminal",ready_terminal="dev-terminal")
        self.cao.side_effect=lambda method,*args,**kwargs: ({"dispatch_boundary":1}
                               if method=="POST" else {"output":self.CODEX_READY})
        self.server.flush_outbox(self.group,self.developer)
        self.assertEqual(len(self.developer["outbox"]),1)
        self.server.handle_reply(self.group,self.developer,"第一条回复")
        self.server.flush_outbox(self.group,self.developer)
        posts=[c.args[2]["message"] for c in self.cao.call_args_list if c.args[0]=="POST"]
        self.assertEqual(posts,["第一条","第二条"])

    def test_owner_broadcast_includes_members_without_terminals(self):
        self.reviewer.update(provider="claude_code",terminal_id=None,ready_terminal=None,outbox=[])
        self.group["members"].append(self.reviewer)
        with patch.object(self.server,"run_bg",side_effect=lambda fn,*args:fn(*args)):
            message=self.server.post_message(self.group,"所有成员启动后处理")
        self.assertEqual(message["to"],["开发","评审"])
        self.assertEqual([len(m["outbox"]) for m in self.group["members"]],[1,1])


class CodexCompletionTests(ServerTestCase):
    # Exact Codex 0.164.0 bytes supplied by the owner; do not import fake constants.
    WORKING = "\x1b]7501;state=working:app=codex\x1b\\"
    DONE = "\x1b]7501;state=done:app=codex\x1b\\"

    def prepare(self, provider="codex", boundary=0):
        self.developer.update(provider=provider, status="idle", pending=True,
                              reply_handled=False, boundary=1, sent_at=0,
                              codex_dispatch_markers=boundary, codex_dispatch_state=None, codex_saw_working=False)
        self.group["members"] = [self.developer]
        self.group["messages"].clear()

    def poll(self, raw, status="idle", generation=2, fail_full=False, last_unsupported=False, output_generation=None):
        def response(method, path, query=None, **kwargs):
            self.assertEqual(method, "GET")
            if path.endswith("/output"):
                if query["mode"] == "full":
                    if fail_full:
                        raise self.server.CaoError(503, "temporary output failure")
                    return {"output": raw}
                if last_unsupported:
                    raise self.server.CaoError(422, "last output unavailable")
                return {"output": "本轮最终回复"}
            return {"status": status, "status_generation": generation,
                    "output_generation": output_generation}
        self.cao.side_effect = response
        class StopPolling(Exception):
            pass
        with patch.object(self.server.time, "sleep", side_effect=[None, StopPolling]):
            with self.assertRaises(StopPolling):
                self.server.poll_loop()

    def dispatch_from(self, previous):
        self.prepare()
        self.cao.side_effect = [{"output": previous}, {"dispatch_boundary": 7}]
        self.server.send_to(self.group, self.developer, "新一轮", 0)

    def test_real_codex_0164_bytes_and_legacy_formats_are_parsed(self):
        raw="\x1b]7501;state=working:app=codex\x1b\\正文\x1b]7501;state=done:app=codex\x1b\\"
        self.assertEqual(self.server.codex_states(raw),["working","done"])
        for suffix in (":app=codex",":app=another-app",""):
            for terminator in ("\x1b\\","\x07"):
                with self.subTest(suffix=suffix,terminator=repr(terminator)):
                    # No-app variants are compatibility cases, not default CLI fixtures.
                    raw=f"\x1b]7501;state=working{suffix}{terminator}正文\x1b]7501;state=done{suffix}{terminator}"
                    self.assertEqual(self.server.codex_states(raw),["working","done"])
        self.assertEqual(self.server.codex_states(
            self.DONE+"\x1b]7501;sta"+"\x1b]7501;state=future_state:app=codex\x1b\\"),
            ["done",None,"future_state"])

    def test_full_fallback_starts_at_the_native_working_marker(self):
        self.dispatch_from(self.WORKING+"旧回复"+self.DONE)
        self.poll("旧重绘前缀不应进群"+self.WORKING+"本轮回复"+self.DONE,last_unsupported=True)
        self.assertFalse(self.developer["pending"])
        self.assertEqual([m["text"] for m in self.group["messages"]],["本轮回复"])

    def test_cleared_buffer_finishes_short_reply_before_any_working_poll(self):
        for previous, current in (("上一轮很长的内容", "短回复"),
                                  ("短", "本轮更长的最终回复"),
                                  ("旧内容", "新内容")):
            with self.subTest(previous=previous, current=current):
                self.dispatch_from(self.WORKING + previous + self.DONE)
                self.poll(self.WORKING + current + self.DONE)
                self.assertFalse(self.developer["pending"])
                self.assertEqual(self.group["messages"][0]["text"], "本轮最终回复")

    def test_cleared_done_only_with_new_generation_finishes_without_a_working_poll(self):
        self.dispatch_from(self.WORKING+"上一轮"+self.DONE+self.DONE)
        self.developer["boundary"]=20
        self.developer["delivery_started"]=False
        self.poll("x"*32700+"本轮"+self.DONE,output_generation=48,generation=48)
        self.assertTrue(self.developer["codex_buffer_reset"])
        self.assertFalse(self.developer["codex_saw_working"])
        self.assertFalse(self.developer["pending"])
        self.assertTrue(self.developer["delivery_started"])
        self.assertEqual([m["text"] for m in self.group["messages"]],["本轮最终回复"])

    def test_cleared_done_only_needs_new_output_not_just_new_status(self):
        self.dispatch_from(self.WORKING+"旧回复"+self.DONE)
        raw="本轮仅剩done"+self.DONE
        for generation in (None,6,7):
            self.poll(raw,output_generation=generation,generation=99)
            self.assertTrue(self.developer["pending"])
            self.assertEqual(self.group["messages"],[])
        self.poll(raw,output_generation=8,generation=99)
        self.assertFalse(self.developer["pending"])

    def test_fresh_old_done_never_forgets_that_dispatch_began_during_working(self):
        self.dispatch_from(self.WORKING+"旧轮仍在执行")
        for generation in (8,9,10):
            self.poll("旧轮结束"+self.DONE,output_generation=generation)
            self.assertTrue(self.developer["pending"])
            self.assertEqual(self.developer["codex_dispatch_state"],"working")
            self.assertEqual(self.group["messages"],[])
        self.poll(self.WORKING+"本轮"+self.DONE,output_generation=11)
        self.assertFalse(self.developer["pending"])

    def test_fast_completion_counts_as_started_for_codex_and_other_providers(self):
        for provider in ("codex","claude_code"):
            with self.subTest(provider=provider):
                self.prepare(provider)
                self.developer["delivery_started"]=False
                self.poll(self.WORKING+"快速完成"+self.DONE,output_generation=2)
                self.assertFalse(self.developer["pending"])
                self.assertTrue(self.developer["delivery_started"])

    def test_identical_short_reply_needs_fresh_output_not_fresh_status(self):
        raw = self.WORKING + "收到" + self.DONE
        self.dispatch_from(raw)
        self.poll(raw, generation=8, output_generation=7)
        self.assertTrue(self.developer["pending"])
        self.poll(raw, generation=7, output_generation=8)
        self.assertFalse(self.developer["pending"])
        self.assertEqual(self.group["messages"][0]["text"], "本轮最终回复")

    def test_cleared_old_done_without_working_cannot_finish_new_input(self):
        self.dispatch_from(self.WORKING + "旧轮仍在执行")
        self.poll("旧轮结束" + self.DONE)
        self.assertTrue(self.developer["pending"])
        self.poll("旧轮结束" + self.DONE)
        self.assertTrue(self.developer["pending"])
        self.poll("旧轮结束" + self.DONE + self.WORKING + "新轮" + self.DONE)
        self.assertFalse(self.developer["pending"])

    def test_cleared_full_fallback_drops_old_turn_offset(self):
        self.dispatch_from(self.WORKING + "上一轮很长的正文" + self.DONE)
        self.poll(self.WORKING + "短" + self.DONE, last_unsupported=True)
        self.assertFalse(self.developer["pending"])
        self.assertEqual([m["text"] for m in self.group["messages"]], ["短"])

    def test_idle_without_any_marker_never_falls_back_to_status_or_elapsed_time(self):
        self.prepare()
        self.poll("正在加载，尚无标记")
        self.assertTrue(self.developer["pending"])
        self.assertEqual(self.group["messages"], [])
        self.assertFalse(any(call.args[2] == {"mode": "last"}
                             for call in self.cao.call_args_list if len(call.args) > 2))

    def test_last_marker_controls_completion_including_split_or_unknown_markers(self):
        for raw, complete in ((self.DONE + self.WORKING, False),
                              (self.WORKING + self.DONE, True),
                              (self.DONE + "\x1b]7501;sta", False),
                              (self.DONE + "\x1b]7501;state=don", False),
                              (self.DONE + "\x1b]7501;state=done_later:app=codex\x1b\\", False),
                              ("\x1b]7501;state=working:app=codex\x07正文\x1b]7501;state=done:app=codex\x07", True),
                              (self.DONE, False)):
            with self.subTest(raw=repr(raw)):
                self.prepare()
                self.poll(raw)
                self.assertEqual(self.developer["pending"], not complete)
                self.assertEqual([m["text"] for m in self.group["messages"]],
                                 ["本轮最终回复"] if complete else [])

    def test_previous_turn_done_is_not_reused_after_new_dispatch(self):
        self.prepare(boundary=2)
        previous = self.WORKING + "上一轮" + self.DONE
        self.poll(previous + "本轮尚无标记")
        self.assertTrue(self.developer["pending"])
        self.assertEqual(self.group["messages"], [])
        self.poll(previous + "本轮" + self.WORKING + self.DONE)
        self.assertFalse(self.developer["pending"])
        self.assertEqual(self.group["messages"][0]["text"], "本轮最终回复")

    def test_completion_of_an_already_working_turn_cannot_finish_the_new_dispatch(self):
        for old_output in (self.WORKING + self.DONE, self.DONE):
            with self.subTest(old_output=repr(old_output)):
                self.prepare(boundary=1)
                self.developer["codex_dispatch_state"] = "working"
                self.poll(old_output)
                self.assertTrue(self.developer["pending"])
                self.poll(old_output)
                self.assertTrue(self.developer["pending"])
                self.poll(old_output + self.WORKING + self.DONE)
                self.assertFalse(self.developer["pending"])

    def test_observed_working_then_done_survives_a_rolling_output_window(self):
        self.prepare(boundary=4)
        self.poll(self.DONE)
        self.assertTrue(self.developer["pending"])
        self.poll(self.WORKING)
        self.assertTrue(self.developer["pending"])
        self.poll(self.DONE)
        self.assertFalse(self.developer["pending"])

    def test_codex_done_is_authoritative_even_when_status_or_generation_lags(self):
        for status in ("idle", "processing"):
            with self.subTest(status=status):
                self.prepare()
                self.poll(self.WORKING + self.DONE, status=status, generation=1)
                self.assertFalse(self.developer["pending"])
                self.assertEqual(self.group["messages"][0]["text"], "本轮最终回复")

    def test_other_providers_keep_status_and_generation_behavior_without_markers(self):
        for provider in ("claude_code", "kiro_cli"):
            for fresh in (False, True):
                with self.subTest(provider=provider, fresh=fresh):
                    self.prepare(provider)
                    self.poll("没有任何 OSC 标记", generation=2 if fresh else 1)
                    self.assertEqual(self.developer["pending"], not fresh)
                    self.assertFalse(any(call.args[2] == {"mode": "full"}
                                         for call in self.cao.call_args_list if len(call.args) > 2))
                    self.cao.reset_mock()

    def test_raw_output_failure_does_not_mark_codex_complete(self):
        self.prepare()
        self.poll("", fail_full=True)
        self.assertTrue(self.developer["pending"])
        self.assertEqual(self.group["messages"], [])

    def test_dispatch_captures_codex_marker_boundary_and_resets_progress(self):
        previous = self.WORKING + "上一轮" + self.DONE
        self.prepare()
        self.developer["codex_saw_working"] = True
        self.cao.side_effect = [{"output": previous}, {"dispatch_boundary": 7}]
        self.server.send_to(self.group, self.developer, "新一轮", 0)
        self.assertEqual(self.developer["codex_dispatch_markers"], 2)
        self.assertFalse(self.developer["codex_saw_working"])
        self.assertEqual(self.developer["full_len"], len(previous))
        self.assertEqual([call.args[0] for call in self.cao.call_args_list], ["GET", "POST"])

    def test_last_output_fallback_keeps_only_new_turn_and_cleans_control_sequences(self):
        previous = self.WORKING + "上一轮" + self.DONE
        self.prepare(boundary=2)
        self.developer["full_len"] = len(previous)
        self.poll(previous + self.WORKING + "本轮最终回复" + self.DONE, last_unsupported=True)
        self.assertFalse(self.developer["pending"])
        self.assertEqual([m["text"] for m in self.group["messages"]], ["本轮最终回复"])

    def test_missing_raw_baseline_prevents_unsafe_dispatch(self):
        self.prepare()
        self.developer["pending"] = False
        self.cao.side_effect = self.server.CaoError(503, "raw output unavailable")
        self.server.send_to(self.group, self.developer, "新一轮", 0)
        self.assertEqual(self.cao.call_args.args[0], "GET")
        self.assertFalse(self.developer["pending"])
        self.assertIn("发给 开发 失败", self.group["messages"][0]["text"])


class SessionStartupTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.group.update(session_slug="shared-session",session_name=None)
        self.member=self.server.clean_member({"name":"启动成员","provider":"codex"},[])
        self.group["members"]=[self.member]
        self.profile_patch=patch.object(self.server,"install_profile")
        self.profile_patch.start();self.addCleanup(self.profile_patch.stop)
        self.provider_patch=patch.object(self.server,"providers_with_install",return_value=[])
        self.provider_patch.start();self.addCleanup(self.provider_patch.stop)

    def test_duplicate_session_race_joins_the_existing_session(self):
        self.cao.side_effect=[self.server.CaoError(404,"session missing"),
            self.server.CaoError(400,"Session 'shared-session' already exists"),
            {"id":"joined","session_name":"shared-session","status":"idle"}]
        self.server.start_member(self.group,self.member)
        self.assertEqual(self.member["terminal_id"],"joined")
        self.assertEqual(self.group["session_name"],"shared-session")
        self.assertEqual([(c.args[0],c.args[1]) for c in self.cao.call_args_list],
            [("GET","/sessions/shared-session"),("POST","/sessions"),
             ("POST","/sessions/shared-session/terminals:operator")])

    def test_unrelated_bad_request_is_not_treated_as_an_existing_session(self):
        self.cao.side_effect=[self.server.CaoError(404,"session missing"),
                              self.server.CaoError(400,"invalid working directory")]
        with self.assertRaises(self.server.CaoError) as failed:
            self.server.start_member(self.group,self.member)
        self.assertEqual(failed.exception.status,400)
        self.assertIsNone(self.member["terminal_id"])
        self.assertEqual(len(self.cao.call_args_list),2)

    def test_session_disappearing_during_join_is_recreated(self):
        self.group["session_name"]="shared-session"
        self.cao.side_effect=[{"session":{"id":"shared-session","status":"active"}},
            self.server.CaoError(404,"session removed"),{"id":"rebuilt","session_name":"shared-session","status":"idle"}]
        self.server.start_member(self.group,self.member)
        self.assertEqual(self.member["terminal_id"],"rebuilt")
        self.assertEqual([(c.args[0],c.args[1]) for c in self.cao.call_args_list],
            [("GET","/sessions/shared-session"),("POST","/sessions/shared-session/terminals:operator"),
             ("POST","/sessions")])

    def test_terminated_session_record_is_recreated_instead_of_joined(self):
        failure={"id":"failed-terminal","status":"error","deferred_init_failure":{"error_type":"provider_start_failure"}}
        for payload in ({"session":{"id":"shared-session","status":"terminated"},"terminals":[failure]},
                        {"status":"terminated","terminals":[failure]}):
            with self.subTest(payload=payload):
                self.member["terminal_id"]=None
                self.cao.reset_mock()
                self.cao.side_effect=[payload,{"id":"rebuilt","session_name":"shared-session","status":"idle"}]
                self.server.start_member(self.group,self.member)
                self.assertEqual(self.member["terminal_id"],"rebuilt")
                self.assertEqual([(c.args[0],c.args[1]) for c in self.cao.call_args_list],
                    [("GET","/sessions/shared-session"),("POST","/sessions")])

    def test_404_rebuild_attempt_is_bounded_when_join_still_fails(self):
        self.cao.side_effect=[{"session":{"id":"shared-session","status":"active"}},
            self.server.CaoError(404,"missing backend"),
            self.server.CaoError(400,"Session 'shared-session' already exists"),
            self.server.CaoError(404,"operator still unavailable")]
        with self.assertRaises(self.server.CaoError) as failure:
            self.server.start_member(self.group,self.member)
        self.assertEqual(failure.exception.status,404)
        self.assertEqual([(c.args[0],c.args[1]) for c in self.cao.call_args_list],
            [("GET","/sessions/shared-session"),("POST","/sessions/shared-session/terminals:operator"),
             ("POST","/sessions"),("POST","/sessions/shared-session/terminals:operator")])

    def test_operator_404_prioritizes_rebuild_even_with_a_stale_live_caller(self):
        self.group["members"].append(self.reviewer)
        for old_operator in (False,True):
            with self.subTest(old_operator=old_operator):
                self.member["terminal_id"]=None
                self.cao.reset_mock()
                tail=([self.server.CaoError(400,"Session 'shared-session' already exists"),
                       self.server.CaoError(404,"old operator route"),{"id":"recovered","status":"idle"}]
                      if old_operator else [{"id":"recovered","status":"idle"}])
                self.cao.side_effect=[{"session":{"id":"shared-session","status":"active"}},
                                     self.server.CaoError(404,"missing backend or route"),*tail]
                self.server.start_member(self.group,self.member)
                self.assertEqual(self.member["terminal_id"],"recovered")
                expected=[("GET","/sessions/shared-session"),("POST","/sessions/shared-session/terminals:operator"),
                          ("POST","/sessions")]
                if old_operator:
                    expected += [("POST","/sessions/shared-session/terminals:operator"),
                                 ("POST","/sessions/shared-session/terminals")]
                    self.assertEqual(self.cao.call_args.args[2]["caller_id"],"review-terminal")
                self.assertEqual([(c.args[0],c.args[1]) for c in self.cao.call_args_list],expected)

    def test_legacy_join_keeps_the_live_caller(self):
        self.group["session_name"]="shared-session"
        self.group["members"].append(self.reviewer)
        self.cao.side_effect=[{"session":{"id":"shared-session","status":"active"}},self.server.CaoError(405,"operator unavailable"),
                              {"id":"legacy-joined","status":"idle"}]
        self.server.start_member(self.group,self.member)
        self.assertEqual(self.member["terminal_id"],"legacy-joined")
        self.assertEqual(self.cao.call_args.args[1],"/sessions/shared-session/terminals")
        self.assertEqual(self.cao.call_args.args[2]["caller_id"],"review-terminal")


class CleanOutputTests(ServerTestCase):
    def test_removes_ansi_colors_cursor_and_erase_sequences(self):
        self.assertEqual(self.server.clean_output("\x1b[32m完成\x1b[0m\x1b[2K\x1b[?25h\r\n"), "完成")

    def test_removes_osc_sequences_with_bell_or_string_terminator(self):
        for suffix in (":app=codex", ""):
            for terminator in ("\x07", "\x1b\\"):
                with self.subTest(suffix=suffix,terminator=repr(terminator)):
                    text = f"\x1b]7501;state=working{suffix}{terminator}正文\x1b]7501;state=done{suffix}{terminator}"
                    self.assertEqual(self.server.clean_output(text), "正文")

    def test_removes_real_claude_tui_footer(self):
        for verb, duration in (("Sautéed", "1m 8s"), ("Cooked", "2m 3s"),
                               ("Brewed", "3s"), ("Churned", "1m"), ("Cogitated", "1m 33s")):
            with self.subTest(verb=verb):
                text = f"已完成\n\n✻ {verb} for {duration} · done 3:26 AM"
                self.assertEqual(self.server.clean_output(text), "已完成")

    def test_removes_real_codex_tui_footer(self):
        self.assertEqual(self.server.clean_output("已完成\nWorked for 4m 55s • 03:19"), "已完成")

    def test_footer_without_body_becomes_empty(self):
        for text in ("✻ Sautéed for 1m 8s · done 3:26 AM", "Worked for 4m 55s • 03:19"):
            with self.subTest(text=text):
                self.assertEqual(self.server.clean_output(text), "")

    def test_preserves_prose_quotes_code_and_non_trailing_footer_examples(self):
        for text in (
            "Worked for a client yesterday", "✻ Sautéed vegetables for dinner",
            "✻ Cooked for a client yesterday",
            "✻ Slow Cooked for 2m 3s · done 3:26 AM",
            "✻ Cooked2 for 2m 3s · done 3:26 AM",
            "✻ Cooked for 2m 3s · done 3:26 AM\n这里还有正文",
            "```text\n✻ Brewed for 3s · done 3:26 AM\n```",
            "示例：Worked for 4m 55s • 03:19",
            "Worked for 4m 55s • 03:19\n这里还有正文",
            "```text\nWorked for 4m 55s • 03:19\n```",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.server.clean_output(text), text)

    def test_normalizes_blank_lines_and_keeps_unicode(self):
        self.assertEqual(self.server.clean_output(" \n中文 ✅\n\n\n下一段\r\n "), "中文 ✅\n\n下一段")


if __name__ == "__main__":
    unittest.main()
