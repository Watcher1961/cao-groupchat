"""Synthetic SGR composer placeholders; no private sample text."""
from unittest.mock import patch
from test_server import ServerTestCase
from test_reply_safety import hermes_frame, fallback


class ComposerTests(ServerTestCase):
    def screen(self, provider, value):
        self.developer.update(provider=provider, ready_terminal=None)
        self.developer.pop("ready_candidate", None)
        self.cao.side_effect = None
        self.cao.return_value = {"output": "────────────\n❯ " + value + "\n────────────"}

    def ready(self):
        for now in (1000, 1001):
            with patch.object(self.server.time, "time", return_value=now):
                value = self.server.member_ready(self.developer, {"status": "idle"})
        return value

    def test_styled_placeholders_are_empty_without_text_heuristics(self):
        for provider, style in (("hermes", "0;38;5;240;3"),
                                ("hermes", "38;5;240m\x1b[3"),
                                ("claude_code", "2"), ("claude_code", "2;90")):
            with self.subTest(provider=provider, style=style):
                self.screen(provider, "\x1b[" + style + "mSynthetic suggestion?\x1b[0m")
                self.assertTrue(self.ready())

    def test_actual_nonempty_input_including_try_is_not_ready(self):
        for provider in ("claude_code", "hermes"):
            for value in ('Try "synthetic command"', "typed input", "\x1b[38;5;240mtyped grey input\x1b[0m"):
                with self.subTest(provider=provider, value=value):
                    self.screen(provider, value)
                    self.assertFalse(self.ready())

    def test_cached_ready_never_bypasses_new_real_input(self):
        for provider in ("claude_code", "hermes"):
            with self.subTest(provider=provider):
                self.screen(provider, "")
                self.assertTrue(self.ready())
                self.cao.return_value = {"output": "────────────\n❯ typed now\n────────────"}
                self.assertFalse(self.server.member_ready(self.developer, {"status": "idle"}))
                self.assertNotEqual(self.developer.get("ready_terminal"), "dev-terminal")
                self.server.send_to(self.group, self.developer, "queued", 0)
                self.assertFalse(any(call.args[0] == "POST" for call in self.cao.call_args_list))
                self.developer.update(pending=False, outbox=[], active_delivery=None)
                self.cao.reset_mock()

    def test_styled_hint_does_not_hide_typed_suffix_or_previous_prompt(self):
        for provider in ("claude_code", "hermes"):
            for text in ("\x1b[2mHint\x1b[0m typed", "\n────────────\n❯ typed"):
                with self.subTest(provider=provider, text=text):
                    self.screen(provider, text)
                    self.assertFalse(self.ready())

    def test_hermes_completed_frame_recovers_from_processing_placeholder_status(self):
        m = self.developer
        m.update(provider="hermes", pending=False, outbox=[])
        raw = "Ready\n> "
        generation = 2
        sent = []
        def response(method, path, query=None, **kwargs):
            if path.endswith("/output"):
                return {"output": fallback(raw) if query["mode"] == "last" else raw}
            if method == "GET":
                return {"status": "idle" if generation == 2 else "processing", "status_generation": generation, "output_generation": generation}
            sent.append(query["message"])
            return {"dispatch_boundary": generation}
        self.cao.side_effect = response
        self.server.send_to(self.group, m, "first", 0)
        self.assertEqual(sent, ["first"])
        self.server.send_to(self.group, m, "second", 0)
        raw = hermes_frame("合成最终答复。\n状态：已交付")
        generation += 1
        m.update(delivery_state="uncertain", delivery_warned=True)
        self.server.poll_member(self.group, m)
        self.assertEqual([x["text"] for x in self.group["messages"] if x["kind"] == "agent"], ["合成最终答复。\n状态：已交付"])
        # Complete frame plus empty styled composer overrides the stale busy status.
        self.assertEqual(sent, ["first", "second"])
        self.assertEqual(m["active_delivery"]["text"], "second")
        self.server.poll_member(self.group, m)
        self.assertEqual(len([x for x in self.group["messages"] if x["kind"] == "agent"]), 1)

    def test_hermes_compound_elapsed_footer_with_placeholder_is_complete(self):
        for elapsed in ("7m4s", "2h7m4.5s", "7m 4s"):
            with self.subTest(elapsed=elapsed):
                raw = hermes_frame("合成最终答案。", "─ " + elapsed + " · read 2 files · 05:07 PM")
                blocks, working = self.server.hermes_blocks(raw)
                self.assertEqual(len(blocks), 1)
                self.assertFalse(working)
                self.assertTrue(self.server.hermes_idle(raw))

    def test_hermes_working_frame_stays_pending(self):
        m = self.developer
        m.update(provider="hermes", pending=True, delivery_state="uncertain", active_delivery=self.server.new_delivery("task", 0), boundary=1)
        self.cao.side_effect = lambda method, path, query=None, **kw: ({"output": "☤ Hermes\npartial\n── ⠼ Working… · 3s ──\n❯ \x1b[0;38;5;240;3mSynthetic hint?\x1b[0m"} if path.endswith("/output") else {"status": "processing", "status_generation": 2, "output_generation": 2})
        self.server.poll_member(self.group, m)
        self.assertTrue(m["pending"])
        self.assertFalse(any(x["kind"] == "agent" for x in self.group["messages"]))
