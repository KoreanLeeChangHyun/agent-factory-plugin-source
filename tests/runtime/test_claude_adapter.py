"""Provider identity, permission fidelity and stream/result protocol regressions."""
import runtime_test_home  # noqa: F401 - imported for its side effect: isolates the runtime home
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

    def test_structured_receipt_schema_reaches_execution_turns_but_not_the_plan_phase(self):
        self.assertIs(claude.final_output_schema({"goalMode": True}), True)
        with tempfile.TemporaryDirectory() as root:
            goal = {"taskMode": "plan-work", "goalMode": True}
            prepared = runtime.create_run(project_root=Path(root), agent_id="claude-structured", actor="main", request=b"test",
                                          session={"role": "work", "maxAttempts": 1, "provider": "claude"}, execution_options=goal)
            self.assertEqual(prepared["responseContract"], 2)
            session = {"claude": "/local/claude", "executionPolicy": POLICY, "projectRoot": root}
            receipt_fields = {"outcome", "changedPaths", "tests", "addressedFindingIds"}
            for phase, expected in ((None, True), ("execute", True), ("plan", False)):
                with self.subTest(phase=phase):
                    command, _ = claude.cli_command(session, prepared, PromptParts("fixed", "request"), phase)
                    schema = json.loads(command[command.index("--json-schema") + 1])
                    self.assertEqual(receipt_fields <= set(schema["properties"]), expected)
                    self.assertEqual(receipt_fields <= set(schema["required"]), expected)
                    self.assertTrue({"status", "resultPath", "resultText", "decisionKind"} <= set(schema["required"]))
                    self.assertNotIn("$schema", schema)
            # The persisted contract is untouched by the plan-phase trim.
            self.assertTrue(receipt_fields <= set(runtime.safe_read_json(Path(prepared["responseSchemaPath"]))["properties"]))

    def test_orchestrate_main_is_limited_to_reads_run_files_and_plugin_scripts(self):
        with tempfile.TemporaryDirectory() as root:
            prepared = runtime.create_run(project_root=Path(root), agent_id="claude-orchestrate", actor="main", request=b"test",
                                          session={"role": "main", "maxAttempts": 1})
            directory = Path(prepared["statePath"]).parent
            schema = directory / "schema.json"
            schema.write_text(json.dumps(runtime.response_schema_document(str(directory / "result.md"))))
            session = {"claude": "/local/claude", "executionPolicy": POLICY, "projectRoot": root}
            state = {"statePath": str(directory / "state.json"), "responseSchemaPath": str(schema), "role": "main",
                     "executionOptions": {"taskMode": "orchestrate"}}
            command, _ = claude.cli_command(session, state, PromptParts("fixed", "request"))
            self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
            allowed = command[command.index("--allowedTools") + 1].split(",")
            self.assertIn(f"Write(/{directory}/**)", allowed)
            self.assertTrue(any(tool.startswith("Bash(python3 ") and tool.endswith("/scripts/*)") for tool in allowed))
            self.assertNotIn("Edit", allowed)
            self.assertNotIn("Bash", allowed)
            self.assertNotIn("WebSearch", allowed)
            for mode, role in (("direct", "main"), ("work", "work")):
                other, _ = claude.cli_command(session, {**state, "role": role, "executionOptions": {"taskMode": mode}},
                                              PromptParts("fixed", "request"))
                self.assertEqual(other[other.index("--permission-mode") + 1], "bypassPermissions")
                self.assertNotIn("--allowedTools", other)

    def test_work_may_spawn_only_the_read_only_explore_subagent(self):
        # Human decision 2026-10-03: launch configuration, not prompt wording, blocks every other sub-agent type.
        import shlex
        import subprocess
        from tasks import subagent_guard
        with tempfile.TemporaryDirectory() as root:
            prepared = runtime.create_run(project_root=Path(root), agent_id="claude-work-subagents", actor="main", request=b"test",
                                          session={"role": "main", "maxAttempts": 1})
            directory = Path(prepared["statePath"]).parent
            schema = directory / "schema.json"
            schema.write_text(json.dumps(runtime.response_schema_document(str(directory / "result.md"))))
            state = {"statePath": str(directory / "state.json"), "responseSchemaPath": str(schema)}
            modes = {}
            for sandbox in ({"type": "danger-full-access"}, {"type": "workspace-write", "writable_roots": [root]}, {"type": "read-only"}):
                policy = runtime.execution_policy.normalize({"schemaVersion": 1, "sandboxPolicy": sandbox, "approvalPolicy": "never"})
                session = {"claude": "/local/claude", "executionPolicy": policy, "projectRoot": root}
                for phase in (None, "plan", "execute"):
                    command, _ = claude.cli_command(session, {**state, "role": "work", "executionOptions": {"taskMode": "plan-work"}},
                                                    PromptParts("fixed", "request"), phase)
                    modes[command[command.index("--permission-mode") + 1]] = True
                    hooks = json.loads(command[command.index("--settings") + 1])["hooks"]["PreToolUse"]
                    self.assertEqual([(hook["matcher"], hook["hooks"][0]["command"]) for hook in hooks],
                                     [(subagent_guard.MATCHER, subagent_guard.HOOK_COMMAND)])
                    self.assertEqual(command[command.index("--disallowedTools") + 1], "Workflow")
                for role, mode in (("main", "direct"), ("main", "orchestrate"), ("verification", "verification")):
                    other, _ = claude.cli_command(session, {**state, "role": role, "executionOptions": {"taskMode": mode}},
                                                  PromptParts("fixed", "request"))
                    self.assertNotIn("--settings", other)
                    self.assertNotIn("--disallowedTools", other)
            self.assertEqual(set(modes), {"bypassPermissions", "acceptEdits", "dontAsk", "plan"})

        def decide(payload):
            result = subprocess.run(shlex.split(subagent_guard.HOOK_COMMAND), input=payload, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] if result.stdout.strip() else "allow"

        def call(**tool_input):
            return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input})
        self.assertEqual(decide(call(subagent_type="Explore", prompt="find the parser")), "allow")
        for denied in (call(subagent_type="general-purpose"), call(subagent_type="Plan"), call(subagent_type="explore"),
                       call(prompt="no type defaults to general-purpose"), call(subagent_type=["Explore"]),
                       json.dumps({"tool_name": "Agent"}), "not json"):
            self.assertEqual(decide(denied), "deny", denied)
        import re
        for tool, matched in (("Agent", True), ("Task", True), ("Bash", False), ("AgentOutput", False), ("TaskCreate", False)):
            self.assertEqual(bool(re.search(subagent_guard.MATCHER, tool)), matched, tool)

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
                       "executionPolicy": POLICY, "projectRoot": root, "thinkingDisplay": True}
            command, message = claude.cli_command(session, state, PromptParts("fixed instruction", "current request"))
            self.assertIn("--include-partial-messages", command)
            self.assertEqual(command[command.index("--thinking-display") + 1], "summarized")
            for unsupported in ({**session, "thinkingDisplay": False}, {k: v for k, v in session.items() if k != "thinkingDisplay"}):
                self.assertNotIn("--thinking-display", claude.cli_command(unsupported, state, PromptParts("fixed", "request"))[0])
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
        self.assertEqual(events.translate({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.25}, "seven_day": {"utilization": 0.19}}}}),
            [{"type": "provider.rate_limits", "fiveHourUsedPercent": 25.0, "weeklyUsedPercent": 19.0}])
        self.assertEqual(events.translate({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.25}}}}), [{"type": "provider.rate_limits", "fiveHourUsedPercent": 25.0}])
        self.assertEqual(events.translate({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 2}}}}), [])
        self.assertEqual(events.translate({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.01, "resetsAt": 1790506200},
            "seven_day": {"utilization": 0, "resetsAt": 1790953200}}}}),
            [{"type": "provider.rate_limits", "fiveHourUsedPercent": 1.0, "fiveHourResetsAt": 1790506200,
              "weeklyUsedPercent": 0, "weeklyResetsAt": 1790953200}])
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

    def test_structured_marker_becomes_interview_question(self):
        session_id = str(uuid.uuid4())
        events = claude.Events(session_id)
        events.translate({"type": "system", "subtype": "init", "session_id": session_id})
        marker = ('<agent-factory-interview-question>{"id":"interview-1-of-1","current":1,"total":1,'
                  '"text":"Proceed?","options":[{"value":"yes","label":"Yes","pros":"Continue",'
                  '"cons":"Uses time"},{"value":"no","label":"No","pros":"Stop now","cons":"No result"}],'
                  '"recommendedValue":"yes","yesNo":true}</agent-factory-interview-question>')
        translated = events.translate({"type": "result", "subtype": "success", "is_error": False,
            "session_id": session_id, "structured_output": {
                "status": "needs-human-decision", "resultText": marker + "\nChoose one."}, "usage": {}})
        question = next(event for event in translated if event["type"] == "interview.question")["question"]
        self.assertEqual((question["options"][0]["value"], question["yesNo"]), ("yes", True))
        self.assertEqual(json.loads(translated[-1]["item"]["text"])["resultText"], "Choose one.")

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
        help_text = " ".join(claude.capabilities.REQUIRED_OPTIONS)
        with mock.patch.object(claude.capabilities.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=help_text)):
            capabilities = claude.inspect_capabilities("claude")
        with mock.patch.object(claude.capabilities.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout=help_text.replace("--replay-user-messages", ""))):
            self.assertFalse(claude.inspect_capabilities("claude", refresh=True)["submit"]["model"])
        self.assertEqual(capabilities["submit"]["taskModes"], list(claude.capabilities.TASK_MODES))
        self.assertIn("plan-work-verification", capabilities["submit"]["taskModes"])
        self.assertTrue(capabilities["submit"]["plan"])
        self.assertTrue(capabilities["submit"]["goal"])
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


