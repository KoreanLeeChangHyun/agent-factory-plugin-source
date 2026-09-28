"""Provider identity, permission fidelity and stream/result protocol regressions."""
import runtime_test_home
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock
from contextlib import redirect_stdout

from native_fixtures import runtime
from adapters import provider_for, claude
from execution.prompts import PromptParts
from execution.usage import UsageAccumulator

POLICY = runtime.execution_policy.normalize({"schemaVersion": 1,
    "sandboxPolicy": {"type": "danger-full-access"}, "approvalPolicy": "never"})


class ClaudeAdapterTests(unittest.TestCase):
    def test_provider_identity_never_resumes_other_provider(self):
        self.assertEqual(provider_for(), "codex")
        self.assertEqual(provider_for("claude-sonnet"), "claude")
        self.assertEqual(provider_for(session={"provider": "claude"}), "claude")
        with self.assertRaisesRegex(runtime.ContractError, "new chat"):
            provider_for("claude-opus", session={"sessionId": str(uuid.uuid4())})
        self.assertEqual(provider_for("claude-opus", session={"sessionId": None}), "claude")
        with self.assertRaises(runtime.ContractError):
            provider_for("gpt-6", "claude")

    def test_every_route_policy_and_effort_is_accepted_and_mapped(self):
        session = {"executionPolicy": POLICY, "role": "main", "model": "claude-opus", "taskMode": "direct"}
        for change in ({"fast": True}, {"goalMode": True}, {"taskMode": "plan"}, {"role": "work"},
                       {"reasoningEffort": "none"}, {"executionPolicy": {**POLICY, "approvalPolicy": "on-request"}}):
            with self.subTest(change=change):
                claude.validate({**session, **change})
        with self.assertRaises(runtime.ContractError):
            claude.validate({**session, "model": "gpt-6"})
        self.assertIsNone(claude.policy.effort("none"))
        self.assertEqual(claude.policy.effort("minimal"), "low")
        self.assertEqual(claude.policy.effort("xhigh"), "xhigh")
        with tempfile.TemporaryDirectory() as root:
            for sandbox, mode in (("danger-full-access", "bypassPermissions"), ("workspace-write", "acceptEdits"), ("read-only", "dontAsk")):
                raw = {"type": sandbox, **({"writable_roots": [root, "/tmp"]} if sandbox == "workspace-write" else {})}
                policy = runtime.execution_policy.normalize({"schemaVersion": 1, "sandboxPolicy": raw, "approvalPolicy": "never"})
                arguments = claude.policy.permission_arguments({"executionPolicy": policy}, root)
                with self.subTest(sandbox=sandbox):
                    self.assertEqual(arguments[:4], ["--permission-mode", mode, "--permission-prompts", "none"])
                    self.assertEqual(arguments[4:], ["--add-dir", "/tmp"] if sandbox == "workspace-write" else [])

    def test_cli_default_reads_claude_settings_without_probing_codex(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"HOME": home}, clear=True):
            args = runtime.parse_args(["submit", "--agent", "test", "--role", "main", "--model", "claude-opus", "--message", "test"])
            with mock.patch.object(runtime.execution_policy, "_configured_policy") as discover:
                self.assertEqual(runtime.resolve_execution_policy(args, Path(root))["sandboxPolicy"]["type"], "read-only")
                settings = Path(root) / ".claude"
                settings.mkdir()
                (settings / "settings.local.json").write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
                self.assertEqual(runtime.resolve_execution_policy(args, Path(root))["sandboxPolicy"]["type"], "danger-full-access")
                discover.assert_not_called()

    def test_plan_only_records_plan_and_host_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            prepared = runtime.create_run(project_root=Path(root), agent_id="claude-plan", actor="main", request=b"test",
                                          session={"role": "work", "maxAttempts": 1})
            state = runtime.safe_read_json(Path(prepared["statePath"]))
            Path(state["receiptSchemaPath"]).parent.mkdir(parents=True, exist_ok=True)
            Path(state["receiptSchemaPath"]).write_text(json.dumps({"properties": {}}))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertFalse(claude.transport.finish_planning(state, {"status": "completed", "resultText": "1. do it"}, execute_next=False))
            directory = Path(state["statePath"]).parent
            self.assertEqual(json.loads((directory / "plan.json").read_text()), {"status": "planned", "plan": "1. do it"})
            receipt = json.loads(Path(state["receiptPath"]).read_text())
            self.assertEqual((receipt["outcome"], receipt["changedPaths"], receipt["tests"]["reason"]), ("completed", [], "plan-only"))
            terminal = json.loads(json.loads(output.getvalue())["item"]["text"])
            self.assertEqual((terminal["status"], terminal["resultText"]), ("completed", "1. do it"))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertTrue(claude.transport.finish_planning(state, {"status": "completed", "resultText": "1. do it"}, execute_next=True))
            self.assertIn("Implementation is starting", output.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertFalse(claude.transport.finish_planning(state, {"status": "needs-human-decision", "resultText": "which db?"}, execute_next=True))
            self.assertEqual(json.loads((directory / "plan.json").read_text())["status"], "needs-human-decision")

    def test_plan_routes_plan_then_resume_same_session(self):
        work = {"role": "work", "executionOptions": {"taskMode": "plan-work"}}
        self.assertEqual(claude.transport.planning_phases(work), ["plan", "execute"])
        self.assertEqual(claude.transport.planning_phases({**work, "executionOptions": {"taskMode": "plan"}}), ["plan"])
        self.assertEqual(claude.transport.planning_phases({**work, "executionOptions": {"taskMode": "work"}}), [None])
        self.assertEqual(claude.transport.planning_phases({"role": "main", "executionOptions": {"taskMode": "plan"}}), [None])

    def test_command_preserves_current_instructions_resume_schema_and_images(self):
        with tempfile.TemporaryDirectory() as root:
            prepared = runtime.create_run(project_root=Path(root), agent_id="claude-command", actor="main", request=b"test",
                                          session={"role": "main", "maxAttempts": 1})
            directory = Path(prepared["statePath"]).parent
            schema = directory / "schema.json"
            original_schema = runtime.response_schema_document(str(directory / "result.md"))
            schema.write_text(json.dumps(original_schema))
            image = directory / "test.png"
            image.write_bytes(b"image-fixture")
            state = {"statePath": str(directory / "state.json"), "responseSchemaPath": str(schema),
                     "imageInputs": [{"path": str(image), "mediaType": "image/png"}]}
            session = {"claude": "/local/claude", "sessionId": str(uuid.uuid4()), "model": "claude-sonnet", "reasoningEffort": "high",
                       "executionPolicy": POLICY, "projectRoot": root}
            command, message = claude.cli_command(session, state, PromptParts("fixed instruction", "current request"))
            self.assertIn("--include-partial-messages", command)
            self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
            planned, plan_message = claude.cli_command(session, state, PromptParts("fixed instruction", "current request"), "plan")
            self.assertEqual(planned[planned.index("--permission-mode") + 1], "plan")
            self.assertIn("do not modify files", plan_message["message"]["content"][0]["text"])
            executed, execute_message = claude.cli_command(session, state, PromptParts("fixed instruction", "current request"), "execute")
            self.assertEqual(executed[executed.index("--permission-mode") + 1], "bypassPermissions")
            self.assertEqual(executed[executed.index("--resume") + 1], session["sessionId"])
            self.assertIn("plan above is approved", execute_message["message"]["content"][0]["text"])
            self.assertEqual(command[command.index("--resume") + 1], session["sessionId"])
            self.assertIn("--replay-user-messages", command)
            self.assertEqual(str(uuid.UUID(message["uuid"])), message["uuid"])
            self.assertEqual(len({message["uuid"], plan_message["uuid"], execute_message["uuid"]}), 3)
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            transmitted_schema = json.loads(command[command.index("--json-schema") + 1])
            self.assertEqual(transmitted_schema, {key: value for key, value in original_schema.items() if key != "$schema"})
            self.assertEqual(json.loads(schema.read_text()), original_schema)
            self.assertEqual(command[command.index("--system-prompt-snapshot") + 1], "off")
            fixed = Path(command[command.index("--append-system-prompt-file") + 1]).read_text()
            self.assertIn("fixed instruction", fixed)
            self.assertIn("skills/agent/SKILL.md", fixed)
            blocks = message["message"]["content"]
            self.assertEqual(blocks[0]["text"], "current request")
            self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), image.read_bytes())

    def test_stream_result_usage_tools_and_session_identity(self):
        session_id = str(uuid.uuid4())
        events = claude.Events(session_id)
        self.assertEqual(events.translate({"type": "system", "subtype": "init", "session_id": session_id})[0],
                         {"type": "thread.started", "thread_id": session_id})
        started = events.translate({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hello"}}]}})
        self.assertEqual(started[0]["type"], "native.commentary")
        self.assertEqual(started[1]["item"]["command"], "echo hello")
        done = events.translate({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "failure", "is_error": True}]}})
        self.assertEqual(done[0]["item"]["status"], "failed")
        self.assertNotIn("exit_code", done[0]["item"])
        self.assertEqual(len(done), 1)
        terminal = {"status": "completed", "resultPath": "/tmp/result.md", "resultText": "done", "decisionKind": None}
        events.translate({"type": "assistant", "message": {"content": [], "usage": {
            "input_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 20}}})
        normalized = events.translate({"type": "result", "subtype": "success", "is_error": False,
            "session_id": session_id, "structured_output": terminal,
            "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}, "claude-haiku-4-5": {"contextWindow": 200000}},
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10, "output_tokens": 7}})
        self.assertIn({"type": "provider.context", "usedTokens": 1025, "contextWindowTokens": 1000000}, normalized)
        accumulator = UsageAccumulator()
        self.assertTrue(accumulator.observe(normalized[0]))
        self.assertEqual(accumulator.snapshot()["inputTokens"], 42)
        self.assertEqual(accumulator.snapshot()["cachedInputTokens"], 30)
        self.assertEqual(json.loads(normalized[-1]["item"]["text"]), terminal)
        self.assertEqual(events.translate({"type": "system", "subtype": "hook_response"}), [])
        planning = claude.Events(session_id, terminal=False)
        planning.translate({"type": "system", "subtype": "init", "session_id": session_id})
        planned = planning.translate({"type": "result", "subtype": "success", "is_error": False,
            "session_id": session_id, "structured_output": {**terminal, "resultText": "the plan"}, "usage": {}})
        self.assertNotIn("agent_message", json.dumps(planned))
        self.assertEqual(planning.structured["resultText"], "the plan")

    def test_failed_missing_and_wrong_session_results_are_not_success(self):
        session_id = str(uuid.uuid4())
        for changes in ({"is_error": True}, {"structured_output": None}, {"session_id": str(uuid.uuid4())}):
            with self.subTest(changes=changes):
                events = claude.Events()
                events.translate({"type": "system", "subtype": "init", "session_id": session_id})
                with self.assertRaises(ValueError):
                    events.translate({"type": "result", "subtype": "success", "is_error": False,
                        "session_id": session_id, "structured_output": {}, **changes})
        with self.assertRaises(ValueError):
            claude.Events(session_id).translate({"type": "system", "subtype": "init", "session_id": str(uuid.uuid4())})

    def test_resume_drains_old_results_and_finishes_only_acknowledged_request(self):
        session_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        init = {"type": "system", "subtype": "init", "session_id": session_id}
        result = {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}
        ack = {"type": "user", "uuid": request_id, "session_id": session_id,
               "message": {"role": "user", "content": "current request"}}
        events = claude.Events(session_id, request_id=request_id)
        events.translate(init)
        for stale in (result, {**result, "structured_output": {"resultText": "old answer"}},
                      {**ack, "uuid": str(uuid.uuid4())}, {**ack, "parent_tool_use_id": "child"},
                      {**ack, "session_id": str(uuid.uuid4())}):
            self.assertEqual(events.translate(stale), [])
            self.assertFalse(events.acknowledged)
            self.assertFalse(events.finished)
        events.translate(ack)
        self.assertTrue(events.acknowledged)
        with self.assertRaisesRegex(ValueError, "structured_output"):
            events.translate(result)

        # Exercise main's stream loop: it must not kill the child on an old result,
        # launch a duplicate request, or report success when the current one is missing.
        for ending, success in (([ack, {**result, "structured_output": {"resultText": "current answer"}}], True),
                                ([], False), ([ack, result], False)):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as root:
                state = {"runtimeBinding": {}, "role": "main"}
                session = {"sessionId": session_id, "executionPolicy": POLICY, "projectRoot": root}
                process = mock.Mock()
                process.stdin = mock.Mock()
                process.stdout = io.StringIO("\n".join(json.dumps(e) for e in [init, result, *ending]))
                process.wait.return_value = 0
                process.poll.return_value = 0
                output = io.StringIO()
                with mock.patch.object(claude.transport, "safe_read_json", side_effect=[state, session]), \
                        mock.patch.object(claude.transport.runtime_paths, "bind"), \
                        mock.patch.object(claude.transport, "cli_command", return_value=(["claude"], ack)), \
                        mock.patch.object(claude.transport.subprocess, "Popen", return_value=process) as popen, \
                        mock.patch.object(claude.transport.sys, "argv", ["transport", "state", "session"]), \
                        mock.patch.object(claude.transport.sys, "stdin", io.StringIO(PromptParts("fixed", "request").encode())), \
                        redirect_stdout(output):
                    self.assertEqual(claude.transport.main(), 0 if success else 1)
                popen.assert_called_once()
                process.stdin.write.assert_called_once()
                process.terminate.assert_not_called()
                emitted = [json.loads(line) for line in output.getvalue().splitlines()]
                finals = [e for e in emitted if e.get("item", {}).get("type") == "agent_message"]
                self.assertEqual(len(finals), int(success))
                if success:
                    self.assertEqual(json.loads(finals[0]["item"]["text"])["resultText"], "current answer")
                else:
                    self.assertEqual(emitted[-1]["type"], "error")

    def test_capabilities_and_session_creation_use_claude_executable(self):
        help_text = "--print --output-format --input-format --json-schema --permission-prompts --system-prompt-snapshot --replay-user-messages"
        with mock.patch.object(claude.capabilities.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=help_text)):
            capabilities = claude.inspect_capabilities("claude")
        with mock.patch.object(claude.capabilities.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout=help_text.replace("--replay-user-messages", ""))):
            self.assertFalse(claude.inspect_capabilities("claude")["submit"]["model"])
        self.assertEqual(capabilities["submit"]["taskModes"], list(claude.capabilities.TASK_MODES))
        self.assertIn("plan-work-verification", capabilities["submit"]["taskModes"])
        self.assertTrue(capabilities["submit"]["plan"])
        self.assertFalse(capabilities["submit"]["goal"])
        self.assertTrue(capabilities["submit"]["worktrees"])
        with tempfile.TemporaryDirectory() as root, mock.patch.object(claude, "inspect_capabilities", return_value=capabilities):
            args = runtime.parse_args(["submit", "--agent", "claude-test", "--role", "main", "--model", "claude-opus", "--claude", "/bin/true", "--message", "hi"])
            args.resolved_execution_policy = POLICY
            session = runtime.create_session(args, Path(root))
            self.assertEqual(session["provider"], "claude")
            self.assertEqual(session["backend"], "claude-print")
            self.assertEqual(session["claude"], str(Path("/bin/true").resolve()))
            self.assertIsNone(session["sessionId"])

    def test_cleared_session_switch_is_saved_before_worker_launch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            args = runtime.parse_args(["submit", "--agent", "switch-test", "--role", "main", "--codex", "/bin/true", "--message", "hi"])
            args.resolved_execution_policy = POLICY
            caps = {"submit": {"instructionDelivery": False}}
            with mock.patch.object(runtime.native_codex, "inspect_capabilities", return_value=caps):
                runtime.create_session(args, root)
            send = runtime.parse_args(["send", "--project-root", str(root), "--agent", "switch-test", "--model", "claude-opus", "--message", "hi"])
            def launch(project, agent, run):
                saved = runtime.load_session(project, agent)
                self.assertEqual(saved["provider"], "claude")
                self.assertEqual(saved["backend"], "claude-print")
                self.assertIsNone(saved["sessionId"])
                return 12345
            with mock.patch("shutil.which", return_value="/bin/true"), mock.patch.object(runtime, "spawn_worker", side_effect=launch), redirect_stdout(io.StringIO()):
                self.assertEqual(runtime.submit(send, False), 0)


if __name__ == "__main__":
    unittest.main()
