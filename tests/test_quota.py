"""Quota scheduling contracts; synthetic screens sourced in docs/TASKS.md."""
import copy
from unittest.mock import patch
from test_server import ServerTestCase

# Installed codex-cli 0.164.0 binary literal; Claude Code 2.1.326 Zn/ZTe.
CODEX_LIMIT = "■ You’ve hit your usage limit."
CLAUDE_LIMIT = "You've hit your session limit · resets 10pm (Asia/Singapore)"
# Labels/title from installed Codex; numbers are synthetic rendered menu inputs.
MENU = "Approaching rate limits\n  1. Switch to another model\n› 2. Keep current model\n  3. Keep current model (never show again)"

class QuotaTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.m = self.developer
        self.m.update(provider="claude_code", status="idle", pending=False, outbox=[])
        self.group["members"] = [self.m]
        self.inputs = []
        self.keys = []
        self.raw = "────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "idle"
        self.generation = 2
        self.cao.side_effect = self.response
        self.clock = patch.object(self.server.time, "time", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        limits_patch = patch.object(self.server, "codex_limits", return_value=None)
        self.limits = limits_patch.start()
        self.addCleanup(limits_patch.stop)

    def response(self, method, path, query=None, **kw):
        if path.endswith("/output"):
            return {"output": self.raw}
        if method == "GET":
            return {"status": self.status, "status_generation": self.generation, "output_generation": self.generation}
        if path.endswith("/key"):
            self.keys.append(query["key"])
            if query["key"] == "Enter":
                self.raw, self.status = "codex>\n? for shortcuts", "idle"
                self.generation += 1
        else:
            self.inputs.append(query["message"])
        return {"dispatch_boundary": self.generation, "success": True}

    def enable(self, **extra):
        self.server.update_automation(self.group, {"quota_enabled": True, **extra})

    def tick(self, seconds=0):
        self.now.return_value += seconds
        self.server.poll_member(self.group, self.m)

    def snapshot(self, percent, resets=1100):
        self.m["provider"] = "codex"
        self.raw = "codex>\n? for shortcuts"
        self.limits.return_value = {"windows": [{"used_percent": percent, "resets_at": resets}], "observed_at": "synthetic"}

    def test_old_data_and_missing_signals_stay_ok_without_reads(self):
        before = copy.deepcopy(self.group)
        view = self.server.public_group(self.group)
        self.assertEqual(view["members"][0]["quota_state"], "ok")
        self.assertIsNone(view["members"][0]["quota_resets_at"])
        self.assertEqual(self.group, before)
        self.tick()
        self.limits.assert_not_called()
        self.cao.assert_called_once()
        self.server.save_state()
        self.server.load_state()
        loaded = self.server.groups[self.group["id"]]
        self.assertNotIn("quota_enabled", loaded.get("automation", {}))
        self.assertEqual(self.server.public_group(loaded)["members"][0]["quota_state"], "ok")
        self.enable()
        self.tick()
        self.assertEqual(self.m.get("quota_state", "ok"), "ok")

    def test_settings_reject_invalid_values_atomically(self):
        self.enable()
        for body in ({"quota_enabled": 1}, {"quota_pause_percent": True}, {"quota_pause_percent": 101},
                     {"quota_pause_percent": -1}, {"quota_pause_percent": float("nan")},
                     {"quota_reset_grace_seconds": -1}, {"quota_probe_interval_seconds": 0},
                     {"quota_probe_max_failures": 0}, {"quota_probe_max_failures": True}):
            before = copy.deepcopy(self.group)
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.server.update_automation(self.group, body)
            self.assertEqual(self.group, before)

    def test_structured_exhausted_queue_fifo_budget_and_single_notice(self):
        self.enable(delivery_budget=3)
        self.snapshot(100)
        self.server.send_to(self.group, self.m, "first", 0)
        self.server.send_to(self.group, self.m, "second", 0)
        self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.assertEqual(self.m["quota_resets_at"], 1100)
        self.assertEqual(self.inputs, [])
        self.assertEqual([j["text"] for j in self.m["outbox"]], ["first", "second"])
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 0)
        self.assertEqual(sum("等额度" in x["text"] for x in self.group["messages"]), 1)
        self.snapshot(0, 2000)
        self.tick(219)
        self.assertEqual(self.inputs, [])
        self.tick(1)
        self.assertEqual(self.inputs, ["first"])
        self.server.handle_reply(self.group, self.m, "完成")
        self.tick()
        self.assertEqual(self.inputs, ["first", "second"])
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["quota_wait_seconds"]["dev"], 220)
        self.tick()
        self.assertEqual(self.inputs.count("first"), 1)

    def test_expired_or_malformed_windows_do_not_pause(self):
        self.enable()
        for percent, reset in ((100, 900), (True, 2000), (float("inf"), 2000), (100, None), ("100", 2000)):
            with self.subTest(percent=percent, reset=reset):
                self.snapshot(percent, reset)
                self.tick()
                self.assertEqual(self.m.get("quota_state", "ok"), "ok")

    def test_near_does_not_interrupt_active_turn(self):
        self.enable(quota_pause_percent=90)
        self.server.send_to(self.group, self.m, "active", 0)
        job = self.m["active_delivery"]
        self.snapshot(90, resets=2000)
        self.tick()
        self.assertEqual(self.m["quota_state"], "near")
        self.assertIs(self.m["active_delivery"], job)
        self.assertTrue(self.m["pending"])
        self.server.send_to(self.group, self.m, "next", 0)
        self.server.handle_reply(self.group, self.m, "完成")
        self.tick(500)
        self.assertEqual(self.inputs, ["active"])
        self.assertEqual(self.m["outbox"][0]["text"], "next")
        self.assertFalse(self.server.public_group(self.group)["members"][0]["can_retry"])

    def test_no_progress_warning_idle_reminder_or_budget_during_pause(self):
        self.enable(idle_minutes=0.01, delivery_budget=2)
        self.server.send_to(self.group, self.m, "active", 0)
        self.raw = CODEX_LIMIT
        self.m["provider"] = "codex"
        self.status = "error"
        self.tick()
        self.tick(10000)
        self.server.watch_delivery(self.group, self.m, {})
        self.assertFalse(self.server.check_idle_reminder(self.group, now=11000))
        state = self.server.automation_view(self.group)["runtime"]
        self.assertEqual(state["deliveries_used"], 1)
        self.assertEqual(state["reminders_sent"], 0)
        self.assertFalse(any("可能未送达" in x["text"] for x in self.group["messages"]))

    def test_unknown_reset_probe_intervals_failure_cap_and_owner_once(self):
        self.enable(quota_probe_interval_seconds=30, quota_probe_max_failures=2)
        self.m["provider"] = "codex"
        self.raw = CODEX_LIMIT
        self.status = "error"
        self.tick()
        self.tick(29)
        self.assertEqual(self.m["quota_probe_failures"], 0)
        self.tick(1)
        self.assertEqual(self.m["quota_probe_failures"], 1)
        self.tick(30)
        self.assertTrue(self.m["quota_needs_owner"])
        self.tick(300)
        self.assertEqual(sum("@群主" in x["text"] for x in self.group["messages"]), 1)
        self.assertEqual(self.m["quota_probe_failures"], 2)

    def test_unknown_reset_recovers_only_with_positive_evidence(self):
        self.enable(quota_probe_interval_seconds=30)
        self.m["provider"] = "codex"
        self.raw = CODEX_LIMIT
        self.status = "error"
        self.tick()
        self.raw = ""
        self.status = "idle"
        self.tick(30)
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.raw = "codex>\n? for shortcuts"
        self.generation += 1
        self.tick(30)
        self.assertEqual(self.m["quota_state"], "ok")

    def test_manual_mark_and_force_resume_bypass_old_signal(self):
        self.enable()
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.server.send_to(self.group, self.m, "first", 0)
        self.assertEqual(self.inputs, [])
        self.server.set_member_quota(self.group, self.m, {"action": "resume"})
        self.tick()
        self.assertEqual(self.inputs, ["first"])
        self.assertEqual(self.m["quota_state"], "ok")
        with self.assertRaises(ValueError):
            self.server.set_member_quota(self.group, self.m, {"action": "oops"})

    def test_claude_enabled_cache_and_spend_window(self):
        self.enable()
        self.group["automation"]["claude_limits"] = True
        with patch.object(self.server, "claude_limits", return_value={"windows": [], "spend_limit": {"used_percentage": 100, "resets_at": 1300}}):
            self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.assertEqual(self.m["quota_resets_at"], 1300)

    def interrupted(self):
        self.enable(delivery_budget=5, quota_probe_interval_seconds=10)
        self.server.send_to(self.group, self.m, "PRIVATE_ORIGINAL_TASK", 0)
        job = self.m["active_delivery"]
        self.server.send_to(self.group, self.m, "second", 0)
        self.raw = "You've hit your session limit"
        self.status = "error"
        self.generation += 1
        self.tick()
        return job

    def test_continuation_first_fixed_text_and_persistent_token(self):
        job = self.interrupted()
        self.assertEqual(self.m["quota_interrupted_delivery"], job["id"])
        self.raw = "────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "idle"
        self.generation += 1
        self.tick(10)
        self.tick(1)
        self.assertEqual(len(self.inputs), 2)
        self.assertTrue(self.inputs[1].startswith("[自动续接 quota:test-group:"))
        self.assertNotIn("PRIVATE_ORIGINAL_TASK", self.inputs[1])
        self.assertEqual(self.m["outbox"][0]["text"], "second")
        token = self.m["active_delivery"]["id"]
        self.server.save_state()
        self.server.load_state()
        self.m = self.server.groups[self.group["id"]]["members"][0]
        self.group = self.server.groups[self.group["id"]]
        self.tick()
        self.assertEqual(self.m["active_delivery"]["id"], token)
        self.assertEqual(len(self.inputs), 2)
        self.server.handle_reply(self.group, self.m, "续接完成")
        self.tick()
        self.assertEqual(self.inputs[-1], "second")
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 3)

    def test_continuation_respects_budget_and_queue_order(self):
        self.interrupted()
        self.server.update_automation(self.group, {"delivery_budget": 1})
        self.raw = "────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "idle"
        self.generation += 1
        self.tick(10)
        self.tick(1)
        self.assertEqual(len(self.inputs), 1)
        self.assertTrue(self.m["outbox"][0]["text"].startswith("[自动续接 quota:"))
        self.assertEqual(self.m["outbox"][1]["text"], "second")
        self.server.update_automation(self.group, {"delivery_budget": 3})
        self.tick()
        self.tick(1)  # The resumed continuation must still pass stable CLI readiness.
        self.assertEqual(len(self.inputs), 2)
        self.tick()
        self.assertEqual(len(self.inputs), 2)

    def test_completed_turn_never_gets_continuation(self):
        self.enable(quota_probe_interval_seconds=10)
        self.server.send_to(self.group, self.m, "active", 0)
        self.server.handle_reply(self.group, self.m, "完成")
        self.raw = "You've hit your session limit"
        self.status = "error"
        self.generation += 1
        self.tick()
        self.raw = "────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "idle"
        self.generation += 1
        self.tick(10)
        self.tick(1)
        self.assertEqual(self.inputs, ["active"])

    def test_disabled_group_rejects_manual_actions_and_never_reads_signals(self):
        with self.assertRaises(ValueError):
            self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.enable()
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.server.update_automation(self.group, {"quota_enabled": False})
        self.assertEqual(self.server.public_group(self.group)["members"][0]["quota_state"], "ok")
        self.server.send_to(self.group, self.m, "normal", 0)
        self.assertEqual(self.inputs, ["normal"])

    def test_same_terminal_limit_before_dispatch_does_not_prove_interruption(self):
        self.enable()
        self.raw = "You've hit your session limit"
        self.status = "error"
        self.tick()
        self.assertIsNone(self.m.get("quota_interrupted_delivery"))

    def test_codex_done_after_hard_screen_is_completion_not_interruption(self):
        raw = CODEX_LIMIT + "\n• 已完成" + chr(27) + "]7501;state=done:app=codex" + chr(27) + chr(92) + "\ncodex>\n? for shortcuts"
        self.assertIsNone(self.server.terminal_quota_signal("codex", raw, now=1000))

    def test_unchanged_terminal_clock_reset_does_not_slide_to_next_day(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.now.return_value = datetime(2026, 11, 17, 21, 59, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
        self.enable(quota_reset_grace_seconds=0, quota_probe_interval_seconds=30)
        self.raw = CLAUDE_LIMIT
        self.status = "error"
        self.tick()
        reset = self.m["quota_resets_at"]
        self.tick(61)
        self.assertEqual(self.m["quota_resets_at"], reset)
        self.assertEqual(self.m["quota_probe_failures"], 1)

    def test_fenced_terminal_examples_cannot_trigger_detection(self):
        for provider, text in (("codex", CODEX_LIMIT), ("claude_code", CLAUDE_LIMIT)):
            self.assertIsNone(self.server.terminal_quota_signal(provider, "```\n" + text + "\n```", 1000))
        self.assertEqual(self.server.codex_quota_menu("```\n" + MENU + "\n```"), (False, None))

    def test_exact_menu_blocks_dispatch_until_screen_changes(self):
        self.enable()
        self.m["provider"] = "codex"
        self.raw = MENU
        self.status = "waiting_user_answer"
        self.server.send_to(self.group, self.m, "queued", 0)
        self.assertEqual(self.inputs, [])
        self.tick()
        self.assertEqual(self.keys, ["Enter"])
        self.assertEqual(self.m["outbox"][0]["text"], "queued")

    def test_known_reset_old_hint_and_ready_prompt_can_recover_without_new_model_call(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.now.return_value = datetime(2026, 11, 17, 21, 59, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
        self.enable(quota_reset_grace_seconds=120)
        self.raw = CLAUDE_LIMIT + "\n────────────\n❯ \n────────────\n? for shortcuts"
        self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.tick(179)
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.tick(1)
        self.assertEqual(self.m["quota_state"], "ok")

    def clock_hint_case(self, provider, *, ready=True, kind="clock"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.now.return_value = datetime(2026, 11, 17, 21, 59, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
        self.m = {"id": "dev", "name": "开发", "terminal_id": "dev-terminal", "ready_terminal": "dev-terminal",
                  "provider": provider, "status": "idle", "pending": False, "outbox": []}
        self.group["members"] = [self.m]
        self.group["messages"] = []
        self.group.pop("automation_state", None)
        self.inputs = []
        self.generation = 2
        self.status = "idle" if ready else "error"
        self.hint = CLAUDE_LIMIT if provider == "claude_code" else CODEX_LIMIT + " Try again at 10:00 PM."
        if kind == "dated":
            self.hint = ("You've hit your session limit · resets Nov 17, 10pm (Asia/Singapore)"
                         if provider == "claude_code" else CODEX_LIMIT + " Try again at Nov 17, 2026 10:00 PM.")
        elif kind == "unknown":
            self.hint = "You've hit your session limit" if provider == "claude_code" else CODEX_LIMIT
        self.prompt = "────────────\n❯ \n────────────\n? for shortcuts" if provider == "claude_code" else "codex>\n? for shortcuts"
        self.raw = self.hint + ("\n" + self.prompt if ready else "")
        self.enable(quota_reset_grace_seconds=120, quota_probe_interval_seconds=30, quota_probe_max_failures=2)
        if provider == "codex":
            # Codex's time-only formatter uses the terminal's local timezone.
            from unittest.mock import patch
            import os, time
            zone_patch = patch.dict(os.environ, {"TZ": "Asia/Singapore"})
            zone_patch.start()
            time.tzset()
            self.addCleanup(lambda: (zone_patch.stop(), time.tzset()))
        self.tick()
        return self.now.return_value + 60

    def test_redrawn_clock_hint_recovers_at_original_reset_for_both_providers(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                reset = self.clock_hint_case(provider)
                self.assertEqual(self.m["quota_resets_at"], reset)
                self.generation += 1
                self.raw += "\nPRIVATE_REDRAW_FOOTER"
                self.tick(179)
                self.assertEqual(self.m["quota_resets_at"], reset)
                self.generation += 1
                self.tick(1)
                self.assertEqual(self.m["quota_state"], "ok")
                self.generation += 1
                self.tick(86400)
                self.assertEqual(self.m["quota_state"], "ok")
                self.assertEqual(self.server.automation_view(self.group)["runtime"]["quota_wait_seconds"]["dev"], 180)
                self.assertNotIn("PRIVATE_REDRAW_FOOTER", self.server.STATE_FILE.read_text())
                self.assertNotIn(self.hint, self.server.STATE_FILE.read_text())
                self.assertNotIn("PRIVATE_REDRAW_FOOTER", self.log_text())

    def test_redrawn_clock_hint_without_prompt_counts_failures_and_escalates(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                reset = self.clock_hint_case(provider, ready=False)
                self.generation += 1
                self.tick(180)
                self.assertEqual(self.m["quota_resets_at"], reset)
                self.assertEqual(self.m["quota_probe_failures"], 1)
                self.generation += 1
                self.tick(86400)
                self.assertEqual(self.m["quota_resets_at"], reset)
                self.assertEqual(self.m["quota_probe_failures"], 2)
                self.assertTrue(self.m["quota_needs_owner"])
                self.assertEqual(sum("@群主" in item["text"] for item in self.group["messages"]), 1)

    def test_redrawn_clock_hint_keeps_anchor_across_restart(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.clock_hint_case(provider)
                self.server.save_state()
                self.server.load_state()
                self.group = self.server.groups[self.group["id"]]
                self.m = self.group["members"][0]
                self.generation += 1
                self.tick(180)
                self.assertEqual(self.m["quota_state"], "ok")

    def test_changed_clock_hint_is_a_new_blocking_signal(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                reset = self.clock_hint_case(provider)
                self.raw = self.raw.replace("10pm", "11pm").replace("10:00 PM", "11:00 PM")
                self.generation += 1
                self.tick(180)
                self.assertEqual(self.m["quota_state"], "exhausted")
                self.assertEqual(self.m["quota_resets_at"], reset + 3600)

    def test_same_clock_hint_after_new_input_is_a_new_rejection(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                reset = self.clock_hint_case(provider)
                self.generation += 1
                self.tick(180)
                self.assertEqual(self.m["quota_state"], "ok")
                self.raw = self.prompt
                self.generation += 1
                self.server.send_to(self.group, self.m, "new task", 0)
                self.assertEqual(self.inputs, ["new task"])
                self.raw = self.hint + "\n" + self.prompt
                self.generation += 1
                self.tick(86400 - 180)
                self.assertEqual(self.m["quota_state"], "exhausted")
                self.assertEqual(self.m["quota_resets_at"], reset + 86400)

    def test_legacy_paused_clock_hint_without_fingerprint_can_recover(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.clock_hint_case(provider)
                self.m.pop("quota_terminal_hint_id", None)
                self.m.pop("quota_terminal_hint_reset", None)
                self.server.save_state()
                self.server.load_state()
                self.group = self.server.groups[self.group["id"]]
                self.m = self.group["members"][0]
                self.generation += 1
                self.tick(180)
                self.assertEqual(self.m["quota_state"], "ok")

    def recovered_hint_history(self, provider, kind="clock"):
        reset = self.clock_hint_case(provider, kind=kind)
        if kind == "unknown":
            self.server.set_member_quota(self.group, self.m, {"action": "resume"})
        else:
            self.generation += 1
            self.tick(180)
        self.assertEqual(self.m["quota_state"], "ok")
        return reset

    def append_normal_work(self, provider):
        self.raw += "\n> new task\nPRIVATE_NORMAL_WORKING"
        if provider == "codex":
            self.raw += chr(27) + "]7501;state=working:app=codex" + chr(27) + chr(92)
        self.status = "processing"
        self.generation += 1
        self.tick(5)

    def test_old_quota_history_does_not_interrupt_new_work(self):
        for provider in ("claude_code", "codex"):
            for kind in ("clock", "dated", "unknown"):
                with self.subTest(provider=provider, kind=kind):
                    self.recovered_hint_history(provider, kind)
                    self.server.send_to(self.group, self.m, "new task", 0)
                    job = self.m["active_delivery"]
                    self.append_normal_work(provider)
                    self.assertEqual(self.m["quota_state"], "ok")
                    self.assertIs(self.m["active_delivery"], job)
                    self.assertTrue(self.m["pending"])
                    self.assertIsNone(self.m.get("quota_interrupted_delivery"))
                    self.assertEqual(self.inputs, ["new task"])
                    self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 1)
                    self.server.save_state()
                    self.assertNotIn("PRIVATE_NORMAL_WORKING", self.server.STATE_FILE.read_text())
                    self.assertNotIn(self.hint, self.server.STATE_FILE.read_text())

    def test_same_hint_at_new_position_after_input_is_a_new_rejection(self):
        for provider in ("claude_code", "codex"):
            for kind in ("clock", "dated", "unknown"):
                with self.subTest(provider=provider, kind=kind):
                    reset = self.recovered_hint_history(provider, kind)
                    self.server.send_to(self.group, self.m, "new task", 0)
                    job = self.m["active_delivery"]
                    self.append_normal_work(provider)
                    self.assertEqual(self.m["quota_state"], "ok")
                    self.raw += "\n" + self.hint + "\n" + self.prompt
                    self.status = "error"
                    self.generation += 1
                    self.tick(1)
                    self.assertEqual(self.m["quota_state"], "exhausted")
                    self.assertEqual(self.m["quota_interrupted_delivery"], job["id"])
                    self.assertEqual(self.m["quota_resets_at"], reset + 86400 if kind == "clock" else None)
                    anchored = self.m["quota_resets_at"]
                    self.generation += 1
                    self.tick(86400)
                    self.assertEqual(self.m["quota_resets_at"], anchored)
                    self.assertEqual(self.m["quota_probe_failures"], 1)
                    self.assertEqual(self.inputs, ["new task"])

    def test_old_hint_boundary_survives_restart_during_new_work(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.recovered_hint_history(provider)
                self.server.send_to(self.group, self.m, "new task", 0)
                self.append_normal_work(provider)
                self.server.save_state()
                self.server.load_state()
                self.group = self.server.groups[self.group["id"]]
                self.m = self.group["members"][0]
                self.generation += 1
                self.tick(5)
                self.assertEqual(self.m["quota_state"], "ok")
                self.assertIsNone(self.m.get("quota_interrupted_delivery"))
                self.assertEqual(self.inputs, ["new task"])

    def test_old_hint_with_trimmed_prefix_stays_history_until_another_copy(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.recovered_hint_history(provider)
                history = self.raw
                self.raw = "earlier output\n" * 60 + history
                self.server.send_to(self.group, self.m, "new task", 0)
                self.raw = history  # The rolling buffer dropped the unrelated prefix.
                self.append_normal_work(provider)
                self.assertEqual(self.m["quota_state"], "ok")
                self.raw += "\n" + self.hint
                self.status = "error"
                self.generation += 1
                self.tick(1)
                self.assertEqual(self.m["quota_state"], "exhausted")

    def test_same_hint_reappearing_after_observed_clear_is_new(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.recovered_hint_history(provider)
                self.server.send_to(self.group, self.m, "new task", 0)
                self.raw = self.prompt
                self.status = "processing"
                self.generation += 1
                self.tick(1)
                self.assertEqual(self.m["quota_state"], "ok")
                self.raw += "\n" + self.hint
                self.status = "error"
                self.generation += 1
                self.tick(1)
                self.assertEqual(self.m["quota_state"], "exhausted")
                self.assertEqual(self.m["quota_interrupted_delivery"], self.m["active_delivery"]["id"])

    def test_missing_dispatch_hint_boundary_cannot_confirm_interruption(self):
        for provider in ("claude_code", "codex"):
            with self.subTest(provider=provider):
                self.clock_hint_case(provider)
                self.server.update_automation(self.group, {"quota_enabled": False})
                self.raw = self.prompt
                self.server.send_to(self.group, self.m, "legacy task", 0)
                self.assertNotIn("quota_dispatch_delivery", self.m)
                self.server.update_automation(self.group, {"quota_enabled": True})
                self.raw += "\n" + self.hint
                self.status = "error"
                self.generation += 1
                self.tick(1)
                self.assertEqual(self.m["quota_state"], "exhausted")
                self.assertIsNone(self.m.get("quota_interrupted_delivery"))
                self.assertEqual(self.inputs, ["legacy task"])

    def test_empty_idle_after_hard_limit_does_not_complete_interrupted_job(self):
        job = self.interrupted()
        self.raw = "────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "idle"
        self.generation += 1
        self.tick(1)  # Before the next allowance probe.
        self.assertIs(self.m["active_delivery"], job)
        self.assertTrue(self.m["pending"])
        self.assertFalse(any(item["kind"] == "agent" for item in self.group["messages"]))

    def test_structured_exhaustion_is_not_hidden_by_precise_menu(self):
        self.enable()
        self.snapshot(100)
        self.raw = MENU
        self.status = "waiting_user_answer"
        self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.assertEqual(self.keys, [])
        self.tick(); self.tick()
        self.assertEqual(self.keys, ["Enter"])

    def test_codex_error_followed_by_done_is_still_exhaustion(self):
        raw = CODEX_LIMIT + "" + chr(27) + "]7501;state=done:app=codex" + chr(27) + chr(92) + "\ncodex>\n? for shortcuts"
        self.assertEqual(self.server.terminal_quota_signal("codex", raw, 1000)["state"], "exhausted")

    def test_manual_resume_ignores_same_structured_snapshot_until_it_changes(self):
        self.enable()
        self.snapshot(100)
        self.tick()
        self.server.set_member_quota(self.group, self.m, {"action": "resume"})
        self.generation += 1
        self.tick()
        self.assertEqual(self.m["quota_state"], "ok")
        self.snapshot(100, resets=1400)
        self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")

    def test_late_completed_claude_reply_cancels_pending_continuation(self):
        self.interrupted()
        self.raw = "已完成\n────────────\n❯ \n────────────\n? for shortcuts"
        self.status = "completed"
        self.generation += 1
        self.tick(1)
        self.assertIsNone(self.m["active_delivery"])
        self.tick(9)
        self.assertFalse(any(text.startswith("[自动续接") for text in self.inputs))

    def test_disable_failure_rolls_back_member_wait_state(self):
        self.enable()
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.server.send_to(self.group, self.m, "queued", 0)
        before = copy.deepcopy(self.group)
        with patch.object(self.server, "save_state", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.server.update_automation(self.group, {"quota_enabled": False})
        self.assertEqual(self.group, before)

    def test_disable_then_reenable_does_not_keep_force_override(self):
        self.enable()
        self.snapshot(100)
        self.tick()
        self.server.set_member_quota(self.group, self.m, {"action": "resume"})
        self.server.update_automation(self.group, {"quota_enabled": False})
        self.server.update_automation(self.group, {"quota_enabled": True})
        self.tick()
        self.assertEqual(self.m["quota_state"], "exhausted")

    def test_manual_unknown_mark_needs_new_positive_evidence(self):
        self.enable(quota_probe_interval_seconds=10)
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.tick(10)
        self.assertEqual(self.m["quota_state"], "exhausted")
        self.assertEqual(self.m["quota_probe_failures"], 1)

    def test_manual_force_resume_does_not_require_successful_observation(self):
        self.enable()
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        with patch.object(self.server, "cao", side_effect=self.server.CaoError(503, "PRIVATE_ERROR_TEXT")):
            self.server.set_member_quota(self.group, self.m, {"action": "resume"})
        self.assertEqual(self.m["quota_state"], "ok")
        self.assertNotIn("PRIVATE_ERROR_TEXT", self.log_text())

    def test_menu_history_in_idle_screen_is_not_a_live_control(self):
        self.enable()
        self.m["provider"] = "codex"
        self.raw = MENU + "\ncodex>\n? for shortcuts"
        self.status = "idle"
        self.server.send_to(self.group, self.m, "normal", 0)
        self.assertEqual(self.keys, [])
        self.assertEqual(self.inputs, ["normal"])

    def test_terminal_reset_formats_from_installed_binaries(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 11, 17, 18, 0, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
        samples = (("claude_code", "You've hit your session limit · resets 10pm (Asia/Singapore)",
                    datetime(2026, 11, 17, 22, 0, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()),
                   ("claude_code", "You've hit your weekly limit · resets Nov 20, 2:30pm (Asia/Singapore)",
                    datetime(2026, 11, 20, 14, 30, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()),
                   ("codex", "■ You’ve hit your usage limit. Try again at Nov 20, 2026 2:30 PM.",
                    datetime(2026, 11, 20, 14, 30).timestamp()))
        for provider, raw, expected in samples:
            with self.subTest(provider=provider, raw=raw):
                self.assertEqual(self.server.terminal_quota_signal(provider, raw, now)["resets_at"], expected)
        self.assertIsNone(self.server.terminal_quota_signal("claude_code", "You've hit your session limit · resets someday", now)["resets_at"])

    def test_menu_rejects_missing_duplicate_or_un_numbered_keep_option(self):
        for text in (MENU.replace("2. Keep current model", "Keep current model"),
                     MENU + "\n2. Other choice", MENU + "\n4. Keep current model"):
            with self.subTest(text=text):
                self.assertEqual(self.server.codex_quota_menu(text), (True, None))
        self.assertEqual(self.server.codex_quota_menu("Example: " + MENU), (False, None))
        self.assertEqual(self.server.codex_quota_menu(MENU), (True, "2"))

    def test_terminal_samples_and_unknown_providers(self):
        for provider, raw in (("codex", CODEX_LIMIT), ("claude_code", CLAUDE_LIMIT)):
            with self.subTest(provider=provider):
                signal = self.server.terminal_quota_signal(provider, raw, now=1000)
                self.assertEqual(signal["state"], "exhausted")
                self.assertEqual(signal["source"], "terminal")
        for provider in ("hermes", "antigravity_cli", "kiro_cli"):
            self.assertIsNone(self.server.terminal_quota_signal(provider, CODEX_LIMIT, now=1000))
        self.assertIsNone(self.server.terminal_quota_signal("codex", "• Example: " + CODEX_LIMIT, now=1000))

    def test_menu_exact_numbered_option_and_uncertain_never_sends_keys(self):
        self.enable()
        self.m["provider"] = "codex"
        self.raw = MENU
        self.status = "waiting_user_answer"
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.keys, ["Enter"])
        self.assertIn("model_menu_action=keep_current_verified", self.log_text())
        self.raw = MENU.replace("2. Keep current model", "2. Keep the current model")
        self.generation += 1
        self.tick()
        self.assertEqual(self.keys, ["Enter"])
        self.assertTrue(self.m["model_menu_needs_owner"])
        self.assertTrue(any("@群主" in x["text"] for x in self.group["messages"]))

    def test_privacy_no_terminal_text_in_state_messages_or_log(self):
        self.enable()
        self.m["provider"] = "codex"
        self.status = "error"
        self.raw = "PRIVATE_SYNTHETIC_BODY\n" + CODEX_LIMIT
        self.tick()
        stored = self.server.STATE_FILE.read_text()
        self.assertNotIn("PRIVATE_SYNTHETIC_BODY", stored)
        self.assertNotIn(CODEX_LIMIT, stored)
        self.assertNotIn("PRIVATE_SYNTHETIC_BODY", self.log_text())

    def test_restart_preserves_wait_and_does_not_retry_active(self):
        self.enable()
        self.server.set_member_quota(self.group, self.m, {"action": "pause"})
        self.server.send_to(self.group, self.m, "queued", 0)
        self.server.save_state()
        self.server.load_state()
        group = self.server.groups[self.group["id"]]
        m = group["members"][0]
        self.assertEqual(m["quota_state"], "exhausted")
        self.assertEqual(m["outbox"][0]["text"], "queued")
        self.server.poll_member(group, m)
        self.assertEqual(self.inputs, [])
