"""Capability tiers: open by default, the closed sandbox on request, and the floor under both."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from cli_manager import access, backend, controller, workspace
from cli_manager.store import DelegateError, Store
from test_delegate import CliTest as Harness


class PolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.db.close)
        self.work = self.root / "work"
        self.work.mkdir()

    def test_defaults_are_open_with_network_and_images(self):
        capabilities = access.normalize(self.store, None)
        self.assertEqual((capabilities["access"], capabilities["network"], capabilities["image"]),
                         ("open", "on", True))
        self.assertEqual(capabilities["explicit"], [])
        self.assertEqual(access.normalize(self.store, {"access": None, "network": None})["access"], "open")

    def test_sandbox_is_explicit_and_closed(self):
        capabilities = access.normalize(self.store, {"access": "sandbox"})
        self.assertEqual((capabilities["network"], capabilities["image"]), ("off", False))
        self.assertFalse(access.is_open(capabilities))
        for extra in ({"network": "on"}, {"allow_domains": ["example.com"]}, {"read_dirs": [str(self.work)]},
                      {"write_dirs": [str(self.work)]}, {"image": True}):
            with self.subTest(extra=extra), self.assertRaises(DelegateError):
                access.normalize(self.store, {"access": "sandbox", **extra})
        with self.assertRaises(DelegateError):
            access.normalize(self.store, {"access": "sandbox"}, kind="image")

    def test_recorded_tasks_without_capabilities_stay_closed(self):
        self.assertEqual(access.of({"id": "old"})["access"], "sandbox")
        self.assertIsNone(backend.codex_grants(self.store, {"id": "one"}, {}, {}, access.of({})))

    def test_full_needs_flag_and_a_person_at_a_terminal(self):
        with self.assertRaises(DelegateError):
            access.normalize(self.store, {"access": "full"})
        with self.assertRaises(DelegateError):
            access.normalize(self.store, {"access": "full", "confirm_full": True}, interactive=False)
        with patch.object(sys, "stdin") as stdin:
            stdin.readline.return_value = "full\n"
            granted = access.normalize(self.store, {"access": "full", "confirm_full": True}, interactive=True)
            stdin.readline.return_value = "yes\n"
            with self.assertRaises(DelegateError):
                access.normalize(self.store, {"access": "full", "confirm_full": True}, interactive=True)
        self.assertEqual(granted["access"], "full")

    def test_network_options(self):
        self.assertEqual(access.normalize(self.store, {"network": "loopback"})["network"], "loopback")
        narrowed = access.normalize(self.store, {"allow_domains": ["Registry.NPMJS.org", "registry.npmjs.org"]})
        self.assertEqual(narrowed["allow_domains"], ["registry.npmjs.org"])
        for bad in ({"network": "loopback", "allow_domains": ["a.example"]},
                    {"allow_domains": ["https://a.example"]}, {"allow_domains": ["*.example.com"]}):
            with self.subTest(bad=bad), self.assertRaises(DelegateError):
                access.normalize(self.store, bad)

    def test_granted_directories_reject_credentials_and_state(self):
        home = access.home()
        granted = access.normalize(self.store, {"read_dirs": [str(self.work)], "write_dirs": [str(self.work)]})
        self.assertEqual(granted["read_dirs"], [str(self.work)])
        rejected = [home, home / ".ssh", Path("/"), self.store.root, self.store.root / "instances", "relative/dir"]
        (self.store.root / "instances").mkdir(exist_ok=True)
        for path in rejected:
            if not Path(path).is_absolute() or Path(path).is_dir():
                with self.subTest(path=path), self.assertRaises(DelegateError):
                    access.normalize(self.store, {"read_dirs": [str(path)]})
                with self.subTest(path=path, mode="write"), self.assertRaises(DelegateError):
                    access.normalize(self.store, {"write_dirs": [str(path)]})

    def profile(self, capabilities, readonly=False):
        grants = {"capabilities": capabilities, "read": ["/granted/read"], "write": ["/granted/write"],
                  "deny": ["/secret"], "images": "/images"}
        return backend.codex_profile(self.work, None, self.root / "tmp", readonly,
                                     grants if access.is_open(capabilities) else None)

    def test_sandbox_profile_is_unchanged(self):
        closed = ["-c", 'default_permissions="delegate"',
                  "-c", "permissions.delegate.filesystem={" + ",".join(
                      f"{json.dumps(key)}={json.dumps(value)}" for key, value in {
                          ":minimal": "read", str(self.work): "write", str(self.root / "tmp"): "write",
                          str(self.work / ".git"): "read"}.items()) + "}",
                  "-c", "permissions.delegate.network.enabled=false",
                  "-c", "mcp_servers={}", "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false",
                  "-c", "features.apps=false", "-c", "features.plugins=false", "-c", "features.hooks=false",
                  "-c", 'web_search="disabled"']
        self.assertEqual(backend.codex_profile(self.work, None, self.root / "tmp", False), closed)
        self.assertEqual(self.profile(access.normalize(self.store, {"access": "sandbox"})), closed)
        self.assertEqual(self.profile(access.LEGACY), closed)

    def test_open_profiles(self):
        opened = " ".join(self.profile(access.normalize(self.store, None)))
        self.assertIn("permissions.delegate.network.enabled=true", opened)
        self.assertIn('"/granted/read"="read"', opened)
        self.assertIn('"/granted/write"="write"', opened)
        self.assertIn('"/secret"="deny"', opened)
        self.assertIn("features.image_generation=true", opened)
        self.assertNotIn("network_proxy", opened)
        loopback = " ".join(self.profile(access.normalize(self.store, {"network": "loopback"})))
        self.assertIn("features.network_proxy=true", loopback)
        self.assertIn("permissions.delegate.network.domains={}", loopback)
        self.assertIn("allow_local_binding=true", loopback)
        narrowed = " ".join(self.profile(access.normalize(self.store, {"allow_domains": ["a.example"]})))
        self.assertIn('permissions.delegate.network.domains={"a.example"="allow"}', narrowed)
        off = " ".join(self.profile(access.normalize(self.store, {"network": "off", "image": False})))
        self.assertIn("permissions.delegate.network.enabled=false", off)
        self.assertIn("features.image_generation=false", off)
        review = " ".join(self.profile(access.normalize(self.store, None), readonly=True))
        self.assertIn('"/granted/write"="read"', review)

    def test_open_workers_use_the_real_home_and_closed_ones_the_instance_home(self):
        instance = {"id": "one", "provider": "codex"}
        attempt = {"id": "attempt", "instance": "one"}
        opened = backend.worker_environment(self.store, instance, {"kind": "code", "capabilities": access.DEFAULT}, attempt)
        self.assertEqual(opened["HOME"], str(access.home()))
        self.assertTrue(opened["CODEX_HOME"].startswith(str(self.store.root)))
        self.assertEqual(opened["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertTrue(opened["npm_config_cache"].startswith(str(self.store.root)))
        for task in ({"kind": "code", "capabilities": access.SANDBOX}, {"kind": "code"},
                     {"kind": "image", "capabilities": access.DEFAULT}):
            with self.subTest(task=task):
                closed = backend.worker_environment(self.store, instance, task, attempt)
                self.assertEqual(closed, backend.environment(self.store, instance, attempt))
                self.assertTrue(closed["HOME"].startswith(str(self.store.root)))
        claude = backend.worker_environment(self.store, {"id": "two", "provider": "claude"},
                                            {"kind": "code", "capabilities": access.DEFAULT}, attempt)
        self.assertTrue(claude["HOME"].startswith(str(self.store.root)))

    def classify(self, command, cwd=None):
        return access.classify_command(command, str(cwd or self.work),
                                       secrets=access.credential_roots(self.store),
                                       writable=[str(self.work)])[0]

    def test_credentials_are_refused_in_every_form(self):
        home = access.home()
        (self.store.root / "instances/other/cli").mkdir(parents=True)
        for command in (f"cat {home}/.ssh/id_ed25519", "cat ~/.ssh/config", "cat $HOME/.aws/credentials",
                        "bash -lc 'cp ~/.codex/auth.json .'", "security find-generic-password -s x -w",
                        f"sqlite3 {self.store.root}/state.sqlite3 .dump", "cat ../../cli/auth.json",
                        f"ls {home}/Library/Keychains", "gh auth token", "cat .env.production",
                        f"cat {self.store.root}/instances/other/cli/config.toml"):
            with self.subTest(command=command):
                self.assertEqual(self.classify(command), "credential")
        secrets = access.credential_roots(self.store)
        self.assertEqual(access.classify_changes([{"path": str(home / ".ssh/authorized_keys"), "kind": "update"}],
                                                 secrets=secrets, writable=[str(self.work)])[0], "credential")
        self.assertTrue(access.secret_path(home / ".netrc", secrets))
        self.assertFalse(access.secret_path(home / ".cache/tool", secrets))

    def test_irreversible_actions_are_marked(self):
        outside = self.root / "elsewhere"
        for command in ("git push origin main", "git -C repo push --force", "bash -lc 'cd x && git push'",
                        "npm publish", "pnpm --filter pkg publish", "gh release create v1", "twine upload dist/*",
                        f"rm -rf {outside}", "rm -rf ../elsewhere", "rm -rf $TARGET", f"mv {outside}/a b",
                        "docker push image", f"find {outside} -name '*.log' -delete", "gh pr merge 3"):
            with self.subTest(command=command):
                self.assertEqual(self.classify(command), "irreversible")
        for command in ("rm -rf build", f"rm {self.work}/out.png", "npx tsc --noEmit", "npx vitest run",
                        "python model_asset.py --quick", "git status", "npm install", "mv a.txt b.txt",
                        "git stash push -u", "git commit -m 'push fix'", "git log --grep push"):
            with self.subTest(command=command):
                self.assertIsNone(self.classify(command))
        secrets = access.credential_roots(self.store)
        self.assertEqual(access.classify_changes([{"path": str(outside / "a"), "kind": "delete"}],
                                                 secrets=secrets, writable=[str(self.work)])[0], "irreversible")
        self.assertIsNone(access.classify_changes([{"path": str(self.work / "a"), "kind": "delete"}],
                                                  secrets=secrets, writable=[str(self.work)])[0])

    def test_project_directory_is_readable_only_behind_links(self):
        task = {"baseline": {"root": str(self.work)}}
        capabilities = access.normalize(self.store, None)
        self.assertNotIn(str(self.work), access.read_roots(self.store, task, capabilities))
        self.assertIn(str(self.work), access.read_roots(self.store, task, capabilities, project=True))

    def test_directory_grants_around_credentials_are_refused(self):
        secrets = access.credential_roots(self.store)
        home = access.home()
        for path in (home, "/", home / ".ssh", home / ".ssh/keys", self.store.root):
            with self.subTest(path=path):
                self.assertTrue(access.grants_secret(path, secrets))
        self.assertFalse(access.grants_secret(self.work, secrets))

    def test_capability_report_and_requirements(self):
        report = backend.codex_capabilities("codex-cli 0.171.0", "image_generation stable true\n"
                                            "network_proxy experimental false\n")
        self.assertEqual(report["missing"], [])
        removed = backend.codex_capabilities("codex-cli 0.171.2", "network_proxy removed false\n")
        self.assertEqual(removed["missing"], ["network.allow_domain", "network.loopback"])
        self.assertIn("access.open", backend.codex_capabilities("codex-cli 0.172.0", "")["missing"])
        needs = backend.required_capabilities(access.normalize(self.store, {"allow_domains": ["a.example"]}))
        self.assertIn("network.allow_domain", needs)
        self.assertIn("approvals", needs)


class OpenTierTest(unittest.TestCase):
    """End to end through the public CLI against the protocol fixture."""
    # Borrow the harness helpers without inheriting (and re-running) the base suite.
    kill_workers, git, cli, register = Harness.kill_workers, Harness.git, Harness.cli, Harness.register
    launch, wait, submit, approve = Harness.launch, Harness.wait, Harness.submit, Harness.approve

    def setUp(self):
        Harness.setUp(self)
        self.register("codex1", "codex", "one@example.invalid")

    def pending(self, name):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.cli("task", "watch", name, "--wait", "1")["task"]
            if status["pending"]:
                return status["pending"][0]
            if status["state"] not in controller.ACTIVE | {"queued"}:
                self.fail(status)
        self.fail("没有出现待批请求")

    def approvals(self):
        log = self.store.instance_dir("codex1") / "cli" / "fixture-approvals"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def events(self, name):
        return [(event["type"], event) for event in self.store.events(name, 0)]

    def test_default_submission_runs_open_and_routes_requests_to_the_planner(self):
        task = self.submit("open", "codex1", "approve-command npx playwright test\nedit change.txt done")
        self.assertEqual(task["capabilities"]["access"], "open")
        self.assertEqual((task["capabilities"]["network"], task["capabilities"]["image"]), ("on", True))
        self.launch()
        request = self.pending("open")
        self.assertEqual((request["kind"], request["floor"], request["session_allow"]), ("command", None, True))
        self.assertEqual(request["input"]["command"], "npx playwright test")
        self.assertTrue(request["input"]["cwd"])
        self.assertEqual(request["input"]["reason"], "fixture")
        self.assertIn("paths", request["input"])
        self.assertIn("network", request["input"])
        self.cli("task", "decide", "open", request["id"], "allow", "--always")
        status = self.wait("open", {"review"})
        self.assertEqual(self.approvals()[0]["result"], {"decision": "acceptForSession"})
        attempt = status["attempts"][-1]
        self.assertEqual(attempt["capabilities"]["access"], "open")
        self.assertEqual(attempt["capabilities"]["approvals"], "planner")
        # Ignored content is cloned into the workspace; the project itself (and its .env) stays out of reach.
        self.assertNotIn(str(self.project), attempt["capabilities"]["read_roots"])
        report = json.loads(Path(status["report_path"]).read_text())
        self.assertEqual(report["capabilities"]["network"], "on")
        self.assertEqual(report["approvals"][0]["behavior"], "allow")
        self.assertTrue(report["approvals"][0]["always"])
        kinds = [kind for kind, _ in self.events("open")]
        self.assertIn("capabilities_applied", kinds)
        self.assertNotIn("approval_denied_by_policy", kinds)
        submitted = next(event for kind, event in self.events("open") if kind == "submitted")
        self.assertEqual(submitted["capabilities"]["access"], "open")
        ledger = self.cli("ledger", "open")[0]
        self.assertEqual(ledger["capabilities"]["access"], "open")
        argv = json.loads((self.store.instance_dir("codex1") / "cli" / "fixture-argv").read_text())
        self.assertIn("permissions.delegate.network.enabled=true", argv)
        self.assertNotIn(json.dumps(str(self.project)) + '="read"', " ".join(argv))

    def test_sandbox_tier_restores_the_closed_behaviour(self):
        task = self.submit("closed", "codex1", "approve-command npx playwright test\nedit change.txt done",
                           "--access", "sandbox")
        self.assertEqual(task["capabilities"]["access"], "sandbox")
        self.launch()
        status = self.wait("closed", {"review"})
        self.assertEqual(self.approvals()[0]["result"], {"decision": "decline"})
        self.assertIn("approval_denied_by_policy", [kind for kind, _ in self.events("closed")])
        self.assertEqual(status["pending"], [])
        argv = json.loads((self.store.instance_dir("codex1") / "cli" / "fixture-argv").read_text())
        self.assertIn("permissions.delegate.network.enabled=false", argv)
        self.assertIn('web_search="disabled"', argv)
        joined = " ".join(argv)
        self.assertNotIn(str(self.project), joined)
        self.assertNotIn("deny", joined)
        self.assertNotIn("image_generation", joined)
        attempt = status["attempts"][-1]
        self.assertEqual((attempt["capabilities"]["access"], attempt["capabilities"]["approvals"]),
                         ("sandbox", "denied"))
        self.assertEqual(attempt.get("mounts"), None)
        self.cli("task", "submit", "--project", self.project, "--instance", "codex1", "--prompt", "x",
                 "--access", "sandbox", "--network", "on", code=2)

    def test_task_recorded_before_capabilities_keeps_closed_behaviour(self):
        self.submit("old", "codex1", "approve-command npx playwright test\nedit change.txt done")
        task = self.store.get("tasks", "old")
        del task["capabilities"]
        self.store.put("tasks", task)
        self.launch()
        status = self.wait("old", {"review"})
        self.assertEqual(self.approvals()[0]["result"], {"decision": "decline"})
        report = json.loads(Path(status["report_path"]).read_text())
        self.assertEqual(report["capabilities"]["access"], "sandbox")
        self.assertTrue(report["capabilities"]["legacy"])

    def test_credentials_are_refused_before_reaching_the_planner(self):
        home = access.home()
        self.submit("secret", "codex1", f"approve-command cat {home}/.ssh/id_ed25519\n"
                    f"approve-permission {home}/.ssh\napprove-command cat ~/.codex/auth.json")
        self.launch()
        status = self.wait("secret", {"review"})
        self.assertEqual(status["pending"], [])
        results = [entry["result"] for entry in self.approvals()]
        self.assertEqual(results, [{"decision": "decline"}, {"permissions": {}, "scope": "turn"},
                                   {"decision": "decline"}])
        denied = [event for kind, event in self.events("secret") if kind == "approval_denied_by_floor"]
        self.assertEqual(len(denied), 3)
        self.assertEqual(denied[0]["floor"], "credential")
        self.assertIn(".ssh/id_ed25519", denied[0]["command"])
        self.assertTrue((self.store.instance_dir("codex1") / "cli" / "fixture-steers").exists())

    def test_irreversible_requests_need_one_approval_each(self):
        self.submit("push", "codex1", "approve-command git push origin main\napprove-command git push origin main")
        self.launch()
        first = self.pending("push")
        self.assertEqual((first["floor"], first["session_allow"]), ("irreversible", False))
        self.cli("task", "decide", "push", first["id"], "allow", "--always", code=2)
        self.cli("task", "decide", "push", first["id"], "allow")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            second = self.pending("push")
            if second["id"] != first["id"]:
                break
        self.assertNotEqual(second["id"], first["id"])
        self.cli("task", "decide", "push", second["id"], "deny", "--message", "只推一次")
        self.wait("push", {"review"})
        self.assertEqual([entry["result"]["decision"] for entry in self.approvals()], ["accept", "decline"])
        decided = [event for kind, event in self.events("push") if kind == "approval_decided"]
        self.assertEqual(decided[0]["floor"], "irreversible")
        self.assertEqual(decided[0]["command"], "git push origin main")

    def test_denied_request_is_not_asked_again_in_the_session(self):
        self.submit("deny", "codex1", "approve-command npm install left-pad\napprove-command npm install left-pad")
        self.launch()
        request = self.pending("deny")
        self.cli("task", "decide", "deny", request["id"], "deny", "--message", "不需要")
        status = self.wait("deny", {"review"})
        self.assertEqual([entry["result"]["decision"] for entry in self.approvals()], ["decline", "decline"])
        self.assertIn("approval_repeat_denied", [kind for kind, _ in self.events("deny")])
        report = json.loads(Path(status["report_path"]).read_text())
        self.assertEqual(len(report["approvals"]), 1)

    def test_expired_request_counts_as_denied(self):
        self.submit("slow", "codex1", "approve-command npm install", "--approval-timeout", "1")
        self.launch()
        status = self.wait("slow", {"review"})
        self.assertEqual(self.approvals()[0]["result"], {"decision": "decline"})
        report = json.loads(Path(status["report_path"]).read_text())
        self.assertEqual((report["approvals"][0]["behavior"], report["approvals"][0]["by"]), ("deny", "timeout"))

    def test_request_cut_short_by_a_stop_is_not_a_denial(self):
        self.submit("stopped", "codex1", "approve-command npm install\nedit change.txt done")
        self.launch()
        first = self.pending("stopped")
        self.cli("task", "cancel", "stopped")
        self.wait("stopped", {"cancelled"})
        attempt = self.store.get("attempts", self.store.get("tasks", "stopped")["attempt"])
        folder = Path(attempt["logs"]) / "approvals"
        self.assertTrue((folder / f"{first['id']}.request.json").is_file())
        self.assertFalse((folder / f"{first['id']}.decision.json").exists())

    def test_home_directory_permission_is_refused_by_the_floor(self):
        self.submit("wide", "codex1", f"approve-permission {access.home()}\nedit change.txt done")
        self.launch()
        status = self.wait("wide", {"review"})
        self.assertEqual(status["pending"], [])
        self.assertEqual(self.approvals()[0]["result"], {"permissions": {}, "scope": "turn"})
        self.assertIn("approval_denied_by_floor", [kind for kind, _ in self.events("wide")])

    def test_network_and_delete_requests_carry_their_targets(self):
        outside = self.root / "outside.txt"
        outside.write_text("keep\n")
        self.submit("targets", "codex1", f"approve-network registry.npmjs.org\napprove-delete {outside}")
        self.launch()
        network = self.pending("targets")
        self.assertEqual(network["input"]["network"], ["https://registry.npmjs.org"])
        self.cli("task", "decide", "targets", network["id"], "allow")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            change = self.pending("targets")
            if change["id"] != network["id"]:
                break
        self.assertEqual((change["kind"], change["floor"]), ("file_change", "irreversible"))
        self.assertEqual(change["input"]["paths"], [str(outside)])
        self.cli("task", "decide", "targets", change["id"], "allow", "--always", code=2)
        self.cli("task", "decide", "targets", change["id"], "deny")
        self.wait("targets", {"review"})

    def test_ignored_assets_are_linked_not_copied_and_stay_out_of_checkpoints(self):
        (self.project / ".gitignore").write_text("assets/raw/\nnode_modules/\nquick*.json\n.env\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore")
        (self.project / "assets/raw").mkdir(parents=True)
        (self.project / "assets/raw/sheet.png").write_text("original\n")
        (self.project / "node_modules/pkg").mkdir(parents=True)
        (self.project / ".env").write_text("SECRET=1\n")
        self.submit("mount", "codex1", "require assets/raw/sheet.png original\nedit change.txt done\n"
                    "edit quick1.json {}\nwait-for-nothing")
        self.launch()
        status = self.wait("mount", {"review"})
        attempt = status["attempts"][-1]
        work = Path(attempt["work"])
        # Same volume: a private copy-on-write clone, writable without touching the original.
        self.assertEqual(attempt["mounts"], [])
        self.assertFalse((work / "assets/raw").is_symlink())
        self.assertEqual((work / "assets/raw/sheet.png").read_text(), "original\n")
        self.assertTrue((work / "node_modules/pkg").is_dir())
        (work / "assets/raw/sheet.png").write_text("scribble\n")
        self.assertEqual((self.project / "assets/raw/sheet.png").read_text(), "original\n")
        self.assertFalse(os.path.lexists(work / ".env"))
        self.assertEqual(attempt["paths"], ["change.txt"])
        patch_text = Path(status["patch_path"]).read_text()
        self.assertNotIn("assets/raw", patch_text)
        self.assertNotIn("node_modules", patch_text)
        self.assertNotIn("quick1.json", patch_text)
        revision = self.approve(status)
        preview = self.cli("task", "apply", "mount", "--revision", revision, "--check")
        self.assertEqual([change["path"] for change in preview["changes"]], ["change.txt"])

    def test_links_replace_clones_across_volumes_and_never_enter_a_snapshot(self):
        (self.project / ".gitignore").write_text("views/\nnode_modules\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore")
        (self.project / "art/views").mkdir(parents=True)
        (self.project / "art/views/front.png").write_text("front\n")
        (self.project / "node_modules").mkdir()
        directory = self.root / "task"
        directory.mkdir()
        baseline = workspace.freeze(self.project, directory, [])
        task = {"baseline": baseline}
        attempt = {"id": "attempt"}
        data = workspace.setup(directory, directory / "baseline.git", task, attempt, [])
        repo, work = Path(data["repo"]), Path(data["work"])
        mounts = workspace.mount_ignored(task, work, cloning=False)
        self.assertEqual(sorted(mounts), ["art/views", "node_modules"])
        self.assertEqual(os.path.realpath(work / "art/views"), str(self.project / "art/views"))
        workspace.register_mounts(repo, mounts)
        # `views/` names a directory, not a link: only the registration keeps the link out.
        commit = workspace.snapshot(repo, work, data["base"])
        self.assertEqual(commit, data["base"])
        self.assertEqual(workspace.mount_ignored(task, work, mounts, cloning=False), mounts)
        (work / "change.txt").write_text("edited\n")
        commit = workspace.snapshot(repo, work, data["base"])
        self.assertEqual(workspace.changes({"repo": str(repo), "base": data["base"]}, commit), ["change.txt"])

    def test_resume_tolerates_ignored_files_added_by_the_planner(self):
        (self.project / ".gitignore").write_text("shots/\nquick*.json\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore")
        self.submit("resume", "codex1", "edit change.txt first")
        self.launch()
        status = self.wait("resume", {"review"})
        work = Path(status["attempts"][-1]["work"])
        (work / "shots").mkdir()
        (work / "shots/front.png").write_text("png\n")
        (work / "quick-run.json").write_text("{}")
        self.cli("task", "review", "request-changes", "resume", "--revision", status["report"], "--note", "继续")
        self.cli("task", "resume", "resume", "--note", "require shots/front.png png")
        self.launch()
        resumed = self.wait("resume", {"review"})
        self.assertEqual(resumed["attempts"][-1]["paths"], ["change.txt"])
        # A controlled file changed behind the worker's back is still a deviation, and is named.
        (work / "untouched.txt").write_text("planner edit\n")
        self.cli("task", "review", "request-changes", "resume", "--revision", resumed["report"], "--note", "再来")
        self.cli("task", "resume", "resume")
        self.launch()
        failed = self.wait("resume", {"failed"})
        report = json.loads(Path(failed["report_path"]).read_text())
        self.assertIn("untouched.txt", report["diagnostic"])

    def test_plain_project_resume_ignores_its_own_ignored_artefacts(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / ".gitignore").write_text("quick*.json\n")
        (plain / "code.txt").write_text("base\n")
        self.submit("plain", "codex1", "edit code.txt first", project=plain)
        self.launch()
        status = self.wait("plain", {"review"})
        work = Path(status["attempts"][-1]["work"])
        (work / "quick1.json").write_text("{}")
        self.cli("task", "review", "request-changes", "plain", "--revision", status["report"], "--note", "继续")
        self.cli("task", "resume", "plain")
        self.launch()
        self.wait("plain", {"review"})

    def test_generated_image_in_a_code_task_is_an_artifact(self):
        target = self.root / "export"
        target.mkdir()
        self.submit("art", "codex1", f"generate-image {target}/cover.png\nedit change.txt done",
                    "--write-dir", target)
        self.launch()
        status = self.wait("art", {"review"})
        report = json.loads(Path(status["report_path"]).read_text())
        artifact, = report["artifacts"]
        self.assertEqual(artifact["kind"], "generated_image")
        self.assertEqual(artifact["copies"], [str(target / "cover.png")])
        self.assertEqual(controller.artifact(Path(artifact["path"]))["sha256"], artifact["sha256"])
        self.assertEqual(report["capabilities"]["write_dirs"], [str(target)])
        self.assertEqual(self.cli("ledger", "art")[0]["artifacts"][0]["sha256"], artifact["sha256"])
        self.assertEqual(status["attempts"][-1]["paths"], ["change.txt"])

    def test_handoff_carries_the_tier_and_reauthorizes_a_new_account(self):
        self.register("codex2", "codex", "two@example.invalid")
        self.submit("move", "codex1", "edit change.txt first\ncodex-quota", "--network", "loopback")
        self.launch()
        self.wait("move", {"waiting_quota"})
        material = self.cli("task", "handoff", "prepare", "move")
        brief = self.root / "handoff.md"
        brief.write_text("edit change.txt second\n")
        arguments = ("task", "handoff", "start", "move", "--source", material["source"], "--to", "codex2",
                     "--brief", brief, "--authorize-target")
        self.cli(*arguments, code=2)
        self.cli(*arguments, "--authorize-access")
        queued = next(event for kind, event in self.events("move") if kind == "handoff_queued")
        self.assertEqual(queued["capabilities"]["network"], "loopback")
        self.assertTrue(queued["access_reauthorized"])
        self.launch()
        status = self.wait("move", {"review"})
        self.assertEqual(status["attempts"][-1]["instance"], "codex2")
        self.assertEqual(status["attempts"][-1]["capabilities"]["network"], "loopback")

    def test_missing_capability_is_an_error_not_a_downgrade(self):
        instance = self.store.get("instances", "codex1")
        instance["status"] = {"capabilities": {"supported": [], "missing": ["network.loopback"]}}
        self.store.put("instances", instance)
        result = self.cli("task", "submit", "--project", self.project, "--instance", "codex1", "--prompt", "x",
                          "--network", "loopback", code=2)
        self.assertIsNone(result)
        self.assertEqual(self.store.rows("tasks"), [])

    def test_claude_instances_stay_closed_and_reject_open_options(self):
        self.register("claude1", "claude", "claude@example.invalid")
        self.submit("c", "claude1", "edit change.txt done")
        self.launch()
        status = self.wait("c", {"review"})
        self.assertEqual(status["attempts"][-1]["capabilities"]["access"], "sandbox")
        self.cli("task", "submit", "--project", self.project, "--instance", "claude1", "--prompt", "x",
                 "--read-dir", self.root, code=2)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (PolicyTest, OpenTierTest):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == "__main__":
    unittest.main()
