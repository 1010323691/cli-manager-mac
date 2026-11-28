"""Task capability tiers: what a Codex worker may reach, and the floor no tier removes."""
from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

from .store import DelegateError

TIERS = ("smart", "open", "sandbox", "full")
NETWORKS = ("on", "loopback", "off")
DEFAULT = {"access": "smart", "network": "on", "allow_domains": [], "read_dirs": [], "write_dirs": [],
           "image": True}
# Tasks recorded before capabilities existed keep the closed profile they were submitted with.
LEGACY = {"access": "sandbox", "network": "off", "allow_domains": [], "read_dirs": [], "write_dirs": [],
          "image": None, "legacy": True}
SANDBOX = {**DEFAULT, "access": "sandbox", "network": "off", "image": False}

# Names that hold credentials wherever they appear.
SECRET_NAMES = {".ssh", ".aws", ".kube", ".gnupg", ".netrc", ".npmrc", ".pypirc", "auth.json",
                ".credentials.json", ".git-credentials", "keychains", ".docker", ".env"}
SECRET_GLOBS = re.compile(r"(?:^\.env\..+|.+\.(?:pem|key|p12|pfx|keychain|keychain-db))$", re.I)
SECRET_COMMANDS = re.compile(
    r"(?:^|[\s;&|(`])(?:security\s+(?:find|dump|export|unlock|show)-[a-z-]+|ssh-add\b|"
    r"gh\s+auth\s+token|git\s+credential\b|codex\s+login\b|claude\s+(?:auth|setup-token)\b)", re.I)
# Publishing and other actions that cannot be taken back: always one approval per request.
IRREVERSIBLE = re.compile(
    r"(?:^|[\s;&|(`])(?:"
    # `push` and `remote` as the Git subcommand (after global options), not `stash push` or a message.
    r"git\s+(?:(?:-[cC]\s+\S+|--?[\w-]+(?:=\S+)?)\s+)*(?:push\b|remote\s+(?:add|set-url)\b)|"
    r"gh\s+(?:release|repo\s+(?:delete|create)|pr\s+(?:merge|create)|workflow\s+run|gist\s+create)\b|"
    r"(?:npm|pnpm|yarn|bun|cargo|gem|poetry|uv|flit)\s+(?:[^;&|]*?\s)?publish\b|"
    r"twine\s+upload\b|docker\s+(?:image\s+)?push\b|(?:vercel|netlify|wrangler|firebase|fly)\s+"
    r"(?:[^;&|]*?\s)?(?:deploy|publish)\b|rsync\b[^;&|]*\s--delete\b|"
    r"(?:scp|sftp|rsync)\s[^;&|]*\S+@\S+:|diskutil\s+(?:erase|partition)|mkfs\b|dd\s[^;&|]*\bof=)", re.I)
# No entry may contain a credential location (so not all of /Library, which holds the system keychain).
SYSTEM_ROOTS = ("/System", "/usr", "/bin", "/sbin", "/opt", "/Applications", "/private/etc/ssl",
                "/private/etc/hosts", "/private/etc/resolv.conf", "/private/etc/paths", "/private/etc/paths.d",
                "/private/etc/zshrc", "/private/etc/zprofile", "/private/etc/profile", "/private/etc/bashrc",
                "/Library/Developer", "/Library/Apple", "/Library/Frameworks", "/Library/Fonts", "/Library/Java",
                "/Library/Perl", "/Library/Ruby", "/Library/Python", "/Library/Internet Plug-Ins",
                "/private/var/db/timezone", "/private/var/select")
REMOVERS = {"rm", "rmdir", "unlink", "trash", "shred", "srm"}
RELOCATORS = {"mv"}
SHELL_BREAK = re.compile(r"\s*(?:&&|\|\||[;|&\n])\s*")


def home():
    """The user's real home, not the instance HOME the worker sees."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def credential_roots(store=None, own=None):
    """Paths no tier may read or write. `own` is the running instance's directory."""
    base = home()
    roots = [base / name for name in (
        ".ssh", ".aws", ".kube", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".docker",
        ".codex", ".claude.json", ".claude/.credentials.json", ".config/gh", ".config/gcloud",
        ".cargo/credentials.toml", ".cargo/credentials", ".gem/credentials", ".config/pip", ".yarnrc.yml",
        ".config/op", "Library/Keychains", "Library/Application Support/com.apple.TCC",
        "Library/Cookies")]
    roots += [Path("/Library/Keychains"), Path("/private/etc/sudoers"), Path("/private/etc/master.passwd")]
    if store is not None:
        root = Path(store.root)
        roots += [root / "state.sqlite3", root / "state.sqlite3-wal", root / "state.sqlite3-shm",
                  root / "locks", root / "tasks"]
        instances = root / "instances"
        if instances.is_dir():
            for entry in sorted(instances.iterdir()):
                if own is not None and entry.resolve() == Path(own).resolve():
                    # The worker's own CLI directory holds its login; generated images stay readable.
                    roots.append(entry / "cli" / "auth.json")
                    continue
                roots.append(entry)
    return roots


