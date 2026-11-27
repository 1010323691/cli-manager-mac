"""Seed two distinct profiles and workspaces for signed-app dock verification."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from seed_status_ui import seed, Store


def coexistence_bridge(root, bundle):
    wrapper = root / "resume-fixture.py"
    entry = bundle.resolve() / "Contents" / "Resources" / "desktop.py"
    wrapper.write_text(f"""import json, os, sys
from pathlib import Path
state = Path(os.environ["CLI_MANAGER_STATE"])
marker = state / "coexist-ended-once"
if sys.argv[1:4] == ["managed-output", "managed-fixture", "managed-attempt"] and marker.exists():
    marker.rename(state / "coexist-ended-served")
    print(json.dumps({{"schema": 1, "attempt_id": "managed-attempt", "state": "ended", "entries": []}}))
else:
    os.execv(sys.executable, [sys.executable, "-I", "-B", {str(entry)!r}, *sys.argv[1:]])
""")
    return wrapper


def set_coexistence(state, task, attempt, active):
    store = Store(state)
    try:
        status = "running" if active else "review"
        if active:
            run = store.attempt_dir(attempt) / "runs" / "run-resumed-fixture"
            run.mkdir(parents=True, exist_ok=True)
            item = {"id": "coexist-message", "type": "agentMessage", "phase": "commentary",
                    "text": "托管任务在同一尝试中继续。"}
            (run / "events.jsonl").write_text(json.dumps({
                "dir": "in", "method": "item/completed", "params": {"item": item}
            }, ensure_ascii=False) + "\n")
            attempt = {**attempt, "logs": str(run), "start": time.time()}
            (state / "coexist-ended-once").touch()
        with store.transaction():
            store.put("tasks", {**task, "state": status})
            store.put("attempts", {**attempt, "state": status})
    finally:
        store.db.close()


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
        wrapper = coexistence_bridge(root, options.bundle)
        args_index = command.index("--args")
        command[args_index:args_index] = ["--env", f"CLI_MANAGER_BRIDGE={wrapper}"]
        command += ["--capture-dock-fixture", str(evidence)]
    subprocess.run(command, check=True, timeout=15)
    if options.manual:
        return
    deadline = time.monotonic() + 65
    marker = evidence / "dock-result"
    request = state / "coexist-request"
    response = state / "coexist-response"
    phase = ""
    while not marker.exists() and time.monotonic() < deadline:
        desired = request.read_text() if request.exists() else ""
        if desired != phase and desired in {"active", "ended"}:
            set_coexistence(state, managed, attempt, active=desired == "active")
            response.write_text(desired)
            phase = desired
        time.sleep(.1)
    if not marker.exists():
        raise TimeoutError("Embedded terminal UI check did not finish")
    output = marker.read_text()
    if not output.startswith("Two native PTYs preserved"):
        raise RuntimeError(output)
    print(output.strip())


if __name__ == "__main__":
    main()
