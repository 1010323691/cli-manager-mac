"""Native CLI protocols with an instance-scoped environment."""
from __future__ import annotations

from contextlib import ExitStack
import stat
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import access
from .store import DelegateError, write_json

SKILL = Path(__file__).resolve().parents[2]
CLI_ENV = ("PATH", "LANG", "LANGUAGE", "LC_CTYPE", "LC_ALL", "TZ", "TERM")
REPORT_KEYS = ("completed", "changes", "tests", "unfinished", "risks", "recommendations")
REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(REPORT_KEYS),
    "properties": {key: {"type": "array", "items": {"type": "string"}} for key in REPORT_KEYS},
}
WORKER_RULES = (
    "Use only the isolated task workspace. The controller handles credentials, Git metadata, "
    "review, and application. Never publish, push, send external messages, change permissions, "
    "read credentials, or attempt a denied operation. Run only checks needed for the task. "
    "Return the requested structured report. Identify unfinished work and tests you actually ran."
)
OPEN_RULES = (
    "You work in an isolated task workspace; the controller handles credentials, Git metadata, review "
    "and application. Commands run in a sandbox that can write the workspace and the granted write "
    "directories, read the granted read-only directories, and use the network as configured below. "
    "When a step needs more (a headless browser, a local server the sandbox blocks, a package install, "
    "a path outside the grants), request escalated permissions for that exact command with a short "
    "justification: the planning session reviews each request. Desktop applications and headless "
    "browsers (Edge, Chrome, Playwright, Puppeteer) cannot start inside the sandbox and crash "
    "visibly on the user's screen when tried: request escalation for such a command before its "
    "first run instead of trying it sandboxed. Do not retry a request that was "
    "denied, do not rephrase it to get around the denial, and report what you could not do. Never "
    "read or write credentials (SSH keys, keychains, CLI auth files, other instances' directories, "
    "the manager state). Pushing, publishing, sending external messages and deleting files outside "
    "the workspace always need an explicit approval. Ignored content of the original project "
    "(source images, textures, node_modules) is already present in the workspace as copy-on-write "
    "clones; it never becomes part of your result, so do not move it into "
    "tracked paths. Run only checks the "
    "task needs. Return the requested structured report, listing unfinished work and the tests you ran."
)
CODEX_AUTH = 'cli_auth_credentials_store="file"'
IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".webp"}
# Bumped when the meaning of probe status changes (2: Codex account keys include the workspace).
# 3: Codex status lists the capability tiers the CLI supports.
PROBE_SCHEMA = 3


def read_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                yield data


def account_key(provider: str, name: str, workspace: str | None = None):
    # One login (email) can belong to several ChatGPT workspaces (personal and Business),
    # each with its own quota, so the workspace is part of the account identity.
    text = f"{provider}:{name.strip().casefold()}"
    if workspace:
        text += f"@{workspace.strip()}"
    return hashlib.sha256(text.encode()).hexdigest()


class AccountMismatch(DelegateError):
    """The executing session's account differs from the one the attempt was dispatched to."""


def codex_identity(reply):
    """(account key, email-only key or None, plan) from a Codex `account/read` reply.

    Any malformed reply (wrong types anywhere) reads as not logged in.
    """
    account = reply.get("account") if isinstance(reply, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return None, None, None
    name = next((value.strip() for value in (account.get("email"), account.get("userId"))
                 if isinstance(value, str) and value.strip()), None)
    if not name:
        return None, None, None
    plan = account.get("planType") if isinstance(account.get("planType"), str) else None
    routing = reply.get("workspaceRouting")
    workspace = routing.get("chatgptAccountId") if isinstance(routing, dict) else None
    # Only a non-empty string names a workspace; anything else counts as no workspace.
    workspace = workspace.strip() if isinstance(workspace, str) and workspace.strip() else None
    # Without a workspace the key is email-only; such a binding is never schedulable for Codex.
    legacy = account_key("codex", name) if workspace else None
    return account_key("codex", name, workspace), legacy, plan


def invalidate_probe(store, name, generation):
    """Invalidate an instance's probe cache at once, where an account mismatch is detected.

    Committed before the detecting server is closed and before any cleanup or result capture,
    so nothing can leave the stale identity schedulable; refresh_instance discards probes that
    started before it (invalidations counter).
    """
    with store.transaction():
        instance = store.get("instances", name, required=False)
        if instance and instance["generation"] == generation:
            instance["probed_at"] = 0
            instance["invalidations"] = instance.get("invalidations", 0) + 1
            store.put("instances", instance)


def check_codex_account(store, instance, app, expected, generation):
    """On a running App Server: raise AccountMismatch, after invalidating, unless it names `expected`.

    A failed query counts as a mismatch. The caller closes the server afterwards.
    """
    try:
        current = codex_identity(app.request("account/read", {"refreshToken": False}, timeout=12))[0]
        problem = None if current == expected else "账户或工作区与派发时不一致"
    except Exception as exc:  # any failure to read a well-formed identity counts as unconfirmed
        problem = f"无法确认账户：{type(exc).__name__}"
    if problem:
        invalidate_probe(store, instance["id"], generation)
        raise AccountMismatch(problem)


def verify_codex_account(store, instance, expected, generation):
    """Only `account/read` on a fresh App Server (no version, model or feature queries)."""
    import tempfile
    base = store.instance_dir(instance["id"])
    try:
        with tempfile.TemporaryDirectory(dir=base) as temp:
            server = open_app(instance, base, environment(store, instance), Path(temp), [])
            try:
                check_codex_account(store, instance, server, expected, generation)
            except AccountMismatch:
                try:
                    server.close()
                except Exception:
                    pass  # the mismatch, already committed, is the result
                raise
            server.close()
    except AccountMismatch:
        raise
    except Exception as exc:
        invalidate_probe(store, instance["id"], generation)
        raise AccountMismatch(f"无法确认账户：{type(exc).__name__}") from exc


def environment(store, instance, attempt=None):
    base = store.instance_dir(instance["id"])
    home = base / "home"
    tmp = store.attempt_dir(attempt) / "tmp" if attempt else base / "tmp"
    cache = base / "cache"
    config = base / "cli"
    for path in (home, tmp, cache, config):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in CLI_ENV if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(tmp), XDG_CACHE_HOME=str(cache),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0",
               CLI_DELEGATE_DEPTH="1")
    env["CLAUDE_CONFIG_DIR" if instance["provider"] == "claude" else "CODEX_HOME"] = str(config)
    return env