class ClaudeAdapterCompletenessTests(unittest.TestCase):
    def events(self):
        session_id = str(uuid.uuid4())
        events = claude.Events(session_id)
        events.translate({"type": "system", "subtype": "init", "session_id": session_id})
        return events

    def test_every_file_editing_tool_is_a_file_change(self):
        events = self.events()
        started = events.translate({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "id": "m", "name": "MultiEdit", "input": {"file_path": "b.py", "edits": []}},
            {"type": "tool_use", "id": "n", "name": "NotebookEdit", "input": {"notebook_path": "c.ipynb"}},
            {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "d.py"}}]}})
        self.assertEqual([(e["item"]["type"], e["item"].get("changes", [{}])[0].get("path")) for e in started],
                         [("file_change", "a.py"), ("file_change", "b.py"), ("file_change", "c.ipynb"), ("mcp_tool_call", None)])

    def test_todo_list_reports_step_progress_and_the_active_step(self):
        events = self.events()
        todos = [{"content": "Read code", "activeForm": "Reading code", "status": "completed"},
                 {"content": "Edit panel", "activeForm": "Editing the panel", "status": "in_progress"},
                 {"content": "Run checks", "activeForm": "Running checks", "status": "pending"}]
        started = events.translate({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "TodoWrite", "input": {"todos": todos}}]}})
        self.assertEqual(started[-1], {"type": "plan.progress", "completed": 1, "total": 3, "current": "Editing the panel"})
        nested = events.translate({"type": "assistant", "parent_tool_use_id": "task", "message": {"content": [
            {"type": "tool_use", "id": "u", "name": "TodoWrite", "input": {"todos": todos}}]}})
        self.assertFalse([event for event in nested if event["type"] == "plan.progress"])

    def test_tool_results_keep_their_text_and_real_error_message(self):
        events = self.events()
        events.translate({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "g", "name": "Grep", "input": {}},
            {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": "x"}}]}})
        done = events.translate({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "g", "content": [{"type": "text", "text": "3 matches"}]},
            {"type": "tool_result", "tool_use_id": "e", "content": "old_string not found", "is_error": True}]}})
        self.assertEqual(done[0]["item"]["result"], "3 matches")
        self.assertEqual(done[1]["item"]["error"], "old_string not found")
        self.assertEqual(done[1]["item"]["status"], "failed")

    def test_tool_targets_web_tools_and_mcp_names_are_summarized(self):
        events = self.events()
        started = events.translate({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "src/a.py", "offset": 120, "limit": 40}},
            {"type": "tool_use", "id": "g", "name": "Grep", "input": {"pattern": "x" * 500, "path": "src", "glob": "*.py",
                                                                      "output_mode": "content", "-n": True}},
            {"type": "tool_use", "id": "s", "name": "WebSearch", "input": {"query": "agent timeline rows"}},
            {"type": "tool_use", "id": "f", "name": "WebFetch", "input": {"url": "https://example.com/a/b", "prompt": "summarize"}},
            {"type": "tool_use", "id": "m", "name": "mcp__playwright__browser_take_screenshot", "input": {"filename": "a.png"}},
            {"type": "tool_use", "id": "t", "name": "TodoWrite", "input": {"todos": []}}]}})
        items = [event["item"] for event in started]
        self.assertEqual(items[0], {"id": "r", "type": "mcp_tool_call", "server": "claude", "tool": "Read",
                                    "arguments": {"file_path": "src/a.py", "offset": 120, "limit": 40}})
        self.assertEqual(items[1]["arguments"], {"pattern": "x" * 200, "path": "src", "glob": "*.py"})
        self.assertEqual(items[2], {"id": "s", "type": "web_search", "action": {"type": "search", "query": "agent timeline rows"}})
        self.assertEqual(items[3], {"id": "f", "type": "web_search", "action": {"type": "openPage", "url": "https://example.com/a/b"}})
        self.assertEqual(items[4], {"id": "m", "type": "mcp_tool_call", "server": "playwright", "tool": "browser_take_screenshot"})
        self.assertEqual(items[5], {"id": "t", "type": "mcp_tool_call", "server": "claude", "tool": "TodoWrite"})
        done = events.translate({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s", "content": "Search failed", "is_error": True}]}})
        self.assertEqual((done[0]["item"]["type"], done[0]["item"]["status"]), ("web_search", "failed"))

    def test_failed_bash_keeps_claude_exit_status(self):
        events = self.events()
        events.translate({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm test"}}]}})
        done = events.translate({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "Exit code 2\nnpm ERR! test failed", "is_error": True}]}})
        self.assertEqual((done[0]["item"]["status"], done[0]["item"]["exit_code"]), ("failed", 2))
        self.assertEqual(done[0]["item"]["error"], "Exit code 2\nnpm ERR! test failed")

    def test_thinking_becomes_a_reasoning_item_once(self):
        events = self.events()
        stream = lambda data: events.translate({"type": "stream_event", "event": data})
        stream({"type": "message_start", "message": {"id": "msg_1"}})
        self.assertEqual(stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
                         [{"type": "item.started", "item": {"id": "msg_1:thinking:0", "type": "reasoning"}}])
        stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Check the "}})
        stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "renderer."}})
        self.assertEqual(stream({"type": "content_block_stop", "index": 0}), [{"type": "item.completed", "item": {
            "id": "msg_1:thinking:0", "type": "reasoning", "summary": ["Check the renderer."]}}])
        # The complete message repeats the streamed thought; it is not shown twice.
        self.assertEqual(events.translate({"type": "assistant", "message": {"id": "msg_1", "content": [
            {"type": "thinking", "thinking": "Check the renderer."}]}}), [])
        # Without partial messages the finished thought is still recorded.
        self.assertEqual(events.translate({"type": "assistant", "message": {"id": "msg_2", "content": [
            {"type": "thinking", "thinking": "Plan"}]}}), [{"type": "item.completed", "item": {
                "id": "msg_2:thinking:0", "type": "reasoning", "summary": ["Plan"]}}])

    def test_subagent_tools_are_visible_but_subagent_prose_and_usage_are_not(self):
        events = self.events()
        out = events.translate({"type": "assistant", "parent_tool_use_id": "task-1", "message": {
            "usage": {"input_tokens": 9, "cache_read_input_tokens": 9, "cache_creation_input_tokens": 9},
            "content": [{"type": "text", "text": "subagent thinking"},
                        {"type": "tool_use", "id": "s", "name": "Bash", "input": {"command": "ls"}}]}})
        self.assertEqual([e["type"] for e in out], ["item.started"])
        self.assertEqual(out[0]["item"]["parentToolUseId"], "task-1")
        self.assertIsNone(events.context_tokens)
        done = events.translate({"type": "user", "parent_tool_use_id": "task-1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s", "content": "ok"}]}})
        self.assertEqual(done[0]["item"]["status"], "completed")

    def test_capability_probe_is_cached_and_names_missing_options(self):
        with tempfile.TemporaryDirectory() as home:
            full = mock.Mock(returncode=0, stdout=" ".join(claude.capabilities.REQUIRED_OPTIONS))
            with mock.patch.object(claude.capabilities.subprocess, "run", return_value=full) as run:
                first = claude.inspect_capabilities("/bin/true", runtime_home=home)
                second = claude.inspect_capabilities("/bin/true", runtime_home=home)
                self.assertEqual(run.call_count, 2)  # --help and --version, once.
                self.assertEqual(first, second)
                claude.inspect_capabilities("/bin/true", runtime_home=home, refresh=True)
                self.assertEqual(run.call_count, 4)
            old = mock.Mock(returncode=0, stdout=full.stdout.replace("--include-partial-messages", ""))
            with mock.patch.object(claude.capabilities.subprocess, "run", return_value=old):
                result = claude.inspect_capabilities("/bin/true", runtime_home=home, refresh=True)
            self.assertIn("--include-partial-messages", result["diagnostic"])
            self.assertFalse(result["submit"]["model"])

    def test_thinking_display_requires_claude_2_1_40_or_newer(self):
        help_text = " ".join(claude.capabilities.REQUIRED_OPTIONS)
        cases = (("2.1.285 (Claude Code)", 0, True), ("2.1.40 (Claude Code)", 0, True), ("3.0.0", 0, True),
                 ("2.1.39 (Claude Code)", 0, False), ("1.0.128 (Claude Code)", 0, False),
                 ("unknown", 0, False), ("", 1, False))
        for version, code, expected in cases:
            def run(argv, **_kwargs):
                return mock.Mock(returncode=code, stdout=version) if argv[-1] == "--version" else mock.Mock(returncode=0, stdout=help_text)  # noqa: B023 - the closure is only called within this iteration
            with self.subTest(version=version), mock.patch.object(claude.capabilities.subprocess, "run", side_effect=run):
                capabilities = claude.inspect_capabilities("claude", refresh=True)
                self.assertTrue(capabilities["submit"]["model"])
                self.assertIs(capabilities["submit"]["thinkingDisplay"], expected)
                self.assertIs(claude.session_fields("claude", capabilities)["thinkingDisplay"], expected)
        with mock.patch.object(claude.capabilities.subprocess, "run", side_effect=[mock.Mock(returncode=0, stdout=help_text), OSError("gone")]):
            self.assertFalse(claude.inspect_capabilities("claude", refresh=True)["submit"]["thinkingDisplay"])
        self.assertFalse(claude.session_fields("claude")["thinkingDisplay"])

    def test_default_mode_uses_claude_config_dir_and_unknown_modes_stay_read_only(self):
        with tempfile.TemporaryDirectory() as config, tempfile.TemporaryDirectory() as project:
            (Path(config) / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config}):
                policy = claude.discover_policy(None, project)
            self.assertEqual(policy["sandboxPolicy"]["type"], "workspace-write")
            for mode in ("default", "plan", "dontAsk", "somethingNew"):
                (Path(config) / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": mode}}))
                with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config}):
                    self.assertEqual(claude.discover_policy(None, project)["sandboxPolicy"]["type"], "read-only", mode)

    def test_unsupported_reasoning_effort_is_rejected(self):
        claude.policy.validate({"reasoningEffort": "minimal"})
        with self.assertRaises(runtime.ContractError):
            claude.policy.validate({"reasoningEffort": "extreme"})
