"""Locate the pinned CLI source used by the desktop integration fixtures."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = os.environ.get("CLI_MANAGER_LEGACY_SOURCE")
LEGACY = Path(source).expanduser().resolve() if source else ROOT.parent / "cli-manager"
