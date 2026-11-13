"""Isolated standard-library unit tests; never use a running CAO instance."""

import importlib.util
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
        self.developer = {"id": "dev", "name": "开发", "terminal_id": "dev-terminal", "hops": 0}
        self.reviewer = {"id": "review", "name": "评审", "terminal_id": "review-terminal"}
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


class CodexCompletionTests(ServerTestCase):
    WORKING = "\x1b]7501;state=working\x07"
    DONE = "\x1b]7501;state=done\x07"

    def prepare(self, provider="codex", boundary=0):
        self.developer.update(provider=provider, status="idle", pending=True,
                              reply_handled=False, boundary=1, sent_at=0,
                              codex_dispatch_markers=boundary, codex_dispatch_state=None, codex_saw_working=False)
        self.group["members"] = [self.developer]
        self.group["messages"].clear()

    def poll(self, raw, status="idle", generation=2, fail_full=False, last_unsupported=False):
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
            return {"status": status, "status_generation": generation}
        self.cao.side_effect = response
        class StopPolling(Exception):
            pass
        with patch.object(self.server.time, "sleep", side_effect=[None, StopPolling]):
            with self.assertRaises(StopPolling):
                self.server.poll_loop()

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
                              (self.DONE + "\x1b]7501;state=done_later\x07", False),
                              ("\x1b]7501;state=working\x1b\\正文\x1b]7501;state=done\x1b\\", True),
                              (self.DONE, True)):
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


class CleanOutputTests(ServerTestCase):
    def test_removes_ansi_colors_cursor_and_erase_sequences(self):
        self.assertEqual(self.server.clean_output("\x1b[32m完成\x1b[0m\x1b[2K\x1b[?25h\r\n"), "完成")

    def test_removes_osc_sequences_with_bell_or_string_terminator(self):
        for terminator in ("\x07", "\x1b\\"):
            with self.subTest(terminator=repr(terminator)):
                text = f"\x1b]7501;state=working{terminator}正文\x1b]7501;state=done{terminator}"
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
