"""Seed two distinct profiles and workspaces for signed-app dock verification."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from seed_status_ui import seed, Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manual", action="store_true")
    options = parser.parse_args()
    root = options.root.resolve()
    seed(root, os.getpid())
    state = root / "state"
    store = Store(state)
    managed = store.get("tasks", "managed-fixture")
    attempt = store.get("attempts", managed["attempt"])
    store.put("attempts", {**attempt, "state": "review"})
    store.put("tasks", {**managed, "state": "review"})
    for identity, project in (("codex-active", "Project Alpha"), ("codex-idle", "Project Beta")):
        (store.instance_dir(identity) / "cli" / "fixture-mode").write_text("dock")
        folder = root / project
        folder.mkdir()
        (folder / "fixture-file.txt").write_text(f"Isolated example {project}\n")
    store.db.close()
    (state / ".dock-ui-fixture").touch()
    command = ["/usr/bin/open", "-n", "-a", str(options.bundle.resolve()),
               "--env", f"CLI_MANAGER_STATE={state}", "--env", f"CLI_MANAGER_PYTHON={sys.executable}",
               "--args", "--manage"]
    evidence = options.evidence.resolve()
    if not options.manual:
        command += ["--capture-dock-fixture", str(evidence)]
    subprocess.run(command, check=True, timeout=15)
    if options.manual:
        return
    deadline = time.monotonic() + 45
    marker = evidence / "dock-result"
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    if not marker.exists():
        raise TimeoutError("Embedded terminal UI check did not finish")
    output = marker.read_text()
    if not output.startswith("Two native PTYs preserved"):
        raise RuntimeError(output)
    print(output.strip())


if __name__ == "__main__":
    main()
