"""Synthetic Codex control menus. Labels from local CLI; layout is synthetic."""
import json
from unittest.mock import patch
from test_server import ServerTestCase

W = "\x1b]7501;state=working:app=codex\x1b\\"
D = "\x1b]7501;state=done:app=codex\x1b\\"

def menu(selected=1):
    options = ("Switch to another model", "Keep current model", "Keep current model (never show again)")
    return "Approaching rate limits\n" + "\n".join(("› " if i == selected else "  ") + f"{i}. {label}" for i, label in enumerate(options, 1)) + "\nenter select · esc back"


class ModelMenuTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.m = self.developer
        self.m.update(provider="codex", status="idle", pending=False, outbox=[])
        self.raw, self.last, self.status, self.generation = "codex>\n? for shortcuts", "合成最终答案", "idle", 2
        self.selected, self.ignore_navigation, self.stick_enter = 1, False, False
        self.keys, self.events = [], []
        self.clock = patch.object(self.server.time, "time", return_value=1000)
        self.now = self.clock.start(); self.addCleanup(self.clock.stop)
        self.cao.side_effect = self.response
        self.server.send_to(self.group, self.m, "synthetic task", 0)
        self.raw, self.status, self.generation = menu(), "processing", 3

    def response(self, method, path, query=None, **kwargs):
        if path.endswith("/output"):
            self.events.append(("read", self.selected))
            return {"output": self.raw if query["mode"] == "full" else self.last}
        if method == "GET":
            return {"status": self.status, "status_generation": self.generation, "output_generation": self.generation}
        if path.endswith("/key"):
            key = query["key"]
            self.keys.append(key); self.events.append((key, self.selected))
            if key in ("Down", "Up") and not self.ignore_navigation:
                self.selected += 1 if key == "Down" else -1
                self.raw = menu(self.selected)
                self.generation += 1
            if key == "Enter":
                if self.selected != 2:
                    raise AssertionError("Enter on the wrong option")
                if not self.stick_enter:
                    self.raw, self.status = W + "working", "processing"
                    self.generation += 1
            return {"success": True}
        return {"dispatch_boundary": self.generation}

    def tick(self, seconds=0):
        self.now.return_value += seconds
        self.server.poll_member(self.group, self.m)

    def test_navigate_verify_then_enter_without_quota_enabled_and_wait_for_reply(self):
        self.tick(); self.tick(); self.tick()
        self.assertEqual(self.keys, ["Down", "Enter"])
        arrow = next(i for i, item in enumerate(self.events) if item[0] == "Down")
        enter = next(i for i, item in enumerate(self.events) if item[0] == "Enter")
        self.assertIn(("read", 2), self.events[arrow + 1:enter])
        self.assertTrue(self.m["pending"])
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))
        self.assertEqual(sum("保持当前模型" in x["text"] for x in self.group["messages"]), 1)
        self.raw, self.status = W + "• 合成最终答案\n" + D + "\n? for shortcuts", "idle"
        self.generation += 1
        self.tick(); self.tick()
        self.assertEqual(len([x for x in self.group["messages"] if x["kind"] == "agent"]), 1)
        self.assertEqual(self.keys, ["Down", "Enter"])

    def test_already_selected_or_never_option_only_confirms_exact_keep(self):
        for selected, expected in ((2, ["Enter"]), (3, ["Up", "Enter"])):
            with self.subTest(selected=selected):
                self.m.pop("model_menu", None)
                self.selected, self.keys, self.raw, self.status = selected, [], menu(selected), "waiting_user_answer"
                self.tick(); self.tick(); self.tick()
                self.assertEqual(self.keys, expected)

    def test_partial_ambiguous_duplicate_or_missing_selection_never_press_keys(self):
        samples = ("[NO RESPONSE - synthetic fallback]\n2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back",
                   "2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back",
                   menu().replace("› ", "  "), menu() + "\n2. Other option", menu().replace("2. Keep current model", "2. Keep the current model"))
        for raw in samples:
            with self.subTest(raw=raw):
                self.m.pop("model_menu", None)
                self.m["model_menu_needs_owner"] = False
                self.keys, self.raw = [], raw
                self.tick(); self.tick()
                self.assertEqual(self.keys, [])
                self.assertTrue(self.m["model_menu_needs_owner"])
                self.assertTrue(self.m["pending"])
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))

    def test_unchanged_selection_after_arrow_times_out_without_enter_or_retry(self):
        self.ignore_navigation = True
        self.tick(); self.tick(3); self.tick(20)
        self.assertEqual(self.keys, ["Down"])
        self.assertTrue(self.m["model_menu_needs_owner"])

    def test_failed_key_response_never_sends_enter(self):
        original = self.response
        def failed_key(method, path, query=None, **kwargs):
            if path.endswith("/key"):
                self.keys.append(query["key"])
                return {"success": False}
            return original(method, path, query, **kwargs)
        self.cao.side_effect = failed_key
        self.tick(); self.tick()
        self.assertEqual(self.keys, ["Down"])
        self.assertTrue(self.m["model_menu_needs_owner"])

    def test_output_probe_failure_after_navigation_never_sends_enter(self):
        self.tick()
        original = self.response
        def unreadable(method, path, query=None, **kwargs):
            if path.endswith("/output"):
                raise self.server.CaoError(503, "synthetic probe unavailable")
            return original(method, path, query, **kwargs)
        self.cao.side_effect = unreadable
        self.tick(); self.tick(20)
        self.assertEqual(self.keys, ["Down"])
        self.assertTrue(self.m["model_menu_needs_owner"])

    def test_owner_can_close_ambiguous_menu_then_current_task_continues(self):
        self.raw = "2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back"
        self.tick()
        self.raw, self.status = W + "working", "processing"
        self.generation += 1
        self.tick()
        self.assertFalse(self.m["model_menu_needs_owner"])
        self.assertTrue(self.m["pending"])
        self.assertEqual(self.keys, [])

    def test_restart_after_navigation_does_not_repeat_arrow(self):
        self.tick()
        self.server.save_state(); self.server.load_state()
        self.group = self.server.groups[self.group["id"]]
        self.m = self.group["members"][0]
        self.tick(); self.tick()
        self.assertEqual(self.keys, ["Down", "Enter"])

    def test_enter_not_confirmed_survives_restart_without_second_enter(self):
        self.stick_enter = True
        self.tick(); self.tick()
        self.server.save_state(); self.server.load_state()
        self.group = self.server.groups[self.group["id"]]; self.m = self.group["members"][0]
        self.tick(11); self.tick(20)
        self.assertEqual(self.keys, ["Down", "Enter"])
        self.assertTrue(self.m["model_menu_needs_owner"])

    def test_menu_blocks_new_dispatch_without_attempt_or_budget(self):
        self.m.update(active_delivery=None, pending=False)
        before = len([c for c in self.cao.call_args_list if c.args[1].endswith("/input")])
        self.server.send_to(self.group, self.m, "queued after menu", 0)
        self.assertEqual(self.m["outbox"][0]["attempt"], 0)
        self.assertEqual(len([c for c in self.cao.call_args_list if c.args[1].endswith("/input")]), before)

    def test_last_mode_menu_fragment_is_never_posted_as_member_reply(self):
        self.raw = W + "• final\n" + D + "\n? for shortcuts"
        self.last = "2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back"
        self.status = "idle"
        self.tick(); self.tick()
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))
        self.assertTrue(self.m["model_menu_needs_owner"])
        self.assertEqual(self.keys, [])

    def test_owner_menu_hold_suppresses_watchdog_when_status_probe_fails(self):
        self.raw = "2. Keep current model\n3. Keep current model (never show again)\nenter select · esc back"
        self.tick()
        self.cao.side_effect = self.server.CaoError(503, "synthetic terminal unavailable")
        self.tick(1000)
        self.assertEqual(self.m["delivery_state"], "sent")
        self.assertTrue(self.m["pending"])
        self.assertFalse(any("可能未送达" in x["text"] for x in self.group["messages"]))

    def test_history_menu_above_real_answer_is_not_an_active_menu(self):
        self.raw = menu() + "\n" + W + "• answer\n" + D + "\ncodex>\n? for shortcuts"
        self.status = "idle"
        self.tick()
        self.assertEqual(self.keys, [])
        self.assertEqual(len([x for x in self.group["messages"] if x["kind"] == "agent"]), 1)

    def test_menu_with_enabled_quota_does_not_clear_exhausted_pause(self):
        self.group["automation"] = {"quota_enabled": True}
        self.group["members"] = [self.m]
        with patch.object(self.server, "codex_limits", return_value={"windows": [{"used_percent": 100, "resets_at": 2000}]}):
            self.tick(); self.tick(); self.tick()
        self.assertEqual(self.keys, ["Down", "Enter"])
        self.assertEqual(self.m["quota_state"], "exhausted")

    def test_menu_state_logs_and_notices_never_store_terminal_body(self):
        self.raw = "PRIVATE_SYNTHETIC_SCREEN\n" + menu()
        self.tick(); self.tick(); self.tick()
        self.assertNotIn("PRIVATE_SYNTHETIC", self.server.STATE_FILE.read_text())
        self.assertNotIn("PRIVATE_SYNTHETIC", self.log_text())
        self.assertNotIn("Switch to another", json.dumps(self.m, ensure_ascii=False))
