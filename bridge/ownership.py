"""Local planner provenance. The engine remains the authority for pending requests and decisions."""
from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from cli_manager import controller
from cli_manager.store import DelegateError, digest

RECORD = "decision_owner"
SESSION = re.compile(r"planner-[0-9a-f]{32}")
SECRET = re.compile(r"[0-9a-f]{64}")
WRONG_OWNER = "此任务的决策归属未核实，或不属于当前规划会话"


def sessions_dir(root, *, create=False):
    folder = root / "decision-sessions"
    if create:
        folder.mkdir(mode=0o700, exist_ok=True)
    try:
        info = folder.lstat()
    except FileNotFoundError as exc:
        raise DelegateError("找不到本地规划会话") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DelegateError("规划会话目录须由当前用户拥有并设为 0700")
    return folder


def open_session(root):
    folder = sessions_dir(root, create=True)
    session = f"planner-{secrets.token_hex(16)}"
    path = folder / session
    secret = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"session": session, "receipt": str(path)}


@contextmanager
def session(root, receipt, *, exclusive=False):
    folder = sessions_dir(root)
    path = receipt.expanduser().absolute()
    if path.parent != folder or not SESSION.fullmatch(path.name):
        raise DelegateError("规划会话凭证不属于此状态目录")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise DelegateError("规划会话凭证失效或不可读取") from exc
    with os.fdopen(fd, "rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        info = os.fstat(stream.fileno())
        try:
            linked = os.path.samestat(info, path.lstat())
        except FileNotFoundError:
            linked = False
        if (not linked or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise DelegateError("规划会话凭证失效或权限不符")
        secret = stream.read(65).decode("ascii", errors="replace")
        if not SECRET.fullmatch(secret):
            raise DelegateError("规划会话凭证无效")
        yield {"source": "planner", "session": path.name, "seal": digest(secret)}


def assigned_owner(task):
    record = task.get(RECORD)
    if (isinstance(record, dict) and record.get("source") == "planner"
            and SESSION.fullmatch(record.get("session") if isinstance(record.get("session"), str) else "")
            and SECRET.fullmatch(record.get("seal") if isinstance(record.get("seal"), str) else "")):
        return record
    return None


def require_owner(task, owner):
    assigned = assigned_owner(task or {})
    if (assigned is None or assigned["session"] != owner["session"]
            or not hmac.compare_digest(assigned["seal"], owner["seal"])):
        raise DelegateError(WRONG_OWNER)


def request_ref(task, attempt, request_id):
    # Claude reuses short request IDs in a new run, including when an attempt resumes.
    if not (attempt.get("reservation") and attempt.get("logs")):
        raise DelegateError("此请求缺少可核实的执行记录")
    return digest([task["id"], attempt["id"], attempt["reservation"], attempt["logs"], request_id])


def checked_request(store, task, request_id, expected):
    pending, _ = controller.get_pending(store, task)
    if not any(item["id"] == request_id for item in pending):
        raise DelegateError("请求已过期、已答复或属于上一次执行")
    attempt = store.get("attempts", task["attempt"])
    if not hmac.compare_digest(request_ref(task, attempt, request_id), expected):
        raise DelegateError("请求属于其他执行，请重新读取当前请求")


def add_request_refs(store, response):
    task = response.get("task", response)
    for item in task.get("pending", []):
        attempt = next((a for a in task.get("attempts", []) if a["id"] == task.get("attempt")), None)
        # Legacy task status deliberately redacts the reservation from its output.
        if attempt is not None:
            current = store.get("attempts", attempt["id"])
            if current.get("logs") == attempt.get("logs") and current.get("logs") and current.get("reservation"):
                item["request_ref"] = request_ref(task, current, item["id"])
    return response


def public(value, *, planner=False):
    if isinstance(value, list):
        return [public(row, planner=planner) for row in value]
    if not isinstance(value, dict):
        return value
    result = {key: part for key, part in value.items() if key != RECORD}
    if "task" in result and isinstance(result["task"], dict):
        result["task"] = public(result["task"], planner=planner)
    if not planner:
        if "pending" in result:
            result["pending"] = []
        if "events" in result:
            result["events"] = [event for event in result["events"]
                                if not event.get("type", "").startswith("approval_")]
    return result


def inspect(store):
    """Allowlisted passive projection, never a source of decisions or a notification feed."""
    entries = []
    for task in store.rows("tasks"):
        if task["state"] not in controller.ACTIVE:
            continue
        pending, _ = controller.get_pending(store, task)
        if not pending:
            continue
        questions = sum(request.get("kind") == "question" for request in pending)
        entries.append({"id": task["id"], "route": "planner" if assigned_owner(task) else "unknown",
                        "questions": questions, "permissions": len(pending) - questions})
    return {"schema": 1, "entries": entries}


def parse_planner(argv, legacy_parser):
    parser = argparse.ArgumentParser(prog="cli-manager planner", allow_abbrev=False)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--request-ref")
    parser.add_argument("command", choices=("open", "close", "task"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if options.command == "open":
        if options.receipt or options.request_ref or options.arguments:
            parser.error("open 无需参数")
        return options, None
    if not options.receipt:
        parser.error("请提供创建任务时使用的 --receipt")
    if options.command == "close":
        if options.arguments or options.request_ref:
            parser.error("close 仅接受 --receipt")
        return options, None
    args = legacy_parser(["task", *options.arguments])
    if args.command_1 not in {"submit", "watch", "status", "decide", "retry", "resume"}:
        parser.error("规划会话支持 submit、watch、status、decide、retry 和 resume")
    if args.command_1 == "decide":
        if not options.request_ref or not SECRET.fullmatch(options.request_ref):
            parser.error("decide 需要 watch 返回的 --request-ref")
    elif options.request_ref:
        parser.error("--request-ref 仅用于 decide")
    if args.command_1 == "watch" and args.wait > 30:
        parser.error("单次 planner watch 最长等待 30 秒")
    return options, args
