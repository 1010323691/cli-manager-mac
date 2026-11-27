"""Run owner checks through the installed Skill launcher and original task engine."""
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from legacy_source import LEGACY, ROOT
sys.path.insert(0, str(LEGACY / "scripts"))
from cli_manager import backend
from cli_manager.store import Store, write_json


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.state = self.root / "state"
        self.project = self.root / "fixture project"
        self.project.mkdir()
        (self.project / "fixture-file.txt").write_text("isolated\n")
        subprocess.run(["/usr/bin/git", "-C", self.project, "init", "--template=", "-q"], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.project, "add", "."], check=True)
        subprocess.run(["/usr/bin/git", "-C", self.project, "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        app = self.root / "installed app" / "CLI Manager.app"
        resources = app / "Contents/Resources"
        resources.mkdir(parents=True)
        for path in (ROOT / "bridge").glob("*.py"):
            shutil.copyfile(path, resources / path.name)
        (resources / "Legacy").symlink_to(LEGACY, target_is_directory=True)
        skill = self.root / "installed skill"
        shutil.copytree(ROOT / "skill/cli-manager-desktop", skill)
        (skill / "app-path").write_text(str(app) + "\n")
        self.manager = skill / "scripts/manager"
        self.env = {**os.environ, "CLI_MANAGER_STATE": str(self.state),
                    "CLI_MANAGER_PYTHON": sys.executable,
                    "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
        self.env.pop("CLI_DELEGATE_DEPTH", None)
        self.primary = self.call("planner", "open")["receipt"]
        self.other = self.call("planner", "open")["receipt"]
        self.bin = self.root / "claude-fixture"
        shutil.copyfile(ROOT / "tests/fixture-decision-provider", self.bin)
        self.bin.chmod(0o700)
        self.call("desktop", "create", "local", "--backend", "claude", "--parallel", 2, "--bin", self.bin)
        self.store = Store(self.state)
        self.addCleanup(self.store.db.close)
        instance = self.store.get("instances", "local")
        instance["bound_account"] = backend.account_key("claude", "planner-fixture@example.invalid")
        self.store.put("instances", instance)
        self.live = set()
        self.addCleanup(self.stop_live)

    def stop_live(self):
        for name in self.live:
            subprocess.run([self.manager, "task", "cancel", name], env=self.env, text=True, capture_output=True,
                           timeout=8)
        if self.live:
            self.wait_for(lambda: not any(row["state"] in {"starting", "running", "stopping"}
                                           and row["id"] in self.live for row in self.store.rows("tasks")), timeout=9)

    def call(self, *arguments, code=0, env=None):
        result = subprocess.run([self.manager, *map(str, arguments)], env=env or self.env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, code, (arguments, result.stdout, result.stderr))
        return json.loads(result.stdout) if result.stdout else None

    def planner(self, receipt, *arguments, reference=None, code=0):
        command = ["planner", "--receipt", receipt]
        if reference:
            command += ["--request-ref", reference]
        return self.call(*command, "task", *arguments, code=code)

    def submit(self, name, receipt=None):
        arguments = ("task", "submit", "--id", name, "--project", self.project, "--instance", "local",
                     "--kind", "review", "--prompt", "approve scoped fixture read")
        if receipt:
            return self.planner(receipt, *arguments[1:])
        return self.call(*arguments)

    def start(self, name):
        self.live.add(name)
        self.call("queue", "work", "--once")

    def wait_for(self, function, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := function():
                return value
            time.sleep(.08)
        self.fail(f"Timed out waiting for the isolated fixture: {self.store.rows('tasks')}")

    def pending(self, name):
        def lookup():
            result = self.planner(self.primary, "watch", name, "--wait", 1)
            self.assertNotIn("decision_owner", json.dumps(result))
            pending = result["task"]["pending"]
            return {item["kind"]: item for item in pending} if len(pending) == 2 else None
        return self.wait_for(lookup)

    def decide(self, name, request, receipt, *, code=0):
        if request["kind"] == "question":
            arguments = ["answer", "--answers", '{"color":"blue"}']
        else:
            arguments = ["allow"]
        return self.planner(receipt, "decide", name, request["id"], *arguments,
                            reference=request["request_ref"], code=code)

    def review(self, name):
        row = self.wait_for(lambda: self.store.get("tasks", name)
                            if self.store.get("tasks", name)["state"] == "review" else None)
        self.live.discard(name)
        return row

    def test_provider_question_and_permission_return_to_original_planner_across_resume(self):
        self.submit("planner-live", self.primary)
        self.start("planner-live")
        first = self.pending("planner-live")
        self.assertEqual(self.call("desktop", "inspect-decisions")["entries"], [
            {"id": "planner-live", "route": "planner", "questions": 1, "permissions": 1}])
        raw = self.call("task", "watch", "planner-live", "--wait", 1)
        self.assertEqual(raw["task"]["pending"], [])
        self.assertNotIn("fixture planner question", json.dumps(raw))
        wrong = self.planner(self.other, "watch", "planner-live", "--wait", 1, code=2)
        self.assertIn("不属于", wrong["error"])
        self.assertIn("不属于", self.decide("planner-live", first["permission"], self.other, code=2)["error"])
        self.call("task", "decide", "planner-live", first["permission"]["id"], "allow", code=2)

        self.decide("planner-live", first["question"], self.primary)
        self.decide("planner-live", first["permission"], self.primary)
        complete = self.review("planner-live")
        self.assertEqual(self.call("desktop", "inspect-decisions")["entries"], [])
        self.planner(self.primary, "resume", "planner-live")
        self.start("planner-live")
        second = self.pending("planner-live")
        self.assertEqual(self.store.get("tasks", "planner-live")["attempt"], complete["attempt"])
        self.assertEqual(first["question"]["id"], second["question"]["id"])
        self.assertNotEqual(first["question"]["request_ref"], second["question"]["request_ref"])
        self.assertIn("其他执行", self.decide("planner-live", first["question"], self.primary, code=2)["error"])
        self.decide("planner-live", second["question"], self.primary)
        self.decide("planner-live", second["permission"], self.primary)
        final = self.review("planner-live")
        report = self.planner(self.primary, "status", final["id"])["report_path"]
        self.assertIn("existing MCP bridge", Path(report).read_text())

    def test_simultaneous_terminal_and_managed_worker_share_profile_only(self):
        self.submit("simultaneous", self.primary)
        self.start("simultaneous")
        original = self.pending("simultaneous")
        instance, = self.call("desktop", "snapshot")["instances"]
        before = self.store.get("instances", "local")
        attempt = self.store.get("attempts", self.store.get("tasks", "simultaneous")["attempt"])
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(self.manager, [str(self.manager), "desktop", "terminal", "local", "--expect",
                                    instance["guard"], "--directory", str(self.project)], self.env)
        output = b""
        try:
            def wait_for_text(marker):
                nonlocal output
                deadline = time.monotonic() + 8
                while marker not in output:
                    self.assertLess(time.monotonic(), deadline, output)
                    if select.select([fd], [], [], .15)[0]:
                        output += os.read(fd, 8192)

            wait_for_text(b"Native fixture permission")
            os.write(fd, b"y\ngreen\n")
            wait_for_text(b"Native fixture complete")
            receipt = json.loads((self.project / "direct-receipt.json").read_text())
            self.assertEqual(receipt["profile"], str(self.store.instance_dir("local") / "cli"))
            self.assertEqual(receipt["answer"], "green")
            self.assertTrue(receipt["allowed"])
            self.assertNotEqual(receipt["pid"], attempt["pid"])
            self.assertEqual(self.pending("simultaneous"), original)
            self.assertEqual(self.call("task", "status", "simultaneous")["pending"], [])
            after = self.store.get("instances", "local")
            self.assertGreater(after["probed_at"], before["probed_at"])
            for key in ("bound_account", "generation", "provider"):
                self.assertEqual(after[key], before[key])
        finally:
            os.close(fd)
            os.waitpid(pid, 0)
        self.decide("simultaneous", original["question"], self.primary)
        self.decide("simultaneous", original["permission"], self.primary)
        self.review("simultaneous")

    def synthetic_pending(self, name, request):
        attempt_id = f"{name}-attempt"
        directory = self.state / "fixture-logs" / name
        row = self.store.get("tasks", name)
        write_json(directory / "approvals/001.request.json",
                   {**request, "expires_at": time.time() + 90})
        self.store.put("attempts", {"id": attempt_id, "task": name, "instance": "local", "state": "running",
                                    "work": str(self.project), "cwd": str(self.project),
                                    "logs": str(directory), "reservation": "fixture-reservation"})
        self.store.put("tasks", {**row, "attempt": attempt_id, "state": "running"})
        return directory

    def test_unknown_and_revoked_owners_have_no_reassignment_and_policy_still_applies(self):
        self.submit("unassigned")
        self.submit("planned", self.primary)
        self.synthetic_pending("unassigned", {"kind": "question", "input": {"questions": [{"question": "private"}]}})
        directory = self.synthetic_pending("planned", {"kind": "permission", "tool_name": "Read",
                                                       "input": {"file_path": "../outside"}})
        row = self.planner(self.primary, "status", "planned")["pending"][0]
        self.assertIn("任务目录", self.decide("planned", row, self.primary, code=2)["error"])
        self.assertEqual(self.call("task", "status", "unassigned")["pending"], [])
        self.assertIn("归属未核实", self.planner(self.other, "status", "unassigned", code=2)["error"])
        cache = self.call("desktop", "inspect-decisions")["entries"]
        self.assertEqual([entry["route"] for entry in cache], ["unknown", "planner"])
        self.assertNotIn("private", json.dumps(cache))

        self.call("planner", "--receipt", self.primary, "close")
        self.assertIn("失效", self.decide("planned", row, self.primary, code=2)["error"])
        self.assertIn("不属于", self.decide("planned", row, self.other, code=2)["error"])
        self.assertFalse((directory / "approvals/001.decision.json").exists())
        self.assertEqual(cache, self.call("desktop", "inspect-decisions")["entries"])
        self.assertNotIn("decision", json.dumps(self.call("desktop", "snapshot")))
