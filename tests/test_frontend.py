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
    def check_ui(self, case):
        with tempfile.TemporaryDirectory(prefix="groupchat-ui-") as tmp:
            result = subprocess.run(
                [NODE, str(ROOT / "tests/frontend_effort.js"), str(ROOT / "index.html"), case],
                cwd=tmp, env={"HOME": tmp, "PATH": os.defpath},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "OK\n")

    def test_provider_specific_choices_and_edit_selection(self):
        self.check_ui("choices")

    def test_provider_switching_and_form_payload(self):
        self.check_ui("switching")

    def test_demo_invitation_editing_and_member_cards(self):
        self.check_ui("demo")


if __name__ == "__main__":
    unittest.main()
