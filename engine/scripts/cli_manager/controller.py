"""Task lifecycle, placement, recovery, and review."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from . import access, backend, workspace
from .store import DelegateError, digest, identifier, identity, lock, write, write_json

ACTIVE = {"starting", "running", "stopping"}
FINISHED = {"applied", "cancelled", "discarded"}
PROBE_TTL = 120
ENTRY = Path(__file__).resolve().parents[1] / "delegate.py"


def new_id(prefix):
    return f"{prefix}-{secrets.token_hex(7)}"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def instance_lock(store, name):
    return lock(store.root / "locks" / f"instance-{identifier(name).casefold()}", blocking=False)


def create_instance(store, name, provider, executable, parallel):
    with instance_lock(store, name), store.transaction():
        if any(row["id"].casefold() == name.casefold() for row in store.rows("instances")):
            raise DelegateError("实例已存在")
        instance = {"id": name, "provider": provider, "binary": executable, "parallel": parallel,
                    "generation": 0, "bound_account": None}
        backend.binary(instance)
        backend.environment(store, instance)
        store.put("instances", instance)
        store.event(None, "instance_created", instance=name, provider=provider)
    return instance


def refresh_instance(store, instance, *, force=False, query_quota=True):
    current_schema = (instance.get("status") or {}).get("schema") == backend.PROBE_SCHEMA
    if not force and current_schema and time.time() - instance.get("probed_at", 0) < PROBE_TTL:
        return instance
    # An invalidation during this probe (e.g. an account mismatch found by a supervisor) makes
    # its result stale: compare the counter before writing back.
    seen = store.get("instances", instance["id"]).get("invalidations", 0)
    try:
        observed = backend.probe(store, instance, query_quota=query_quota)
        error = None
    except (DelegateError, OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        observed = {"schema": backend.PROBE_SCHEMA, "logged_in": False, "account": None,
                    "quota": backend.quota([], "probe_failed"),
                    "models": [], "image": False}
        error = f"{type(exc).__name__}: CLI 查询失败"
    with store.transaction():
        current = store.get("instances", instance["id"])
        if (current["generation"] != instance["generation"] or current.get("auth_busy")
                or current.get("deleting") or current.get("invalidations", 0) != seen):
            return current
        legacy = observed.pop("legacy_account", None)
        previous = current.get("status") or {}
        if observed.get("logged_in") and not legacy and current.get("bound_scoped"):
            # A reply without workspaceRouting cannot tell personal from Business workspaces;
            # never fall back to the email-only key once bound to a workspace-scoped one.
            observed.update(logged_in=False, account=None, quota=backend.quota([], "probe_failed"))
            error = "ValueError: CLI 未返回工作区，无法区分账户"
        if current["provider"] == "codex" and current.get("bound_account") and not current.get("bound_scoped"):
            # Bound by email only (before the workspace joined the key, or while same-email work
            # was still running): never schedulable until `instance rebind`, whatever the reply.
            observed["rebind_required"] = True
        if observed.get("account") and observed["account"] == previous.get("account"):
            cached = previous.get("quota") or {}
            last = cached if cached.get("quality") == "observed" else cached.get("last_observation")
            if observed["quota"]["quality"] == "unknown" and last:
                observed["quota"]["last_observation"] = last
        if observed.get("account") != current.get("bound_account") and not observed.get("rebind_required"):
            observed["identity_changed"] = True
        current.update(status=observed, probed_at=time.time(), probe_error=error)
        store.record_quota(current, observed["quota"])
        store.put("instances", current)
    return current


invalidate_probe = backend.invalidate_probe


def auth_instance(store, name, action):
    if not sys.stdin.isatty():
        raise DelegateError("请在终端交互执行账户操作")
    with instance_lock(store, name):
        with store.transaction():
            instance = store.get("instances", name)
            if any(a["instance"] == name and a["state"] in ACTIVE for a in store.rows("attempts")):
                raise DelegateError("实例仍有执行中的任务")
            instance.update(auth_busy=True, generation=instance["generation"] + 1,
                            bound_account=None, status={}, probed_at=0)
            store.put("instances", instance)
        operation = "logout" if action == "switch" else action
        try:
            if backend.auth_command(store, instance, operation):
                raise DelegateError(f"原生 CLI {operation} 失败")
            if action == "switch" and backend.auth_command(store, instance, "login"):
                raise DelegateError("原生 CLI 登录失败")
            observed = backend.probe(store, instance, query_quota=False)
            legacy = observed.pop("legacy_account", None)
            if observed["logged_in"] != (action != "logout"):
                raise DelegateError("原生 CLI 状态与账户操作结果不符")
            if observed["logged_in"] and not observed.get("account"):
                raise DelegateError("CLI 未返回可安全去重的账户身份")
            if observed["logged_in"] and instance["provider"] == "codex" and not legacy:
                raise DelegateError("CLI 未返回工作区，无法区分账户，未写入绑定")
            bound, scoped = observed["account"], bool(legacy)
            if legacy and any(a["state"] in ACTIVE and a.get("account") == legacy for a in store.rows("attempts")):
                # Same-email work under the email-only key is still running; its load would not count
                # against the workspace key, so bind by email only and require `instance rebind` later.
                bound, scoped = legacy, False
            if bound and not scoped and instance["provider"] == "codex":
                observed["rebind_required"] = True
            instance.update(status=observed, bound_account=bound, bound_scoped=scoped, probed_at=0)
        finally:
            with store.transaction():
                instance["auth_busy"] = False
                store.put("instances", instance)
                store.event(None, "instance_auth", instance=name, operation=action,
                            generation=instance["generation"], logged_in=bool(instance["status"].get("logged_in")))
    return instance


def rebind_instance(store, name):
    """Bind an email-only Codex instance to its current workspace as a new login generation.

    Bindings made before account keys included the workspace cannot name the workspace their
    tasks, sessions and cooldowns ran in, so nothing of the old generation carries over: queued
    tasks bound to it, native resume and limits recorded under the old key stop applying.
    """
    with instance_lock(store, name):
        instance = store.get("instances", name)
        if any(a["instance"] == name and a["state"] in ACTIVE for a in store.rows("attempts")):
            raise DelegateError("实例仍有执行中的任务")
        try:
            observed = backend.probe(store, instance)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            raise DelegateError(f"{type(exc).__name__}: CLI 查询失败") from exc
        legacy = observed.pop("legacy_account", None)
        if not observed.get("logged_in") or not observed.get("account") or not instance.get("bound_account"):
            raise DelegateError("实例未登录或未绑定账户，请改用 instance login")
        if not legacy:
            raise DelegateError("CLI 未返回工作区，无法重绑")
        if instance.get("bound_account") != legacy:
            raise DelegateError("实例不是仅按邮箱绑定，无需重绑")
        with store.transaction():
            current = store.get("instances", name)
            if current["generation"] != instance["generation"] or current.get("auth_busy"):
                raise DelegateError("实例状态已变化，请重试")
            if any(a["state"] in ACTIVE and (a["instance"] == name or a.get("account") == legacy)
                   for a in store.rows("attempts")):
                # Load under the email-only key would not count against the new key.
                raise DelegateError("实例或同邮箱的其他实例仍有执行中的任务")
            current.update(generation=current["generation"] + 1, bound_account=observed["account"],
                           bound_scoped=True, status=observed, probed_at=time.time(), probe_error=None)
            store.put("instances", current)
            store.record_quota(current, observed["quota"])
            store.event(None, "instance_rebound", instance=name, generation=current["generation"])
    return current


def delete_instance(store, name, confirmed):
    with instance_lock(store, name):
        instance = store.get("instances", name)
        with store.transaction():
            states = {task["id"]: task["state"] for task in store.rows("tasks")}
            attempts = [a["id"] for a in store.rows("attempts") if a["instance"] == name
                        and (a["state"] in ACTIVE or states.get(a["task"]) not in FINISHED)]
            waiting = [task["id"] for task in store.rows("tasks") if task["state"] not in FINISHED and
                       (name in task["allowed"] or name in task["handoff_targets"] or task.get("target") == name)]
            result = {"instance": instance["id"], "path": str(store.instance_dir(name)), "attempts": attempts,
                      "tasks": waiting, "deleted": False, "requires_logout": bool(instance["bound_account"])}
            if not confirmed:
                return result
            if attempts or waiting or instance["bound_account"]:
                raise DelegateError("实例仍被任务引用或仍绑定账户，请先完成任务并原生退出")
            instance["deleting"] = True
            store.put("instances", instance)
        directory = store.instance_dir(name)
        if directory.exists():
            shutil.rmtree(directory)
        with store.transaction():
            store.db.execute("DELETE FROM instances WHERE id=?", (name,))
            store.event(None, "instance_deleted", instance=name)
        result["deleted"] = True
        return result


def stage_references(paths, destination):
    images = {}
    limit = 64 * 1024 * 1024
    for index, path in enumerate(paths, 1):
        if not path.is_file() or path.stat().st_size > limit:
            raise DelegateError(f"参考图不可读或超过 64 MiB：{path}")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise DelegateError(f"参考图超过 64 MiB：{path}")
        suffix = backend.image_format_bytes(data)
        if not suffix:
            raise DelegateError(f"不支持的参考图格式：{path}")
        name = f"{index}{suffix}"
        write(destination / name, data)
        images[name] = hashlib.sha256(data).hexdigest()
    return images


def submission_routes(store, spec, selected, provider):
    known = {i["id"]: i for i in store.rows("instances") if not i.get("deleting")}
    if not selected and not provider and spec["kind"] != "image":
        raise DelegateError("请用 --instance 或 --backend 指定授权范围")
    allowed = selected or [i["id"] for i in known.values()
                           if provider in ("auto", i["provider"]) or (not provider and spec["kind"] == "image")]
    if spec["kind"] == "image":
        allowed = [name for name in allowed if known.get(name, {}).get("provider") == "codex"]
        if spec["dependencies"] or spec["include"] or spec["verify"]:
            raise DelegateError("生图任务使用独立目录，不能指定项目依赖、快照路径或验证命令")
    if not allowed or any(name not in known for name in [*allowed, *spec["handoff_targets"]]):
        raise DelegateError("授权列表没有可用的已注册实例")
    if provider not in (None, "auto") and any(known[name]["provider"] != provider for name in allowed):
        raise DelegateError("实例与指定后端不一致")
    return list(dict.fromkeys(allowed))


def record_submission(store, name, spec, baseline, allowed, images, *, batch=None):
    bindings = {}
    for instance_id in {*allowed, *spec["handoff_targets"]}:
        current = store.get("instances", instance_id)
        if current.get("deleting"):
            raise DelegateError("授权的实例正在删除")
        if current.get("bound_account"):
            bindings[instance_id] = current["generation"]
    for dep in spec["dependencies"]:
        other = store.get("tasks", dep)
        if other["baseline"]["root"] != baseline["root"] or other["kind"] == "image":
            raise DelegateError("依赖任务必须在同一项目中")
    task = {**spec, "id": name, "state": "queued", "baseline": baseline, "references": list(images),
            "allowed": allowed, "bindings": bindings, "created": time.time(), "attempt": None}
    if batch:
        task["batch"] = batch
    store.put("tasks", task)
    store.event(name, "submitted", allowed=allowed, dependencies=task["dependencies"],
                capabilities=access.summary(access.of(task)))
    return task


def resolve_capabilities(store, spec, allowed, *, interactive=None):
    """Effective capability tier for a submission, checked against the instances it may run on."""
    requested = spec.pop("capabilities", None)
    capabilities = access.normalize(store, requested, kind=spec["kind"], interactive=interactive)
    providers = {name: store.get("instances", name) for name in {*allowed, *spec["handoff_targets"]}}
    codex = [row for row in providers.values() if row["provider"] == "codex"]
    explicit = [key for key in capabilities.get("explicit", []) if key != "access" or capabilities["access"] != "sandbox"]
    if not codex and explicit and capabilities["access"] != "sandbox":
        raise DelegateError("开放档能力（" + "、".join(explicit) + "）只适用于 Codex 实例；"
                            "Claude 实例使用封闭沙箱，请去掉这些选项或改派 Codex")
    needs = backend.required_capabilities(capabilities)
    for row in codex:
        reported = (row.get("status") or {}).get("capabilities")
        missing = sorted(set(needs) & set((reported or {}).get("missing", [])))
        if missing:
            raise DelegateError(f"实例 {row['id']} 的 Codex 不支持所需能力：{'、'.join(missing)}；"
                                "请升级 CLI、改用其他实例或显式选择受支持的档位，不会自动降级为封闭沙箱")
    return capabilities


def image_baseline():
    return {"git": False, "root": None, "project_subdir": ".", "commit": None}


def submit(store, spec, prompt, references):
    name = identifier(spec.get("id") or new_id("task"))
    if not prompt.strip():
        raise DelegateError("任务简报为空")
    directory = store.task_dir(name)
    spec = spec.copy()
    project, selected, provider = (spec.pop(key) for key in ("project", "instances", "backend"))
    if references and spec["kind"] != "image":
        raise DelegateError("参考图仅用于生图任务")
    with lock(store.root / "locks" / ("task-" + name), blocking=False), \
            tempfile.TemporaryDirectory(dir=store.root) as temp:
        staged = Path(temp) / "references"
        images = stage_references(references, staged)
        if store.get("tasks", name, required=False) or os.path.lexists(directory):
            raise DelegateError("任务 ID 或目录已存在")
        allowed = submission_routes(store, spec, selected, provider)
        # Before any transaction: `full` waits for a person at the terminal.
        spec["capabilities"] = resolve_capabilities(store, spec, allowed)
        if spec["kind"] != "image":
            if project is None or not project.is_dir():
                raise DelegateError("代码任务需要有效的 --project")
            if store.root.is_relative_to(project) or project.is_relative_to(store.root):
                raise DelegateError("项目目录和调度状态目录不能重叠")
            root = workspace.git(project, "rev-parse", "--show-toplevel", required=False)
            if root and store.root.is_relative_to(Path(os.fsdecode(root.rstrip(b"\n"))).resolve()):
                raise DelegateError("Git 根目录不能包含调度状态目录")
        directory.mkdir(mode=0o700, parents=True)
        try:
            write(directory / "brief.md", prompt.encode())
            baseline = (image_baseline() if spec["kind"] == "image" else
                        workspace.freeze(project, directory, spec["include"]))
            if images:
                os.replace(staged, directory / "references")
            with store.transaction():
                return record_submission(store, name, spec, baseline, allowed, images)
        except BaseException:
            shutil.rmtree(directory)
            raise


def submit_batch(store, spec, prompts, references):
    spec = spec.copy()
    prefix = identifier(spec.pop("id") or new_id("batch"))
    spec.pop("project")
    selected, provider = spec.pop("instances"), spec.pop("backend")
    members = {identifier(f"{prefix}-{index}"): (str(path), path.read_text(encoding="utf-8"))
               for index, path in enumerate(prompts, 1)}
    if not members or any(not prompt.strip() for _, prompt in members.values()):
        raise DelegateError("批次没有简报或包含空简报")

    with tempfile.TemporaryDirectory(dir=store.root) as temporary, ExitStack() as locks:
        staging = Path(temporary)
        images = stage_references(references, staging / "_references")
        # A batch submitted before capability tiers existed stays idempotent when resubmitted as is.
        requested = spec.get("capabilities") or {}
        plain = not any(value not in (None, [], False) for value in requested.values())
        manifest = digest({"members": list(members.items()),
                           "spec": {key: value for key, value in spec.items() if key != "capabilities"} if plain else spec,
                           "instances": selected, "backend": provider, "references": images})
        marker = ".batch-submission.json"
        metadata = {}
        for name, (source, prompt) in members.items():
            metadata[name] = {"id": prefix, "source": source, "manifest": manifest}
            write(staging / name / "brief.md", prompt.encode())
            write_json(staging / name / marker, metadata[name])
            if images:
                shutil.copytree(staging / "_references", staging / name / "references")

        locks.enter_context(lock(store.root / "locks" / "batches" / prefix.casefold(), blocking=False))
        for name in sorted(members):
            locks.enter_context(lock(store.root / "locks" / ("task-" + name), blocking=False))
        published = {}
        try:
            with store.transaction():
                records = store.rows("tasks")
                previous = {task["id"]: task for task in records if (task.get("batch") or {}).get("id") == prefix}
                if previous:
                    if previous.keys() != members.keys() or any(previous[name]["batch"] != metadata[name] for name in members):
                        raise DelegateError("批次已存在且成员或输入不同，请使用新的批次 ID")
                    return [previous[name] for name in members]
                collision = next((task["id"] for task in records if task["id"] in members), None)
                if collision:
                    raise DelegateError(f"批次成员的任务 ID 已存在：{collision}")
                allowed = submission_routes(store, spec, selected, provider)
                spec["capabilities"] = resolve_capabilities(store, spec, allowed)
                destinations = {name: store.task_dir(name) for name in members}
                for name, directory in destinations.items():
                    if os.path.lexists(directory):
                        proof = directory / marker
                        if (directory.is_symlink() or not proof.is_file() or proof.is_symlink()
                                or read(proof) != metadata[name]):
                            raise DelegateError(f"批次成员的任务目录已存在：{name}")
                for name, directory in destinations.items():
                    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if directory.exists():
                        # Only a matching, unregistered payload from a prior interrupted submission.
                        shutil.rmtree(directory)
                    os.rename(staging / name, directory)
                    published[name] = directory
                baseline = image_baseline()
                return [record_submission(store, name, spec, baseline, allowed, images, batch=metadata[name])
                        for name in members]
        except BaseException:
            for name, directory in published.items():
                if store.get("tasks", name, required=False) is None:
                    shutil.rmtree(directory)
            raise


def dependencies(store, task):
    revisions = {}
    path = set()

    def visit(name):
        if name in revisions:
            return
        if name in path:
            raise DelegateError("依赖存在循环")
        item = store.get("tasks", name)
        if item["state"] not in {"approved", "applied"}:
            raise DelegateError(f"上游任务尚未批准：{name}")
        path.add(name)
        for parent in item["dependencies"]:
            visit(parent)
        path.remove(name)
        revisions[name] = item["approved"]
    for name in task["dependencies"]:
        visit(name)
    return revisions


def update_descendants(store, task_id):
    pending = {task_id}
    for task in store.rows("tasks"):
        if not pending.intersection(task["dependencies"]):
            continue
        pending.add(task["id"])
        if task["state"] in FINISHED or not task.get("attempt"):
            continue
        attempt = store.get("attempts", task["attempt"])
        if attempt["state"] in ACTIVE:
            attempt["stop"] = "stale"
            store.put("attempts", attempt)
        task.update(state="stale", approved=None)
        store.put("tasks", task)
        store.event(task["id"], "dependency_invalidated", upstream=task_id)


def current_quota(store, instance, now):
    blocked = False
    live = []
    for kind, stamp, value in store.quota_signals(instance.get("bound_account")):
        fresh = now - stamp < PROBE_TTL
        if kind == "blocked":
            blocked = fresh and value
            continue
        window = value
        reset = window.get("resets_at")
        if isinstance(reset, (float, int)) and reset <= now:
            continue
        if fresh or (isinstance(reset, (float, int)) and window["used"] >= 100):
            live.append(window["used"])
    return blocked or any(u >= 100 for u in live), bool(live), max(live, default=0)


def model_settings(task, provider, available, prior):
    model, effort = task.get("model"), task.get("effort")
    if provider == "claude":
        if model and model not in available and not model.startswith("claude-"):
            return None
        if effort and effort not in {"low", "medium", "high", "xhigh", "max", "ultracode"}:
            return None
        return {"model": model, "effort": effort}

    if prior and prior.get("model_selection"):
        model = prior["model_selection"]["model"]
        effort = prior["model_selection"]["effort"]
    elif prior and not model:
        model = prior.get("model")
    if not model:
        versions = []
        for identifier in available:
            match = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-sol", identifier or "")
            if match:
                versions.append((tuple(map(int, match[1].split("."))), identifier))
        if not versions:
            return None
        model = max(versions)[1]
    effort = effort or "medium"
    if model not in available or effort not in available[model]["efforts"]:
        return None
    return {"model": model, "effort": effort}


def choose(store, task, instances, active, now):
    candidates = []
    allowed = [task["target"]] if task.get("target") else task["allowed"]
    for instance in instances:
        name, info = instance["id"], instance.get("status") or {}
        account = instance.get("bound_account")
        if (name not in allowed or instance.get("auth_busy") or instance.get("deleting")
                or not account or not info.get("logged_in") or info.get("account") != account
                or info.get("schema") != backend.PROBE_SCHEMA
                or not instance.get("probed_at")  # invalidated (e.g. account mismatch): wait for a re-probe
                or (instance["provider"] == "codex" and not instance.get("bound_scoped"))):
            continue
        generation = task.get("bindings", {}).get(name)
        if generation is not None and generation != instance["generation"]:
            continue
        if task["kind"] == "image" and not (instance["provider"] == "codex" and info.get("image")):
            continue
        if instance["provider"] == "codex" and set(backend.required_capabilities(access.of(task))) & set(
                (info.get("capabilities") or {}).get("missing", [])):
            continue  # never run a task below the tier it was submitted with
        prior = store.get("attempts", task["attempt"]) if task.get("native_resume") else None
        if prior and (prior["generation"] != instance["generation"] or prior["account"] != account):
            continue
        available = {item["id"]: item for item in info.get("models", [])}
        settings = model_settings(task, instance["provider"], available, prior)
        if settings is None:
            continue
        blocked, observed, used = current_quota(store, instance, now)
        if any(a.get("account") == account and a.get("limited_until", 0) > now for a in store.rows("attempts")):
            blocked = True
        if blocked:
            continue
        own = sum(a["instance"] == name for a in active)
        load = sum(a["account"] == account for a in active)
        requested = task.get("parallel") or 0
        if own >= max(instance["parallel"], requested) or (not observed and load and requested <= 1):
            continue
        candidates.append(((not observed, load, own, used, name), instance,
                           {"account_load": load, "instance_load": own, "quota_observed": observed, "used": used,
                            "model_selection": settings}))
    return min(candidates, key=lambda row: row[0], default=(None, None, None))[1:]


def stop_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def reconcile(store):
    with store.transaction():
        for attempt in store.rows("attempts"):
            if attempt["state"] not in ACTIVE:
                continue
            pid = attempt.get("pid")
            if not pid and time.time() - attempt["reserved"] < 15:
                continue
            if pid and attempt.get("identity") and identity(pid) == attempt["identity"]:
                continue
            if pid:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    # Session exists but its recorded leader has gone. Block redispatch until drained.
                    attempt["state"] = "stopping"
                    store.put("attempts", attempt)
                    if not identity(pid):
                        stop_group(pid, signal.SIGKILL)
                    continue
            task = store.get("tasks", attempt["task"])
            if task["attempt"] == attempt["id"]:
                recovery = recover_session(attempt)
                finish(store, task, attempt, {**recovery, "outcome": attempt.get("stop") or "interrupted",
                                              "diagnostic": "工作进程中断，已尝试保存检查点"})
            else:
                attempt.update(state="interrupted", outcome="interrupted", end=time.time())
                store.put("attempts", attempt)
            store.event(attempt["task"], "worker_interrupted", attempt=attempt["id"])


def recover_session(attempt):
    if not attempt.get("logs"):
        return {}
    result = {}
    for event in backend.read_jsonl(Path(attempt["logs"]) / "events.jsonl"):
        if event.get("type") == "system" and event.get("subtype") == "init":
            result.update(session=event.get("session_id"), model=event.get("model"))
    return result


def dispatch(store, *, parallel, once):
    with lock(store.root / "locks" / "queue-worker", blocking=False):
        children = []
        while True:
            children[:] = [child for child in children if child.poll() is None]
            reconcile(store)
            tasks = store.rows("tasks")
            active = [attempt for attempt in store.rows("attempts") if attempt["state"] in ACTIVE]
            pending = [task for task in tasks if task["state"] in {"queued", "waiting_quota"}]
            if pending and len(active) < parallel:
                wanted = {name for task in pending for name in
                          ([task["target"]] if task.get("target") else task["allowed"])}
                for instance in store.rows("instances"):
                    if instance["id"] not in wanted or instance.get("auth_busy") or instance.get("deleting"):
                        continue
                    try:
                        with instance_lock(store, instance["id"]):
                            refresh_instance(store, instance)
                    except DelegateError:
                        continue
                for original in pending:
                    if len(active) >= parallel:
                        break
                    with store.transaction():
                        task = store.get("tasks", original["id"])
                        if task["state"] not in {"queued", "waiting_quota"} or task.get("wait_until", 0) > time.time():
                            continue
                        try:
                            revisions = dependencies(store, task)
                        except DelegateError:
                            if task.get("blocked_by") != "dependencies":
                                task["blocked_by"] = "dependencies"
                                store.put("tasks", task)
                                store.event(task["id"], "waiting_for_dependencies")
                            continue
                        instance, reason = choose(store, task, store.rows("instances"), active, time.time())
                        if not instance:
                            if task.get("blocked_by") != "eligible_instance":
                                task["blocked_by"] = "eligible_instance"
                                store.put("tasks", task)
                                store.event(task["id"], "waiting_for_instance")
                            continue
                        attempt_id = task["attempt"] if task.get("native_resume") else new_id("attempt")
                        previous = store.get("attempts", attempt_id, required=False) or {}
                        attempt = {**previous, "id": attempt_id, "task": task["id"], "instance": instance["id"],
                                   "account": instance["bound_account"], "generation": instance["generation"],
                                   "quota_before": (instance.get("status") or {}).get("quota"),
                                   "model_selection": reason["model_selection"],
                                   "state": "starting", "reserved": time.time(), "pid": None, "identity": None,
                                   "reservation": secrets.token_hex(12),
                                   "stop": None, "revisions": list(revisions.items()), "outcome": None}
                        task.setdefault("bindings", {})[instance["id"]] = instance["generation"]
                        task.update(state="starting", attempt=attempt_id, blocked_by=None)
                        store.put("attempts", attempt)
                        store.put("tasks", task)
                        store.event(task["id"], "scheduled", instance=instance["id"], attempt=attempt_id, reason=reason)
                    env = {**os.environ, "CLI_MANAGER_STATE": str(store.root)}
                    try:
                        queue_log = store.task_dir(task["id"]) / "supervisor.log"
                        with queue_log.open("ab") as output:
                            child = subprocess.Popen(
                                [sys.executable, str(ENTRY), "_attempt", attempt_id, attempt["reservation"]], env=env,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
                            )
                        children.append(child)
                    except OSError:
                        with store.transaction():
                            attempt["reserved"] = 0
                            store.put("attempts", attempt)
                        reconcile(store)
                        raise
                    active.append(attempt)
            if once or (not active and not pending):
                return
            time.sleep(.5 if active else 3)


def validate_report(result):
    report = result.get("structured")
    return (isinstance(report, dict) and set(report) == set(backend.REPORT_KEYS)
            and all(isinstance(entries, list) and all(isinstance(value, str) for value in entries)
                    for entries in report.values()))


def monitor(root, task, attempt, cancel, reason, done):
    from .store import Store
    store = Store(root)
    clock = time.monotonic()
    spent = 0.0
    try:
        while not done.wait(.25):
            current = store.get("attempts", attempt["id"])
            now = time.monotonic()
            if not current.get("logs") or not pending_requests(Path(current["logs"]) / "approvals"):
                spent += now - clock
            clock = now
            if current.get("stop"):
                reason.append(current["stop"])
            elif spent >= task["timeout"]:
                reason.append("timed_out")
            if reason:
                cancel.set()
                return
    finally:
        store.db.close()


def pending_requests(location):
    requests = []
    for path in sorted(location.glob("*.request.json")):
        key = path.name.split(".", 1)[0]
        if (location / f"{key}.decision.json").exists():
            continue
        try:
            data = read(path)
        except (OSError, ValueError):
            continue
        expires = data.get("expires_at")
        if expires is None:
            expires = path.stat().st_mtime + data.get("timeout_s", 1800)
        if expires > time.time():
            requests.append({**data, "id": key, "expires_at": expires})
    return requests


def drain_group():
    pid = os.getpid()
    if os.getpgrp() != pid:
        return
    deadline = time.monotonic() + 3
    while True:
        proc = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True,
                              text=True, check=True, timeout=5)
        children = []
        for line in proc.stdout.splitlines():
            child, group, state = line.split()
            child_id = int(child)
            if int(group) != pid or child_id == pid or state.startswith("Z"):
                continue
            try:
                if os.getpgid(child_id) == pid:
                    children.append(child_id)
            except ProcessLookupError:
                continue
        if not children:
            return
        for child in children:
            try:
                if os.getpgid(child) == pid:
                    os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                continue
        if time.monotonic() >= deadline:
            raise DelegateError("执行进程组仍有后代进程，不能生成成果检查点")
        time.sleep(.05)


def execute(store, attempt_id, reservation):
    pid = os.getpid()
    with store.transaction():
        attempt = store.get("attempts", attempt_id)
        task = store.get("tasks", attempt["task"])
        instance = store.get("instances", attempt["instance"])
        if attempt["state"] != "starting" or attempt.get("reservation") != reservation:
            return
        if attempt.get("stop"):
            attempt.update(state="cancelled", end=time.time())
            store.put("attempts", attempt)
            task["state"] = attempt["stop"]
            store.put("tasks", task)
            return
        attempt.update(state="running", pid=pid, identity=identity(pid), start=time.time(), logs=None)
        task["state"] = "running"
        store.put("attempts", attempt)
        store.put("tasks", task)
    directory = store.attempt_dir(attempt)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stopped, done = threading.Event(), threading.Event()
    reasons = []
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    watcher = threading.Thread(target=monitor, args=(store.root, task, attempt, stopped, reasons, done), daemon=True)
    watcher.start()
    result, extras = {}, {}
    try:
        if instance["provider"] == "codex":
            # Only account/read: a failing model or feature query must not hide a mismatch.
            backend.verify_codex_account(store, instance, attempt["account"], attempt["generation"])
            verified = {"logged_in": True, "account": attempt["account"]}
        else:
            verified = backend.probe(store, instance, query_quota=False)
        if (not verified["logged_in"] or verified["account"] != attempt["account"]
                or instance["generation"] != attempt["generation"]):
            invalidate_probe(store, instance["id"], attempt["generation"])
            result = {"outcome": "auth", "diagnostic": "实例账户已改变或未登录"}
        else:
            extras, result = run_native(store, instance, task, attempt, directory, stopped)
    except backend.AccountMismatch as error:
        invalidate_probe(store, instance["id"], attempt["generation"])
        result = {"outcome": "auth", "diagnostic": str(error)[:1000]}
    except (DelegateError, OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        result = {"outcome": "failed", "diagnostic": str(error)[:1000]}
    finally:
        done.set()
        stopped.set()
        watcher.join(timeout=2)
    try:
        drain_group()
    except (DelegateError, OSError, subprocess.SubprocessError) as error:
        # Leave the live reservation for the queue to reconcile after this
        # supervisor exits. It must not capture a snapshot of a live worker.
        print(error, file=sys.stderr)
        return
    attempt.update(extras)
    # A confirmed account mismatch stays `auth` when the deadline passes during its cleanup;
    # an explicit stop (cancel) still takes precedence.
    if reasons and not (result.get("outcome") == "auth" and reasons[0] == "timed_out"):
        result["outcome"] = reasons[0]
    if reason := attempt.get("handoff_migration_fallback"):
        result["diagnostic"] = "\n".join(filter(None, ["交接回退：" + reason, result.get("diagnostic")]))
    prepared = capture_result(store, task, attempt, result)
    with store.transaction():
        live = store.get("attempts", attempt_id)
        if live.get("stop"):
            result["outcome"] = live["stop"]
        finish(store, task, attempt, result, prepared)


def run_native(store, instance, task, attempt, directory, cancel):
    prompt = (store.task_dir(task["id"]) / "brief.md").read_text(encoding="utf-8")
    logs = directory / "runs" / new_id("run")
    logs.mkdir(mode=0o700, parents=True)
    data = {"logs": str(logs)}
    source = None
    if task.get("handoff"):
        handoff_tasks = task["handoff"]
        original = "\n".join("> " + line for line in prompt.splitlines())
        prompt = ("Use the transferred checkpoint as the current state. Follow the handoff plan "
                  "and verify completed work before executing anything.\n\n## 原任务（仅供核对约束）\n"
                  + original + "\n\n## 主会话交接\n" + Path(handoff_tasks["brief"]).read_text())
        source = store.get("attempts", handoff_tasks["source"])
    if task.get("continue_note"):
        prompt += "\n\n## 主会话追加指示\n" + task["continue_note"]
    if task["kind"] == "image":
        work = directory / "work"
        work.mkdir(mode=0o700, exist_ok=True)
        data.update(work=str(work), cwd=str(work))
    elif not task.get("native_resume"):
        patches = []
        for task_id, revision in attempt["revisions"]:
            dependency_task = store.get("tasks", task_id)
            dependency_report = review_report(store, dependency_task, revision)
            patch = (store.task_dir(task_id) / "checkpoints" / f"{revision}.patch").read_bytes()
            if hashlib.sha256(patch).hexdigest() != dependency_report["patch_sha256"]:
                raise DelegateError("依赖补丁已变化")
            patches.append(patch)
        if source and [tuple(pair) for pair in source["revisions"]] != [tuple(pair) for pair in attempt["revisions"]]:
            raise DelegateError("交接期间上游版本发生变化，请重新规划此任务")
        checkpoint = (Path(source["repo"]), source["base_tree"], source["checkpoint"], source["checkpoint_tree"]) if source else None
        data.update(workspace.setup(directory, store.task_dir(task["id"]) / "baseline.git",
                                    task, attempt, patches, checkpoint))
    elif attempt.get("checkpoint_tree"):
        # Only controlled files count: links to the original project's ignored content and
        # ignored artefacts left in the workspace (screenshots, logs) are not a deviation.
        changed = workspace.drift(Path(attempt["repo"]), Path(attempt["work"]), attempt,
                                  force_all=not task["baseline"]["git"])
        if changed:
            listed = "、".join(changed[:8]) + ("…" if len(changed) > 8 else "")
            raise DelegateError(f"原生续跑的工作目录已偏离检查点（受控文件：{listed}）")
    if (task["kind"] != "image" and instance["provider"] == "codex" and access.is_open(access.of(task))
            and (data.get("work") or attempt.get("work"))):
        data["mounts"] = workspace.mount_ignored(task, Path(data.get("work") or attempt["work"]),
                                                 attempt.get("mounts") or ())
        if task["baseline"]["git"]:
            workspace.register_mounts(Path(data.get("repo") or attempt["repo"]), data["mounts"])
    attempt.update(data)
    with store.transaction():
        row = store.get("attempts", attempt["id"])
        row.update(data)
        store.put("attempts", row)
    if cancel.is_set():
        return data, {"outcome": "cancelled"}
    env = backend.worker_environment(store, instance, task, attempt)
    action = (backend.collect_image if task["kind"] == "image" else
              backend.collect_codex if instance["provider"] == "codex" else backend.collect_claude)
    configured_task = {**task, **(attempt.get("model_selection") or {})}
    params = (store, instance, configured_task, attempt, logs, env, prompt, cancel)
    if source and not task.get("native_resume"):
        def record(mode, reason=None, quota=None):
            fields = {"handoff_mode": mode}
            if reason:
                fields.update(handoff_migration_fallback=reason, session=None)
            with store.transaction():
                if quota:
                    # The migrated turn ran under the verified target account; keep what it measured.
                    target = store.get("instances", attempt["instance"])
                    if (target["generation"], target.get("bound_account")) == (attempt["generation"], attempt["account"]):
                        store.record_quota(target, quota)
                row = store.get("attempts", attempt["id"])
                row.update(fields)
                store.put("attempts", row)
                if reason:
                    store.event(task["id"], "handoff_migration_fallback", attempt=attempt["id"], reason=reason)
            attempt.update(fields)

        source_instance = store.get("instances", source["instance"])
        if task["kind"] != "image" and source_instance["provider"] == instance["provider"] == "codex":
            session = source.get("session")
            try:
                if not session:
                    raise ValueError("源尝试没有原生会话")
                backend.copy_rollout(store.instance_dir(source["instance"]) / "cli", Path(env["CODEX_HOME"]), session)
            except (ValueError, OSError) as error:
                record("brief", str(error))
            else:
                result = backend.collect_codex(*params, session, migrating=True)
                if not result.get("migration_retry") or cancel.is_set():
                    record("migrated")
                    return data, result
                record("brief", result.get("diagnostic") or "迁移后首轮在调用工具前失败",
                       quota=result.get("quota"))
        else:
            record("brief")
    if cancel.is_set():
        return data, {"outcome": "cancelled"}
    if task["kind"] == "image":
        return data, action(*params)
    session = attempt.get("session") if task.get("native_resume") else None
    return data, action(*params, session)


def artifact(path):
    with path.open("rb") as stream:
        return {"path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def capture_result(store, task, attempt, result):
    paths, patch, artifacts = [], b"", []
    validation_issue = None
    if attempt.get("repo"):
        repo, work = Path(attempt["repo"]), Path(attempt["work"])
        try:
            commit = workspace.snapshot(repo, work, attempt["base"], force_all=not task["baseline"]["git"],
                                        exclude=attempt.get("mounts") or ())
            workspace.git(repo, "update-ref", "refs/heads/checkpoint", commit)
            attempt.update(checkpoint=commit, checkpoint_tree=workspace.text(repo, "rev-parse", commit + "^{tree}"))
            paths = workspace.changes(attempt, commit)
            for path in paths:
                value = workspace.tree_entry(repo, commit, path)
                if value and value["type"] != "directory":
                    workspace.entry(work, path)
            patch = workspace.git(repo, "diff", "--no-renames", "--binary", attempt["base"], commit)
            if task["kind"] == "review" and paths:
                raise DelegateError("只读任务产生工作树改动")
        except (DelegateError, OSError, subprocess.SubprocessError) as error:
            validation_issue = str(error)
    if result.get("image") and attempt.get("logs"):
        source = Path(attempt["logs"]) / result["image"]
        exported = store.task_dir(task["id"]) / "reports" / (attempt["id"] + source.suffix)
        write(exported, source.read_bytes())
        artifacts.append(artifact(exported))
    for index, generated in enumerate(result.get("generated") or [], 1):
        # Built-in image generation inside a code task: keep a controller-owned copy for review
        # and note where the worker placed the image.
        source = Path(generated)
        exported = store.task_dir(task["id"]) / "reports" / f"{attempt['id']}-image{index}{source.suffix.lower()}"
        write(exported, source.read_bytes())
        record = artifact(exported)
        record.update(kind="generated_image", copies=image_copies(task, attempt, paths, record))
        artifacts.append(record)
    return paths, patch, artifacts, validation_issue


def image_copies(task, attempt, paths, record):
    """Where the worker put a generated image: changed workspace paths and granted write directories."""
    candidates = [Path(attempt["work"]) / path for path in paths] if attempt.get("work") else []
    for directory in access.of(task).get("write_dirs") or []:
        for index, path in enumerate(Path(directory).rglob("*")):
            if index >= 5000:
                break
            candidates.append(path)
    copies = []
    for path in candidates:
        try:
            if (path.suffix.lower() in backend.IMAGE_FORMATS and path.is_file() and not path.is_symlink()
                    and path.stat().st_size == record["bytes"] and artifact(path)["sha256"] == record["sha256"]):
                copies.append(str(path))
        except OSError:
            continue
    return copies


def finish(store, task, attempt, result, prepared=None):
    task = store.get("tasks", task["id"])
    code = result.get("outcome") or "failed"
    attempt.update(end=time.time(), session=result.get("session") or attempt.get("session"),
                   outcome=code, model=result.get("model") or attempt.get("model"), usage=result.get("usage"),
                   cost_usd=result.get("cost_usd"), quota_after=result.get("quota"))
    paths, patch, artifacts, validation_issue = prepared or capture_result(store, task, attempt, result)
    if validation_issue:
        code = "failed"
    elif code == "ok" and not validate_report(result):
        code = "needs_report"
    state = {"ok": "review", "quota": "waiting_quota"}.get(code, code)
    attempt.update(state=state, outcome=code, paths=paths)
    if code == "auth" or result.get("quota"):
        instance = store.get("instances", attempt["instance"], required=False)
        if instance and instance["generation"] == attempt["generation"]:
            if code == "auth":
                instance["probed_at"] = 0
                instance["invalidations"] = instance.get("invalidations", 0) + 1
            elif (instance.get("bound_account") == attempt.get("account")
                  and not instance.get("auth_busy") and not instance.get("deleting")
                  and (status := instance.get("status") or {}).get("account") == attempt.get("account")
                  and status.get("logged_in")):
                store.record_quota(instance, result["quota"])
                if result["quota"]["quality"] == "observed":
                    status["quota"] = result["quota"]
                instance["status"] = status
            store.put("instances", instance)
    if code == "quota":
        quota = result.get("quota") or {}
        times = [w["resets_at"] for w in quota.get("windows", [])
                 if w["used"] >= 100 and isinstance(w.get("resets_at"), (int, float))]
        prior = task.get("quota_hits", 0)
        attempt["limited_until"] = max(times) + 30 if times else time.time() + min(1800, 60 * 2 ** min(prior, 5))
        task.update(quota_hits=prior + 1, wait_until=attempt["limited_until"], target=attempt["instance"],
                    native_resume=bool(attempt.get("session") and task["kind"] != "image"))
    selection = attempt.get("model_selection") or {}
    report = {"schema_version": 1, "task": task["id"], "attempt": attempt["id"], "state": state,
              "started": attempt.get("start"), "ended": attempt["end"], "instance": attempt["instance"],
              "model": attempt["model"], "requested_model": task.get("model"), "selected_model": selection.get("model"),
              "effort": selection.get("effort", task.get("effort")), "requested_effort": task.get("effort"),
              "reported": result.get("structured"), "usage": result.get("usage"),
              "cost_usd": result.get("cost_usd"), "quota_before": attempt.get("quota_before"),
              "quota_after": result.get("quota"),
              "diagnostic": validation_issue or result.get("diagnostic"), "verification": None,
              "baseline": attempt.get("base"), "paths": paths, "branch": attempt.get("branch"),
              "work": attempt.get("work"), "commit": attempt.get("checkpoint"),
              "dependencies": attempt["revisions"], "artifacts": artifacts,
              "patch_sha256": hashlib.sha256(patch).hexdigest()}
    if attempt.get("capabilities"):
        # Tasks from before capability tiers keep their original report shape.
        report["capabilities"] = attempt["capabilities"]
        report["approvals"] = approval_summary(attempt)
    reports = store.task_dir(task["id"])
    revision = digest(report)
    report.update(revision=revision)
    write(reports / "checkpoints" / f"{revision}.patch", patch)
    write_json(reports / "reports" / f"{revision}.json", report)
    report_path = reports / "reports" / f"{revision}.md"
    summary = [f"# {task['id']} · {state}", "", f"实例 {attempt['instance']} · 模型 {attempt['model'] or '未知'}"]
    summary += ["", "变更文件"] + [f"- {path}" for path in paths]
    summary += ["", "子代理报告", "", json.dumps(report["reported"], ensure_ascii=False, indent=2)]
    if report["diagnostic"]:
        summary += ["", str(report["diagnostic"])]
    write(report_path, ("\n".join(summary) + "\n").encode())
    attempt["report"] = revision
    attempt["artifacts"] = artifacts
    store.put("attempts", attempt)
    if task["state"] != "stale":
        task.update(state=state, report=revision, review_candidate=None,
                    native_resume=bool(task.get("native_resume")) if code == "quota" else False,
                    target=task.get("target") if code == "quota" else None)
    store.put("tasks", task)
    store.event(task["id"], "attempt_finished", attempt=attempt["id"], outcome=code, report=revision)


def approval_summary(attempt):
    """Requests of the attempt's current run and how each was settled."""
    rows = []
    if not attempt.get("logs"):
        return rows
    folder = Path(attempt["logs"]) / "approvals"
    for request_file in sorted(folder.glob("*.request.json")):
        key = request_file.name.replace(".request.json", "")
        try:
            request = read(request_file)
            decision = read(folder / f"{key}.decision.json") if (folder / f"{key}.decision.json").is_file() else {}
        except (OSError, ValueError):
            continue
        inputs = request.get("input") or {}
        rows.append({"request": key, "kind": request.get("kind"), "floor": request.get("floor"),
                     "command": inputs.get("command"), "cwd": inputs.get("cwd"), "paths": inputs.get("paths"),
                     "network": inputs.get("network"), "behavior": decision.get("behavior") or "unanswered",
                     "always": bool(decision.get("always")), "by": decision.get("by")})
    return rows


