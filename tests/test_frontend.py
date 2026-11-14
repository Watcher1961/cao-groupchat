"""Optional inline-UI behavior checks; Node is a test tool, never an app dependency."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is unavailable; inline UI behavior checks skipped")
class FrontendEffortTests(unittest.TestCase):
    def check_ui(self, case, script="frontend_effort.js"):
        with tempfile.TemporaryDirectory(prefix="groupchat-ui-") as tmp:
            result = subprocess.run(
                [NODE, str(ROOT / "tests" / script), str(ROOT / "index.html"), case],
                cwd=tmp, env={"HOME": tmp, "PATH": os.defpath},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "OK\n")

    def test_provider_specific_choices_and_edit_selection(self):
        self.check_ui("choices")

    def test_provider_switching_and_form_payload(self):
        self.check_ui("switching")

    def test_uncertain_delivery_has_manual_retry_without_endless_typing(self):
        self.check_ui("delivery")

    def test_drawer_breakpoints_entries_and_overlay_state(self):
        self.check_ui("drawers","frontend_drawers.js")

    def test_drawer_keyboard_modal_priority_and_resizing(self):
        self.check_ui("drawer_keyboard","frontend_drawers.js")

    def test_group_selection_closes_only_the_current_left_drawer(self):
        self.check_ui("drawer_group_select","frontend_drawers.js")

    def test_shared_log_ignores_stale_group_or_tab_responses(self):
        self.check_ui("drawer_log_race","frontend_drawers.js")

    def test_mobile_demo_create_switch_send_edit_logs_and_themes(self):
        self.check_ui("drawer_workflow","frontend_drawers.js")

    def test_demo_invitation_editing_and_member_cards(self):
        self.check_ui("demo")


if __name__ == "__main__":
    unittest.main()
