"""Launch a signed desktop build against synthetic quota schedules and aliases."""
import os
import subprocess
import sys
import time
from pathlib import Path

from seed_status_ui import seed, Store


def main(root, bundle):
    seed(root, os.getpid())
    state = root / "state"
    store = Store(state)
    managed = store.get("tasks", "managed-fixture")
    attempt = store.get("attempts", managed["attempt"])
    store.put("attempts", {**attempt, "state": "review"})
    store.put("tasks", {**managed, "state": "review"})
    primary = store.get("instances", "codex-active")
    alias = store.get("instances", "codex-idle")
    store.put("instances", {**alias, "bound_account": primary["bound_account"], "status": primary["status"]})
    (store.instance_dir("codex-idle") / "cli/fixture-account").write_text("codex-active")
    for instance in store.rows("instances"):
        (store.instance_dir(instance["id"]) / "cli/fixture-query-count").touch()
    due = store.get("instances", "codex-quota")
    idle = store.get("instances", "codex-active")
    now = time.time()
    for item, used, reset in [(due, 100, now + 2), (idle, 24, now + 86400)]:
        account = item["bound_account"]
        store.db.execute("DELETE FROM account_quota WHERE account=?", (account,))
        store._put_quota_signal(account, "window", now - 7200, {"bucket": "codex", "window": "primary",
                                                               "minutes": 300, "used": used, "resets_at": reset})
    (store.instance_dir("codex-quota") / "cli/fixture-mode").write_text("normal")
    store.db.close()
    subprocess.run(["/usr/bin/open", "-n", "-a", str(bundle.resolve()),
                    "--env", f"CLI_MANAGER_STATE={state}", "--env", f"CLI_MANAGER_PYTHON={sys.executable}"],
                   check=True, timeout=15)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
