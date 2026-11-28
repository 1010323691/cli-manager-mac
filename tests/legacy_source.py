"""Use the engine the app bundle ships: this repository's, or a pinned external checkout."""
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
source = os.environ.get("CLI_MANAGER_LEGACY_SOURCE")
export = TemporaryDirectory(prefix="cli-manager-test-engine-")
LEGACY = Path(export.name)
if source:
    checkout = Path(source).expanduser().resolve()
    revision = (ROOT / "resources" / "engine-revision").read_text().strip()
    archive = subprocess.check_output(["/usr/bin/git", "-C", checkout, "archive", revision, "--", "."])
else:
    archive = subprocess.check_output(["/usr/bin/tar", "-cf", "-", "-C", ROOT / "engine", "--exclude", "__pycache__", "."])
subprocess.run(["/usr/bin/tar", "-xf", "-", "-C", LEGACY], input=archive, check=True)
if not (LEGACY / "scripts" / "delegate.py").is_file():
    raise RuntimeError("Fixed CLI source is missing. Check CLI_MANAGER_LEGACY_SOURCE.")
os.environ["CLI_MANAGER_LEGACY_SOURCE"] = str(LEGACY)
