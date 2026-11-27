#!/usr/bin/env python3
"""Allowlisted desktop API built on the unchanged CLI Manager engine."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Resources inside the signed app bundle must not acquire Python cache files,
# including when this launcher is called directly without the -B option.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
LEGACY = HERE / "Legacy"
if not LEGACY.is_dir() and (HERE.parent / "Package.swift").is_file():
    source = os.environ.get("CLI_MANAGER_LEGACY_SOURCE")
    LEGACY = Path(source).expanduser().resolve() if source else HERE.parent.parent / "cli-manager"
sys.path[:0] = [str(LEGACY / "scripts"), str(HERE)]

import delegate
import ownership
import probes
from cli_manager import backend, controller, store as state_module
from cli_manager.store import DelegateError, Store, digest, identifier, identity, write_json

SCHEMA = 1


def state_root():
    configured = os.environ.get("CLI_MANAGER_STATE") or os.environ.get("CLI_DELEGATE_V2_STATE")
    return Path(configured or "~/.cli-delegate-v2").expanduser().resolve()


class SnapshotStore:
    rows = Store.rows
    get = Store.get
    unpack = staticmethod(Store.unpack)
    instance_dir = Store.instance_dir

    def __init__(self, root):
        self.root = root
        stat = root.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise DelegateError("状态目录须由当前用户拥有并设为 0700")
        database = root / "state.sqlite3"
        if not database.is_file():
            raise FileNotFoundError(database)
        # Read ongoing WAL transactions without initializing or migrating any tables.
        self.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")

    def quota_signals(self, account):
        columns = {col["name"] for col in self.db.execute("PRAGMA table_info(account_quota)")}
        if "bucket" in columns:
            return Store.quota_signals(self, account)
        if not columns:
            return None
        # Early v2 stores packed their windows into one row per account.
        result = []
        for kind, stamp, value in Store.quota_signals(self, account):
            if kind == "windows":
                result.extend(("window", stamp, window) for window in value)
            elif kind == "blocked":
                result.append((kind, stamp, value))
        return result


def registration_for(store, instance):
    try:
        info = store.instance_dir(instance["id"]).stat()
        return [info.st_dev, info.st_ino, getattr(info, "st_birthtime", None)]
    except FileNotFoundError:
        return None


def guard_for(store, instance, *, registration=None):
    if registration is None:
        registration = registration_for(store, instance)
    return digest([instance["id"], instance["provider"], instance["generation"],
                   instance.get("bound_account"), registration])


STATE_COLUMNS = {
    "instances": [("id", "TEXT"), ("data", "TEXT")],
    "tasks": [("id", "TEXT"), ("state", "TEXT"), ("data", "TEXT")],
    "attempts": [("id", "TEXT"), ("task", "TEXT"), ("instance", "TEXT"), ("state", "TEXT"), ("data", "TEXT")],
    "events": [("seq", "INTEGER"), ("task", "TEXT"), ("time", "REAL"), ("type", "TEXT"), ("data", "TEXT")],
    "account_quota": [("account", "TEXT"), ("kind", "TEXT"), ("bucket", "TEXT"), ("window", "TEXT"),
                      ("measured", "REAL"), ("data", "TEXT")],
}


class EngineStore(Store):
    """Open existing state without invoking the frozen engine's schema migration."""
    def __init__(self, root=None):
        self.root = root if root is not None else state_root()
        database = self.root / "state.sqlite3"
        if not database.exists():
            super().__init__(self.root)
            return
        stat = self.root.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise DelegateError("状态目录须由当前用户拥有并设为 0700")
        self.db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("BEGIN")
            names = {row["name"] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            columns = {table: [(row["name"], row["type"]) for row in self.db.execute(f"PRAGMA table_info({table})")]
                       for table in STATE_COLUMNS}
            compatible = (names == set(STATE_COLUMNS) and columns == STATE_COLUMNS and
                          self.db.execute("PRAGMA user_version").fetchone()[0] == 0 and
                          self.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
            self.db.rollback()
            if not compatible:
                raise DelegateError("共享状态库版本与此应用不同，请用匹配的 CLI 或更新应用")
            self.db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.db.close()
            raise


    def put(self, table, record):
        if table == "instances":
            probes.preserve(Store.get(self, "instances", record["id"], required=False), record)
        return super().put(table, record)


class GuardedStore(EngineStore):
    def __init__(self, name, expected):
        super().__init__()
        self.expected = expected
        self.name = name
        self.observation = None
        self.probe_committed = False
        self.probe_quota_observed = False

    def put(self, table, record):
        super().put(table, record)
        if table == "instances" and self.observation is not None:
            self.probe_committed = True
            status = record.get("status") or {}
            sample = status.get("quota") or {}
            self.probe_quota_observed = (login_state(self, record, status) == "signed_in"
                                         and sample.get("quality") == "observed" and bool(sample.get("windows")))
            if record.get("probe_error"):
                record[probes.TRACE] = self.observation.result(record["probed_at"])
                Store.put(self, table, record)

    def get(self, table, key, **options):
        record = super().get(table, key, **options)
        if table == "instances" and key == self.name and self.expected is not None:
            if record is None or guard_for(self, record) != self.expected:
                raise DelegateError("实例或账户代次已变化，请重新选择")
            self.expected = None
        return record


class PlannerStore(EngineStore):
    def __init__(self, owner, args):
        super().__init__()
        self.owner = owner
        self.operation = args.command_1
        self.target = args.id
        self.check = None

    def put(self, table, record):
        if table == "tasks" and self.operation == "submit":
            record[ownership.RECORD] = self.owner
        return super().put(table, record)

    def get(self, table, key, **options):
        record = super().get(table, key, **options)
        if table == "tasks" and key == self.target and self.operation != "submit":
            ownership.require_owner(record, self.owner)
            if self.check is not None:
                ownership.checked_request(self, record, *self.check)
        return record


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def auth_is_active(store, name):
    location = store.root / "locks" / f"instance-{name.casefold()}"
    try:
        with location.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def login_state(store, instance, status):
    if instance.get("auth_busy"):
        return "authenticating" if auth_is_active(store, instance["id"]) else "auth_interrupted"
    if instance.get("probe_error") or not status or status.get("schema") != backend.PROBE_SCHEMA:
        return "unknown"
    if not instance.get("probed_at") and instance.get("invalidations", 0):
        return "invalidated"
    if not status.get("logged_in"):
        return "expired" if instance.get("bound_account") else "signed_out"
    if status.get("rebind_required") or (instance["provider"] == "codex" and
                                         instance.get("bound_account") and not instance.get("bound_scoped")):
        return "rebind_required"
    if not instance.get("bound_account") or status.get("account") != instance["bound_account"]:
        return "unbound"
    return "signed_in"


def cached_signals(sample):
    for observation in (sample.get("last_observation") or {}, sample):
        measured = observation.get("observed_at") or observation.get("checked_at")
        for window in observation.get("windows") or []:
            yield "window", window.get("observed_at", measured), window
        if type(observation.get("explicitly_blocked")) is bool:
            stamp = observation.get("blocked_checked_at") or observation.get("checked_at")
            yield "blocked", stamp, observation["explicitly_blocked"]


def terminal_usage(store, instance, account):
    if not account:
        return None
    try:
        marker = json.loads((store.instance_dir(instance["id"]) / "desktop-terminal-usage.json").read_bytes())
    except (OSError, ValueError):
        return None
    if isinstance(marker, dict) and marker.get("account") == account:
        return marker
    return None


def quota_view(status, cooldown_until, signals, now, used_at):
    sample = status.get("quota") or {}
    latest = {}
    for kind, raw_stamp, value in signals:
        stamp = number(raw_stamp)
        if stamp is None:
            continue
        key = (kind, value.get("bucket"), value.get("window")) if kind == "window" else (kind, None, None)
        if key not in latest or stamp >= latest[key][0]:
            latest[key] = (stamp, value)

    windows = []
    resets = []
    for (kind, bucket, window), (stamp, value) in latest.items():
        if kind != "window":
            continue
        used, reset = number(value.get("used")), number(value.get("resets_at"))
        if used is None or not 0 <= used <= 100:
            continue
        if reset is not None and reset <= 0:
            reset = None
        # The desktop's cache has no age TTL. The engine's short dispatch TTL
        # remains in the unchanged legacy eligibility logic.
        reset_due = used > 0 and reset is not None and reset <= now
        confirmed_limit = used == 100 and reset is not None and now < reset
        used_since = not confirmed_limit and used_at is not None and used_at > stamp
        stale = "reset" if reset_due else "usage" if used_since else None
        if used > 0 and reset is not None:
            resets.append({"bucket": bucket, "window": window, "resets_at": reset})
        windows.append({"bucket": bucket, "window": window, "remaining_percent": 100 - used,
                        "observed_at": stamp, "resets_at": reset, "minutes": number(value.get("minutes")),
                        "expired": stale is not None, "refresh_reason": stale})
    live = [row["remaining_percent"] for row in windows if not row["expired"]]
    signal = latest.get(("blocked", None, None))
    blocked_at = None
    if signal and signal[1] and (used_at is None or used_at <= signal[0]):
        if not any(row["refresh_reason"] == "reset" and signal[0] <= row["resets_at"] for row in windows):
            blocked_at = signal[0]
    if blocked_at is not None or cooldown_until or 0 in live:
        state = "limited"
    elif live:
        state = "observed"
    else:
        state = "expired" if windows else "unknown"
    full_resets = [row["resets_at"] for row in windows if not row["expired"] and row["remaining_percent"] == 0]
    reset = max([*(value for value in full_resets if value), cooldown_until or 0])
    return {"state": state, "windows": windows, "remaining_percent": min(live, default=None),
            "observed_at": max((row["observed_at"] for row in windows), default=None),
            "checked_at": number(sample.get("checked_at")), "blocked_at": blocked_at,
            "cooldown_until": cooldown_until, "reset_events": resets,
            "next_reset_at": min((event["resets_at"] for event in resets), default=None),
            "limit_reset_at": reset if reset and blocked_at is None and all(full_resets) else None}


def activity(attempt, now):
    pid = attempt.get("pid")
    reserved = number(attempt.get("reserved"))
    if not pid and attempt["state"] == "starting" and reserved is not None and 0 <= now - reserved < 15:
        return "stopping" if attempt.get("stop") else "starting"
    try:
        if pid and attempt.get("identity") and identity(pid) == attempt["identity"]:
            return "stopping" if attempt.get("stop") else attempt["state"]
    except (OSError, subprocess.SubprocessError):
        pass
    return "unverified"


def snapshot():
    now = time.time()
    result = {"schema": SCHEMA, "snapshot_at": now, "instances": [], "tasks": [], "live_attempts": []}
    try:
        store = SnapshotStore(state_root())
    except FileNotFoundError:
        return result
    try:
        store.db.execute("BEGIN")
        instances = store.rows("instances")
        attempts = store.rows("attempts")
        tasks = store.rows("tasks")
        accounts = {instance.get("bound_account") for instance in instances} - {None}
        quota = {account: store.quota_signals(account) for account in accounts}
    finally:
        store.db.close()

    history = {}
    active = {}
    running = {}
    cooldown = {}
    last_usage = {}
    usage_events = {}
    for instance in instances:
        account = instance.get("bound_account")
        marker = terminal_usage(store, instance, account) or {}
        at = number(marker.get("at"))
        if at is not None:
            last_usage[account] = max(at, last_usage.get(account, 0))
        ended = number(marker.get("ended_at"))
        if ended is not None and marker.get("generation") == instance["generation"]:
            usage_events[(instance["id"], account, instance["generation"])] = {"ended": ended}
    for attempt in attempts:
        history[attempt["id"]] = attempt
        if attempt["state"] in controller.ACTIVE:
            state = activity(attempt, now)
            active[attempt["id"]] = state
            running.setdefault(attempt["instance"], []).append({"id": attempt["task"], "state": state})
        until = number(attempt.get("limited_until"))
        account = attempt.get("account")
        if account and until and until > now:
            cooldown[account] = max(until, cooldown.get(account, 0))
        if account and attempt.get("logs"):
            started = number(attempt.get("start"))
            if started is not None:
                ended = number(attempt.get("end"))
                at = max(started, ended or 0)
                last_usage[account] = max(at, last_usage.get(account, 0))
                key = (attempt["instance"], account, attempt.get("generation"))
                events = usage_events.setdefault(key, {})
                # Resumed attempts may still carry the previous run's end until they finish.
                if active.get(attempt["id"]) in controller.ACTIVE or attempt["state"] not in controller.ACTIVE:
                    events["started"] = max(started, events.get("started", 0))
                if attempt["state"] not in controller.ACTIVE and ended is not None and ended >= started:
                    events["ended"] = max(ended, events.get("ended", 0))

    for instance in instances:
        name = instance["id"]
        status = instance.get("status") or {}
        account = instance.get("bound_account")
        login = login_state(store, instance, status)
        checked = number(instance.get("probed_at")) or None
        signals = quota.get(account)
        if signals is None:
            signals = cached_signals(status.get("quota") or {})
        previous_login = probes.last_auth(instance)
        display_quota = {"state": "unknown", "windows": []}
        if login == "signed_in":
            display_quota = quota_view(status, cooldown.get(account), signals, now,
                                       last_usage.get(account))
        elif instance.get("probe_error") and previous_login and previous_login["login"] == "signed_in":
            prior = quota_view(status, cooldown.get(account), quota.get(account) or (), now,
                               last_usage.get(account))
            confirmed = [row for row in prior["windows"] if not row["expired"] and row["remaining_percent"] == 0]
            if prior["state"] == "limited":
                display_quota = {**prior, "remaining_percent": None, "windows": confirmed}
            display_quota["next_reset_at"] = prior["next_reset_at"]
            display_quota["reset_events"] = prior["reset_events"]
            display_quota["unverified_windows"] = [
                {**row, "remaining_percent": None} for row in prior["windows"] if row not in confirmed
            ]
        registration = registration_for(store, instance)
        result["instances"].append({
            "id": name, "guard": guard_for(store, instance, registration=registration),
            "registration": digest([name, instance["provider"], registration]) if registration is not None else None,
            "provider": instance["provider"],
            "quota_group": digest([instance["provider"], account]) if account else None,
            "quota_usage": usage_events.get((name, account, instance["generation"])),
            "parallel": instance["parallel"], "login": login, "checked_at": checked,
            "login_is_stale": login in {"signed_in", "signed_out"} and
                              (checked is None or now - checked >= controller.PROBE_TTL),
            "version": status.get("version"), "plan": status.get("plan"),
            "probe_failed": bool(instance.get("probe_error")), "deleting": bool(instance.get("deleting")),
            "last_confirmed_auth": previous_login,
            "probe_diagnostic": (instance.get(probes.TRACE)
                                 if (instance.get(probes.TRACE) or {}).get("checked_at") == checked else None),
            "running": running.get(name, []),
            "quota": display_quota,
        })
    registered = {instance["id"] for instance in instances}
    for task in tasks:
        attempt_id = task.get("attempt")
        attempt = history.get(attempt_id) or {}
        live_state = active.get(attempt_id)
        if (task["state"] in controller.ACTIVE and live_state in controller.ACTIVE
                and attempt["task"] == task["id"] and attempt["instance"] in registered):
            result["live_attempts"].append({
                "task_id": task["id"], "attempt_id": attempt_id, "instance": attempt["instance"],
                "state": live_state, "since": number(attempt.get("start")) or number(attempt.get("reserved")),
            })
        result["tasks"].append({
            "id": task["id"], "kind": task.get("kind"), "state": task["state"],
            "activity": active.get(attempt.get("id")), "instance": attempt.get("instance"),
            "target": task.get("target"), "allowed": task.get("allowed") or [],
            "blocked_by": task.get("blocked_by"), "wait_until": task.get("wait_until") or None,
        })
    return result


def parse():
    parser = argparse.ArgumentParser(prog="cli-manager-desktop", allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("snapshot")
    commands.add_parser("inspect-decisions")
    terminal = commands.add_parser("terminal")
    terminal.add_argument("id", type=identifier)
    terminal.add_argument("--expect", required=True)
    terminal.add_argument("--directory", type=Path, required=True)
    create = commands.add_parser("create")
    create.add_argument("id", type=identifier)
    create.add_argument("--backend", required=True, choices=("codex", "claude"))
    create.add_argument("--parallel", type=delegate.positive, required=True)
    create.add_argument("--bin", type=delegate.path)
    for action in ("refresh", "login", "logout", "switch", "rebind", "delete"):
        command = commands.add_parser(action)
        command.add_argument("id", type=identifier)
        command.add_argument("--expect", required=True)
        if action == "delete":
            command.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def verify_terminal_account(store, instance, env):
    try:
        if instance["provider"] == "claude":
            status = backend.probe(store, instance, query_quota=False)
            return status.get("account") if status.get("logged_in") else None
        base = store.instance_dir(instance["id"])
        with tempfile.TemporaryDirectory(prefix="terminal-probe-", dir=base / "tmp") as temporary:
            app = backend.open_app(instance, base, env, Path(temporary), [])
            try:
                account, workspace, _ = backend.codex_identity(
                    app.request("account/read", {"refreshToken": False}, timeout=12))
                return account if workspace else None
            finally:
                app.close()
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError):
        raise DelegateError("无法核实实例的提供方账户") from None


def wait_foreground(command, directory, env, record_usage):
    # The native CLI and this wrapper share the foreground PTY process group.
    # Ctrl-C is delivered directly to the CLI without ending the wrapper/lock.
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}
    stop = None

    def request_stop(sig, _frame):
        nonlocal stop
        if stop is None:
            stop = (sig, time.monotonic() + 3)

    signal.signal(signal.SIGINT, lambda *_: None)
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, request_stop)
    child = None
    try:
        child = subprocess.Popen(command, cwd=directory, env=env)
        record_usage(False)
        forwarded = False
        while True:
            if stop is not None and not forwarded:
                child.send_signal(stop[0])
                forwarded = True
            if stop is not None and time.monotonic() >= stop[1]:
                child.kill()
                code = child.wait()
                break
            try:
                code = child.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        return code if code >= 0 else 128 - code
    finally:
        if child is not None:
            record_usage(True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def refresh_terminal_quota(instance, expected):
    # The caller already holds the instance lock. Use the same guarded probe
    # without opening another lock or changing account state.
    try:
        engine = GuardedStore(instance["id"], expected)
        try:
            with probes.observed_probe() as observation:
                engine.observation = observation
                controller.refresh_instance(engine, engine.get("instances", instance["id"]), force=True)
        finally:
            engine.db.close()
    except (DelegateError, OSError, sqlite3.Error):
        # Quota data is optional for a user terminal whose account was already verified.
        pass


def contains_directory(ancestor, descendant):
    reference = ancestor.stat()
    for path in (descendant, *descendant.parents):
        if os.path.samestat(reference, path.stat()):
            return True
    return False


def run_terminal(args):
    if not all(os.isatty(fd) for fd in (0, 1, 2)) or os.tcgetpgrp(0) != os.getpgrp():
        raise DelegateError("用户终端需要前台交互式 PTY")
    if not args.directory.is_absolute():
        raise DelegateError("请选择一个绝对工作目录")
    try:
        directory = args.directory.resolve(strict=True)
    except OSError:
        raise DelegateError("所选工作目录已不存在") from None
    root = state_root()
    if not directory.is_dir():
        raise DelegateError("所选工作目录不是目录")
    if contains_directory(root, directory) or contains_directory(directory, root):
        raise DelegateError("工作目录不能与 CLI Manager 的状态目录重叠")

    store = SnapshotStore(root)
    try:
        with controller.instance_lock(store, args.id):
            instance = store.get("instances", args.id)
            if guard_for(store, instance) != args.expect:
                raise DelegateError("实例或账户代次已变化，请重新选择")
            account = instance.get("bound_account")
            if (not account or instance.get("auth_busy") or instance.get("deleting")
                    or (instance["provider"] == "codex" and not instance.get("bound_scoped"))):
                raise DelegateError("此实例需要先登录并绑定账户")
            start = time.time()
            refresh_terminal_quota(instance, args.expect)
            env = backend.environment(store, instance)
            if verify_terminal_account(store, instance, env) != account:
                raise DelegateError("当前提供方账户或工作区与实例绑定不一致")
            binary = backend.binary(instance)
            if instance["provider"] == "codex":
                options = ["-c", backend.CODEX_AUTH, "--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
            else:
                options = ["--permission-mode", "default"]
            marker = store.instance_dir(args.id) / "desktop-terminal-usage.json"

            def record_usage(ended):
                try:
                    at = time.time() if ended else start
                    write_json(marker, {"account": account, "generation": instance["generation"],
                                        "at": at, "ended_at": at if ended else None})
                except OSError:
                    # Never interfere with the user's terminal if the optional marker cannot be saved.
                    pass

            # Inherit the PTY streams. Only the terminal emulator renders these bytes.
            return wait_foreground([binary, *options], directory, env, record_usage)
    finally:
        store.db.close()


def dispatch(args):
    action = args.operation
    if action == "snapshot":
        return snapshot()
    if action == "inspect-decisions":
        try:
            store = SnapshotStore(state_root())
        except FileNotFoundError:
            return {"schema": 1, "entries": []}
        try:
            store.db.execute("BEGIN")
            return ownership.inspect(store)
        finally:
            store.db.close()
    if action == "create":
        store = EngineStore()
        try:
            controller.create_instance(store, args.id, args.backend, str(args.bin) if args.bin else None,
                                       args.parallel)
        finally:
            store.db.close()
        return {"instance": args.id, "completed": True}

    # The original CLI opens the instance lock before its first get(), so the
    # guard is evaluated on the actual record the command is about to use.
    store = GuardedStore(args.id, args.expect)
    try:
        argv = ["instance", "status" if action == "refresh" else action, args.id]
        if getattr(args, "confirm", False):
            argv.append("--confirm")
        if action == "refresh":
            with probes.observed_probe() as observation:
                store.observation = observation
                value = delegate.run(store, delegate.parse(argv))
            return {"instance": args.id, "completed": store.probe_committed,
                    "probe_failed": bool(value.get("probe_error")), "quota_observed": store.probe_quota_observed}
        value = delegate.run(store, delegate.parse(argv))
        if action == "delete":
            return {key: value[key] for key in ("instance", "tasks", "attempts", "requires_logout", "deleted")}
        return {"instance": args.id, "completed": True}
    finally:
        store.db.close()


def legacy(argv):
    # Keep all reconnects and spawned supervisors on the same non-migrating store.
    args = delegate.parse(argv)
    if args.command == "task" and args.command_1 == "decide":
        raise DelegateError("请用创建任务的规划会话处理决策")
    state_module.Store = EngineStore
    controller.ENTRY = Path(__file__).resolve()
    store = EngineStore()
    try:
        result = delegate.run(store, args)
        if result is not None:
            delegate.emit(ownership.public(result))
        return 1 if isinstance(result, dict) and result.get("state") == "blocked" else 0
    finally:
        store.db.close()


def planner(argv):
    options, args = ownership.parse_planner(argv, delegate.parse)
    if options.command == "open":
        store = EngineStore()
        try:
            return ownership.open_session(store.root)
        finally:
            store.db.close()

    root = state_root()
    with ownership.session(root, options.receipt, exclusive=options.command == "close") as owner:
        if options.command == "close":
            options.receipt.expanduser().absolute().unlink()
            return {"session": owner["session"], "closed": True}
        store = PlannerStore(owner, args)
        try:
            if args.command_1 != "submit":
                store.get("tasks", args.id)
            if args.command_1 == "decide":
                # The old decision function reads this task inside its own transaction.
                store.check = (args.request, options.request_ref)
            result = delegate.run(store, args)
            if args.command_1 == "submit" and isinstance(result, list):
                for member in result:
                    ownership.require_owner(store.get("tasks", member["id"]), owner)
            if args.command_1 in {"watch", "status"}:
                result = ownership.add_request_refs(store, result)
            return ownership.public(result, planner=True)
        finally:
            store.db.close()


def interrupt(_signal, _frame):
    raise KeyboardInterrupt


def main():
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更高版本", file=sys.stderr)
        return 2
    os.umask(0o077)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    legacy_mode = sys.argv[1:2] in (["legacy"], ["_attempt"])
    terminal_mode = sys.argv[1:2] == ["terminal"]
    try:
        if os.environ.get("CLI_DELEGATE_DEPTH") not in (None, "", "0"):
            raise DelegateError("受管子代理不能再次调用调度器")
        if legacy_mode:
            return legacy(sys.argv[2:] if sys.argv[1] == "legacy" else sys.argv[1:])
        if sys.argv[1:2] == ["planner"]:
            delegate.emit(planner(sys.argv[2:]))
        else:
            args = parse()
            if args.operation == "terminal":
                return run_terminal(args)
            delegate.emit(dispatch(args))
    except DelegateError as error:
        if legacy_mode or terminal_mode:
            print(f"cli-manager: {error}", file=sys.stderr)
        else:
            delegate.emit({"error": str(error)})
        return 2
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
        message = "无法读取本地状态或执行 CLI，请检查运行环境"
        if legacy_mode or terminal_mode:
            print(f"cli-manager: {message}", file=sys.stderr)
        else:
            delegate.emit({"error": message})
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
