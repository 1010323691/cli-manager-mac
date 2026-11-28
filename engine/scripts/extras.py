"""Building blocks for cli-delegate: project config, snapshots/undo, git-worktree isolation,
verification, the usage ledger. Standard library only.

Snapshots use a *shadow* git repository per working directory
(~/.cli-delegate/snapshots/<hash>.git, work tree = the directory), so the user's own
repository, branches and index are never touched, and directories that are not git
repositories are covered too. .gitignore files are honoured.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

STATE_DIR = Path(os.environ.get("CLI_DELEGATE_STATE", Path.home() / ".cli-delegate")).expanduser()
SNAP_DIR = STATE_DIR / "snapshots"
WT_DIR = STATE_DIR / "worktrees"
RMETA_DIR = STATE_DIR / "runs"
LEDGER = STATE_DIR / "ledger.jsonl"
SNAP_MAX_BYTES = int(os.environ.get("CLI_DELEGATE_SNAPSHOT_MAX_MB", "300")) * 1024 * 1024
DEFAULT_EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".DS_Store", ".next/", ".nuxt/",
                    ".cache/", ".gradle/", ".idea/", "*.pyc", ".tox/", ".mypy_cache/", ".pytest_cache/"]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt", ".cache", ".gradle",
             ".tox", ".mypy_cache", ".pytest_cache"}
GIT_ID = {"GIT_AUTHOR_NAME": "cli-delegate", "GIT_AUTHOR_EMAIL": "cli-delegate@localhost",
          "GIT_COMMITTER_NAME": "cli-delegate", "GIT_COMMITTER_EMAIL": "cli-delegate@localhost"}


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ------------------------------------------------------------ router meta ----
def rmeta_path(run_id: str) -> Path:
    return RMETA_DIR / f"{run_id}.json"


def load_rmeta(run_id: str) -> dict:
    return read_json(rmeta_path(run_id), {}) or {}


def save_rmeta(run_id: str, meta: dict) -> None:
    write_json(rmeta_path(run_id), meta)


# --------------------------------------------------------- project config ----
def find_project_config(cwd: str) -> Path | None:
    """Nearest .cli-delegate.toml at or above cwd (stops at the home directory)."""
    p = Path(cwd).resolve()
    home = Path.home().resolve()
    for d in [p, *p.parents]:
        f = d / ".cli-delegate.toml"
        if f.is_file():
            return f
        if d == home or d == d.parent:
            break
    return None


TRUST_FILE = STATE_DIR / "trusted-configs.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trust_key(path: Path) -> str:
    """Bound to the path the PROJECT uses (absolute, symlinks NOT resolved): another project that
    links to the same file does not inherit the trust."""
    p = Path(os.path.abspath(path))
    return str(p.parent.resolve() / p.name)  # resolve the directory spelling, never the file itself


def is_trusted(path: Path, digest: str | None = None) -> bool:
    if Path(path).is_symlink():
        return False  # a linked config could be shared across projects: never auto-trusted
    digest = digest or hashlib.sha256(path.read_bytes()).hexdigest()
    return (read_json(TRUST_FILE, {}) or {}).get(_trust_key(path)) == digest


def trust_config(path: Path, expected_sha: str) -> str:
    """Trust exactly the version the user reviewed: expected_sha (full or >=12-char prefix) must match."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(expected_sha) < 12 or not digest.startswith(expected_sha.lower()):
        raise RuntimeError(f"文件内容与预览时不同（当前 sha256 {digest[:12]}…），请重新预览后再确认")
    if Path(path).is_symlink():
        raise RuntimeError("项目配置是符号链接，不能确认；请把它换成项目内的普通文件")
    data = read_json(TRUST_FILE, {}) or {}
    data[_trust_key(path)] = digest
    write_json(TRUST_FILE, data)
    return digest


