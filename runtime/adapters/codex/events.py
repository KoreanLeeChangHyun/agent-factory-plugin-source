"""Translate Codex app-server item notifications into runtime events."""
from __future__ import annotations

from execution.streaming import JsonStringField

# app-server item types -> runtime item types shared with the Claude adapter and the extension.
ITEM_TYPES = {"commandExecution": "command_execution", "fileChange": "file_change", "mcpToolCall": "mcp_tool_call"}


def item_event(method: str, item: dict) -> dict:
    """item/started|completed for a non-message item as an item.started|completed runtime event."""
    item = dict(item)
    item["type"] = ITEM_TYPES.get(item.get("type"), item.get("type"))
    if "exitCode" in item:
        item["exit_code"] = item.pop("exitCode")
    return {"type": method.replace("/", "."), "item": item}


def agent_message_stream(item: dict):
    """Live-preview stream for an agent message: commentary verbatim, the final message's resultText."""
    return ("commentary", None) if item.get("phase") == "commentary" else ("final", JsonStringField())
