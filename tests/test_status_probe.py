import json
import os
import pty
import select
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_desktop
from test_desktop import ROOT
from cli_manager import backend


class StatusFixtureTest(unittest.TestCase):
    def setUp(self):
        test_desktop.DesktopTest.setUp(self)
        self.project = self.root / "chosen directory"
        self.project.mkdir()

    bridge = test_desktop.DesktopTest.bridge
    open_store = test_desktop.DesktopTest.open_store
    instance = test_desktop.DesktopTest.instance
    instance_command = test_desktop.DesktopTest.instance_command

    def register_codex(self, name="alpha", mode="normal", bound=True):
        self.provider = self.root / "codex-native"
        shutil.copyfile(ROOT / "tests/fixture-status-provider", self.provider)
        self.provider.chmod(0o700)
        self.bridge("create", name, "--backend", "codex", "--parallel", 2, "--bin", self.provider)
        store = self.open_store()
        row = store.get("instances", name)
        if bound:
            account = backend.account_key("codex", f"{name}@example.invalid", f"workspace-{name}")
            store.put("instances", {**row, "bound_account": account, "bound_scoped": True})
        profile = store.instance_dir(name) / "cli"
        (profile / "fixture-mode").write_text(mode)
        return store, profile

    def test_failed_rpc_preserves_auth_timestamp_and_live_limit(self):
        store, profile = self.register_codex(mode="limited")
        self.instance_command("refresh")
        good = self.instance()
        self.assertEqual(good["login"], "signed_in")
        self.assertEqual(good["quota"]["remaining_percent"], 0)
        (profile / "fixture-mode").write_text("failure")
        self.assertTrue(self.instance_command("refresh")["probe_failed"])
        failed = self.instance()
        self.assertEqual(failed["login"], "unknown")
        self.assertEqual(failed["last_confirmed_auth"]["checked_at"], good["checked_at"])
        self.assertGreater(failed["checked_at"], good["checked_at"])
        self.assertEqual(failed["probe_diagnostic"]["stage"], "initialize")
        self.assertEqual(failed["probe_diagnostic"]["reason"], "permission_denied")
        self.assertNotIn("private-fixture", json.dumps(failed))
        self.assertEqual(failed["quota"]["state"], "limited")
        self.assertEqual(failed["quota"]["limit_reset_at"], good["quota"]["limit_reset_at"])
        self.assertIsNone(failed["quota"]["remaining_percent"])
        self.assertEqual([(row["minutes"], row["remaining_percent"]) for row in failed["quota"]["windows"]], [(300, 0)])
        self.assertEqual([(row["minutes"], row["remaining_percent"]) for row in failed["quota"]["unverified_windows"]],
                         [(10080, None)])
        self.assertFalse(store.get("instances", "alpha")["status"]["logged_in"])

        (profile / "fixture-mode").write_text("malformed")
        self.assertTrue(self.instance_command("refresh")["probe_failed"])
        invalid = self.instance()
        self.assertEqual(invalid["login"], "unknown")
        self.assertEqual(invalid["probe_diagnostic"]["reason"], "invalid_response")
        self.assertEqual(invalid["last_confirmed_auth"]["checked_at"], good["checked_at"])

        (profile / "fixture-mode").write_text("signed-out")
        self.instance_command("refresh")
        expired = self.instance()
        self.assertEqual(expired["login"], "expired")
        self.assertFalse(expired["probe_failed"])
        self.assertEqual(expired["quota"]["state"], "unknown")

    def test_invalidation_cannot_commit_an_outdated_refresh(self):
        store, profile = self.register_codex()
        self.instance_command("refresh")
        (profile / "fixture-mode").write_text("wait")
        instance = self.instance()
        command = [sys.executable, "-I", "-B", str(ROOT / "bridge/desktop.py"), "refresh",
                   "alpha", "--expect", instance["guard"]]
        proc = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (profile / "probe-entered").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            backend.invalidate_probe(store, "alpha", 0)
            (profile / "probe-release").touch()
            stdout, _ = proc.communicate(timeout=8)
            self.assertFalse(json.loads(stdout)["completed"])
            self.assertIsNone(self.instance()["checked_at"])
            (profile / "fixture-mode").write_text("normal")
            self.assertTrue(self.instance_command("refresh")["completed"])
            self.assertEqual(self.instance()["login"], "signed_in")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_foreground_pty_refreshes_before_launch_and_records_exit_with_bound_profile(self):
        store, profile = self.register_codex()
        self.instance_command("refresh")
        instance = self.instance()
        self.bridge("terminal", "alpha", "--expect", instance["guard"], "--directory", self.project, code=2,
                    terminal=True)
        before = store.get("instances", "alpha")
        command = [sys.executable, "-I", "-B", str(ROOT / "bridge/desktop.py"), "terminal", "alpha",
                   "--expect", instance["guard"], "--directory", str(self.project)]
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, command, self.env)
        output = b""
        try:
            deadline = time.monotonic() + 8
            while b"Permission" not in output:
                self.assertLess(time.monotonic(), deadline, output)
                if select.select([fd], [], [], .2)[0]:
                    output += os.read(fd, 8192)
            quota = self.instance()["quota"]
            self.assertIsNone(quota["windows"][0]["refresh_reason"])
            self.assertEqual(quota["remaining_percent"], 76)
            self.assertGreater(quota["observed_at"], instance["quota"]["observed_at"])
            # Legacy account or status operations cannot race the shared profile.
            result = self.instance_command("refresh", code=2)
            self.assertIn("锁", result["error"])
            os.write(fd, b"y\nblue\n")
            while b"Fixture complete" not in output:
                self.assertLess(time.monotonic(), deadline, output)
                if select.select([fd], [], [], .2)[0]:
                    output += os.read(fd, 8192)
            receipt = json.loads((self.project / "fixture-terminal-result.json").read_text())
            self.assertEqual(receipt["profile"], str(profile))
            self.assertEqual(receipt["answer"], "blue")
            self.assertTrue(receipt["allowed_fixture_read"])
            self.assertNotEqual(receipt["user_pid"], pid)
            current = store.get("instances", "alpha")
            self.assertEqual(current["generation"], before["generation"])
            self.assertEqual(current["bound_account"], before["bound_account"])
        finally:
            os.close(fd)
            os.waitpid(pid, 0)
        record = self.instance()
        self.assertGreater(record["quota_usage"]["ended"], record["quota"]["observed_at"])
        self.assertEqual(record["quota"]["windows"][0]["refresh_reason"], "usage")