def worker_environment(store, instance, task, attempt):
    """Environment of one attempt's CLI and of the commands it runs.

    Open-tier Codex workers get the user's real HOME: desktop software such as a headless browser
    needs the login keychain and `~/Library`, and aborts or prompts under an empty home. The CLI's
    own login stays in the instance's CODEX_HOME, Git still ignores the user's global config, and
    caches still go to the instance.
    """
    env = environment(store, instance, attempt)
    if instance["provider"] == "codex" and task["kind"] != "image" and access.is_open(access.of(task)):
        cache = store.instance_dir(instance["id"]) / "cache"
        env.update(HOME=str(access.home()), npm_config_cache=str(cache / "npm"),
                   PIP_CACHE_DIR=str(cache / "pip"), UV_CACHE_DIR=str(cache / "uv"))
    return env


def binary(instance):
    path = instance.get("binary") or shutil.which(instance["provider"])
    if not path or not Path(path).is_file() or not os.access(path, os.X_OK):
        raise DelegateError(f"找不到可执行的 {instance['provider']} CLI")
    return str(Path(path).resolve())


def invoke(instance, args, env, cwd, *, timeout=15):
    return subprocess.run([binary(instance), *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def auth_command(store, instance, operation):
    bin_path = binary(instance)
    provider = instance["provider"]
    if provider == "claude":
        args = ["auth", operation, *(["--claudeai"] if operation == "login" else [])]
    else:
        args = ["login" if operation == "login" else "logout", "-c", CODEX_AUTH]
    return subprocess.run([bin_path, *args], cwd=store.instance_dir(instance["id"]),
                          env=environment(store, instance)).returncode


def app_class():
    path = SKILL / "backends/codex/codex_delegate.py"
    spec = importlib.util.spec_from_file_location("delegate_v2_codex_protocol", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AppServer


def open_app(instance, cwd, env, logs, options, request=None, notify=None, *, tracked=False):
    server = app_class()([binary(instance), "app-server", "-c", CODEX_AUTH,
                          "-c", "analytics.enabled=false", *options], str(cwd), env, logs,
                         request or (lambda *_: None), notify or (lambda *_: None),
                         start_new_session=not tracked, inherit_directory_lock=False)
    try:
        server.request("initialize", {"clientInfo": {"name": "cli-manager", "version": "2"},
                                      "capabilities": {"experimentalApi": True}}, timeout=12)
        server.notify("initialized")
        return server
    except BaseException:
        server.close()
        raise


def codex_windows(data):
    buckets = data.get("rateLimitsByLimitId") or {}
    default = data.get("rateLimits") or {}
    if default:
        buckets = {default.get("limitId") or "codex": default, **buckets}
    windows = []
    for key, bucket in buckets.items():
        for kind in ("primary", "secondary"):
            window = bucket.get(kind) or {}
            percent = window.get("usedPercent")
            if isinstance(percent, (float, int)) and not isinstance(percent, bool) and 0 <= percent <= 100:
                windows.append({"bucket": key, "window": kind, "used": percent,
                                "resets_at": window.get("resetsAt"), "minutes": window.get("windowDurationMins")})
    return windows


def quota(windows, source, **fields):
    return {"quality": "observed" if windows else "unknown", "observed_at": time.time() if windows else None,
            "checked_at": time.time(), "source": source, "windows": windows, **fields}


def codex_quota(result, source):
    allowed = result.get("ordinaryUsageAllowed")
    return quota(codex_windows(result), source, explicitly_blocked=allowed is False if isinstance(allowed, bool) else None)


def probe(store, instance, *, query_quota=True):
    base = store.instance_dir(instance["id"])
    env = environment(store, instance)
    version_run = invoke(instance, ["--version"], env, base)
    version = version_run.stdout.strip().splitlines()[0] if version_run.stdout.strip() else ""
    if version_run.returncode:
        raise DelegateError("无法读取 CLI 版本")
    provider = instance["provider"]
    status = {"schema": PROBE_SCHEMA, "version": version, "logged_in": False, "account": None, "models": [],
              "image": False, "quota": quota([], "unavailable")}
    if provider == "claude":
        auth = invoke(instance, ["auth", "status", "--json"], env, base)
        data = json.loads(auth.stdout)
        if Path(data.get("configDirectory") or "/").resolve() != (base / "cli").resolve():
            raise DelegateError("Claude CLI 使用了其他配置目录")
        status["logged_in"] = bool(data.get("loggedIn") and data.get("authMethod") == "claude.ai")
        name = data.get("email") or data.get("accountUuid")
        if status["logged_in"] and name:
            status["account"] = account_key(provider, name)
        if status["logged_in"] and query_quota and version.split(" ", 1)[0] == "2.1.329":
            status["quota"] = claude_quota(instance, env, base)
        # Subscription/model availability is not exposed by a stable Claude CLI RPC.
        status["models"] = [{"id": name, "verified": False, "efforts": []}
                            for name in ("opus", "sonnet", "haiku", "default")]
    else:
        import tempfile
        with tempfile.TemporaryDirectory(dir=base) as temp:
            server = open_app(instance, base, env, Path(temp), [])
            try:
                reply = server.request("account/read", {"refreshToken": False}, timeout=12)
                key, legacy, plan = codex_identity(reply)
                status["logged_in"] = key is not None
                if status["logged_in"] and key:
                    status["account"] = key
                    if legacy:
                        # Recognises bindings made before the workspace joined the key (instance rebind).
                        status["legacy_account"] = legacy
                    status["plan"] = plan
                cursor = None
                visited = set()
                while True:
                    models = server.request("model/list", {"cursor": cursor} if cursor else {}, timeout=12)
                    status["models"].extend(
                        {"id": item.get("model") or item.get("id"), "default": item.get("isDefault", False),
                         "efforts": [entry["reasoningEffort"] for entry in item.get("supportedReasoningEfforts", [])
                                     if "reasoningEffort" in entry], "verified": True}
                        for item in models.get("data", [])
                    )
                    cursor = models.get("nextCursor")
                    if not cursor:
                        break
                    if cursor in visited:
                        raise DelegateError("Codex 模型枚举返回重复游标")
                    visited.add(cursor)
                if status["logged_in"] and query_quota:
                    try:
                        result = server.request("account/rateLimits/read", {}, timeout=12)
                        status["quota"] = codex_quota(result, "codex.account/rateLimits/read")
                    except RuntimeError:
                        status["quota"] = quota([], "codex.account/rateLimits/read")
            finally:
                server.close()
        features = invoke(instance, ["features", "list"], env, base)
        status["image"] = bool(re.search(r"^image_generation\s+\S+(?:\s+\S+)*\s+true\s*$",
                                         features.stdout, re.M))
        status["capabilities"] = codex_capabilities(version, features.stdout)
    return status


def codex_capabilities(version, features):
    """What this CLI can enforce natively; anything missing is refused at submission, never downgraded."""
    known = {match[1]: match[2] != "removed" for match in
             re.finditer(r"^(\S+)\s+(.*?)\s+(?:true|false)\s*$", features, re.M)}
    supported = bool(re.search(r"\b0\.171\.\d+\b", version))
    # Fixture and older listings may omit a row; only an explicit "removed" withdraws a feature.
    proxy = supported and known.get("network_proxy", True)
    image = supported and known.get("image_generation", True)
    available = {"access.sandbox": supported, "access.open": supported, "access.full": supported,
                 "network.on": supported, "network.off": supported, "network.loopback": proxy,
                 "network.allow_domain": proxy, "image": image, "approvals": supported,
                 "read_dirs": supported, "write_dirs": supported}
    return {"supported": sorted(key for key, value in available.items() if value),
            "missing": sorted(key for key, value in available.items() if not value)}


def required_capabilities(capabilities):
    needs = ["access." + capabilities["access"], "network." + capabilities["network"]]
    if capabilities.get("allow_domains"):
        needs.append("network.allow_domain")
    if capabilities.get("image"):
        needs.append("image")
    if access.is_open(capabilities):
        needs.append("approvals")
    for key in ("read_dirs", "write_dirs"):
        if capabilities.get(key):
            needs.append(key)
    return needs


def claude_quota(instance, env, cwd):
    result = invoke(instance, ["-p", "/usage", "--restricted", "--strict-mcp-config",
                               "--permission-prompts", "none", "--no-session-persistence",
                               "--output-format", "json"], env, cwd, timeout=12)
    try:
        body = json.loads(result.stdout)
    except ValueError:
        return quota([], "claude./usage")
    if result.returncode or body.get("is_error"):
        return quota([], "claude./usage")
    windows = [
        {"bucket": "claude", "window": label.strip(), "used": int(used),
         "resets_at": claude_reset(reset), "reset_text": reset or None}
        for label, used, reset in re.findall(
            r"^\s*Current ([^:\n]+):\s*(\d+)%\s*used(?:\s*·\s*(resets [^\n]+))?\s*$",
            str(body.get("result") or ""), re.I | re.M
        )
        if int(used) <= 100
    ]
    return quota(windows, "claude./usage")


def claude_reset(raw):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    # Parse only known English output that includes an explicit time zone.
    match = re.fullmatch(r"resets (?:(?P<month>[a-z]{3}) (?P<day>\d{1,2}),? (?:at )?)?"
                         r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?"
                         r"\s*\((?P<zone>[^)]+)\)", (raw or "").strip(), re.I)
    if not match:
        return None
    try:
        now = dt.datetime.now(ZoneInfo(match["zone"]))
        hour = int(match["hour"])
        if match["ampm"]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
        reset = now.replace(hour=hour, minute=int(match["minute"] or 0), second=0, microsecond=0)
        if match["month"]:
            months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
            reset = reset.replace(month=months.index(match["month"].lower()) + 1, day=int(match["day"]))
            if reset < now - dt.timedelta(days=180):
                reset = reset.replace(year=reset.year + 1)
        elif reset < now - dt.timedelta(minutes=1):
            reset += dt.timedelta(days=1)
        return int(reset.timestamp())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def codex_profile(work, repo, tmp, readonly, grants=None):
    """Named permission profile. Without `grants` this is the closed sandbox, byte for byte."""
    entries = {":minimal": "read", str(work): "read" if readonly else "write", str(tmp): "write"}
    if repo:
        entries[str(repo)] = "read"
    entries[str(work / ".git")] = "read"
    isolation = ["-c", "mcp_servers={}", "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false",
                 "-c", "features.apps=false", "-c", "features.plugins=false", "-c", "features.hooks=false"]
    if not grants:
        fields = ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in entries.items())
        return ["-c", 'default_permissions="delegate"',
                "-c", "permissions.delegate.filesystem={" + fields + "}",
                "-c", "permissions.delegate.network.enabled=false", *isolation, "-c", 'web_search="disabled"']
    capabilities = grants["capabilities"]
    for path in grants["read"]:
        entries.setdefault(path, "read")
    for path in grants["write"]:
        entries[path] = "read" if readonly else "write"
    # Deny wins over the grants above: credentials stay unreachable inside the sandbox in every tier.
    for path in grants["deny"]:
        if path not in entries:
            entries[path] = "deny"
    fields = ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in entries.items())
    network, domains = capabilities["network"], capabilities.get("allow_domains") or []
    options = ["-c", 'default_permissions="delegate"',
               "-c", "permissions.delegate.filesystem={" + fields + "}",
               "-c", f"permissions.delegate.network.enabled={'false' if network == 'off' else 'true'}"]
    if network == "loopback" or domains:
        # The managed proxy enforces the allowlist; local binding keeps 127.0.0.1 servers usable.
        allowed = ",".join(f'{json.dumps(domain)}="allow"' for domain in domains)
        options += ["-c", "features.network_proxy=true",
                    "-c", "permissions.delegate.network.allow_local_binding=true",
                    "-c", "permissions.delegate.network.domains={" + allowed + "}"]
    search = "live" if network == "on" and not domains else "disabled"
    return [*options, *isolation, "-c", f'web_search="{search}"',
            "-c", f"features.image_generation={'true' if capabilities.get('image') else 'false'}"]


def codex_grants(store, instance, task, attempt, capabilities):
    """Resolved directories for one attempt, or None for the closed sandbox."""
    if not access.is_open(capabilities):
        return None
    base = store.instance_dir(instance["id"])
    home, cache = base / "home", base / "cache"
    # Nothing of the original project is granted: its ignored content is cloned into the workspace.
    read = access.read_roots(store, task, capabilities, own=base)
    images = base / "cli" / "generated_images"
    images.mkdir(mode=0o700, parents=True, exist_ok=True)
    skills = base / "cli" / "skills"  # Codex's bundled skill instructions (e.g. image generation)
    if skills.is_dir():
        read.append(str(skills))
    read.append(str(images))
    write = [str(home), str(cache), *(capabilities.get("write_dirs") or [])]
    # No deny entries: Codex 0.171 keeps an escalated command inside the filesystem sandbox whenever
    # the profile denies a path, and a sandboxed desktop application (a headless browser) cannot
    # register with the window server. Credentials stay unreadable in the sandbox because no granted
    # directory contains one (access.read_roots, access.directories); for approved, unsandboxed
    # commands the gate screens the command line and the planning session reviews the request.
    deny = []
    return {"capabilities": capabilities, "read": read, "write": write, "deny": deny, "images": str(images)}



def validate_platform(instance, env, directory, options, work, readonly):
    version = invoke(instance, ["--version"], env, directory).stdout
    import sys
    if sys.platform != "darwin":
        raise DelegateError("当前版本仅开放已验收的 macOS 执行环境")
    if instance["provider"] == "codex":
        if not re.search(r"\b0\.171\.\d+\b", version):
            raise DelegateError("Codex 隔离适配目前已验证 0.171.x")
        # A harmless probe checks the same named OS sandbox before any model call.
        marker = directory / "probe-private"
        marker.write_text("private", encoding="utf-8")
        permitted = work / f".delegate-probe-{os.getpid()}"
        shell = 'test -r "$1" && exit 31; ( printf ok > "$2" ) 2>/dev/null'
        result = invoke(instance, ["sandbox", "-P", "delegate", *options, "--", "/bin/sh",
                                   "-c", shell, "probe", str(marker), str(permitted)],
                        env, work)
        marker.unlink(missing_ok=True)
        created = permitted.is_file()
        permitted.unlink(missing_ok=True)
        if (result.returncode != (1 if readonly else 0)) or created == readonly:
            raise DelegateError("Codex 文件沙箱检查失败")
    else:
        if not re.search(r"\b2\.1\.329\b", version):
            raise DelegateError("Claude 隔离适配目前已验证 2.1.329 的启动能力")
        help_text = invoke(instance, ["--help"], env, directory).stdout
        if any(flag not in help_text for flag in ("--restricted", "--strict-mcp-config", "--permission-prompts")):
            raise DelegateError("Claude CLI 缺少隔离参数")


def claude_settings(store, task, attempt, readonly):
    work, tmp = Path(attempt["work"]), store.attempt_dir(attempt) / "tmp"
    roots = [str(work), str(tmp), "/System", "/usr", "/bin", "/sbin", "/Library/Developer",
             "/opt", "/private/etc", "/dev"]
    if task["baseline"]["git"]:
        roots.append(attempt["repo"])
    permissions = {
        "deny": ["Bash(git push *)", "Bash(git commit *)", "Bash(git merge *)", "WebFetch", "WebSearch",
                 "Write(//" + str(work).lstrip("/") + "/.git)", "Edit(//" + str(work).lstrip("/") + "/.git)"],
        "disableBypassPermissionsMode": "disable",
        "disableAutoMode": "disable",
    }
    return {"disableAllHooks": True, "permissions": permissions,
            "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                        "autoAllowBashIfSandboxed": False, "excludedCommands": [],
                        "filesystem": {"denyRead": ["/"], "allowRead": roots,
                                       "allowWrite": [str(tmp)],
                                       "denyWrite": [attempt["repo"], str(work / ".git")]},
                        "network": {"allowedDomains": [], "allowAllUnixSockets": False}}}


