"""Synthetic relay storms, queue provenance and owner priority."""
from unittest.mock import patch
from test_server import ServerTestCase


class RelayGuardTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.third = {"id": "third", "name": "合成丙", "terminal_id": "third-t", "ready_terminal": "third-t"}
        self.group["members"].append(self.third)

    def reply(self, member, text):
        member.update(reply_handled=False, silent=False, hops=0)
        self.server.handle_reply(self.group, member, text)

    def test_three_way_old_echo_status_pattern_stops_even_with_hops_reset(self):
        with patch.object(self.server, "send_to") as send:
            for i in range(30):
                member, text = ((self.third, "@评审 旧消息，没有新内容。"),
                                (self.reviewer, "@开发 收到，没有新内容。\n状态：等对方"),
                                (self.developer, "@合成丙 状态：等对方"))[i % 3]
                self.reply(member, text)
            self.assertEqual(send.call_count, 0)
        self.assertEqual(len([x for x in self.group["messages"] if x["kind"] == "agent"]), 30)

    def test_real_short_request_and_quoted_status_still_relay(self):
        with patch.object(self.server, "send_to") as send:
            self.reply(self.developer, "@评审 请检查合成实现。")
            self.reply(self.developer, "@评审 新证据：\n```\n状态：等对方\n```")
            self.assertEqual(send.call_count, 2)

    def test_group_burst_circuit_survives_restart_and_owner_message_resumes(self):
        with patch.object(self.server, "send_to") as send, patch.object(self.server.time, "time", return_value=1000):
            for i in range(20):
                self.reply(self.developer, f"@评审 新合成步骤 {i}")
            self.assertEqual(send.call_count, 11)
            self.assertTrue(self.group["relay_control"]["paused"])
            notices = [x for x in self.group["messages"] if x["kind"] == "system" and "自动转发" in x["text"]]
            self.assertEqual(len(notices), 1)
            self.assertIn("@群主", notices[0]["text"])
            self.server.save_state()
            self.server.load_state()
            restored = self.server.groups[self.group["id"]]
            self.assertTrue(restored["relay_control"]["paused"])
            self.group = restored
            with patch.object(self.server, "run_bg"):
                self.server.post_message(restored, "@开发 新的群主指令")
            self.assertFalse(restored["relay_control"]["paused"])
            self.assertEqual(restored["members"][0]["outbox"][0]["text"], "[群主] @开发 新的群主指令")

    def test_old_events_expire_and_silent_reply_does_not_count(self):
        with patch.object(self.server, "send_to") as send:
            for i in range(15):
                with patch.object(self.server.time, "time", return_value=1000 + i * 61):
                    self.reply(self.developer, f"@评审 step {i}")
            self.assertEqual(send.call_count, 15)
            self.assertFalse(self.group.get("relay_control", {}).get("paused", False))
            self.developer.update(reply_handled=False, silent=True)
            self.server.handle_reply(self.group, self.developer, "@评审 silent")
            self.assertLessEqual(len(self.group["relay_control"]["events"]), 1)

    def test_queue_coalesces_only_unsent_same_sender_and_owner_has_priority(self):
        m = self.reviewer
        m.update(pending=True, status="processing", outbox=[], active_delivery=self.server.new_delivery("in flight", 0))
        for text, sender in (("old", "dev"), ("other", "third"), ("latest", "dev")):
            self.server.send_to(self.group, m, "[来自 开发] " + text, 1, sender_id=sender)
        self.server.send_to(self.group, m, "[群主] owner one", 0)
        self.server.send_to(self.group, m, "[群主] owner two", 0)
        self.assertEqual([x["text"] for x in m["outbox"]], ["[群主] owner one", "[群主] owner two", "[来自 开发] other", "[来自 开发] latest"])
        self.assertEqual(m["outbox"][-1]["relay_merged"], 1)
        self.assertIn("合并了 1 条", self.server.delivery_text(m["outbox"][-1]))
        self.assertEqual(m["outbox"][-1]["attempt"], 0)

    def test_legacy_queue_compacts_before_dispatch_without_merging_owner_or_attempts(self):
        m = self.developer
        jobs = [self.server.new_delivery("[来自 评审] old", 1), self.server.new_delivery("[群主] keep", 0),
                self.server.new_delivery("[来自 评审] retry", 1), self.server.new_delivery("[来自 评审] latest", 2)]
        jobs[2]["attempt"] = 1
        m.update(outbox=jobs, pending=False, active_delivery=None)
        with patch.object(self.server, "dispatch_delivery", return_value=True) as dispatch:
            self.server.flush_outbox(self.group, m)
            self.assertEqual(dispatch.call_args.args[2]["text"], "[群主] keep")
        self.assertEqual([j["text"] for j in m["outbox"]], ["[来自 评审] retry", "[来自 评审] latest"])
        self.assertEqual(m["outbox"][-1]["relay_merged"], 1)
        self.assertEqual(jobs[2]["attempt"], 1)

    def test_paused_relay_queue_does_not_block_owner_or_consume_budget(self):
        m = self.developer
        m.update(provider="codex", outbox=[self.server.new_delivery("[来自 评审] queued", 1)], pending=False)
        self.group["relay_control"] = {"paused": True, "events": []}
        with patch.object(self.server, "dispatch_delivery", return_value=True) as dispatch:
            self.assertFalse(self.server.flush_outbox(self.group, m))
            dispatch.assert_not_called()
            self.server.send_to(self.group, m, "[群主] priority", 0)
            self.assertEqual(dispatch.call_args.args[2]["text"], "[群主] priority")
        self.assertEqual(m["outbox"][0]["text"], "[来自 评审] queued")

    def test_existing_interrupted_turn_continuation_stays_before_later_owner(self):
        m = self.developer
        continuation = self.server.new_delivery("fixed continuation", 0)
        continuation["quota_continuation"] = True
        m["outbox"] = [continuation, self.server.new_delivery("[群主] later", 0), self.server.new_delivery("[来自 评审] old", 1)]
        self.server.compact_outbox(self.group, m)
        self.assertIs(m["outbox"][0], continuation)
        self.assertEqual(m["outbox"][1]["text"], "[群主] later")

    def test_owner_is_durably_queued_before_background_delivery_or_circuit_resume(self):
        m = self.developer
        m.update(outbox=[self.server.new_delivery("[来自 评审] old", 1)], active_delivery=None, pending=False)
        self.group["relay_control"] = {"paused": True, "events": []}
        with patch.object(self.server, "run_bg"):
            self.server.post_message(self.group, "@开发 owner priority")
        self.assertEqual(m["outbox"][0]["text"], "[群主] @开发 owner priority")
        import json
        stored = json.loads(self.server.STATE_FILE.read_text())[self.group["id"]]
        self.assertEqual(stored["members"][0]["outbox"][0]["text"], "[群主] @开发 owner priority")
        with patch.object(self.server, "dispatch_delivery", return_value=True) as dispatch:
            self.server.flush_outbox(self.group, m)
            self.assertEqual(dispatch.call_args.args[2]["text"], "[群主] @开发 owner priority")

    def test_owner_arriving_during_readiness_replaces_stale_queue_reference(self):
        m = self.developer
        m.update(provider="claude_code", ready_terminal=None, pending=False, active_delivery=None,
                 outbox=[self.server.new_delivery("[来自 评审] old", 1)])
        self.cao.side_effect = None
        self.cao.return_value = {"status": "idle"}
        def ready(*args, **kwargs):
            with patch.object(self.server, "run_bg"):
                self.server.post_message(self.group, "@开发 priority during probe")
            return True
        with patch.object(self.server, "member_ready", side_effect=ready), patch.object(self.server, "dispatch_delivery", return_value=True) as dispatch:
            self.server.flush_outbox(self.group, m)
            self.assertEqual(dispatch.call_args.args[2]["text"], "[群主] @开发 priority during probe")
        self.assertEqual([job["text"] for job in m["outbox"]], ["[来自 评审] old"])

    def test_merging_is_idempotent_and_records_no_message_body_in_merge_log(self):
        m = self.reviewer
        m.update(pending=True, status="processing", outbox=[], active_delivery=self.server.new_delivery("in flight", 0))
        for i in range(10):
            self.server.send_to(self.group, m, "PRIVATE_SYNTHETIC_" + str(i), 1, sender_id="dev")
        self.assertEqual(len(m["outbox"]), 1)
        self.assertEqual(m["outbox"][0]["relay_merged"], 9)
        before = self.server.delivery_text(m["outbox"][0])
        self.server.compact_outbox(self.group, m)
        self.assertEqual(self.server.delivery_text(m["outbox"][0]), before)
        log = self.log_text()
        self.assertNotIn("PRIVATE_SYNTHETIC", log)