def stop_task(store, name):
    with store.transaction():
        task = store.get("tasks", name)
        if task["state"] in FINISHED or task["state"] in {"applying", "approved"}:
            raise DelegateError("当前状态不能取消")
        if task["state"] in ACTIVE:
            attempt = store.get("attempts", task["attempt"])
            attempt["stop"] = "cancelled"
            store.put("attempts", attempt)
        else:
            task["state"] = "cancelled"
            store.put("tasks", task)
        store.event(name, "cancel_requested")


def requeue(store, name, *, native, note):
    with store.transaction():
        task = store.get("tasks", name)
        if task["state"] in FINISHED | ACTIVE | {"approved", "applying", "queued"}:
            raise DelegateError("请先完成当前操作或退回审核")
        attempt = store.get("attempts", task["attempt"]) if task.get("attempt") else None
        if attempt and attempt["state"] in ACTIVE:
            raise DelegateError("尝试尚未停止")
        if native:
            if task["state"] == "stale" or not attempt or not attempt.get("session") or task["kind"] == "image":
                raise DelegateError("当前尝试不能使用原生会话续跑")
            instance = store.get("instances", attempt["instance"])
            if (instance["generation"], instance["bound_account"]) != (attempt["generation"], attempt["account"]):
                raise DelegateError("原生续跑要求原实例、账户和登录代次")
            if list(dependencies(store, task).items()) != [tuple(pair) for pair in attempt["revisions"]]:
                raise DelegateError("原生续跑要求依赖版本一致")
            if not Path(attempt.get("work", "/nonexistent")).is_dir():
                raise DelegateError("原任务工作目录不存在")
        task.update(state="queued", native_resume=native, continue_note=note or task.get("continue_note"),
                    handoff=task.get("handoff"),
                    target=attempt["instance"] if native else (task.get("handoff") or {}).get("target"), review_candidate=None, report=None)
        if not native and attempt and attempt.get("outcome") == "quota":
            # A retry is not permission to rotate accounts after a provider limit.
            task["target"] = attempt["instance"]
        store.put("tasks", task)
        store.event(name, "resume_queued" if native else "retry_queued", prior=attempt and attempt["id"])
    return task


