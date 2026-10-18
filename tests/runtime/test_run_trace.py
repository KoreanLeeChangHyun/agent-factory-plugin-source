"""Diagnostic evidence follows real run identities without changing authority."""
import runtime_test_home  # noqa: F401

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from native_fixtures import runtime, native, native_fixture
from execution.prompts import PromptParts
from runs import trace


def rows(state):
    return [json.loads(line) for line in Path(state["tracePath"]).read_text().splitlines()]


class RunTraceTests(unittest.TestCase):
    def test_cli_transports_capture_messages_before_lossy_translation(self):
        for provider in ("claude", "antigravity"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                transport = importlib.import_module(f"adapters.{provider}.transport")
                bridge, _, state = native_fixture(Path(directory), goal=False, existing=False)
                state["provider"] = provider
                fixed = Path(state["statePath"]).with_name("test-instructions.txt")
                fixed.write_text("fixed instructions")
                message = {"uuid": "request-id", "message": {"role": "user", "content": "current request"}}
                response = {"type": "unknown-provider-event", "providerOnly": {"toolOutput": "full result", "model": "resolved"}}
                command = ["fake", "--append-system-prompt-file", str(fixed)]
                process = mock.Mock(stdin=mock.Mock(), stdout=io.StringIO(json.dumps(response) + "\n"))
                process.wait.return_value = process.poll.return_value = 0
                translated = mock.Mock(session="session-current", finished=True, acknowledged=True)
                translated.translate.return_value = []
                stack.enter_context(mock.patch.object(transport, "safe_read_json", side_effect=[state, bridge.session]))
                stack.enter_context(mock.patch.object(transport, "validate"))
                stack.enter_context(mock.patch.object(transport, "cli_command", return_value=(command, message if provider == "claude" else [("request", message)])))
                stack.enter_context(mock.patch.object(transport.subprocess, "Popen", return_value=process))
                stack.enter_context(mock.patch.object(transport, "Events", return_value=translated))
                stack.enter_context(mock.patch.object(transport.sys, "argv", ["transport", "state", "session"]))
                stack.enter_context(mock.patch.object(transport.sys, "stdin", io.StringIO(PromptParts("fixed", "dynamic").encode())))
                stack.enter_context(redirect_stdout(io.StringIO()))
                if provider == "claude":
                    stack.enter_context(mock.patch.object(transport, "setup_messages", return_value=[]))
                else:
                    stack.enter_context(mock.patch.object(transport, "install_agent"))
                    stack.enter_context(mock.patch.object(transport, "install_guard"))
                self.assertEqual(transport.main(), 0)
                events = [json.loads(line) for line in Path(state["providerEventsPath"]).read_text().splitlines()]
                self.assertEqual([item["data"]["body"] for item in events], [message, response])
                delivery = next(item for item in rows(state) if item["event"] == "provider.delivery.requested")
                self.assertFalse(delivery["data"]["snapshot"]["redacted"])

    def test_provider_journal_preserves_large_unknown_fields_and_masks_nested_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            message = {"type": "future-tool-event", "timestamp": "2026-10-15T16:00:00Z",
                       "arguments": json.dumps({"password": "nested-private", "safe": "한글"}),
                       "unknown": "😀" * 600000, "usage": {"input_tokens": 123, "cached_tokens": 50},
                       "env": {"UNCLASSIFIED_VALUE": "must-not-collect"}}
            trace.provider_message(state, "receive", message)
            saved = json.loads(Path(state["providerEventsPath"]).read_text())
            self.assertEqual(saved["data"]["body"]["unknown"], message["unknown"])
            self.assertEqual(saved["data"]["body"]["usage"], message["usage"])
            self.assertEqual(saved["data"]["sourceTimestamp"], message["timestamp"])
            self.assertIsInstance(saved["monotonicNs"], int)
            self.assertEqual(saved["writerPid"], os.getpid())
            self.assertEqual(saved["resource"]["serviceName"], "agent-factory")
            self.assertNotIn("nested-private", json.dumps(saved))
            self.assertNotIn("must-not-collect", json.dumps(saved))
            self.assertIn("nested-private", message["arguments"])

    def test_native_wire_observer_keeps_notifications_and_replies_before_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            notification = {"method": "future/notification", "params": {"toolCallId": "call-1", "output": "complete output"}}
            response = {"id": 1, "result": {"model": "actual-model", "unknown_field": 42}}
            process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(notification) + "\n" + json.dumps(response) + "\n"))
            process.poll.return_value = None
            rpc = native.Rpc(process)
            rpc.reader.join(2)
            wrapped = trace.DeliveryRpc(rpc, state)
            self.assertEqual(wrapped.call("config/read", {}), response["result"])
            self.assertEqual(wrapped.event(), notification)
            messages = [json.loads(line) for line in Path(state["providerEventsPath"]).read_text().splitlines()]
            self.assertEqual([event["data"]["direction"] for event in messages], ["send", "receive", "receive"])
            self.assertEqual(messages[1]["data"]["body"], notification)
            self.assertEqual(messages[2]["data"]["body"], response)

    def test_native_invalid_wire_frame_is_logged_before_parse_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            process = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO('malformed password=private\n'))
            process.poll.return_value = None
            rpc = native.Rpc(process)
            rpc.reader.join(2)
            wrapped = trace.DeliveryRpc(rpc, state)
            with self.assertRaises(ValueError):
                wrapped.event()
            saved = json.loads(Path(state["providerEventsPath"]).read_text())
            self.assertEqual(saved["data"]["direction"], "receive-invalid")
            self.assertFalse(saved["data"]["validJson"])
            self.assertNotIn("private", saved["data"]["body"])

    def test_pooled_observer_changes_run_without_duplicating_previous_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, first = native_fixture(root, goal=False)
            _, _, second = native_fixture(root, goal=False)
            rpc = mock.Mock(observer=None)
            trace.DeliveryRpc(rpc, first)
            rpc.observer("receive", {"type": "first"})
            before = Path(first["providerEventsPath"]).read_bytes()
            trace.DeliveryRpc(rpc, second)
            rpc.observer("receive", {"type": "second"})
            self.assertEqual(Path(first["providerEventsPath"]).read_bytes(), before)
            self.assertEqual(len(Path(second["providerEventsPath"]).read_text().splitlines()), 1)
            self.assertEqual(json.loads(Path(second["providerEventsPath"]).read_text())["runId"], second["runId"])

    def test_invalid_protocol_message_is_retained_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            trace.provider_message(state, "receive", 'not-json password=private-value\n')
            saved = json.loads(Path(state["providerEventsPath"]).read_text())
            self.assertFalse(saved["data"]["validJson"])
            self.assertEqual(saved["severity"], "WARN")
            self.assertIn("not-json", saved["data"]["body"])
            self.assertNotIn("private-value", saved["data"]["body"])

    def test_stream_redaction_handles_split_credentials_and_multiline_keys(self):
        text = ('normal\npassword=private-value\n-----BEGIN RSA PRIVATE KEY-----\nprivate-bytes\n'
                '-----END RSA PRIVATE KEY-----\nafter\npostgres://user:private-url@example.test/db\n'
                '--api-key private-flag\nlast line')
        for chunk_size in (1, 3, 20, 8192):
            redactor = trace.StreamRedactor()
            saved = "".join(redactor.feed(text[i:i + chunk_size]) for i in range(0, len(text), chunk_size))
            saved += redactor.feed("", final=True)
            for secret in ("private-value", "private-bytes", "private-url", "private-flag"):
                self.assertNotIn(secret, saved)
            self.assertIn("normal\n", saved)
            self.assertTrue(saved.endswith("last line"))
        redactor = trace.StreamRedactor()
        self.assertEqual(redactor.feed("x" * 10000), "")
        self.assertEqual(redactor.feed("", final=True), "x" * 10000)

    def test_redacted_jsonl_keeps_line_boundaries_and_valid_json(self):
        original = ' {"password": "private", "text": "한글"}\r\n'
        masked = trace.redact(original)
        self.assertTrue(masked.startswith(" "))
        self.assertTrue(masked.endswith("\r\n"))
        self.assertEqual(json.loads(masked)["text"], "한글")
        self.assertEqual(len((masked * 2).splitlines()), 2)
        self.assertEqual(trace.redact(["cmd", "--token", "private", "visible"]),
                         ["cmd", "--token", "[REDACTED]", "visible"])

    def test_exception_records_stack_and_duration_without_frame_locals(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            never_collect_this_local = "local-secret-value"
            try:
                raise ValueError("password=exception-private")
            except ValueError as error:
                trace.exception(state, "test.failed", error, durationMs=12.5)
            saved = rows(state)[-1]
            self.assertEqual(saved["severity"], "ERROR")
            self.assertEqual(saved["data"]["durationMs"], 12.5)
            self.assertIn("ValueError", saved["data"]["stacktrace"])
            self.assertNotIn("exception-private", json.dumps(saved))
            self.assertNotIn(never_collect_this_local, json.dumps(saved))

    def test_parent_child_request_and_criteria_are_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, parent = native_fixture(root, goal=False, existing=False)
            binding = {"workflowId": "workflow-one", "taskId": "task-one",
                       "completionCriteria": "Required output is present"}
            child = runtime.create_run(project_root=root, agent_id="work-child", actor="main",
                request="명령서 😀".encode(), session={"role": "work", "maxAttempts": 2},
                parent_agent_id=parent["agentId"], parent_run_id=parent["runId"], task_binding=binding)
            accepted = rows(child)[0]
            self.assertEqual(accepted["parentRunId"], parent["runId"])
            self.assertEqual(accepted["workflowId"], binding["workflowId"])
            self.assertEqual(accepted["data"]["taskBinding"], binding)
            self.assertEqual(accepted["data"]["request"]["sha256"], child["requestHash"])
            link = rows(parent)[-1]
            self.assertEqual(link["event"], "child.accepted")
            self.assertEqual(link["data"]["childTracePath"], child["tracePath"])

    def test_loop_decisions_link_to_parent_without_claiming_verification(self):
        import loop
        with tempfile.TemporaryDirectory() as directory:
            _, _, parent = native_fixture(Path(directory), goal=False)
            path = Path(parent["statePath"]).with_name("loop-test-state.json")
            state = {"loopId": "loop-test", "statePath": str(path), "parentStatePath": parent["statePath"],
                     "status": "completed", "phase": "done", "humanSkip": {"source": "user request"},
                     "lastVerificationDecision": None, "execution": {"taskMode": "work"}}
            with mock.patch.object(loop, "publish_progress"):
                loop.save_loop_state(path, state)
            event = rows(parent)[-1]
            self.assertEqual(event["event"], "loop.transition")
            self.assertEqual(event["data"]["stateRevision"], json.loads(path.read_text())["stateRevision"])
            self.assertIsNone(event["data"]["lastVerificationDecision"])
            self.assertEqual(event["data"]["humanSkip"], state["humanSkip"])

    def test_snapshot_is_immutable_unicode_and_redacts_recognized_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            payload = {"fixed": "규칙 😀\nAuthorization: Bearer abc.secret.token\nOPENAI_API_KEY=private-value",
                       "dynamic": "line1\r\nline2", "password": "hidden", "api_key": "other-hidden"}
            reference = trace.snapshot(state, "prompt.prepared", payload, attempt=1)
            saved = json.loads(Path(reference["path"]).read_text())
            self.assertTrue(saved["redacted"])
            self.assertEqual(saved["payload"]["dynamic"], payload["dynamic"])
            self.assertIn("규칙 😀", saved["payload"]["fixed"])
            for secret in ("abc.secret.token", "private-value", "hidden", "other-hidden"):
                self.assertNotIn(secret, Path(reference["path"]).read_text())
            original = Path(reference["path"]).read_bytes()
            second = trace.snapshot(state, "prompt.prepared", {"dynamic": "revised"}, attempt=2)
            self.assertNotEqual(reference["path"], second["path"])
            self.assertEqual(Path(reference["path"]).read_bytes(), original)
            self.assertEqual(reference["sha256"], hashlib.sha256(original).hexdigest())
            if os.name != "nt":
                self.assertEqual(Path(reference["path"]).stat().st_mode & 0o777, 0o600)

    def test_concurrent_writers_keep_complete_unique_events(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda i: trace.record(state, "test.concurrent", ordinal=i), range(60)))
            events = [row for row in rows(state) if row["event"] == "test.concurrent"]
            self.assertEqual(len(events), 60)
            self.assertEqual(len({row["eventId"] for row in events}), 60)
            self.assertEqual({row["data"]["ordinal"] for row in events}, set(range(60)))

    def test_snapshot_failure_does_not_block_rpc_and_is_visible(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            _, _, state = native_fixture(Path(directory), goal=False)
            rpc = mock.Mock()
            rpc.call.return_value = {"turn": {"id": "turn-ack"}}
            original = trace.files.atomic_write_json
            def fail_snapshot(path, value):
                if path.name.startswith("prompt-"):
                    raise OSError("disk full; api_key=must-not-appear")
                return original(path, value)
            with mock.patch.object(trace.files, "atomic_write_json", side_effect=fail_snapshot):
                result = trace.DeliveryRpc(rpc, state).call("turn/start", {"input": [{"text": "request"}]})
            self.assertEqual(result["turn"]["id"], "turn-ack")
            rpc.call.assert_called_once()
            warning = Path(state["traceErrorPath"]).read_text()
            self.assertIn('"incomplete": true', warning)
            self.assertNotIn("must-not-appear", warning)

    def test_trace_symlink_is_rejected_without_touching_target_or_result(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            _, _, state = native_fixture(Path(directory), goal=False)
            target = Path(directory) / "unrelated.txt"
            target.write_text("preserve")
            Path(state["tracePath"]).unlink()
            Path(state["tracePath"]).symlink_to(target)
            self.assertIsNone(trace.record(state, "test.unsafe"))
            self.assertEqual(target.read_text(), "preserve")
            self.assertTrue(Path(state["traceErrorPath"]).exists())
            self.assertEqual(runtime.safe_read_json(Path(state["statePath"]))["status"], "accepted")

    def test_delivery_records_actual_inherited_prompt_and_ack(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory), goal=False, existing=False)
            bridge.session["nativeCapabilities"] = {"instructionDelivery": True}
            call = rpc.call
            def inherited(method, params, **kwargs):
                if method == "config/read":
                    return {"config": {"developer_instructions": "Inherited rule", "api_key": "never-collected"}}
                return call(method, params, **kwargs)
            rpc.call = inherited
            bridge.setup(PromptParts("Fixed rule", "Current request"))
            events = rows(state)
            deliveries = [row for row in events if row["event"] == "provider.delivery.requested"]
            documents = [json.loads(Path(row["data"]["snapshot"]["path"]).read_text()) for row in deliveries]
            self.assertEqual(documents[0]["payload"]["developerInstructions"], "Inherited rule\n\nFixed rule")
            self.assertEqual(documents[1]["payload"]["input"][0]["text"], "Current request")
            self.assertNotIn("never-collected", json.dumps(documents))
            self.assertEqual(events[-1]["event"], "provider.delivery.acknowledged")
            self.assertEqual(events[-1]["data"]["turnId"], "turn-0")

    def test_delivery_failure_is_not_an_ack_and_preserves_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            rpc = mock.Mock()
            error = RuntimeError("transport failed")
            rpc.call.side_effect = error
            with self.assertRaises(RuntimeError) as raised:
                trace.DeliveryRpc(rpc, state).call("turn/start", {"input": []})
            self.assertIs(raised.exception, error)
            self.assertEqual(rows(state)[-1]["event"], "provider.delivery.failed")

    def test_cancelled_terminal_records_committed_state_and_does_not_replace_it(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, state = native_fixture(Path(directory), goal=False)
            path = Path(state["statePath"])
            runtime.update_json(path, path.parent / ".state.lock", lambda value: value.update(cancelRequested=True))
            runtime.mark_terminal(path, "completed")
            self.assertEqual(rows(state)[-1]["data"]["status"], "cancelled")
            self.assertEqual(runtime.safe_read_json(path)["status"], "cancelled")
            self.assertFalse(runtime.mark_terminal(path, "failed", active_only=True))
            self.assertEqual(len([row for row in rows(state) if row["event"] == "run.terminal"]), 1)

    def test_retry_and_failure_preserve_attempt_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, state = native_fixture(root, goal=False)
            path = Path(state["statePath"])
            runtime.update_json(path, path.parent / ".state.lock", lambda value: value.update(maxAttempts=2))
            session_path = runtime.session_file(root, state["agentId"])
            runtime.update_json(session_path, session_path.parent / ".session-state.lock",
                                lambda value: value.update(agentId=state["agentId"], heartbeatInterval=5))
            args = argparse.Namespace(project_root=root, agent=state["agentId"], run_id=state["runId"])
            with mock.patch.object(runtime, "Heartbeat"), mock.patch.object(runtime, "run_codex_attempt",
                    side_effect=runtime.AttemptFailure("codex_start_failed", "No launch", False)), \
                    mock.patch.object(runtime, "capture_lesson"):
                self.assertEqual(runtime.worker(args), 1)
            events = rows(state)
            self.assertEqual([row["attempt"] for row in events if row["event"] == "attempt.started"], [1, 2])
            self.assertEqual([row["data"]["nextAttempt"] for row in events if row["event"] == "attempt.retry_scheduled"], [2])
            self.assertEqual(events[-1]["data"]["status"], "failed")

    def test_attempt_indexes_exact_provider_bytes_and_distinguishes_receipt_rejection(self):
        for role in ("main", "work"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bridge, _, state = native_fixture(root, goal=False, existing=False, role=role, response_contract=1)
                terminal = {"status": "completed", "resultPath": state["resultPath"], "resultText": "결과 😀"}
                lines = [json.dumps(event, ensure_ascii=False) + "\n" for event in [
                    {"type": "thread.started", "thread_id": "thread-exact"},
                    {"type": "item.completed", "item": {"type": "command_execution", "aggregatedOutput": "검사 결과 password=private-output"}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(terminal, ensure_ascii=False)}}]]
                process = mock.Mock(pid=12345, stdout=io.StringIO("".join(lines)), stderr=io.StringIO())
                process.poll.return_value = None
                process.wait.return_value = 0
                adapter = mock.Mock()
                adapter.check.return_value = {"passed": True}
                adapter.uses_prompt_parts.return_value = False
                adapter.build_command.return_value = ["fake-provider"]
                adapter.persisted_fields.return_value = {}
                with mock.patch.object(runtime.adapters, "for_session", return_value=adapter), \
                        mock.patch.object(runtime, "spawn_contained_process", return_value=(process, {}, None)), \
                        mock.patch.object(runtime, "release_contained_process"), \
                        mock.patch.object(runtime, "terminate_attempt_group"), \
                        mock.patch.object(runtime, "capture_lesson"):
                    kwargs = dict(project_root=root, session=bridge.session, state=state, attempt=1,
                                  heartbeat=mock.Mock(), cancel_event=threading.Event(),
                                  expected_agent_id=state["agentId"], expected_run_id=state["runId"])
                    if role == "main":
                        self.assertEqual(runtime.run_codex_attempt(**kwargs), ("completed", "thread-exact"))
                    else:
                        with self.assertRaises(runtime.AttemptFailure):
                            runtime.run_codex_attempt(**kwargs)
                events = rows(state)
                indexed = [row["data"]["source"] for row in events if row["event"] == "provider.event"]
                content = Path(state["eventsPath"]).read_bytes()
                self.assertEqual(len(indexed), len(lines))
                for reference, line in zip(indexed, lines):
                    start = reference["offsetBytes"]
                    part = content[start:start + reference["lengthBytes"]]
                    self.assertEqual(part, trace.redact(line).encode())
                    self.assertEqual(hashlib.sha256(part).hexdigest(), reference["sha256"])
                self.assertEqual(Path(state["resultPath"]).read_text(), terminal["resultText"])
                self.assertNotIn(b"private-output", content)
                kinds = [row["event"] for row in events]
                self.assertIn("prompt.prepared", kinds)
                self.assertIn("result.reported", kinds)
                self.assertEqual(kinds[-1], "result.validated" if role == "main" else "receipt.rejected")
                if role == "work":
                    self.assertNotIn("receipt.validated", kinds)

    def test_cli_trace_pages_are_lossless_and_old_runs_are_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, state = native_fixture(root, goal=False)
            trace.record(state, "test.utf8", text="한글 😀\nsecond line")
            args = runtime.parse_args(["status", "--project-root", directory, "--agent", state["agentId"],
                                       "--run-id", state["runId"], "--document", "trace", "--length", "200"])
            pieces = []
            while True:
                with mock.patch.object(runtime, "emit") as emit:
                    runtime.command_status(args)
                page = emit.call_args.args[0]
                pieces.append(page["text"])
                if page["nextOffset"] is None:
                    break
                args.offset, args.revision = page["nextOffset"], page["revision"]
            self.assertEqual("".join(pieces), Path(state["tracePath"]).read_text())
            path = Path(state["statePath"])
            runtime.update_json(path, path.parent / ".state.lock", lambda value: value.pop("tracePath"))
            with self.assertRaises(runtime.ContractError) as error:
                runtime.command_status(args)
            self.assertEqual(error.exception.code, "read_document_unavailable")


if __name__ == "__main__":
    unittest.main()
