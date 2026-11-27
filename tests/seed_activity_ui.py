"""Drive a signed debug app against fixture state and local fake providers."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from seed_status_ui import seed, controller, Store


def add_activity(store, pid, phase):
    tasks = [("audit-bridge", "codex-active"), ("review-interface", "codex-idle")]
    stamp = time.time()
    with store.transaction():
        for index, (task, instance) in enumerate(tasks):
            attempt = f"fixture-attempt-{task}"
            verified = phase == "active" or phase == "one" and index == 0
            state = ("running" if index == 0 else "stopping") if verified else "review"
            store.put("tasks", {"id": task, "state": state, "kind": "review",
                                "allowed": [instance], "attempt": attempt})
            store.put("attempts", {"id": attempt, "task": task, "instance": instance,
                                   "state": state, "pid": pid, "identity": controller.identity(pid),
                                   "start": stamp + index / 10})
    # An unverified managed record should never appear in the activity overlay.
    store.put("tasks", {"id": "unverified-fixture", "state": "running", "attempt": "unverified-attempt"})
    store.put("attempts", {"id": "unverified-attempt", "task": "unverified-fixture",
                           "instance": "codex-active", "state": "running", "pid": pid,
                           "identity": "mismatched-fixture-identity"})


def setup(root, pid):
    seed(root, pid)
    state = root / "state"
    store = Store(state)
    add_activity(store, pid, "empty")
    managed = store.get("tasks", "managed-fixture")
    store.put("tasks", {**managed, "state": "review"})
    first_attempt = store.get("attempts", managed["attempt"])
    store.put("attempts", {**first_attempt, "state": "review"})
    store.put("tasks", {"id": "queued-fixture", "state": "queued", "kind": "review", "allowed": ["codex-idle"]})
    instance = store.get("instances", "codex-active")
    measured = time.time() - controller.PROBE_TTL - 15
    past_window = {"bucket": "codex", "window": "primary", "used": 17, "minutes": 300,
                   "resets_at": time.time() + 3600}
    store.db.execute("DELETE FROM account_quota WHERE account=?", (instance["bound_account"],))
    store._put_quota_signal(instance["bound_account"], "window", measured, past_window)
    store.db.close()
    (state / ".activity-ui-fixture").touch()
    return state


def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument("root", type=Path)
    arguments.add_argument("bundle", type=Path)
    arguments.add_argument("evidence", type=Path)
    arguments.add_argument("--manual", action="store_true")
    options = arguments.parse_args()
    root, bundle, evidence = options.root.resolve(), options.bundle.resolve(), options.evidence.resolve()
    with subprocess.Popen(["/bin/sleep", "150"]) as worker:
        try:
            state = setup(root, worker.pid)
            env = {**os.environ, "CLI_MANAGER_STATE": str(state)}
            env.pop("CLI_DELEGATE_V2_STATE", None)
            env.pop("CLI_MANAGER_BRIDGE", None)
            command = ["/usr/bin/open", "-n", "-a", str(bundle), "--env", f"CLI_MANAGER_STATE={state}",
                       "--env", f"CLI_MANAGER_PYTHON={sys.executable}", "--args", "--manage"]
            if not options.manual:
                command += ["--capture-activity-fixture", str(evidence)]
            subprocess.run(command, env=env, check=True, timeout=15)
            if options.manual:
                (state / "activity-phase").write_text("active")
            deadline = time.monotonic() + (130 if options.manual else 70)
            last = None
            while time.monotonic() < deadline:
                if (evidence / "activity-result").exists():
                    result = (evidence / "activity-result").read_text()
                    if not result.startswith("Stable native coordinates."):
                        raise RuntimeError(result)
                    print(result.strip())
                    return
                request = state / "activity-phase"
                if request.exists() and (phase := request.read_text()) != last:
                    if phase in {"active", "one", "empty"}:
                        store = Store(state)
                        try:
                            add_activity(store, worker.pid, phase)
                        finally:
                            store.db.close()
                        last = phase
                time.sleep(.1)
            raise TimeoutError("Isolated UI driver timed out")
        finally:
            worker.terminate()


if __name__ == "__main__":
    main()