def get_pending(store, task):
    if not task.get("attempt") or task["state"] not in ACTIVE:
        return [], None
    attempt = store.get("attempts", task["attempt"])
    if not attempt.get("logs"):
        return [], None
    folder = Path(attempt["logs"]) / "approvals"
    return pending_requests(folder), folder


def decide(store, name, request_id, action, answers, message, always=False):
    identifier(request_id)
    with store.transaction():
        task = store.get("tasks", name)
        requests, folder = get_pending(store, task)
        request = next((r for r in requests if r["id"] == request_id), None)
        if not request:
            raise DelegateError("请求不存在、已答复或已过期")
        tool, inputs = request.get("tool_name"), request.get("input") or {}
        question = request.get("kind") == "question"
        if action != "deny" and question != (action == "answer"):
            raise DelegateError("提问使用 answer，工具审批使用 allow")
        native = request.get("provider") == "codex" and not question
        if always and (action != "allow" or not native):
            raise DelegateError("--always 只用于批准 Codex 的越界请求")
        if action == "answer":
            provided = {}
            for item in inputs.get("questions") or []:
                if item.get("isSecret"):
                    raise DelegateError("禁止通过任务通道提交凭据")
                key = item.get("id") or item.get("question")
                value = answers.get(key, answers.get(item.get("question")))
                if not (isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(x, str) for x in value))):
                    raise DelegateError(f"缺少有效答案：{key}")
                provided[key] = value
            if tool == "AskUserQuestion":
                answer = {key: ", ".join(value) if isinstance(value, list) else value for key, value in provided.items()}
                decision = {"behavior": "allow", "updatedInput": {**inputs, "answers": answer}}
            else:
                decision = {"behavior": "answer", "answers": provided}
        elif action == "allow" and native:
            # The request came from an open-tier Codex worker; the floor was applied before it was
            # shown. An irreversible action is approved one request at a time, never for the session.
            if always and not request.get("session_allow"):
                raise DelegateError("不可逆操作（推送、发布、删除工作树外文件等）必须逐条审批，不能本会话放行")
            decision = {"behavior": "allow", "always": bool(always), "by": "planner"}
        elif action == "allow":
            if tool not in {"Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}:
                raise DelegateError("这类权限请求不能扩大任务沙箱")
            if tool == "Bash" and (inputs.get("dangerouslyDisableSandbox") or task["kind"] != "code"):
                raise DelegateError("命令不得越出已授权沙箱")
            attempt = store.get("attempts", task["attempt"])
            for key in ("path", "file_path", "notebook_path"):
                if inputs.get(key):
                    target = (Path(attempt["cwd"]) / inputs[key]).resolve()
                    if not target.is_relative_to(Path(attempt["work"])):
                        raise DelegateError("工具请求越出任务目录")
                    if tool in {"Edit", "Write", "MultiEdit"}:
                        workspace.safe_path(Path(attempt["work"]), target.relative_to(attempt["work"]).as_posix())
            decision = {"behavior": "allow", "updatedInput": inputs}
        else:
            decision = {"behavior": "deny", "message": message or "主会话拒绝此请求"}
        write_json(folder / f"{request_id}.decision.json", decision)
        detail = {"always": True} if always else {}
        if native:
            inputs_view = request.get("input") or {}
            detail.update(request_kind=request.get("kind"), floor=request.get("floor"),
                          command=inputs_view.get("command"), cwd=inputs_view.get("cwd"),
                          paths=inputs_view.get("paths"), network=inputs_view.get("network"))
        store.event(name, "approval_decided", attempt=task["attempt"], request=request_id, action=action, **detail)
    return {"id": request_id, "action": action, **({"always": True} if always else {})}


def prepare_handoff(store, name):
    task = store.get("tasks", name)
    if task["state"] in ACTIVE | FINISHED | {"approved", "applying", "queued"}:
        raise DelegateError("交接要求未合入、已停止的尝试")
    attempt = store.get("attempts", task["attempt"])
    if task["kind"] != "image" and not attempt.get("checkpoint"):
        raise DelegateError("没有可信工作树检查点")
    report_path = store.task_dir(name) / "reports" / (attempt["report"] + ".json")
    decisions = []
    if attempt.get("logs"):
        folder = Path(attempt["logs"]) / "approvals"
        for request_file in sorted(folder.glob("*.request.json")):
            key = request_file.name.replace(".request.json", "")
            decision_file = folder / (key + ".decision.json")
            if decision_file.is_file():
                request = read(request_file)
                decision = read(decision_file)
                decisions.append({"request": key, "tool": request.get("tool_name"),
                                  "behavior": decision.get("behavior"), "message": decision.get("message")})
    material = {"task": name, "source": attempt["id"], "initial_brief": str(store.task_dir(name) / "brief.md"),
                "source_instance": attempt["instance"], "work": attempt.get("work"),
                "checkpoint": attempt.get("checkpoint"), "checkpoint_tree": attempt.get("checkpoint_tree"),
                "report": read(report_path), "decisions": decisions}
    path = store.task_dir(name) / "handoffs" / f"{attempt['id']}.material.json"
    write_json(path, material)
    store.event(name, "handoff_prepared", attempt=attempt["id"])
    return {"material": str(path), "source": attempt["id"], "preauthorized": task["handoff_targets"]}


def start_handoff(store, name, target, brief, authorized, source_id, model=None, effort=None, *,
                  authorize_access=False, confirm_full=False, interactive=None):
    if not brief.strip():
        raise DelegateError("交接要求主会话撰写简报")
    preview, destination = store.get("tasks", name), store.get("instances", target)
    if (access.of(preview)["access"] == "full" and destination["provider"] == "codex"
            and not (target in preview["handoff_targets"]
                     and preview.get("bindings", {}).get(target) == destination["generation"])):
        # `full` is granted by a person for named accounts; another account needs the same confirmation.
        if not confirm_full:
            raise DelegateError("full 档交接到另一个账户需要 --confirm-full，并由用户在交互式终端确认")
        access.confirm_full(interactive)
    with store.transaction():
        task = store.get("tasks", name)
        if task["state"] in ACTIVE | FINISHED | {"approved", "applying", "queued"}:
            raise DelegateError("任务目前无法交接")
        source = store.get("attempts", source_id)
        other = store.get("instances", target)
        original = store.get("instances", source["instance"])
        material = store.task_dir(name) / "handoffs" / f"{source_id}.material.json"
        if not material.is_file() or source["task"] != name or source_id != task["attempt"]:
            raise DelegateError("请先对当前尝试生成交接素材")
        if source["state"] in ACTIVE or target == original["id"]:
            raise DelegateError("交接要求源已停止、目标是另一个实例")
        if task["kind"] == "image" and other["provider"] != "codex":
            raise DelegateError("生图仅可交接到 Codex")
        if original["provider"] == other["provider"] and source.get("outcome") == "quota" and (
                source.get("account") == other.get("bound_account") or not source.get("account")):
            raise DelegateError("额度受限任务交接到同服务方时必须更换账户")
        generation = task.get("bindings", {}).get(target)
        allowed = target in task["handoff_targets"] and generation in (None, other["generation"])
        if not allowed and not authorized:
            raise DelegateError("目标账户未预授权或已切换，请由主会话在获得授权后显式提交")
        if not other.get("bound_account"):
            raise DelegateError("目标实例尚未完成账户绑定")
        if task["kind"] != "image" and not source.get("checkpoint"):
            raise DelegateError("源尝试没有安全检查点")
        capabilities = access.of(task)
        # The tier travels with the task. A target named at submission was granted it together with
        # the task, for the account it was logged in to then; any other account is granted it again.
        changed_account = not (target in task["handoff_targets"] and generation == other["generation"])
        if other["provider"] == "codex" and access.is_open(capabilities):
            if changed_account and not authorize_access:
                raise DelegateError(
                    f"目标实例的账户未随任务授权或已切换：交接会把 {capabilities['access']} 档"
                    f"（网络 {capabilities['network']}、授权目录、生图）一并带过去，请在获得授权后加 --authorize-access")
            missing = sorted(set(backend.required_capabilities(capabilities))
                             & set(((other.get("status") or {}).get("capabilities") or {}).get("missing", [])))
            if missing:
                raise DelegateError(f"目标实例的 Codex 不支持所需能力：{'、'.join(missing)}")
        path = store.task_dir(name) / "handoffs" / (new_id(source_id) + ".brief.md")
        write(path, brief.encode())
        task.setdefault("bindings", {})[target] = other["generation"]
        task.update(state="queued", target=target, native_resume=False, report=None, review_candidate=None,
                    wait_until=0, continue_note=None,
                    model=model or (task.get("model") if other["provider"] == original["provider"] else None),
                    effort=effort or (task.get("effort") if other["provider"] == original["provider"] else None),
                    handoff={"source": source_id, "brief": str(path), "target": target})
        store.put("tasks", task)
        store.event(name, "handoff_queued", source=source_id, target=target,
                    capabilities=access.summary(capabilities), account_changed=changed_account,
                    access_reauthorized=bool(changed_account and authorize_access))
    return task


def review_report(store, task, revision):
    if task.get("report") != revision and task.get("review_candidate") != revision:
        raise DelegateError("报告已变化，请重新读取结果")
    if not revision or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise DelegateError("需要完整报告摘要")
    path = store.task_dir(task["id"]) / "reports" / (revision + ".json")
    report = read(path)
    if report.pop("revision") != revision or digest(report) != revision:
        raise DelegateError("报告摘要校验失败")
    patch = store.task_dir(task["id"]) / "checkpoints" / f"{revision}.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != report["patch_sha256"]:
        raise DelegateError("报告对应的变更文件校验失败")
    return report


def verify_report(store, task, report):
    if not task.get("verify"):
        return {"status": "not_configured"}
    attempt = store.get("attempts", report["attempt"])
    with tempfile.TemporaryDirectory(dir=store.task_dir(task["id"])) as tmp_name:
        directory = Path(tmp_name)
        repo, work = directory / "test.git", directory / "work"
        workspace.init_repo(repo, literal=not task["baseline"]["git"])
        workspace.import_commit(repo, Path(attempt["repo"]), report["commit"])
        workspace.git(repo, "worktree", "add", "--quiet", "--detach", str(work), report["commit"])
        if not task["baseline"]["git"]:
            (work / ".git").unlink()
        home, tmp = directory / "home", directory / "tmp"
        for path in (home, tmp):
            path.mkdir(mode=0o700)
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
               "CODEX_HOME": str(home), "TMPDIR": str(tmp), "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_NOSYSTEM": "1"}
        instance = next((i for i in store.rows("instances") if i["provider"] == "codex"), {"provider": "codex"})
        options = backend.codex_profile(work, repo if task["baseline"]["git"] else None, tmp, False)
        backend.validate_platform(instance, env, directory, options, work, False)
        command = [backend.binary(instance), "sandbox", "-P", "delegate", *options, "--",
                   "/bin/sh", "-c", task["verify"]]
        output = directory / "verification.log"
        with output.open("wb") as logs:
            child = subprocess.Popen(command, cwd=work / task["baseline"]["project_subdir"], env=env,
                                     stdin=subprocess.DEVNULL, stdout=logs, stderr=logs, start_new_session=True)
            try:
                rc = child.wait(timeout=task["verify_timeout"])
            except subprocess.TimeoutExpired:
                rc = 124
            finally:
                stop_group(child.pid, signal.SIGKILL)
                child.wait()
        with output.open("rb") as logs:
            logs.seek(max(0, output.stat().st_size - 4000))
            tail = logs.read().decode(errors="replace")
        return {"status": "passed" if rc == 0 else "failed", "exit": rc, "command": task["verify"], "tail": tail}


