"""Public desktop CLI tests against an isolated copy of the manager state."""
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from legacy_source import LEGACY, ROOT
BRIDGE = ROOT / "bridge/desktop.py"
sys.path.insert(0, str(LEGACY / "scripts"))
from cli_manager import controller
from cli_manager.store import Store
sys.path.insert(0, str(ROOT / 'bridge'))
import desktop


class DesktopTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "state"
        self.provider = self.root / "Claude CLI fixture"
        shutil.copyfile(ROOT / "tests/fixture-provider", self.provider)
        self.provider.chmod(0o700)
        self.env = {**os.environ, "CLI_MANAGER_STATE": str(self.state),
                    "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]])}
        self.env.pop("CLI_DELEGATE_DEPTH", None)

    def bridge(self, *args, code=0, terminal=False):
        fd = master = None
        if terminal:
            master, fd = pty.openpty()
        try:
            result = subprocess.run([sys.executable, "-I", "-B", str(BRIDGE), *map(str, args)],
                                    env=self.env, stdin=fd or subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=20)
        finally:
            if fd is not None:
                os.close(fd)
                os.close(master)
        self.assertEqual(result.returncode, code, (args, result.stdout, result.stderr))
        return result.stdout if terminal else json.loads(result.stdout)

    def install_instance(self, name="alpha"):
        return self.bridge("create", name, "--backend", "claude", "--parallel", 2, "--bin", self.provider)

    def instance(self, name="alpha"):
        return next(row for row in self.bridge("snapshot")["instances"] if row["id"] == name)

    def instance_command(self, action, *extra, code=0, terminal=False):
        return self.bridge(action, "alpha", "--expect", self.instance()["guard"], *extra, code=code, terminal=terminal)

    def open_store(self):
        store = Store(self.state)
        self.addCleanup(store.db.close)
        return store

    def test_future_schema_refuses_mutations_and_agent_commands(self):
        self.install_instance()
        store = self.open_store()
        store.db.execute("PRAGMA user_version=777")
        self.assertEqual(self.instance()["id"], "alpha")
        self.assertIn("版本", self.bridge("create", "other", "--backend", "claude", "--parallel", 2,
                                       "--bin", self.provider, code=2)["error"])
        result = subprocess.run([sys.executable, "-I", "-B", str(BRIDGE), "legacy", "task", "list"],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("版本", result.stderr)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 777)
        self.assertEqual(len(store.rows("instances")), 1)

    def test_empty_snapshot_does_not_create_state(self):
        data = self.bridge("snapshot")
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["instances"], [])
        self.assertEqual(data["tasks"], [])
        self.assertFalse(self.state.exists())

    def test_native_auth_and_lifecycle(self):
        self.install_instance()
        initial = self.instance()
        self.assertEqual(initial["login"], "unknown")
        self.assertEqual(initial["quota"]["state"], "unknown")
        self.instance_command("refresh")
        self.assertEqual(self.instance()["login"], "signed_out")
        self.instance_command("login", code=2)
        output = self.instance_command("login", terminal=True)
        self.assertIn("Fixture login complete", output)
        self.assertEqual(self.instance()["login"], "signed_in")
        self.instance_command("refresh")
        data = self.bridge("snapshot")
        self.assertNotIn("fixture.private", json.dumps(data))
        self.assertNotIn(str(self.provider), json.dumps(data))
        self.assertEqual(self.instance()["quota"]["remaining_percent"], 72)

        store = self.open_store()
        before = store.get("instances", "alpha")["bound_account"]
        (store.instance_dir("alpha") / "cli/fixture-next").write_text("second.private@example.invalid")
        self.instance_command("switch", terminal=True)
        self.assertNotEqual(store.get("instances", "alpha")["bound_account"], before)
        self.assertNotEqual(self.instance()["guard"], initial["guard"])
        self.assertTrue(self.instance_command("delete")["requires_logout"])
        self.instance_command("delete", "--confirm", code=2)
        self.instance_command("logout", terminal=True)
        self.assertEqual(self.instance()["login"], "signed_out")
        self.assertTrue(self.instance_command("delete", "--confirm")["deleted"])
        self.assertEqual(self.bridge("snapshot")["instances"], [])
        self.install_instance()
        self.bridge("delete", "alpha", "--expect", initial["guard"], "--confirm", code=2)

    def test_interrupted_auth_can_retry_when_lock_is_gone(self):
        self.install_instance()
        store = self.open_store()
        row = store.get("instances", "alpha")
        store.put("instances", {**row, "auth_busy": True})
        with controller.instance_lock(store, "alpha"):
            self.assertEqual(self.instance()["login"], "authenticating")
        self.assertEqual(self.instance()["login"], "auth_interrupted")
        self.instance_command("login", terminal=True)
        self.assertEqual(self.instance()["login"], "signed_in")

    def test_queue_and_delete_guards(self):
        self.install_instance()
        store = self.open_store()
        instance = store.get("instances", "alpha")
        default_task = {"id": "example", "kind": "review", "state": "queued", "allowed": ["alpha"],
                        "handoff_targets": [], "brief": "secret_do_not_emit"}
        store.put("tasks", default_task)
        self.assertEqual(self.instance_command("delete")["tasks"], ["example"])
        self.instance_command("delete", "--confirm", code=2)
        try:
            process_identity = controller.identity(os.getpid())
        except PermissionError:
            process_identity = None
        attempt = {"id": "attempt", "instance": "alpha", "task": "example", "state": "stopping",
                   "pid": os.getpid(), "identity": process_identity,
                   "generation": instance["generation"]}
        store.put("attempts", attempt)
        store.put("tasks", {**default_task, "state": "stopping", "attempt": attempt["id"]})
        snap = self.bridge("snapshot")
        expected_activity = "stopping" if process_identity else "unverified"
        self.assertEqual(snap["instances"][0]["running"], [{"id": "example", "state": expected_activity}])
        self.assertEqual(snap["tasks"][0]["activity"], expected_activity)
        with patch.object(desktop, "identity", return_value="identified"):
            self.assertEqual(desktop.activity({**attempt, "identity": "identified"}, time.time()), "stopping")
        self.assertNotIn("secret_do_not_emit", json.dumps(snap))
        self.instance_command("login", terminal=True, code=2)
        store.put("attempts", {**attempt, "identity": "wrong process"})
        self.assertEqual(self.instance()["running"][0]["state"], "unverified")

    def test_registration_is_stable_through_account_generation_but_not_recreation(self):
        self.install_instance()
        first = self.instance()
        store = self.open_store()
        row = store.get("instances", "alpha")
        store.put("instances", {**row, "generation": row["generation"] + 1})
        later = self.instance()
        self.assertEqual(first["registration"], later["registration"])
        self.assertNotEqual(first["guard"], later["guard"])
        folder = store.instance_dir("alpha")
        folder.rename(folder.with_name("old-alpha"))
        folder.mkdir()
        self.assertNotEqual(later["registration"], self.instance()["registration"])

    def test_live_attempt_requires_current_task_and_verified_activity(self):
        self.install_instance()
        store = self.open_store()
        now = time.time()

        def add(name, state="starting", *, task_state=None, reserved=now, pointer=None, owner=None, **extra):
            attempt = f"{name}-attempt"
            store.put("tasks", {"id": name, "state": task_state or state, "attempt": pointer or attempt})
            store.put("attempts", {"id": attempt, "task": owner or name, "instance": "alpha",
                                   "state": state, "reserved": reserved, **extra})

        add("fresh")
        add("stopping", stop="cancelled")
        add("future", reserved=now + 45)
        add("stale", reserved=now - 45)
        add("finished", task_state="review")
        add("detached", pointer="fresh-attempt")
        add("mismatched", owner="fresh")
        add("wrong-pid", state="running", pid=os.getpid(), identity="wrong identity")

        rows = self.bridge("snapshot")["live_attempts"]
        self.assertEqual([(row["task_id"], row["attempt_id"], row["instance"], row["state"]) for row in rows],
                         [("fresh", "fresh-attempt", "alpha", "starting"),
                          ("stopping", "stopping-attempt", "alpha", "stopping")])
        for state in ("review", "failed", "cancelled", "applied"):
            task = store.get("tasks", "fresh")
            store.put("tasks", {**task, "state": state})
            self.assertEqual([row["task_id"] for row in self.bridge("snapshot")["live_attempts"]], ["stopping"])

    def test_quota_follows_usage_and_reset_without_inheriting_dispatch_ttl(self):
        self.install_instance()
        self.instance_command("login", terminal=True)
        self.assertTrue(self.instance_command("refresh")["quota_observed"])
        store = self.open_store()
        account = store.get("instances", "alpha")["bound_account"]
        now = time.time()
        stamp = now - 86400 * 30
        store.db.execute("DELETE FROM account_quota WHERE account=?", (account,))
        window = {"bucket": "claude", "window": "session", "used": 50, "resets_at": now + 300}
        store._put_quota_signal(account, "window", stamp, window)
        quota = self.instance()["quota"]
        self.assertEqual(quota["state"], "observed")
        self.assertEqual(quota["remaining_percent"], 50)
        self.assertEqual(quota["next_reset_at"], window["resets_at"])
        self.assertFalse(controller.current_quota(store, store.get("instances", "alpha"), now)[1])
        store.put("tasks", {"id": "used", "state": "queued"})
        attempt = {"id": "used-attempt", "task": "used", "instance": "alpha", "state": "starting",
                   "account": account, "reserved": now - 30}
        store.put("attempts", attempt)
        self.assertEqual(self.instance()["quota"]["remaining_percent"], 50)
        store.put("attempts", {**attempt, "state": "applied", "start": now - 25, "end": now - 10,
                               "logs": "isolated-native-run"})
        quota = self.instance()["quota"]
        self.assertEqual(quota["windows"][0]["refresh_reason"], "usage")
        self.assertIsNone(quota["remaining_percent"])
        store._put_quota_signal(account, "window", stamp + 1, {**window, "used": 100})
        self.assertEqual(self.instance()["quota"]["state"], "limited")
        store._put_quota_signal(account, "window", stamp + 2, {**window, "used": 100, "resets_at": now - 1})
        quota = self.instance()["quota"]
        self.assertEqual(quota["windows"][0]["refresh_reason"], "reset")
        self.assertEqual(quota["next_reset_at"], now - 1)
        store._put_quota_signal(account, "window", now + 1, {**window, "used": 0, "resets_at": now - 1})
        quota = self.instance()["quota"]
        self.assertIsNone(quota["next_reset_at"])
        self.assertEqual(quota["remaining_percent"], 100)

    def test_expired_binding_and_legacy_migration(self):
        self.install_instance()
        self.instance_command("login", terminal=True)
        self.instance_command("refresh")
        store = self.open_store()
        account = store.get("instances", "alpha")["bound_account"]
        store.db.execute("DROP TABLE account_quota")
        store.db.execute("CREATE TABLE account_quota(account TEXT, kind TEXT, measured REAL, data TEXT)")
        window = {"bucket": "claude", "window": "session", "used": 31}
        store.db.execute("INSERT INTO account_quota VALUES (?,?,?,?)",
                         (account, "windows", time.time(), json.dumps([window])))
        self.assertEqual(self.instance()["quota"]["remaining_percent"], 69)
        self.assertNotIn("bucket", store._quota_columns())
        self.assertIn("版本", self.instance_command("refresh", code=2)["error"])
        self.assertNotIn("bucket", store._quota_columns())
        (store.instance_dir("alpha") / "cli/fixture-login").unlink()
        self.open_store()  # Explicitly simulate the current external CLI's migration in the fixture.
        self.instance_command("refresh")
        self.assertEqual(self.instance()["login"], "expired")
        self.assertEqual(self.instance()["quota"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
