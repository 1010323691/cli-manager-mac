"""Projection permits visible progress while omitting raw provider messages."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import activity_output


def events(*messages):
    return b"".join(json.dumps(message).encode() + b"\n" for message in messages)


class ActivityOutputTest(unittest.TestCase):
    def test_codex_only_projects_public_commentary_and_tool_progress(self):
        def item(method, kind, **fields):
            return {"dir": "in", "method": f"item/{method}", "params": {"item": {
                "id": kind, "type": kind, **fields}}}

        content = events(
            item("started", "agentMessage", phase="commentary"),
            {"dir": "in", "method": "item/agentMessage/delta", "params": {
                "itemId": "agentMessage", "delta": "Reviewing the project.\x07"}},
            item("started", "commandExecution", command="raw command payload", aggregatedOutput="raw bytes"),
            item("completed", "reasoning", content="hidden reasoning"),
            item("completed", "userMessage", content="user prompt"),
            item("completed", "agentMessage", phase="final_answer", text="structured final report"),
            {"dir": "in", "id": 5, "method": "item/tool/requestUserInput", "params": {"questions": ["pending"]}},
        )
        result = activity_output.projected_entries(content + b'{"partial"', "codex")
        self.assertEqual([row["text"] for row in result], ["Reviewing the project.", "正在执行命令"])

    def test_claude_projection_filters_private_and_actionable_fields(self):
        content = events({"type": "assistant", "uuid": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Reviewing the project."},
            {"type": "thinking", "thinking": "hidden reasoning"},
            {"type": "tool_use", "name": "Read", "input": {"path": "private tool argument"}},
            {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": "pending"}},
        ]}}, {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "raw tool output"},
        ]}})
        result = activity_output.projected_entries(content, "claude")
        self.assertEqual([row["text"] for row in result], ["Reviewing the project.", "读取文件"])

    def test_projection_retains_bounded_recent_entries(self):
        content = events(*({"type": "assistant", "uuid": str(i), "message": {
            "role": "assistant", "content": [{"type": "text", "text": str(i)}]
        }} for i in range(100)))
        result = activity_output.projected_entries(content, "claude")
        self.assertEqual(len(result), activity_output.MAX_ENTRIES)
        self.assertEqual(result[-1]["text"], "99")