def secret_path(path, roots):
    """True when `path` is a credential location or lies inside (or contains) one."""
    try:
        resolved = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return True
    for root in roots:
        real = Path(os.path.realpath(root))
        if resolved == real or resolved.is_relative_to(real):
            return True
    return any(part.casefold() in SECRET_NAMES or SECRET_GLOBS.match(part) for part in resolved.parts)


def contains_secret(path, roots):
    resolved = Path(os.path.realpath(path))
    return any(Path(os.path.realpath(root)).is_relative_to(resolved) for root in roots)


def grants_secret(path, roots):
    """True when granting `path` as a directory would expose a credential location inside or around it."""
    return secret_path(path, roots) or contains_secret(path, [root for root in roots if os.path.lexists(root)])


def directories(values, label, store):
    result = []
    roots = credential_roots(store)
    for value in values or []:
        raw = Path(os.path.expanduser(str(value)))
        if not raw.is_absolute():
            raise DelegateError(f"{label} 需要绝对路径：{value}")
        path = Path(os.path.realpath(raw))
        if not path.is_dir():
            raise DelegateError(f"{label} 不是已存在的目录：{value}")
        if path == Path(path.anchor) or path == Path(os.path.realpath(home())):
            raise DelegateError(f"{label} 不能是根目录或整个主目录：{value}")
        if secret_path(path, roots) or contains_secret(path, roots):
            raise DelegateError(f"{label} 与凭据或调度状态路径重叠：{value}")
        if store is not None and (path.is_relative_to(Path(store.root).resolve())
                                  or Path(store.root).resolve().is_relative_to(path)):
            raise DelegateError(f"{label} 与调度状态目录重叠：{value}")
        if str(path) not in result:
            result.append(str(path))
    return result


def normalize(store, requested, *, kind="code", interactive=None):
    """Effective capabilities for a submission. Unset options take the open defaults."""
    requested = requested or {}
    tier = requested.get("access") or DEFAULT["access"]
    if tier not in TIERS:
        raise DelegateError(f"未知的 --access：{tier}")
    network = requested.get("network")
    domains = [d.strip().lower() for d in requested.get("allow_domains") or [] if d.strip()]
    image = requested.get("image")
    explicit = sorted(key for key in ("access", "network", "allow_domains", "read_dirs", "write_dirs", "image")
                      if requested.get(key) not in (None, [], ()))
    if tier == "sandbox":
        if network not in (None, "off") or domains:
            raise DelegateError("--access sandbox 强制禁网，不能同时指定 --network 或 --allow-domain")
        if requested.get("read_dirs") or requested.get("write_dirs"):
            raise DelegateError("--access sandbox 只能读写任务工作树，不能追加 --read-dir / --write-dir")
        if image is True or kind == "image":
            raise DelegateError("--access sandbox 关闭生图；生图任务请使用默认的 open 档")
        return {**SANDBOX, "explicit": explicit}
    network = network or DEFAULT["network"]
    if network not in NETWORKS:
        raise DelegateError(f"未知的 --network：{network}")
    for domain in domains:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain):
            raise DelegateError(f"--allow-domain 需要不带协议、端口和通配符的域名：{domain}")
    if domains and network != "on":
        raise DelegateError("--allow-domain 只在 --network on 时收窄外网")
    if tier == "full":
        if not requested.get("confirm_full"):
            raise DelegateError("--access full 需要 --confirm-full")
        confirm_full(interactive)
    return {"access": tier, "network": network, "allow_domains": list(dict.fromkeys(domains)),
            "read_dirs": directories(requested.get("read_dirs"), "--read-dir", store),
            "write_dirs": directories(requested.get("write_dirs"), "--write-dir", store),
            "image": True if image is None else bool(image), "explicit": explicit}