def review(store, name, action, revision, note):
    with lock(store.root / "locks" / ("task-" + name), blocking=False):
        task = store.get("tasks", name)
        if task["state"] == "approved" and action == "approve":
            if revision in (task.get("approved"), task.get("report")):
                return task
            raise DelegateError("任务已审核另一个版本")
        if task["state"] not in {"review", "approved", "needs_report"}:
            raise DelegateError("任务没有可以审核的结果")
        report = review_report(store, task, revision)
        if action == "request-changes":
            if task.get("export_intent"):
                raise DelegateError("请先用原路径重试未结算的图像导出")
            if not note:
                raise DelegateError("退回修改需要 --note")
            with store.transaction():
                task.update(state="changes_requested", approved=None, continue_note=note)
                store.put("tasks", task)
                update_descendants(store, name)
                store.event(name, "review_changes_requested", revision=revision)
            return task
        if task["state"] == "needs_report":
            raise DelegateError("请先续跑并补齐有效的结构化报告")
        if not report["verification"]:
            report["verification"] = verify_report(store, task, report)
            verified_revision = digest(report)
            base_dir = store.task_dir(name)
            patch = (base_dir / "checkpoints" / f"{revision}.patch").read_bytes()
            write(base_dir / "checkpoints" / f"{verified_revision}.patch", patch)
            write_json(base_dir / "reports" / f"{verified_revision}.json", {**report, "revision": verified_revision})
            previous_summary = base_dir / "reports" / f"{revision}.md"
            if previous_summary.is_file():
                content = previous_summary.read_text(encoding="utf-8")
                verification = json.dumps(report["verification"], ensure_ascii=False, indent=2)
                write(base_dir / "reports" / f"{verified_revision}.md",
                      (content + "\n## 控制器验证\n\n" + verification + "\n").encode())
        else:
            verified_revision = revision
        with store.transaction():
            current = store.get("tasks", name)
            if current["state"] != task["state"] or current["report"] != task["report"]:
                raise DelegateError("审核期间任务状态已变化")
            dependencies_now = list(dependencies(store, task).items())
            if dependencies_now != [tuple(pair) for pair in report["dependencies"]]:
                raise DelegateError("依赖版本已变化")
            if report["verification"]["status"] == "failed":
                task["review_candidate"] = verified_revision
                store.event(name, "verification_failed", revision=verified_revision)
            else:
                task.update(state="approved", approved=verified_revision, review_candidate=verified_revision)
                store.event(name, "review_approved", revision=verified_revision)
            store.put("tasks", task)
    return task