def load_project_config(cwd: str, require_trust: bool = True) -> dict:
    """Parse .cli-delegate.toml into {path, rules, verify, defaults}.

    [approve] commands = ["npm test", ...]  # simple commands with this prefix: auto-approve
              paths = ["src/", "tests/"]   # file writes inside: auto-approve
    [deny]    commands = ["git push"]      paths = [".env", "secrets/"]   message = "..."
    [verify]  command = "npm test"  timeout = 900
    [defaults] backend/access/model/effort/approval_timeout/allow_switch
    """
    f = find_project_config(cwd)
    if not f:
        return {}
    try:
        raw = f.read_bytes()  # read once: the digest and the parsed rules come from the same bytes
    except OSError as e:
        return {"path": str(f), "error": f"无法读取: {e}"}
    digest = hashlib.sha256(raw).hexdigest()
    if require_trust and not is_trusted(f, digest):
        # repository content must not widen permissions or run commands until the user trusts it
        return {"path": str(f), "untrusted": True, "sha256": digest}
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return {"path": str(f), "error": "需要 Python 3.11+ 才能读取 .cli-delegate.toml", "sha256": digest}
    try:
        cfg = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        return {"path": str(f), "error": f"无法解析: {e}", "sha256": digest}
    base = f.parent
    rules = []
    for section, decision in (("approve", "allow"), ("deny", "deny")):
        sec = cfg.get(section) or {}
        for c in sec.get("commands") or []:
            rules.append({"decision": decision, "tool": "Bash", "prefix": str(c).strip(), "exact": False,
                          "source": "project", **({"message": sec["message"]} if sec.get("message") else {})})
        for p in sec.get("paths") or []:
            rules.append({"decision": decision, "tool": "FILE", "dir": str((base / p).resolve()),
                          "source": "project", **({"message": sec["message"]} if sec.get("message") else {})})
    return {"path": str(f), "sha256": digest, "rules": rules, "verify": cfg.get("verify") or {},
            "defaults": cfg.get("defaults") or {}}


# -------------------------------------------------------------- snapshots ----
def _shadow(cwd: str) -> Path:
    h = hashlib.sha1(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
    return SNAP_DIR / f"{h}.git"


def _git(args: list, env_extra: dict | None = None, cwd: str | None = None, check=True, text=True, inp=None):
    env = {**os.environ, **GIT_ID, **(env_extra or {})}
    r = subprocess.run(["git", *args], capture_output=True, text=text, env=env, cwd=cwd, input=inp)
    if check and r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args[:3])} 失败: {(err or '').strip()[:300]}")
    return r


def _names(args: list, env: dict) -> list:
    """NUL-separated git path output read as BYTES -> real paths (no quoting, no newline translation)."""
    raw = _git(args, env, text=False).stdout
    return [os.fsdecode(x) for x in raw.split(b"\0") if x]


def _dir_size_over(cwd: str, limit: int) -> bool:
    total = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
            if total > limit:
                return True
    return False


def _shadow_env(cwd: str, tag: str) -> dict:
    sh = _shadow(cwd)
    return {"GIT_DIR": str(sh), "GIT_WORK_TREE": str(Path(cwd).resolve()), "GIT_INDEX_FILE": str(sh / f"index-{tag}")}


def snapshot(cwd: str, run_id: str, label: str) -> dict:
    """Record the directory state as refs/runs/<run_id>/<label> in the shadow repo."""
    if not shutil.which("git"):
        return {"ok": False, "error": "没有 git，无法拍快照"}
    cwd = str(Path(cwd).resolve())
    if _dir_size_over(cwd, SNAP_MAX_BYTES):
        return {"ok": False, "error": f"目录超过 {SNAP_MAX_BYTES // 1024 // 1024}MB（不含依赖/缓存目录），跳过快照"}
    sh = _shadow(cwd)
    if not (sh / "HEAD").exists():
        sh.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "-q", str(sh)])
        (sh / "info").mkdir(exist_ok=True)
        (sh / "info" / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n")
        (sh / "cli-delegate-worktree").write_text(cwd)
    env = _shadow_env(cwd, f"{os.getpid()}")
    try:
        if label == "before":  # all present paths, ignored ones too, for safe undo
            skip = [f"--exclude={d}/" for d in SKIP_DIRS if d != ".git"]  # dependency/cache trees stay out
            allp = _git(["ls-files", "-z", "--others", "--cached", *skip, "."], env, cwd=cwd, text=False).stdout
            (SNAP_DIR / f"{run_id}.preexisting").write_bytes(allp)
        _git(["add", "-A", "."], env, cwd=cwd)
        tree = _git(["write-tree"], env, cwd=cwd).stdout.strip()
        commit = _git(["commit-tree", tree, "-m", f"{run_id} {label} {now_iso()}"], env, cwd=cwd).stdout.strip()
        _git(["update-ref", f"refs/runs/{run_id}/{label}", commit], env, cwd=cwd)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    finally:
        Path(env["GIT_INDEX_FILE"]).unlink(missing_ok=True)
    return {"ok": True, "repo": str(sh), "cwd": cwd, "commit": commit, "tree": tree}


def snap_diff(cwd: str, run_id: str, stat: bool = False, a: str = "before", b: str = "after") -> str:
    env = {"GIT_DIR": str(_shadow(cwd))}
    args = ["diff", "--stat"] if stat else ["diff", "--binary"]
    r = _git([*args, f"refs/runs/{run_id}/{a}", f"refs/runs/{run_id}/{b}"], env, check=False)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip() or "没有这次运行的快照")
    return r.stdout


