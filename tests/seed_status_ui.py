"""One isolated registry for the signed app refresh and terminal smoke tests."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from legacy_source import LEGACY, ROOT
sys.path.insert(0, str(LEGACY / "scripts"))
from cli_manager import backend, controller
from cli_manager.store import Store


def seed(root, live_pid):
    state = root / "state"
    project = root / "Selected Fixture Workspace"
    project.mkdir()
    (project / "fixture-file.txt").write_text("Fixture preview only.\n")
    executable = root / "codex-fixture"
    shutil.copyfile(ROOT / "tests/fixture-status-provider", executable)
    executable.chmod(0o700)
    env = {**os.environ, "CLI_MANAGER_STATE": str(state)}
    env.pop("CLI_DELEGATE_DEPTH", None)
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    command = [sys.executable, "-I", "-B", str(ROOT / "bridge/desktop.py")]

    def bridge(*args):
        return json.loads(subprocess.check_output([*command, *map(str, args)], env=env, text=True))

    store = Store(state)
    fixtures = [
        ("codex-quota", "limited", True, None),
        ("codex-query", "dual", True, "failure"),
        ("codex-both", "limited", True, "failure"),
        ("codex-active", "normal", True, None),
        ("codex-idle", "dual", True, None),
        ("codex-signed-out", "signed-out", False, None),
        ("codex-expired", "signed-out", True, None),
    ]
    for name, initial, bound, final in fixtures:
        bridge("create", name, "--backend", "codex", "--parallel", "2", "--bin", executable)
        row = store.get("instances", name)
        if bound:
            row.update(bound_account=backend.account_key("codex", f"{name}@example.invalid", f"workspace-{name}"),
                       bound_scoped=True)
            store.put("instances", row)
        mode = store.instance_dir(name) / "cli" / "fixture-mode"
        mode.write_text(initial)
        guard = next(item["guard"] for item in bridge("snapshot")["instances"] if item["id"] == name)
        bridge("refresh", name, "--expect", guard)
        if final:
            mode.write_text(final)
            bridge("refresh", name, "--expect", guard)

    store.put("tasks", {"id": "managed-fixture", "state": "running", "kind": "review",
                        "allowed": ["codex-active"], "attempt": "managed-attempt"})
    store.put("attempts", {"id": "managed-attempt", "task": "managed-fixture", "instance": "codex-active",
                           "state": "running", "pid": live_pid, "identity": controller.identity(live_pid)})
    store.db.close()
    (state / ".status-ui-fixture").touch()


if __name__ == "__main__":
    seed(Path(sys.argv[1]).resolve(), int(sys.argv[2]))