def apply_task(store, name, revision, mode):
    task = store.get("tasks", name)
    if task["kind"] == "image":
        raise DelegateError("图像任务使用 task export 交付产物")
    with workspace.project_lock(store, task), lock(store.root / "locks" / ("task-" + name), blocking=False):
        task = store.get("tasks", name)
        directory = store.task_dir(name)
        journal_path = directory / "apply.json"
        if journal_path.exists():
            journal = read(journal_path)
            if journal["status"] == "rolled_back":
                workspace.cleanup_application(journal)
            if journal["status"] == "applied":
                if mode != "recover" and revision != journal["approved"]:
                    raise DelegateError("任务已应用另一个版本")
                revision = journal["approved"]
                with store.transaction():
                    if task["state"] != "applied":
                        task.update(state="applied", applied=revision)
                        store.put("tasks", task)
                        store.event(name, "application_reconciled", revision=revision)
                retained = workspace.read_scratch(journal)
                return {"state": "applied", "revision": revision, "retained": str(retained) if retained else None}
            if journal["status"] in {"applying", "conflict"}:
                if mode != "recover":
                    raise DelegateError("存在未结算应用，请先运行 task apply --recover")
                conflicts = workspace.recover_application(workspace.origin(task), journal, journal_path)
                with store.transaction():
                    task["state"] = "applying" if conflicts else "approved"
                    store.put("tasks", task)
                    store.event(name, "application_recovered", conflicts=conflicts)
                return {"state": task["state"], "conflicts": conflicts,
                        "retained": journal.get("scratch") if conflicts else None}
        if task["state"] == "applying":
            with store.transaction():
                task["state"] = "approved"
                store.put("tasks", task)
        if mode == "recover":
            return {"state": task["state"], "conflicts": []}
        if task["state"] != "approved" or task["approved"] != revision:
            raise DelegateError("只有已审核的准确版本可应用")
        report = review_report(store, task, revision)
        with store.transaction():
            if any(store.get("tasks", dep)["state"] != "applied" for dep, _ in report["dependencies"]):
                raise DelegateError("请先按顺序应用所有依赖任务")
            if list(dependencies(store, task).items()) != [tuple(pair) for pair in report["dependencies"]]:
                raise DelegateError("依赖版本已变化")
        attempt = store.get("attempts", report["attempt"])
        rows, conflicts = workspace.plan_application(task, attempt, report["paths"])
        root = workspace.origin(task)
        staging_parent = None
        if rows and not conflicts:
            try:
                staging_parent = workspace.application_scratch_parent(root, rows, journal_path)
            except (OSError, DelegateError) as error:
                conflicts.append({"path": str(root), "reason": str(error)})
        preview = {"state": "blocked" if conflicts else "ready", "revision": revision, "conflicts": conflicts,
                   "staging_parent": str(staging_parent) if staging_parent else None,
                   "changes": [{"path": row["path"], "before": workspace.summarize(row["before"]),
                                "after": workspace.summarize(row["after"])} for row in rows]}
        if mode == "check":
            return preview
        if conflicts:
            raise DelegateError("原目录有冲突，请先运行 task apply --check")
        with store.transaction():
            if store.get("tasks", name)["state"] != "approved":
                raise DelegateError("审核状态已变化")
            task["state"] = "applying"
            store.put("tasks", task)
        try:
            root = workspace.origin(task)
            retained = workspace.apply_application(root, rows, journal_path, revision)
        except Exception:
            if not journal_path.exists() or read(journal_path)["status"] == "rolled_back":
                with store.transaction():
                    task["state"] = "approved"
                    store.put("tasks", task)
            raise
        with store.transaction():
            task.update(state="applied", applied=revision)
            store.put("tasks", task)
            store.event(name, "applied", revision=revision, paths=report["paths"])
        return {**preview, "state": "applied", "retained": str(retained) if retained else None}