def approval_wait(store, task, attempt, directory, display, cancel):
    import secrets
    request_id = secrets.token_hex(6)
    approvals = directory / "approvals"
    deadline = time.time() + task["approval_timeout"]
    request = {**display, "id": request_id, "expires_at": deadline}
    write_json(approvals / f"{request_id}.request.json", request)
    with StoreConnection(store) as same:
        same.event(task["id"], "approval_requested", attempt=attempt["id"], request=request)
    decision_file = approvals / f"{request_id}.decision.json"
    while not cancel.is_set() and time.time() < deadline:
        if decision_file.is_file():
            return json.loads(decision_file.read_text())
        cancel.wait(.25)
    expired = {"behavior": "deny", "by": "timeout", "message": "请求已过期或任务已停止"}
    if display.get("provider") == "codex" and not cancel.is_set() and not decision_file.exists():
        # An expired request is a denial the session must not retry. A request cut short because
        # the task stopped was never judged: a resumed session may ask again.
        write_json(decision_file, expired)
    return expired


class StoreConnection:
    """Separate SQLite connection for protocol callbacks."""
    def __init__(self, store):
        self.root = store.root

    def __enter__(self):
        from .store import Store
        self.store = Store(self.root)
        return self.store

    def __exit__(self, *_):
        self.store.db.close()


