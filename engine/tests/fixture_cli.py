#!/usr/bin/env python3
"""Deterministic native CLI protocol fixture. Writes only in the supplied fake task directory."""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
claude = Path(sys.argv[0]).name.startswith("claude")
directory = Path(os.environ["CLAUDE_CONFIG_DIR" if claude else "CODEX_HOME"])
identity = (directory / "fixture-account").read_text().strip() if (directory / "fixture-account").exists() else ""
# Codex replies name a workspace unless a test writes an empty fixture-workspace.
workspace = (directory / "fixture-workspace").read_text().strip() if (directory / "fixture-workspace").exists() else "ws-default"
model = "opus" if claude else "gpt-6.2-sol"
fields = ("completed", "changes", "tests", "unfinished", "risks", "recommendations")
report = {key: ["fixture"] if key == "completed" else [] for key in fields}


def emit(value):
    print(json.dumps(value), flush=True)


def edit(prompt):
    for line in prompt.splitlines():
        if line.startswith("edit "):
            _, path, body = line.split(" ", 2)
            Path(path).write_text(body + "\n")
        if line.startswith("require "):
            _, path, body = line.split(" ", 2)
            assert Path(path).read_text() == body + "\n", line
        if line.startswith("mkdir "):
            Path(line.split(" ", 1)[1]).mkdir(parents=True)
        if line.startswith("remove "):
            path = Path(line.split(" ", 1)[1])
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        if line.startswith("symlink "):
            _, path, target = line.split(" ", 2)
            Path(path).symlink_to(target)
        if line.startswith("rename "):
            _, path, target = line.split(" ", 2)
            Path(path).rename(".rename-fixture").rename(target)


def ask(method, params):
    """Send a server request and return the client's result, answering steers that arrive first."""
    ask.count = getattr(ask, "count", 0) + 1
    key = f"approval-{ask.count}"
    emit({"id": key, "method": method, "params": params})
    while True:
        response = json.loads(sys.stdin.readline())
        if response.get("id") == key and "method" not in response:
            with (directory / "fixture-approvals").open("a") as log:
                print(json.dumps({"method": method, "params": params, "result": response.get("result")}), file=log)
            return response.get("result")
        if "method" in response and "id" in response:
            with (directory / "fixture-steers").open("a") as log:
                print(json.dumps(response["params"]), file=log)
            emit({"id": response["id"], "result": {}})


def escalations(prompt, thread):
    """`approve-command <cmd>`, `approve-delete <path>`, `approve-permission <path>`, `generate-image <dest>`."""
    for line in prompt.splitlines():
        verb, _, value = line.partition(" ")
        base = {"threadId": thread, "turnId": "turn", "startedAtMs": 1}
        if verb == "approve-command":
            result = ask("item/commandExecution/requestApproval",
                         {**base, "itemId": "cmd", "command": value, "cwd": str(Path.cwd()), "reason": "fixture"})
            if result["decision"] in ("accept", "acceptForSession"):
                marker = Path.cwd() / "approved.log"
                marker.write_text((marker.read_text() if marker.exists() else "") + value + "\n")
        elif verb == "approve-network":
            ask("item/commandExecution/requestApproval",
                {**base, "itemId": "net", "command": "curl " + value, "cwd": str(Path.cwd()),
                 "networkApprovalContext": {"host": value, "protocol": "https"}})
        elif verb == "approve-delete":
            emit({"method": "item/started", "params": {"item": {"type": "fileChange", "id": "change", "changes": [
                {"path": value, "kind": {"type": "delete"}, "diff": ""}]}}})
            ask("item/fileChange/requestApproval", {**base, "itemId": "change", "reason": "fixture"})
        elif verb == "approve-permission":
            ask("item/permissions/requestApproval",
                {**base, "cwd": str(Path.cwd()), "reason": "fixture",
                 "permissions": {"fileSystem": {"read": [value]}}})
        elif verb == "generate-image":
            folder = directory / "generated_images" / thread
            folder.mkdir(parents=True, exist_ok=True)
            image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR3cAAAAASUVORK5CYII=")
            (folder / "code.png").write_bytes(image)
            if value:
                Path(value).parent.mkdir(parents=True, exist_ok=True)
                Path(value).write_bytes(image)
            emit({"method": "item/completed", "params": {"item": {"type": "imageGeneration", "id": "image",
                  "status": "completed", "failure": None, "savedPath": str(folder / "code.png")}}})


