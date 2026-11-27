"""Use an archived copy of the exact CLI revision used by the app bundle."""
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
source = os.environ.get("CLI_MANAGER_LEGACY_SOURCE")
checkout = Path(source).expanduser().resolve() if source else ROOT.parent / "cli-manager"
revision = (ROOT / "resources" / "engine-revision").read_text().strip()
export = TemporaryDirectory(prefix="cli-manager-test-engine-")
LEGACY = Path(export.name)
archive = subprocess.check_output(["/usr/bin/git", "-C", checkout, "archive", revision, "--", "."])
subprocess.run(["/usr/bin/tar", "-xf", "-", "-C", LEGACY], input=archive, check=True)
if not (LEGACY / "scripts" / "delegate.py").is_file():
    raise RuntimeError("Fixed CLI source is missing. Check CLI_MANAGER_LEGACY_SOURCE.")
os.environ["CLI_MANAGER_LEGACY_SOURCE"] = str(LEGACY)
