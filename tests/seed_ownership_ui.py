"""Seed the exact queue inspector with isolated CLI tasks and structured requests."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from legacy_source import LEGACY, ROOT
sys.path.insert(0, str(LEGACY / "scripts"))
from cli_manager import controller
from cli_manager.store import Store, write_json


def seed(state, pid):
    if state.exists():
        raise SystemExit("Choose a new absent fixture directory.")
    state.mkdir(mode=0o700)
    env = {**os.environ, "CLI_MANAGER_STATE": str(state)}
    env.pop("CLI_DELEGATE_DEPTH", None)

    def call(*arguments):
        return json.loads(subprocess.check_output(
            [sys.executable, "-I", "-B", str(ROOT / "bridge/desktop.py"), *map(str, arguments)],
            env=env, text=True))

    fixture = state.parent / "claude-fixture-provider"
    shutil.copyfile(ROOT / "tests/fixture-decision-provider", fixture)
    fixture.chmod(0o700)
    call("create", "claude-fixture", "--backend", "claude", "--parallel", 2, "--bin", fixture)
    receipt = call("planner", "open")["receipt"]
    project = state.parent / "fixture-project"
    project.mkdir(exist_ok=False)
    (project / "fixture-file.txt").write_text("isolated")
    subprocess.run(["/usr/bin/git", "-C", project, "init", "--template=", "-q"], check=True)
    subprocess.run(["/usr/bin/git", "-C", project, "add", "."], check=True)
    subprocess.run(["/usr/bin/git", "-C", project, "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    tasks = [
        ("review-isolated-change", ("planner", "--receipt", receipt),
         [{"kind": "question", "tool_name": "AskUserQuestion"}, {"kind": "permission", "tool_name": "Read"}]),
        ("legacy-queue-task", ("legacy",), [{"kind": "permission", "tool_name": "Read"}]),
    ]
    store = Store(state)
    for name, prefix, requests in tasks:
        call(*prefix, "task", "submit", "--id", name, "--project", project, "--instance", "claude-fixture",
             "--kind", "review", "--prompt", "Isolated UI fixture")
        task = store.get("tasks", name)
        run = state / "fixture-logs" / name
        attempt = {"id": f"{name}-attempt", "task": name, "instance": "claude-fixture", "state": "running",
                   "pid": pid, "identity": controller.identity(pid), "logs": str(run), "reservation": "fixture"}
        store.put("attempts", attempt)
        store.put("tasks", {**task, "state": "running", "attempt": attempt["id"]})
        for index, request in enumerate(requests):
            write_json(run / "approvals" / f"{index}.request.json",
                       {**request, "expires_at": time.time() + 600, "input": {"private": "not for desktop"}})
    store.db.close()
    (state / ".decision-ui-fixture").touch()


if __name__ == "__main__":
    seed(Path(sys.argv[1]).resolve(), int(sys.argv[2]))
