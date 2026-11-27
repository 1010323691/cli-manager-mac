"""App publication uses atomic macOS renames on the destination volume."""
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "scripts" / "publish-app.swift"


class PublishAppTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.source = root / "stage" / "CLI Manager.app"
        self.destination = root / "output" / "CLI Manager.app"
        self.destination.parent.mkdir()

    def signed_bundle(self, path, marker):
        contents = path / "Contents"
        binaries = contents / "MacOS"
        resources = contents / "Resources"
        binaries.mkdir(parents=True)
        resources.mkdir()
        binary = binaries / "CLIManager"
        shutil.copyfile("/bin/echo", binary)
        binary.chmod(0o755)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.myskills.cli-manager-macos",
            "CFBundleExecutable": "CLIManager",
            "CFBundlePackageType": "APPL",
        }))
        (resources / "marker").write_text(marker)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", path],
                       check=True, capture_output=True, timeout=10)

    def publish(self, mode):
        return subprocess.run(["/usr/bin/swift", PUBLISHER, mode, self.source, self.destination],
                              capture_output=True, text=True, timeout=15)

    def test_existing_unrelated_directory_is_preserved(self):
        self.signed_bundle(self.source, "replacement")
        self.destination.mkdir()
        marker = self.destination / "personal-file"
        marker.write_text("keep")
        for mode in ("exclusive", "replace"):
            self.assertNotEqual(self.publish(mode).returncode, 0)
            self.assertEqual(marker.read_text(), "keep")
            self.assertTrue(self.source.is_dir())

    def test_default_replaces_only_verified_installed_bundle(self):
        self.signed_bundle(self.source, "replacement")
        self.signed_bundle(self.destination, "original")
        self.assertNotEqual(self.publish("exclusive").returncode, 0)
        result = self.publish("replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / "Contents/Resources/marker").read_text(), "replacement")
        self.assertFalse(self.source.exists())