def confirm_full(interactive=None):
    """`full` is only ever granted by a person typing at a terminal."""
    import sys
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        raise DelegateError("--access full 只能由用户在交互式终端确认")
    print("即将授予 full 档：越出沙箱的命令不再逐条审批（凭据与不可逆操作仍受底线约束）。\n"
          "确认请输入 full：", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip() != "full":
        raise DelegateError("未确认 full 档")


def of(task):
    """Capabilities of a recorded task; tasks from before this feature stay closed."""
    return task.get("capabilities") or LEGACY


def is_open(capabilities):
    return capabilities.get("access") in ("smart", "open", "full")


def tool_roots():
    """Read-only locations a worker needs to run the host's toolchains."""
    base = Path(os.path.realpath(home()))
    blocked = {base, base / ".local", Path("/"), Path("/usr"), Path("/usr/local"), Path("/opt")}
    roots = []

    def add(path):
        path = Path(os.path.realpath(path))
        if path.exists() and path not in blocked and str(path) not in roots:
            roots.append(str(path))

    def prefix(directory):
        directory = Path(os.path.realpath(directory))
        add(directory)
        if directory.name in ("bin", "sbin") and directory.parent not in blocked:
            add(directory.parent)

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).is_absolute() and Path(entry).is_dir():
            prefix(entry)
    for tool in ("node", "npm", "npx", "pnpm", "yarn", "bun", "python3", "uv", "git"):
        found = shutil.which(tool)
        if found:
            prefix(Path(os.path.realpath(found)).parent)
    return roots


def environment_roots(cache):
    """Interpreters behind virtual environments kept under the user's cache directory."""
    roots = []
    for config in [*cache.glob("*/pyvenv.cfg"), *cache.glob("*/*/pyvenv.cfg")]:
        try:
            lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                # A version alias (cpython-3.12 -> cpython-3.12.16) is a link: reading it needs
                # its containing directory, not only the directory it points to.
                lexical = Path(value.strip())
                for parent in (lexical, *lexical.parents):
                    if parent.is_symlink() and parent.parent not in (home(), home() / ".local", Path("/")):
                        roots.append(str(parent.parent))
                directory = Path(os.path.realpath(value.strip()))
                target = directory.parent if directory.name == "bin" else directory
                # A framework build keeps its libraries beside `bin`, a few levels up.
                for parent in (target, *target.parents):
                    if parent.name.endswith(".framework"):
                        target = parent
                        break
                if target.is_dir() and target not in (Path("/"), Path("/usr")):
                    roots.append(str(target))
    return roots


def read_roots(store, task, capabilities, own=None):
    """Default and requested read-only directories, resolved, minus credential locations.

    The original project is not among them: its ignored content is brought into the workspace,
    and the project directory itself may hold `.env` files and keys a workspace never receives.
    """
    if not is_open(capabilities):
        return []
    base = home()
    candidates = []
    skills = base / ".claude" / "skills"
    if skills.is_dir():
        candidates.append(skills)
        for entry in sorted(skills.iterdir()):
            if entry.is_symlink() and entry.is_dir():
                candidates.append(os.path.realpath(entry))
    cache = base / ".cache"
    if cache.is_dir():
        candidates.append(cache)
        candidates.extend(environment_roots(cache))
    candidates.extend(tool_roots())
    # System frameworks, toolchains and applications (OpenSSL and Perl runtimes, Xcode tools,
    # browsers). User data lives elsewhere; the system keychain stays denied.
    candidates.extend(SYSTEM_ROOTS)
    candidates.extend(capabilities.get("read_dirs") or [])
    secrets = credential_roots(store, own)
    result = []
    for candidate in candidates:
        path = os.path.realpath(candidate)
        if (path not in result and os.path.exists(path) and not secret_path(path, secrets)
                and not contains_secret(path, [root for root in secrets if os.path.lexists(root)])):
            result.append(path)
    return result


def expand(token, cwd, env_home):
    token = token.strip("'\"")
    if token.startswith("~"):
        token = str(home()) + token[1:] if token[:2] in ("~", "~/") else token
    token = token.replace("$HOME", env_home).replace("${HOME}", env_home)
    if not token or token.startswith("-") or "$" in token or "`" in token:
        return None
    path = Path(token)
    return Path(os.path.normpath(path if path.is_absolute() else Path(cwd or "/") / path))


def words(command):
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def segments(command):
    """Simple commands of a shell line, with wrappers like `bash -lc '...'` unwrapped."""
    result = []
    for part in SHELL_BREAK.split(command or ""):
        tokens = words(part)
        while tokens and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0])
                          or tokens[0] in ("sudo", "env", "command", "exec", "nohup", "time", "xargs")):
            tokens = tokens[1:]
        if len(tokens) >= 3 and Path(tokens[0]).name in ("sh", "bash", "zsh", "dash") and tokens[1].startswith("-") \
                and "c" in tokens[1]:
            result.extend(segments(tokens[2]))
        elif tokens:
            result.append(tokens)
    return result


