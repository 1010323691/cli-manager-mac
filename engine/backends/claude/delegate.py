#!/usr/bin/env python3
"""cli-delegate claude backend: run a task with the Claude Code CLI (`claude -p`, non-interactive).

Subcommands:
  run       Run a task in the caller's working directory (default subcommand).
  doctor    Check the CLI, the Claude Code CLI's login, and the cleaned environment.
  resume    Continue an interrupted run (usage limit / timeout) on the same session.
  show      Print the summary of a finished run directory.
  progress  Print what a running (or finished) run has done so far.
  list      List recent runs.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- settings ----
HOME = Path.home()
STATE_DIR = Path(os.environ.get("CLI_DELEGATE_STATE", HOME / ".cli-delegate")).expanduser()


def _config_dir() -> Path | None:
    """Claude Code CLI config dir to use: CLAUDECLI_CONFIG_DIR, else `claude_config_dir` in
    ~/.cli-delegate/config.json, else None (the CLI's own default). The CLI is a separate client
    from the Desktop app; the env cleaning below keeps the Desktop session's credentials out."""
    v = os.environ.get("CLAUDECLI_CONFIG_DIR")
    if not v:
        try:
            v = json.loads((STATE_DIR / "config.json").read_text(encoding="utf-8")).get("claude_config_dir")
        except (OSError, json.JSONDecodeError):
            v = None
    return Path(v).expanduser() if v else None


CONFIG_DIR = _config_dir()
CLAUDE_BIN = os.environ.get("CLAUDECLI_BIN") or str(HOME / ".local/bin/claude")
RUNS_DIR = STATE_DIR / "claude-runs"
# Shared by both backends (neither strips it), so claude->codex->claude chains are counted too.
DEPTH_VAR = "CLI_DELEGATE_DEPTH"
LEGACY_DEPTH_VARS = ("CODEXCLI_DELEGATE_DEPTH",)


def migrate_runs() -> int:
    """Move run records from older locations into RUNS_DIR, run by run (never at import time,
    never during a dry run). Skips runs that are still active and names that already exist."""
    olds = []
    legacy = os.environ.get("CLAUDE2_CONFIG_DIR")  # custom dir of the old claude2-delegate version
    for base in (CONFIG_DIR, Path(legacy).expanduser() if legacy else None, HOME / ".claude-account2", HOME / ".claude"):
        if base and (base / "delegate-runs").is_dir() and (base / "delegate-runs") not in olds:
            olds.append(base / "delegate-runs")
    moved = 0
    for old in olds:
        for d in sorted(p for p in old.iterdir() if p.is_dir()):
            if (d / "pid").exists() or (RUNS_DIR / d.name).exists():
                continue
            try:
                RUNS_DIR.mkdir(parents=True, exist_ok=True)
                d.rename(RUNS_DIR / d.name)
                moved += 1
            except OSError:
                pass
    return moved

EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultracode"]
ACCESS = ["read", "edit", "strict", "auto", "full"]
APPROVAL_TIERS = ("edit", "strict", "auto")
# acceptEdits auto-approves filesystem commands (rm, mv, ...) inside the working dir.
# These "ask" rules force them through the caller's review instead (verified v2.1.329).
ASK_RULES = [
    "Bash(rm *)", "Bash(rmdir *)", "Bash(mv *)", "Bash(cp *)", "Bash(ln *)", "Bash(chmod *)", "Bash(chown *)",
    "Bash(truncate *)", "Bash(dd *)", "Bash(sed -i *)", "Bash(find * -delete*)", "Bash(xargs *)",
    "Bash(git commit *)", "Bash(git reset *)", "Bash(git clean *)", "Bash(git checkout *)", "Bash(git restore *)",
    "Bash(git rm *)", "Bash(git stash *)", "Bash(git rebase *)", "Bash(git merge *)", "Bash(git branch -D *)",
    "Bash(git tag *)", "Bash(git remote *)", "Bash(git config *)",
]
READ_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,TodoWrite"
DEFAULT_DENY = ["Bash(git push *)", "Bash(git push)"]
# Tools that schedule work that outlives the run, or reach the user / other sessions
# from the Claude Code CLI. Always removed from the child. (AskUserQuestion is NOT
# blocked: with the approval bridge it is answered by the caller session.)
BLOCKED_TOOLS = ["ExitPlanMode", "EnterPlanMode", "CronCreate", "CronDelete",
                 "ScheduleWakeup", "RemoteTrigger", "PushNotification", "SendMessage"]
NEEDS_TAG = "needs-confirmation"
ASK_RULE_APPROVAL = (
    "Make minor choices yourself and state them in the report. When you need a decision you "
    "should not make alone (an ambiguity whose choices lead to materially different results, an "
    "irreversible or destructive step), ask with the AskUserQuestion tool: the caller session "
    "answers it, consulting the user when needed.")
ASK_RULE_NO_APPROVAL = (
    "Nobody can answer questions during this run and AskUserQuestion is unavailable. Make "
    "reasonable, conservative choices and state the assumptions in your final report.")
BRIDGE = Path(__file__).resolve().parent / "approval_mcp.py"
BRIDGE_TOOL = "mcp__cliapproval__permission_prompt"

# Env vars injected by the calling Claude session / Desktop host. They would make
# the child use the caller's account, gateway, effort or session plumbing, so they
# are removed before starting the child.
STRIP_PREFIXES = (
    "CLAUDE", "ANTHROPIC_", "MCP_", "OTEL_", "DISABLE_", "ENABLE_",
    "MAX_THINKING", "MAX_MCP", "API_TIMEOUT", "BASH_DEFAULT_TIMEOUT", "BASH_MAX_",
    "AWS_BEARER_TOKEN_BEDROCK", "VERTEX_", "CLOUD_ML_", "ANTHROPIC",
)
STRIP_EXACT = {"CLAUDECODE", "NODE_OPTIONS", "DEBUG", "CLI_DELEGATE_UNSAFE_OK", "CLI_DELEGATE_LOCK_FD", "CLI_DELEGATE_QUEUE_JOB",
               "CLI_DELEGATE_QUEUE_RUN", "CLI_DELEGATE_QUEUE_TOKEN"}  # plumbing must not reach the child

WORKER_PROMPT = """\
You are a delegated worker started non-interactively by another Claude session \
(the "caller") on behalf of the same user. Rules:
- {ask_rule}
- Work only inside the working directory{extra_dirs}. Do not touch files elsewhere.
- Keep the task hand-over-ready: before each major step, write one short line \
starting with `PROGRESS:` (what is done so far, what you do next). If you are \
interrupted (e.g. by a usage limit), these lines are how another agent continues.
- Do only what the task asks. No unrelated refactors, no new dependencies unless \
the task asks for them.
- Do not commit, push, open PRs, publish, send messages or delete data unless the \
task explicitly says so.
- Permission requests are reviewed by the caller session, which may take a \
while. If a tool call is denied, read the denial message, do not try to get \
around it; work with what is allowed and list what was blocked in your report.
- End with a concise final report for the caller: what you did, files \
created/changed (paths), commands run and their results, anything unfinished or \
unverified, and assumptions. Write the report in the same language as the task.
- If a decision still blocks you (or you lack credentials/access the task cannot do without): do \
everything else you can first, do NOT guess on that point, and put this block \
at the very end of your final report:
<{tag}>
1. <question> | options: A) ... B) ... | recommended: A | why it matters: ...
2. ...
</{tag}>
The caller will ask the user and resume this same session with the answer. \
Do not use the block for minor choices you can make yourself.\
"""


# ------------------------------------------------------------------ helpers ----
def die(msg: str, code: int = 2) -> None:
    print(f"[claude-cli] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def clean_env(extra: dict[str, str], passthrough: list[str], config_dir_override: str | None = None) -> dict[str, str]:
    env = {}
    keep = set(passthrough)
    for k, v in os.environ.items():
        if k in keep:
            env[k] = v
            continue
        if k in STRIP_EXACT or k.startswith(STRIP_PREFIXES):
            continue
        env[k] = v
    cfg_dir = config_dir_override or CONFIG_DIR
    if cfg_dir:
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    env[DEPTH_VAR] = str(current_depth() + 1)
    env.update(extra)
    return env


def current_depth() -> int:
    vals = []
    for k in (DEPTH_VAR, *LEGACY_DEPTH_VARS):
        try:
            vals.append(int(os.environ.get(k, "0") or 0))
        except ValueError:
            pass
    return max(vals or [0])


def find_bin() -> str:
    b = CLAUDE_BIN
    if not (os.path.isfile(b) and os.access(b, os.X_OK)):
        b = shutil.which("claude") or ""
    if not b:
        die("找不到 claude CLI（默认 ~/.local/bin/claude，可用环境变量 CLAUDECLI_BIN 指定）", 4)
    return b


def read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_brief(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if key in inp and inp[key]:
            return f"{name}: {short(inp[key], 100)}"
    return name


_ASKING_HINTS = ("请告诉我", "请确认", "请选择", "请决定", "需要你来", "需要您来", "需要你确认", "需要您确认",
                 "你希望", "您希望", "你想要", "您想要", "要不要", "是否需要我", "等你", "等您",
                 "请指定", "请回复", "请告知", "收到选择", "收到你的", "等你确认", "等待你", "需要你决定", "需要你指定",
                 "let me know", "please confirm", "should i", "do you want", "would you like", "which do you", "please specify")


def extract_needs(text: str) -> str | None:
    """Return the questions the child left for the user.

    Prefers the agreed <needs-confirmation> block; otherwise falls back to spotting a
    report that ends by asking the user something (models sometimes ignore the format).
    """
    import re
    text = text or ""
    m = re.search(rf"<{NEEDS_TAG}>(.*?)</{NEEDS_TAG}>", text, re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    tail = text.strip()[-600:]
    last = tail.splitlines()[-1].strip() if tail else ""
    if last.endswith(("?", "？")) or any(h in tail.lower() for h in _ASKING_HINTS):
        return "（子会话没有使用约定格式，以下是它报告的结尾，看起来在等用户答复）\n" + tail
    return None


PROMPT_FOOTER = (
    "\n\n---\n（委派运行说明：你的权限请求和 AskUserQuestion 提问由调用方会话审核回答。能自己合理决定的就自己决定并在报告里说明；"
    "确实需要别人拍板的，用 AskUserQuestion 提问，不要只在文字里提问后结束。"
    f"只有在提问不可用或被拒、又无法继续时，才在最终报告的最末尾输出 <{NEEDS_TAG}> … </{NEEDS_TAG}> 块，格式见系统提示。）"
)
PROMPT_FOOTER_NO_APPROVAL = (
    "\n\n---\n（委派运行说明：本次运行中没有人能回答你的问题，需要批准的操作会被自动拒绝。能自己合理决定的就自己决定并在报告里说明；"
    f"确实必须由用户决定的，先把其余能做的做完，再在最终报告的最末尾输出 <{NEEDS_TAG}> … </{NEEDS_TAG}> 块，格式见系统提示。）"
)


_MULTI_WORD = {"git", "npm", "pnpm", "yarn", "npx", "cargo", "go", "docker", "kubectl", "pip", "pip3",
               "python", "python3", "uv", "poetry", "make", "gh", "brew", "bun", "deno", "dotnet", "mvn", "gradle"}


def suggest_rules(denials: list[dict]) -> list[str]:
    """Turn permission denials into allow-rule suggestions (one per simple command prefix)."""
    import re
    rules: list[str] = []
    for d in denials:
        name = d.get("tool_name", "?")
        inp = d.get("tool_input") or {}
        if name == "Bash" and inp.get("command"):
            for part in re.split(r"&&|\|\||;|\||\n", inp["command"]):
                words = [w for w in part.strip().split() if not re.match(r"^\w+=", w)]
                words = [w for w in words if w not in ("(", ")")]
                if not words:
                    continue
                w0 = words[0].lstrip("(")
                prefix = f"{w0} {words[1]}" if w0 in _MULTI_WORD and len(words) > 1 and not words[1].startswith("-") else w0
                rule = f"Bash({prefix} *)"
                if rule not in rules:
                    rules.append(rule)
        elif name not in rules:
            rules.append(name)
    return rules


def summarize_events(events_path: Path) -> dict:
    """Collect init info, tool calls, last assistant text and the result message."""
    info = {"init": None, "tools": [], "result": None, "last_text": "", "errors": []}
    for ev in read_jsonl(events_path):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            info["init"] = ev
        elif t == "system" and ev.get("subtype") in ("api_retry",):
            info["errors"].append(f"api_retry: {short(ev.get('error', ''), 120)}")
        elif t == "assistant" and not ev.get("parent_tool_use_id"):
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    info["tools"].append(tool_brief(block.get("name", "?"), block.get("input") or {}))
                elif block.get("type") == "text" and block.get("text", "").strip():
                    info["last_text"] = block["text"]
        elif t == "result":
            info["result"] = ev
    return info


def fmt_summary(run_dir: Path, meta: dict, info: dict, max_chars: int, exit_code, timed_out: bool) -> str:
    res = info.get("result") or {}
    init = info.get("init") or {}
    lines = ["=== claude-cli 结果 ==="]
    if timed_out:
        status = "超时（已中断）"
    elif not res:
        status = f"失败（无 result 消息，进程退出码 {exit_code}）"
    elif res.get("api_error") == "usage_limit_reached" or res.get("api_error_type") == "rate_limit_error":
        status = "Claude CLI 账号额度用尽 / 被限流（任务已中断，可能已部分完成；会话已保存，可续跑）"
    elif res.get("subtype") == "error_max_turns":
        status = "达到 --max-turns 上限，任务可能未完成"
    elif res.get("subtype") == "error_max_budget_usd":
        status = "达到 --max-budget-usd 上限，任务可能未完成"
    elif res.get("is_error") or res.get("subtype") not in (None, "success"):
        detail = ", ".join(f"{k}={res[k]}" for k in ("subtype", "terminal_reason", "api_error_status", "api_error") if res.get(k))
        status = f"出错（{detail}）"
    elif extract_needs(res.get("result") or ""):
        status = "需要用户确认（子会话停在需要用户决定的地方，见下方「待确认」）"
    else:
        status = "成功"
    lines.append(f"状态: {status}")
    lines.append(f"工作目录: {meta.get('cwd')}")
    if init.get("cwd") and Path(init["cwd"]).resolve() != Path(meta.get("cwd", "")).resolve():
        lines.append(f"警告: 子会话实际工作目录为 {init['cwd']}，与请求不一致!")
    lines.append(f"权限档位: {meta.get('access')}  | 请求模型: {meta.get('model') or '默认'}  | 思考强度: {meta.get('effort') or '默认'}")
    if init.get("model"):
        lines.append(f"实际主模型: {init.get('model')}")
    mu = res.get("modelUsage") or {}
    if mu:
        lines.append("各模型用量: " + ", ".join(
            f"{m}(in {u.get('inputTokens', 0)}, out {u.get('outputTokens', 0)})" for m, u in mu.items()))
    sid = res.get("session_id") or init.get("session_id")
    if sid:
        lines.append(f"session_id: {sid}   （续聊: --resume {sid}）")
    parts = []
    if res.get("num_turns") is not None:
        parts.append(f"轮数 {res['num_turns']}")
    if res.get("duration_ms") is not None:
        parts.append(f"耗时 {res['duration_ms'] / 1000:.1f}s")
    if res.get("total_cost_usd") is not None:
        parts.append(f"折算费用 ${res['total_cost_usd']:.4f}（订阅账号仅供参考）")
    if parts:
        lines.append(" | ".join(parts))
    if meta.get("approval"):
        st = approval_stats(run_dir)
        lines.append(f"审批: 批准 {st['allow']}（其中用户决定 {st['by_user']}） | 拒绝 {st['deny']}（其中超时 {st['timeout']}）"
                     f" | 按规则自动批准 {st['auto']} / 自动拒绝 {st['rule_deny']}")
    denials = res.get("permission_denials") or []
    if denials:
        lines.append(f"被拒绝的工具调用 ({len(denials)}):")
        for d in denials[:15]:
            lines.append("  - " + tool_brief(d.get("tool_name", "?"), d.get("tool_input") or {}))
        rules = suggest_rules(denials)
        if rules:
            lines.append("  放行建议（需用户确认后才能用）: " + " ".join(f'--allow "{r}"' for r in rules))
    if info["tools"]:
        lines.append(f"工具调用 {len(info['tools'])} 次（最近 12 次）:")
        for tb in info["tools"][-12:]:
            lines.append("  - " + tb)
    for e in info["errors"][-5:]:
        lines.append("警告: " + e)
    if res.get("structured_output") is not None:
        lines.append("structured_output:")
        lines.append(json.dumps(res["structured_output"], ensure_ascii=False, indent=2))
    text = res.get("result") if res else info.get("last_text")
    if not text:
        err_tail = ""
        ep = run_dir / "stderr.log"
        if ep.exists():
            err_tail = ep.read_text(errors="replace")[-1500:]
        text = "(无结果文本)" + (f"\nstderr 末尾:\n{err_tail}" if err_tail.strip() else "")
    needs = extract_needs(text)
    if needs:
        lines.append("--- 待确认（请主会话向用户确认后用 resume --answer 续跑）---")
        lines.append(needs)
    lines.append(f"运行记录: {run_dir}")
    lines.append("--- 子会话最终报告 ---")
    if max_chars and len(text) > max_chars:
        lines.append(text[:max_chars])
        lines.append(f"…（已截断，全文见 {run_dir / 'result.md'}）")
    else:
        lines.append(text)
    return "\n".join(lines)


def load_text_arg(value: str | None, file: str | None, what: str) -> str | None:
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


# --------------------------------------------------------------------- run ----
PASSTHROUGH_SENSITIVE = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                         "--permission-mode", "--permission-prompt-tool", "--permission-prompts", "--allowedTools",
                         "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--settings", "--tools",
                         "--add-dir", "--mcp-config", "--strict-mcp-config", "--setting-sources")


def check_sensitive(a) -> None:
    """Every option that widens what the child may do needs the `unsafe` marker - checked on the
    EFFECTIVE values (after a resume merged the original options) incl. raw claude passthrough."""
    found = [n for n, on in (("--access full", getattr(a, "access", None) == "full"),
                             ("--allow-push", getattr(a, "allow_push", False)),
                             ("--confirm-full", getattr(a, "confirm_full", False))) if on]
    found += [x for x in (getattr(a, "extra", None) or []) if x.split("=")[0] in PASSTHROUGH_SENSITIVE]
    if found and os.environ.get("CLI_DELEGATE_UNSAFE_OK") != "1":
        die("敏感参数 " + ", ".join(found) + " 只能经统一入口的 `unsafe` 子命令使用（须用户同意）")


def cmd_run(a: argparse.Namespace) -> int:
    depth = current_depth()
    if depth >= a.max_depth:
        die(f"已经处在第 {depth} 层委派里，拒绝继续嵌套（--max-depth {a.max_depth}）", 2)

    prompt = load_text_arg(a.prompt, a.prompt_file, "任务提示词")
    if prompt is None and not sys.stdin.isatty() and not a.resume and not a.cont:
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        if a.resume or a.cont:
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
        die("--access full 会跳过全部权限检查，必须同时加 --confirm-full（且只能在用户明确同意后使用）")

    claude = find_bin()
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose"]

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.id:
        if not all(c.isalnum() or c in "-_." for c in a.id):
            die("--id 只能包含字母、数字、- _ .")
        run_dir = RUNS_DIR / a.id
        if run_dir.exists():
            die(f"--id 已被占用: {run_dir}")
    else:
        run_dir = RUNS_DIR / f"{stamp}-{secrets.token_hex(3)}"
    approval = a.access in APPROVAL_TIERS and not a.no_approval

    # model / effort / limits
    if a.model:
        cmd += ["--model", a.model]
    if a.effort:
        cmd += ["--effort", a.effort]
    if a.fallback_model:
        cmd += ["--fallback-model", a.fallback_model]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    if a.max_budget_usd:
        cmd += ["--max-budget-usd", str(a.max_budget_usd)]

    # permissions
    allow = list(a.allow)
    deny = list(a.deny)
    if not a.allow_push:
        deny += DEFAULT_DENY
    deny += BLOCKED_TOOLS
    rules = []
    if a.rules_json:
        try:
            rules = json.loads(a.rules_json)
        except json.JSONDecodeError as e:
            die(f"--rules-json 不是合法 JSON: {e}")
        for r in rules:  # project deny rules hold in every tier, even full / acceptEdits
            if r.get("decision") != "deny":
                continue
            if r.get("tool") == "Bash" and r.get("prefix"):
                deny += [f"Bash({r['prefix']})", f"Bash({r['prefix']} *)"]
            elif r.get("tool") == "FILE" and r.get("dir"):
                # Edit(...) rules cover every file-editing tool; "//" marks an absolute path
                deny += [f"Edit(/{r['dir']})", f"Edit(/{r['dir']}/**)"]
    if a.access == "read":
        cmd += ["--permission-mode", "dontAsk", "--tools", a.tools or READ_TOOLS]
        allow += ["WebSearch", "WebFetch"]
    elif a.access == "edit":
        cmd += ["--permission-mode", "acceptEdits"]
    elif a.access == "strict":
        cmd += ["--permission-mode", "default"]
    elif a.access == "auto":
        cmd += ["--permission-mode", "auto"]
    if a.access in APPROVAL_TIERS:
        if approval:
            bridge_cfg = {"mcpServers": {"cliapproval": {
                "command": sys.executable, "args": [str(BRIDGE), str(run_dir)],
                "env": {"CLI_APPROVAL_TIMEOUT": str(a.approval_timeout)}}}}
            cmd += ["--mcp-config", json.dumps(bridge_cfg), "--permission-prompt-tool", BRIDGE_TOOL]
        else:
            cmd += ["--permission-prompts", "none"]
    elif a.access == "full":
        cmd += ["--permission-mode", "bypassPermissions"]
    if a.tools and a.access != "read":
        cmd += ["--tools", a.tools]
    if allow:
        cmd += ["--allowedTools", ",".join(allow)]
    if deny:
        cmd += ["--disallowedTools", ",".join(deny)]
    for d in add_dirs:
        cmd += ["--add-dir", d]

    # session
    if a.resume:
        cmd += ["--resume", a.resume]
    elif a.cont:
        cmd += ["--continue"]
    if a.fork and (a.resume or a.cont):
        cmd += ["--fork-session"]
    if a.ephemeral:
        cmd += ["--no-session-persistence"]
    if a.name:
        cmd += ["--name", a.name]

    # system prompt
    appended = []
    if not a.no_worker_prompt:
        extra = (" and these extra directories: " + ", ".join(add_dirs)) if add_dirs else ""
        ask_rule = ASK_RULE_APPROVAL if approval else ASK_RULE_NO_APPROVAL
        appended.append(WORKER_PROMPT.format(extra_dirs=extra, tag=NEEDS_TAG, ask_rule=ask_rule))
    if a.inherit_claude_md:
        g = HOME / ".claude" / "CLAUDE.md"
        if g.is_file():
            appended.append("User's global instructions (from the caller's ~/.claude/CLAUDE.md):\n" + g.read_text(encoding="utf-8"))
    extra_sp = load_text_arg(a.append_system_prompt, a.append_system_prompt_file, "追加系统提示词")
    # store the content, not the file path, so `resume` still works after the file is gone
    a.append_system_prompt, a.append_system_prompt_file = extra_sp, None
    if extra_sp:
        appended.append(extra_sp)
    if appended:
        cmd += ["--append-system-prompt", "\n\n".join(appended)]

    # structured output / MCP / settings
    if a.json_schema:
        js = a.json_schema
        if Path(js).expanduser().is_file():
            js = Path(js).expanduser().read_text(encoding="utf-8")
        try:
            json.loads(js)
        except json.JSONDecodeError as e:
            die(f"--json-schema 不是合法 JSON: {e}")
        a.json_schema = js
        cmd += ["--json-schema", js]
    if a.mcp_config:
        if approval:  # --mcp-config takes several values; merge into one list after ours
            i = cmd.index("--mcp-config")
            cmd[i + 2:i + 2] = list(a.mcp_config)
        else:
            cmd += ["--mcp-config", *a.mcp_config]
    if a.strict_mcp:
        cmd += ["--strict-mcp-config"]
    settings: dict = {}
    if a.settings:
        sp = Path(a.settings).expanduser()
        try:
            settings = json.loads(sp.read_text(encoding="utf-8") if sp.is_file() else a.settings)
        except json.JSONDecodeError as e:
            die(f"--settings 不是合法 JSON 或文件: {e}")
    if a.access in ("edit", "auto"):
        perms = settings.setdefault("permissions", {})
        perms["ask"] = list(dict.fromkeys([*perms.get("ask", []), *ASK_RULES]))
    if settings:
        a.settings = json.dumps(settings, ensure_ascii=False)
        cmd += ["--settings", a.settings]
    cmd += a.extra

    extra_env = {}
    if a.thinking_tokens is not None:
        extra_env["MAX_THINKING_TOKENS"] = str(a.thinking_tokens)
    for kv in a.env:
        if "=" not in kv:
            die(f"--env 需要 KEY=VALUE 形式: {kv}")
        k, v = kv.split("=", 1)
        extra_env[k] = v
    env = clean_env(extra_env, a.pass_env, getattr(a, "config_dir", None))

    meta = {
        "cwd": str(cwd), "access": a.access, "approval": approval, "model": a.model, "effort": a.effort,
        "resume": a.resume, "continue": a.cont, "add_dirs": add_dirs,
        "timeout": a.timeout, "started": _dt.datetime.now().isoformat(timespec="seconds"),
        "caller_session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "resumed_from": getattr(a, "resumed_from", None),
        "config_dir": env.get("CLAUDE_CONFIG_DIR") or str(HOME / ".claude"),  # absolute: resumes use the same session store
        "argv": [c if len(c) < 300 else c[:300] + "…" for c in cmd],
        # options needed to resume this run with the same settings
        "opts": {k: v for k, v in vars(a).items() if k not in RUN_ONLY_KEYS},
    }

    if a.dry_run:
        print("工作目录:", cwd)
        print("命令:", " ".join(shlex.quote(c if len(c) < 200 else c[:200] + "…") for c in cmd))
        removed = sorted(k for k in os.environ if k not in env)
        print("已剔除的环境变量:", ", ".join(removed) or "无")
        print("附加环境变量:", json.dumps({k: env[k] for k in ["CLAUDE_CONFIG_DIR", DEPTH_VAR, *extra_env] if k in env}, ensure_ascii=False))
        print("提示词长度:", len(prompt or ""))
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    a.run_dir = str(run_dir)
    if rules:
        (run_dir / "approvals").mkdir(exist_ok=True)
        (run_dir / "approvals" / "rules.json").write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "prompt.md").write_text(prompt or "", encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[claude-cli] 已启动，运行记录: {run_dir}", file=sys.stderr, flush=True)
    if approval:
        print(f"[claude-cli] 审批通道已开启：用 `watch {run_dir.name}` 等待审批请求，用 `decide` 批复",
              file=sys.stderr, flush=True)

    timed_out = False
    t0 = time.time()
    with (run_dir / "events.jsonl").open("wb") as out, (run_dir / "stderr.log").open("wb") as err:
        lock_fds = ()
        if os.environ.get("CLI_DELEGATE_LOCK_FD", "").isdigit():
            lock_fds = (int(os.environ["CLI_DELEGATE_LOCK_FD"]),)  # the CLI holds the directory lock too
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=out, stderr=err, start_new_session=True, pass_fds=lock_fds)
        (run_dir / "pid").write_text(str(proc.pid))
        footer = PROMPT_FOOTER if approval else PROMPT_FOOTER_NO_APPROVAL
        sent = (prompt or "") + ("" if a.no_worker_prompt else footer)
        try:
            proc.stdin.write(sent.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            # The overall timeout does not run while an approval is waiting for the caller,
            # so a slow (user) decision is not killed by --timeout.
            paused = 0.0
            while True:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if approval and pending_requests(run_dir):
                    paused += 1
                if a.timeout > 0 and time.time() - t0 - paused > a.timeout:
                    raise subprocess.TimeoutExpired(cmd, a.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig, wait in ((signal.SIGINT, 20), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGINT)
            proc.wait(timeout=30)
            raise
    exit_code = proc.returncode
    (run_dir / "pid").unlink(missing_ok=True)

    info = summarize_events(run_dir / "events.jsonl")
    res = info.get("result") or {}
    limited = res.get("api_error") == "usage_limit_reached" or res.get("api_error_type") == "rate_limit_error"
    needs = extract_needs(res.get("result") or "") if res and not res.get("is_error") else None
    meta.update({"exit_code": exit_code, "timed_out": timed_out, "wall_seconds": round(time.time() - t0, 1),
                 "session_id": res.get("session_id") or (info.get("init") or {}).get("session_id"),
                 "usage_limited": limited, "needs_confirmation": needs,
                 "denials": len(res.get("permission_denials") or []),
                 "suggested_allow": suggest_rules(res.get("permission_denials") or [])})
    if limited:
        reset = parse_reset(res.get("result") or "")
        meta["reset_at"] = reset.isoformat(timespec="minutes") if reset else None
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if res:
        (run_dir / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "result.md").write_text(res.get("result") or info.get("last_text") or "", encoding="utf-8")
    summary = fmt_summary(run_dir, meta, info, a.max_chars, exit_code, timed_out)
    if (limited or timed_out) and meta.get("session_id"):
        summary += "\n" + resume_hint(run_dir, meta)
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)

    if timed_out:
        return 3
    if limited:
        return 5
    if not res or res.get("is_error") or exit_code not in (0, None):
        return 1
    if needs:
        return 6
    return 0


# ------------------------------------------------------------------ resume ----
RUN_ONLY_KEYS = {"prompt", "prompt_file", "dry_run", "func", "resume", "cont", "fork", "sub",
                 "resumed_from", "run", "wait", "max_waits", "run_dir", "id", "config_dir"}


def pid_alive(run_dir: Path) -> bool:
    """True if the run's claude process is still alive; removes a stale pid file."""
    pid_file = run_dir / "pid"
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text()), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False

DEFAULT_RESUME_PROMPT = (
    "上一次运行在中途被打断了（额度用尽或超时），现在继续。"
    "先检查工作目录里已经完成的部分（读文件、看 git status / git diff），不要重复或回滚已做的改动；"
    "然后继续完成原任务，最后给出完整的最终报告（包括本次之前已完成的部分）。"
)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_reset(text: str):
    """Parse e.g. "resets 8:30pm (Asia/Tokyo)" / "resets Nov 21, 3pm (Asia/Tokyo)" into an aware datetime."""
    import re
    m = re.search(r"resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s*(?:at\s+)?)?"
                  r"(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm)?\s*(?:\((?P<tz>[^)]+)\))?", text, re.I)
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m["tz"]) if m["tz"] else None
    except Exception:
        tz = None
    now = _dt.datetime.now(tz) if tz else _dt.datetime.now().astimezone()
    h = int(m["h"]) % 12 if m["ap"] else int(m["h"])
    if (m["ap"] or "").lower() == "pm":
        h += 12
    mi = int(m["mi"] or 0)
    if m["mon"] and m["mon"].lower()[:3] in _MONTHS:
        cand = now.replace(month=_MONTHS[m["mon"].lower()[:3]], day=int(m["day"]), hour=h, minute=mi,
                           second=0, microsecond=0)
        if cand < now - _dt.timedelta(days=180):
            cand = cand.replace(year=cand.year + 1)
    else:
        cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand <= now:
            cand += _dt.timedelta(days=1)
    return cand.astimezone()  # local time of this machine


ENTRY = Path(__file__).resolve().parents[2] / "scripts" / "cli_delegate.py"


def resume_hint(run_dir: Path, meta: dict) -> str:
    # resumes must go through the unified entry (snapshots, locks, current project rules, ledger)
    me = f"python3 {ENTRY}"
    lines = ["--- 续跑 ---", f"会话已保存，可在同一 session 上继续（session_id {meta['session_id']}）。"]
    reset = meta.get("reset_at")
    if reset:
        at = _dt.datetime.fromisoformat(reset) + _dt.timedelta(minutes=3)
        lines.append(f"额度重置时间（本机时间）: {reset}")
        lines.append(f"建议定时（一次性 CronCreate，本机时区）: cron \"{at.minute} {at.hour} {at.day} {at.month} *\"，recurring=false")
        lines.append(f"到点后执行: {me} resume {run_dir.name} --id <新运行名>")
        lines.append(f"或现在就后台挂起等待: {me} resume {run_dir.name} --id <新运行名> --wait")
    elif meta.get("usage_limited"):
        lines.append("未能解析重置时间，请看上面的报告文本。")
        lines.append(f"重置后执行: {me} resume {run_dir.name} --id <新运行名>")
    else:
        lines.append(f"继续执行: {me} resume {run_dir.name} --id <新运行名>")
    return "\n".join(lines)


def cmd_resume(a) -> int:
    run_dir = resolve_run(a.run)
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        die(f"运行记录缺少 meta.json: {run_dir}")
    for attempt in range(a.max_waits + 1):
        meta = json.loads(meta_path.read_text())
        sid = meta.get("session_id")
        if not sid:
            die("这次运行没有 session_id（可能根本没启动成功），不能续跑，请重新 run")
        if meta.get("opts", {}).get("ephemeral"):
            die("这次运行用了 --ephemeral，会话没有保存，不能续跑")
        if pid_alive(run_dir):
            die(f"这次运行还在进行中（{run_dir}），不要重复续跑")
        if a.wait and meta.get("reset_at"):
            target = _dt.datetime.fromisoformat(meta["reset_at"]) + _dt.timedelta(seconds=90)
            secs = (target - _dt.datetime.now().astimezone()).total_seconds()
            if secs > 0:
                print(f"[claude-cli] 等待额度重置：{target.isoformat(timespec='minutes')}（约 {secs / 60:.0f} 分钟）",
                      file=sys.stderr, flush=True)
                time.sleep(secs)
        ns = build_parser().parse_args(["run"])
        for k, v in (meta.get("opts") or {}).items():
            if hasattr(ns, k):
                setattr(ns, k, v)
        ns.cwd = meta["cwd"]
        ns.resume = sid
        ns.config_dir = meta.get("config_dir")
        ns.id = a.id if attempt == 0 else None
        ns.resumed_from = str(run_dir)
        custom = load_text_arg(a.prompt, a.prompt_file, "续跑提示词")
        parts = []
        if a.answer:
            parts.append("用户对你提出的待确认问题的答复：\n" + a.answer.strip())
        if a.allow:
            parts.append("用户已放行这些操作，可以重试之前被拒绝、但完成任务必需的步骤：" + "、".join(a.allow))
        if a.access:
            parts.append(f"权限档位已调整为 {a.access}。")
        if parts and not custom:
            parts.append("请据此继续完成原任务，不要重复已经完成的部分。如果还有需要用户决定的事项，"
                         f"照旧在最终报告末尾用 <{NEEDS_TAG}> 块列出。最后给出完整的最终报告（包括之前已完成的部分）。")
        ns.prompt = custom or ("\n\n".join(parts) if parts else DEFAULT_RESUME_PROMPT)
        ns.prompt_file = None
        if a.rules_json is not None:
            ns.rules_json = a.rules_json
        for k in ("model", "effort", "timeout", "approval_timeout", "access"):
            if getattr(a, k) is not None:
                setattr(ns, k, getattr(a, k))
        ns.allow = list(ns.allow or []) + [r for r in a.allow if r not in (ns.allow or [])]
        ns.deny = list(ns.deny or []) + [r for r in a.deny if r not in (ns.deny or [])]
        ns.confirm_full = a.confirm_full or (ns.access == "full" and ns.confirm_full)
        if a.allow_push:
            ns.allow_push = True
        check_sensitive(ns)  # merged options of the original run count too
        rc = cmd_run(ns)
        if rc == 5 and a.wait and attempt < a.max_waits and getattr(ns, "run_dir", None):
            run_dir = Path(ns.run_dir)
            meta_path = run_dir / "meta.json"
            if not json.loads(meta_path.read_text()).get("reset_at"):
                return rc
            continue
        return rc
    return 5


# --------------------------------------------------------------- approvals ----
def pending_requests(run_dir: Path) -> list[dict]:
    ap = run_dir / "approvals"
    out = []
    if not ap.is_dir():
        return out
    for p in sorted(ap.glob("*.request.json")):
        rid = p.name.split(".")[0]
        if (ap / f"{rid}.decision.json").exists():
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def approval_stats(run_dir: Path) -> dict:
    stats = {"allow": 0, "deny": 0, "auto": 0, "rule_deny": 0, "timeout": 0, "by_user": 0}
    logf = run_dir / "approvals" / "log.jsonl"
    for ev in read_jsonl(logf):
        if ev.get("auto") and ev.get("denied"):
            stats["rule_deny"] += 1
        elif ev.get("auto"):
            stats["auto"] += 1
    ap = run_dir / "approvals"
    if ap.is_dir():
        for p in ap.glob("*.decision.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            stats["allow" if d.get("behavior") == "allow" else "deny"] += 1
            stats["timeout"] += d.get("by") == "timeout"
            stats["by_user"] += d.get("by") == "user"
    return stats


def fmt_request(req: dict) -> str:
    rid, tool, inp = req.get("id"), req.get("tool_name"), req.get("input") or {}
    lines = [f"[{rid}] {('提问' if req.get('kind') == 'question' else '权限请求')} · {tool} · {req.get('created')}"]
    if tool == "AskUserQuestion":
        for i, q in enumerate(inp.get("questions") or [], 1):
            multi = "（可多选）" if q.get("multiSelect") else ""
            lines.append(f"  Q{i}{multi}: {q.get('question')}")
            for o in q.get("options") or []:
                lines.append(f"     - {o.get('label')}: {o.get('description', '')}")
    elif tool == "Bash":
        lines.append(f"  命令: {inp.get('command')}")
        if inp.get("description"):
            lines.append(f"  说明: {inp.get('description')}")
    else:
        lines.append("  输入: " + short(json.dumps(inp, ensure_ascii=False), 600))
    return "\n".join(lines)


def cmd_watch(a) -> int:
    # the run may have been started a moment ago in the background: give it time to appear
    if a.run not in (None, "", "last"):
        for _ in range(60):
            if Path(a.run).expanduser().is_dir() or (RUNS_DIR / a.run).is_dir():
                break
            time.sleep(0.5)
    run_dir = resolve_run(a.run)
    deadline = time.time() + a.max_wait
    started = time.time()
    while True:
        pend = pending_requests(run_dir)
        if pend:
            print(f"=== {run_dir.name}: {len(pend)} 个请求等待主会话审批 ===")
            for r in pend:
                print(fmt_request(r))
            print(f"\n批复: python3 {Path(__file__).resolve()} decide {run_dir.name} <编号> allow|deny|answer ...")
            return 10
        summary = run_dir / "summary.txt"
        if summary.exists() and not pid_alive(run_dir):
            print(summary.read_text(encoding="utf-8"))
            meta = json.loads((run_dir / "meta.json").read_text())
            return 0 if meta.get("exit_code") == 0 and not meta.get("needs_confirmation") else 1
        if not pid_alive(run_dir) and time.time() - started > 15 and not summary.exists():
            print(f"{run_dir.name}: 进程已不在运行，也没有结果（可能被中断）。可用 resume 续跑。")
            return 1
        if time.time() > deadline:
            print(f"{run_dir.name}: 仍在运行，暂无待批请求。再次执行 watch 继续等待。")
            info = summarize_events(run_dir / "events.jsonl")
            for tb in info["tools"][-5:]:
                print("  最近: " + tb)
            return 11
        time.sleep(1)


def cmd_decide(a) -> int:
    run_dir = resolve_run(a.run)
    ap = run_dir / "approvals"
    reqs = pending_requests(run_dir) if a.req == "all" else []
    if a.req != "all":
        p = ap / f"{a.req}.request.json"
        if not p.exists():
            die(f"没有这个请求: {p}")
        if (ap / f"{a.req}.decision.json").exists():
            die(f"请求 {a.req} 已经批复过了")
        reqs = [json.loads(p.read_text(encoding="utf-8"))]
    if not reqs:
        die("没有待批请求")
    for req in reqs:
        rid, tool, inp = req["id"], req["tool_name"], req.get("input") or {}
        if a.decision == "answer":
            if tool != "AskUserQuestion":
                die(f"[{rid}] 不是提问请求，不能用 answer")
            if not a.answers:
                die("answer 需要 --answers JSON")
            try:
                answers = json.loads(a.answers)
            except json.JSONDecodeError as e:
                die(f"--answers 不是合法 JSON: {e}")
            qs = [q.get("question") for q in inp.get("questions") or []]
            missing = [q for q in qs if q not in answers]
            if missing:
                die(f"[{rid}] 缺少这些问题的答案（键必须是问题原文）: {missing}")
            answers = {k: (", ".join(v) if isinstance(v, list) else str(v)) for k, v in answers.items()}
            d = {"behavior": "allow", "updatedInput": {**inp, "answers": answers}}
        elif a.decision == "allow":
            if tool == "AskUserQuestion":
                die(f"[{rid}] 是提问请求：请用 answer --answers 回答，或用 deny 拒绝")
            new_inp = inp
            if a.updated_input:
                try:
                    new_inp = json.loads(a.updated_input)
                except json.JSONDecodeError as e:
                    die(f"--updated-input 不是合法 JSON: {e}")
            d = {"behavior": "allow", "updatedInput": new_inp}
            if a.always:
                if tool == "Bash":
                    cmd_s = str(inp.get("command", "")).strip()
                    # default: only this exact command; a broader prefix must be given explicitly
                    rule = {"tool": tool, "exact": a.prefix is None, "prefix": a.prefix or cmd_s}
                else:
                    tgt = str(inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "")
                    if a.prefix is not None:
                        base = a.prefix
                    elif tgt:
                        base = str(Path(tgt).expanduser().resolve().parent)
                    else:
                        die(f"[{rid}] {tool} 的请求里没有文件路径，不能设常驻规则；只批准这一次请去掉 --always")
                    rule = {"tool": tool, "dir": str(Path(base).expanduser().resolve())}
                rule["added"] = _dt.datetime.now().isoformat(timespec="seconds")
                rules_p = ap / "rules.json"
                rules = json.loads(rules_p.read_text()) if rules_p.exists() else []
                rules.append(rule)
                rules_p.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"已加入常驻规则: {json.dumps(rule, ensure_ascii=False)}")
        else:
            d = {"behavior": "deny", "message": a.message or "Denied by the caller session."}
        if a.message and d["behavior"] == "allow":
            d["note"] = a.message
        d["by"] = a.by
        d["decided"] = _dt.datetime.now().isoformat(timespec="seconds")
        tmp = ap / f"{rid}.decision.json.tmp"
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(ap / f"{rid}.decision.json")
        print(f"[{rid}] {tool}: {d['behavior']}")
    return 0


# ------------------------------------------------------------------ others ----
def run_order(p: Path):
    """Sort runs by start time (meta.json), not by name: --id names would sort wrongly."""
    try:
        return json.loads((p / "meta.json").read_text()).get("started") or ""
    except (OSError, json.JSONDecodeError):
        return _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def all_runs() -> list:
    return sorted((p for p in RUNS_DIR.glob("*/") if p.is_dir()), key=run_order) if RUNS_DIR.exists() else []


def resolve_run(arg: str | None) -> Path:
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
    info = summarize_events(run_dir / "events.jsonl")
    running = pid_alive(run_dir)
    print(f"运行记录: {run_dir}")
    print(f"状态: {'运行中' if running else ('已结束' if info['result'] else '未在运行（可能已中断）')}")
    print(f"工作目录: {meta.get('cwd')}  开始于 {meta.get('started')}")
    init = info.get("init") or {}
    if init:
        print(f"模型: {init.get('model')}  session_id: {init.get('session_id')}")
    print(f"工具调用 {len(info['tools'])} 次，最近 {a.n} 次:")
    for tb in info["tools"][-a.n:]:
        print("  - " + tb)
    if info["last_text"]:
        print("最近一段文字: " + short(info["last_text"], 400))
    return 0


def cmd_list(a) -> int:
    runs = all_runs()
    for r in runs[-a.n:]:
        meta = {}
        if (r / "meta.json").exists():
            meta = json.loads((r / "meta.json").read_text())
        prompt = (r / "prompt.md").read_text(errors="replace") if (r / "prompt.md").exists() else ""
        if pid_alive(r):
            state = "运行中"
        elif meta.get("timed_out"):
            state = "超时"
        elif meta.get("usage_limited"):
            state = f"额度用尽 重置 {meta.get('reset_at') or '?'}"
        elif meta.get("needs_confirmation"):
            state = "待用户确认"
        else:
            state = "成功" if meta.get("exit_code") == 0 else f"exit={meta.get('exit_code')}"
        print(f"{r.name}  [{state}]  sid={meta.get('session_id') or '-'}  cwd={meta.get('cwd')}  {short(prompt, 60)}")
    if not runs:
        print("还没有任何运行记录")
    return 0


def cmd_doctor(a) -> int:
    ok = True
    claude = find_bin()
    env = clean_env({}, [])
    ver = subprocess.run([claude, "--version"], capture_output=True, text=True, env=env)
    print(f"CLI: {claude}  版本: {ver.stdout.strip() or ver.stderr.strip()}")
    print(f"配置目录: {CONFIG_DIR or '~/.claude（CLI 默认）'}"
          + ("" if not CONFIG_DIR else f"  {'存在' if CONFIG_DIR.is_dir() else '不存在!'}")
          + "  （来源：环境变量 CLAUDECLI_CONFIG_DIR 或 ~/.cli-delegate/config.json 的 claude_config_dir）")
    st = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, env=env, cwd=str(HOME))
    try:
        js = json.loads(st.stdout)
    except json.JSONDecodeError:
        js = {}
        print("auth status 输出无法解析:", short(st.stdout + st.stderr, 300))
        ok = False
    if js:
        email = js.get("email") or ""
        masked = (email[:2] + "***@" + email.split("@")[-1]) if "@" in email else email
        print(f"登录: {js.get('loggedIn')}  方式: {js.get('authMethod')}  套餐: {js.get('subscriptionType')}  账号: {masked}")
        print(f"登录到期: {js.get('loginExpiresAt')}  configDirectory: {js.get('configDirectory')}")
        if not js.get("loggedIn"):
            ok = False
            print("未登录：在终端运行 claude auth login --claudeai（指定了配置目录时前面加 CLAUDE_CONFIG_DIR=<目录>），用 Claude 订阅账号登录")
        if CONFIG_DIR and js.get("configDirectory") and Path(js["configDirectory"]).resolve() != CONFIG_DIR.resolve():
            ok = False
            print("警告: 子进程没有使用指定的配置目录")
        host_email = os.environ.get("CLAUDE_CODE_USER_EMAIL")
        if host_email and email and host_email.lower() == email.lower():
            print("提示: CLI 登录的账号与当前会话是同一个邮箱（会共用额度；同一邮箱的不同组织可忽略）")
    removed = sorted(k for k in os.environ if k not in env)
    print(f"会剔除的宿主环境变量 {len(removed)} 个: {', '.join(removed) or '无'}")
    print(f"当前委派层数: {current_depth()}")
    print("结论:", "可用" if ok else "有问题，见上")
    return 0 if ok else 4