def snap_undo(cwd: str, run_id: str, force: bool = False, since: float | None = None) -> dict:
    """Restore cwd to the 'before' snapshot. Refuses if files changed after the run unless force."""
    cwd = str(Path(cwd).resolve())
    sh = _shadow(cwd)
    env = {"GIT_DIR": str(sh)}
    for lab in ("before", "after"):
        if _git(["rev-parse", "-q", "--verify", f"refs/runs/{run_id}/{lab}"], env, check=False).returncode != 0:
            raise RuntimeError(f"没有这次运行的 {lab} 快照，无法撤销")
    cur = snapshot(cwd, run_id, "pre-undo")
    if not cur.get("ok"):
        raise RuntimeError(f"撤销前无法记录当前状态: {cur.get('error')}")
    if not force:
        # files the task deleted that exist again on disk (perhaps now ignored, so invisible to the
        # tree comparison below): restoring would overwrite them
        deleted = set(_names(["diff", "--name-only", "-z", "--diff-filter=D", f"refs/runs/{run_id}/before",
                              f"refs/runs/{run_id}/after"], env))
        back = sorted(x for x in deleted if (Path(cwd) / x).exists())
        if back:
            raise RuntimeError("这些被任务删除的文件现在又存在了（可能是之后重建的），撤销会覆盖它们："
                               + ", ".join(back[:20]) + "。确认要撤销请加 --force")
    after_tree = _git(["rev-parse", f"refs/runs/{run_id}/after^{{tree}}"], env).stdout.strip()
    if cur["tree"] != after_tree and not force:
        changed = _names(["diff", "--name-only", "-z", f"refs/runs/{run_id}/after", f"refs/runs/{run_id}/pre-undo"], env)
        raise RuntimeError("运行结束后这些文件又被改过（可能是你或其他任务），直接撤销会丢掉这些改动："
                           + ", ".join(changed[:20]) + "。确认要撤销请加 --force")
    before = f"refs/runs/{run_id}/before"
    now_files = set(_names(["ls-tree", "-r", "-z", "--name-only", f"refs/runs/{run_id}/pre-undo"], env))
    old_files = set(_names(["ls-tree", "-r", "-z", "--name-only", before], env))
    removed, kept = [], []
    pre_existing = set()
    plist = SNAP_DIR / f"{run_id}.preexisting"
    if plist.exists():  # every path present before the run, incl. ignored ones
        pre_existing = {os.fsdecode(x) for x in plist.read_bytes().split(b"\0") if x}
    for rel in sorted(now_files - old_files):
        p = Path(cwd) / rel
        if not (p.is_file() or p.is_symlink()):
            continue
        try:
            older = since is not None and p.lstat().st_mtime < since - 2
        except OSError:
            older = False
        if rel in pre_existing or older or any(part in SKIP_DIRS for part in Path(rel).parts):
            kept.append(rel)  # existed before the run / dependency or cache tree: never delete it
            continue
        p.unlink()
        removed.append(rel)
    wenv = _shadow_env(cwd, f"undo-{os.getpid()}")
    try:
        _git(["read-tree", before], wenv, cwd=cwd)
        _git(["checkout-index", "-a", "-f"], wenv, cwd=cwd)
    finally:
        Path(wenv["GIT_INDEX_FILE"]).unlink(missing_ok=True)
    changed = _names(["diff", "--name-only", "-z", before, f"refs/runs/{run_id}/pre-undo"], env)
    return {"removed": removed, "kept": kept, "restored": [c for c in changed if c not in removed],
            "redo_ref": f"refs/runs/{run_id}/pre-undo"}


