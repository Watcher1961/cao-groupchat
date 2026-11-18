"""Synthetic provider frames and size limits; never load private samples."""
import json
from urllib import parse
from unittest.mock import patch
from test_server import ServerTestCase


def hermes_frame(body, footer="─ 2.7s · read 1 file · 01:02 PM"):
    return "☤ Hermes\n\n" + body + "\n" + footer + "\n────────────\n❯ synthetic prompt\n────────────\n ☤ sample-model · 1/10 · [█░]\n"


def fallback(raw):
    return "[NO RESPONSE - agent completed without producing a text response (999 lines in buffer)]\n" + raw


class ReplySafetyTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.m = self.developer
        self.m.update(provider="hermes", status="idle", pending=False, outbox=[])
        self.outputs = {"full": "Ready\n> ", "last": ""}
        self.generation = 2
        self.status = "completed"
        self.sent = []
        self.cao.side_effect = self.response

    def response(self, method, path, query=None, **kwargs):
        if path.endswith("/output"):
            value = self.outputs[query["mode"]]
            if isinstance(value, Exception):
                raise value
            return {"output": value}
        if method == "GET":
            return {"status": self.status, "status_generation": self.generation,
                    "output_generation": self.generation}
        self.sent.append((path, query["message"]))
        return {"dispatch_boundary": self.generation}

    def dispatch(self, full="Ready\n> ", last=""):
        self.outputs.update(full=full, last=last)
        self.server.send_to(self.group, self.m, "synthetic task", 0)
        self.generation += 1

    def result(self, raw, last=None):
        self.outputs.update(full=raw, last=fallback(raw) if last is None else last)
        return self.server.get_reply(self.m)

    def test_hermes_fallback_extracts_answer_and_removes_redrawn_chrome(self):
        self.dispatch()
        body = ("合成结论。\n── ⠼ Working… · 3s ────────────\n─\n"
                "☤ ❯ synthetic input\n────────────\n ☤ sample-model · 1/10 · [█░]\n"
                "\x08⠏Working… · 3s · ↓ tokens ─\n@评审 请复核。\n状态：已交付")
        raw = "SYNTHETIC_BANNER\nPRIVATE_THINKING\n" + hermes_frame(body)
        self.assertEqual(self.result(raw), "合成结论。\n@评审 请复核。\n状态：已交付")
        self.assertEqual(self.m["_reply_work_state"], "已交付")

    def test_hermes_redraw_bar_can_end_with_stale_input_text(self):
        self.dispatch()
        body = ("第一段合成答案。\n── ⠹ Working… · 3s ─────PRIVATE_STALE_INPUT\n"
                "☤ ❯ synthetic prompt\n────────────\n ☤ sample-model · 1/10 · [█░]\n"
                "第二段合成答案。")
        answer = self.result(hermes_frame(body))
        self.assertEqual(answer, "第一段合成答案。\n第二段合成答案。")
        self.assertNotIn("PRIVATE_STALE_INPUT", answer)

    def test_hermes_prefers_rendered_last_over_fifo_cursor_residue(self):
        self.dispatch()
        full = hermes_frame("合成最终答案。\n1234状态：等群主")
        rendered = hermes_frame("合成最终答案。\n状态：等群主")
        self.assertEqual(self.result(full, fallback(rendered)), "合成最终答案。\n状态：等群主")
        self.assertEqual(self.m["_reply_work_state"], "等群主")

    def test_hermes_raw_only_backspace_output_is_not_treated_as_rendered_reply(self):
        self.dispatch(last=self.server.CaoError(422, "unsupported mode"))
        self.outputs.update(full=hermes_frame("合成正文。\n1234\x08状态：等群主"),
                            last=self.server.CaoError(422, "unsupported mode"))
        with self.assertRaises(self.server.ReplyExtractionError):
            self.server.get_reply(self.m)

    def test_hermes_new_round_excludes_old_frames_even_when_last_has_more_history(self):
        old = hermes_frame("旧轮 @评审 不应转发。")
        self.dispatch(full=old, last=fallback(old * 7))
        new = hermes_frame("本轮合成答案。", "─ 3.1s · 01:03 PM")
        self.assertEqual(self.result(new, fallback(old * 7 + new)), "本轮合成答案。")
        self.assertNotIn("☤ Hermes", self.server.STATE_FILE.read_text())

    def test_hermes_unchanged_old_fallback_is_not_a_new_reply(self):
        old = hermes_frame("旧轮 @评审 不应转发。")
        self.dispatch(full=old, last=fallback(old * 7))
        self.outputs.update(full=old, last=fallback(old * 7))
        self.server.poll_member(self.group, self.m)
        self.server.poll_member(self.group, self.m)
        agents = [x for x in self.group["messages"] if x["kind"] == "agent"]
        notices = [x for x in self.group["messages"] if x["kind"] == "system"]
        self.assertEqual(agents, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("未能", notices[0]["text"])
        self.assertLess(len(notices[0]["text"]), 180)
        self.assertEqual(len(self.sent), 1)

    def test_hermes_identical_answer_is_new_only_with_an_additional_frame(self):
        frame = hermes_frame("相同但属于新轮的答案。")
        self.dispatch(full=frame, last=fallback(frame))
        self.assertEqual(self.result(frame, fallback(frame + frame)), "相同但属于新轮的答案。")

    def test_hermes_working_without_new_footer_keeps_pending(self):
        old = hermes_frame("旧轮答案。")
        self.dispatch(full=old, last=fallback(old))
        raw = "☤ Hermes\n尚未完成\n── ⠼ Working… · 3s ────────────"
        self.outputs.update(full=raw, last=fallback(old + raw))
        self.server.poll_member(self.group, self.m)
        self.assertTrue(self.m["pending"])
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))

    def test_hermes_unframed_huge_fallback_only_posts_short_notice_once(self):
        self.dispatch()
        raw = "PRIVATE_SCREEN_BODY\n" * 20000 + "@评审 do not relay"
        self.outputs.update(full=raw[-32000:], last=fallback(raw))
        self.server.poll_member(self.group, self.m)
        self.server.poll_member(self.group, self.m)
        self.assertFalse(self.m["pending"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertEqual(self.group["messages"][0]["kind"], "system")
        stored = self.server.STATE_FILE.read_text()
        self.assertNotIn("PRIVATE_SCREEN_BODY", stored)
        self.assertNotIn("NO RESPONSE", stored)

    def test_hermes_framed_fallback_supports_last_unavailable(self):
        self.dispatch(last=self.server.CaoError(422, "unsupported mode"))
        raw = hermes_frame("仅 full 的新答案。")
        self.assertEqual(self.result(raw, self.server.CaoError(422, "unsupported mode")), "仅 full 的新答案。")

    def test_hermes_legacy_pending_without_snapshot_never_pastes_screen(self):
        self.m.update(pending=True, active_delivery=self.server.new_delivery("old task", 0),
                      boundary=1, dispatch_confirmed=True, sent_at=0, saw_busy=True)
        raw = hermes_frame("无法证明归属的旧答案 @评审")
        self.outputs.update(full=raw, last=fallback(raw))
        self.server.poll_member(self.group, self.m)
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))
        self.assertEqual(self.sent, [])

    def test_input_limit_counts_url_encoding_and_retains_original_job(self):
        text = "中文😀 +%\n" * 1200
        self.server.send_to(self.group, self.m, text, 2)
        wire = self.sent[-1][1]
        target = "/terminals/dev-terminal/input?" + parse.urlencode({"message": wire})
        self.assertLessEqual(len(target.encode("ascii")), 6000)
        self.assertIn("已截断", wire)
        self.assertIn(str(len(text)), wire)
        self.assertEqual(self.m["active_delivery"]["text"], text)
        self.assertEqual(self.m["hops"], 2)

    def test_input_limit_preserves_small_text_and_valid_unicode(self):
        for text in ("短消息😀 +%", "A" * 20000, "汉" * 20000, "😀" * 20000):
            with self.subTest(size=len(text)):
                query = self.server.bounded_input_query("/terminals/synthetic/input", {"message": text})
                target = "/terminals/synthetic/input?" + parse.urlencode(query)
                self.assertLessEqual(len(target.encode("ascii")), 6000)
                query["message"].encode("utf-8").decode("utf-8")
                if len(text) < 100:
                    self.assertEqual(query["message"], text)

    def test_failed_and_uncertain_retry_also_limits_the_original_text(self):
        for state in ("failed", "uncertain"):
            with self.subTest(state=state):
                text = "合成旧任务😀" * 1500
                job = self.server.new_delivery(text, 2)
                job["attempt"] = 1
                self.m.update(active_delivery=job, pending=state == "uncertain", delivery_state=state, ready_terminal=None)
                with patch.object(self.server, "member_ready", return_value=True):
                    self.server.retry_delivery(self.group, self.m, {"delivery_id": job["id"], "delivery_attempt": 1})
                wire = self.sent[-1][1]
                self.assertLessEqual(len(("/terminals/dev-terminal/input?" + parse.urlencode({"message": wire})).encode()), 6000)
                self.assertEqual(job["text"], text)
                self.assertEqual(job["attempt"], 2)

    def test_long_member_reply_is_capped_publicly_and_saved_in_private_state(self):
        text = "合成正文" * 4000 + "\n状态：已交付"
        self.m["provider"] = "claude_code"
        self.server.handle_reply(self.group, self.m, text)
        message = self.group["messages"][-1]
        self.assertLessEqual(len(message["text"]), 8000)
        self.assertIn("已截断", message["text"])
        self.assertEqual(message["full_text"], text)
        self.assertEqual(self.m["work_state"], "已交付")
        public = self.server.public_group(self.group)["messages"][-1]
        self.assertNotIn("full_text", public)
        self.assertEqual(json.loads(self.server.STATE_FILE.read_text())[self.group["id"]]["messages"][-1]["full_text"], text)

    def test_gemini_removes_multiline_input_tail_before_own_answer(self):
        self.m["provider"] = "antigravity_cli"
        task = "[来自 开发] 首行合成任务\n" + "SYNTHETIC_INPUT_BANNER " * 5 + "\n合成输入尾部。"
        self.server.send_to(self.group, self.m, task, 1)
        echo_tail = "SYNTHETIC_INPUT_BANNER " * 5 + "\n合成输入尾部。"
        self.outputs["last"] = echo_tail + "\n○ Thought for 1.2s (synthetic title)\n自己的回答。\n状态：已交付"
        self.assertEqual(self.server.get_reply(self.m), "自己的回答。\n状态：已交付")

    def test_gemini_input_middle_is_not_presented_as_own_answer(self):
        self.m["provider"] = "antigravity_cli"
        segment = "SYNTHETIC_INPUT_MIDDLE " * 5
        task = "synthetic beginning\n" + segment + "\nsynthetic ending"
        self.server.send_to(self.group, self.m, task, 0)
        self.generation += 1
        self.outputs["last"] = segment
        self.server.poll_member(self.group, self.m)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertEqual(self.group["messages"][0]["kind"], "system")
        self.assertEqual(len(self.sent), 1)

    def test_gemini_no_response_buffer_is_not_presented_as_own_answer(self):
        self.m["provider"] = "antigravity_cli"
        self.dispatch()
        self.outputs["last"] = fallback("PRIVATE_GEMINI_SCREEN\n" * 2000)
        self.server.poll_member(self.group, self.m)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertEqual(self.group["messages"][0]["kind"], "system")
        self.assertNotIn("PRIVATE_GEMINI_SCREEN", self.server.STATE_FILE.read_text())
        self.assertEqual(len(self.sent), 1)

    def test_gemini_echo_only_is_not_forwarded_as_reply(self):
        self.m["provider"] = "antigravity_cli"
        self.dispatch()
        self.outputs["last"] = "synthetic task"
        self.server.poll_member(self.group, self.m)
        self.assertEqual(len(self.group["messages"]), 1)
        self.assertEqual(self.group["messages"][0]["kind"], "system")
        self.assertEqual(len(self.sent), 1)
