"""Live text previews: partial JSON decoding, Claude partial messages and Codex deltas."""
from __future__ import annotations

import runtime_test_home  # Isolate all runtime subprocesses from the real home.

import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from native_fixtures import native_fixture
from adapters import claude
from execution.streaming import DeltaBuffer, JsonStringField


def deltas(events, stream):
    return "".join(event["text"] for event in events if event.get("type") == "native.delta" and event["stream"] == stream)


class JsonStringFieldTests(unittest.TestCase):
    def test_decodes_result_text_across_arbitrary_fragment_boundaries(self):
        value = {"status": "completed", "resultText": "줄1\n\"인용\" \\ tab\t emoji 😀 é end", "decisionKind": None}
        document = json.dumps(value)
        for size in (1, 2, 3, 7):
            with self.subTest(size=size):
                field = JsonStringField()
                decoded = "".join(field.feed(document[index:index + size]) for index in range(0, len(document), size))
                self.assertEqual(decoded, value["resultText"])
                self.assertTrue(field.closed)
        ascii_only = json.dumps(value, ensure_ascii=True)
        field = JsonStringField()
        self.assertEqual("".join(field.feed(character) for character in ascii_only), value["resultText"])

    def test_ignores_other_fields_and_non_string_values(self):
        field = JsonStringField()
        self.assertEqual(field.feed('{"note": "resultText inside", "resultText": "ok"}'), "ok")
        field = JsonStringField()
        self.assertEqual(field.feed('{"resultText": null}'), "")
        self.assertEqual(JsonStringField().feed("plain plan text"), "")

    def test_buffer_coalesces_until_interval_and_flushes_per_stream(self):
        now = [0.0]
        buffer = DeltaBuffer(interval=0.1, clock=lambda: now[0])
        self.assertEqual(buffer.add("commentary", "a", "He"), [])
        now[0] = 0.05
        self.assertEqual(buffer.add("commentary", "a", "llo"), [])
        self.assertEqual(buffer.add("final", "b", "X"), [])
        self.assertEqual(buffer.flush("a"), [{"type": "native.delta", "stream": "commentary", "id": "a", "text": "Hello"}])
        now[0] = 0.2
        self.assertEqual(buffer.add("final", "b", "Y"), [{"type": "native.delta", "stream": "final", "id": "b", "text": "XY"}])