def collect_claude(store, instance, task, attempt, directory, env, prompt, cancel, session):
    readonly = task["kind"] == "review"
    validate_platform(instance, env, directory, [], Path(attempt["work"]), readonly)
    # The open tier is a Codex capability; Claude workers keep their closed sandbox.
    record_capabilities(store, task, attempt, {**access.SANDBOX, "image": False}, None)
    config_path = directory / "claude-settings.json"
    write_json(config_path, claude_settings(store, task, attempt, readonly))
    bridge = SKILL / "backends/claude/approval_mcp.py"
    mcp = {"mcpServers": {"cliapproval": {"command": os.sys.executable,
           "args": [str(bridge), str(directory)], "env": {"CLI_APPROVAL_TIMEOUT": str(task["approval_timeout"])}}}}
    tools = "Read,Glob,Grep,AskUserQuestion" if readonly else "Read,Glob,Grep,Write,Edit,MultiEdit,Bash,AskUserQuestion"
    args = [binary(instance), "-p", "--restricted", "--strict-mcp-config", "--settings", str(config_path),
            "--tools", tools, "--permission-mode", "manual", "--output-format", "stream-json", "--verbose",
            "--append-system-prompt", WORKER_RULES, "--json-schema", json.dumps(REPORT_SCHEMA),
            "--mcp-config", json.dumps(mcp), "--permission-prompt-tool", "mcp__cliapproval__permission_prompt"]
    if task.get("model") and task["model"] != "default":
        args += ["--model", task["model"]]
    if task.get("effort"):
        args += ["--effort", task["effort"]]
    if session:
        args += ["--resume", session]
    rc = run_process(args, Path(attempt["cwd"]), env, directory, prompt, cancel)
    result, initial = {}, {}
    for event in read_jsonl(directory / "events.jsonl"):
        if event.get("type") == "result":
            result = event
        if event.get("type") == "system" and event.get("subtype") == "init":
            initial = event
    state = "ok" if result and not result.get("is_error") and rc == 0 else "failed"
    if result.get("api_error") == "usage_limit_reached" or result.get("api_error_type") == "rate_limit_error":
        state = "quota"
    elif result.get("api_error_status") in (401, 403):
        state = "auth"
    return {"outcome": state, "structured": result.get("structured_output"),
            "session": result.get("session_id") or initial.get("session_id"),
            "model": initial.get("model"), "usage": result.get("usage"), "cost_usd": result.get("total_cost_usd"),
            "diagnostic": result.get("api_error") or result.get("subtype"), "exit": rc}


