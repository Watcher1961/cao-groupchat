"""Project-local opt-in and privacy-preserving Claude statusLine capture."""

import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_server import ServerTestCase


COLLECTOR = Path(__file__).resolve().parents[1] / "claude_statusline.py"


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-statusline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / "limits" / "group.json"

    def invoke(self, payload, target=None):
        result = subprocess.run([sys.executable, "-I", "-B", str(COLLECTOR), str(target or self.target)],
                                input=payload, text=True, capture_output=True, timeout=5,
                                cwd=self.root, env={"HOME": str(self.root), "PATH": os.defpath})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_only_percentage_reset_and_timestamp_are_written(self):
        future = time.time() + 3600
        window = {"used_percentage": 37.5, "resets_at": future, "private": "never-save"}
        self.invoke(json.dumps({"rate_limits": {name: window for name in ("five_hour", "seven_day", "spend_limit")},
                                "cwd": "private-cwd", "transcript_path": "private-transcript",
                                "messages": ["private-body"], "token": "private-token"}))
        data = json.loads(self.target.read_text())
        self.assertEqual(set(data), {"rate_limits", "updated_at"})
        self.assertLess(abs(data["updated_at"] - time.time()), 5)
        for window in data["rate_limits"].values():
            self.assertEqual(window, {"used_percentage": 37.5, "resets_at": future})
        self.assertNotIn("private", self.target.read_text())
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_missing_independent_invalid_and_expired_windows_replace_snapshot(self):
        future = time.time() + 3600
        self.invoke(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": future}}}))
        self.invoke(json.dumps({"rate_limits": {"seven_day": {"used_percentage": 20, "resets_at": future},
            "five_hour": {"used_percentage": 90, "resets_at": 1},
            "spend_limit": {"used_percentage": True, "resets_at": future}}}))
        self.assertEqual(set(json.loads(self.target.read_text())["rate_limits"]), {"seven_day"})
        self.invoke("{}")
        self.assertEqual(json.loads(self.target.read_text())["rate_limits"], {})

    def test_bad_input_is_silent_and_keeps_previous_snapshot(self):
        self.invoke("{}")
        before = self.target.read_bytes()
        for payload in ("not-json", "[]", "null", '{"rate_limits":[]}', "x" * (1024 * 1024 + 1)):
            with self.subTest(payload=payload[:24]):
                self.invoke(payload)
                self.assertEqual(self.target.read_bytes(), before)

    def test_atomic_replace_failure_is_silent_and_cleans_temporary_file(self):
        self.invoke("{}")
        before = self.target.read_bytes()
        code = ('import os,runpy,sys;'
                'os.replace=lambda *a:(_ for _ in ()).throw(OSError("write failed"));'
                'sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")')
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(COLLECTOR), str(self.target)],
                                input="{}", text=True, capture_output=True, timeout=5,
                                cwd=self.root, env={"HOME": str(self.root), "PATH": os.defpath})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])


