"""End-to-end tests for the public CLI, with local process protocols and disposable projects."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from cli_manager import backend, controller, workspace
from cli_manager.store import Store


class AccountQuotaTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.addCleanup(self.store.db.close)
        self.instance = {"id": "one", "generation": 0, "provider": "codex",
                         "bound_scoped": True, "bound_account": "account"}
        self.store.put("instances", self.instance)

    def probe(self, instance, stamp, used):
        windows = [] if used is None else [{"used": used, "resets_at": 1000}]
        sample = backend.quota(windows, "test", observed_at=stamp, checked_at=stamp)
        status = {"logged_in": True, "account": "account", "legacy_account": "legacy", "quota": sample}
        with patch.object(backend, "probe", return_value=status):
            return controller.refresh_instance(self.store, instance, force=True)

    def test_P1_windows_preserve_explicit_block(self):
        instance = self.probe(self.instance, 10, None)
        self.store.record_quota(instance, {"explicitly_blocked": True, "checked_at": 11})
        self.probe(instance, 12, 5)
        self.assertTrue(controller.current_quota(self.store, instance, 130)[0])
        self.assertFalse(controller.current_quota(self.store, instance, 131)[0])
        self.store.record_quota(instance, {"explicitly_blocked": False, "checked_at": 13})
        self.assertFalse(controller.current_quota(self.store, instance, 14)[0])

    def test_P2_partial_update_preserves_other_windows_after_upgrade(self):
        instance = self.probe(self.instance, 5, None)
        full = {"bucket": "A", "window": "primary", "used": 100, "resets_at": 500}
        secondary = dict(full, window="secondary", used=50)
        self.store.db.execute("DROP TABLE account_quota")
        self.store.db.execute("""CREATE TABLE account_quota(account TEXT, kind TEXT, measured REAL,
                              data TEXT, PRIMARY KEY(account, kind))""")
        self.store.db.execute("INSERT INTO account_quota VALUES(?,?,?,?)",
                              ("account", "windows", 10, json.dumps([full, secondary])))
        upgraded = Store(self.store.root)
        self.addCleanup(upgraded.db.close)
        for stamp, window in [(150, dict(full, bucket="B", used=20)),
                              (151, dict(secondary, used=30)), (9, dict(full, used=1))]:
            upgraded.record_quota(instance, {"observed_at": stamp, "windows": [window]})
        self.assertEqual(controller.current_quota(upgraded, instance, 152), (True, True, 100))
        self.assertEqual(controller.current_quota(upgraded, instance, 500), (False, False, 0))
        upgraded.record_quota(instance, {"observed_at": 153, "windows": [dict(full, used=5)]})
        self.assertEqual(controller.current_quota(upgraded, instance, 154), (False, True, 30))

    def test_P5_turn_keeps_quota_updates_from_both_buckets(self):
        instance = self.probe(self.instance, 9, None)
        window = {"bucket": "A", "window": "primary", "used": 100, "resets_at": 500}
        self.store.record_quota(instance, {"observed_at": 10, "windows": [window]})
        self.assertTrue(controller.current_quota(self.store, instance, 11)[0])

        sample = {}
        for bucket, used, stamp in [("A", 5, 12), ("B", 20, 16)]:
            payload = {"rateLimits": {"limitId": bucket, "primary": {"usedPercent": used}}}
            if bucket == "A":
                payload["ordinaryUsageAllowed"] = True
            with patch.object(backend.time, "time", return_value=stamp):
                sample = backend.merge_codex_quota_update(sample, payload)
        self.store.record_quota(instance, sample)
        self.assertEqual(controller.current_quota(self.store, instance, 17), (False, True, 20))
        self.assertEqual([(w["bucket"], w["used"], w["observed_at"]) for w in sample["windows"]],
                         [("A", 5, 12), ("B", 20, 16)])
        self.assertIn(("blocked", 12, False), self.store.quota_signals("account"))
        # An independent A measurement between the two notifications still supersedes A=5.
        self.store.record_quota(instance, {"observed_at": 14, "windows": [window]})
        self.assertTrue(controller.current_quota(self.store, instance, 17)[0])

    def test_P11_full_survives_unknown_until_reset(self):
        self.probe(self.instance, 10, 100)
        self.probe(self.instance, 12, None)
        self.assertTrue(controller.current_quota(self.store, self.instance, 999)[0])
        self.assertFalse(controller.current_quota(self.store, self.instance, 1000)[0])

    def test_P22_late_older_probe(self):
        self.probe(self.instance, 8, 5)
        task = {"id": "task", "state": "running"}
        self.store.put("tasks", task)
        attempt = {"id": "attempt", "task": "task", "instance": "one", "account": "account",
                   "generation": 0, "revisions": {}}
        quota = backend.quota([{"used": 100, "resets_at": 1000}], "execution",
                              observed_at=12, checked_at=12)
        with self.store.transaction():
            controller.finish(self.store, task, attempt, {"outcome": "failed", "quota": quota},
                              prepared=([], b"", [], None))
        self.probe(self.instance, 10, 5)
        self.assertTrue(controller.current_quota(self.store, self.instance, 14)[0])

    def test_P24_unknown_after_sibling_low(self):
        sibling = dict(self.instance, id="two")
        self.store.put("instances", sibling)
        self.probe(self.instance, 10, 100)
        self.probe(sibling, 12, 5)
        result = self.probe(sibling, 13, None)
        self.assertEqual(result["status"]["quota"]["last_observation"]["observed_at"], 12)
        self.assertEqual(controller.current_quota(self.store, self.instance, 14), (False, True, 5))

    def test_upgrade_uses_verified_last_observation(self):
        sibling = dict(self.instance, id="two")
        self.store.put("instances", sibling)
        self.probe(self.instance, 10, 100)
        self.probe(sibling, 12, 5)
        self.probe(sibling, 13, None)
        # Simulate the schema before per-account observations existed.
        self.store.db.execute("DROP TABLE account_quota")
        upgraded = Store(self.store.root)
        self.addCleanup(upgraded.db.close)
        self.assertEqual(controller.current_quota(upgraded, self.instance, 14), (False, True, 5))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.env = {**os.environ, "CLI_MANAGER_STATE": str(self.store.root), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env.pop("CLI_DELEGATE_DEPTH", None)
        self.project = self.root / "project"
        self.project.mkdir()
        self.git("init", "-q", "--template=")
        (self.project / "change.txt").write_text("HEAD\n")
        (self.project / "untouched.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.children = []
        self.addCleanup(self.kill_workers)

    def kill_workers(self):
        for attempt in self.store.rows("attempts"):
            if attempt["state"] in controller.ACTIVE and attempt.get("pid"):
                controller.stop_group(attempt["pid"], signal.SIGKILL)
        for p in self.children:
            if p.poll() is None:
                p.kill()
                p.wait()

    def git(self, *args):
        return workspace.git(self.project, *args)

    def cli(self, *args, code=0):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/delegate.py"), *map(str, args)], env=self.env,
            capture_output=True, text=True, timeout=25,
        )
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr, args))
        return json.loads(result.stdout) if result.stdout else None

    def register(self, name, provider, account, legacy=False):
        instance_bin = self.root / f"{provider}-{name}"
        shutil.copyfile(ROOT / "tests/fixture_cli.py", instance_bin)
        instance_bin.chmod(0o700)
        self.cli("instance", "create", name, "--backend", provider, "--bin", instance_bin)
        account_file = self.store.instance_dir(name) / "cli" / "fixture-account"
        account_file.write_text(account)
        instance = self.store.get("instances", name)
        if provider == "codex" and not legacy:
            instance.update(bound_account=backend.account_key(provider, account, "ws-default"), bound_scoped=True)
        else:
            instance["bound_account"] = backend.account_key(provider, account)
        self.store.put("instances", instance)

    def launch(self):
        self.cli("queue", "work", "--once")

    def wait(self, key, states):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.cli("task", "status", key)
            if status["state"] in states:
                return status
            if status["state"] in {"failed", "needs_report", "auth"} and status["state"] not in states:
                detail = Path(status["report_path"]).read_text() if status["report_path"] else status
                self.fail(detail)
            time.sleep(.08)
        self.fail(f"{key} state {status}")

    def submit(self, name, instance, prompt, *args, project=None):
        return self.cli("task", "submit", "--id", name, "--project", project or self.project,
                        "--instance", instance, "--prompt", prompt, *args)

    def approve(self, task):
        return self.cli("task", "review", "approve", task["id"], "--revision", task["report"])["approved"]

    def test_git_worktree_preserves_index_and_unrelated_edits(self):
        self.register("claude1", "claude", "claude@example.invalid")
        (self.project / "untouched.txt").write_text("staged\n")
        self.git("add", "untouched.txt")
        (self.project / "untouched.txt").write_text("unstaged\n")
        index = (self.project / ".git/index").read_bytes()
        self.submit("first", "claude1", "require untouched.txt original\nedit change.txt delivered")
        self.launch()
        status = self.wait("first", {"review"})
        attempt = status["attempts"][-1]
        self.assertEqual((Path(attempt["work"]) / "change.txt").read_text(), "delivered\n")
        self.assertEqual((self.project / "change.txt").read_text(), "HEAD\n")
        revision = self.approve(status)
        self.assertEqual(self.cli("task", "apply", "first", "--revision", revision, "--check")["state"], "ready")
        self.assertEqual(self.cli("task", "apply", "first", "--revision", revision, "--apply")["state"], "applied")
        self.cli("task", "apply", "first", "--revision", revision, "--apply")
        self.assertEqual((self.project / "change.txt").read_text(), "delivered\n")
        self.assertEqual((self.project / "untouched.txt").read_text(), "unstaged\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)

    def test_baselines_retain_explicit_and_non_git_ignored_files(self):
        (self.project / ".gitignore").write_text("local.txt\n")
        (self.project / "local.txt").write_text("local\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore local file")
        copy = self.root / "plain"
        copy.mkdir()
        for filename in (".gitignore", "local.txt"):
            shutil.copyfile(self.project / filename, copy / filename)
        for project, include in ((self.project, ["local.txt"]), (copy, [])):
            with self.subTest(project=project), tempfile.TemporaryDirectory(dir=self.root) as temp:
                directory = Path(temp)
                baseline = workspace.freeze(project, directory, include)
                content = workspace.git(directory / "baseline.git", "show", f"{baseline['commit']}:local.txt")
                self.assertEqual(content, b"local\n")

    def test_case_alias_cannot_register_or_remove_another_instance(self):
        self.register("CodexOne", "codex", "account@example.invalid")
        owner = self.store.get("instances", "CodexOne")
        self.cli("instance", "create", "codexone", "--backend", "codex", "--bin", owner["binary"], code=2)
        directory = self.store.instance_dir("CodexOne")
        if (directory.parent / "codexone").exists():
            # Simulate a record registered before alias detection was added.
            self.store.put("instances", {**owner, "id": "codexone", "bound_account": None})
            self.cli("instance", "delete", "codexone", "--confirm", code=2)
        self.assertEqual((directory / "cli" / "fixture-account").read_text(), "account@example.invalid")

    def test_case_only_rename_is_never_approved_as_an_empty_change(self):
        self.register("claude1", "claude", "claude@example.invalid")
        insensitive = (self.project / "CHANGE.txt").exists()
        self.submit("rename", "claude1", "rename change.txt CHANGE.txt")
        self.launch()
        if insensitive:
            result = self.wait("rename", {"failed"})
            report = json.loads(Path(result["report_path"]).read_text())
            self.assertIn("大小写", report["diagnostic"])
            self.cli("task", "review", "approve", "rename", "--revision", result["report"], code=2)
            self.assertNotIn("CHANGE.txt", os.listdir(self.project))
        else:
            result = self.wait("rename", {"review"})
            revision = self.approve(result)
            self.cli("task", "apply", "rename", "--revision", revision, "--apply")
            self.assertFalse((self.project / "change.txt").exists())
            self.assertEqual((self.project / "CHANGE.txt").read_text(), "HEAD\n")

    def test_unicode_spelling_rename_is_never_approved_as_an_empty_change(self):
        self.register("claude1", "claude", "claude@example.invalid")
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        for before, after in ((composed + ".txt", decomposed + ".txt"), (composed, decomposed)):
            with self.subTest(before=before):
                original = self.project / before
                if before == composed:
                    original.mkdir()
                    (original / "entry.txt").write_text("baseline\n")
                else:
                    original.write_text("baseline\n")
                self.git("add", "-A")
                self.git("commit", "-qm", "add Unicode path")
                aliases = (self.project / after).exists()
                name = "rename-file" if before.endswith(".txt") else "rename-directory"
                self.submit(name, "claude1", f"rename {before} {after}")
                self.launch()
                if aliases:
                    result = self.wait(name, {"failed"})
                    report = json.loads(Path(result["report_path"]).read_text())
                    self.assertIn("Unicode", report["diagnostic"])
                    self.cli("task", "review", "approve", name, "--revision", result["report"], code=2)
                else:
                    result = self.wait(name, {"review"})
                    report = json.loads(Path(result["report_path"]).read_text())
                    self.assertTrue(report["paths"])
                    revision = self.approve(result)
                    self.cli("task", "apply", name, "--revision", revision, "--apply")
                    self.assertTrue((self.project / after).exists())

    def test_quota_handoff_preserves_checkpoint(self):
        self.register("claude1", "claude", "claude@example.invalid")
        self.register("claude2", "claude", "claude@example.invalid")
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("move", "claude1", "edit change.txt partial\nquota-fixture",
                    "--handoff-to", "codex1", "--handoff-to", "claude2")
        self.launch()
        status = self.wait("move", {"waiting_quota"})
        material = self.cli("task", "handoff", "prepare", "move")
        brief = self.root / "handoff.md"
        brief.write_text("require change.txt partial\nedit change.txt final")
        self.cli("task", "handoff", "start", "move", "--source", material["source"],
                 "--to", "claude2", "--brief", brief, code=2)
        self.cli("task", "handoff", "start", "move", "--source", material["source"],
                 "--to", "codex1", "--brief", brief)
        self.launch()
        done = self.wait("move", {"review"})
        old, new = done["attempts"]
        self.assertEqual(old["instance"], "claude1")
        self.assertEqual(new["instance"], "codex1")
        self.assertEqual(done["id"], status["id"])
        self.assertEqual((Path(new["work"]) / "change.txt").read_text(), "final\n")
        self.approve(done)

    def codex_handoff(self, scenario):
        self.register("source", "codex", "first@example.invalid")
        self.register("target", "codex", "second@example.invalid")
        self.submit("move", "source", "edit change.txt partial\ncodex-quota", "--handoff-to", "target")
        self.launch()
        self.wait("move", {"waiting_quota"})
        if scenario == "retry":
            self.cli("task", "retry", "move")
            self.assertEqual(self.store.get("tasks", "move")["target"], "source")
            return
        material = self.cli("task", "handoff", "prepare", "move")
        rollout = Path("sessions/2026/01/02/rollout-fixture-codex-session.jsonl")
        if scenario != "missing":
            path = self.store.instance_dir("source") / "cli" / rollout
            path.parent.mkdir(parents=True)
            path.write_text("fixture history")
        brief = self.root / "handoff.md"
        brief.write_text("require change.txt partial\nedit change.txt final")
        home = self.store.instance_dir("target") / "cli"
        if scenario == "source-cwd":
            (home / "fixture-source-cwd").write_text(self.store.get("attempts", material["source"])["cwd"])
        (home / "fixture-migration").write_text(scenario)
        self.cli("task", "handoff", "start", "move", "--source", material["source"],
                 "--to", "target", "--brief", brief)
        self.launch()
        if scenario in ("tool-error", "quota"):
            state = "failed" if scenario == "tool-error" else "waiting_quota"
            attempt = self.wait("move", {state})["attempts"][-1]
            self.assertEqual(attempt["handoff_mode"], "migrated")
            self.assertEqual((home / "fixture-methods").read_text().splitlines(), ["thread/resume"])
            self.assertNotIn("handoff_migration_fallback", attempt)
            return
        done = self.wait("move", {"review"})
        attempt = done["attempts"][-1]
        self.assertEqual((Path(attempt["work"]) / "change.txt").read_text(), "final\n")
        self.assertEqual(attempt["handoff_mode"], "migrated" if scenario == "success" else "brief")
        methods = (home / "fixture-methods").read_text().splitlines()
        self.assertEqual(methods, {"success": ["thread/resume"], "missing": ["thread/start"],
                                   "source-cwd": ["thread/resume", "thread/start"],
                                   "missing-profile": ["thread/resume", "thread/start"],
                                   "full-window-missing-profile": ["thread/resume", "thread/start"],
                                   "reject": ["thread/resume", "thread/start"],
                                   "full-window-turn-rejected": ["thread/resume", "thread/start"],
                                   "full-window-error": ["thread/resume", "thread/start"],
                                   "retried-429-then-fail": ["thread/resume", "thread/start"],
                                   "turn-error": ["thread/resume", "thread/start"]}[scenario])
        if scenario != "success":
            report = json.loads(Path(done["report_path"]).read_text())
            self.assertIn(attempt["handoff_migration_fallback"], report["diagnostic"])
            events = [e for e in self.store.events("move", 0) if e["type"] == "handoff_migration_fallback"]
            self.assertEqual([e["reason"] for e in events], [attempt["handoff_migration_fallback"]])
            if scenario == "source-cwd":
                self.assertIn("非任务工作目录", events[0]["reason"])
            elif scenario == "missing-profile":
                self.assertIn("未确认受管权限配置", events[0]["reason"])

    def test_quota_retry_keeps_source(self):
        self.codex_handoff("retry")

    def test_codex_handoff_resumes_copied_rollout(self):
        self.codex_handoff("success")

    def test_codex_handoff_missing_rollout(self):
        self.codex_handoff("missing")

    def test_codex_handoff_resume_rejected(self):
        self.codex_handoff("reject")

    def test_P6_resume_validation_failure_preserves_target_quota(self):
        self.codex_handoff("full-window-missing-profile")
        target = self.store.get("instances", "target")
        self.assertEqual(controller.current_quota(self.store, target, time.time()), (True, True, 100))

    def test_codex_handoff_resume_source_cwd(self):
        self.codex_handoff("source-cwd")

    def test_codex_handoff_resume_missing_profile(self):
        self.codex_handoff("missing-profile")

    def test_P6_handoff_fallback_preserves_target_quota(self):
        self.codex_handoff("full-window-error")
        target = self.store.get("instances", "target")
        self.assertEqual(controller.current_quota(self.store, target, time.time()), (True, True, 100))

    def test_P6_rejected_turn_start_preserves_target_quota(self):
        self.codex_handoff("full-window-turn-rejected")
        target = self.store.get("instances", "target")
        self.assertEqual(controller.current_quota(self.store, target, time.time()), (True, True, 100))

    def test_P9_retried_429_does_not_skip_brief_fallback(self):
        self.codex_handoff("retried-429-then-fail")

    def test_codex_handoff_first_turn_rejected(self):
        self.codex_handoff("turn-error")

    def test_codex_handoff_tool_error_keeps_session(self):
        self.codex_handoff("tool-error")

    def test_codex_handoff_quota_keeps_session(self):
        self.codex_handoff("quota")

    def test_question_cancel_and_worker_restart(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("ask", "codex1", "ask-fixture\nedit change.txt blue")
        self.launch()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            status = self.cli("task", "status", "ask")
            if status["pending"]:
                break
            time.sleep(.08)
        self.assertTrue(status["pending"], status)
        self.launch()
        req_id = status["pending"][0]["id"]
        self.cli("task", "decide", "ask", req_id, "answer", "--answers", '{"color":"blue"}')
        done = self.wait("ask", {"review"})
        self.assertEqual(len(done["attempts"]), 1)

        self.submit("cancelled", "codex1", "wait-for-cancel")
        self.launch()
        self.wait("cancelled", {"running"})
        self.cli("task", "cancel", "cancelled")
        self.wait("cancelled", {"cancelled"})

    def test_image_and_code_run_on_one_instance(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("code", "codex1", "wait-for-cancel")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--prompt", "image fixture")
        self.launch()
        self.wait("code", {"running"})
        done = self.wait("image", {"review"})
        revision = self.approve(done)
        output = self.root / "delivered.png"
        result = self.cli("task", "export", "image", "--revision", revision, "--output", output)
        self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.cli("task", "cancel", "code")
        self.wait("code", {"cancelled"})

    def test_P7_image_turn_start_rate_limit_waits_for_quota(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--instance", "codex1",
                 "--prompt", "reject-turn-at-rate-limit")
        self.launch()
        done = self.wait("image", {"waiting_quota"})
        self.assertEqual(done["attempts"][-1]["outcome"], "quota")

    def test_P7_image_turn_start_too_many_requests_waits_for_quota(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--instance", "codex1",
                 "--prompt", "reject-turn-too-many")
        self.launch()
        done = self.wait("image", {"waiting_quota"})
        self.assertEqual(done["attempts"][-1]["outcome"], "quota")

    def image_limit_after_start(self, prompt):
        self.register("codex1", "codex", "codex1@example.invalid")
        self.register("codex2", "codex", "codex2@example.invalid")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--instance", "codex1",
                 "--instance", "codex2", "--prompt", prompt)
        self.launch()
        done = self.wait("image", {"waiting_quota", "failed"})
        self.assertEqual(done["state"], "waiting_quota")
        source = done["attempts"][-1]
        self.assertEqual(source["outcome"], "quota")
        self.cli("task", "retry", "image")
        self.assertEqual(self.store.get("tasks", "image")["target"], source["instance"])

    def test_P8_image_terminal_limit_notice_after_start(self):
        self.image_limit_after_start("image-terminal-limit")

    def test_P11_terminal_limit_then_connection_drop(self):
        self.image_limit_after_start("image-limit-then-drop")

    def test_P8_image_failed_turn_http_429(self):
        self.image_limit_after_start("image-failed-429")

    def test_code_failed_turn_message_quota(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("code", "codex1", "code-failed-429")
        self.launch()
        done = self.wait("code", {"waiting_quota", "failed"})
        self.assertEqual(done["state"], "waiting_quota")
        self.assertEqual(done["attempts"][-1]["outcome"], "quota")

    def test_P10_code_turn_ok_after_retried_rate_limit(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("code", "codex1", "retry-code-rate-limit\nedit change.txt done")
        self.launch()
        done = self.wait("code", {"review", "waiting_quota", "failed"})
        self.assertEqual(done["state"], "review")
        self.assertEqual(done["attempts"][-1]["outcome"], "ok")

    def test_image_kept_after_retried_rate_limit(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--instance", "codex1",
                 "--prompt", "retry-image-rate-limit")
        self.launch()
        done = self.wait("image", {"review", "waiting_quota"})
        self.assertEqual(done["attempts"][-1]["outcome"], "ok")
        report = json.loads(Path(done["report_path"]).read_text())
        image, = report["artifacts"]
        self.assertEqual(backend.image_format(Path(image["path"])), ".png")

    def test_image_export_recovers_only_its_published_file(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "image", "--kind", "image", "--prompt", "image fixture")
        self.launch()
        revision = self.approve(self.wait("image", {"review"}))
        first, second = self.root / "first.png", self.root / "second.png"
        for output in (first, second):
            with patch.object(self.store, "event", side_effect=OSError("commit step interrupted")):
                with self.assertRaisesRegex(OSError, "commit step interrupted"):
                    controller.export_image(self.store, "image", revision, output)
            self.assertTrue(output.is_file())
            self.assertIn("export_intent", self.store.get("tasks", "image"))
            if output == second:
                outsider = self.root / "outsider.png"
                shutil.copyfile(output, outsider)
                outsider.replace(output)
                self.cli("task", "export", "image", "--revision", revision, "--output", output, code=2)
            else:
                result = self.cli("task", "export", "image", "--revision", revision, "--output", output)
                self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(self.store.get("tasks", "image")["export"], str(first))
            self.assertNotIn("export_intent", self.store.get("tasks", "image"))

    def test_image_batch_reuses_only_matching_submissions(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("covers-1", "codex1", "edit change.txt unrelated")
        prompts = self.root / "prompts"
        prompts.mkdir()
        first = prompts / "art.txt"
        first.write_text("first image")
        (prompts / "scene.txt").write_text("second image")
        reference = self.root / "reference.png"
        reference.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
        batch = ("task", "submit", "--kind", "image", "--batch-dir", prompts, "--reference", reference)
        self.cli(*batch, "--id", "covers", code=2)
        self.assertEqual(self.store.get("tasks", "covers-1")["kind"], "code")
        self.assertIsNone(self.store.get("tasks", "covers-2", required=False))
        created = self.cli(*batch, "--id", "pictures")
        self.assertEqual(self.cli(*batch, "--id", "pictures"), created)
        self.cli(*batch, "--id", "pictures", "--timeout", 30, code=2)
        first.write_text("replacement image")
        self.cli(*batch, "--id", "pictures", code=2)
        first.write_text("first image")
        reference.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
        self.cli(*batch, "--id", "pictures", code=2)
        reference.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
        first.rename(prompts / "00-art.txt")
        self.cli(*batch, "--id", "pictures", code=2)

    def test_image_batch_validates_members_and_late_collisions_before_enqueue(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("covers-2", "codex1", "edit change.txt unrelated")
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "art.txt").write_text("first image")
        scene = prompts / "scene.txt"
        scene.write_text("second image")
        batch = ("task", "submit", "--kind", "image", "--batch-dir", prompts)
        self.cli(*batch, "--id", "covers", code=2)
        self.assertIsNone(self.store.get("tasks", "covers-1", required=False))
        self.assertFalse(self.store.task_dir("covers-1").exists())
        self.assertFalse(self.store.events("covers-1", 0))

        accepted = self.cli(*batch, "--id", "pictures")
        scene.unlink()
        self.cli(*batch, "--id", "pictures", code=2)
        scene.write_text("second image")
        extra = prompts / "third.txt"
        extra.write_text("third image")
        self.cli(*batch, "--id", "pictures", code=2)
        self.assertIsNone(self.store.get("tasks", "pictures-3", required=False))
        extra.unlink()
        self.assertEqual(self.cli(*batch, "--id", "pictures"), accepted)

    def test_image_batch_recovers_after_process_exits_in_transaction(self):
        self.register("codex1", "codex", "codex@example.invalid")
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "art.txt").write_text("first image")
        (prompts / "scene.txt").write_text("second image")
        args = ["task", "submit", "--kind", "image", "--batch-dir", str(prompts), "--id", "pictures"]
        script = """
