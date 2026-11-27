"""Create a disposable, synthetic state directory for checking the real .app."""
import shutil
import sys
import time
from pathlib import Path

from legacy_source import LEGACY, ROOT
sys.path.insert(0, str(LEGACY / "scripts"))
from cli_manager import backend, controller
from cli_manager.store import Store


def seed(path, active_pid):
    if path.exists() or path.is_symlink():
        raise SystemExit("Choose a new, absent fixture directory.")
    path.mkdir(mode=0o700)
    store = Store(path)
    now = time.time()
    codex_fixture = path / "codex-fixture"
    shutil.copyfile(LEGACY / "tests/fixture_cli.py", codex_fixture)
    codex_fixture.chmod(0o700)
    fixtures = [
        ("codex-studio", "codex", "plus", [(22, 300, now - 4), (58, 10080, now - 5)]),
        ("codex-review", "codex", "team", [(35, 300, now - 330)]),
        ("claude-research", "claude", None, []),
        ("claude-spare", "claude", None, []),
    ]
    for name, provider, plan, windows in fixtures:
        fixture = ROOT / "tests/fixture-provider" if provider == "claude" else codex_fixture
        binary = str(fixture)
        key = backend.account_key(provider, name, "fixture")
        logged_in = bool(windows)
        scope = name != "claude-spare"
        instance = {
            "id": name, "provider": provider, "generation": 0, "parallel": 2, "binary": binary,
            "bound_account": key if scope else None, "bound_scoped": scope, "probed_at": now - 5,
            "status": {"schema": backend.PROBE_SCHEMA, "version": "CLI UI Fixture", "logged_in": logged_in,
                       "account": key if logged_in else None, "plan": plan, "quota": {"checked_at": now - 5}},
        }
        store.instance_dir(name).mkdir(mode=0o700, parents=True)
        store.put("instances", instance)
        for idx, (used, minutes, measured) in enumerate(windows):
            sample = {"bucket": provider, "window": ("primary", "secondary")[idx], "minutes": minutes,
                      "used": used, "resets_at": now + (minutes * 40)}
            store._put_quota_signal(key, "window", measured, sample)
    for task, state, name, why in [
        ("audit-workspace", "running", "codex-studio", None),
        ("review-diff", "stopping", "codex-review", None),
        ("docs-refresh", "queued", "codex-studio", "dependencies"),
        ("outline", "waiting_quota", "codex-review", "eligible_instance"),
        ("readme-review", "review", "codex-studio", None),
        ("draft-notes", "cancelled", "codex-review", None),
    ]:
        active = state in controller.ACTIVE
        attempt = f"{task}-attempt" if active else None
        store.put("tasks", {"id": task, "state": state, "kind": "review", "allowed": [name],
                            "handoff_targets": [], "attempt": attempt, "blocked_by": why,
                            "wait_until": now + 300 if state == "waiting_quota" else None})
        if active:
            store.put("attempts", {"id": attempt, "task": task, "state": state, "instance": name,
                                   "pid": active_pid, "identity": controller.identity(active_pid),
                                   "reserved": now - 20})
    store.db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 -B tests/seed_ui.py NEW_FIXTURE_DIR LIVE_FIXTURE_PID")
    seed(Path(sys.argv[1]).absolute(), int(sys.argv[2]))
