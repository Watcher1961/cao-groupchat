"""Permission validation and legacy-state regressions using isolated server imports."""

import json
from unittest.mock import patch

from test_server import ServerTestCase


class PermissionModeTests(ServerTestCase):
    INVALID = ("bogus", "default", "bypassPermissions", "plan ", " acceptEdits",
               "plan\nmodel: injected", None, [], {}, 1, False)

    def member(self, provider="claude_code", **extra):
        member = self.server.clean_member({"name": "权限测试", "provider": provider,
                                           "effort": "low", **extra}, [])
        self.group["members"] = [member]
        return member

    def test_ui_modes_and_omission_keep_effort_and_profile(self):
        for extra in ({}, {"permission_mode": ""}, {"permission_mode": "acceptEdits"},
                      {"permission_mode": "plan"}):
            with self.subTest(extra=extra):
                member = self.member(**extra)
                mode = extra.get("permission_mode", "")
                self.assertEqual(member["permission_mode"], mode)
                self.assertEqual(member["effort"], "low")
                profile = self.server.build_profile(self.group, member)
                self.assertIn("claudeConfig:\n  effort: low\n", profile)
                if mode:
                    self.assertIn(f"permissionMode: {mode}\n", profile)
                else:
                    self.assertNotIn("permissionMode:", profile)

    def test_claude_rejects_unknown_modes_and_non_string_values(self):
        for value in self.INVALID:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "权限模式"):
                    self.member(permission_mode=value)

    def test_other_providers_store_empty_permission_mode(self):
        for provider in (p["id"] for p in self.server.PROVIDERS if p["id"] != "claude_code"):
            for value in ("acceptEdits", "plan", *self.INVALID):
                with self.subTest(provider=provider, value=value):
                    member = self.member(provider, effort="", permission_mode=value)
                    self.assertEqual(member["permission_mode"], "")
                    self.assertNotIn("permissionMode:", self.server.build_profile(self.group, member))

    def test_legacy_invalid_modes_load_without_losing_groups_or_effort(self):
        cases = [("claude_code", value, "") for value in self.INVALID]
        cases += [("claude_code", value, value) for value in ("", "acceptEdits", "plan")]
        cases += [("codex", "plan", "")]
        members = []
        for index, (provider, value, _) in enumerate(cases):
            member = self.member(provider)
            member.update(id=f"legacy-{index}", permission_mode=value)
            members.append(member)
        self.group["members"] = members
        self.server.save_state()
        self.server.groups.clear()
        self.server.load_state()
        restored = self.server.groups[self.group["id"]]
        self.assertEqual(len(restored["members"]), len(cases))
        for member, (provider, value, expected) in zip(restored["members"], cases):
            with self.subTest(provider=provider, value=value):
                self.assertEqual(member["permission_mode"], expected)
                self.assertEqual(member["effort"], "low")
                profile = self.server.build_profile(restored, member)
                if expected:
                    self.assertIn(f"permissionMode: {expected}\n", profile)
                else:
                    self.assertNotIn("permissionMode:", profile)

    def test_profile_defaults_invalid_mode_even_without_state_loading(self):
        for value in self.INVALID:
            with self.subTest(value=value):
                member = self.member()
                member["permission_mode"] = value
                self.assertNotIn("permissionMode:", self.server.build_profile(self.group, member))

    def test_invalid_routes_do_not_mutate_members_or_start_stop_terminals(self):
        member = self.member(permission_mode="acceptEdits")
        member.update(terminal_id="running-terminal", status="idle")
        snapshot = json.dumps(self.server.groups)
        invalid = {"name": member["name"], "provider": "claude_code",
                   "effort": "low", "permission_mode": "bogus"}
        valid = {"name": "先校验的合法成员", "provider": "claude_code"}
        routes = [
            ("POST", "/api/groups", {"name": "新群", "workdir": self.group["workdir"],
                                     "members": [valid, invalid]}),
            ("POST", f"/api/groups/{self.group['id']}/members", {**invalid, "name": "新成员"}),
            ("PUT", f"/api/groups/{self.group['id']}/members/{member['id']}", invalid),
        ]
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                self.server.groups = json.loads(snapshot)
                handler = object.__new__(self.server.H)
                handler.path = path
                handler._body = lambda: dict(body)
                handler._json = lambda status, payload: (status, payload)
                with patch.object(self.server, "run_bg") as start, \
                     patch.object(self.server, "stop_member") as stop, \
                     patch.object(self.server, "save_state") as save, \
                     patch.object(self.server, "append_log") as log:
                    status, payload = handler._route(method)
                    self.assertEqual(status, 400)
                    self.assertIn("权限模式", payload["error"])
                    self.assertEqual(self.server.groups, json.loads(snapshot))
                    for action in (start, stop, save, log, self.cao):
                        action.assert_not_called()