class ClaudeStreamingTests(unittest.TestCase):
    def start(self, **options):
        session_id = str(uuid.uuid4())
        events = claude.Events(session_id, **options)
        events.deltas.interval = 0
        events.translate({"type": "system", "subtype": "init", "session_id": session_id})
        return events, session_id

    @staticmethod
    def stream(event, parent=None):
        return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}

    def test_text_and_structured_result_stream_before_their_complete_events(self):
        events, session_id = self.start()
        out = []
        out += events.translate(self.stream({"type": "message_start", "message": {"id": "msg-1"}}))
        out += events.translate(self.stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}))
        out += events.translate(self.stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hidden"}}))
        out += events.translate(self.stream({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}))
        for piece in ("Work", "ing", " now"):
            out += events.translate(self.stream({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}}))
        out += events.translate({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working now"}]}})
        out += events.translate(self.stream({"type": "content_block_stop", "index": 1}))
        out += events.translate(self.stream({"type": "message_start", "message": {"id": "msg-2"}}))
        out += events.translate(self.stream({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_final", "name": "StructuredOutput", "input": {}}}))
        document = json.dumps({"status": "completed", "resultText": "최종 **답변**\n끝", "decisionKind": None})
        for index in range(0, len(document), 5):
            out += events.translate(self.stream({"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "input_json_delta", "partial_json": document[index:index + 5]}}))
        out += events.translate(self.stream({"type": "content_block_stop", "index": 0}))
        # The final answer's transport tool is never shown as a tool call.
        out += events.translate({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_final", "name": "StructuredOutput", "input": {}}]}})
        out += events.translate({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_final", "content": "ok"}]}})
        self.assertFalse([event for event in out if event.get("item", {}).get("tool") == "StructuredOutput"])
        out += events.translate({"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
                                 "structured_output": json.loads(document), "usage": {}})
        self.assertEqual(deltas(out, "commentary"), "Working now")
        self.assertEqual(deltas(out, "final"), "최종 **답변**\n끝")
        self.assertNotIn("hidden", json.dumps(out, ensure_ascii=False))
        kinds = [event["type"] for event in out]
        self.assertLess(max(i for i, event in enumerate(out) if event.get("stream") == "commentary"), kinds.index("native.commentary"))
        self.assertEqual(kinds[-1], "item.completed")

    def test_subagent_unacknowledged_and_planning_output_is_not_previewed(self):
        events, _ = self.start(request_id="request-1")
        # Resume drains an earlier turn before our request is acknowledged.
        self.assertEqual(events.translate(self.stream({"type": "message_start", "message": {"id": "old"}})), [])
        events.acknowledged = True
        events.translate(self.stream({"type": "message_start", "message": {"id": "m"}}))
        self.assertEqual(events.translate(self.stream({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}, parent="toolu_task")), [])
        events.translate(self.stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}))
        self.assertEqual(events.translate(self.stream({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "sub"}}, parent="toolu_task")), [])
        planning, _ = self.start(terminal=False)
        planning.translate(self.stream({"type": "message_start", "message": {"id": "p"}}))
        planning.translate(self.stream({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "t", "name": "StructuredOutput"}}))
        self.assertEqual(planning.translate(self.stream({"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"resultText": "plan"'}})), [])


class CodexStreamingTests(unittest.TestCase):
    def test_commentary_and_final_result_text_stream_from_app_server_deltas(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, _ = native_fixture(Path(directory), goal=False)
            bridge.deltas.interval = 0
            final = json.dumps({"status": "completed", "resultPath": rpc.result_path, "resultText": "Native answer"})
            commentary = json.dumps({"status": "completed", "resultPath": rpc.result_path,
                                     "resultText": "Checking files", "decisionKind": None})
            completed = rpc.events[1]
            completed["params"]["item"]["id"] = "final-1"
            thread = {"threadId": "thread-exact"}
            rpc.events[1:1] = [
                {"method": "item/started", "params": {**thread, "item": {"id": "c-1", "type": "agentMessage", "phase": "commentary", "text": ""}}},
                *({"method": "item/agentMessage/delta", "params": {**thread, "itemId": "c-1", "delta": commentary[i:i + 3]}}
                  for i in range(0, len(commentary), 3)),
                {"method": "item/completed", "params": {**thread, "item": {"id": "c-1", "type": "agentMessage", "phase": "commentary", "text": commentary}}},
                {"method": "item/started", "params": {**thread, "item": {"id": "final-1", "type": "agentMessage", "text": ""}}},
                *({"method": "item/agentMessage/delta", "params": {**thread, "itemId": "final-1", "delta": final[i:i + 4]}}
                  for i in range(0, len(final), 4)),
                {"method": "item/agentMessage/delta", "params": {**thread, "itemId": "unknown", "delta": "ignored"}},
            ]
            bridge.run("Main role")
            events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(deltas(events, "commentary"), "Checking files")
        self.assertEqual(deltas(events, "final"), "Native answer")
        self.assertNotIn("ignored", json.dumps(events))
        kinds = [event["type"] for event in events]
        self.assertLess(max(i for i, event in enumerate(events) if event.get("stream") == "commentary"),
                        max(i for i, event in enumerate(events) if event["type"] == "native.commentary"))
        self.assertIn({"type": "native.commentary", "text": "Checking files"}, events)
        self.assertIn("item.completed", kinds)

    def test_plain_commentary_stream_remains_compatible(self):
        from adapters.codex.events import agent_message_stream, commentary_text

        _, stream = agent_message_stream({"phase": "commentary"})
        self.assertEqual(stream.feed("  Plain "), "  Plain ")
        self.assertEqual(stream.feed("update"), "update")
        self.assertEqual(commentary_text({"text": "  Plain update"}), "  Plain update")
        self.assertEqual(commentary_text({"text": '{"other":"value"}'}), '{"other":"value"}')


if __name__ == "__main__":
    unittest.main()


class EventLogDurabilityTests(unittest.TestCase):
    def test_previews_skip_fsync_but_remain_in_order(self):
        from unittest import mock
        from system import transport as process_transport
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            writer = process_transport.EventLogWriter(path)
            with mock.patch.object(process_transport.os, "fsync") as fsync:
                writer.append('{"type": "native.delta", "text": "a"}\n', durable=False)
                writer.append('{"type": "native.delta", "text": "b"}\n', durable=False)
                self.assertEqual(fsync.call_count, 0)
                writer.append('{"type": "turn.completed"}\n')
                self.assertEqual(fsync.call_count, 1)
            writer.close()
            self.assertEqual([json.loads(line)["type"] for line in path.read_text().splitlines()],
                             ["native.delta", "native.delta", "turn.completed"])