def run_process(args, cwd, env, directory, prompt, cancel, *, log="events.jsonl"):
    from .store import write
    source = directory / "sent-prompt.txt"
    write(source, prompt.encode())
    with source.open("rb") as inp, (directory / log).open("wb") as output, (directory / "stderr.log").open("ab") as error:
        child = subprocess.Popen(args, cwd=cwd, env=env, stdin=inp, stdout=output, stderr=error)
        try:
            while child.poll() is None:
                if cancel.wait(.2):
                    child.send_signal(signal.SIGINT)
                    try:
                        return child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        try:
                            return child.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            child.kill()
            return child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def fingerprint(kind, *parts):
    return hashlib.sha256(json.dumps([kind, *parts], sort_keys=True, default=str).encode()).hexdigest()


class CodexGate:
    """Routes a Codex worker's escalation requests: floor first, then the planning session.

    Credential access is refused outright. Irreversible actions always reach the planning session
    and can never be accepted for the whole session. In `full`, everything else is accepted
    without asking; in `open`, it waits for `task decide`.
    """
    METHODS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
               "item/permissions/requestApproval")

    def __init__(self, store, task, attempt, directory, cancel, grants, env):
        self.store, self.task, self.attempt = store, task, attempt
        self.directory, self.cancel, self.env = directory, cancel, env
        self.capabilities = grants["capabilities"]
        self.secrets = access.credential_roots(store, own=store.instance_dir(attempt["instance"]))
        # Deleting anything outside the task workspace is approved one request at a time, even
        # inside the temporary directory or a granted write directory.
        self.writable = [attempt["work"]]
        self.items, self.steer = {}, None
        self.denied = self.previous_denials()

    def previous_denials(self):
        """Denials of every run of this attempt: a resumed session inherits them."""
        denied = set()
        for request_file in self.directory.parent.glob("*/approvals/*.request.json"):
            decision_file = request_file.with_name(request_file.name.replace(".request.json", ".decision.json"))
            try:
                request = json.loads(request_file.read_text())
                decision = json.loads(decision_file.read_text())
            except (OSError, ValueError):
                continue
            if decision.get("behavior") == "deny" and request.get("fingerprint"):
                denied.add(request["fingerprint"])
        return denied

    def event(self, kind, **fields):
        with StoreConnection(self.store) as state:
            state.event(self.task["id"], kind, attempt=self.attempt["id"], **fields)

    def settle(self, request, behavior, by):
        """Record a request the controller answered itself, so reports list it like any other."""
        import secrets
        key = secrets.token_hex(6)
        approvals = self.directory / "approvals"
        # Decision first: a request file without one would read as pending.
        write_json(approvals / f"{key}.decision.json", {"behavior": behavior, "by": by})
        write_json(approvals / f"{key}.request.json", {**request, "id": key})

    def remember(self, item):
        if item.get("id"):
            self.items[item["id"]] = item

    def describe(self, method, payload):
        cwd = payload.get("cwd") or self.attempt.get("cwd")
        if method == "item/commandExecution/requestApproval":
            command = payload.get("command") or ""
            floor, paths = access.classify_command(command, cwd, secrets=self.secrets, writable=self.writable,
                                                   env_home=self.env.get("HOME"))
            network = payload.get("networkApprovalContext")
            extra = payload.get("additionalPermissions")
            extra_paths = access.permission_paths(extra)
            if any(access.grants_secret(path, self.secrets) for path in extra_paths):
                floor = "credential"
            targets = [f"{network.get('protocol')}://{network.get('host')}"] if network else []
            return ("command", "Command", {"command": command, "cwd": cwd, "reason": payload.get("reason"),
                                           "paths": [*paths, *extra_paths], "network": targets,
                                           "additional_permissions": extra}, floor, (command, cwd, targets))
        if method == "item/fileChange/requestApproval":
            item = self.items.get(payload.get("itemId")) or {}
            changes = [{"path": change.get("path"), "kind": (change.get("kind") or {}).get("type"),
                        "diff": (change.get("diff") or "")[:4000]} for change in item.get("changes") or []]
            if payload.get("grantRoot"):
                changes.append({"path": payload["grantRoot"], "kind": "grant_root", "diff": ""})
            floor, paths = access.classify_changes(changes, secrets=self.secrets, writable=self.writable)
            if payload.get("grantRoot") and access.grants_secret(payload["grantRoot"], self.secrets):
                floor = "credential"
            return ("file_change", "FileChange", {"changes": changes, "cwd": cwd, "reason": payload.get("reason"),
                                                  "paths": paths, "network": []}, floor,
                    ([(c["path"], c["kind"], c["diff"]) for c in changes],))
        permissions = payload.get("permissions") or {}
        paths = access.permission_paths(permissions)
        # A directory grant that holds a credential location (the home directory, `/`) is refused too.
        floor = "credential" if any(access.grants_secret(path, self.secrets) for path in paths) else None
        network = ["*"] if (permissions.get("network") or {}).get("enabled") else []
        return ("permissions", "Permissions", {"permissions": permissions, "cwd": cwd,
                                               "reason": payload.get("reason"), "paths": paths,
                                               "network": network}, floor, (permissions, cwd))

    def reply(self, method, payload, allowed, always=False):
        if method == "item/permissions/requestApproval":
            return {"permissions": (payload.get("permissions") or {}) if allowed else {},
                    "scope": "session" if allowed and always else "turn"}
        return {"decision": ("acceptForSession" if always else "accept") if allowed else "decline"}

    def refuse(self, method, payload, what, message):
        if self.steer and message:
            try:
                self.steer(f"[主会话审批] 已拒绝{what}。说明：{message} 不要原样重试这一请求。")
            except Exception:
                pass
        return self.reply(method, payload, False)

    def handle(self, method, payload):
        kind, tool, display, floor, identity = self.describe(method, payload)
        mark = fingerprint(kind, *identity)
        audit = {"request_kind": kind, "floor": floor, "command": display.get("command"),
                 "cwd": display.get("cwd"), "paths": display.get("paths"), "network": display.get("network")}
        request = {"provider": "codex", "kind": kind, "tool_name": tool, "input": display, "floor": floor,
                   "session_allow": floor is None, "fingerprint": mark,
                   "access": self.capabilities["access"]}
        if floor == "credential":
            self.denied.add(mark)
            self.settle(request, "deny", "floor")
            self.event("approval_denied_by_floor", **audit)
            return self.refuse(method, payload, "凭据访问", "安全底线禁止读写凭据路径。")
        if mark in self.denied:
            self.settle(request, "deny", "repeat")
            self.event("approval_repeat_denied", **audit)
            return self.refuse(method, payload, "重复请求", "同一请求在本会话已被拒绝。")
        if self.capabilities["access"] == "full" and floor is None:
            self.settle(request, "allow", "auto")
            self.event("approval_auto_allowed", **audit)
            return self.reply(method, payload, True)
        decision = approval_wait(self.store, self.task, self.attempt, self.directory, request, self.cancel)
        if decision.get("behavior") == "allow":
            return self.reply(method, payload, True, bool(decision.get("always")) and floor is None)
        self.denied.add(mark)
        return self.refuse(method, payload, {"command": "命令", "file_change": "文件修改"}.get(kind, "额外权限"),
                           decision.get("message") or "主会话拒绝此请求")