def cleanup(store, name, confirmed):
    with lock(store.root / "locks" / ("task-" + name), blocking=False):
        task = store.get("tasks", name)
        refs = [other["id"] for other in store.rows("tasks") if other["state"] not in FINISHED
                and name in other["dependencies"]]
        all_attempts = store.rows("attempts")
        attempts = [a for a in all_attempts if a["task"] == name]
        paths = [store.attempt_dir(a) for a in attempts]
        paths.extend(store.task_dir(name) / part for part in ("baseline.git", "integration", "references"))
        if task["kind"] == "image":
            for attempt in attempts:
                session = attempt.get("session")
                if session and not any(a["task"] != name and a["instance"] == attempt["instance"]
                                       and a.get("session") == session for a in all_attempts):
                    try:
                        identifier(session)
                    except DelegateError:
                        continue
                    clip = store.instance_dir(attempt["instance"]) / "cli" / "generated_images"
                    disposable = clip / session
                    if not clip.is_symlink() and disposable.resolve().is_relative_to(clip):
                        paths.append(disposable)
        paths = list(dict.fromkeys(path for path in paths if path.is_dir() and not path.is_symlink()))
        journal_path = store.task_dir(name) / "apply.json"
        journal = read(journal_path) if journal_path.is_file() and task["state"] == "applied" else {}
        application_staging = None
        conflicts = []
        try:
            application_staging = workspace.read_scratch(journal)
            if application_staging:
                workspace.cleanup_application(journal, inspect_only=True)
        except (OSError, DelegateError) as error:
            conflicts.append(str(error))
        plan = {"task": name, "paths": [str(path) for path in paths] + ([str(application_staging)] if application_staging else []),
                "application_staging": str(application_staging) if application_staging else None, "conflicts": conflicts,
                "dependencies": refs, "eligible": task["state"] in FINISHED and not refs
                and not any(a["state"] in ACTIVE for a in attempts) and not conflicts}
        if confirmed:
            if not plan["eligible"]:
                if conflicts:
                    raise DelegateError("；".join(conflicts))
                raise DelegateError("任务尚未结束或仍被依赖")
            if application_staging:
                workspace.cleanup_application(journal)
            for directory in paths:
                shutil.rmtree(directory)
            store.event(name, "cleaned_up")
        return plan


