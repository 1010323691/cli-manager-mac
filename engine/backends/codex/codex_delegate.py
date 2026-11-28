#!/usr/bin/env python3
"""codex-cli: run a task with the local Codex CLI from a Claude session (stdlib only).

The script drives `codex app-server` (JSON-RPC over stdio) as a client:
  initialize -> thread/start (or thread/resume) -> turn/start -> wait for turn/completed.
Approval requests from Codex (commands, file changes, extra permissions, MCP
elicitations) and request_user_input questions are written to
RUN_DIR/approvals/<id>.request.json and wait for the caller session to answer with
`decide`, so the caller reviews them in real time.

Subcommands:
  run       Run a task in the caller's working directory (default subcommand).
  watch     Wait until an approval request is pending or the run ends.
  decide    Answer an approval request (allow / deny / answer).
  resume    Continue a previous run's thread (answer, usage limit, timeout, follow-up).
  doctor    Check the CLI, login, models and rate limits.
  show / progress / list   Inspect runs.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import queue
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- settings ----
HOME = Path.home()
RUNS_DIR = Path(os.environ.get("CODEXCLI_RUNS", HOME / ".codex-cli" / "runs")).expanduser()
# Shared with claude-cli (neither strips it), so cross-tool chains are counted too.
DEPTH_VAR = "CLI_DELEGATE_DEPTH"
LEGACY_DEPTH_VARS = ("CODEXCLI_DELEGATE_DEPTH",)
ME = "codex-cli"

ACCESS = ["read", "edit", "strict", "auto", "full"]
# access tier -> (sandbox, approvalPolicy, approvalsReviewer)
TIERS = {
    "read": ("read-only", "never", "user"),
    "edit": ("workspace-write", "on-request", "user"),
    "strict": ("read-only", "on-request", "user"),
    "auto": ("workspace-write", "on-request", "auto_review"),
    "full": ("danger-full-access", "never", "user"),
}
STRIP_PREFIXES = ("CLAUDE", "ANTHROPIC_", "CODEX_", "MCP_", "OTEL_")
STRIP_EXACT = {"CLAUDECODE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "NODE_OPTIONS", "CLI_DELEGATE_UNSAFE_OK", "CLI_DELEGATE_LOCK_FD", "CLI_DELEGATE_QUEUE_JOB",
               "CLI_DELEGATE_QUEUE_RUN", "CLI_DELEGATE_QUEUE_TOKEN"}  # plumbing must not reach the child
NEEDS_TAG = "needs-confirmation"

WORKER_PROMPT = """\
You are a delegated worker started by another agent session (the "caller") on \
behalf of the same user. Rules:
- Your approval requests (commands needing escalation, file changes outside the \
sandbox, network access, extra permissions) are reviewed by the caller session, \
which may take a while. If a request is declined, do not try to get around it; \
work with what is allowed and list what was blocked in your final report. A \
declined request may be followed by a message from the caller explaining why.
- Work only inside the working directory{extra_dirs}. Do not touch files elsewhere.
- Keep the task hand-over-ready: before each major step, write one short line \
starting with `PROGRESS:` (what is done so far, what you do next). If you are \
interrupted (e.g. by a usage limit), these lines are how another agent continues.
- Do only what the task asks. No unrelated refactors, no new dependencies unless \
the task asks for them.
- Do not commit, push, open PRs, publish, send messages or delete data unless the \
task explicitly says so.
- When you need a decision you should not make alone (an ambiguity whose choices \
lead to materially different results, an irreversible or destructive step), ask \
with the request_user_input tool if it is available; the caller answers it, \
consulting the user when needed. Do not ask about minor choices you can make yourself.
- If you cannot ask and the decision still blocks you: do everything else first, \
do NOT guess on that point, and end your final report with:
<{tag}>
1. <question> | options: A) ... B) ... | recommended: A | why it matters: ...
</{tag}>
- End with a concise final report for the caller: what you did, files \
created/changed (paths), commands run and their results, anything unfinished or \
unverified, and assumptions. Write the report in the same language as the task.\
"""
PROMPT_FOOTER = (
    "\n\n---\n（委派运行说明：你的审批请求和提问由调用方会话审核回答。能自己合理决定的就自己决定并在报告里说明；"
    "确实需要别人拍板的，优先用 request_user_input 提问；无法提问又无法继续时，"
    f"才在最终报告最末尾输出 <{NEEDS_TAG}> … </{NEEDS_TAG}> 块。）"
)
DEFAULT_RESUME_PROMPT = (
    "上一次运行在中途被打断了（额度用尽、超时或等待确认），现在继续。"
    "先检查工作目录里已经完成的部分（读文件、看 git status / git diff），不要重复或回滚已做的改动；"
    "然后继续完成原任务，最后给出完整的最终报告（包括本次之前已完成的部分）。"
)


# ------------------------------------------------------------------ helpers ----
def die(msg: str, code: int = 2) -> None:
    print(f"[{ME}] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def short(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def current_depth() -> int:
    vals = []
    for k in (DEPTH_VAR, *LEGACY_DEPTH_VARS):
        try:
            vals.append(int(os.environ.get(k, "0") or 0))
        except ValueError:
            pass
    return max(vals or [0])


def find_bin() -> str:
    for c in (os.environ.get("CODEXCLI_BIN"), shutil.which("codex"), str(HOME / ".local/bin/codex"),
              "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    die("找不到 codex CLI（npm i -g @openai/codex，或用环境变量 CODEXCLI_BIN 指定）", 4)
    return ""


def clean_env(codex_home: str | None, extra: dict, passthrough: list[str]) -> dict:
    keep = set(passthrough)
    env = {k: v for k, v in os.environ.items()
           if k in keep or not (k in STRIP_EXACT or k.startswith(STRIP_PREFIXES))}
    # an explicit --codex-home wins; otherwise keep a CODEX_HOME the user already set
    codex_home = codex_home or os.environ.get("CODEX_HOME")
    if codex_home:
        env["CODEX_HOME"] = str(Path(codex_home).expanduser())
    env[DEPTH_VAR] = str(current_depth() + 1)
    env.update(extra)
    return env


def read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(path)


def load_text_arg(value, file, what):
    if value and file:
        die(f"{what} 不能同时用字符串和文件")
    if file:
        if file == "-":
            return sys.stdin.read()
        p = Path(file).expanduser()
        if not p.is_file():
            die(f"{what} 文件不存在: {p}")
        return p.read_text(encoding="utf-8")
    return value


def extract_needs(text: str):
    import re
    text = text or ""
    m = re.search(rf"<{NEEDS_TAG}>(.*?)</{NEEDS_TAG}>", text, re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    tail = text.strip()[-600:]
    last = tail.splitlines()[-1].strip() if tail else ""
    hints = ("请告诉我", "请确认", "请选择", "请决定", "需要你来", "需要您来", "你希望", "您希望", "要不要",
             "请指定", "请回复", "请告知", "收到选择", "收到你的", "等你确认", "等待你", "需要你决定", "需要你指定",
             "let me know", "please confirm", "should i", "do you want", "would you like", "please specify")
    if last.endswith(("?", "？")) or any(h in tail.lower() for h in hints):
        return "（子会话没有使用约定格式，以下是它报告的结尾，看起来在等答复）\n" + tail
    return None


def pid_alive(run_dir: Path) -> bool:
    pf = run_dir / "pid"
    if not pf.exists():
        return False
    try:
        os.kill(int(pf.read_text()), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        pf.unlink(missing_ok=True)
        return False


def run_order(p: Path):
    """Sort runs by start time recorded in meta.json (directory mtime is unreliable)."""
    try:
        return json.loads((p / "meta.json").read_text()).get("started") or ""
    except (OSError, json.JSONDecodeError):
        return _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def all_runs() -> list:
    return sorted((p for p in RUNS_DIR.glob("*/") if p.is_dir()), key=run_order) if RUNS_DIR.exists() else []


def resolve_run(arg) -> Path:
    if arg in (None, "", "last"):
        runs = all_runs()
        if not runs:
            die("还没有任何运行记录")
        return runs[-1]
    p = Path(arg).expanduser()
    if not p.is_dir():
        p = RUNS_DIR / arg
    if not p.is_dir():
        die(f"找不到运行记录: {arg}")
    return p


# ---------------------------------------------------------------- app-server ----
class AppServer:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self, cmd, cwd, env, run_dir: Path, on_request, on_notify, *,
                 start_new_session=True, inherit_directory_lock=True):
        self.run_dir = run_dir
        self.start_new_session = start_new_session
        self.log = (run_dir / "events.jsonl").open("a", encoding="utf-8")
        self.err = (run_dir / "stderr.log").open("ab")
        lock_fds = ()
        if inherit_directory_lock and os.environ.get("CLI_DELEGATE_LOCK_FD", "").isdigit():
            lock_fds = (int(os.environ["CLI_DELEGATE_LOCK_FD"]),)  # codex holds the directory lock too
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.err, start_new_session=start_new_session, bufsize=0, pass_fds=lock_fds)
        (run_dir / "pid").write_text(str(self.proc.pid))
        self.lock = threading.Lock()
        self.next_id = 1
        self.waiters: dict = {}
        self.on_request = on_request
        self.on_notify = on_notify
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _write(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            if self.log.closed:
                return
            self.log.write(json.dumps({"dir": "out", **msg}, ensure_ascii=False) + "\n")
            self.log.flush()
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _reader(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                if self.log.closed:
                    break
                self.log.write(json.dumps({"dir": "in", **msg}, ensure_ascii=False) + "\n")
                self.log.flush()
            if "method" in msg and "id" in msg:
                threading.Thread(target=self._serve, args=(msg,), daemon=True).start()
            elif "method" in msg:
                try:
                    self.on_notify(msg["method"], msg.get("params") or {})
                except Exception as e:  # keep reading no matter what
                    print(f"[{ME}] 通知处理出错: {e}", file=sys.stderr)
            elif "id" in msg:
                q = self.waiters.pop(msg["id"], None)
                if q:
                    q.put(msg)
        self.closed.set()

    def _serve(self, msg):
        try:
            result = self.on_request(msg["method"], msg.get("params") or {})
            if result is None:
                self._write({"id": msg["id"], "error": {"code": -32601, "message": "not supported by codex-cli client"}})
            else:
                self._write({"id": msg["id"], "result": result})
        except Exception as e:
            self._write({"id": msg["id"], "error": {"code": -32603, "message": f"client error: {e}"}})

    def request(self, method, params, timeout=120):
        with self.lock:
            mid = self.next_id
            self.next_id += 1
        q: queue.Queue = queue.Queue()
        self.waiters[mid] = q
        self._write({"id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self.waiters.pop(mid, None)
            raise RuntimeError(f"{method} 没有响应（{timeout}s）")
        if "error" in msg:
            raise RuntimeError(f"{method} 失败: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def notify(self, method, params=None):
        self._write({"method": method, "params": params or {}})

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    if self.start_new_session:
                        os.killpg(self.proc.pid, sig)
                    else:
                        self.proc.send_signal(sig)
                    self.proc.wait(timeout=5)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
        self.reader.join(timeout=1)
        (self.run_dir / "pid").unlink(missing_ok=True)
        with self.lock:
            self.log.close()
            self.err.close()


# ------------------------------------------------------------ run state ----
_SHELL_WRAP = None
_SHELL_META = None


def unwrap_shell(cmd: str) -> str:
    """'/bin/zsh -lc "npm test"' -> 'npm test' (Codex wraps commands in a login shell)."""
    import re
    global _SHELL_WRAP
    _SHELL_WRAP = _SHELL_WRAP or re.compile(r"^(?:/bin/|/usr/bin/)?(?:zsh|bash|sh)\s+-l?c\s+(['\"])(.*)\1\s*$", re.S)
    m = _SHELL_WRAP.match((cmd or "").strip())
    return m.group(2).strip() if m else (cmd or "").strip()


def match_rules(rules: list, kind: str, command: str | None = None, paths: list | None = None):
    """Same rule format as the claude backend: decision allow|deny, tool Bash|FILE, prefix/exact or dir."""
    import re
    global _SHELL_META
    _SHELL_META = _SHELL_META or re.compile(r"[;&|`<>]|\$\(|\n")
    protected = any(r.get("decision") == "deny" and r.get("tool") == "FILE" for r in rules or [])
    for r in sorted(rules or [], key=lambda r: 0 if r.get("decision") == "deny" else 1):
        if kind == "command" and protected and r.get("decision") != "deny":
            continue  # a command can write anywhere: with protected paths, commands are never auto-approved
        if kind == "command" and r.get("tool") == "Bash" and r.get("prefix"):
            c = unwrap_shell(command or "")
            if _SHELL_META.search(c):
                continue
            p = r["prefix"]
            if c == p or (not r.get("exact", True) and c.startswith(p + " ")):
                return r
        elif kind == "fileChange" and r.get("tool") == "FILE" and r.get("dir") and paths:
            try:
                base = Path(r["dir"]).expanduser().resolve()
                inside = [Path(x).expanduser().resolve().is_relative_to(base) for x in paths]
            except (OSError, ValueError):
                continue
            # deny: ANY path in the batch is forbidden -> deny the whole batch; allow: ALL paths must be inside
            if (r.get("decision") == "deny" and any(inside)) or (r.get("decision") != "deny" and all(inside)):
                return r
    return None


class RunState:
    def __init__(self, run_dir: Path, approval_timeout: int, rules: list | None = None):
        self.rules = rules or []
        self.run_dir = run_dir
        self.ap = run_dir / "approvals"
        self.ap.mkdir(parents=True, exist_ok=True)
        self.approval_timeout = approval_timeout
        self.items: dict = {}
        self.turn_id = None
        self.turn_done = threading.Event()
        self.turn = None
        self.errors: list = []
        self.rate_limits = None
        self.token_usage = None
        self.agent_texts: list = []
        self.commands: list = []
        self.file_changes: list = []
        self.counter = 0
        self.lock = threading.Lock()
        self.steer = None  # callable(text) set by runner
        self.thread_id = None

    # notifications
    def on_notify(self, method, p):
        if method == "turn/started":
            self.turn_id = self.turn_id or (p.get("turn") or {}).get("id")
        elif method in ("item/started", "item/completed"):
            it = p.get("item") or {}
            if it.get("id"):
                self.items[it["id"]] = it
            if method == "item/completed" and (self.turn_id is None or p.get("turnId") in (None, self.turn_id)):
                t = it.get("type")
                if t == "agentMessage" and it.get("text"):
                    self.agent_texts.append(it["text"])
                elif t == "commandExecution":
                    self.commands.append({"command": it.get("command"), "exitCode": it.get("exitCode"),
                                          "status": it.get("status")})
                elif t == "fileChange":
                    for c in it.get("changes") or []:
                        self.file_changes.append({"path": c.get("path"), "kind": (c.get("kind") or {}).get("type"),
                                                  "status": it.get("status")})
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            if self.turn_id in (None, turn.get("id")):
                self.turn = turn
                self.turn_done.set()
        elif method == "error":
            self.errors.append(p)
        elif method == "account/rateLimits/updated":
            self.rate_limits = p.get("rateLimits")
        elif method == "thread/tokenUsage/updated":
            self.token_usage = p.get("tokenUsage")

    # server -> client requests
    def on_request(self, method, p):
        if method == "item/commandExecution/requestApproval":
            disp = {"command": p.get("command"), "cwd": p.get("cwd"), "reason": p.get("reason"),
                    "network": p.get("networkApprovalContext")}
            rule = None if p.get("networkApprovalContext") else match_rules(self.rules, "command", command=p.get("command"))
            d = self._auto("command", "Command", disp, rule) if rule else self._ask("command", "Command", disp, p)
            if d["behavior"] == "allow":
                return {"decision": "acceptForSession" if d.get("always") else "accept"}
            self._steer_denial(d, f"命令 {short(p.get('command'), 120)}")
            return {"decision": "cancel" if d.get("cancel") else "decline"}
        if method == "item/fileChange/requestApproval":
            item = self.items.get(p.get("itemId")) or {}
            changes = [{"path": c.get("path"), "kind": (c.get("kind") or {}).get("type"),
                        "diff": short(c.get("diff", ""), 4000)} for c in item.get("changes") or []]
            disp = {"changes": changes, "reason": p.get("reason"), "grantRoot": p.get("grantRoot")}
            rule = match_rules(self.rules, "fileChange", paths=[c["path"] for c in changes if c.get("path")])
            d = self._auto("fileChange", "FileChange", disp, rule) if rule else self._ask("fileChange", "FileChange", disp, p)
            if d["behavior"] == "allow":
                return {"decision": "acceptForSession" if d.get("always") else "accept"}
            self._steer_denial(d, "文件修改 " + ", ".join(c["path"] or "?" for c in changes))
            return {"decision": "cancel" if d.get("cancel") else "decline"}
        if method == "item/permissions/requestApproval":
            disp = {"permissions": p.get("permissions"), "reason": p.get("reason"), "cwd": p.get("cwd")}
            d = self._ask("permissions", "Permissions", disp, p)
            if d["behavior"] == "allow":
                return {"permissions": p.get("permissions") or {}, "scope": "session" if d.get("always") else "turn"}
            self._steer_denial(d, "额外权限")
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            disp = {"questions": p.get("questions") or []}
            d = self._ask("question", "request_user_input", disp, p)
            if d["behavior"] == "answer":
                return {"answers": d["answers"]}
            self._steer_denial(d, "提问")
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            d = self._ask("elicitation", "MCP elicitation", p, p)
            if d["behavior"] == "allow":
                return {"action": "accept", "content": d.get("content") or {}}
            return {"action": "decline"}
        return None  # unsupported -> JSON-RPC error

    def _auto(self, kind, tool, display, rule) -> dict:
        with self.lock:
            existing = [int(x.name.split(".")[0]) for x in self.ap.glob("*.request.json") if x.name[:3].isdigit()]
            self.counter = max([self.counter, *existing]) + 1
            rid = f"{self.counter:03d}"
        write_json(self.ap / f"{rid}.request.json", {"id": rid, "kind": kind, "tool_name": tool, "input": display,
                                                      "created": now_iso(), "auto_rule": rule})
        if rule.get("decision") == "deny":
            d = {"behavior": "deny", "by": "rule", "message": rule.get("message") or
                 "项目委派规则（.cli-delegate.toml）禁止这个操作，不要再尝试。"}
        else:
            d = {"behavior": "allow", "by": "rule"}
        write_json(self.ap / f"{rid}.decision.json", {**d, "decided": now_iso(), "rule": rule})
        return d

    def _ask(self, kind, tool, display, raw) -> dict:
        with self.lock:
            existing = [int(x.name.split(".")[0]) for x in self.ap.glob("*.request.json") if x.name[:3].isdigit()]
            self.counter = max([self.counter, *existing]) + 1
            rid = f"{self.counter:03d}"
        req = {"id": rid, "kind": kind, "tool_name": tool, "input": display, "created": now_iso(),
               "timeout_s": self.approval_timeout}
        write_json(self.ap / f"{rid}.request.json", req)
        dp = self.ap / f"{rid}.decision.json"
        deadline = time.time() + self.approval_timeout
        while time.time() < deadline:
            if dp.exists():
                try:
                    return json.loads(dp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        d = {"behavior": "deny", "by": "timeout",
             "message": f"调用方在 {self.approval_timeout}s 内没有批复，按拒绝处理。请不要依赖这一步，继续完成其余部分并在报告中说明。"}
        write_json(dp, d)
        return d

    def _steer_denial(self, d, what):
        msg = d.get("message")
        if msg and self.steer:
            try:
                self.steer(f"[调用方审批] 已拒绝{what}。说明：{msg}")
            except Exception:
                pass


# --------------------------------------------------------------------- run ----
RUN_ONLY_KEYS = {"prompt", "prompt_file", "dry_run", "func", "resume", "sub", "resumed_from", "run",
                 "wait", "max_waits", "id", "answer"}


def check_sensitive(a) -> None:
    """Options that widen what the child may do need the `unsafe` marker (checked on effective values)."""
    found = [n for n, on in (("--access full", getattr(a, "access", None) == "full"),
                             ("--confirm-full", getattr(a, "confirm_full", False))) if on]
    found += [f"-c {kv}" for kv in (getattr(a, "config", None) or [])
              if kv.split("=")[0].strip().startswith(("sandbox", "approval", "permissions", "shell_environment"))]
    if found and os.environ.get("CLI_DELEGATE_UNSAFE_OK") != "1":
        die("敏感参数 " + ", ".join(found) + " 只能经统一入口的 `unsafe` 子命令使用（须用户同意）")


def cmd_run(a) -> int:
    depth = current_depth()
    if depth >= a.max_depth:
        die(f"已经处在第 {depth} 层委派里，拒绝继续嵌套（--max-depth {a.max_depth}）")
    prompt = load_text_arg(a.prompt, a.prompt_file, "任务提示词")
    if prompt is None and not sys.stdin.isatty() and not a.resume:
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        if a.resume:
            prompt = DEFAULT_RESUME_PROMPT
        else:
            die("缺少任务：用 --prompt、--prompt-file 或从 stdin 传入")
    cwd = Path(a.cwd or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir():
        die(f"工作目录不存在: {cwd}")
    add_dirs = []
    for d in a.add_dir:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            die(f"--add-dir 不是目录: {p}")
        add_dirs.append(str(p))
    if a.access == "full" and not a.confirm_full:
        die("--access full 会关闭沙箱且不审批，必须同时加 --confirm-full（且只能在用户明确同意后使用）")
    schema = None
    if a.json_schema:
        js = a.json_schema
        if Path(js).expanduser().is_file():
            js = Path(js).expanduser().read_text(encoding="utf-8")
        try:
            schema = json.loads(js)
        except json.JSONDecodeError as e:
            die(f"--json-schema 不是合法 JSON: {e}")
        a.json_schema = js

    codex = find_bin()
    sandbox, policy, reviewer = TIERS[a.access]
    if a.no_approval and policy == "on-request":
        policy = "never"
    cmd = [codex, "app-server"]
    if policy == "on-request" and reviewer == "user" and not a.no_ask_tool:
        # lets the model ask questions (request_user_input) outside Plan mode; answered by the caller
        cmd += ["-c", "features.default_mode_request_user_input=true"]
    for kv in a.config:
        cmd += ["-c", kv]
    if add_dirs and sandbox == "workspace-write":
        cmd += ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(add_dirs)]
    if a.network and sandbox == "workspace-write":
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    extra_env = {}
    for kv in a.env:
        if "=" not in kv:
            die(f"--env 需要 KEY=VALUE 形式: {kv}")
        k, v = kv.split("=", 1)
        extra_env[k] = v
    env = clean_env(a.codex_home, extra_env, a.pass_env)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.id:
        if not all(c.isalnum() or c in "-_." for c in a.id):
            die("--id 只能包含字母、数字、- _ .")
        run_dir = RUNS_DIR / a.id
        if run_dir.exists():
            die(f"--id 已被占用: {run_dir}")
    else:
        run_dir = RUNS_DIR / f"{stamp}-{secrets.token_hex(3)}"

    extra = (" and these extra directories: " + ", ".join(add_dirs)) if add_dirs else ""
    dev = "" if a.no_worker_prompt else WORKER_PROMPT.format(extra_dirs=extra, tag=NEEDS_TAG)
    extra_sp = load_text_arg(a.append_instructions, a.append_instructions_file, "附加指令")
    a.append_instructions, a.append_instructions_file = extra_sp, None
    if extra_sp:
        dev = (dev + "\n\n" + extra_sp).strip()
    thread_params = {"cwd": str(cwd), "sandbox": sandbox, "approvalPolicy": policy, "approvalsReviewer": reviewer}
    if a.model:
        thread_params["model"] = a.model
    if dev:
        thread_params["developerInstructions"] = dev
    if a.ephemeral and not a.resume:
        thread_params["ephemeral"] = True
    sent = prompt + ("" if a.no_worker_prompt else PROMPT_FOOTER)
    turn_params = {"input": [{"type": "text", "text": sent}]}
    if a.effort:
        turn_params["effort"] = a.effort
    if a.model:
        turn_params["model"] = a.model
    if schema is not None:
        turn_params["outputSchema"] = schema

    meta = {"cwd": str(cwd), "access": a.access, "sandbox": sandbox, "approval_policy": policy,
            "model": a.model, "effort": a.effort, "resume": a.resume, "add_dirs": add_dirs, "timeout": a.timeout,
            "started": now_iso(), "resumed_from": getattr(a, "resumed_from", None),
            "approval": policy == "on-request" and reviewer == "user",
            "codex_home": env.get("CODEX_HOME") or str(HOME / ".codex"),  # actual account dir
            "caller_session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            "opts": {k: v for k, v in vars(a).items() if k not in RUN_ONLY_KEYS}}
    if a.dry_run:
        print("工作目录:", cwd)
        print("命令:", " ".join(shlex.quote(c) for c in cmd))
        print("thread 参数:", json.dumps({k: (short(v, 80) if k == "developerInstructions" else v)
                                         for k, v in thread_params.items()}, ensure_ascii=False))
        print("turn 参数:", json.dumps({k: v for k, v in turn_params.items() if k != "input"}, ensure_ascii=False),
              f"提示词 {len(prompt)} 字")
        print("已剔除的环境变量:", ", ".join(sorted(k for k in os.environ if k not in env)) or "无")
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    a.run_dir = str(run_dir)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    write_json(run_dir / "meta.json", meta)
    print(f"[{ME}] 已启动，运行记录: {run_dir}", file=sys.stderr, flush=True)
    if meta["approval"]:
        print(f"[{ME}] 审批通道已开启：用 `watch {run_dir.name}` 等待审批请求，用 `decide` 批复", file=sys.stderr, flush=True)

    rules = []
    if a.rules_json:
        try:
            rules = json.loads(a.rules_json)
        except json.JSONDecodeError as e:
            die(f"--rules-json 不是合法 JSON: {e}")
    st = RunState(run_dir, a.approval_timeout, rules)
    srv = AppServer(cmd, str(cwd), env, run_dir, st.on_request, st.on_notify)
    t0 = time.time()
    timed_out = False
    startup_error = None
    try:
        srv.request("initialize", {"clientInfo": {"name": "codex-cli-delegate", "title": "codex-cli skill",
                                                  "version": "1.0.0"},
                                   "capabilities": {"experimentalApi": True}}, timeout=60)
        srv.notify("initialized")
        if a.resume:
            rp = {**{k: v for k, v in thread_params.items() if k != "ephemeral"}, "threadId": a.resume}
            res = srv.request("thread/resume", rp, timeout=120)
        else:
            res = srv.request("thread/start", thread_params, timeout=120)
        thread = res.get("thread") or {}
        st.thread_id = thread.get("id")
        meta.update({"thread_id": st.thread_id, "actual_model": res.get("model"),
                     "actual_effort": res.get("reasoningEffort")})
        write_json(run_dir / "meta.json", meta)
        turn_params["threadId"] = st.thread_id
        tres = srv.request("turn/start", turn_params, timeout=120)
        st.turn_id = (tres.get("turn") or {}).get("id") or st.turn_id

        def steer(text):
            srv.request("turn/steer", {"threadId": st.thread_id, "expectedTurnId": st.turn_id,
                                       "input": [{"type": "text", "text": text}]}, timeout=30)
        st.steer = steer
        paused = 0.0  # the timeout does not run while an approval waits for the caller
        while not st.turn_done.is_set():
            if srv.closed.is_set():
                break
            if pending(run_dir):
                paused += 1
            if a.timeout > 0 and time.time() - t0 - paused > a.timeout:
                timed_out = True
                try:
                    srv.request("turn/interrupt", {"threadId": st.thread_id, "turnId": st.turn_id}, timeout=20)
                    st.turn_done.wait(20)
                except Exception:
                    pass
                break
            st.turn_done.wait(1)
        if not st.turn_done.is_set() and srv.closed.is_set():
            startup_error = "codex app-server 意外退出"
    except RuntimeError as e:
        startup_error = str(e)
    except KeyboardInterrupt:
        srv.close()
        raise
    finally:
        srv.close()

    return finish(run_dir, meta, st, timed_out, startup_error, a.max_chars, time.time() - t0)


def parse_limits(st: RunState, text: str):
    rl = st.rate_limits or {}
    wins = [w for w in (rl.get("primary"), rl.get("secondary")) if w and w.get("resetsAt")]
    full = [w["resetsAt"] for w in wins if (w.get("usedPercent") or 0) >= 100]
    if full:
        best = max(full)  # every exhausted window must have reset
    elif wins:
        # nothing at 100% (e.g. a short throttle): use the most-used window, not just the earliest
        best = max(wins, key=lambda w: w.get("usedPercent") or 0)["resetsAt"]
    else:
        best = None
    if best:
        ts = best / 1000 if best > 1e12 else best
        return _dt.datetime.fromtimestamp(ts).astimezone()
    return None


def finish(run_dir, meta, st: RunState, timed_out, startup_error, max_chars, wall) -> int:
    turn = st.turn or {}
    status = turn.get("status")
    err = turn.get("error") or {}
    infos = [str((e.get("error") or {}).get("codexErrorInfo")) for e in st.errors] + [str(err.get("codexErrorInfo"))]
    limited = any(k in " ".join(infos) for k in ("usageLimitExceeded", "rateLimitExceeded"))
    text = st.agent_texts[-1] if st.agent_texts else ""
    needs = extract_needs(text) if status == "completed" else None
    reset = parse_limits(st, err.get("message", "")) if limited else None
    stats = {"allow": 0, "deny": 0, "timeout": 0, "by_user": 0}
    for p in (run_dir / "approvals").glob("*.decision.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        stats["allow" if d.get("behavior") in ("allow", "answer") else "deny"] += 1
        stats["timeout"] += d.get("by") == "timeout"
        stats["by_user"] += d.get("by") == "user"
        stats["by_rule"] = stats.get("by_rule", 0) + (d.get("by") == "rule")
    meta.update({"turn_status": status, "timed_out": timed_out, "usage_limited": limited,
                 "reset_at": reset.isoformat(timespec="minutes") if reset else None,
                 "needs_confirmation": needs, "error": err.get("message") or startup_error,
                 "wall_seconds": round(wall, 1), "approvals": stats, "token_usage": st.token_usage,
                 "rate_limits": st.rate_limits})
    if timed_out:
        rc, label = 3, "超时（已中断）"
    elif startup_error and not status:
        rc, label = (4 if "auth" in startup_error.lower() or "login" in startup_error.lower() else 1), f"失败（{startup_error}）"
    elif limited:
        rc, label = 5, "额度用尽 / 被限流（任务已中断，可能已部分完成；线程已保存，可续跑）"
    elif status == "completed" and needs:
        rc, label = 6, "需要用户确认（见下方「待确认」）"
    elif status == "completed":
        rc, label = 0, "成功"
    else:
        rc, label = 1, f"出错（status={status}, {err.get('codexErrorInfo')}: {short(err.get('message', ''), 200)}）"
    meta["exit_code"] = rc
    write_json(run_dir / "meta.json", meta)
    (run_dir / "result.md").write_text(text, encoding="utf-8")

    L = [f"=== {ME} 结果 ===", f"状态: {label}", f"工作目录: {meta['cwd']}",
         f"权限档位: {meta['access']}（sandbox={meta['sandbox']}, approval={meta['approval_policy']}）"
         f" | 请求模型: {meta.get('model') or '默认'} | 思考强度: {meta.get('effort') or '默认'}"]
    if meta.get("actual_model"):
        L.append(f"实际模型: {meta['actual_model']}（默认强度 {meta.get('actual_effort')}）")
    if meta.get("thread_id"):
        L.append(f"thread_id: {meta['thread_id']}")
    tu = (st.token_usage or {}).get("total") or {}
    parts = [f"耗时 {wall:.1f}s"]
    if tu:
        parts.append(f"tokens in {tu.get('inputTokens')}（缓存 {tu.get('cachedInputTokens')}）/ out {tu.get('outputTokens')}"
                     f"（推理 {tu.get('reasoningOutputTokens')}）")
    rl = st.rate_limits or {}
    pw = rl.get("primary") or {}
    if pw:
        parts.append(f"额度已用 {pw.get('usedPercent')}%（{pw.get('windowDurationMins')} 分钟窗口）")
    L.append(" | ".join(parts))
    if meta.get("approval") or any(stats.values()):
        L.append(f"审批: 批准/回答 {stats['allow']}（其中用户决定 {stats['by_user']}） | 拒绝 {stats['deny']}（其中超时 {stats['timeout']}）")
    if st.commands:
        L.append(f"执行命令 {len(st.commands)} 条（最近 12 条）:")
        for c in st.commands[-12:]:
            L.append(f"  - [{c.get('exitCode')}] {short(c.get('command'), 110)}")
    if st.file_changes:
        L.append(f"文件改动 {len(st.file_changes)} 处:")
        for c in st.file_changes[-20:]:
            L.append(f"  - {c.get('kind')} {c.get('path')} ({c.get('status')})")
    for e in st.errors[-3:]:
        ee = e.get("error") or {}
        L.append(f"警告: {ee.get('codexErrorInfo')}: {short(ee.get('message'), 160)}（willRetry={e.get('willRetry')}）")
    if needs:
        L.append("--- 待确认（主会话能答就答，答不了问用户，然后 resume --answer 续跑）---")
        L.append(needs)
    L.append(f"运行记录: {run_dir}")
    L.append("--- 子会话最终报告 ---")
    body = text or (err.get("message") or startup_error or "(无结果文本)")
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + f"\n…（已截断，全文见 {run_dir / 'result.md'}）"
    L.append(body)
    if (limited or timed_out) and meta.get("thread_id"):
        me = f"python3 {Path(__file__).resolve().parents[2] / 'scripts' / 'cli_delegate.py'}"  # unified entry
        L += ["--- 续跑 ---", f"线程已保存（thread_id {meta['thread_id']}），可在同一线程上继续。"]
        if reset:
            at = reset + _dt.timedelta(minutes=3)
            L.append(f"额度重置时间（本机时间）: {reset.isoformat(timespec='minutes')}")
            L.append(f"建议定时（一次性 CronCreate，本机时区）: cron \"{at.minute} {at.hour} {at.day} {at.month} *\"，recurring=false")
            L.append(f"到点后执行: {me} resume {run_dir.name} --id <新运行名>（后台运行并循环 watch）")
            L.append(f"或现在就后台挂起等待: {me} resume {run_dir.name} --wait")
        else:
            L.append(f"继续执行: {me} resume {run_dir.name}")
    summary = "\n".join(L)
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    return rc


# ------------------------------------------------------------------ resume ----
def cmd_resume(a) -> int:
    run_dir = resolve_run(a.run)
    for attempt in range(a.max_waits + 1):
        meta = json.loads((run_dir / "meta.json").read_text())
        tid = meta.get("thread_id")
        if not tid:
            die("这次运行没有 thread_id（可能没启动成功），不能续跑，请重新 run")
        if meta.get("opts", {}).get("ephemeral"):
            die("这次运行用了 --ephemeral，线程没有保存，不能续跑")
        if pid_alive(run_dir):
            die(f"这次运行还在进行中（{run_dir}），不要重复续跑")
        if a.wait and meta.get("reset_at"):
            target = _dt.datetime.fromisoformat(meta["reset_at"]) + _dt.timedelta(seconds=90)
            secs = (target - _dt.datetime.now().astimezone()).total_seconds()
            if secs > 0:
                print(f"[{ME}] 等待额度重置：{target.isoformat(timespec='minutes')}（约 {secs / 60:.0f} 分钟）",
                      file=sys.stderr, flush=True)
                time.sleep(secs)
        ns = build_parser().parse_args(["run"])
        for k, v in (meta.get("opts") or {}).items():
            if hasattr(ns, k):
                setattr(ns, k, v)
        ns.cwd, ns.resume, ns.resumed_from = meta["cwd"], tid, str(run_dir)
        ns.id = a.id if attempt == 0 else None
        custom = load_text_arg(a.prompt, a.prompt_file, "续跑提示词")
        parts = []
        if a.answer:
            parts.append("对你提出的待确认问题的答复：\n" + a.answer.strip())
        if a.access:
            parts.append(f"权限档位已调整为 {a.access}，之前被拒绝、但完成任务必需的步骤可以重试。")
        if parts and not custom:
            parts.append("请据此继续完成原任务，不要重复已经完成的部分，最后给出完整的最终报告。")
        ns.prompt = custom or ("\n\n".join(parts) if parts else DEFAULT_RESUME_PROMPT)
        ns.prompt_file = None
        if a.rules_json is not None:
            ns.rules_json = a.rules_json
        for k in ("model", "effort", "timeout", "approval_timeout", "access"):
            if getattr(a, k) is not None:
                setattr(ns, k, getattr(a, k))
        ns.confirm_full = a.confirm_full or ns.confirm_full
        check_sensitive(ns)  # merged options of the original run count too
        rc = cmd_run(ns)
        if rc == 5 and a.wait and attempt < a.max_waits and getattr(ns, "run_dir", None):
            run_dir = Path(ns.run_dir)
            if not json.loads((run_dir / "meta.json").read_text()).get("reset_at"):
                return rc
            continue
        return rc
    return 5


# --------------------------------------------------------------- approvals ----
def pending(run_dir: Path) -> list:
    ap = run_dir / "approvals"
    out = []
    if ap.is_dir():
        for p in sorted(ap.glob("*.request.json")):
            rid = p.name.split(".")[0]
            if not (ap / f"{rid}.decision.json").exists():
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
    return out


def fmt_request(r: dict) -> str:
    kind, inp = r.get("kind"), r.get("input") or {}
    names = {"command": "命令审批", "fileChange": "文件修改审批", "permissions": "额外权限审批",
             "question": "提问", "elicitation": "MCP 请求"}
    L = [f"[{r['id']}] {names.get(kind, kind)} · {r.get('created')}"]
    if kind == "command":
        L.append(f"  命令: {inp.get('command')}")
        L.append(f"  目录: {inp.get('cwd')}")
        if inp.get("reason"):
            L.append(f"  理由: {inp.get('reason')}")
        if inp.get("network"):
            L.append(f"  网络: {json.dumps(inp['network'], ensure_ascii=False)}")
    elif kind == "fileChange":
        if inp.get("reason"):
            L.append(f"  理由: {inp.get('reason')}")
        for c in inp.get("changes") or []:
            L.append(f"  - {c.get('kind')} {c.get('path')}")
            for dl in (c.get("diff") or "").splitlines()[:30]:
                L.append(f"      {dl}")
    elif kind == "question":
        for q in inp.get("questions") or []:
            L.append(f"  Q[{q.get('id')}]: {q.get('question')}" + ("（可自由回答）" if q.get("isOther") else ""))
            for o in q.get("options") or []:
                L.append(f"     - {o.get('label')}: {o.get('description', '')}")
    else:
        L.append("  " + short(json.dumps(inp, ensure_ascii=False), 800))
    return "\n".join(L)


def cmd_watch(a) -> int:
    if a.run not in (None, "", "last"):
        for _ in range(60):
            if Path(a.run).expanduser().is_dir() or (RUNS_DIR / a.run).is_dir():
                break
            time.sleep(0.5)
    run_dir = resolve_run(a.run)
    start = time.time()
    while True:
        pend = pending(run_dir)
        if pend:
            print(f"=== {run_dir.name}: {len(pend)} 个请求等待主会话审批 ===")
            for r in pend:
                print(fmt_request(r))
            print(f"\n批复: python3 {Path(__file__).resolve()} decide {run_dir.name} <编号> allow|deny|answer ...")
            return 10
        s = run_dir / "summary.txt"
        if s.exists() and not pid_alive(run_dir):
            print(s.read_text(encoding="utf-8"))
            meta = json.loads((run_dir / "meta.json").read_text())
            return 0 if meta.get("exit_code") == 0 else 1
        if not pid_alive(run_dir) and time.time() - start > 20 and not s.exists():
            print(f"{run_dir.name}: 进程已不在运行，也没有结果（可能被中断）。可用 resume 续跑。")
            return 1
        if time.time() - start > a.max_wait:
            print(f"{run_dir.name}: 仍在运行，暂无待批请求。再次执行 watch 继续等待。")
            return 11
        time.sleep(1)


def cmd_decide(a) -> int:
    run_dir = resolve_run(a.run)
    ap = run_dir / "approvals"
    if a.req == "all":
        reqs = pending(run_dir)
        if a.decision != "deny":
            die("all 只能用于 deny")
    else:
        p = ap / f"{a.req}.request.json"
        if not p.exists():
            die(f"没有这个请求: {p}")
        if (ap / f"{a.req}.decision.json").exists():
            die(f"请求 {a.req} 已经批复过了")
        reqs = [json.loads(p.read_text(encoding="utf-8"))]
    if not reqs:
        die("没有待批请求")
    for r in reqs:
        rid, kind, inp = r["id"], r.get("kind"), r.get("input") or {}
        if a.decision == "answer":
            if kind != "question":
                die(f"[{rid}] 不是提问，不能用 answer")
            if not a.answers:
                die("answer 需要 --answers JSON")
            try:
                given = json.loads(a.answers)
            except json.JSONDecodeError as e:
                die(f"--answers 不是合法 JSON: {e}")
            out = {}
            for q in inp.get("questions") or []:
                v = given.get(q.get("id"), given.get(q.get("question")))
                if v is None:
                    die(f"[{rid}] 缺少问题 {q.get('id')!r}（{q.get('question')}）的答案；键用问题 id 或问题原文")
                out[q["id"]] = {"answers": [str(x) for x in v] if isinstance(v, list) else [str(v)]}
            d = {"behavior": "answer", "answers": out}
        elif a.decision == "allow":
            if kind == "question":
                die(f"[{rid}] 是提问：请用 answer --answers 回答，或用 deny 拒绝")
            d = {"behavior": "allow", "always": bool(a.always)}
        else:
            d = {"behavior": "deny", "message": a.message or "", "cancel": bool(a.cancel)}
        if a.message and d["behavior"] != "deny":
            d["note"] = a.message
        d.update({"by": a.by, "decided": now_iso()})
        write_json(ap / f"{rid}.decision.json", d)
        print(f"[{rid}] {kind}: {d['behavior']}" + ("（本会话内同类请求自动批准）" if d.get("always") else ""))
    return 0


# ------------------------------------------------------------------ others ----
def cmd_show(a) -> int:
    run_dir = resolve_run(a.run)
    s = run_dir / "summary.txt"
    if s.exists():
        print(s.read_text(encoding="utf-8"))
        return 0
    return cmd_progress(a)


def cmd_progress(a) -> int:
    run_dir = resolve_run(a.run)
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    print(f"运行记录: {run_dir}")
    print(f"状态: {'运行中' if pid_alive(run_dir) else '已结束或已中断'}  thread_id: {meta.get('thread_id')}")
    print(f"工作目录: {meta.get('cwd')}  开始于 {meta.get('started')}")
    cmds, last = [], ""
    for ev in read_jsonl(run_dir / "events.jsonl"):
        if ev.get("method") == "item/completed":
            it = (ev.get("params") or {}).get("item") or {}
            if it.get("type") == "commandExecution":
                cmds.append(f"[{it.get('exitCode')}] {short(it.get('command'), 110)}")
            elif it.get("type") == "fileChange":
                cmds.append("改文件: " + ", ".join(c.get("path", "?") for c in it.get("changes") or []))
            elif it.get("type") == "agentMessage":
                last = it.get("text") or last
    print(f"已完成的操作 {len(cmds)} 个，最近 {a.n} 个:")
    for c in cmds[-a.n:]:
        print("  - " + c)
    if last:
        print("最近一段文字: " + short(last, 400))
    pend = pending(run_dir)
    if pend:
        print(f"待批请求 {len(pend)} 个（用 watch 查看）")
    return 0


def cmd_list(a) -> int:
    runs = all_runs()
    for r in runs[-a.n:]:
        meta = json.loads((r / "meta.json").read_text()) if (r / "meta.json").exists() else {}
        prompt = (r / "prompt.md").read_text(errors="replace") if (r / "prompt.md").exists() else ""
        if pid_alive(r):
            state = "运行中" + (f"，待批 {len(pending(r))}" if pending(r) else "")
        elif meta.get("timed_out"):
            state = "超时"
        elif meta.get("usage_limited"):
            state = f"额度用尽 重置 {meta.get('reset_at') or '?'}"
        elif meta.get("needs_confirmation"):
            state = "待确认"
        else:
            state = "成功" if meta.get("exit_code") == 0 else f"exit={meta.get('exit_code')}"
        print(f"{r.name}  [{state}]  thread={meta.get('thread_id') or '-'}  cwd={meta.get('cwd')}  {short(prompt, 50)}")
    if not runs:
        print("还没有任何运行记录")
    return 0


def cmd_doctor(a) -> int:
    ok = True
    codex = find_bin()
    env = clean_env(a.codex_home, {}, [])
    v = subprocess.run([codex, "--version"], capture_output=True, text=True, env=env)
    print(f"CLI: {codex}  版本: {(v.stdout or v.stderr).strip()}")
    print(f"CODEX_HOME: {env.get('CODEX_HOME', str(HOME / '.codex'))}")
    s = subprocess.run([codex, "login", "status"], capture_output=True, text=True, env=env)
    out = (s.stdout + s.stderr).strip()
    print(f"登录: {out.splitlines()[-1] if out else '?'}")
    if s.returncode != 0:
        ok = False
        print("未登录：让用户在终端运行  codex login（浏览器里选 Sign in with ChatGPT，用订阅账号登录）")
    tmp = RUNS_DIR.parent / "doctor"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "events.jsonl").write_text("")
    srv = AppServer([codex, "app-server"], str(HOME), env, tmp, lambda m, p: None, lambda m, p: None)
    try:
        srv.request("initialize", {"clientInfo": {"name": "codex-cli-doctor", "version": "1.0.0"},
                                   "capabilities": {"experimentalApi": True}}, timeout=60)
        srv.notify("initialized")
        ml = srv.request("model/list", {}, timeout=60)
        print("可用模型:")
        for m in ml.get("data") or []:
            effs = ",".join(e.get("reasoningEffort", "?") for e in m.get("supportedReasoningEfforts") or [])
            print(f"  - {m.get('model') or m.get('id')}{' (默认)' if m.get('isDefault') else ''}"
                  f"  强度: {effs}  默认 {m.get('defaultReasoningEffort')}")
        try:
            rl = srv.request("account/rateLimits/read", {}, timeout=60).get("rateLimits") or {}
            for name in ("primary", "secondary"):
                w = rl.get(name) or {}
                if w:
                    ra = w.get("resetsAt")
                    ra = _dt.datetime.fromtimestamp(ra / 1000 if ra and ra > 1e12 else ra).strftime("%m-%d %H:%M") if ra else "?"
                    print(f"额度 {name}: 已用 {w.get('usedPercent')}%（{w.get('windowDurationMins')} 分钟窗口，{ra} 重置）")
        except RuntimeError as e:
            print("额度查询失败:", e)
    except RuntimeError as e:
        ok = False
        print("app-server 检查失败:", e)
    finally:
        srv.close()
    removed = sorted(k for k in os.environ if k not in env)
    print(f"会剔除的宿主环境变量 {len(removed)} 个")
    print("结论:", "可用" if ok else "有问题，见上")
    return 0 if ok else 4


# -------------------------------------------------------------------- main ----
def build_parser():
    p = argparse.ArgumentParser(prog="codex_delegate.py", description="在 Claude 会话里调用本机 Codex CLI 执行任务",
                                allow_abbrev=False)
    sub = p.add_subparsers(dest="sub")
    r = sub.add_parser("run", help="执行任务", allow_abbrev=False)
    g = r.add_argument_group("任务")
    g.add_argument("--prompt", "-p")
    g.add_argument("--prompt-file", help="从文件读提示词，'-' 表示 stdin")
    g.add_argument("--cwd", help="工作目录（传调用会话的主工作目录）")
    g.add_argument("--add-dir", action="append", default=[], help="额外可写目录（workspace-write 下生效）")
    g.add_argument("--id", help="运行记录名（字母数字 - _ .），便于 watch/decide 引用")
    g = r.add_argument_group("模型")
    g.add_argument("--model", "-m", help="模型 id（doctor 列出可用模型）")
    g.add_argument("--effort", "-e", help="思考强度（如 minimal/low/medium/high/xhigh，按模型支持，doctor 可查）")
    g = r.add_argument_group("权限与限制")
    g.add_argument("--access", "-a", choices=ACCESS, default="edit",
                   help="read=只读沙箱不审批; edit=工作区可写、越出沙箱的操作交主会话审批（默认）; "
                        "strict=只读沙箱、所有写入和越权命令交主会话审批; auto=Codex 自动审查员审批; full=无沙箱不审批（需 --confirm-full）")
    g.add_argument("--confirm-full", action="store_true")
    g.add_argument("--no-approval", action="store_true", help="不审批：越出沙箱的操作直接失败（无人值守时用）")
    g.add_argument("--no-ask-tool", action="store_true",
                   help="不开启 request_user_input 提问工具（默认在审批档位开启，由主会话回答）")
    g.add_argument("--network", action="store_true", help="workspace-write 沙箱内允许联网（须在用户授权范围内）")
    g.add_argument("--approval-timeout", type=int, default=1800, help="单个审批等待秒数，超时按拒绝，默认 1800")
    g.add_argument("--timeout", type=int, default=3600, help="整体超时秒数，默认 3600，0 为不限")
    g.add_argument("--max-depth", type=int, default=1)
    g = r.add_argument_group("会话与输出")
    g.add_argument("--resume", "-r", help="按 thread_id 续上之前的线程")
    g.add_argument("--ephemeral", action="store_true", help="不保存线程（之后无法续跑）")
    g.add_argument("--no-worker-prompt", action="store_true")
    g.add_argument("--append-instructions", help="追加给子会话的开发者指令")
    g.add_argument("--append-instructions-file")
    g.add_argument("--json-schema", help="最终回复的 JSON Schema（字符串或文件）")
    g.add_argument("--config", "-c", action="append", default=[], help="透传给 codex 的 -c key=value，可多次")
    g.add_argument("--rules-json", help="项目审批规则（JSON 数组，由 cli-delegate 入口从 .cli-delegate.toml 生成）")
    g.add_argument("--codex-home", default=os.environ.get("CODEXCLI_HOME"), help="使用另一个 CODEX_HOME（如另一个账号）")
    g.add_argument("--env", action="append", default=[])
    g.add_argument("--pass-env", action="append", default=[])
    g.add_argument("--max-chars", type=int, default=12000)
    g.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="在之前的线程上续跑（带答复 / 额度用尽 / 超时 / 追加任务）", allow_abbrev=False)
    rs.add_argument("run", nargs="?", default="last")
    rs.add_argument("--prompt", "-p")
    rs.add_argument("--prompt-file")
    rs.add_argument("--answer", help="对「待确认」问题的答复")
    rs.add_argument("--wait", action="store_true", help="先等到额度重置再跑")
    rs.add_argument("--max-waits", type=int, default=3)
    rs.add_argument("--model", "-m", default=None)
    rs.add_argument("--effort", "-e", default=None)
    rs.add_argument("--timeout", type=int, default=None)
    rs.add_argument("--approval-timeout", type=int, default=None, help="覆盖单个审批的等待秒数")
    rs.add_argument("--access", choices=ACCESS, default=None, help="改变权限档位（须在用户授权范围内）")
    rs.add_argument("--confirm-full", action="store_true")
    rs.add_argument("--id")
    rs.add_argument("--rules-json", default=None, help="当前的项目审批规则（由入口重新读取后传入）")
    rs.set_defaults(func=cmd_resume)

    w = sub.add_parser("watch", help="等待审批请求或任务结束")
    w.add_argument("run", nargs="?", default="last")
    w.add_argument("--max-wait", type=int, default=540)
    w.set_defaults(func=cmd_watch)

    dc = sub.add_parser("decide", help="批复审批请求")
    dc.add_argument("run")
    dc.add_argument("req", help="请求编号，如 001；all 只能配合 deny")
    dc.add_argument("decision", choices=["allow", "deny", "answer"])
    dc.add_argument("--message", "-m", help="拒绝理由与替代做法（会作为消息发给子会话）")
    dc.add_argument("--answers", help='answer 用：JSON，{"问题id或原文": "选项label或自由文本"}，多选用数组')
    dc.add_argument("--always", action="store_true", help="allow 时本线程内同类请求自动批准（acceptForSession）")
    dc.add_argument("--cancel", action="store_true", help="deny 时同时中断本轮")
    dc.add_argument("--by", default="caller", choices=["caller", "user"])
    dc.set_defaults(func=cmd_decide)

    d = sub.add_parser("doctor", help="检查 CLI、登录、模型与额度")
    d.add_argument("--codex-home", default=os.environ.get("CODEXCLI_HOME"))
    d.set_defaults(func=cmd_doctor)
    for name, fn, n in (("show", cmd_show, 12), ("progress", cmd_progress, 15)):
        s = sub.add_parser(name)
        s.add_argument("run", nargs="?", default="last")
        s.add_argument("-n", type=int, default=n)
        s.set_defaults(func=fn)
    ls = sub.add_parser("list")
    ls.add_argument("-n", type=int, default=15)
    ls.set_defaults(func=cmd_list)
    return p


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("run", "resume", "watch", "decide", "doctor", "show", "progress", "list",
                                   "-h", "--help"):
        argv = ["run", *argv]
    a = build_parser().parse_args(argv)
    check_sensitive(a)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
