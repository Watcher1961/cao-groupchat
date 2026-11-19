"""Synthetic VT screen redraws; never read private terminal samples."""
from unittest.mock import patch
from test_server import ServerTestCase
from test_reply_safety import hermes_frame

ESC = "\x1b["
RAIL = "─" * 32


def box(value="", row=5, final="H"):
    return (ESC + "2J" + ESC + f"{row};1H" + RAIL +
            ESC + f"{row+1};1{final}" + "❯\xa0" + value +
            ESC + f"{row+2};1H" + RAIL)


class TerminalScreenTests(ServerTestCase):
    def ready(self, raw, provider="claude_code"):
        m = self.developer
        m.update(provider=provider, ready_terminal=None)
        m.pop("ready_candidate", None)
        self.cao.side_effect = None
        self.cao.return_value = {"output": raw}
        for now in (1000, 1001):
            with patch.object(self.server.time, "time", return_value=now):
                result = self.server.member_ready(m, {"status": "idle"})
        return result

    def test_cold_start_animation_below_raw_tail_does_not_hide_composer(self):
        raw = box(ESC + "2mSynthetic forecast" + ESC + "22m")
        raw += "".join(ESC + "1;1H" + ESC + "2KLOGO FRAME " + str(i) + "\r\n" for i in range(30))
        self.assertTrue(self.ready(raw))

    def test_cursor_addressed_prediction_and_hermes_hint_are_empty(self):
        for provider, style in (("claude_code", "2"), ("hermes", "0;38;5;240;3")):
            for position in ("H", "f"):
                with self.subTest(provider=provider, position=position):
                    raw = box(final=position) + ESC + "6;3" + position + ESC + style + "mSynthetic forecast" + ESC + "0m" + ESC + "9;1H"
                    self.assertTrue(self.ready(raw, provider))

    def test_prediction_only_redraw_without_border_uses_rendered_empty_prompt(self):
        raw = ESC + "48;1H❯\xa0" + ESC + "50;1H" + ESC + "H" + ESC + "47B" + ESC + "2C" + ESC + "2mSynthetic forecast" + ESC + "22m" + ESC + "50;1H"
        self.assertTrue(self.ready(raw))

    def test_same_prediction_text_as_real_input_stays_nonempty(self):
        for provider in ("claude_code", "hermes"):
            for value in ("Synthetic forecast", "真实合成输入", ESC + "38;5;240mGrey real input" + ESC + "39m"):
                with self.subTest(provider=provider, value=value):
                    raw = box() + ESC + "6;3H" + value
                    self.assertFalse(self.ready(raw, provider))

    def test_real_cell_overwrites_dim_prediction_without_inheriting_its_style(self):
        for provider, style in (("claude_code", "2"), ("hermes", "38;5;240;3")):
            with self.subTest(provider=provider):
                raw = box() + ESC + "6;3H" + ESC + style + "mForecast" + ESC + "0m" + ESC + "6;3HX"
                self.assertFalse(self.ready(raw, provider))

    def test_erase_line_repaint_removes_old_real_input(self):
        for code in ("K", "0K", "1K", "2K"):
            with self.subTest(code=code):
                raw = box("real input") + ESC + "6;1H" + ESC + "2K" + "❯\xa0" + ESC + "6;3H" + ESC + "2mHint" + ESC + "22m"
                if code != "2K":
                    raw += ESC + "6;3H" + ESC + code + ESC + "6;1H❯\xa0"
                self.assertTrue(self.ready(raw))

    def test_cr_lf_backspace_and_relative_cursor_moves_update_cells(self):
        cases = (("abc\rZ\nQ", "Zbc\n Q"), ("abc\bZ", "abZ"),
                 ("abc" + ESC + "2DXY", "aXY"),
                 ("abc" + ESC + "1GZ", "Zbc"),
                 ("abc\r\nDEF" + ESC + "A" + ESC + "2CZ", "abc  Z\nDEF"),
                 ("abc" + ESC + "2B" + ESC + "3DZ", "abc\n\nZ"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.server.composer_text(raw, "hermes"), expected)

    def test_erase_in_line_and_display_modes(self):
        for mode, expected in ((0, "abc"), (1, "    ef"), (2, "")):
            with self.subTest(kind="K", mode=mode):
                self.assertEqual(self.server.composer_text("abcdef" + ESC + "1;4H" + ESC + str(mode) + "K", "hermes"), expected)
        initial = "abc\r\nDEF\r\nghi" + ESC + "2;2H"
        for mode, expected in ((0, "abc\nD"), (1, "\n  F\nghi"), (2, ""), (3, "")):
            with self.subTest(kind="J", mode=mode):
                self.assertEqual(self.server.composer_text(initial + ESC + str(mode) + "J", "hermes"), expected)

    def test_saved_cursor_and_ignored_queries_do_not_relocate_input(self):
        raw = box() + ESC + "6;3H" + ESC + "s" + ESC + "1;1HHEADER" + ESC + "?u" + ESC + "u" + "X"
        self.assertFalse(self.ready(raw))
        raw = box() + ESC + "6;3H" + "\x1b7" + ESC + "1;1HHEADER" + "\x1b8X"
        self.assertFalse(self.ready(raw))

    def test_wide_and_combining_real_input_remains_visible(self):
        for text in ("汉字", "😀", "a\u0301"):
            with self.subTest(text=text):
                raw = box() + ESC + "6;3H" + ESC + "2m" + text + ESC + "22m" + ESC + "6;3HX"
                self.assertFalse(self.ready(raw))
        self.assertEqual(self.server.composer_text("a\u0301\bZ", "hermes"), "Z")

    def test_multiline_true_input_is_not_hidden_by_empty_prompt(self):
        for provider in ("claude_code", "hermes"):
            with self.subTest(provider=provider):
                raw = ESC + "2J" + ESC + "5;1H" + RAIL + ESC + "6;1H❯ " + ESC + "7;3Hreal continuation" + ESC + "8;1H" + RAIL
                self.assertFalse(self.ready(raw, provider))

    def test_outside_animation_does_not_restart_initial_ready_stability(self):
        self.developer.update(provider="claude_code", ready_terminal=None)
        first = box(ESC + "2mHint" + ESC + "22m") + ESC + "1;1HLOGO A"
        second = box(ESC + "2mHint" + ESC + "22m") + ESC + "1;1HLOGO B"
        self.cao.side_effect = [{"output": first}, {"output": second}]
        with patch.object(self.server.time, "time", return_value=1000):
            self.assertFalse(self.server.member_ready(self.developer, {"status": "idle"}))
        with patch.object(self.server.time, "time", return_value=1001):
            self.assertTrue(self.server.member_ready(self.developer, {"status": "idle"}))

    def test_screen_clear_does_not_reuse_old_empty_box(self):
        raw = box() + ESC + "2J" + ESC + "6;1H❯ real input"
        self.assertFalse(self.ready(raw))

    def test_reverse_index_and_scroll_region_keep_visible_rows(self):
        raw = "ABC\r\nDEF" + ESC + "2;2H" + "\x1bMX"
        self.assertEqual(self.server.composer_text(raw, "hermes"), "AXC\nDEF")
        frame = "A\r\nB\r\nC" + ESC + "1;3r"
        self.assertEqual(self.server.composer_text(frame + "\x1bM", "hermes"), "\nA\nB")
        self.assertEqual(self.server.composer_text(frame + ESC + "3;1H\n", "hermes"), "B\nC")

    def test_borderless_prediction_never_hides_real_continuation_below_prompt(self):
        for provider, style in (("claude_code", "2"), ("hermes", "38;5;240;3")):
            with self.subTest(provider=provider):
                raw = (ESC + "48;1H❯ " + ESC + style + "mSynthetic hint" + ESC + "0m" +
                       ESC + "49;3Hreal continuation" + ESC + "48;3H")
                self.assertFalse(self.ready(raw, provider))

    def test_large_coordinates_and_incomplete_controls_fail_closed(self):
        for suffix in (ESC + "999999;999999H", ESC + "12;", ESC + "2P"):
            with self.subTest(suffix=suffix):
                self.assertFalse(self.ready(box() + suffix))

    def test_lf_only_hermes_history_preserves_ready_and_completed_evidence(self):
        for lines in (80, 700):
            with self.subTest(lines=lines):
                history = ("Synthetic historical row " + "x" * 80 + "\n") * lines
                raw = history + hermes_frame("合成已完成答案。")
                self.assertNotIn("\r", raw)
                self.assertTrue(self.ready(raw, "hermes"))
                self.assertTrue(self.server.hermes_idle(raw))

    def test_lf_only_history_does_not_hide_current_real_input(self):
        for provider in ("claude_code", "hermes"):
            with self.subTest(provider=provider):
                raw = ("Synthetic row " + "x" * 80 + "\n") * 80 + RAIL + "\n❯ real input\n" + RAIL
                self.assertFalse(self.ready(raw, provider))

    def test_recent_redrawn_composer_overrides_lower_historical_box(self):
        for provider, style in (("claude_code", "2"), ("hermes", "38;5;240;3")):
            for current, expected in (("real current input", False), (ESC + style + "mSynthetic hint" + ESC + "0m", True)):
                with self.subTest(provider=provider, empty=expected):
                    history = box("", row=70)
                    current_box = box(current, row=5).replace(ESC + "2J", "", 1)
                    self.assertEqual(self.ready(history + current_box, provider), expected)

    def test_new_input_patch_does_not_reuse_old_lower_empty_prompt(self):
        for provider in ("claude_code", "hermes"):
            with self.subTest(provider=provider):
                raw = box("", row=70) + box("", row=5).replace(ESC + "2J", "", 1) + ESC + "6;3Hreal current input"
                self.assertFalse(self.ready(raw, provider))