class ClaudeLimitsTests(ServerTestCase):
    @property
    def settings_file(self):
        return Path(self.group["workdir"]) / ".claude" / "settings.local.json"

    def put_settings(self, content):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(content)

    def test_default_off_and_strict_bool_do_not_touch_project(self):
        self.assertFalse(self.server.automation_view(self.group)["settings"]["claude_limits"])
        self.server.update_automation(self.group, {"relay_limit": 7})
        self.assertFalse(self.settings_file.parent.exists())
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.server.update_automation(self.group, {"claude_limits": value})
        self.assertFalse(self.settings_file.parent.exists())

    def test_enable_atomic_merge_and_disable_only_owned_key(self):
        original = {"permissions": {"allow": ["Read"]}, "env": {"UNRELATED": "synthetic-value"}}
        self.put_settings(json.dumps(original))
        original_mode = self.settings_file.stat().st_mode & 0o777
        with patch.object(self.server.os, "replace", wraps=os.replace) as replace:
            self.server.update_automation(self.group, {"claude_limits": True})
        enabled = json.loads(self.settings_file.read_text())
        self.assertEqual({k: v for k, v in enabled.items() if k != "statusLine"}, original)
        self.assertEqual(self.settings_file.stat().st_mode & 0o777, original_mode)
        argv = shlex.split(enabled["statusLine"]["command"])
        self.assertEqual(enabled["statusLine"]["type"], "command")
        self.assertIn(str(COLLECTOR), argv)
        self.assertEqual(Path(argv[-1]), self.server.claude_limits_path(self.group))
        self.assertTrue(any(Path(call.args[1]).resolve() == self.settings_file.resolve() for call in replace.call_args_list))
        self.server.update_automation(self.group, {"claude_limits": False})
        self.assertEqual(json.loads(self.settings_file.read_text()), original)

    def test_owned_only_file_is_deleted_and_foreign_replacement_is_preserved(self):
        self.server.update_automation(self.group, {"claude_limits": True})
        self.server.update_automation(self.group, {"claude_limits": False})
        self.assertFalse(self.settings_file.exists())
        self.server.update_automation(self.group, {"claude_limits": True})
        replacement = '{"statusLine":{"type":"command","command":"user-status"},"other":42}'
        self.put_settings(replacement)
        self.server.update_automation(self.group, {"claude_limits": False})
        self.assertEqual(self.settings_file.read_text(), replacement)

    def test_deleting_enabled_group_removes_only_its_statusline(self):
        self.put_settings('{"other":42}')
        self.server.update_automation(self.group, {"claude_limits": True})
        handler = object.__new__(self.server.H)
        handler.path = f"/api/groups/{self.group['id']}"
        handler._json = lambda code, value: (code, value)
        with patch.object(self.server, "stop_member"):
            self.assertEqual(handler._route("DELETE"), (200, {"ok": True}))
        self.assertEqual(json.loads(self.settings_file.read_text()), {"other": 42})
        self.assertNotIn(self.group["id"], self.server.groups)

    def quota_failure_cases(self):
        return (("foreign", '{"statusLine":{"type":"command","command":"user-status"}}'),
                ("broken", '{broken'),
                ("moved", '{"statusLine":{"type":"command","command":"python moved/claude_statusline.py moved/cache.json"}}'),
                ("write_failure", '{}'))

    def test_optional_quota_failure_does_not_block_claude_start(self):
        for reason, raw in self.quota_failure_cases():
            with self.subTest(reason=reason):
                self.put_settings('{}')
                self.server.update_automation(self.group, {"claude_limits": True})
                self.put_settings(raw)
                self.group["messages"].clear()
                member = self.server.clean_member({"name": "额度成员", "provider": "claude_code", "effort": "low"}, [])
                self.group["members"] = [member]
                failure = patch.object(self.server, "atomic_project_settings", side_effect=OSError("synthetic disk failure"))
                with patch.object(self.server, "providers_with_install", return_value=[]), \
                     patch.object(self.server, "install_profile") as install, \
                     patch.object(self.server, "start_group_terminal", return_value={"id": "new-terminal", "status": "idle"}), \
                     failure if reason == "write_failure" else nullcontext():
                    self.server.start_member(self.group, member)
                install.assert_called_once_with(self.group, member)
                self.assertEqual(member["terminal_id"], "new-terminal")
                self.assertEqual(member["effort"], "low")
                self.assertEqual(self.settings_file.read_text(), raw)
                self.assertEqual(len(self.group["messages"]), 1)
                self.assertEqual(self.group["messages"][0]["kind"], "system")
                self.assertIn("继续启动", self.group["messages"][0]["text"])

    def test_disabling_survives_foreign_broken_moved_or_unwritable_settings(self):
        for reason, raw in self.quota_failure_cases():
            with self.subTest(reason=reason):
                self.put_settings('{}')
                self.server.update_automation(self.group, {"claude_limits": True})
                if reason == "write_failure":
                    raw = self.settings_file.read_text()
                self.put_settings(raw)
                self.group["messages"].clear()
                failure = patch.object(self.server, "atomic_project_settings", side_effect=OSError("synthetic disk failure"))
                with failure if reason == "write_failure" else nullcontext():
                    result = self.server.update_automation(self.group, {"claude_limits": False})
                self.assertFalse(result["settings"]["claude_limits"])
                self.assertEqual(len(result["warnings"]), 1)
                self.assertEqual(self.settings_file.read_text(), raw)
                self.assertEqual(len(self.group["messages"]), 1)

    def test_deleting_survives_foreign_broken_moved_or_unwritable_settings(self):
        for reason, raw in self.quota_failure_cases():
            with self.subTest(reason=reason):
                self.server.groups[self.group["id"]] = self.group
                self.put_settings('{}')
                self.server.update_automation(self.group, {"claude_limits": True})
                if reason == "write_failure":
                    raw = self.settings_file.read_text()
                self.put_settings(raw)
                self.group["messages"].clear()
                handler = object.__new__(self.server.H)
                handler.path = f"/api/groups/{self.group['id']}"
                handler._json = lambda code, value: (code, value)
                failure = patch.object(self.server, "atomic_project_settings", side_effect=OSError("synthetic disk failure"))
                with patch.object(self.server, "stop_member") as stop, \
                     failure if reason == "write_failure" else nullcontext():
                    code, result = handler._route("DELETE")
                self.assertEqual((code, result["ok"]), (200, True))
                self.assertEqual(len(result["warnings"]), 1)
                self.assertEqual(stop.call_count, len(self.group["members"]))
                self.assertNotIn(self.group["id"], self.server.groups)
                self.assertEqual(self.settings_file.read_text(), raw)

    def test_disabled_quota_keeps_legacy_usage_contract(self):
        member = {**self.developer, "provider": "claude_code", "plan": "pro"}
        self.group["members"] = [member]
        with patch.object(self.server, "all_prices", return_value={}), \
             patch.object(self.server, "claude_usage", return_value=None):
            for enabled in (False, True, False):
                if enabled or "automation" in self.group:
                    self.server.update_automation(self.group, {"claude_limits": enabled})
                usage = self.server.group_usage(self.group)["members"][member["id"]]
                self.assertEqual(usage["limits_available"], enabled)
                if enabled:
                    self.assertEqual(usage["limits"]["windows"], [])
                else:
                    self.assertIsNone(usage["limits"])

    def test_foreign_statusline_and_invalid_json_refuse_without_any_changes(self):
        for raw in ('{"statusLine":{"type":"command","command":"user-status"}}',
                    '{"statusLine":null}', '{broken', '[]', '{"other":NaN}'):
            with self.subTest(raw=raw):
                self.put_settings(raw)
                before = copy.deepcopy(self.group)
                with self.assertRaises(ValueError):
                    self.server.update_automation(self.group, {"claude_limits": True, "relay_limit": 20})
                self.assertEqual(self.settings_file.read_text(), raw)
                self.assertEqual(self.group, before)

    def test_symlink_and_global_home_targets_are_rejected(self):
        destination = self.server.DATA_DIR / "elsewhere.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}")
        self.settings_file.parent.mkdir(parents=True)
        self.settings_file.symlink_to(destination)
        with self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.assertEqual(destination.read_text(), "{}")
        self.group["workdir"] = str(Path.home())
        with self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.server.update_automation(self.group, {"claude_limits": False})
        self.assertFalse((Path.home() / ".claude").exists())

    def test_cache_directory_cannot_be_inside_global_claude_directory(self):
        global_dir = Path.home() / ".claude"
        global_dir.mkdir()
        self.server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (self.server.DATA_DIR / "claude_limits").symlink_to(global_dir, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.assertEqual(list(global_dir.iterdir()), [])
        self.assertFalse(self.settings_file.exists())
        self.server.DATA_DIR = Path.home() / ".claude" / "quota-cache"
        self.server.STATE_FILE = self.server.DATA_DIR / "groups.json"
        with self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.assertEqual(list(global_dir.iterdir()), [])

    def test_shared_workdir_is_rejected_for_group_isolation(self):
        self.server.groups["other"] = {**self.group, "id": "other"}
        with self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.assertFalse(self.settings_file.exists())
        del self.server.groups["other"]
        self.server.update_automation(self.group, {"claude_limits": True})
        with self.assertRaises(ValueError):
            self.server.create_group({"name": "另一个群", "workdir": self.group["workdir"]})

    def test_real_claude_nbsp_composer_bytes_still_satisfy_readiness(self):
        # Read-only tmux capture, 2026-11-14: 220 rails, prompt UTF-8 e29dafc2a0.
        screen = "─" * 220 + "\n❯\xa0\n" + "─" * 220 + "\n"
        member = self.server.clean_member({"name": "字节回归", "provider": "claude_code"}, [])
        member["terminal_id"] = "screen-terminal"
        self.cao.side_effect = lambda *args, **kwargs: {"output": screen}
        with patch.object(self.server.time, "time", return_value=1000):
            self.assertFalse(self.server.member_ready(member, {"status": "idle"}))
        with patch.object(self.server.time, "time", return_value=1001):
            self.assertTrue(self.server.member_ready(member, {"status": "idle"}))

    def test_project_replace_failure_and_state_save_failure_roll_back(self):
        self.put_settings('{"other":42}')
        before = copy.deepcopy(self.group)
        original_replace = os.replace
        def replace(source, destination):
            if Path(destination).resolve() == self.settings_file.resolve():
                raise OSError("synthetic write failure")
            return original_replace(source, destination)
        with patch.object(self.server.os, "replace", side_effect=replace), self.assertRaises(ValueError):
            self.server.update_automation(self.group, {"claude_limits": True})
        self.assertEqual(self.settings_file.read_text(), '{"other":42}')
        self.assertEqual(self.group, before)
        with patch.object(self.server, "save_state", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.server.update_automation(self.group, {"claude_limits": True})
        self.assertEqual(self.settings_file.read_text(), '{"other":42}')
        self.assertEqual(self.group, before)
        self.assertEqual(list(self.settings_file.parent.iterdir()), [self.settings_file])

    def test_backend_partial_expired_and_bad_cache_and_usage_without_log(self):
        self.server.update_automation(self.group, {"claude_limits": True})
        path = self.server.claude_limits_path(self.group)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated_at": 880, "rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": 1500},
            "seven_day": {"used_percentage": 99, "resets_at": 999},
            "spend_limit": {"used_percentage": 8, "resets_at": 1800}}}))
        self.group["members"] = [{**self.developer, "provider": "claude_code", "plan": "pro"}]
        with patch.object(self.server.time, "time", return_value=1000), \
             patch.object(self.server, "all_prices", return_value={}), \
             patch.object(self.server, "claude_usage", return_value=None):
            limits = self.server.group_usage(self.group)["members"]["dev"]["limits"]
        self.assertEqual(limits["updated_at"], 880)
        self.assertEqual([(w["window_minutes"], w["used_percent"]) for w in limits["windows"]], [(300, 12)])
        self.assertEqual(limits["spend_limit"]["used_percentage"], 8)
        path.write_text("bad-json")
        self.assertEqual(self.server.claude_limits(self.group)["windows"], [])
        self.server.update_automation(self.group, {"claude_limits": False})
        self.assertEqual(self.server.claude_limits(self.group)["windows"], [])
