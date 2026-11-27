"""App publication uses atomic macOS renames on the destination volume."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublishAppTest(unittest.TestCase):
    def test_existing_preview_is_preserved_and_default_is_swapped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "stage" / "CLI Manager.app"
            destination = root / "output" / "CLI Manager.app"
            for app, marker in ((destination, "original"), (source, "replacement")):
                app.mkdir(parents=True)
                (app / "marker").write_text(marker)
            script = ROOT / "scripts" / "publish-app.swift"

            def publish(mode):
                return subprocess.run(["/usr/bin/swift", script, mode, source, destination],
                                      capture_output=True, text=True, timeout=15)

            refused = publish("exclusive")
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual((destination / "marker").read_text(), "original")
            self.assertEqual((source / "marker").read_text(), "replacement")

            replaced = publish("replace")
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual((destination / "marker").read_text(), "replacement")
            self.assertEqual((source / "marker").read_text(), "original")