# -------------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="delegate.py", description="调用 Claude Code CLI（claude -p）执行任务",
                                allow_abbrev=False)
    sub = p.add_subparsers(dest="sub")

    r = sub.add_parser("run", help="执行任务", allow_abbrev=False)
    g = r.add_argument_group("任务")
    g.add_argument("--prompt", "-p", help="任务提示词（长文本请用 --prompt-file）")
    g.add_argument("--prompt-file", help="从文件读提示词，'-' 表示 stdin")
    g.add_argument("--cwd", help="工作目录，默认当前目录（应传调用会话的工作目录）")
    g.add_argument("--add-dir", action="append", default=[], help="额外允许访问的目录，可多次")
    g = r.add_argument_group("模型")
    g.add_argument("--model", "-m", help="模型别名或全名：fable / opus / sonnet / haiku / opusplan / opus[1m] / claude-…")
    g.add_argument("--effort", "-e", choices=EFFORTS, help="思考强度")
    g.add_argument("--thinking-tokens", type=int, help="设置 MAX_THINKING_TOKENS（固定思考预算，多数新模型忽略）")
    g.add_argument("--fallback-model", help="主模型不可用时的回退模型，逗号分隔")
    g = r.add_argument_group("权限与限制")
    g.add_argument("--access", "-a", choices=ACCESS, default="edit",
                   help="read=只读; edit=自动接受文件编辑，其余需批准的操作（含删除/移动/git 改写）交主会话审批（默认）;"
                        " strict=连文件编辑也交主会话审批; auto=自动模式分类器，拦下的交主会话审批; full=跳过全部权限（需 --confirm-full）")
    g.add_argument("--confirm-full", action="store_true", help="确认使用 --access full")
    g.add_argument("--allow", action="append", default=[], help='额外放行规则，如 "Bash(npm test *)"，可多次')
    g.add_argument("--deny", action="append", default=[], help='额外禁止规则，如 "Bash(rm *)"，可多次')
    g.add_argument("--allow-push", action="store_true", help="不默认禁止 git push")
    g.add_argument("--no-approval", action="store_true",
                   help="关闭审批通道：需要批准的操作直接拒绝（无人值守时用）")
    g.add_argument("--approval-timeout", type=int, default=1800,
                   help="单个审批请求等待主会话批复的秒数，超时按拒绝处理，默认 1800")
    g.add_argument("--id", help="指定运行记录名（字母数字 - _ .），便于 watch/decide 引用")
    g.add_argument("--rules-json", help="项目审批规则（JSON 数组，由 cli-delegate 入口从 .cli-delegate.toml 生成）")
    g.add_argument("--tools", help='限定内置工具集，如 "Read,Edit,Bash"')
    g.add_argument("--max-turns", type=int, help="最大轮数")
    g.add_argument("--max-budget-usd", type=float, help="费用上限（客户端估算）")
    g.add_argument("--timeout", type=int, default=1800, help="超时秒数，默认 1800，0 为不限")
    g.add_argument("--max-depth", type=int, default=1, help="允许的最大委派嵌套层数，默认 1（子会话不能再委派）")
    g = r.add_argument_group("会话")
    g.add_argument("--resume", "-r", help="按 session_id 续上之前的委派会话")
    g.add_argument("--continue", dest="cont", action="store_true", help="续上该目录下 CLI 最近的会话")
    g.add_argument("--fork", action="store_true", help="续聊时分叉出新 session_id")
    g.add_argument("--ephemeral", action="store_true", help="不保存会话（之后无法续聊）")
    g.add_argument("--name", help="会话显示名")
    g = r.add_argument_group("上下文与输出")
    g.add_argument("--no-worker-prompt", action="store_true", help="不追加内置的委派工作守则")
    g.add_argument("--inherit-claude-md", action="store_true", help="把调用方 ~/.claude/CLAUDE.md 追加给子会话")
    g.add_argument("--append-system-prompt", help="追加系统提示词")
    g.add_argument("--append-system-prompt-file", help="从文件追加系统提示词")
    g.add_argument("--json-schema", help="结构化输出的 JSON Schema（字符串或文件路径）")
    g.add_argument("--mcp-config", nargs="+", help="MCP 配置文件或 JSON")
    g.add_argument("--strict-mcp", action="store_true", help="只用 --mcp-config 里的 MCP")
    g.add_argument("--settings", help="额外 settings 文件或 JSON")
    g.add_argument("--env", action="append", default=[], help="给子进程加环境变量 KEY=VALUE")
    g.add_argument("--pass-env", action="append", default=[], help="保留某个本会被剔除的环境变量")
    g.add_argument("--max-chars", type=int, default=12000, help="打印报告的最大字符数，0 为不截断")
    g.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不运行")
    r.add_argument("extra", nargs=argparse.REMAINDER, help="'--' 之后的参数原样传给 claude")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="在中断（额度用尽/超时）的会话上用原参数续跑", allow_abbrev=False)
    rs.add_argument("run", nargs="?", default="last", help="运行目录或其名字，默认最近一次")
    rs.add_argument("--prompt", "-p", help="续跑提示词，默认是内置的「检查已完成部分后继续」")
    rs.add_argument("--prompt-file", help="从文件读续跑提示词")
    rs.add_argument("--wait", action="store_true", help="先等到额度重置时间再跑；仍被限流则继续等")
    rs.add_argument("--max-waits", type=int, default=3, help="--wait 时最多再等几次，默认 3")
    rs.add_argument("--model", "-m", default=None, help="覆盖原来的模型")
    rs.add_argument("--effort", "-e", choices=EFFORTS, default=None, help="覆盖原来的思考强度")
    rs.add_argument("--timeout", type=int, default=None, help="覆盖原来的超时秒数")
    rs.add_argument("--approval-timeout", type=int, default=None, help="覆盖单个审批的等待秒数")
    rs.add_argument("--answer", help="用户对「待确认」问题的答复，会作为续跑消息发给子会话")
    rs.add_argument("--allow", action="append", default=[], help="追加放行规则（须在用户授权范围内），可多次")
    rs.add_argument("--deny", action="append", default=[], help="追加禁止规则，可多次")
    rs.add_argument("--access", choices=ACCESS, default=None, help="改变权限档位（须在用户授权范围内）")
    rs.add_argument("--confirm-full", action="store_true", help="改成 --access full 时必须加")
    rs.add_argument("--allow-push", action="store_true", help="续跑时允许 git push（须经用户确认）")
    rs.add_argument("--id", help="新运行记录的名字，便于 watch/decide 引用")
    rs.add_argument("--rules-json", default=None, help="当前的项目审批规则（由入口重新读取后传入，覆盖原运行的规则）")

    w = sub.add_parser("watch", help="等待审批请求或任务结束（主会话审批循环用）")
    w.add_argument("run", nargs="?", default="last")
    w.add_argument("--max-wait", type=int, default=540, help="最多等待秒数，默认 540（留在 Bash 10 分钟上限内）")
    w.set_defaults(func=cmd_watch)

    dc = sub.add_parser("decide", help="批复一个审批请求")
    dc.add_argument("run", help="运行记录名或目录（可用 last）")
    dc.add_argument("req", help="请求编号，如 001；all 表示当前所有待批请求")
    dc.add_argument("decision", choices=["allow", "deny", "answer"], help="allow 批准 / deny 拒绝 / answer 回答 AskUserQuestion")
    dc.add_argument("--message", "-m", help="拒绝理由或给子会话的说明（deny 时强烈建议填写替代做法）")
    dc.add_argument("--answers", help='answer 用：JSON，{"问题原文": "选项 label 或自由文本"}，多选用数组')
    dc.add_argument("--updated-input", help="allow 用：改写后的工具输入（JSON），如改成更安全的命令")
    dc.add_argument("--always", action="store_true",
                    help="allow 时同时记为本次运行的常驻规则：Bash 默认只匹配完全相同的命令（不含管道/分号等）；"
                         "文件类匹配所在目录及其子目录（按真实路径判断）")
    dc.add_argument("--prefix", help="--always 时放宽范围：Bash 为命令前缀（如 \"npm test\"），文件类为目录；须在授权范围内")
    dc.add_argument("--by", default="caller", choices=["caller", "user"], help="谁做的决定，记入日志")
    dc.set_defaults(func=cmd_decide)
    rs.set_defaults(func=cmd_resume)

    d = sub.add_parser("doctor", help="检查 Claude CLI 与登录状态")
    d.set_defaults(func=cmd_doctor)
    s = sub.add_parser("show", help="查看某次运行的结果（默认最近一次）")
    s.add_argument("run", nargs="?", default="last")
    s.add_argument("-n", type=int, default=12)
    s.set_defaults(func=cmd_show)
    pr = sub.add_parser("progress", help="查看运行中任务的进度（默认最近一次）")
    pr.add_argument("run", nargs="?", default="last")
    pr.add_argument("-n", type=int, default=15)
    pr.set_defaults(func=cmd_progress)
    ls = sub.add_parser("list", help="列出最近的运行")
    ls.add_argument("-n", type=int, default=15)
    ls.set_defaults(func=cmd_list)
    return p


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("run", "resume", "watch", "decide", "doctor", "show", "progress", "list",
                                   "-h", "--help"):
        argv = ["run", *argv]
    a = build_parser().parse_args(argv)
    if getattr(a, "extra", None) and a.extra[:1] == ["--"]:
        a.extra = a.extra[1:]
    check_sensitive(a)
    if a.func in (cmd_run, cmd_resume, cmd_list, cmd_show, cmd_progress, cmd_doctor) and not getattr(a, "dry_run", False):
        migrate_runs()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
