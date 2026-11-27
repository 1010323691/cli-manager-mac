"""Exercise no-overwrite and rollback on macOS destination filesystems."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install_bundle as installer


class InstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.bundle = root / "test.app"
        self.bundle.mkdir()
        (self.bundle / "signature.fixture").write_text("placeholder")
        self.skill = root / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("fixture")
        self.apps = root / "Apps with spaces"
        self.skills = root / "Agent skills with spaces"
        real = installer.subprocess.run

        def fixture_signing(args, **kwargs):
            if args[0] != "/usr/bin/codesign":
                return real(args, **kwargs)
            self.assertTrue((args[-1] / "signature.fixture").is_file())

        signing = patch.object(installer.subprocess, "run", side_effect=fixture_signing)
        signing.start()
        self.addCleanup(signing.stop)

    def run_install(self):
        return installer.install(self.bundle, self.skill, self.apps, self.skills)

    def test_staged_install_supports_spaces_and_refuses_overwrite(self):
        app, skill = self.run_install()
        self.assertEqual((skill / "app-path").read_text(), str(app) + "\n")
        self.assertTrue((app / "signature.fixture").is_file())
        with self.assertRaises(FileExistsError):
            self.run_install()
        self.assertEqual(list(self.apps.iterdir()), [app])
        self.assertEqual(list(self.skills.iterdir()), [skill])

    def test_concurrent_skill_collision_rolls_back_only_our_app(self):
        original = installer.exclusive_rename

        def collision(source, destination):
            if destination == self.skills / installer.SKILL:
                destination.mkdir()
                (destination / "other").write_text("unrelated")
            original(source, destination)

        with patch.object(installer, "exclusive_rename", side_effect=collision):
            with self.assertRaises(FileExistsError):
                self.run_install()
        self.assertEqual(list(self.apps.iterdir()), [])
        self.assertEqual((self.skills / installer.SKILL / "other").read_text(), "unrelated")
        self.assertEqual(len(list(self.skills.iterdir())), 1)

    def test_full_skill_is_shared_by_both_agent_directories(self):
        full = self.skill.parent / "full"
        (full / "scripts").mkdir(parents=True)
        (full / "SKILL.md").write_text("full fixture")
        (full / "scripts" / "delegate.py").write_text("entry point")
        codex = self.skills.parent / ".codex" / "skills"
        claude = self.skills.parent / ".claude" / "skills"
        app, companion, installed, *links = installer.install(
            self.bundle, self.skill, self.apps, self.skills, full, (codex, claude)
        )
        self.assertEqual(len(links), 4)
        for agent in (codex, claude):
            self.assertEqual((agent / installer.SKILL).readlink(), companion)
            shared = agent / installer.FULL_SKILL
            self.assertEqual(shared.readlink(), installed)
            self.assertEqual((shared / "SKILL.md").read_text(), "full fixture")
            self.assertTrue((shared / "scripts" / "delegate.py").is_file())
        self.assertTrue(app.is_dir())

    def test_late_link_collision_rolls_back_app_copies_and_other_links(self):
        full = self.skill.parent / "full"
        full.mkdir()
        (full / "SKILL.md").write_text("full fixture")
        codex = self.skills.parent / ".codex" / "skills"
        claude = self.skills.parent / ".claude" / "skills"
        target = claude / installer.FULL_SKILL
        original = installer.exclusive_rename

        def collision(source, destination):
            if destination == target:
                target.mkdir()
                (target / "other").write_text("unrelated")
            original(source, destination)

        with patch.object(installer, "exclusive_rename", side_effect=collision):
            with self.assertRaises(FileExistsError):
                installer.install(self.bundle, self.skill, self.apps, self.skills, full, (codex, claude))
        for root in (self.apps, self.skills, codex):
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(list(claude.iterdir()), [target])
        self.assertEqual((target / "other").read_text(), "unrelated")

    def test_alias_with_newline_target_cannot_serialize_app_path(self):
        target = self.apps.with_name("Applications with\nnewline")
        target.mkdir()
        self.apps.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(self.skills.exists())
