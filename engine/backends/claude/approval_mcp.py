#!/usr/bin/env python3
"""Approval bridge for claude-cli (stdio MCP server, stdlib only).

The child `claude -p` is started with
    --mcp-config '{"mcpServers":{"cliapproval":{"command":"python3","args":[this, RUN_DIR]}}}'
    --permission-prompt-tool mcp__cliapproval__permission_prompt
so every permission prompt (and every AskUserQuestion) of the child becomes a call to
the `permission_prompt` tool below. The call is written to
    RUN_DIR/approvals/<id>.request.json
and blocks until the caller session writes
    RUN_DIR/approvals/<id>.decision.json
(with `delegate.py decide`). Standing rules from `decide --always` live in
RUN_DIR/approvals/rules.json and are applied without asking.

If no decision arrives within CLI_APPROVAL_TIMEOUT seconds (default 1800) the request
is denied, so the child never hangs forever.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

RUN_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
APPROVALS = RUN_DIR / "approvals"
APPROVALS.mkdir(parents=True, exist_ok=True)
TIMEOUT = float(os.environ.get("CLI_APPROVAL_TIMEOUT", "1800"))
TOOL_NAME = "permission_prompt"

_out_lock = threading.Lock()
_id_lock = threading.Lock()
_counter = [0]

SHELL_META = re.compile(r"[;&|`<>]|\$\(|\n")


def send(msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def log(event: dict) -> None:
    event = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
    with _out_lock:
        with (APPROVALS / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def next_id() -> str:
    with _id_lock:
        existing = [int(p.name.split(".")[0]) for p in APPROVALS.glob("*.request.json")
                    if p.name.split(".")[0].isdigit()]
        _counter[0] = max([_counter[0], *existing]) + 1
        return f"{_counter[0]:03d}"


FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def matches_rule(tool: str, inp: dict) -> dict | None:
    """Standing rules written by `decide --always`. Bash rules only match simple commands."""
    path = APPROVALS / "rules.json"
    if not path.exists():
        return None
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # deny rules (e.g. from the project's .cli-delegate.toml) win over allow rules
    rules = sorted(rules, key=lambda r: 0 if r.get("decision") == "deny" else 1)
    protected = any(r.get("decision") == "deny" and r.get("tool") == "FILE" for r in rules)
    if protected and tool == "Bash":
        # a command can write anywhere: with protected paths, only deny rules apply to commands
        rules = [r for r in rules if r.get("decision") == "deny"]
    for r in rules:
        rt = r.get("tool")
        if not (rt == tool or (rt == "FILE" and tool in FILE_TOOLS)):
            continue
        if tool == "Bash":
            cmd = str(inp.get("command", "")).strip()
            prefix = r.get("prefix")
            if not prefix or SHELL_META.search(cmd):
                continue
            if r.get("exact", True):
                if cmd == prefix:
                    return r
            elif cmd == prefix or cmd.startswith(prefix + " "):
                return r
        else:
            base = r.get("dir")
            target = str(inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "")
            if not base or not target:
                continue  # never a tool-wide rule
            try:
                Path(target).expanduser().resolve().relative_to(Path(base).expanduser().resolve())
                return r  # target lies inside the rule's directory (real path boundary)
            except ValueError:
                continue
    return None


def answer(tool: str, inp: dict, tool_use_id: str | None) -> dict:
    rule = matches_rule(tool, inp)
    if rule and rule.get("decision") == "deny":
        log({"auto": True, "denied": True, "tool": tool, "input": inp, "rule": rule})
        return {"behavior": "deny", "message": rule.get("message") or
                "This operation is forbidden by the project's delegation rules (.cli-delegate.toml). Do not retry it."}
    if rule:
        log({"auto": True, "tool": tool, "input": inp, "rule": rule})
        return {"behavior": "allow", "updatedInput": inp}

    rid = next_id()
    req = {"id": rid, "tool_name": tool, "input": inp, "tool_use_id": tool_use_id,
           "kind": "question" if tool == "AskUserQuestion" else "permission",
           "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "timeout_s": TIMEOUT}
    tmp = APPROVALS / f"{rid}.request.json.tmp"
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(APPROVALS / f"{rid}.request.json")
    log({"request": rid, "tool": tool})

    decision_path = APPROVALS / f"{rid}.decision.json"
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if decision_path.exists():
            try:
                d = json.loads(decision_path.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                pass  # half-written; retry
        time.sleep(0.5)
    else:
        d = {"behavior": "deny",
             "message": f"No decision from the caller within {int(TIMEOUT)}s; treat this as denied and continue without it."}
        decision_path.write_text(json.dumps({**d, "by": "timeout"}, ensure_ascii=False), encoding="utf-8")

    log({"decision": rid, "behavior": d.get("behavior")})
    if d.get("behavior") == "allow":
        return {"behavior": "allow", "updatedInput": d.get("updatedInput") or inp}
    return {"behavior": "deny", "message": d.get("message") or "Denied by the caller."}


def handle(msg: dict) -> None:
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cliapproval", "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": TOOL_NAME,
            "description": "Routes a permission request of the delegated session to the caller session for a decision.",
            "inputSchema": {"type": "object", "properties": {
                "tool_name": {"type": "string"}, "input": {"type": "object"},
                "tool_use_id": {"type": "string"}}, "required": ["tool_name", "input"]}}]}})
    elif method == "tools/call":
        args = params.get("arguments") or {}
        if os.environ.get("CLI_APPROVAL_DEBUG"):
            log({"raw_arguments": args})

        def work():
            try:
                res = answer(str(args.get("tool_name", "?")), args.get("input") or {}, args.get("tool_use_id"))
            except Exception as e:  # never leave the child hanging
                res = {"behavior": "deny", "message": f"approval bridge error: {e}"}
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}})

        threading.Thread(target=work, daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
