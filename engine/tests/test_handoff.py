"""Handoff policy and single-rollout copy boundaries."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cli_manager import backend, controller
from cli_manager.store import DelegateError, Store


class HandoffTest(unittest.TestCase):
    def test_quota_uses_source_attempt_account(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "state")
                self.addCleanup(store.db.close)
                task = dict(id="move", kind="code", state="waiting_quota", attempt="old", handoff_targets=["dst"])
                store.put("tasks", task)
                src = dict(id="src", provider=provider, generation=1, bound_account="later-login")
                dst = dict(src, id="dst", bound_account="original")
                for instance in (src, dst):
                    store.put("instances", instance)
                store.put("attempts", dict(id="old", task="move", state="waiting_quota", instance="src",
                                           account="original", outcome="quota", checkpoint="commit"))
                folder = store.task_dir("move") / "handoffs"
                folder.mkdir(parents=True)
                (folder / "old.material.json").write_text("{}")
                with self.assertRaisesRegex(DelegateError, "必须更换账户"):
                    controller.start_handoff(store, "move", "dst", "brief", False, "old")
                dst["bound_account"] = "different"
                store.put("instances", dst)
                queued = controller.start_handoff(store, "move", "dst", "brief", False, "old")
                self.assertEqual(queued["target"], "dst")
                store.put("tasks", task)
                self.assertEqual(controller.requeue(store, "move", native=False, note=None)["target"], "src")

    def test_rollout_copy_is_exclusive_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = (Path(tmp) / name for name in ("src", "dst"))
            relative = Path("sessions/2026/01/02/rollout-date-thread.jsonl")
            file = source / relative
            file.parent.mkdir(parents=True)
            target.mkdir()
            file.write_bytes(b"fixture")
            backend.copy_rollout(source, target, "thread")
            file.write_bytes(b"new data")
            with self.assertRaises(FileExistsError):
                backend.copy_rollout(source, target, "thread")
            self.assertEqual((target / relative).read_bytes(), b"fixture")
            (target / relative).unlink()
            file.unlink()
            file.symlink_to(__file__)
            with self.assertRaises((OSError, ValueError)):
                backend.copy_rollout(source, target, "thread")
            self.assertFalse((target / relative).exists())
            file.unlink()
            file.write_bytes(b"fixture")
            other = Path(tmp) / "other"
            other.mkdir()
            (other / "sessions").symlink_to(target / "sessions")
            with self.assertRaises(OSError):
                backend.copy_rollout(source, other, "thread")
            self.assertFalse((target / relative).exists())
