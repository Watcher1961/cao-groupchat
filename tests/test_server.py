"""Isolated standard-library unit tests; never use a running CAO instance."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


class ImportSafetyTests(unittest.TestCase):
    def assert_import_safe(self, configured):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = home / ("custom-data" if configured else ".cao-groupchat")
            env = {
                "HOME": str(home),
                "PATH": os.defpath,
                "CAO_URL": "http://127.0.0.1:1",
                "CAO_TOKEN": "",
                "GROUPCHAT_PORT": "8765",
            }
            if configured:
                env["GROUPCHAT_DATA"] = str(expected)
            script = r'''
from contextlib import ExitStack
import http.server
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading
from unittest.mock import patch
import urllib.request

source, expected = map(Path, sys.argv[1:])
protected = {expected, Path.home() / ".cao-groupchat"}

def audit(event, args):
    if event in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove",
                 "os.rmdir", "os.rename"}:
        for value in args:
            if isinstance(value, (str, bytes)):
                path = Path(value.decode() if isinstance(value, bytes) else value)
                if any(path == p or p in path.parents for p in protected):
                    raise AssertionError("import accessed runtime data")

sys.addaudithook(audit)
with ExitStack() as stack:
    # A read attempt or existence probe also violates the import contract.
    for method in ("open", "read_text", "read_bytes", "write_text", "write_bytes",
                   "exists", "mkdir", "replace"):
        stack.enter_context(patch.object(Path, method,
                            side_effect=AssertionError("import performed file I/O")))
    for target in ("http.server.ThreadingHTTPServer", "threading.Thread.start",
                   "urllib.request.urlopen", "socket.socket.connect",
                   "socket.socket.bind", "subprocess.Popen"):
        stack.enter_context(patch(target,
                            side_effect=AssertionError("import started external work")))
    spec = importlib.util.spec_from_file_location("isolated_groupchat", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DATA_DIR == expected
    assert module.STATE_FILE == expected / "groups.json"
    assert module.groups == {}
    assert callable(module.main)
'''
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(SERVER_PATH), str(expected)],
                cwd=home, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(list(home.iterdir()), [])

    def test_default_data_directory_is_not_accessed_on_import(self):
        self.assert_import_safe(configured=False)

    def test_configured_data_directory_is_not_accessed_on_import(self):
        self.assert_import_safe(configured=True)


if __name__ == "__main__":
    unittest.main()
