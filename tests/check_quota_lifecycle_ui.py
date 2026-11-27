"""Signed-app smoke test for quota startup, execution and reset triggers."""
import os
import sys
import time
from pathlib import Path

from seed_quota_ui import main as launch
from seed_status_ui import Store, controller


def check(root, bundle):
    launch(root, bundle)
    state = root / "state"
    store = Store(state)
    names = [item["id"] for item in store.rows("instances")]

    def counts():
        return {name: len((store.instance_dir(name) / "cli/fixture-query-count").read_text().splitlines())
                for name in names}

    def wait_for(expected, label):
        deadline = time.monotonic() + 24
        while time.monotonic() < deadline:
            current = counts()
            if current == expected:
                print(f"{label}: {current}", flush=True)
                return
            if any(current[name] > expected[name] for name in names):
                raise AssertionError(f"{label}: {current} exceeded {expected}")
            time.sleep(.1)
        raise AssertionError(f"{label}: observed {counts()}, expected {expected}")

    baseline = {name: int(name in {"codex-active", "codex-both", "codex-query", "codex-quota"})
                for name in names}
    wait_for(baseline, "startup")
    current = store.get("instances", "codex-active")
    job = {"id": "fixture-quota-lifecycle", "state": "running", "kind": "review", "attempt": "quota-attempt"}
    stamp = time.time()
    attempt = {"id": "quota-attempt", "task": job["id"], "instance": current["id"], "state": "running",
               "account": current["bound_account"], "generation": current["generation"],
               "start": stamp, "pid": os.getpid(), "identity": controller.identity(os.getpid()),
               "logs": "quota-lifecycle-fixture"}
    with store.transaction():
        store.put("tasks", job)
        store.put("attempts", attempt)
    baseline["codex-active"] += 1
    wait_for(baseline, "managed start")
    with store.transaction():
        store.put("attempts", {**attempt, "state": "review", "end": time.time()})
        store.put("tasks", {**job, "state": "review"})
    baseline["codex-active"] += 1
    wait_for(baseline, "managed end")
    full = store.get("instances", "codex-quota")
    stamp = time.time()
    reset = stamp + 2
    store._put_quota_signal(full["bound_account"], "window", stamp, {"bucket": "codex", "window": "primary",
                                                                    "minutes": 300, "used": 35, "resets_at": reset})
    baseline["codex-quota"] += 1
    wait_for(baseline, "quota reset")
    assert time.time() >= reset
    store.db.close()


if __name__ == "__main__":
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