def command_paths(command, cwd, env_home=None):
    """Absolute paths a command line names. Best effort: variables and globs stay unresolved."""
    env_home = env_home or str(home())
    found = []
    for tokens in segments(command):
        for token in tokens[1:]:
            for piece in re.split(r"[=:,]", token) if "/" in token else [token]:
                if "/" not in piece and not piece.startswith("~") and piece not in (".", ".."):
                    continue
                path = expand(piece, cwd, env_home)
                if path is not None and str(path) not in found:
                    found.append(str(path))
    return found


def inside(path, roots):
    resolved = Path(os.path.realpath(path))
    return any(resolved == Path(os.path.realpath(root)) or resolved.is_relative_to(os.path.realpath(root))
               for root in roots)


def classify_command(command, cwd, *, secrets, writable, env_home=None):
    """(floor, paths): floor is "credential", "irreversible" or None."""
    command = command or ""
    paths = command_paths(command, cwd, env_home)
    lowered = command.replace("${HOME}", "~").replace("$HOME", "~")
    if SECRET_COMMANDS.search(command) or any(secret_path(path, secrets) for path in paths):
        return "credential", paths
    for tokens in segments(command):
        for token in tokens[1:]:
            name = Path(token.strip("'\"")).name
            if name.casefold() in SECRET_NAMES or SECRET_GLOBS.match(name):
                return "credential", paths
    if re.search(r"(?:^|[\s/'\"=])(?:\.ssh|\.aws|\.gnupg|\.netrc|\.git-credentials|auth\.json|"
                 r"\.credentials\.json|Keychains|state\.sqlite3)(?:$|[\s/'\"])", lowered):
        return "credential", paths
    if IRREVERSIBLE.search(command):
        return "irreversible", paths
    for tokens in segments(command):
        name = Path(tokens[0]).name
        targets = [token for token in tokens[1:] if not token.startswith("-")]
        if name in RELOCATORS:
            targets = targets[:-1]
        elif name == "find" and ("-delete" in tokens or "-exec" in tokens):
            targets = targets[:1] or ["."]
        elif name == "git" and "clean" in tokens:
            targets = [cwd or "."]
        elif name not in REMOVERS:
            continue
        for target in targets or ["?"]:
            path = expand(target, cwd, env_home or str(home()))
            if path is None or not inside(path, writable):
                return "irreversible", paths
    return None, paths


# `smart` settles routine out-of-sandbox requests itself and sends the rest to the planning session.
# Commands that change the machine, reach other hosts or hand control to another program are never routine.
SMART_REVIEW = re.compile(
    r"(?:^|[\s;&|(`'\"/])(?:sudo|doas|su|osascript|launchctl|defaults|networksetup|systemsetup|scutil|spctl|csrutil|"
    r"tccutil|pmset|crontab|at|chflags|chown|chgrp|xattr|kill|pkill|killall|open|ssh|scp|sftp|rsync|curl|wget|nc|"
    r"ncat|telnet|ftp|brew|port|pipx|eval|source)(?=$|[\s;&|)'\"])|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:[^;&|]*\s)?(?:-g|--global)\b|(?:pip|pip3|uv\s+pip)\s+install\b|"
    # Installing packages pulls and runs third-party code.
    r"(?:npm|pnpm|yarn|bun)\s+(?:[^;&|]*\s)?(?:install|i|add|ci|update|upgrade|dlx|create)\b|"
    r"(?:cargo|gem|go)\s+install\b|(?:bunx|uvx|pipx)\b|"
    r"git\s+config\s+(?:[^;&|]*\s)?--(?:global|system)\b|chmod\s+(?:[^;&|]*\s)?-R\b|"
    r"(?:>|>>)\s*(?:/|~)|\$\(|`", re.I)
SMART_SCRIPT_REVIEW = re.compile(
    r"\b(?:sudo|osascript|launchctl|networksetup|crontab|killall|pkill)\b|child_process[^\n]*\b(?:exec|execSync)\s*\(|"
    r"\bos\.system\s*\(|\bsubprocess\.[a-z_]+\([^\n]*shell\s*=\s*True|\beval\s*\(|"
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(?:/|~|\$HOME)", re.I)
URL = re.compile(r"\b(?:https?|wss?|ftp)://([^/\s'\"`:)\]]+|\[[^\]]+\])", re.I)
LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0"}
SCRIPT_RUNNERS = {"sh", "bash", "zsh", "dash", "node", "bun", "deno", "python", "python3", "ruby", "perl", "tsx",
                  "vite-node", "npx", "uv"}
SCRIPT_LIMIT = 256 * 1024


def loopback(target):
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(target or ""), flags=re.I).split("/")[0]
    host = host if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.lower() in LOOPBACK or host.lower().endswith(".localhost")