def merge_codex_quota_update(previous, payload):
    sample = codex_quota(payload, "codex.account/rateLimits/updated")
    windows = {(w["bucket"], w["window"]): w for w in previous.get("windows", [])}
    for window in sample["windows"]:
        windows[window["bucket"], window["window"]] = {**window, "observed_at": sample["observed_at"]}
    if windows:
        sample.update(quality="observed", windows=list(windows.values()),
                      observed_at=max(w["observed_at"] for w in windows.values()))
    if isinstance(sample["explicitly_blocked"], bool):
        sample["blocked_checked_at"] = sample["checked_at"]
    elif isinstance(previous.get("explicitly_blocked"), bool):
        sample["explicitly_blocked"] = previous["explicitly_blocked"]
        sample["blocked_checked_at"] = previous["blocked_checked_at"]
    return sample


def copy_rollout(source, target, session):
    """Copy a single dated rollout, anchoring every lookup to an open directory."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        raise ValueError("无效的线程 ID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        def directory(path, parent=None):
            fd = os.open(path, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        src = directory(source)
        dst = directory(target)
        matches = []
        for root, dirs, files, fd in os.fwalk("sessions", dir_fd=src, follow_symlinks=False):
            parts = Path(root).parts
            depth = len(parts)
            pattern = r"[0-9]{4}" if depth == 1 else r"[0-9]{2}"
            dirs[:] = [d for d in dirs if depth < 4 and re.fullmatch(pattern, d)]
            if depth == 4:
                matches.extend((parts, name) for name in files if name.startswith("rollout-")
                               and name.endswith("-" + session + ".jsonl"))
        if len(matches) != 1:
            raise ValueError("没有找到唯一匹配的 rollout")
        parts, name = matches[0]
        for part in parts:
            src = directory(part, src)
            try:
                os.mkdir(part, 0o700, dir_fd=dst)
            except FileExistsError:
                pass
            dst = directory(part, dst)
        with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src), "rb") as reader:
            metadata = os.fstat(reader.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("rollout 必须是普通文件")
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst)
            try:
                with os.fdopen(output, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
            except BaseException:
                os.unlink(name, dir_fd=dst)
                raise


def migration_quota_error(message):
    return bool(re.search(r"usage.?limit|rate.?limit|quota|too many requests|\b429\b", message, re.I))


def migration_error(error, session, before_tools, measurement):
    message = str(error)
    quota = migration_quota_error(message)
    return {"outcome": "quota" if quota else "failed", "migration_retry": before_tools and not quota,
            "diagnostic": message, "session": session, "quota": measurement}


def collect_codex(store, instance, task, attempt, directory, env, prompt, cancel, session, *, migrating=False):
    image_task = task["kind"] == "image"
    readonly = image_task or task["kind"] == "review"
    work = Path(attempt["work"])
    repo = Path(attempt["repo"]) if not image_task and task["baseline"]["git"] else None
    capabilities = access.of(task)
    # Image tasks keep their own closed, read-only directory: generation happens on the service.
    grants = None if image_task else codex_grants(store, instance, task, attempt, capabilities)
    profile = codex_profile(work, repo, Path(env["TMPDIR"]), readonly, grants)
    validate_platform(instance, env, directory, profile, work, readonly)
    gate = CodexGate(store, task, attempt, directory, cancel, grants, env) if grants else None
    record_capabilities(store, task, attempt, capabilities, grants)
    image_items = []
    events, completed = {}, threading.Event()

    def notify(method, payload):
        if method in ("item/started", "item/completed") and (payload.get("item") or {}).get("type") not in (
                "userMessage", "agentMessage", "reasoning", "plan", "contextCompaction",
                "enteredReviewMode", "exitedReviewMode"):
            events["tool"] = True
        if gate and method in ("item/started", "item/completed"):
            gate.remember(payload.get("item") or {})
        if method == "item/completed":
            item = payload.get("item") or {}
            if (image_task or gate) and item.get("type") == "imageGeneration":
                image_items.append({key: item.get(key) for key in ("status", "failure", "savedPath")})
            if item.get("type") == "agentMessage" and item.get("text"):
                events["text"] = item["text"]
        elif method == "turn/completed":
            events["turn"] = payload.get("turn") or {}
            completed.set()
        elif method == "thread/tokenUsage/updated":
            tokens = payload.get("tokenUsage") or {}
            usage = (tokens.get("total") if image_task else None) or tokens.get("last")
            if image_task and isinstance(usage, dict):
                fields = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
                          "outputTokens": "output_tokens"}
                usage = {target: usage[key] for key, target in fields.items() if key in usage}
            events["usage"] = usage
        elif method == "account/rateLimits/updated":
            events["quota"] = merge_codex_quota_update(events.get("quota", {}), payload)
        elif method == "error":
            events["error"] = payload.get("error") or {}
            if not payload.get("willRetry"):
                events["final_error"] = events["error"]

    def request(method, payload):
        if method.startswith(("item/", "mcpServer/")):
            events["tool"] = True
        if method == "item/tool/requestUserInput":
            questions = payload.get("questions") or []
            if image_task or any(question.get("isSecret") for question in questions):
                return {"answers": {}}
            decision = approval_wait(store, task, attempt, directory,
                                     {"kind": "question", "tool_name": "request_user_input",
                                      "input": {"questions": questions}}, cancel)
            answers = decision.get("answers") or {}
            return {"answers": {
                q["id"]: {"answers": value if isinstance(value, list) else [value]}
                for q in questions
                if (value := answers.get(q["id"], answers.get(q.get("question")))) is not None
            }}
        if gate and method in CodexGate.METHODS:
            return gate.handle(method, payload)
        if method in ("item/permissions/requestApproval", "item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval", "mcpServer/elicitation/request"):
            with StoreConnection(store) as state:
                state.event(task["id"], "approval_denied_by_policy", attempt=attempt["id"], method=method)
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "decline"}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None}
        return None

    app = open_app(instance, Path(attempt["cwd"]), env, directory,
                   [*profile, "-c", "features.default_mode_request_user_input=true"], request, notify, tracked=True)
    native_session, actual = session, None
    try:
        # The startup probe ran on another server; the CLI may have switched workspace since.
        check_codex_account(store, instance, app, attempt["account"], attempt["generation"])
        thread_options = {"cwd": attempt["cwd"], "approvalPolicy": "on-request", "approvalsReviewer": "user",
                          "developerInstructions": worker_rules(capabilities, grants, attempt)}
        if task.get("model"):
            thread_options["model"] = task["model"]
        if session:
            thread_options["threadId"] = session
        try:
            result = app.request("thread/resume" if session else "thread/start", thread_options, timeout=20)
        except RuntimeError as error:
            if not session or not migrating:
                raise
            app.close()
            return migration_error(error, session, True, events.get("quota"))
        validation_error = None
        if (result.get("activePermissionProfile") or {}).get("id") != "delegate":
            validation_error = "Codex App Server 未确认受管权限配置"
        elif Path(result.get("cwd") or "/").resolve() != Path(attempt["cwd"]).resolve():
            validation_error = "Codex App Server 返回了非任务工作目录"
        if validation_error:
            if migrating:
                app.close()
                return {"outcome": "failed", "migration_retry": True, "diagnostic": validation_error,
                        "quota": events.get("quota")}
            raise DelegateError(validation_error)
        native_session = result["thread"]["id"]
        if migrating and native_session != session:
            app.close()
            return {"outcome": "failed", "migration_retry": True, "diagnostic": "恢复请求返回了不同的线程 ID",
                    "quota": events.get("quota")}
        with store.transaction():
            current = store.get("attempts", attempt["id"])
            current["session"] = native_session
            attempt["session"] = native_session
            store.put("attempts", current)
        actual = result.get("model")
        options = {"threadId": native_session, "input": [{"type": "text", "text": prompt}]}
        if image_task:
            for name in task["references"]:
                attachment = work / name
                shutil.copyfile(store.task_dir(task["id"]) / "references" / name, attachment)
                options["input"].append({"type": "localImage", "path": str(attachment)})
        else:
            options["outputSchema"] = REPORT_SCHEMA
        if task.get("effort"):
            options["effort"] = task["effort"]
        try:
            turn = app.request("turn/start", options, timeout=20)["turn"]["id"]
            if gate:
                gate.steer = lambda text: app.request(
                    "turn/steer", {"threadId": native_session, "expectedTurnId": turn,
                                   "input": [{"type": "text", "text": text}]}, timeout=10)
        except RuntimeError as error:
            # An explicit RPC rejection precedes execution. A timeout is ambiguous.
            message = str(error)
            if not message.startswith("turn/start 失败:"):
                raise
            if migrating:
                app.close()
                return migration_error(error, native_session, not events.get("tool"), events.get("quota"))
            if not migration_quota_error(message):
                raise
            app.close()
            return {"outcome": "quota", "session": native_session, "model": actual,
                    "quota": events.get("quota"), "diagnostic": message}
        while not completed.wait(.2):
            if app.closed.is_set():
                break
            if cancel.is_set():
                try:
                    app.request("turn/interrupt", {"threadId": native_session, "turnId": turn}, timeout=5)
                    completed.wait(3)
                except RuntimeError:
                    pass
                break
        if image_task and completed.is_set() and not cancel.is_set():
            check_codex_account(store, instance, app, attempt["account"], attempt["generation"])
    except AccountMismatch:
        try:
            app.close()
        except Exception:
            pass  # the mismatch, already committed, is the result
        raise
    except BaseException:
        app.close()
        raise
    app.close()
    turn = events.get("turn") or {}
    # Retried errors can precede a successful turn on this connection; only an unfinished turn's
    # own error, or a terminal (non-retried) error notification, describes the outcome. The
    # connection may drop after a terminal error without ever reporting the turn's end state.
    error = (turn.get("error") or events.get("final_error") or {}) if turn.get("status") != "completed" else {}
    info = str(error.get("codexErrorInfo") or "")
    state = "ok" if turn.get("status") == "completed" else "failed"
    if "usageLimitExceeded" in info or "rateLimitExceeded" in info:
        state = "quota"
    elif state == "failed" and migration_quota_error(json.dumps(error)):
        state = "quota"
    elif "unauthorized" in info.lower():
        state = "auth"
    if image_task:
        result = {"outcome": state, "session": native_session, "model": actual,
                  "usage": events.get("usage"), "quota": events.get("quota"), "diagnostic": info or None}
        return image_result(result, image_items, directory, env, cancel, error)
    try:
        structured = json.loads(events.get("text") or "null")
    except ValueError:
        structured = None
    return {"outcome": state, "structured": structured, "session": native_session, "model": actual,
            "migration_retry": bool(turn.get("status") == "failed" and state != "quota" and not events.get("tool")),
            "usage": events.get("usage"), "quota": events.get("quota"), "diagnostic": info or error.get("message"),
            "generated": generated_images(image_items, env, native_session) if gate else []}


def worker_rules(capabilities, grants, attempt):
    if not grants:
        return WORKER_RULES
    network = {"on": "The network is available" + (
        f", limited to: {', '.join(capabilities['allow_domains'])} and loopback."
        if capabilities.get("allow_domains") else ", including loopback."),
        "loopback": "Only loopback (127.0.0.1 / localhost) networking is available.",
        "off": "The network is disabled."}[capabilities["network"]]
    real = access.home()
    lines = [OPEN_RULES, network,
             f"Workspace (writable): {attempt['work']}",
             "Extra writable directories: " + (", ".join(capabilities.get("write_dirs") or []) or "none"),
             "Read-only directories: " + ", ".join(grants["read"]),
             f"HOME is the user's real home ({real}): skills are at ~/.claude/skills and tool environments "
             "at ~/.cache, both read-only. Everything else under HOME is outside your grants."]
    if capabilities.get("image"):
        lines.append("Image generation is enabled. Generated files land under "
                     f"{grants['images']}/<thread>; copy the ones you keep into the workspace or "
                     "an extra writable directory and name them in your report.")
    return "\n".join(lines)


def record_capabilities(store, task, attempt, capabilities, grants):
    """Audit what this attempt actually runs with."""
    effective = {**access.summary(capabilities),
                 "read_roots": grants["read"] if grants else [],
                 "write_roots": [attempt["work"], *(grants["write"] if grants else [])],
                 "approvals": ("auto" if capabilities["access"] == "full" else "planner") if grants else "denied"}
    if task["kind"] == "image":
        effective.update(access="sandbox", network="off", read_roots=[], image=True,
                         note="生图任务使用独立只读目录")
    with store.transaction():
        row = store.get("attempts", attempt["id"])
        row["capabilities"] = effective
        store.put("attempts", row)
        store.event(task["id"], "capabilities_applied", attempt=attempt["id"], capabilities=effective)
    attempt["capabilities"] = effective


def generated_images(items, env, thread):
    """Completed built-in image generations of a code task, confined to this thread's folder."""
    if not thread or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", thread):
        return []
    root = Path(env["CODEX_HOME"]) / "generated_images" / thread
    found = []
    for item in items:
        saved = item.get("savedPath")
        if item.get("status") != "completed" or item.get("failure") is not None or not saved:
            continue
        path = Path(saved)
        if (path.is_absolute() and path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(root.resolve())
                and 0 < path.stat().st_size <= 64 * 1024 * 1024 and image_format(path)
                and str(path) not in found):
            found.append(str(path))
    return found


