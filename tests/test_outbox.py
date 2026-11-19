"""Queue clearing keeps current delivery and owner provenance intact."""
import copy
from unittest.mock import patch
from test_server import ServerTestCase


class OutboxTests(ServerTestCase):
    def prepare(self, state="uncertain"):
        m = self.developer
        active = self.server.new_delivery("PRIVATE_ACTIVE", 0)
        active["attempt"] = 3
        queue = [self.server.new_delivery("[来自 评审] PRIVATE_RELAY", 1),
                 self.server.new_delivery("[群主] PRIVATE_OWNER", 0),
                 self.server.new_delivery("[来自 评审] PRIVATE_OTHER", 1)]
        m.update(active_delivery=active, outbox=queue, delivery_state=state, pending=state == "uncertain")
        return m, active, queue

    def test_default_keeps_owner_and_active_for_all_delivery_states(self):
        for state in ("sent", "failed", "uncertain", "not_ready"):
            with self.subTest(state=state):
                m, active, queue = self.prepare(state)
                snapshot = copy.deepcopy(active)
                before = (m["pending"], m["delivery_state"])
                result = self.server.clear_outbox(self.group, m, {})
                self.assertEqual(result["cleared"], 2)
                self.assertEqual(result["retained"], 1)
                self.assertIs(m["outbox"][0], queue[1])
                self.assertEqual(m["active_delivery"], snapshot)
                self.assertEqual((m["pending"], m["delivery_state"]), before)
        self.cao.assert_not_called()

    def test_explicit_clear_all_and_repeated_clear_are_idempotent(self):
        m, active, queue = self.prepare("not_ready")
        m.update(active_delivery=None, pending=False)
        self.assertEqual(self.server.clear_outbox(self.group, m, {"keep_owner": False})["cleared"], 3)
        self.assertEqual(self.server.clear_outbox(self.group, m, {"keep_owner": False})["cleared"], 0)
        self.assertFalse(self.server.public_group(self.group)["members"][0]["can_retry"])
        self.assertEqual(m["delivery_state"], "completed")

    def test_invalid_keep_owner_does_not_mutate_queue(self):
        m, active, queue = self.prepare()
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.server.clear_outbox(self.group, m, {"keep_owner": value})
                self.assertEqual(m["outbox"], queue)

    def test_log_records_counts_without_bodies(self):
        m, active, queue = self.prepare()
        self.server.clear_outbox(self.group, m, {})
        log = self.log_text()
        self.assertIn("cleared=2", log)
        self.assertIn("retained=1", log)
        self.assertNotIn("PRIVATE_", log)

    def test_active_retry_token_stays_valid_after_clearing_queue(self):
        m, active, queue = self.prepare()
        token = {"delivery_id": active["id"], "delivery_attempt": active["attempt"]}
        self.server.clear_outbox(self.group, m, {"keep_owner": False})
        self.cao.side_effect = None
        self.cao.return_value = {"status": "idle", "dispatch_boundary": 4}
        with patch.object(self.server, "member_ready", return_value=True):
            self.server.retry_delivery(self.group, m, token)
        self.assertEqual(m["active_delivery"]["id"], token["delivery_id"])
        self.assertEqual(m["active_delivery"]["attempt"], 4)
        with self.assertRaises(ValueError):
            self.server.retry_delivery(self.group, m, token)