import os
import sys
sys.path.insert(0, sys.argv.pop(1))
import delegate
from cli_manager.store import Store
record = Store.event

def interrupt(store, task, kind, **data):
    record(store, task, kind, **data)
    if kind == "submitted":
        os._exit(71)

Store.event = interrupt
delegate.submit(Store(), delegate.parse(sys.argv[1:]))
"""
        crash = subprocess.run(
            [sys.executable, "-B", "-c", script, str(ROOT / "scripts"), *args], env=self.env, timeout=15,
        )
        self.assertEqual(crash.returncode, 71)
        self.assertFalse(self.store.rows("tasks"))
        self.assertTrue(self.store.task_dir("pictures-1").is_dir())
        self.assertTrue(self.store.task_dir("pictures-2").is_dir())
        recovered = self.cli(*args)
        self.assertEqual([task["id"] for task in recovered], ["pictures-1", "pictures-2"])
        for task in recovered:
            self.assertEqual([event["type"] for event in self.store.events(task["id"], 0)], ["submitted"])

    def test_non_git_drift_and_recovery(self):
        self.register("claude1", "claude", "claude@example.invalid")
        project = self.root / "notes"
        project.mkdir()
        original = project / "change.txt"
        original.write_text("base\n")
        self.submit("notes", "claude1", "edit change.txt new", project=project)
        self.launch()
        status = self.wait("notes", {"review"})
        self.assertFalse((Path(status["attempts"][-1]["work"]) / ".git").exists())
        revision = self.approve(status)
        original.write_text("concurrent edit\n")
        self.assertEqual(self.cli("task", "apply", "notes", "--revision", revision, "--check", code=1)
                         ["conflicts"][0]["path"], "change.txt")
        original.write_text("base\n")
        self.cli("task", "apply", "notes", "--revision", revision, "--apply")
        self.assertEqual(original.read_text(), "new\n")

    def test_non_git_checkpoint_reports_new_files_ignored_by_gitignore(self):
        self.register("claude1", "claude", "claude@example.invalid")
        project = self.root / "notes"
        project.mkdir()
        (project / ".gitignore").write_text("new.txt\n__pycache__/\n")
        (project / ".gitattributes").write_text("*.txt text eol=crlf\n")
        (project / "base.txt").write_bytes(b"plain\n")
        self.submit("notes", "claude1",
                    "edit new.txt delivered\nmkdir nested/__pycache__\nedit nested/__pycache__/cache.pyc temporary",
                    project=project)
        self.launch()
        review = self.wait("notes", {"review"})
        self.assertEqual(json.loads(Path(review["report_path"]).read_text())["paths"], ["new.txt"])
        self.assertEqual((Path(review["attempts"][-1]["work"]) / "base.txt").read_bytes(), b"plain\n")
        revision = self.approve(review)
        self.cli("task", "apply", "notes", "--revision", revision, "--apply")
        self.assertEqual((project / "new.txt").read_bytes(), b"delivered\n")
        self.assertFalse((project / "nested").exists())

    def test_apply_blocks_new_link_if_target_now_leaves_project(self):
        self.register("claude1", "claude", "claude@example.invalid")
        internal = self.project / "internal"
        internal.mkdir()
        (internal / "sample.txt").write_text("baseline\n")
        self.git("add", "internal")
        self.git("commit", "-qm", "add internal directory")
        self.submit("link", "claude1", "symlink alias.txt internal/sample.txt")
        self.launch()
        revision = self.approve(self.wait("link", {"review"}))
        external = self.root / "external"
        internal.rename(external)
        internal.symlink_to(external, target_is_directory=True)
        preview = self.cli("task", "apply", "link", "--revision", revision, "--check", code=1)
        self.assertEqual(preview["conflicts"][0]["path"], "alias.txt")
        self.cli("task", "apply", "link", "--revision", revision, "--apply", code=2)
        self.assertFalse((self.project / "alias.txt").is_symlink())

    def test_apply_preserves_original_paths_renamed_to_aliases_after_approval(self):
        self.register("claude1", "claude", "claude@example.invalid")
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        folder = self.project / composed
        folder.mkdir()
        (folder / "item.txt").write_text("baseline\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "add folder")
        self.submit("delete", "claude1", f"remove change.txt\nremove {composed}/item.txt")
        self.launch()
        revision = self.approve(self.wait("delete", {"review"}))
        old = self.project / "change.txt"
        renamed = old.rename(self.project / ".renaming").rename(self.project / "CHANGE.txt")
        renamed_folder = folder.rename(self.project / ".renaming").rename(self.project / decomposed)
        preview = self.cli("task", "apply", "delete", "--revision", revision, "--check", code=1)
        self.assertIn("change.txt", {item["path"] for item in preview["conflicts"]})
        if old.exists():
            self.assertTrue(any("大小写或 Unicode" in item["reason"] for item in preview["conflicts"]))
        self.cli("task", "apply", "delete", "--revision", revision, "--apply", code=2)
        self.assertIn("CHANGE.txt", os.listdir(self.project))
        self.assertIn(decomposed, os.listdir(self.project))
        self.assertEqual(renamed.read_text(), "HEAD\n")
        self.assertEqual((renamed_folder / "item.txt").read_text(), "baseline\n")

    def test_apply_and_recovery_recheck_exact_spelling_after_preview(self):
        old = self.project / "change.txt"
        renamed = self.project / "CHANGE.txt"
        if not renamed.exists():
            self.skipTest("requires a case-insensitive volume")
        before = workspace.entry(self.project, "change.txt")
        rows = [{"path": "change.txt", "before": before, "after": None}]
        journal_path = self.root / "apply.json"
        write_journal = workspace.write_json

        def rename_during_application(path, journal):
            write_journal(path, journal)
            if journal["started"] == 1 and journal["status"] == "applying" and not journal.get("untouched"):
                old.rename(self.project / ".renaming").rename(renamed)

        with patch.object(workspace, "write_json", side_effect=rename_during_application):
            with self.assertRaisesRegex(workspace.DelegateError, "别名"):
                workspace.apply_application(self.project, rows, journal_path, "approved")
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal["status"], "conflict")
        self.assertEqual(workspace.recover_application(self.project, journal, journal_path), ["change.txt"])
        self.assertIn("CHANGE.txt", os.listdir(self.project))
        self.assertEqual(renamed.read_text(), "HEAD\n")

    def test_application_preserves_an_editor_write_during_journal_sync(self):
        path = self.project / "change.txt"
        before = workspace.entry(self.project, path.name)
        rows = [{"path": path.name, "before": before, "after": None}]
        write_journal = workspace.write_json
        for change in ("new bytes", "same bytes new inode", "external deletion"):
            with self.subTest(change=change):
                path.write_text("HEAD\n")
                journal_path = self.root / "apply.json"
                inode = None

                def save_during_journal(destination, journal):
                    nonlocal inode
                    write_journal(destination, journal)
                    if journal["started"] != 1 or journal.get("untouched"):
                        return
                    if change == "external deletion":
                        path.unlink()
                    elif change == "new bytes":
                        path.write_text("user saved\n")
                    else:
                        replacement = self.project / "editor-save"
                        replacement.write_text("HEAD\n")
                        inode = replacement.stat().st_ino
                        replacement.replace(path)

                with patch.object(workspace, "write_json", side_effect=save_during_journal):
                    with self.assertRaises(workspace.WriteConflict):
                        workspace.apply_application(self.project, rows, journal_path, "approved")
                if change == "external deletion":
                    self.assertFalse(path.exists())
                elif change == "new bytes":
                    self.assertEqual(path.read_text(), "user saved\n")
                else:
                    self.assertEqual(path.stat().st_ino, inode)

    def test_recovery_preserves_editor_write_while_preparing_replacement(self):
        path = self.project / "change.txt"
        before = workspace.entry(self.project, path.name)
        path.write_text("task wrote\n")
        after = workspace.entry(self.project, path.name)
        journal_path = self.root / "apply.json"
        journal = {"status": "applying", "started": 1,
                   "paths": [{"path": path.name, "before": before, "after": after}]}
        sync = os.fsync
        saved = False

        def save_while_syncing(fd):
            nonlocal saved
            if not saved:
                path.write_text("user saved\n")
                saved = True
            return sync(fd)

        with patch.object(workspace.os, "fsync", side_effect=save_while_syncing):
            self.assertEqual(workspace.recover_application(self.project, journal, journal_path), [path.name])
        self.assertEqual(path.read_text(), "user saved\n")
        self.assertEqual(json.loads(journal_path.read_text())["status"], "conflict")

    def test_new_file_publication_recovers_on_both_sides_of_rename(self):
        journal_path = self.root / "apply.json"
        script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from cli_manager import workspace
publish = workspace.rename_new
save_journal = workspace.write_json

root, journal, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

def interrupt_publish(source, target):
    if target.name != "created.txt":
        return publish(source, target)
    if mode == "after":
        publish(source, target)
    os._exit(71)

workspace.rename_new = interrupt_publish
def interrupt_receipt(path, log):
    if mode == "allocation" and log["started"] == 0 and log.get("scratch_id"):
        os._exit(71)
    if mode == "unrecorded" and log["started"] == 2 and log["paths"][1].get("forward", {}).get("post_id"):
        os._exit(71)
    save_journal(path, log)

workspace.write_json = interrupt_receipt
if mode == "allocation":
    workspace.application_scratch_parent = lambda root, *_: root
rows = []
if mode in {"nested", "unrecorded"}:
    rows.append({"path": "generated", "before": None, "after": {"type": "directory"}})
name = "generated/created.txt" if rows else "created.txt"
rows.append({"path": name, "before": None,
             "after": {"type": "file", "executable": False, "data": "Y3JlYXRlZAo="}})
workspace.apply_application(root, rows, journal, "approved")
"""
        before = set(os.listdir(self.project))
        for mode in ("after", "nested", "unrecorded", "allocation", "foreign"):
            with self.subTest(mode=mode):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(ROOT / "scripts"), self.project, journal_path, mode],
                    env=self.env, timeout=15,
                )
                self.assertEqual(child.returncode, 71)
                created = self.project / "created.txt"
                if mode == "after":
                    self.assertEqual(created.stat().st_nlink, 1)
                if mode == "foreign":
                    created.write_text("created\n")
                    inode = created.stat().st_ino
                journal = json.loads(journal_path.read_text())
                conflicts = workspace.recover_application(self.project, journal, journal_path)
                if mode == "foreign":
                    self.assertEqual(conflicts, [created.name])
                    self.assertEqual(created.stat().st_ino, inode)
                    self.assertEqual(created.read_text(), "created\n")
                    created.unlink()
                else:
                    self.assertEqual(conflicts, [])
                self.assertEqual(set(os.listdir(self.project)), before)

    def test_recovery_restores_a_removed_file_after_process_exits(self):
        journal_path = self.root / "apply.json"
        original = self.project / "change.txt"
        inode = original.stat().st_ino
        script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from cli_manager import workspace
