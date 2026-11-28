#!/usr/bin/env python3
"""Manage independently authenticated Claude Code and Codex workers."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from cli_manager import controller as ctl
from cli_manager.store import DelegateError, Store, identifier


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def path(value):
    return Path(value).expanduser().resolve()


def absolute(value):
    if not os.path.isabs(os.path.expanduser(value)):
        raise argparse.ArgumentTypeError("需要绝对路径")
    return Path(value).expanduser()


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def task_status(store, name):
    task = store.get("tasks", name)
    folder = store.task_dir(name)
    attempts = [a for a in store.rows("attempts") if a["task"] == name]
    for attempt in attempts:
        for private in ("account", "reservation", "identity"):
            attempt.pop(private, None)
    pending, _ = ctl.get_pending(store, task)
    report = task.get("review_candidate") or task.get("report")
    task.update(attempts=attempts, pending=pending,
                report_path=str(folder / "reports" / f"{report}.json") if report else None,
                patch_path=str(folder / "checkpoints" / f"{report}.patch") if report else None,
                summary_path=str(folder / "reports" / f"{report}.md") if report else None)
    return task


def instance_status(store, name=None):
    rows = store.rows("instances") if not name else [store.get("instances", name)]
    attempts = store.rows("attempts")
    for row in rows:
        if row["provider"] == "codex" and row.get("bound_account") and not row.get("bound_scoped") and row.get("status"):
            row["status"]["rebind_required"] = True  # also for statuses cached before the upgrade
        row.pop("bound_account", None)
        if row.get("status"):
            row["status"].pop("account", None)
        row["running"] = [a["task"] for a in attempts if a["instance"] == row["id"] and a["state"] in ctl.ACTIVE]
    return rows


def submit(store, a):
    spec = {"id": a.id, "kind": a.kind, "project": a.project, "instances": a.instance, "backend": a.backend,
            "dependencies": list(dict.fromkeys(a.depends_on)), "include": a.include, "verify": a.verify,
            "verify_timeout": a.verify_timeout, "timeout": a.timeout, "approval_timeout": a.approval_timeout,
            "model": a.model, "effort": a.effort, "handoff_targets": a.handoff_to, "parallel": a.parallel,
            # Unset options resolve to the open defaults; only `--access sandbox` closes the task.
            "capabilities": {"access": a.access, "network": a.network, "allow_domains": a.allow_domain,
                             "read_dirs": [str(item) for item in a.read_dir],
                             "write_dirs": [str(item) for item in a.write_dir],
                             "image": {"on": True, "off": False, None: None}[a.image],
                             "confirm_full": a.confirm_full}}
    if a.batch_dir:
        if a.kind != "image":
            raise DelegateError("提示词目录批量提交仅用于 image")
        prompts = sorted(a.batch_dir.glob("*.txt"))
        if not prompts:
            raise DelegateError("批量目录中没有 .txt 提示词")
        tasks = ctl.submit_batch(store, spec, prompts, a.reference)
        return [{"id": task["id"], "state": task["state"], "source": task["batch"]["source"]}
                for task in tasks]
    note = a.brief.read_text(encoding="utf-8") if a.brief else a.prompt or ""
    return ctl.submit(store, spec, note, a.reference)


def watch(store, a):
    deadline = time.monotonic() + a.wait
    cursor = a.since
    while True:
        task = task_status(store, a.id)
        events = store.events(a.id, cursor)
        if task["pending"] or events or task["state"] not in ctl.ACTIVE | {"queued", "waiting_quota"}:
            return {"task": task, "events": events, "since": events[-1]["seq"] if events else cursor}
        if time.monotonic() >= deadline:
            return {"task": task, "events": [], "since": cursor}
        time.sleep(.25)


def image_sheet(store, a):
    images = []
    for name in a.ids:
        task = store.get("tasks", name)
        if task["kind"] != "image" or task["state"] not in {"approved", "applied"}:
            raise DelegateError(f"图像任务尚未批准：{name}")
        report = ctl.review_report(store, task, task["approved"])
        asset, = report["artifacts"]
        location = Path(asset["path"])
        if ctl.artifact(location) != asset:
            raise DelegateError(f"产物摘要不匹配：{name}")
        images.append(str(location))
    if a.output.exists() or a.output.is_symlink():
        raise DelegateError("已有输出文件")
    script = Path(__file__).resolve().parent.parent / "backends/codex/image/contact_sheet.py"
    import tempfile
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=a.output.parent) as tmp:
        staged = Path(tmp) / a.output.name
        subprocess.run([sys.executable, str(script), str(staged), *images, "--cols", str(a.cols)],
                       check=True, stdout=subprocess.DEVNULL)
        os.link(staged, a.output, follow_symlinks=False)
    return {"path": str(a.output)}


def commands(parser, depth=0):
    dest = "command" if depth == 0 else f"command_{depth}"
    return parser.add_subparsers(dest=dest, required=True)


def add_id(parser, name):
    sub = parser.add_parser(name)
    sub.add_argument("id", type=identifier)
    return sub


def parse(argv=None):
    parser = argparse.ArgumentParser(prog="cli-manager", description=__doc__, allow_abbrev=False)
    root = commands(parser)
    instances = commands(root.add_parser("instance"), 1)
    create = add_id(instances, "create")
    create.add_argument("--backend", dest="provider", required=True, choices=("claude", "codex"))
    create.add_argument("--bin", type=path)
    create.add_argument("--parallel", type=positive, default=2)
    instances.add_parser("list")
    add_id(instances, "status")
    for operation in ("login", "logout", "switch"):
        add_id(instances, operation)
    add_id(instances, "rebind")
    add_id(instances, "delete").add_argument("--confirm", action="store_true")

    quotas = root.add_parser("quota")
    quotas.add_argument("id", nargs="?", type=identifier)
    ledger = root.add_parser("ledger")
    ledger.add_argument("id", nargs="?", type=identifier)
    queue = commands(root.add_parser("queue"), 1)
    work = queue.add_parser("work")
    work.add_argument("--parallel", type=positive, default=4)
    work.add_argument("--once", action="store_true", help="扫描并派发一轮后退出")
    tasks = commands(root.add_parser("task"), 1)
    add = tasks.add_parser("submit")
    add.add_argument("--id", type=identifier)
    add.add_argument("--project", type=path)
    add.add_argument("--kind", choices=("code", "review", "image"), default="code")
    add.add_argument("--instance", type=identifier, action="append", default=[])
    add.add_argument("--backend", choices=("auto", "claude", "codex"))
    add.add_argument("--handoff-to", type=identifier, action="append", default=[])
    add.add_argument("--depends-on", type=identifier, action="append", default=[])
    add.add_argument("--include", action="append", default=[])
    add.add_argument("--reference", type=path, action="append", default=[])
    prompts = add.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--brief", type=path)
    prompts.add_argument("--prompt")
    prompts.add_argument("--batch-dir", type=path)
    add.add_argument("--model")
    add.add_argument("--effort")
    add.add_argument("--parallel", type=positive)
    add.add_argument("--timeout", type=positive, default=3600)
    add.add_argument("--approval-timeout", type=positive, default=1800)
    add.add_argument("--access", choices=("open", "sandbox", "full"),
                     help="open（默认）：可联网、可读授权目录，越界请求送主会话审批；"
                          "sandbox：封闭沙箱；full：越界不逐条审批，需 --confirm-full 并在终端确认")
    add.add_argument("--network", choices=("on", "loopback", "off"), help="默认 on；sandbox 档强制 off")
    add.add_argument("--allow-domain", action="append", default=[], help="把外网收窄为域名白名单，可重复")
    add.add_argument("--read-dir", type=absolute, action="append", default=[], help="追加只读目录（绝对路径），可重复")
    add.add_argument("--write-dir", type=absolute, action="append", default=[], help="追加可写目录（绝对路径），可重复")
    add.add_argument("--image", choices=("on", "off"), help="Codex 自带生图，默认 on；sandbox 档关闭")
    add.add_argument("--confirm-full", action="store_true")
    add.add_argument("--verify")
    add.add_argument("--verify-timeout", type=positive, default=900)
    tasks.add_parser("list")
    add_id(tasks, "status")
    w = add_id(tasks, "watch")
    w.add_argument("--wait", type=positive, default=10)
    w.add_argument("--since", type=int, default=0)
    d = add_id(tasks, "decide")
    d.add_argument("request", type=identifier)
    d.add_argument("action", choices=("allow", "deny", "answer"))
    d.add_argument("--answers", type=json.loads, default={})
    d.add_argument("--message")
    d.add_argument("--always", action="store_true",
                   help="allow 时本会话同类请求放行（acceptForSession）；不可逆操作不适用")
    add_id(tasks, "cancel")
    for operation in ("retry", "resume"):
        add_id(tasks, operation).add_argument("--note")

    handoff = commands(tasks.add_parser("handoff"), 2)
    add_id(handoff, "prepare")
    hs = add_id(handoff, "start")
    hs.add_argument("--source", required=True, type=identifier)
    hs.add_argument("--to", required=True, type=identifier)
    hs.add_argument("--brief", required=True, type=path)
    hs.add_argument("--authorize-target", action="store_true")
    hs.add_argument("--authorize-access", action="store_true",
                    help="目标换了账户时，重新授权任务的能力档位")
    hs.add_argument("--confirm-full", action="store_true")
    hs.add_argument("--model")
    hs.add_argument("--effort")

    reviews = commands(tasks.add_parser("review"), 2)
    for operation in ("approve", "request-changes"):
        review = add_id(reviews, operation)
        review.add_argument("--revision", required=True)
        review.add_argument("--note")

    apply = add_id(tasks, "apply")
    apply.add_argument("--revision")
    modes = apply.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "recover", "integrate", "record-integration"):
        modes.add_argument("--" + mode, dest="mode", action="store_const", const=mode)
    add_id(tasks, "cleanup").add_argument("--confirm", action="store_true")
    export = add_id(tasks, "export")
    export.add_argument("--revision", required=True)
    export.add_argument("--output", type=Path, required=True)

    sheet = root.add_parser("image-sheet")
    sheet.add_argument("ids", type=identifier, nargs="+")
    sheet.add_argument("--output", type=Path, required=True)
    sheet.add_argument("--cols", type=positive, default=2)
    add_id(root, "_attempt").add_argument("reservation")
    return parser.parse_args(argv)


def run(store, a):
    if a.command == "_attempt":
        ctl.execute(store, a.id, a.reservation)
        return None
    group, action = (a.command, getattr(a, "command_1", None))
    if group == "instance":
        if action == "create":
            ctl.create_instance(store, a.id, a.provider, str(a.bin) if a.bin else None, a.parallel)
            return instance_status(store, a.id)[0]
        if action == "list":
            return instance_status(store)
        if action == "status":
            with ctl.instance_lock(store, a.id):
                ctl.refresh_instance(store, store.get("instances", a.id), force=True)
            return instance_status(store, a.id)[0]
        if action == "delete":
            return ctl.delete_instance(store, a.id, a.confirm)
        if action == "rebind":
            ctl.rebind_instance(store, a.id)
            return instance_status(store, a.id)[0]
        ctl.auth_instance(store, a.id, action)
        return instance_status(store, a.id)[0]
    if group == "quota":
        instances = store.rows("instances") if not a.id else [store.get("instances", a.id)]
        for row in instances:
            with ctl.instance_lock(store, row["id"]):
                ctl.refresh_instance(store, row, force=True)
        return [{"id": row["id"], "provider": row["provider"], **row.get("status", {}).get("quota", {})}
                for row in instance_status(store, a.id)]
    if group == "ledger":
        keys = ("id", "task", "instance", "state", "model", "start", "end", "usage", "cost_usd",
                "quota_before", "quota_after", "report", "capabilities", "artifacts")
        return [{key: attempt.get(key) for key in keys} for attempt in store.rows("attempts")
                if not a.id or a.id == attempt["task"]]
    if group == "queue":
        ctl.dispatch(store, parallel=a.parallel, once=a.once)
        return None
    if group == "image-sheet":
        return image_sheet(store, a)
    if action == "submit":
        return submit(store, a)
    if action == "list":
        return store.rows("tasks")
    if action == "status":
        return task_status(store, a.id)
    if action == "watch":
        return watch(store, a)
    if action == "decide":
        if not isinstance(a.answers, dict):
            raise DelegateError("--answers 要求 JSON 对象")
        return ctl.decide(store, a.id, a.request, a.action, a.answers, a.message, a.always)
    if action == "cancel":
        ctl.stop_task(store, a.id)
        return task_status(store, a.id)
    if action in {"retry", "resume"}:
        return ctl.requeue(store, a.id, native=action == "resume", note=a.note)
    if action == "handoff":
        if a.command_2 == "prepare":
            return ctl.prepare_handoff(store, a.id)
        return ctl.start_handoff(store, a.id, a.to, a.brief.read_text(), a.authorize_target, a.source,
                                 a.model, a.effort, authorize_access=a.authorize_access,
                                 confirm_full=a.confirm_full)
    if action == "review":
        return ctl.review(store, a.id, a.command_2, a.revision, a.note)
    if action == "apply":
        if a.mode != "recover" and not a.revision:
            raise DelegateError("--revision 需要提供已审核版本")
        if a.mode in {"integrate", "record-integration"}:
            return ctl.integrate(store, a.id, a.revision, record=a.mode == "record-integration")
        return ctl.apply_task(store, a.id, a.revision, a.mode)
    if action == "cleanup":
        return ctl.cleanup(store, a.id, a.confirm)
    if action == "export":
        return ctl.export_image(store, a.id, a.revision, a.output)
    raise DelegateError("未知命令")


def main(argv=None):
    if sys.version_info < (3, 11):
        print("cli-manager 需要 Python 3.11 或更高版本", file=sys.stderr)
        return 2
    args = parse(argv)
    os.umask(0o077)
    try:
        if os.environ.get("CLI_DELEGATE_DEPTH") not in (None, "", "0"):
            raise DelegateError("受管子代理不能再次调用调度器")
        store = Store()
        try:
            result = run(store, args)
            if result is not None:
                emit(result)
            return 1 if isinstance(result, dict) and result.get("state") == "blocked" else 0
        finally:
            store.db.close()
    except (DelegateError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"cli-manager: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