def snap_drop(cwd: str, run_id: str) -> None:
    env = {"GIT_DIR": str(_shadow(cwd))}
    for lab in ("before", "after", "pre-undo"):
        _git(["update-ref", "-d", f"refs/runs/{run_id}/{lab}"], env, check=False)


LOCK_DIR = STATE_DIR / "locks"
_held: dict = {}


def _lock_path(cwd: str) -> Path:
    return LOCK_DIR / (hashlib.sha1(str(Path(cwd).resolve()).encode()).hexdigest()[:16] + ".lock")


def dir_lock(cwd: str, run_id: str) -> str | None:
    """Claim cwd (a working directory or an isolated worktree) with an flock held until dir_unlock or
    process exit. Returns the holder's run id when another process holds it. No stale-lock window:
    the kernel drops the lock when the holder dies."""
    import fcntl
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lp = _lock_path(cwd)
    f = lp.open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        try:
            holder = json.loads(f.read() or "{}").get("run")
        except json.JSONDecodeError:
            holder = None
        f.close()
        return holder or "?"
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"run": run_id, "pid": os.getpid(), "cwd": cwd}))
    f.flush()
    _held[str(lp)] = f
    return None


def lock_fd(cwd: str) -> int | None:
    """File descriptor of a held directory lock, to pass to child processes (flock is shared by an
    inherited descriptor, so the lock lives as long as ANY holder: the entry or the backend)."""
    f = _held.get(str(_lock_path(cwd)))
    return f.fileno() if f else None


def dir_unlock(cwd: str, run_id: str | None = None) -> None:
    f = _held.pop(str(_lock_path(cwd)), None)
    if f:
        f.close()  # closing the descriptor releases the flock