rename = workspace.rename_new
root, journal = map(Path, sys.argv[1:])

def interrupt_after_move(source, target):
    rename(source, target)
    assert source == root / "change.txt"
    os._exit(71)

workspace.rename_new = interrupt_after_move
rows = [{"path": "change.txt", "before": workspace.entry(root, "change.txt"), "after": None}]
workspace.apply_application(root, rows, journal, "approved")
"""
        child = subprocess.run(
            [sys.executable, "-B", "-c", script, str(ROOT / "scripts"), self.project, journal_path],
            env=self.env, timeout=15,
        )
        self.assertEqual(child.returncode, 71)
        self.assertFalse(original.exists())
        journal = json.loads(journal_path.read_text())
        scratch = Path(journal["scratch"])
        self.assertFalse(scratch.is_relative_to(self.project))
        original.write_text("HEAD\n")
        self.assertEqual(workspace.recover_application(self.project, journal, journal_path), ["change.txt"])
        self.assertNotEqual(original.stat().st_ino, inode)
        self.assertTrue(scratch.is_dir())
        original.unlink()
        self.assertEqual(workspace.recover_application(self.project, journal, journal_path), [])
        self.assertEqual(original.stat().st_ino, inode)
        self.assertEqual(original.read_text(), "HEAD\n")
        self.assertFalse(scratch.exists())

    def test_exclusive_publication_preserves_an_already_saved_file(self):
        existing, staged = self.project / "existing.txt", self.project / "staged.txt"
        existing.write_text("editor saved\n")
        staged.write_text("task output\n")
        with self.assertRaises(FileExistsError):
            workspace.rename_new(staged, existing)
        self.assertEqual(existing.read_text(), "editor saved\n")
        self.assertEqual(staged.read_text(), "task output\n")

    def test_application_retains_original_inode_until_confirmed_cleanup(self):
        self.register("claude1", "claude", "claude@example.invalid")
        source = self.project / "change.txt"
        self.submit("remove", "claude1", "remove change.txt")
        self.launch()
        revision = self.approve(self.wait("remove", {"review"}))
        with source.open("r+b") as editor:
            result = self.cli("task", "apply", "remove", "--revision", revision, "--apply")
            staging = Path(result["retained"])
            stash = staging / "forward-0"
            self.assertFalse(source.exists())
            self.assertEqual(stash.stat().st_ino, os.fstat(editor.fileno()).st_ino)
            self.assertEqual(self.cli("task", "cleanup", "remove")["application_staging"], str(staging))
            original = stash.stat()
            editor.write(b"EDIT\n")
            editor.flush()
            os.fsync(editor.fileno())
            os.utime(stash, ns=(original.st_atime_ns, original.st_mtime_ns))
            pending = self.cli("task", "cleanup", "remove")
            self.assertFalse(pending["eligible"])
            self.assertIn(str(stash), pending["conflicts"][0])
            self.cli("task", "cleanup", "remove", "--confirm", code=2)
            self.assertEqual(stash.read_bytes(), b"EDIT\n")

    def test_replacement_keeps_the_original_inode_and_concurrent_edits(self):
        source = self.project / "change.txt"
        with source.open("r+b") as editor:
            before = workspace.entry(self.project, source.name)
            after = {**before, "data": "dGFzayByZXN1bHQK"}
            rows = [{"path": source.name, "before": before, "after": after}]
            journal_path = self.root / "apply.json"
            write_journal = workspace.write_json

            def save_before_settlement(path, document):
                if document["status"] == "applied":
                    editor.write(b"editor update\n")
                    editor.truncate()
                    editor.flush()
                write_journal(path, document)

            with patch.object(workspace, "write_json", side_effect=save_before_settlement):
                scratch = workspace.apply_application(self.project, rows, journal_path, "approved")
            retained = scratch / "forward-0"
            self.assertEqual(retained.stat().st_ino, os.fstat(editor.fileno()).st_ino)
            self.assertEqual(retained.read_bytes(), b"editor update\n")
            self.assertEqual(source.read_bytes(), b"task result\n")
            with self.assertRaises(workspace.WriteConflict):
                workspace.cleanup_application(json.loads(journal_path.read_text()))
            self.assertEqual(retained.read_bytes(), b"editor update\n")

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS extended attributes and ACLs")
    def test_cleanup_preserves_extended_attribute_and_acl_changes(self):
        source = self.project / "change.txt"
        journal_path = self.root / "apply.json"
        rows = [{"path": source.name, "before": workspace.entry(self.project, source.name), "after": None}]
        scratch = workspace.apply_application(self.project, rows, journal_path, "approved")
        retained = scratch / "forward-0"
        journal = json.loads(journal_path.read_text())
        attribute = "com.apple.metadata:_kMDItemUserTags"
        subprocess.run(["/usr/bin/xattr", "-w", attribute, "fixture-tag", retained], check=True)
        with self.assertRaises(workspace.WriteConflict):
            workspace.cleanup_application(journal)
        self.assertEqual(subprocess.check_output(["/usr/bin/xattr", "-p", attribute, retained]).strip(), b"fixture-tag")
        subprocess.run(["/usr/bin/xattr", "-d", attribute, retained], check=True)
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", retained], check=True)
        with self.assertRaises(workspace.WriteConflict):
            workspace.cleanup_application(journal)
        self.assertTrue(retained.is_file())
        subprocess.run(["/bin/chmod", "-N", retained], check=True)
        workspace.cleanup_application(journal)
        self.assertFalse(scratch.exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS extended attributes and ACLs")
    def test_content_replacement_preserves_original_acl_and_byte_names_of_attributes(self):
        self.register("codex1", "codex", "account@example.invalid")
        original = self.project / "change.txt"
        attributes = {"com.apple.metadata:_kMDItemUserTags": "fixture-tag", "com.example.line\nname": "newline-name"}
        for name, value in attributes.items():
            subprocess.run(["/usr/bin/xattr", "-w", name, value, original], check=True)
        subprocess.run(["/bin/chmod", "+a", "nobody deny read", original], check=True)
        before = workspace.file_metadata(original, copyable=True)
        self.submit("replace", "codex1", "edit change.txt updated")
        self.launch()
        revision = self.approve(self.wait("replace", {"review"}))
        self.cli("task", "apply", "replace", "--revision", revision, "--apply")
        self.cli("task", "cleanup", "replace", "--confirm")
        self.assertEqual(original.read_text(), "updated\n")
        self.assertEqual(workspace.file_metadata(original, copyable=True), before)
        for name, value in attributes.items():
            self.assertEqual(subprocess.check_output(["/usr/bin/xattr", "-p", name, original]).strip(), value.encode())

    def test_recovery_restores_replaced_original_inode(self):
        source = self.project / "change.txt"
        before = workspace.entry(self.project, source.name)
        after = {**before, "data": "dGFzayByZXN1bHQK"}
        rows = [{"path": source.name, "before": before, "after": after}]
        journal_path = self.root / "apply.json"
        exchange = workspace.rename_exchange
        inode = source.stat().st_ino

        def interrupt_after_replacement(first, second):
            exchange(first, second)
            raise KeyboardInterrupt

        recover = workspace.recover_application
        with patch.object(workspace, "rename_exchange", side_effect=interrupt_after_replacement), \
                patch.object(workspace, "recover_application", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspace.apply_application(self.project, rows, journal_path, "approved")
        self.assertNotEqual(source.stat().st_ino, inode)
        permissions = source.stat().st_mode & 0o777
        source.chmod(0o600)
        self.assertEqual(recover(self.project, json.loads(journal_path.read_text()), journal_path), [source.name])
        self.assertEqual(source.stat().st_mode & 0o777, 0o600)
        self.assertEqual(source.read_bytes(), b"task result\n")
        source.chmod(permissions)
        self.assertEqual(recover(self.project, json.loads(journal_path.read_text()), journal_path), [])
        self.assertEqual(source.stat().st_ino, inode)
        self.assertEqual(source.read_text(), "HEAD\n")

    def test_cleaning_application_rechecks_each_deleted_entry(self):
        names = ("change.txt", "untouched.txt")
        rows = [{"path": name, "before": workspace.entry(self.project, name), "after": None} for name in names]
        journal_path = self.root / "apply.json"
        with (self.project / names[0]).open("r+b") as first, (self.project / names[1]).open("r+b") as second:
            scratch = workspace.apply_application(self.project, rows, journal_path, "approved")
            journal = json.loads(journal_path.read_text())
            stamp = workspace.path_stamp
            opened = {scratch / "forward-0": first, scratch / "forward-1": second}
            visited = []

            def save_during_inspection(path):
                if path in opened and path not in visited:
                    visited.append(path)
                    if len(visited) == 2:
                        earlier = opened[visited[0]]
                        earlier.write(b"editor update\n")
                        earlier.truncate()
                        earlier.flush()
                return stamp(path)

            with patch.object(workspace, "path_stamp", side_effect=save_during_inspection):
                with self.assertRaises(workspace.WriteConflict):
                    workspace.cleanup_application(journal)
            self.assertEqual(visited[0].read_bytes(), b"editor update\n")

    def test_application_preview_checks_same_volume_staging_and_can_use_project_root(self):
        self.register("claude1", "claude", "claude@example.invalid")
        self.submit("mount", "claude1", "remove change.txt")
        self.launch()
        accepted = self.approve(self.wait("mount", {"review"}))
        with patch.object(workspace.os, "access", return_value=False):
            result = controller.apply_task(self.store, "mount", accepted, "check")
            self.assertEqual(result["state"], "blocked")
            self.assertIn("同一卷", result["conflicts"][0]["reason"])
        access = os.access
        git_dir = self.project / ".git"
        with patch.object(workspace.os, "access", side_effect=lambda path, mode: path == git_dir and access(path, mode)):
            preview = controller.apply_task(self.store, "mount", accepted, "check")
            self.assertEqual(preview["staging_parent"], str(git_dir))
            result = controller.apply_task(self.store, "mount", accepted, "apply")
        staging = Path(result["retained"])
        self.assertEqual(staging.parent, git_dir)
        self.assertTrue(workspace.protected(workspace.PurePosixPath(staging.name)))
        self.git("add", "-A")
        self.assertEqual(self.git("diff", "--cached", "--name-status"), b"D\tchange.txt\n")
        self.cli("task", "cleanup", "mount", "--confirm")
        self.assertFalse(staging.exists())

    def test_apply_replaces_files_with_directories_and_reverses_safely(self):
        self.register("claude1", "claude", "claude@example.invalid")
        nested = self.project / "reverse" / "nested"
        nested.mkdir(parents=True)
        (nested / "item.txt").write_text("baseline\n")
        self.git("add", "reverse")
        self.git("commit", "-qm", "add nested baseline directory")
        self.submit("shape", "claude1", "\n".join((
            "remove change.txt", "mkdir change.txt/new", "edit change.txt/new/item.txt delivered",
            "remove reverse/nested/item.txt", "remove reverse/nested", "remove reverse", "edit reverse collapsed",
        )))
        self.launch()
        revision = self.approve(self.wait("shape", {"review"}))
        untracked = nested / "keep.txt"
        untracked.write_text("do not touch\n")
        preview = self.cli("task", "apply", "shape", "--revision", revision, "--check", code=1)
        self.assertIn("reverse/nested", {conflict["path"] for conflict in preview["conflicts"]})
        self.assertEqual(untracked.read_text(), "do not touch\n")
        untracked.unlink()
        index = (self.project / ".git/index").read_bytes()
        original_install = workspace.install

        def interrupt(root, relative, value, **kwargs):
            if relative == "reverse" and value and value["type"] == "file":
                raise OSError("fixture interruption")
            return original_install(root, relative, value, **kwargs)

        with patch.object(workspace, "install", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "fixture interruption"):
                controller.apply_task(self.store, "shape", revision, "apply")
        self.assertEqual((self.project / "change.txt").read_text(), "HEAD\n")
        self.assertEqual((nested / "item.txt").read_text(), "baseline\n")

        write_journal = workspace.write_json

        def interrupt_recovery(path, journal):
            if len(journal.get("recovered", ())) == 3:
                raise KeyboardInterrupt
            write_journal(path, journal)

        with patch.object(workspace, "install", side_effect=interrupt), \
                patch.object(workspace, "write_json", side_effect=interrupt_recovery):
            with self.assertRaises(KeyboardInterrupt):
                controller.apply_task(self.store, "shape", revision, "apply")
        self.assertEqual(self.cli("task", "apply", "shape", "--recover")["state"], "approved")
        self.assertEqual((self.project / "change.txt").read_text(), "HEAD\n")
        self.assertEqual((nested / "item.txt").read_text(), "baseline\n")
        self.cli("task", "apply", "shape", "--revision", revision, "--apply")
        self.assertEqual((self.project / "change.txt/new/item.txt").read_text(), "delivered\n")
        self.assertEqual((self.project / "reverse").read_text(), "collapsed\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)

    def test_conflict_integration_copies_both_original_file_shapes(self):
        self.register("claude1", "claude", "claude@example.invalid")
        folder = self.project / "collection"
        folder.mkdir()
        (folder / "item.txt").write_text("original\n")
        self.git("add", "collection")
        self.git("commit", "-qm", "add collection")
        self.submit("shapes", "claude1", "\n".join((
            "remove change.txt", "mkdir change.txt", "edit change.txt/child.txt task",
            "remove collection/item.txt", "remove collection", "edit collection task",
        )))
        self.launch()
        revision = self.approve(self.wait("shapes", {"review"}))
        (self.project / "change.txt").write_text("local file\n")
        (folder / "item.txt").write_text("local item\n")
        self.git("add", "change.txt")
        index = (self.project / ".git/index").read_bytes()
        self.cli("task", "apply", "shapes", "--revision", revision, "--check", code=1)
        integration = self.cli("task", "apply", "shapes", "--revision", revision, "--integrate")
        work = Path(integration["work"])
        workspace.git(work, "reset", "--hard", "HEAD")
        self.assertEqual((work / "change.txt").read_text(), "local file\n")
        self.assertEqual((work / "collection/item.txt").read_text(), "local item\n")
        (work / "change.txt").unlink()
        (work / "change.txt").mkdir()
        (work / "change.txt/child.txt").write_text("local file and task\n")
        shutil.rmtree(work / "collection")
        (work / "collection").write_text("local item and task\n")
        workspace.git(work, "add", "-A")
        recorded = self.cli("task", "apply", "shapes", "--revision", revision, "--record-integration")
        approved = self.approve(recorded)
        self.cli("task", "apply", "shapes", "--revision", approved, "--apply")
        self.assertEqual((self.project / "change.txt/child.txt").read_text(), "local file and task\n")
        self.assertEqual((self.project / "collection").read_text(), "local item and task\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)

    def test_git_apply_compares_and_installs_checkout_bytes(self):
        self.register("claude1", "claude", "claude@example.invalid")
        sub = self.project / "subdir"
        sub.mkdir()
        (sub / ".gitattributes").write_text("attributes.txt text eol=crlf\n")
        attributes_file = sub / "attributes.txt"
        configured_file = self.project / "configured.txt"
        for original in (attributes_file, configured_file):
            original.write_bytes(b"baseline\r\n")
        self.git("config", "core.autocrlf", "true")
        self.git("add", "subdir", "configured.txt")
        self.git("commit", "-qm", "add files with CRLF checkout policy")
        self.submit("line-endings", "claude1", "edit subdir/attributes.txt delegated\nedit configured.txt delegated")
        self.launch()
        revision = self.approve(self.wait("line-endings", {"review"}))
        check = self.cli("task", "apply", "line-endings", "--revision", revision, "--check")
        self.assertEqual(check["state"], "ready")
        self.cli("task", "apply", "line-endings", "--revision", revision, "--apply")
        for original in (attributes_file, configured_file):
            self.assertEqual(original.read_bytes(), b"delegated\r\n")

    def test_dependencies_release_before_apply(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("upper", "codex1", "edit change.txt upper")
        self.submit("lower", "codex1", "require change.txt upper\nedit extra.txt lower", "--depends-on", "upper")
        self.launch()
        up = self.wait("upper", {"review"})
        self.assertEqual(self.cli("task", "status", "lower")["state"], "queued")
        rev_up = self.approve(up)
        self.launch()
        lower = self.wait("lower", {"review"})
        rev_low = self.approve(lower)
        self.cli("task", "apply", "lower", "--revision", rev_low, "--apply", code=2)
        self.cli("task", "apply", "upper", "--revision", rev_up, "--apply")
        self.cli("task", "apply", "lower", "--revision", rev_low, "--apply")
        self.assertEqual((self.project / "extra.txt").read_text(), "lower\n")

    def test_recover_killed_supervisor_and_resume(self):
        self.register("claude1", "claude", "account@example.invalid")
        self.submit("crash", "claude1", "edit change.txt checkpoint\nwait-for-cancel")
        self.launch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            attempt = self.cli("task", "status", "crash")["attempts"][-1]
            if attempt.get("work") and (Path(attempt["work"]) / "change.txt").read_text() == "checkpoint\n":
                break
            time.sleep(.08)
        self.assertEqual((Path(attempt["work"]) / "change.txt").read_text(), "checkpoint\n")
        controller.stop_group(attempt["pid"], signal.SIGKILL)
        self.launch()
        stopped = self.wait("crash", {"interrupted"})
        self.assertEqual(stopped["attempts"][0]["session"], "claude-session")
        self.assertTrue(stopped["attempts"][0]["checkpoint"])
        self.cli("task", "resume", "crash", "--note", "edit change.txt resumed")
        self.launch()
        resumed = self.wait("crash", {"review"})
        self.assertEqual(len(resumed["attempts"]), 1)
        self.assertEqual((Path(attempt["work"]) / "change.txt").read_text(), "resumed\n")

    def test_background_process_is_drained(self):
        self.register("claude1", "claude", "account@example.invalid")
        self.submit("background", "claude1", "background-fixture")
        self.launch()
        done = self.wait("background", {"review"})
        time.sleep(2)
        self.assertFalse((Path(done["attempts"][0]["work"]) / "late.txt").exists())

    def test_stale_quota_does_not_block_fresh_sibling_account(self):
        first = {"id": "first", "bound_account": "same", "status": {"account": "same", "logged_in": True}}
        second = {"id": "second", "bound_account": "same", "status": {"account": "same", "logged_in": True}}
        now = time.time()
        first["status"]["quota"] = {"explicitly_blocked": True, "checked_at": now - 3600}
        second["status"]["quota"] = {"checked_at": now, "observed_at": now,
                                     "windows": [{"used": 5, "resets_at": now + 300}]}
        self.store.record_quota(first, first["status"]["quota"])
        self.store.record_quota(second, second["status"]["quota"])
        self.assertEqual(controller.current_quota(self.store, second, now), (False, True, 5))

    def test_externally_switched_account_does_not_block_sibling_instance(self):
        self.register("codex1", "codex", "original@example.invalid")
        self.register("codex2", "codex", "original@example.invalid")
        config = self.store.instance_dir("codex1") / "cli"
        self.cli("quota", "codex1")
        (config / "fixture-account").write_text("other@example.invalid")
        (config / "fixture-quota-used").write_text("100")
        self.cli("quota", "codex1")
        switched = self.store.get("instances", "codex1")
        self.assertTrue(switched["status"]["identity_changed"])
        self.assertEqual(switched["status"]["quota"]["windows"][0]["used"], 100)
        self.assertEqual(controller.current_quota(self.store, switched, time.time()), (False, True, 15))
        self.submit("healthy", "codex2", "edit change.txt complete")
        self.launch()
        result = self.wait("healthy", {"review"})
        self.assertEqual(result["attempts"][-1]["instance"], "codex2")

    def set_workspace(self, name, workspace):
        (self.store.instance_dir(name) / "cli" / "fixture-workspace").write_text(workspace)

    def test_email_only_binding_requires_explicit_rebind(self):
        self.register("codex1", "codex", "legacy@example.invalid", legacy=True)
        legacy = backend.account_key("codex", "legacy@example.invalid")
        self.set_workspace("codex1", "ws-1")
        self.cli("quota", "codex1")
        pending = self.store.get("instances", "codex1")
        self.assertEqual(pending["bound_account"], legacy)
        self.assertTrue(pending["status"]["rebind_required"])
        self.assertNotIn("identity_changed", pending["status"])
        listed = self.cli("instance", "list")[0]
        self.assertTrue(listed["status"]["rebind_required"])
        self.assertNotIn("legacy_account", pending["status"])
        task = {"id": "new", "allowed": ["codex1"], "kind": "code", "bindings": {}}
        self.assertIsNone(controller.choose(self.store, task, [pending], [], time.time())[0])
        self.store.put("tasks", {"id": "t", "state": "waiting_quota"})
        self.store.put("attempts", {"id": "old-limit", "task": "t", "instance": "codex1", "account": legacy,
                                    "state": "waiting_quota", "generation": 0,
                                    "limited_until": time.time() + 600})
        self.cli("instance", "rebind", "codex1")
        rebound = self.store.get("instances", "codex1")
        self.assertEqual(rebound["generation"], 1)
        self.assertTrue(rebound["bound_scoped"])
        self.assertEqual(rebound["bound_account"], backend.account_key("codex", "legacy@example.invalid", "ws-1"))
        self.assertNotIn("rebind_required", rebound["status"])
        # Nothing of the email-only generation carries over: its limit no longer applies.
        chosen, _ = controller.choose(self.store, task, [rebound], [], time.time())
        self.assertEqual(chosen["id"], "codex1")
        stale = {"id": "old", "allowed": ["codex1"], "kind": "code", "bindings": {"codex1": 0}}
        self.assertIsNone(controller.choose(self.store, stale, [rebound], [], time.time())[0])

    def test_rebind_refuses_unsafe_or_unneeded_cases(self):
        self.register("codex1", "codex", "legacy@example.invalid", legacy=True)
        self.set_workspace("codex1", "")
        self.cli("instance", "rebind", "codex1", code=2)  # no workspace reported
        self.set_workspace("codex1", "ws-1")
        self.store.put("tasks", {"id": "t", "state": "running"})
        self.store.put("attempts", {"id": "live", "task": "t", "instance": "codex1", "state": "running",
                                    "account": backend.account_key("codex", "legacy@example.invalid"),
                                    "generation": 0})
        self.cli("instance", "rebind", "codex1", code=2)  # active attempt
        live = self.store.get("attempts", "live")
        live["instance"] = "codex2"  # same email, still email-only, on a sibling
        self.store.put("attempts", live)
        self.cli("instance", "rebind", "codex1", code=2)
        live["state"] = "review"
        self.store.put("attempts", live)
        self.cli("instance", "rebind", "codex1")
        self.cli("instance", "rebind", "codex1", code=2)  # already workspace-scoped
        self.assertEqual(self.store.get("instances", "codex1")["generation"], 1)

    def test_same_email_in_two_workspaces_keeps_separate_quota(self):
        self.register("codex1", "codex", "shared@example.invalid", legacy=True)
        self.register("codex2", "codex", "shared@example.invalid", legacy=True)
        self.set_workspace("codex1", "ws-business")
        self.set_workspace("codex2", "ws-personal")
        self.cli("instance", "rebind", "codex1")
        self.cli("instance", "rebind", "codex2")
        (self.store.instance_dir("codex1") / "cli" / "fixture-quota-used").write_text("100")
        self.cli("quota", "codex1")
        self.cli("quota", "codex2")
        business, personal = self.store.get("instances", "codex1"), self.store.get("instances", "codex2")
        self.assertNotEqual(business["bound_account"], personal["bound_account"])
        self.assertTrue(controller.current_quota(self.store, business, time.time())[0])
        self.assertFalse(controller.current_quota(self.store, personal, time.time())[0])
        self.submit("personal", "codex2", "edit change.txt complete")
        self.launch()
        self.assertEqual(self.wait("personal", {"review"})["attempts"][-1]["instance"], "codex2")

    def test_missing_workspace_does_not_fall_back_to_email_key(self):
        self.register("codex1", "codex", "shared@example.invalid", legacy=True)
        self.set_workspace("codex1", "ws-personal")
        self.cli("instance", "rebind", "codex1")
        bound = self.store.get("instances", "codex1")["bound_account"]
        self.set_workspace("codex1", "")
        for _ in range(2):
            self.cli("quota", "codex1")
            instance = self.store.get("instances", "codex1")
            self.assertEqual(instance["bound_account"], bound)
            self.assertFalse(instance["status"]["logged_in"])
            self.assertTrue(instance["probe_error"])
            self.assertEqual(instance["status"]["quota"]["windows"], [])

    def test_stale_probe_schema_is_not_dispatched(self):
        self.register("codex1", "codex", "legacy@example.invalid")
        self.cli("quota", "codex1")
        instance = self.store.get("instances", "codex1")
        instance["status"].pop("schema")
        self.store.put("instances", instance)
        task = {"id": "new", "allowed": ["codex1"], "kind": "code", "bindings": {}}
        self.assertIsNone(controller.choose(self.store, task, [instance], [], time.time())[0])
        self.submit("fresh", "codex1", "edit change.txt complete")
        self.launch()
        self.assertEqual(self.wait("fresh", {"review"})["attempts"][-1]["instance"], "codex1")
        self.assertEqual(self.store.get("instances", "codex1")["status"]["schema"], backend.PROBE_SCHEMA)

    def login(self, name):
        from unittest import mock
        with mock.patch.object(controller.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(controller.backend, "auth_command", return_value=0):
            return controller.auth_instance(self.store, name, "login")

    def test_email_only_binding_never_dispatches_even_without_workspace_reply(self):
        self.register("codex1", "codex", "legacy@example.invalid", legacy=True)
        self.set_workspace("codex1", "")
        self.cli("quota", "codex1")
        instance = self.store.get("instances", "codex1")
        self.assertTrue(instance["status"]["logged_in"])
        self.assertTrue(instance["status"]["rebind_required"])
        task = {"id": "new", "allowed": ["codex1"], "kind": "code", "bindings": {}}
        self.assertIsNone(controller.choose(self.store, task, [instance], [], time.time())[0])

    def test_codex_login_requires_workspace_and_respects_running_legacy_work(self):
        self.register("codex1", "codex", "user@example.invalid")
        self.set_workspace("codex1", "")
        with self.assertRaises(controller.DelegateError):
            self.login("codex1")
        unbound = self.store.get("instances", "codex1")
        self.assertIsNone(unbound["bound_account"])
        self.assertFalse(unbound["auth_busy"])
        self.set_workspace("codex1", "ws-1")
        legacy = backend.account_key("codex", "user@example.invalid")
        self.store.put("tasks", {"id": "t", "state": "running"})
        self.store.put("attempts", {"id": "live", "task": "t", "instance": "codex2", "account": legacy,
                                    "state": "running", "generation": 0})
        bound = self.login("codex1")
        self.assertEqual(bound["bound_account"], legacy)
        self.assertFalse(bound["bound_scoped"])
        live = self.store.get("attempts", "live")
        live["state"] = "review"
        self.store.put("attempts", live)
        self.cli("instance", "rebind", "codex1")
        self.assertTrue(self.store.get("instances", "codex1")["bound_scoped"])
        self.store.put("attempts", {**live, "id": "done"})
        fresh = self.login("codex1")
        self.assertEqual(fresh["bound_account"], backend.account_key("codex", "user@example.invalid", "ws-1"))
        self.assertTrue(fresh["bound_scoped"])

    def test_codex_identity_names_workspace(self):
        reply = {"account": {"type": "chatgpt", "email": "a@example.invalid", "planType": "plus"},
                 "workspaceRouting": {"chatgptAccountId": "ws-1"}}
        key, legacy, plan = backend.codex_identity(reply)
        self.assertEqual(key, backend.account_key("codex", "a@example.invalid", "ws-1"))
        self.assertEqual(legacy, backend.account_key("codex", "a@example.invalid"))
        self.assertEqual(plan, "plus")
        other = backend.codex_identity({**reply, "workspaceRouting": {"chatgptAccountId": "ws-2"}})[0]
        self.assertNotEqual(key, other)
        self.assertEqual(backend.codex_identity({"account": None}), (None, None, None))
        email_only = backend.account_key("codex", "a@example.invalid")
        for bad in ("", "   ", 123, None, ["ws"]):
            key, legacy, _ = backend.codex_identity({**reply, "workspaceRouting": {"chatgptAccountId": bad}})
            self.assertEqual((key, legacy), (email_only, None))
        self.assertEqual(backend.codex_identity({**reply, "workspaceRouting": "ws"})[1], None)
        for bad_reply in ({"account": "x"}, {"account": {"type": "chatgpt", "email": 42}},
                          {"account": {"type": "chatgpt", "email": "  "}}, "not-a-dict", None):
            self.assertEqual(backend.codex_identity(bad_reply), (None, None, None))

    def test_execution_session_rejects_switched_workspace(self):
        self.register("codex1", "codex", "user@example.invalid")
        self.submit("switched", "codex1", "edit change.txt complete")
        # The dispatch probe sees ws-default; the executing server then reports another workspace.
        script = self.store.instance_dir("codex1") / "cli" / "fixture-workspace-after"
        script.write_text("ws-other")
        self.launch()
        result = self.wait("switched", {"failed", "auth", "review", "needs_report"})
        self.assertEqual(result["state"], "auth")
        self.assertEqual(self.store.get("instances", "codex1")["probed_at"], 0)
        self.assertIsNone(result["attempts"][-1]["session"])  # no thread was started
        self.assertIn("工作区", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_image_workspace_mismatch_blocks_turn_input(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "img", "--kind", "image", "--instance", "codex1",
                 "--prompt", "private image prompt")
        home = self.store.instance_dir("codex1") / "cli"
        (home / "fixture-workspace-after").write_text("ws-other")
        self.launch()
        result = self.wait("img", {"auth", "failed", "review"})
        self.assertEqual(result["state"], "auth")
        self.assertIsNone(result["attempts"][-1]["session"])
        self.assertFalse((home / "fixture-turn-started").exists())
        self.assertEqual(self.store.get("instances", "codex1")["probed_at"], 0)

    def test_image_output_discarded_when_workspace_switches_during_run(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "img", "--kind", "image", "--instance", "codex1",
                 "--prompt", "switch-workspace-during-run")
        self.launch()
        result = self.wait("img", {"auth", "failed", "review"})
        self.assertEqual(result["state"], "auth")
        self.assertTrue((self.store.instance_dir("codex1") / "cli" / "fixture-image-input").exists())
        self.assertFalse(list((self.store.task_dir("img") / "reports").glob("*.png")))
        self.assertEqual(self.store.get("instances", "codex1")["probed_at"], 0)
        self.assertIn("工作区", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_image_rechecks_executing_connection_after_switch_back(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("task", "submit", "--id", "img", "--kind", "image", "--instance", "codex1",
                 "--prompt", "switch-back-before-probe")
        self.launch()
        result = self.wait("img", {"auth", "failed", "review"})
        self.assertEqual(result["state"], "auth")
        self.assertTrue((self.store.instance_dir("codex1") / "cli" / "fixture-image-input").exists())
        self.assertFalse(list((self.store.task_dir("img") / "reports").glob("*.png")))
        self.assertEqual(self.store.get("instances", "codex1")["probed_at"], 0)
        self.assertIn("工作区", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_login_with_running_legacy_work_flags_rebind(self):
        self.register("codex1", "codex", "user@example.invalid")
        legacy = backend.account_key("codex", "user@example.invalid")
        self.store.put("tasks", {"id": "t", "state": "running"})
        self.store.put("attempts", {"id": "live", "task": "t", "instance": "codex2", "account": legacy,
                                    "state": "running", "generation": 0})
        self.assertTrue(self.login("codex1")["status"]["rebind_required"])

    def test_logout_does_not_ask_for_rebind(self):
        from unittest import mock
        self.register("codex1", "codex", "user@example.invalid")
        (self.store.instance_dir("codex1") / "cli" / "fixture-account").unlink()
        with mock.patch.object(controller.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(controller.backend, "auth_command", return_value=0):
            out = controller.auth_instance(self.store, "codex1", "logout")
        self.assertIsNone(out["bound_account"])
        self.assertNotIn("rebind_required", out["status"])

    def test_account_check_invalidates_before_closing_the_server(self):
        from unittest import mock
        self.register("codex1", "codex", "codex@example.invalid")
        self.cli("quota", "codex1")
        instance = self.store.get("instances", "codex1")
        gen = instance["generation"]
        backend.verify_codex_account(self.store, instance, instance["bound_account"], gen)  # match: no-op
        self.assertGreater(self.store.get("instances", "codex1")["probed_at"], 0)
        seen = []

        class Server:
            def request(self, *a, **k):
                return {"account": {"type": "chatgpt", "email": "codex@example.invalid"},
                        "workspaceRouting": {"chatgptAccountId": "ws-other"}}

            def close(self):
                seen.append(self_store.get("instances", "codex1")["probed_at"])
                raise OSError("close failed")

        self_store = self.store
        with mock.patch.object(backend, "open_app", return_value=Server()):
            with self.assertRaises(backend.AccountMismatch):
                backend.verify_codex_account(self.store, instance, instance["bound_account"], gen)
        self.assertEqual(seen, [0])  # invalidated before close; the close error did not mask it
        with mock.patch.object(backend, "open_app", side_effect=OSError("down")):
            with self.assertRaises(backend.AccountMismatch):
                backend.verify_codex_account(self.store, instance, instance["bound_account"], gen)

    def test_invalidate_probe_blocks_dispatch_immediately(self):
        self.register("codex1", "codex", "user@example.invalid")
        self.cli("quota", "codex1")
        instance = self.store.get("instances", "codex1")
        controller.invalidate_probe(self.store, "codex1", instance["generation"] + 1)  # other login: no-op
        self.assertGreater(self.store.get("instances", "codex1")["probed_at"], 0)
        controller.invalidate_probe(self.store, "codex1", instance["generation"])
        stale = self.store.get("instances", "codex1")
        self.assertEqual(stale["probed_at"], 0)
        self.assertEqual(stale["invalidations"], 1)
        task = {"id": "new", "allowed": ["codex1"], "kind": "code", "bindings": {}}
        self.assertIsNone(controller.choose(self.store, task, [stale], [], time.time())[0])

    def test_probe_result_discarded_after_concurrent_invalidation(self):
        from unittest import mock
        self.register("codex1", "codex", "user@example.invalid")
        self.cli("quota", "codex1")
        instance = self.store.get("instances", "codex1")
        real = backend.probe

        def probe_then_invalidate(store, inst, **kwargs):
            observed = real(store, inst, **kwargs)
            row = store.get("instances", inst["id"])  # a supervisor invalidates meanwhile
            row.update(probed_at=0, invalidations=row.get("invalidations", 0) + 1)
            store.put("instances", row)
            return observed

        with mock.patch.object(controller.backend, "probe", side_effect=probe_then_invalidate):
            controller.refresh_instance(self.store, instance, force=True)
        self.assertEqual(self.store.get("instances", "codex1")["probed_at"], 0)

    def test_application_failure_rolls_back_completed_paths(self):
        for filename in ("first.txt", "second.txt"):
            (self.project / filename).write_text(filename)
        rows = [{"path": name, "before": workspace.entry(self.project, name), "after": None}
                for name in ("first.txt", "second.txt")]
        journal = self.root / "apply.json"
        install = workspace.install

        def unreliable(root, relative, value, **kwargs):
            if relative == "second.txt" and value is None:
                raise OSError("fixture interruption")
            install(root, relative, value, **kwargs)

        with patch.object(workspace, "install", side_effect=unreliable):
            with self.assertRaisesRegex(OSError, "fixture interruption"):
                workspace.apply_application(self.project, rows, journal, "approved")
        self.assertEqual(json.loads(journal.read_text())["status"], "rolled_back")
        for row in rows:
            self.assertEqual(workspace.entry(self.project, row["path"]), row["before"])

    def test_git_conflict_can_be_resolved_in_integration_worktree(self):
        self.register("claude1", "claude", "claude@example.invalid")
        self.submit("conflict", "claude1", "edit change.txt delegated")
        self.launch()
        revision = self.approve(self.wait("conflict", {"review"}))
        (self.project / "change.txt").write_text("local work\n")
        candidate = self.cli("task", "apply", "conflict", "--revision", revision, "--integrate")
        self.assertEqual((self.project / "change.txt").read_text(), "local work\n")
        work = Path(candidate["work"])
        (work / "change.txt").write_text("local work and delegated\n")
        workspace.git(work, "add", "change.txt")
        recorded = self.cli("task", "apply", "conflict", "--revision", revision, "--record-integration")
        accepted = self.approve(recorded)
        index = (self.project / ".git/index").read_bytes()
        self.cli("task", "apply", "conflict", "--revision", accepted, "--apply")
        self.assertEqual((self.project / "change.txt").read_text(), "local work and delegated\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index)
        self.submit("after-integration", "claude1", "edit another.txt scheduled")
        self.launch()
        self.wait("after-integration", {"review"})

    def test_code_load_balances_distinct_accounts(self):
        self.register("first", "codex", "shared@example.invalid")
        self.register("second", "codex", "shared@example.invalid")
        self.register("third", "codex", "different@example.invalid")
        for name in ("lane1", "lane2"):
            self.cli("task", "submit", "--id", name, "--project", self.project, "--backend", "codex",
                     "--prompt", "wait-for-cancel")
        self.launch()
        instances = {self.wait(name, {"running"})["attempts"][0]["instance"] for name in ("lane1", "lane2")}
        self.assertEqual(instances, {"first", "third"})
        for name in ("lane1", "lane2"):
            self.cli("task", "cancel", name)
            self.wait(name, {"cancelled"})

    def test_codex_defaults_to_newest_sol_and_allows_independent_overrides(self):
        self.register("codex1", "codex", "account@example.invalid")
        cases = [
            ("default", "gpt-6.10-sol", "medium", ()),
            ("effort", "gpt-6.10-sol", "high", ("--effort", "high")),
            ("model", "gpt-7.1-astra", "medium", ("--model", "gpt-7.1-astra")),
            ("both", "gpt-6.2-sol", "low", ("--model", "gpt-6.2-sol", "--effort", "low")),
        ]
        for task, model, effort, options in cases:
            self.submit(task, "codex1", f"expect-config {model} {effort}", *options)
        self.cli("task", "submit", "--id", "default-image", "--kind", "image", "--instance", "codex1",
                 "--prompt", "expect-config gpt-6.10-sol medium")
        for task, model, effort, _ in [*cases, ("default-image", "gpt-6.10-sol", "medium", ())]:
            self.launch()
            status = self.wait(task, {"review"})
            report = json.loads(Path(status["report_path"]).read_text())
            self.assertEqual((report["model"], report["selected_model"], report["effort"]), (model, model, effort))

    def test_account_switch_invalidates_existing_route(self):
        self.register("codex1", "codex", "original@example.invalid")
        self.submit("pinned", "codex1", "edit change.txt delegated")
        instance = self.store.get("instances", "codex1")
        instance.update(generation=1, bound_account=backend.account_key("codex", "other@example.invalid"))
        (self.store.instance_dir("codex1") / "cli" / "fixture-account").write_text("other@example.invalid")
        self.store.put("instances", instance)
        self.launch()
        result = self.cli("task", "status", "pinned")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["blocked_by"], "eligible_instance")
        self.assertFalse(result["attempts"])

    def test_diamond_dependencies_and_validation(self):
        self.register("codex1", "codex", "codex@example.invalid")
        self.submit("base", "codex1", "edit common.txt base")
        self.submit("z-left", "codex1", "require common.txt base\nedit left.txt left", "--depends-on", "base")
        self.submit("a-right", "codex1", "require common.txt base\nedit right.txt right", "--depends-on", "base")
        self.submit("bottom", "codex1", "require left.txt left\nrequire right.txt right\nedit result.txt done",
                    "--depends-on", "z-left", "--depends-on", "a-right",
                    "--verify", "test -f common.txt && test -f result.txt")
        self.launch()
        self.approve(self.wait("base", {"review"}))
        self.launch()
        for name in ("z-left", "a-right"):
            self.approve(self.wait(name, {"review"}))
        self.launch()
        bottom = self.wait("bottom", {"review"})
        revision = self.approve(bottom)
        approved = self.cli("task", "status", "bottom")
        report = json.loads(Path(approved["report_path"]).read_text())
        self.assertEqual([name for name, _ in report["dependencies"]], ["base", "z-left", "a-right"])
        self.assertEqual(report["paths"], ["result.txt"])
        self.assertEqual(report["verification"]["status"], "passed")
        self.assertEqual(approved["approved"], revision)


if __name__ == "__main__":
    unittest.main()