def script_review(text, *, secrets):
    """Why a script the worker wants to run outside the sandbox needs a person to read it, or None."""
    if SMART_SCRIPT_REVIEW.search(text) or IRREVERSIBLE.search(text) or SECRET_COMMANDS.search(text):
        return "脚本含改动系统、不可逆或凭据相关的操作"
    for host in URL.findall(text):
        if not loopback(host):
            return f"脚本访问本机之外的地址：{host}"
    for name in re.findall(r"[\w.~/-]+", text):
        part = Path(name).name
        if part.casefold() in SECRET_NAMES or SECRET_GLOBS.match(part):
            return f"脚本提到凭据路径：{part}"
    return None


def smart_review(kind, display, floor, *, secrets, own, readable):
    """Reason the planning session must decide this request, or None when `smart` may accept it.

    Accepted without asking: a command (or file change) with no floor, no extra permission grant and no
    host beyond loopback, whose every named path lies in the task's own directories (`own`) or the
    read-only tool locations (`readable`), and whose scripts, when it runs any, were read and are routine.
    """
    if floor:
        return "触及安全底线"
    if kind == "permissions":
        return "申请额外权限"
    if display.get("additional_permissions"):
        return "命令附带额外权限"
    if any(not loopback(target) for target in display.get("network") or []):
        return "访问本机之外的网络"
    if kind == "file_change":
        outside = [path for path in display.get("paths") or [] if not inside(path, own)]
        return f"修改任务目录之外的文件：{outside[0]}" if outside else None
    command = display.get("command") or ""
    if SMART_REVIEW.search(command):
        return "命令改动系统、访问其他主机或把控制交给其他程序"
    for host in URL.findall(command):
        if not loopback(host):
            return f"命令访问本机之外的地址：{host}"
    for path in display.get("paths") or []:
        if not inside(path, own) and not inside(path, readable):
            return f"命令涉及任务目录之外的路径：{path}"
    cwd = display.get("cwd")
    if cwd and not inside(cwd, own):
        return "命令不在任务目录内执行"
    for tokens in segments(command):
        if Path(tokens[0]).name == "npx":
            # `npx <tool>` downloads the package when the project does not have it.
            tool = next((token for token in tokens[1:] if not token.startswith("-")), None)
            folder = Path(os.path.realpath(cwd or "/"))
            installed = tool and any((parent / "node_modules" / ".bin" / tool).exists()
                                     for parent in (folder, *folder.parents) if inside(parent, own))
            if not installed:
                return f"npx 会下载并运行项目里没有的包：{tool or '?'}"
        if Path(tokens[0]).name not in SCRIPT_RUNNERS:
            continue
        for token in tokens[1:]:
            path = expand(token, cwd, str(home()))
            if path is None or not path.is_file() or path.suffix in (".json", ".md", ".txt", ".log"):
                continue
            if not inside(path, own):
                continue   # a tool on a read-only root; named paths outside both sets were refused above
            try:
                if path.stat().st_size > SCRIPT_LIMIT:
                    return f"脚本过大，未能读完：{path.name}"
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return f"脚本读不到：{path.name}"
            reason = script_review(text, secrets=secrets)
            if reason:
                return f"{path.name}：{reason}"
    return None


def classify_changes(changes, *, secrets, writable):
    paths = [change.get("path") for change in changes if change.get("path")]
    if any(secret_path(path, secrets) for path in paths):
        return "credential", paths
    if any(change.get("kind") == "delete" and change.get("path") and not inside(change["path"], writable)
           for change in changes):
        return "irreversible", paths
    return None, paths


def permission_paths(permissions):
    files = (permissions or {}).get("fileSystem") or {}
    paths = [*(files.get("read") or []), *(files.get("write") or [])]
    for entry in files.get("entries") or []:
        target = entry.get("path") or {}
        if target.get("type") == "path":
            paths.append(target.get("path"))
        elif target.get("type") == "special" and (target.get("value") or {}).get("kind") == "root":
            paths.append("/")
    return [path for path in paths if path]


def summary(capabilities):
    """The fields worth showing in task records, the ledger and audit events."""
    keys = ("access", "network", "allow_domains", "read_dirs", "write_dirs", "image")
    return {key: capabilities.get(key) for key in keys} | (
        {"legacy": True} if capabilities.get("legacy") else {})