def export_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino] if stat.S_ISREG(info.st_mode) else None


def export_image(store, name, revision, output):
    with lock(store.root / "locks" / ("task-" + name), blocking=False):
        task = store.get("tasks", name)
        if task["kind"] != "image" or task["state"] not in {"approved", "applied"} or task["approved"] != revision:
            raise DelegateError("请选择已审核的图像报告")
        report = review_report(store, task, revision)
        asset, = report["artifacts"]
        source = Path(asset["path"])
        if artifact(source) != asset:
            raise DelegateError("图像产物校验失败")
        output = output.expanduser()
        output = output.parent.resolve() / output.name
        if output.suffix.lower() != source.suffix.lower():
            raise DelegateError(f"输出扩展名须为 {source.suffix}")
        goal = {"path": str(output), "sha256": asset["sha256"]}
        intent = task.get("export_intent")
        if intent and (intent["goal"], intent["revision"]) != (goal, revision):
            raise DelegateError("请先用原路径和版本重试未结算的导出")
        if intent:
            staged = Path(intent["temporary"])
            if export_identity(staged) == intent["identity"]:
                staged.unlink()
        current = export_identity(output)
        if current or os.path.lexists(output):
            claimed = current and (current == intent["identity"] if intent else
                                   task["state"] == "applied" and task.get("export") == str(output))
            if not claimed or not export_matches(output, current, asset):
                if intent:
                    with store.transaction():
                        task.pop("export_intent")
                        store.put("tasks", task)
                        store.event(name, "image_export_conflict", path=str(output))
                raise DelegateError("已有输出文件，请选择新路径")
            if not intent:
                return goal
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".cli-manager-export-", dir=output.parent)
            staged = Path(temporary)
            try:
                with os.fdopen(fd, "wb") as stream, source.open("rb") as src:
                    shutil.copyfileobj(src, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                proof = export_identity(staged)
                with store.transaction():
                    task["export_intent"] = {"goal": goal, "revision": revision,
                                             "identity": proof, "temporary": str(staged)}
                    store.put("tasks", task)
                os.link(staged, output, follow_symlinks=False)
            finally:
                staged.unlink(missing_ok=True)
            if not export_matches(output, proof, asset):
                raise DelegateError("图像导出后校验失败")
        with store.transaction():
            task.pop("export_intent", None)
            task.update(state="applied", applied=revision, export=str(output))
            store.put("tasks", task)
            store.event(name, "image_exported", sha256=asset["sha256"])
        return goal


def export_matches(path, proof, asset):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            return (stat.S_ISREG(info.st_mode) and [info.st_dev, info.st_ino] == proof
                    and info.st_size == asset["bytes"]
                    and hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"]
                    and export_identity(path) == proof)
    except OSError:
        return False


def integrate(store, name, revision, *, record):
    """Resolve Git application conflicts in a private integration worktree."""
    original = store.get("tasks", name)
    with workspace.project_lock(store, original), lock(store.root / "locks" / ("task-" + name), blocking=False):
        task = store.get("tasks", name)
        if not task["baseline"]["git"] or task["state"] != "approved" or task["approved"] != revision:
            raise DelegateError("集成工作树要求已批准的 Git 任务版本")
        report = review_report(store, task, revision)
        source = store.get("attempts", report["attempt"])
        directory = store.task_dir(name) / "integration" / revision
        record_path = directory / "record.json"
        root = workspace.origin(task)
        if record_path.exists():
            candidate = read(record_path)
        else:
            if record:
                raise DelegateError("请先创建集成工作树")
            if directory.exists():
                shutil.rmtree(directory)
            repo, work = directory / "repo.git", directory / "work"
            workspace.init_repo(repo)
            workspace.import_commit(repo, Path(source["repo"]), source["base"])
            workspace.import_commit(repo, Path(source["repo"]), source["checkpoint"])
            branch = "cli-manager/integration"
            workspace.git(repo, "worktree", "add", "--quiet", "-b", branch, str(work), source["base"])
            staged = {relative: workspace.git(root, "ls-files", "--stage", "-z", "--", ":(literal)" + relative).hex()
                      for relative in report["paths"]}
            copied = workspace.copy_integration_paths(root, work, report["paths"])
            base = workspace.snapshot(repo, work, source["base"], include=copied)
            workspace.git(repo, "update-ref", "refs/heads/" + branch, base)
            workspace.git(work, "reset", "--mixed", "--quiet", base)
            patch = workspace.git(Path(source["repo"]), "diff", "--no-renames", "--binary",
                                  source["base"], source["checkpoint"])
            resolved = not patch or workspace.git(work, "apply", "--3way", "--binary", "-", inp=patch, required=False) is not None
            candidate = {"id": new_id("integration"), "task": name, "instance": source["instance"],
                         "base": base, "base_tree": workspace.text(repo, "rev-parse", base + "^{tree}"),
                         "model_selection": source.get("model_selection"),
                         "repo": str(repo), "work": str(work), "cwd": str(work / task["baseline"]["project_subdir"]),
                         "branch": branch, "start": time.time(), "revisions": source["revisions"],
                         "integration_stage": staged, "integration_resolved": resolved}
            write_json(record_path, candidate)
            write(directory / "approved.patch", patch)
            store.event(name, "integration_prepared", revision=revision, conflict=not resolved)
        if not record:
            return {"work": candidate["work"], "patch": str(directory / "approved.patch"),
                    "resolved_automatically": candidate["integration_resolved"]}
        if workspace.git(Path(candidate["work"]), "ls-files", "--unmerged", "-z"):
            raise DelegateError("集成工作树仍有未解决冲突，请解决并暂存冲突文件")
        commit = workspace.snapshot(Path(candidate["repo"]), Path(candidate["work"]), candidate["base"])
        if not set(workspace.changes(candidate, commit)).issubset(report["paths"]):
            raise DelegateError("集成结果修改了原批准版本之外的路径")
        with store.transaction():
            task["approved"] = None
            task["attempt"] = candidate["id"]
            task["state"] = "review"
            store.put("tasks", task)
            update_descendants(store, name)
            finish(store, task, candidate, {"outcome": "ok", "structured": report["reported"],
                                            "model": report["model"]})
        return store.get("tasks", name)