# -------------------------------------------------------------- isolation ----
def git_root(cwd: str) -> str | None:
    r = _git(["-C", cwd, "rev-parse", "--show-toplevel"], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def make_worktree(cwd: str, run_id: str) -> dict:
    """A worktree + branch cli-delegate/<id> based on the CURRENT working state (incl. uncommitted)."""
    root = git_root(cwd)
    if not root:
        raise RuntimeError("隔离运行需要工作目录在 git 仓库里")
    gitdir = _git(["-C", root, "rev-parse", "--absolute-git-dir"]).stdout.strip()  # .git may be a file
    idx = Path(gitdir) / f"cli-delegate-index-{os.getpid()}"
    real_idx = Path(_git(["-C", root, "rev-parse", "--path-format=absolute", "--git-path", "index"]).stdout.strip())
    if real_idx.exists():
        shutil.copy2(real_idx, idx)  # start from the real index: tracked-but-ignored files stay included
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        _git(["-C", root, "add", "-A", "."], env)
        tree = _git(["-C", root, "write-tree"], env).stdout.strip()
        head = _git(["-C", root, "rev-parse", "-q", "--verify", "HEAD"], check=False).stdout.strip()
        args = ["-C", root, "commit-tree", tree, "-m", f"cli-delegate base for {run_id}"] + (["-p", head] if head else [])
        base = _git(args).stdout.strip()
    finally:
        idx.unlink(missing_ok=True)
    WT_DIR.mkdir(parents=True, exist_ok=True)
    wt = WT_DIR / run_id
    branch = f"cli-delegate/{run_id}"
    _git(["-C", root, "worktree", "add", "-q", "-b", branch, str(wt), base])
    rel = os.path.relpath(Path(cwd).resolve(), Path(root).resolve())
    exec_cwd = str((wt / rel).resolve()) if rel != "." else str(wt.resolve())
    return {"root": root, "worktree": str(wt.resolve()), "branch": branch, "base": base, "exec_cwd": exec_cwd}


def worktree_state_commit(iso: dict) -> str:
    """Commit object (in the real repo's object store) of the worktree's current files."""
    wt = iso["worktree"]
    gitdir = _git(["-C", wt, "rev-parse", "--absolute-git-dir"]).stdout.strip()
    idx = Path(gitdir) / f"cli-delegate-merge-index-{os.getpid()}"
    real_idx = Path(_git(["-C", wt, "rev-parse", "--path-format=absolute", "--git-path", "index"]).stdout.strip())
    if real_idx.exists():
        shutil.copy2(real_idx, idx)
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        _git(["-C", wt, "add", "-A", "."], env)
        tree = _git(["-C", wt, "write-tree"], env).stdout.strip()
        return _git(["-C", wt, "commit-tree", tree, "-p", iso["base"], "-m", "cli-delegate merge state"]).stdout.strip()
    finally:
        idx.unlink(missing_ok=True)


def merge_worktree(iso: dict, run_id: str, three_way: bool = False) -> str:
    """Apply everything the isolated task changed (all rounds: run, resumes, handoffs) to the original repo."""
    state = worktree_state_commit(iso)
    # bytes end to end: CRLF and binary content must reach `git apply` unchanged
    patch = _git(["-C", iso["root"], "diff", "--binary", iso["base"], state], text=False).stdout
    if not patch.strip():
        return "没有需要合并的改动"
    root = iso["root"]
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    chk = _git(["-C", root, "apply", "--check", "--binary", "-"], check=False, inp=patch, text=False)
    if chk.returncode == 0:
        _git(["-C", root, "apply", "--binary", "-"], inp=patch, text=False)
        return "已把改动应用到原工作目录（未暂存、未提交）"
    if three_way:
        r = _git(["-C", root, "apply", "--3way", "--binary", "-"], check=False, inp=patch, text=False)
        if r.returncode == 0:
            return "已用三方合并应用（git 索引中会出现相关条目，请检查）"
        raise RuntimeError("三方合并有冲突：" + dec(r.stderr)[:600])
    raise RuntimeError("改动与原目录当前内容冲突，无法直接应用：" + dec(chk.stderr)[:600]
                       + "。可以先看 diff，手动处理，或加 --3way 尝试三方合并")


def remove_worktree(iso: dict) -> list:
    """Remove the worktree and its branch; returns a list of problems (empty = fully cleaned)."""
    problems = []
    if Path(iso["worktree"]).exists():
        r = _git(["-C", iso["root"], "worktree", "remove", "--force", iso["worktree"]], check=False)
        if r.returncode != 0:
            problems.append(f"worktree 未删除: {(r.stderr or '').strip()[:200]}")
    _git(["-C", iso["root"], "worktree", "prune"], check=False)  # drops registrations of vanished dirs
    listing = _git(["-C", iso["root"], "worktree", "list", "--porcelain"], check=False).stdout or ""
    if f"worktree {Path(iso['worktree']).resolve()}" in listing or f"worktree {iso['worktree']}" in listing:
        problems.append("git 中仍登记着这个 worktree")
    if _git(["-C", iso["root"], "rev-parse", "-q", "--verify", f"refs/heads/{iso['branch']}"], check=False).returncode == 0:
        r = _git(["-C", iso["root"], "branch", "-D", iso["branch"]], check=False)
        if r.returncode != 0:
            problems.append(f"分支未删除: {(r.stderr or '').strip()[:200]}")
    return problems


# ---------------------------------------------------------------- verify ----
_VERIFY_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
                    "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "VIRTUAL_ENV", "PYTHONPATH",
                    "NODE_PATH", "NVM_DIR", "PNPM_HOME", "CI"}


def verify_env() -> dict:
    """Allow-list environment: tooling paths only, never tokens, keys or account variables."""
    env = {k: v for k, v in os.environ.items() if k in _VERIFY_ENV_KEEP or k.startswith("LC_")}
    env["CI"] = env.get("CI", "1")
    return env


