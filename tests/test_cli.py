import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copilot_clm.cli import child_environment


class LauncherTests(unittest.TestCase):
    def test_child_uses_only_local_provider_and_preserves_model_limits(self):
        env = {"PATH": "/bin", "CLM_API_KEY": "private-key", "COPILOT_API_KEY": "other-key",
               "COPILOT_PROVIDERS_CONFIG": "/saved/providers.json",
               "COPILOT_PROVIDER_API_KEY_COMMAND": "print-secret",
               "COPILOT_PROVIDER_HEADERS": '{"Authorization":"bad"}',
               "COPILOT_PROVIDER_BASE_URL": "https://other.example/v1",
               "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": "4096"}
        with patch.dict(os.environ, env, clear=True):
            child = child_environment(SimpleNamespace(wire_api="responses", model="test-model"),
                                      Path("/state"), 1234, "ephemeral-token")
        self.assertEqual(child["COPILOT_PROVIDER_BASE_URL"], "http://127.0.0.1:1234/v1")
        self.assertEqual(child["COPILOT_PROVIDER_API_KEY"], "ephemeral-token")
        self.assertEqual(child["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"], "4096")
        for key in ("CLM_API_KEY", "COPILOT_API_KEY", "COPILOT_PROVIDERS_CONFIG",
                    "COPILOT_PROVIDER_API_KEY_COMMAND", "COPILOT_PROVIDER_HEADERS"):
            self.assertNotIn(key, child)

    @unittest.skipUnless(os.name == "posix", "POSIX launcher")
    def test_sigterm_reaps_native_child_and_releases_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "copilot-fixture"
            pid_file = root / "child.pid"
            executable.write_text(f"#!{sys.executable}\nimport os,time\n"
                                  f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
                                  "time.sleep(60)\n")
            executable.chmod(0o700)
            command = [sys.executable, "-m", "copilot_clm", "run", "--copilot", str(executable),
                       "--state-dir", str(root / "state")]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists(), "native child never started")
                child_pid = int(pid_file.read_text())
                process.send_signal(signal.SIGTERM)
                _, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, errors)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                import fcntl
                with (root / "state/.lock").open() as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