if "--version" in args:
    print("2.1.329 (Claude Code)" if claude else "codex-cli 0.171.0")
elif "--help" in args:
    print("--restricted --strict-mcp-config --permission-prompts")
elif args[:2] == ["auth", "status"]:
    emit({"loggedIn": bool(identity), "authMethod": "claude.ai" if identity else "none",
          "email": identity, "configDirectory": str(directory)})
elif args[0] == "features":
    print("image_generation stable true")
elif args[0] == "sandbox":
    command = args[args.index("--") + 1:]
    if len(command) >= 5 and command[3] == "probe":
        read_only = any(f'{json.dumps(str(Path.cwd()))}="read"' in option for option in args)
        if read_only:
            sys.exit(1)
        Path(command[-1]).write_text("ok")
    elif command:
        os.execv(command[0], command)
elif args[0] == "app-server":
    selected_model = model
    if any("default_permissions" in option for option in args):
        (directory / "fixture-argv").write_text(json.dumps(args))
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, params = message.get("method"), message.get("params") or {}
        if not method:
            continue
        if method == "account/read":
            result = {"account": {"type": "chatgpt", "email": identity} if identity else None}
            current = workspace
            after = directory / "fixture-workspace-after"
            if "features.default_mode_request_user_input=true" in sys.argv and after.exists():
                current = after.read_text().strip()  # the executing server sees a later switch
            if identity and current:
                result["workspaceRouting"] = {"chatgptAccountId": current}
        elif method == "model/list":
            result = {"data": [
                {"model": model, "isDefault": True,
                 "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")]},
                {"model": "gpt-6.10-sol",
                 "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")]},
                {"model": "gpt-7.1-astra",
                 "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("medium", "high")]},
            ]}
        elif method == "account/rateLimits/read":
            quota = directory / "fixture-quota-used"
            percent = int(quota.read_text()) if quota.is_file() else 15
            result = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": percent, "resetsAt": time.time() + 600}}}
        elif method in ("thread/start", "thread/resume"):
            scenario = directory / "fixture-migration"
            resumed = method == "thread/resume"
            if scenario.exists():
                with (directory / "fixture-methods").open("a") as log:
                    print(method, file=log)
                if resumed:
                    assert params["threadId"] == "codex-session"
                    rollout = directory / "sessions/2026/01/02/rollout-fixture-codex-session.jsonl"
                    assert rollout.read_text() == "fixture history"
                    if scenario.read_text() == "reject":
                        emit({"id": message["id"], "error": {"message": "resume rejected"}})
                        continue
            selected_model = params.get("model") or model
            result = {"thread": {"id": "codex-session"}, "model": selected_model,
                      "activePermissionProfile": {"id": "delegate"}, "cwd": str(Path.cwd())}
            if resumed and scenario.exists():
                if scenario.read_text() == "source-cwd":
                    result["cwd"] = (directory / "fixture-source-cwd").read_text()
                elif scenario.read_text() in ("missing-profile", "full-window-missing-profile"):
                    del result["activePermissionProfile"]
                if scenario.read_text() == "full-window-missing-profile":
                    emit({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                        "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": time.time() + 600}}}})
        elif method == "turn/start":
            (directory / "fixture-turn-started").touch()
            if "reject-turn-at-rate-limit" in params["input"][0]["text"]:
                emit({"id": message["id"], "error": {"code": -32000, "message": "rate limit exceeded"}})
                continue
            if "reject-turn-too-many" in params["input"][0]["text"]:
                emit({"id": message["id"], "error": {"code": -32000, "message": "Too Many Requests"}})
                continue
            if resumed and scenario.exists() and scenario.read_text() == "full-window-turn-rejected":
                emit({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                    "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": time.time() + 600}}}})
                emit({"id": message["id"], "error": {"message": "invalid encrypted content"}})
                continue
            emit({"id": message["id"], "result": {"turn": {"id": "turn"}}})
            prompt = params["input"][0]["text"]
            for line in prompt.splitlines():
                if line.startswith("expect-config "):
                    _, expected_model, expected_effort = line.split(" ")
                    assert selected_model == expected_model and params.get("effort") == expected_effort
            if "wait-for-cancel" in prompt:
                continue
            if "ask-fixture" in prompt:
                emit({"id": "question", "method": "item/tool/requestUserInput",
                      "params": {"questions": [{"id": "color", "question": "Color?"}]}})
                response = json.loads(sys.stdin.readline())
                assert response["result"]["answers"]["color"]["answers"] == ["blue"]
            if resumed and scenario.exists() and scenario.read_text() == "retried-429-then-fail":
                emit({"method": "error", "params": {"willRetry": True, "error": {
                    "message": "HTTP 429 Too Many Requests"}}})
                emit({"method": "turn/completed", "params": {"turn": {"status": "failed"}}})
                continue
            if resumed and scenario.exists() and scenario.read_text() in ("turn-error", "full-window-error", "tool-error", "quota"):
                if scenario.read_text() == "full-window-error":
                    emit({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                        "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": time.time() + 600}}}})
                if scenario.read_text() == "tool-error":
                    emit({"method": "item/started", "params": {"item": {"type": "commandExecution"}}})
                if scenario.read_text() == "quota":
                    emit({"method": "turn/completed", "params": {"turn": {"status": "failed",
                          "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
                    continue
                emit({"method": "turn/completed", "params": {"turn": {"status": "failed",
                      "error": {"message": "invalid encrypted content"}}}})
                continue
            if "outputSchema" not in params:
                if "retry-image-rate-limit" in prompt:
                    emit({"method": "error", "params": {"willRetry": True, "error": {
                        "message": "rate limit exceeded", "codexErrorInfo": "rateLimitExceeded"}}})
                if "image-terminal-limit" in prompt:
                    emit({"method": "error", "params": {"willRetry": False, "error": {
                        "message": "limit reached", "codexErrorInfo": "usageLimitExceeded"}}})
                    emit({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "failed"}}})
                    continue
                if "image-limit-then-drop" in prompt:
                    emit({"method": "error", "params": {"willRetry": False, "error": {
                        "message": "limit reached", "codexErrorInfo": "usageLimitExceeded"}}})
                    sys.exit(0)
                if "image-failed-429" in prompt:
                    emit({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "failed",
                          "error": {"message": "HTTP 429 Too Many Requests"}}}})
                    continue
                (directory / "fixture-image-input").write_text(json.dumps(params["input"]))
                for reference in params["input"][1:]:
                    assert reference["type"] == "localImage" and Path(reference["path"]).is_file()
                if "switch-workspace-during-run" in prompt:
                    workspace = "ws-other"
                    (directory / "fixture-workspace").write_text(workspace)
                elif "switch-back-before-probe" in prompt:
                    workspace = "ws-other"  # only the executing connection still sees the switch
                folder = directory / "generated_images" / params["threadId"]
                folder.mkdir(parents=True, exist_ok=True)
                image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR3cAAAAASUVORK5CYII=")
                path = folder / "image.png"
                path.write_bytes(image)
                emit({"method": "item/completed", "params": {"item": {"type": "imageGeneration",
                      "id": "image", "status": "completed", "failure": None, "savedPath": str(path)}}})
                emit({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
                      "last": {"inputTokens": 4, "outputTokens": 2}}}})
            else:
                if "retry-code-rate-limit" in prompt.splitlines():
                    emit({"method": "error", "params": {"willRetry": True, "error": {
                        "message": "rate limit exceeded", "codexErrorInfo": "rateLimitExceeded"}}})
                edit(prompt)
                escalations(prompt, params["threadId"])
                if "code-failed-429" in prompt.splitlines():
                    emit({"method": "turn/completed", "params": {"turn": {"status": "failed",
                          "error": {"message": "HTTP 429 Too Many Requests"}}}})
                    continue
                if "codex-quota" in prompt.splitlines():
                    emit({"method": "turn/completed", "params": {"turn": {"status": "failed",
                          "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
                    continue
            emit({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": json.dumps(report)}}})
            emit({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed"}}})
            continue
        elif method == "turn/interrupt":
            result = {}
            emit({"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "interrupted"}}})
        else:
            result = {}
        emit({"id": message["id"], "result": result})
elif "/usage" in args:
    emit({"result": "Current session: 25% used\n"})
else:
    prompt = sys.stdin.read()
    emit({"type": "system", "subtype": "init", "model": model, "session_id": "claude-session"})
    edit(prompt)
    if "quota-fixture" in prompt:
        emit({"type": "result", "is_error": True, "api_error_type": "rate_limit_error"})
        sys.exit(1)
    if "background-fixture" in prompt:
        subprocess.Popen(["/bin/sh", "-c", "sleep 2; printf stray > late.txt"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if "wait-for-cancel" in prompt and "--resume" not in args:
        time.sleep(60)
    emit({"type": "result", "is_error": False, "structured_output": report, "session_id": "claude-session"})
