"""Per-group autonomy controls; clocks and CAO are isolated, no real providers."""

from concurrent.futures import ThreadPoolExecutor
import copy
import threading
from unittest.mock import patch

from test_server import ServerTestCase


THREAD_START = threading.Thread.start


class AutonomyTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        for member in self.group["members"]:
            member.update(provider="claude_code", status="idle", outbox=[], pending=False)
        self.inputs = []
        self.cao.side_effect = self.response

    def response(self, method, path, query=None, **kwargs):
        self.assertEqual(method, "POST")
        self.inputs.append((path, query["message"]))
        return {"dispatch_boundary": len(self.inputs)}

    def configure(self, **settings):
        return self.server.update_automation(self.group, settings)

    def finish(self, member=None, text="本轮完成"):
        self.server.handle_reply(self.group, member or self.developer, text)

    def test_legacy_defaults_are_read_only_and_delivery_stays_unlimited(self):
        before = copy.deepcopy(self.group)
        settings = self.server.automation_view(self.group)
        self.assertEqual(settings["settings"], {
            "relay_limit": 6, "delivery_budget": None, "idle_minutes": 0,
            "reminder_cooldown_minutes": 5, "reminder_limit": 3, "coordinator_id": None})
        self.assertEqual(self.group, before)
        self.server.check_idle_reminder(self.group, now=1000)
        self.server.check_idle_reminder(self.group, now=100000)
        self.assertEqual(self.group, before)
        for index in range(3):
            self.server.send_to(self.group, self.developer, str(index), 0)
            self.finish()
        self.assertEqual(len(self.inputs), 3)
        self.assertNotIn("automation", self.group)

    def test_settings_validate_atomically_and_are_scoped_to_one_group(self):
        self.configure(relay_limit=9, delivery_budget=2)
        before = copy.deepcopy(self.group)
        for settings in ({"relay_limit": -1}, {"relay_limit": True}, {"relay_limit": 1.5},
                         {"delivery_budget": "2"}, {"delivery_budget": -1},
                         {"idle_minutes": float("nan")}, {"idle_minutes": float("inf")},
                         {"idle_minutes": True}, {"idle_minutes": -1},
                         {"reminder_cooldown_minutes": 0}, {"reminder_limit": False},
                         {"coordinator_id": "missing"}, {"deliveries_used": 0}, []):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    self.server.update_automation(self.group, settings)
                self.assertEqual(self.group, before)
        other = {"id": "other", "members": [], "messages": []}
        self.assertEqual(self.server.automation_view(other)["settings"]["relay_limit"], 6)

    def test_relay_limit_is_per_group_including_zero_and_legacy_six(self):
        for limit, hops, forwarded in ((None, 6, False), (0, 0, False), (9, 6, True), (9, 9, False)):
            with self.subTest(limit=limit, hops=hops):
                if limit is not None:
                    self.configure(relay_limit=limit)
                self.developer.update(reply_handled=False, hops=hops)
                with patch.object(self.server, "send_to") as send:
                    self.finish(text="@评审 请接力")
                self.assertEqual(send.called, forwarded)

    def test_budget_pause_survives_time_and_resumes_fifo_when_raised(self):
        self.configure(delivery_budget=0)
        with patch.object(self.server.time, "time", return_value=1000):
            self.server.send_to(self.group, self.developer, "第一条", 0)
            self.server.send_to(self.group, self.developer, "第二条", 0)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.developer["delivery_state"], "budget_paused")
        with patch.object(self.server.time, "time", return_value=100000):
            self.server.flush_outbox(self.group, self.developer)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertIn("投递预算", self.group["messages"][0]["text"])
        self.configure(delivery_budget=2)
        self.server.flush_outbox(self.group, self.developer)
        self.finish()
        self.server.flush_outbox(self.group, self.developer)
        self.assertEqual([text for _, text in self.inputs], ["第一条", "第二条"])
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 2)

    def test_concurrent_broadcast_reserves_one_budget_slot_atomically(self):
        self.configure(delivery_budget=1)
        self.group["members"] = [dict(self.developer, id=f"m-{i}", name=f"成员{i}",
                                      terminal_id=f"t-{i}", ready_terminal=f"t-{i}", outbox=[])
                                 for i in range(6)]
        gate = threading.Barrier(6)
        def deliver(member):
            gate.wait(timeout=3)
            self.server.send_to(self.group, member, "广播", 0)
        with patch("threading.Thread.start", THREAD_START), patch.object(self.server, "save_state"):
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(deliver, self.group["members"]))
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(sum(len(m["outbox"]) for m in self.group["members"]), 5)
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 1)
        self.assertEqual(len(self.group["messages"]), 1)

    def test_budget_pause_before_cli_start_survives_new_message_after_increase(self):
        self.developer.update(terminal_id=None, ready_terminal=None)
        self.configure(delivery_budget=0)
        with patch.object(self.server.time, "time", return_value=1000):
            self.server.send_to(self.group, self.developer, "先排队", 0)
        self.configure(delivery_budget=2)
        with patch.object(self.server.time, "time", return_value=100000):
            self.server.send_to(self.group, self.developer, "新消息", 0)
        self.assertEqual(self.developer["delivery_state"], "waiting_ready")
        self.developer.update(terminal_id="dev-terminal", ready_terminal="dev-terminal")
        self.server.flush_outbox(self.group, self.developer)
        self.finish()
        self.server.flush_outbox(self.group, self.developer)
        self.assertEqual([text for _, text in self.inputs], ["先排队", "新消息"])

    def test_pre_input_persistence_failure_returns_reserved_budget(self):
        self.configure(delivery_budget=1)
        with patch.object(self.server, "save_state", side_effect=[None, OSError("disk"), None]):
            self.server.send_to(self.group, self.developer, "尚未 input", 0)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 0)

    def test_repeated_configuration_keeps_the_current_idle_observation(self):
        self.configure(idle_minutes=1)
        self.server.check_idle_reminder(self.group, now=1000)
        self.configure(idle_minutes=1)
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1060))

    def test_budget_does_not_charge_preflight_or_definite_rejection(self):
        self.configure(delivery_budget=1)
        self.cao.side_effect = self.server.CaoError(400, "rejected")
        self.server.send_to(self.group, self.developer, "拒绝", 0)
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 0)
        self.developer.update(active_delivery=None, pending=False, delivery_state="completed", provider="codex")
        self.cao.side_effect = self.server.CaoError(503, "baseline unavailable")
        self.server.send_to(self.group, self.developer, "基线失败", 0)
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 0)

    def test_uncertain_input_and_explicit_retry_consume_separate_slots(self):
        self.configure(delivery_budget=1)
        self.cao.side_effect = self.server.CaoError(0, "input response lost")
        self.server.send_to(self.group, self.developer, "原消息", 0)
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 1)
        job = self.developer["active_delivery"]
        self.server.retry_delivery(self.group, self.developer,
                                   {"delivery_id": job["id"], "delivery_attempt": job["attempt"]})
        self.assertEqual(self.developer["delivery_state"], "budget_paused")
        self.assertEqual(job["attempt"], 1)
        self.configure(delivery_budget=2)
        self.developer["ready_terminal"] = self.developer["terminal_id"]
        self.cao.side_effect = self.response
        self.server.flush_outbox(self.group, self.developer)
        self.assertEqual(job["attempt"], 2)
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 2)

    def test_state_marker_only_accepts_unquoted_final_line_outside_code(self):
        for state in ("进行中", "等对方", "等群主", "阻塞", "已交付"):
            with self.subTest(state=state):
                self.assertEqual(self.server.reply_work_state("正文\n状态：" + state + "\n"), state)
        for text in ("正文提到状态：等群主", "> 状态：等群主", "`状态：已交付`", "- 状态：阻塞",
                     "    状态：等群主", "```\n状态：阻塞\n```", "```python\n状态：阻塞",
                     "~~~\n状态：已交付", "状态：已交付\n后续正文", "状态：未知"):
            with self.subTest(text=text):
                self.assertEqual(self.server.reply_work_state(text), "进行中")
        self.finish(text="完成\n状态：等群主")
        self.assertEqual(self.developer["work_state"], "等群主")
        self.developer["reply_handled"] = False
        self.finish(text="下一轮没有标记")
        self.assertEqual(self.developer["work_state"], "进行中")

    def test_received_indentation_and_already_stripped_last_mode_limit(self):
        for raw, expected in (("    状态：等群主", "进行中"), ("> 状态：阻塞", "进行中"),
                              ("```\n状态：已交付", "进行中"),
                              ("状态：等群主", "等群主"),  # CAO last may already have stripped indentation.
                              ("正文\n状态：阻塞\n✻ Worked for 1s · done 01:01 AM", "阻塞")):
            with self.subTest(raw=raw):
                self.developer["reply_handled"] = False
                self.cao.side_effect = lambda *args, **kwargs: {"output": raw}
                self.finish(text=self.server.get_reply(self.developer))
                self.assertEqual(self.developer["work_state"], expected)

    def test_codex_real_bullet_and_two_space_continuation_states(self):
        for state in ("进行中", "等对方", "等群主", "阻塞", "已交付"):
            with self.subTest(state=state):
                raw = "• 已完成两条回复指令，等待群主安排下一步。\n\n  状态：" + state
                self.developer.update(provider="codex", reply_handled=False)
                self.cao.side_effect = lambda *args, **kwargs: {"output": raw}
                self.finish(text=self.server.get_reply(self.developer))
                self.assertEqual(self.developer["work_state"], state)
                self.assertEqual(self.server.reply_work_state("正文\n\n  状态：" + state), state)

    def test_codex_rendered_fences_quotes_and_relative_code_indent_stay_excluded(self):
        for raw in ("• 引用示例\n\n  > 状态：等群主", "• > 状态：阻塞",
                    "• ```text\n  状态：等群主\n  ```", "• ```text\n  状态：阻塞",
                    "\n• ```text\n  状态：等群主", "\n\n• ~~~\n  状态：已交付",
                    "• ~~~\n  状态：已交付", "• 代码示例\n\n      状态：等群主"):
            with self.subTest(raw=raw):
                self.assertEqual(self.server.reply_work_state(raw), "进行中")
        self.assertEqual(self.server.reply_work_state(
            "• 示例\n    ```\n    状态：进行中\n    ```\n\n  状态：已交付"), "已交付")

    def test_owner_mention_without_state_blocks_reminders_until_new_owner_message(self):
        self.configure(idle_minutes=1)
        self.finish(text="@群主 需要你决定范围")
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1000))
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1060))
        self.assertEqual(self.inputs, [])
        self.assertTrue(self.server.automation_view(self.group)["runtime"]["awaiting_owner"])
        self.finish(self.reviewer, text="仍在等决定\n状态：进行中")
        self.configure(delivery_budget=10)
        self.assertFalse(self.server.check_idle_reminder(self.group, now=2000))
        with self.assertRaises(ValueError):
            self.server.post_message(self.group, "   ")
        self.assertTrue(self.server.automation_view(self.group)["runtime"]["awaiting_owner"])
        with patch.object(self.server, "run_bg"):
            self.server.post_message(self.group, "继续当前范围")
        self.assertFalse(self.server.automation_view(self.group)["runtime"]["awaiting_owner"])
        self.assertFalse(self.server.check_idle_reminder(self.group, now=2001))
        self.assertTrue(self.server.check_idle_reminder(self.group, now=2061))

    def test_relay_limit_wait_survives_restart_and_increase_until_owner_resumes(self):
        self.configure(relay_limit=0, idle_minutes=1)
        self.finish(text="@评审 请审查")
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1000))
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1060))
        self.assertEqual(self.inputs, [])
        self.server.save_state()
        self.server.groups.clear()
        self.server.load_state()
        self.group = self.server.groups[self.group["id"]]
        self.assertTrue(self.server.automation_view(self.group)["runtime"]["awaiting_owner"])
        self.configure(relay_limit=10)
        self.assertFalse(self.server.check_idle_reminder(self.group, now=2000))
        with patch.object(self.server, "run_bg"):
            self.server.post_message(self.group, "开始新一轮审查")
        self.assertFalse(self.server.check_idle_reminder(self.group, now=2001))
        self.assertTrue(self.server.check_idle_reminder(self.group, now=2061))

    def test_silent_owner_mention_and_longer_name_do_not_block_idle_reminders(self):
        self.configure(idle_minutes=1)
        self.developer["silent"] = True
        self.finish(text="@群主 静默通知确认")
        self.finish(self.reviewer, text="@群主助手 不是群主")
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1000))
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1060))

    def test_reminder_interval_cooldown_limit_and_coordinator(self):
        self.configure(idle_minutes=1, reminder_cooldown_minutes=3, reminder_limit=2, coordinator_id="review")
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1000))
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1059))
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1060))
        self.assertEqual(self.inputs[0][0], "/terminals/review-terminal/input")
        self.assertIn("[自动提醒 idle:", self.inputs[0][1])
        self.assertEqual(self.group["messages"][0]["kind"], "system")
        self.finish(self.reviewer)
        self.server.check_idle_reminder(self.group, now=1061)
        self.assertFalse(self.server.check_idle_reminder(self.group, now=1121))
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1240))
        self.finish(self.reviewer)
        self.server.check_idle_reminder(self.group, now=1241)
        self.assertFalse(self.server.check_idle_reminder(self.group, now=100000))
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["reminders_sent"], 2)

    def test_reminder_exclusions_reset_continuous_idle_time(self):
        self.configure(idle_minutes=1)
        original = copy.deepcopy(self.reviewer)
        for excluded in ({"work_state": "等群主"}, {"work_state": "阻塞"}, {"work_state": "已交付"},
                         {"status": "waiting_user_answer"}, {"status": "processing"}, {"status": "starting"},
                         {"pending": True}, {"active_delivery": {"id": "active"}},
                         {"autonomy_poll_ok": False}, {"poll_error_type": "RuntimeError"},
                         {"outbox": [{"id": "queued"}]}):
            with self.subTest(excluded=excluded):
                self.reviewer.clear(); self.reviewer.update(copy.deepcopy(original))
                self.server.check_idle_reminder(self.group, now=1000)
                self.reviewer.update(excluded)
                self.assertFalse(self.server.check_idle_reminder(self.group, now=1060))
                self.reviewer.clear(); self.reviewer.update(copy.deepcopy(original))
                self.assertFalse(self.server.check_idle_reminder(self.group, now=1061))
                self.assertFalse(self.server.check_idle_reminder(self.group, now=1120))
        self.assertEqual(self.inputs, [])

    def test_reminder_counts_toward_budget_and_falls_back_to_available_member(self):
        self.configure(delivery_budget=1, idle_minutes=1, coordinator_id="review")
        self.reviewer.update(terminal_id=None, ready_terminal=None, status="gone")
        self.server.check_idle_reminder(self.group, now=1000)
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1060))
        self.assertEqual(self.inputs[0][0], "/terminals/dev-terminal/input")
        self.assertEqual(self.server.automation_view(self.group)["runtime"]["deliveries_used"], 1)
        self.finish()
        self.server.check_idle_reminder(self.group, now=2000)
        self.assertFalse(self.server.check_idle_reminder(self.group, now=2060))
        self.assertEqual(len(self.inputs), 1)

    def test_removed_coordinator_does_not_block_partial_updates_or_fallback(self):
        self.configure(idle_minutes=1, coordinator_id="review")
        self.group["members"].remove(self.reviewer)
        self.configure(delivery_budget=1)
        self.developer["work_state"] = "等对方"
        self.server.check_idle_reminder(self.group, now=1000)
        self.assertTrue(self.server.check_idle_reminder(self.group, now=1060))
        self.assertEqual(self.inputs[0][0], "/terminals/dev-terminal/input")

    def test_state_and_budget_survive_restart_but_idle_observation_restarts(self):
        self.configure(delivery_budget=1, idle_minutes=1)
        self.server.send_to(self.group, self.developer, "本轮", 0)
        self.finish(text="需要群主确认\n状态：等群主")
        self.group["automation_state"].update(idle_since=1000, reminders_sent=2, last_reminder_at=900)
        self.server.save_state()
        self.server.groups.clear()
        self.server.load_state()
        restored = self.server.groups[self.group["id"]]
        view = self.server.automation_view(restored)
        self.assertEqual(view["runtime"]["deliveries_used"], 1)
        self.assertEqual(view["runtime"]["reminders_sent"], 2)
        self.assertEqual(view["runtime"]["last_reminder_at"], 900)
        self.assertEqual(restored["members"][0]["work_state"], "等群主")
        self.assertIsNone(restored["automation_state"]["idle_since"])