def image_format(path: Path):
    with path.open("rb") as stream:
        return image_format_bytes(stream.read(16))


def image_format_bytes(data: bytes):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def collect_image(store, instance, task, attempt, directory, env, prompt, cancel):
    instruction = ("Call your built-in image generation tool once. Generate exactly one image. "
                   "Do not run shell commands or write files manually. Use attachments as references.\n\n")
    return collect_codex(store, instance, task, attempt, directory, env, instruction + prompt, cancel, None)


def image_result(result, items, directory, env, cancel, error):
    failures = [item["failure"] or item["status"] for item in items
                if item["failure"] is not None or item["status"] != "completed"]
    if migration_quota_error(json.dumps([error, failures])):
        result["outcome"] = "quota"
    if result["outcome"] != "ok":
        return result
    result.update(outcome="failed", diagnostic="未得到唯一、可验证的图像产物")
    thread = result["session"]
    if failures or len(items) != 1 or cancel.is_set() or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", thread):
        return result
    root = Path(env["CODEX_HOME"]) / "generated_images" / thread
    paths = set(root.glob("*"))
    saved = items[0]["savedPath"]
    if saved is not None:
        path = Path(saved)
        if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
            return result
        paths.add(path)
    found = {path.resolve() for path in paths if path.suffix.lower() in IMAGE_FORMATS
             and path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
             and 0 < path.stat().st_size <= 64 * 1024 * 1024 and image_format(path)}
    if len(found) == 1 and (saved is None or Path(saved).resolve() in found):
        source = found.pop()
        name = "image" + image_format(source)
        shutil.copyfile(source, directory / name)
        result.update(outcome="ok", diagnostic=None, image=name,
                      structured={key: ["已生成图像"] if key == "completed" else [] for key in REPORT_KEYS})
    return result