def _kill_group(pgid: int, p: subprocess.Popen, grace: float = 5) -> None:
    """SIGTERM the group, give it `grace` seconds, then SIGKILL the whole group unconditionally:
    descendants that ignore SIGTERM or outlive the leader are killed too."""
    import signal
    import time
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    end = time.time() + grace
    while time.time() < end and p.poll() is None:
        time.sleep(0.2)
    p.poll()  # reap the leader so the group can be signalled reliably
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_verify(command: str, cwd: str, timeout: int = 900, codex_bin: str | None = None,
               host: bool = False) -> dict:
    """Run the verify command. Default: inside the Codex sandbox with pinned settings (writes only in
    cwd, no network) and an allow-listed environment, because files the delegated task changed (tests,
    build scripts) must not run with the user's full rights or credentials. host=True: unsandboxed."""
    import tempfile
    if host:
        argv, where = ["sh", "-c", command], "本机（未隔离）"
    elif codex_bin:
        argv = [codex_bin, "sandbox", "-c", "sandbox_mode=workspace-write",
                "-c", "sandbox_workspace_write.network_access=false",
                "-c", "sandbox_workspace_write.writable_roots=[]",
                "-c", "shell_environment_policy.inherit=core",
                "--", "sh", "-c", command]
        where = "codex 沙箱（仅工作目录可写、无网络、精简环境变量、HOME 指向空临时目录）"
    else:
        return {"command": command, "rc": -2, "ok": False, "where": "未执行", "seconds": 0,
                "tail": "没有 codex 可用于隔离执行验证命令；确需在本机执行请用 unsafe 子命令加 --verify-host（须用户同意）"}
    t0 = _dt.datetime.now()
    env = verify_env()
    tmp_home = None
    if not host:
        tmp_home = tempfile.mkdtemp(prefix="cli-delegate-verify-home-")
        env["HOME"] = tmp_home  # tools resolving ~ see an empty home, not the user's
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                 env=env, stdin=subprocess.DEVNULL)
        except OSError as e:
            if tmp_home:
                shutil.rmtree(tmp_home, ignore_errors=True)
            return {"command": command, "rc": -3, "ok": False, "where": where, "seconds": 0,
                    "tail": f"验证命令无法启动: {e}"}
        try:
            rc = p.wait(timeout=timeout)
            note = ""
            _kill_group(p.pid, p, grace=0)  # leftover background processes must not keep running
        except subprocess.TimeoutExpired:
            _kill_group(p.pid, p)
            rc, note = -1, f"\n验证命令超过 {timeout}s，已终止整个进程组"
        out.seek(0)
        text = out.read().decode("utf-8", "replace") + note
    if tmp_home:
        shutil.rmtree(tmp_home, ignore_errors=True)
    return {"command": command, "rc": rc, "ok": rc == 0, "where": where, "tail": text[-3000:],
            "seconds": round((_dt.datetime.now() - t0).total_seconds(), 1)}


# ---------------------------------------------------------------- ledger ----
def ledger_append(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_entries() -> list:
    out = []
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def quota_brief(q: dict | None) -> str:
    if not q:
        return "未查询"
    if not q.get("ok"):
        return "未知"
    return "；".join(f"{w['name']} {w['used']}%" for w in q.get("windows") or [])


def fmt_ledger(e: dict) -> str:
    tok = e.get("tokens") or {}
    lines = [
        "=== 账本 ===",
        f"运行: {e.get('id')}（{e.get('sub')}）  后端: {e.get('backend')}  结果: {e.get('status')}（exit {e.get('rc')}）",
        f"模型: {e.get('model_actual') or '?'}（请求 {e.get('model_requested') or '默认'}"
        f"{ {'default': '，来自默认模型设置', 'project': '，来自项目默认参数'}.get(e.get('model_source'), '')}）  强度: {e.get('effort') or '默认'}  档位: {e.get('access')}",
        f"撞限: {'是' if e.get('usage_limited') else '否'}" + (f"（{e.get('reset_at')} 重置）" if e.get('reset_at') else "")
        + f"  切换: {e.get('switch') or '否'}",
        f"消耗: tokens 入 {tok.get('input', '?')}（缓存 {tok.get('cached', '?')}）/ 出 {tok.get('output', '?')}"
        + (f"（推理 {tok['reasoning']}）" if tok.get("reasoning") else "")
        + (f" | 折算 ${e['cost_usd']:.4f}" if isinstance(e.get("cost_usd"), (int, float)) else "")
        + f" | 耗时 {e.get('seconds')}s",
        f"额度: 运行前 {quota_brief(e.get('quota_before'))} → 运行后 {quota_brief(e.get('quota_after'))}",
    ]
    if e.get("approvals"):
        a = e["approvals"]
        lines.append("审批: " + "，".join(f"{k} {v}" for k, v in a.items() if v))
    if e.get("verify"):
        v = e["verify"]
        lines.append(f"验证: `{v['command']}` → {'通过' if v['ok'] else '失败'}（rc {v['rc']}，{v['seconds']}s，{v.get('where', '')}）")
    if e.get("snapshot"):
        lines.append(f"快照: {e['snapshot']}")
    if e.get("isolated"):
        lines.append(f"隔离: 分支 {e['isolated'].get('branch')}，worktree {e['isolated'].get('worktree')}")
    return "\n".join(lines)
