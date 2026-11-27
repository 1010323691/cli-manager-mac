"""Bounded, read-only projection of visible managed-provider activity."""
import json
import os
import stat
import unicodedata
from contextlib import ExitStack

MAX_TAIL = 512 * 1024
MAX_ENTRIES = 60
MAX_TEXT = 2_400

CODEX_ACTIONS = {"read": "读取文件", "listFiles": "列出文件", "search": "搜索内容"}
CLAUDE_TOOLS = {
    "Bash": "执行命令", "Read": "读取文件", "Glob": "查找文件", "Grep": "搜索内容",
    "Edit": "修改文件", "Write": "写入文件", "MultiEdit": "修改文件",
}


def events_tail(root, run):
    # Walk below the trusted state root using directory fds. Never follow a
    # replaced symlink into another instance, an auth file, or a user path.
    with ExitStack() as fds:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.callback(os.close, directory)
        for component in run.relative_to(root).parts:
            if component in {"", ".", ".."}:
                return b""
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            fds.callback(os.close, directory)
        file = os.open("events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(file, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                return b""
            start = max(0, info.st_size - MAX_TAIL)
            stream.seek(max(0, start - 1))
            content = stream.read(info.st_size - stream.tell())
    if start:
        _, _, content = content.partition(b"\n")
    return content


def display_text(value):
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value[:MAX_TEXT]
                   if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    return text.strip()


def codex_entries(events):
    entries = {}
    commentary = {}
    for event in events:
        if event.get("dir") != "in" or "id" in event:
            continue
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict):
            continue
        if method == "item/agentMessage/delta":
            key = params.get("itemId")
            if isinstance(key, str) and key in commentary:
                delta = params.get("delta")
                if isinstance(delta, str):
                    text = commentary[key] = (commentary[key] + delta)[:MAX_TEXT]
                    entries[key] = ("message", display_text(text))
            continue
        if method not in {"item/started", "item/completed"}:
            continue
        item = params.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        key, kind = item["id"], item.get("type")
        if kind == "agentMessage" and item.get("phase") == "commentary":
            if method == "item/started":
                commentary[key] = ""
            else:
                commentary.pop(key, None)
                entries[key] = ("message", display_text(item.get("text")))
        elif kind == "commandExecution":
            actions = item.get("commandActions")
            action = "执行命令"
            if isinstance(actions, list) and actions and isinstance(actions[0], dict):
                first = actions[0].get("type")
                if isinstance(first, str) and all(isinstance(row, dict) and row.get("type") == first for row in actions):
                    action = CODEX_ACTIONS.get(first, action)
            state = "正在" if method == "item/started" else "已完成" if item.get("status") == "completed" else "已结束"
            entries[key] = ("operation", state + action if method == "item/started" else f"{action} · {state}")
        elif kind == "fileChange":
            label = "正在处理文件变更" if method == "item/started" else "文件变更操作已结束"
            entries[key] = ("operation", label)
    return list(entries.items())


def claude_entries(events):
    entries = {}
    for index, event in enumerate(events):
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            key = (event.get("uuid") if isinstance(event.get("uuid"), str) else str(index)) + f"/{block_index}"
            if block.get("type") == "text":
                text = display_text(block.get("text"))
                if text and not text.startswith(("{", "[")):
                    entries[key] = ("message", text)
            elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                if label := CLAUDE_TOOLS.get(block["name"]):
                    entries[key] = ("operation", label)
    return list(entries.items())


def projected_entries(content, provider):
    providers = {"codex": codex_entries, "claude": claude_entries}
    if provider not in providers:
        return []
    events = []
    for line in content.split(b"\n")[:-1]:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            events.append(event)
    rendered = [{"id": key, "kind": kind, "text": text}
                for key, (kind, text) in providers[provider](events) if text]
    return rendered[-MAX_ENTRIES:]
