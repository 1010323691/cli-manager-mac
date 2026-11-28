"""Private state and transactional task/attempt records."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path


class DelegateError(RuntimeError):
    pass


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise DelegateError(f"无效标识符：{value!r}")
    return value


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def write_json(path: Path, data) -> None:
    write(path, (encode(data) + "\n").encode())


def identity(pid: int) -> str | None:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
        capture_output=True, text=True, timeout=5,
    )
    fields = result.stdout.strip().rsplit(maxsplit=1)
    if result.returncode or len(fields) != 2 or fields[1].startswith("Z"):
        return None
    return fields[0]


@contextlib.contextmanager
def lock(path: Path, *, blocking: bool = True):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise DelegateError("已有操作持有此锁") from error
        yield


class Store:
    def __init__(self, root: Path | None = None):
        configured = os.environ.get("CLI_MANAGER_STATE") or os.environ.get("CLI_DELEGATE_V2_STATE")
        self.root = (root or Path(configured or "~/.cli-delegate-v2")).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = self.root.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise DelegateError(f"状态目录须由当前用户拥有并设为 0700：{self.root}")
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS instances(
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                instance TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_by_state ON attempts(state);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, task TEXT, time REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
            );
        """)

        if "bucket" not in self._quota_columns():
            self._migrate_quota()

    def _quota_columns(self):
        return {row["name"] for row in self.db.execute("PRAGMA table_info(account_quota)")}

    def _migrate_quota(self):
        # Only a missing or legacy table takes the write lock; every other open stays read-only.
        with self.transaction():
            columns = self._quota_columns()
            if "bucket" in columns:
                return
            legacy = bool(columns)
            if legacy:
                self.db.execute("ALTER TABLE account_quota RENAME TO account_quota_old")
            self.db.execute("""CREATE TABLE IF NOT EXISTS account_quota(
                account TEXT NOT NULL, kind TEXT NOT NULL, bucket TEXT NOT NULL, window TEXT NOT NULL,
                measured REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY(account, kind, bucket, window))""")
            if legacy:
                for account, kind, stamp, data in self.db.execute("SELECT account, kind, measured, data FROM account_quota_old"):
                    if kind == "windows":
                        for window in json.loads(data):
                            self._put_quota_signal(account, "window", stamp, window)
                    elif kind == "blocked":
                        self._put_quota_signal(account, kind, stamp, json.loads(data))
                self.db.execute("DROP TABLE account_quota_old")
            elif not columns:
                for instance in self.rows("instances"):
                    sample = (instance.get("status") or {}).get("quota") or {}
                    self.record_quota(instance, sample.get("last_observation") or {})
                    self.record_quota(instance, sample)

    def record_quota(self, instance, sample):
        status = instance.get("status") or {}
        account = instance.get("bound_account")
        if (not account or status.get("account") != account or not status.get("logged_in")
                or instance.get("auth_busy") or instance.get("deleting")
                or (instance.get("provider") == "codex" and not instance.get("bound_scoped"))):
            return
        windows = sample.get("windows")
        measured = sample.get("observed_at") or sample.get("checked_at")
        if windows and sample.get("quality") != "unknown":
            for window in windows:
                stamp = window.get("observed_at", measured)
                if stamp is not None:
                    self._put_quota_signal(account, "window", stamp, window)
        blocked = sample.get("explicitly_blocked")
        checked = sample.get("blocked_checked_at", sample.get("checked_at") or measured)
        if isinstance(blocked, bool) and checked is not None:
            self._put_quota_signal(account, "blocked", checked, blocked)

    def _put_quota_signal(self, account, kind, stamp, value):
        bucket = value.get("bucket", "") if kind == "window" else ""
        window = value.get("window", "") if kind == "window" else ""
        self.db.execute("""INSERT INTO account_quota(account, kind, bucket, window, measured, data)
            VALUES(?,?,?,?,?,?) ON CONFLICT(account, kind, bucket, window) DO UPDATE
            SET measured=excluded.measured, data=excluded.data WHERE excluded.measured > account_quota.measured""",
                        (account, kind, bucket, window, stamp, encode(value)))

    def quota_signals(self, account):
        return [(kind, stamp, json.loads(data)) for kind, stamp, data in self.db.execute(
            "SELECT kind, measured, data FROM account_quota WHERE account=?", (account,))]

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def get(self, table: str, key: str, *, required: bool = True):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier(key),)).fetchone()
        if row is None:
            if required:
                raise DelegateError(f"找不到记录：{key}")
            return None
        return self.unpack(row)

    @staticmethod
    def unpack(row):
        data = json.loads(row["data"])
        return {**data, **{key: row[key] for key in row.keys() if key != "data"}}

    def rows(self, table: str):
        return [self.unpack(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def put(self, table: str, record: dict):
        fields = {"instances": ("id",), "tasks": ("id", "state"),
                  "attempts": ("id", "task", "instance", "state")}[table]
        cols = ", ".join((*fields, "data"))
        values = [record[k] for k in fields] + [encode({k: v for k, v in record.items() if k not in fields})]
        marks = ", ".join("?" for _ in values)
        updates = ", ".join(f"{key}=excluded.{key}" for key in (*fields[1:], "data"))
        self.db.execute(f"INSERT INTO {table}({cols}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", values)

    def event(self, task, kind: str, **data):
        self.db.execute("INSERT INTO events(task,time,type,data) VALUES(?,?,?,?)",
                        (task, time.time(), kind, encode(data)))

    def events(self, task: str, since: int):
        return [self.unpack(row) for row in self.db.execute(
            "SELECT * FROM events WHERE task=? AND seq>? ORDER BY seq", (task, since)
        )]

    def instance_dir(self, name: str) -> Path:
        directory = self.root / "instances" / identifier(name)
        if directory.parent.is_symlink() or directory.is_symlink():
            raise DelegateError("实例目录不能是符号链接")
        if directory.exists() and (not directory.is_dir() or name not in os.listdir(directory.parent)):
            raise DelegateError("实例目录与注册名称不一致")
        return directory

    def task_dir(self, name: str) -> Path:
        return self.root / "tasks" / identifier(name)

    def attempt_dir(self, attempt: dict) -> Path:
        return self.instance_dir(attempt["instance"]) / "attempts" / identifier(attempt["id"])
