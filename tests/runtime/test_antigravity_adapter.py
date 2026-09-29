"""Antigravity (agy) provider selection, launch arguments and recorded stream translation."""
import runtime_test_home
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from native_fixtures import runtime
from adapters import provider_for, antigravity
from adapters.antigravity import command, policy
from adapters.antigravity.events import Events, split_answer
from execution.prompts import PromptParts

CONVERSATION = "c3e9eb44-b219-40bb-9d33-de43e12bccee"


def step(index, state, kind, **fields):
    return {"event": "step_update", "step_update": {"conversation_id": CONVERSATION, "step_index": index,
                                                     "state": state, "step_type": kind, **fields}}


def usage(**counts):
    return {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0, **counts}


INIT = {"event": "init", "conversation_id": CONVERSATION,
        "init": {"model": "gemini-3.8-flash-low", "cwd": "/tmp/agy-probe", "tools": ["view_file", "run_command"],
                 "permission_mode": "request-review"}}
# Recorded from agy 1.2.12 (gemini-3.8-flash-low): read a file, answer with the schema result.
ANSWER = '{"content":"hello","status":"ok","toolAction":"Finishing task","toolSummary":"Complete task"}\n'
RECORDED = [
    INIT,
    step(0, "DONE", "user_input"),
    step(1, "DONE", "agent_response", usage=usage(input_tokens=12436, output_tokens=99, thinking_tokens=54)),
    step(2, "ACTIVE", "tool", tool_name="view_file",
         tool_info={"name": "view_file", "parameters": {"AbsolutePath": "/tmp/agy-probe/note.txt"}}),
    step(2, "DONE", "tool", tool_name="view_file",
         tool_info={"name": "view_file", "parameters": {"AbsolutePath": "/tmp/agy-probe/note.txt"}, "output": "2 lines, 6 bytes"}),
    step(3, "DONE", "agent_response", text_delta=ANSWER),
    step(4, "ACTIVE", "tool", tool_name="finish", tool_info={"name": "finish"}),
    step(4, "DONE", "finish"),
    {"event": "result", "result": {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": ANSWER,
                                   "num_turns": 1, "structured_output": {"content": "hello", "status": "ok"},
                                   "usage": usage(input_tokens=25156, output_tokens=190, thinking_tokens=108,
                                                  cache_read_tokens=8000)}},
]


def translate(events, **options):
    translator = Events(**options)
    return translator, [item for event in events for item in translator.translate(event)]


class AntigravityProviderTests(unittest.TestCase):
    def test_gemini_models_select_antigravity_and_its_sessions_accept_any_model(self):
        self.assertEqual(provider_for("gemini-3.8-flash-high"), "antigravity")
        self.assertEqual(provider_for("claude-sonnet-4-6", "antigravity"), "antigravity")
        self.assertEqual(provider_for("antigravity/claude-sonnet-4-6", session={"provider": "antigravity", "sessionId": CONVERSATION}),
                         "antigravity")
        self.assertEqual(provider_for("claude-sonnet-4-6"), "claude")
        # A plain claude-* model in an Antigravity conversation is Claude Code, which needs a new chat.
        with self.assertRaisesRegex(runtime.ContractError, "new chat"):
            provider_for("claude-opus-5-5", session={"provider": "antigravity", "sessionId": CONVERSATION})
        self.assertEqual(policy.native_model("antigravity/gpt-oss-120b-medium"), "gpt-oss-120b-medium")
        self.assertEqual(policy.effort_arguments({"model": "antigravity/gemini-3.8-flash", "reasoningEffort": "low"}),
                         ["--effort", "low"])
        with self.assertRaises(runtime.ContractError):
            provider_for("gemini-3.8-flash-high", "claude")
        with self.assertRaisesRegex(runtime.ContractError, "new chat"):
            provider_for("gemini-3.1-pro-high", session={"provider": "codex", "sessionId": "thread-1"})

    def test_policy_maps_to_agy_permissions_and_effort(self):
        with tempfile.TemporaryDirectory() as root:
            def arguments(raw):
                normalized = runtime.execution_policy.normalize({"schemaVersion": 1, "sandboxPolicy": raw, "approvalPolicy": "never"})
                return policy.permission_arguments({"executionPolicy": normalized}, root)
            self.assertEqual(arguments({"type": "danger-full-access"}), ["--dangerously-skip-permissions"])
            self.assertEqual(arguments({"type": "workspace-write", "writable_roots": [root, "/tmp"]}), ["--add-dir", "/tmp"])
            self.assertEqual(arguments({"type": "read-only"}), [])
        self.assertIsNone(policy.effort("none"))
        self.assertEqual([policy.effort(v) for v in ("minimal", "high", "xhigh", "ultra")], ["low", "high", "max", "max"])
        policy.validate({"goalMode": True, "model": "claude-opus-4-6-thinking"})
        with self.assertRaises(runtime.ContractError):
            policy.validate({"reasoningEffort": "extreme"})
        # Observed agy 1.2.12: base Gemini IDs take --effort; level-suffixed IDs and other families reject it.
        for model, expected in (("gemini-3.8-flash", ["--effort", "high"]), ("gemini-3.8-flash-low", []),
                                ("claude-sonnet-4-6", []), ("gpt-oss-120b-medium", []), (None, ["--effort", "high"])):
            with self.subTest(model=model):
                self.assertEqual(policy.effort_arguments({"model": model, "reasoningEffort": "high"}), expected)
        # Observed: gemini-3.1-pro offers only low and high, so medium resolves to the nearest (higher) level.
        offered = {"gemini-3.1-pro": ["high", "low"]}
        for requested, expected in (("medium", "high"), ("minimal", "low"), ("xhigh", "high"), ("high", "high")):
            with self.subTest(requested=requested):
                self.assertEqual(policy.effort_arguments({"model": "gemini-3.1-pro", "reasoningEffort": requested,
                                                          "effortLevels": offered}), ["--effort", expected])
        self.assertEqual(policy.effort_arguments({"model": "gemini-3.8-flash", "reasoningEffort": "medium",
                                                  "effortLevels": offered}), ["--effort", "medium"])

    def test_launch_sends_instructions_and_schema_on_stdin_message(self):
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps({"$schema": "x", "type": "object"}), encoding="utf-8")
            full = runtime.execution_policy.normalize({"schemaVersion": 1, "sandboxPolicy": {"type": "danger-full-access"},
                                                        "approvalPolicy": "never"})
            session = {"agy": "agy", "projectRoot": directory, "executionPolicy": full, "sessionId": CONVERSATION,
                       "model": "gemini-3.1-pro", "reasoningEffort": "xhigh"}
            state = {"responseSchemaPath": str(schema_path), "role": "work", "statePath": f"{directory}/run/state.json"}
            argv, turns = command.cli_command(session, state, PromptParts("FIXED", "DYNAMIC"))
            self.assertEqual([kind for kind, _ in turns], ["request"])
            message = turns[0][1]
            self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object", "properties": {}, "required": []})
            for pair in (["--conversation", CONVERSATION], ["--model", "gemini-3.1-pro"], ["--effort", "max"]):
                self.assertIn(pair, [argv[i:i + 2] for i in range(len(argv))])
            self.assertIn("--dangerously-skip-permissions", argv)
            self.assertIn("--disable-slash-commands", argv)
            self.assertIn(["--agent", command.AGENT_NAME], [argv[i:i + 2] for i in range(len(argv))])
            self.assertEqual(argv[-1], "--print=")
            text = message["message"]["content"][0]["text"]
            self.assertEqual(message["event"], "user")
            self.assertTrue(text.startswith("<agent-factory-instructions>\nFIXED"))
            self.assertIn("DYNAMIC", text)
            self.assertIn(f"Working directory (resolve relative paths here): {directory}", text)
            plan, turns = command.cli_command(session, state, PromptParts("FIXED", "DYNAMIC"), "plan")
            message = turns[-1][1]
            self.assertNotIn("--dangerously-skip-permissions", plan)
            # Without skipped permissions the run's own files stay readable.
            self.assertIn(["--add-dir", f"{directory}/run"], [plan[i:i + 2] for i in range(len(plan))])
            self.assertIn(["--add-dir", str(Path(command.__file__).resolve().parents[3] / "skills")], [plan[i:i + 2] for i in range(len(plan))])
            self.assertNotIn(f"{directory}/run", argv)
            self.assertIn(command.PLAN_REQUEST, message["message"]["content"][0]["text"])
            with self.assertRaisesRegex(runtime.ContractError, "text only"):
                command.cli_command(session, {**state, "imageInputs": [{"path": "a.png"}]}, PromptParts("F", "D"))

    def test_nullable_result_fields_become_optional_for_gemini(self):
        from system.transport import response_schema_document
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.json"
            path.write_text(json.dumps(response_schema_document("/r/result.md")), encoding="utf-8")
            schema, nullable = command.result_schema({"responseSchemaPath": str(path)})
        self.assertEqual(nullable, ("decisionKind",))
        self.assertNotIn("decisionKind", schema["required"])
        self.assertEqual((schema["properties"]["decisionKind"]["enum"], schema["properties"]["decisionKind"]["type"]),
                         (["approval", "clarification"], "string"))
        self.assertNotIn("$schema", schema)
        result = {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": ANSWER,
                  "structured_output": {"content": "hello", "status": "ok"}}
        translator, _ = translate([INIT, {"event": "result", "result": result}], nullable=nullable)
        self.assertEqual(translator.structured, {"decisionKind": None, "content": "hello", "status": "ok"})
        # Recorded from gemini-3.8-flash-low: decisionKind filled on a completed result is dropped.
        filled = {"decisionKind": "clarification", "resultText": "Done.", "status": "completed"}
        raw = json.dumps(filled)
        translator, _ = translate([INIT, {"event": "result", "result": {"conversation_id": CONVERSATION,
                                  "status": "SUCCESS", "response": raw, "structured_output": filled}}], nullable=nullable)
        self.assertIsNone(translator.structured["decisionKind"])
        asked = {**filled, "status": "needs-human-decision"}
        translator, _ = translate([INIT, {"event": "result", "result": {"conversation_id": CONVERSATION,
                                  "status": "SUCCESS", "response": json.dumps(asked), "structured_output": asked}}], nullable=nullable)
        self.assertEqual(translator.structured["decisionKind"], "clarification")

    def test_goal_turns_follow_or_precede_the_request(self):
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text('{"type": "object"}', encoding="utf-8")
            full = runtime.execution_policy.normalize({"schemaVersion": 1, "sandboxPolicy": {"type": "danger-full-access"},
                                                        "approvalPolicy": "never"})
            session = {"agy": "agy", "projectRoot": directory, "executionPolicy": full}
            state = {"responseSchemaPath": str(schema_path), "role": "work", "goalObjective": "notes.txt says done",
                     "statePath": f"{directory}/run/state.json"}
            argv, turns = command.cli_command(session, state, PromptParts("F", "D"))
            self.assertEqual([kind for kind, _ in turns], ["request", "goal"])
            self.assertEqual(turns[1][1]["message"]["content"][0]["text"], "/goal notes.txt says done")
            self.assertNotIn("--disable-slash-commands", argv)
            _, turns = command.cli_command({**session, "goal": {"status": "paused"}}, {**state, "goalObjective": None},
                                           PromptParts("F", "D"))
            self.assertEqual([kind for kind, _ in turns], ["setup", "request"])
            _, turns = command.cli_command(session, state, PromptParts("F", "D"), "plan")
            self.assertEqual([kind for kind, _ in turns], ["request"])

    def test_lean_agent_replaces_the_default_prompt_and_keeps_result_transport(self):
        definition = command.agent_definition()
        header, body = definition.split("---\n")[1:3]
        self.assertIn(f"name: {command.AGENT_NAME}\n", header)
        self.assertIn("mainAgent: true\n", header)
        self.assertIn("inheritCustomizations: true\n", header)  # Project rules still apply, as for Codex and Claude.
        self.assertIn("inheritMcp: false\n", header)
        # agy carries --json-schema results through `finish`; a custom agent must list it.
        self.assertIn("finish", command.AGENT_TOOLS)
        self.assertIn("generate_image", command.AGENT_TOOLS)
        # Agent Factory Skills are offered like plugin Skills on Codex and Claude.
        self.assertIn(f"skills: [{json.dumps(str(command.SKILLS_ROOT))}]\n", header)
        self.assertTrue((command.SKILLS_ROOT / "convention" / "SKILL.md").is_file())
        self.assertTrue(body.startswith("# System Prompt\n"))
        with tempfile.TemporaryDirectory() as home:
            path = command.install_agent(home)
            self.assertEqual(path, Path(home) / ".gemini" / "config" / "agents" / command.AGENT_NAME / "agent.md")
            self.assertEqual(path.read_text(encoding="utf-8"), definition)
            before = path.stat().st_mtime_ns
            command.install_agent(home)  # Unchanged content is not rewritten.
            self.assertEqual(path.stat().st_mtime_ns, before)
            path.write_text("stale", encoding="utf-8")
            command.install_agent(home)
            self.assertEqual(path.read_text(encoding="utf-8"), definition)
            self.assertEqual([item.name for item in path.parent.iterdir()], ["agent.md"])
            # Other plugin copies own their own agent; only agents for removed copies are pruned.
            agents = path.parent.parent
            gone = command.agent_definition(Path(home) / "removed" / "skills").replace(command.AGENT_NAME, "agent-factory-gone")
            live = command.agent_definition(Path(home)).replace(command.AGENT_NAME, "agent-factory-live")
            for name, text in (("agent-factory-gone", gone), ("agent-factory-live", live), ("agent-factory-user", "custom")):
                (agents / name).mkdir()
                (agents / name / "agent.md").write_text(text, encoding="utf-8")
            command.install_agent(home)
            self.assertEqual(sorted(item.name for item in agents.iterdir()),
                             sorted([command.AGENT_NAME, "agent-factory-live", "agent-factory-user"]))
        self.assertRegex(command.AGENT_NAME, r"^agent-factory-[0-9a-f]{12}$")

    def test_plan_work_routes_plan_then_execute(self):
        self.assertEqual(command.planning_phases({"role": "work", "executionOptions": {"taskMode": "plan-work"}}), ["plan", "execute"])
        self.assertEqual(command.planning_phases({"role": "work", "executionOptions": {"taskMode": "plan"}}), ["plan"])
        self.assertEqual(command.planning_phases({"role": "main", "executionOptions": {"taskMode": "plan"}}), [None])


class AntigravityEventTests(unittest.TestCase):
    def test_recorded_stream_yields_tools_usage_and_terminal_result(self):
        translator, events = translate(RECORDED)
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[:2], ["thread.started", "provider.model"])
        self.assertEqual(events[0]["thread_id"], CONVERSATION)
        self.assertNotIn("native.commentary", kinds)  # The final response is the result, not commentary.
        tool = next(event["item"] for event in events if event["type"] == "item.completed" and event["item"].get("tool") == "view_file")
        self.assertEqual((tool["status"], tool["result"]), ("completed", "2 lines, 6 bytes"))
        completed = next(event for event in events if event["type"] == "turn.completed")
        self.assertEqual(completed["usage"], {"input_tokens": 25156, "cached_input_tokens": 8000,
                                              "output_tokens": 190, "reasoning_output_tokens": 108})
        self.assertEqual(json.loads(events[-1]["item"]["text"]), {"content": "hello", "status": "ok"})
        self.assertTrue(translator.finished)

    def test_anthropic_served_input_adds_cache_reads(self):
        # Recorded from claude-sonnet-4-6 over agy: a second turn reports input without its cache reads.
        result = {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": ANSWER,
                  "structured_output": {"content": "hello", "status": "ok"},
                  "usage": usage(input_tokens=559, output_tokens=29, cache_read_tokens=13698, total_tokens=588)}
        claude = {**INIT, "init": {**INIT["init"], "model": "claude-sonnet-4-6"}}
        _, events = translate([claude, {"event": "result", "result": result}])
        completed = next(event for event in events if event["type"] == "turn.completed")
        self.assertEqual((completed["usage"]["input_tokens"], completed["usage"]["cached_input_tokens"]), (14257, 13698))

    def test_commands_edits_and_denials_become_tool_items(self):
        _, events = translate([
            INIT,
            step(5, "DONE", "agent_response", text_delta="Creating the file."),
            step(6, "ACTIVE", "tool", tool_name="write_to_file", tool_info={"parameters": {"TargetFile": "/w/a.txt"}}),
            step(6, "DONE", "tool", tool_name="write_to_file", tool_info={"parameters": {"TargetFile": "/w/a.txt"}}),
            step(7, "ACTIVE", "tool", tool_name="run_command", tool_info={"parameters": {"CommandLine": "touch b.txt"}}),
            step(7, "ERROR", "tool", tool_name="run_command", tool_info={
                "parameters": {"CommandLine": "touch b.txt"},
                "error": {"type": "TOOL_ERROR", "message": "user denied permission to run command"}}),
        ])
        # Text is previewed live and becomes commentary once the next tool shows it was not the result.
        commentary = events.index({"type": "native.commentary", "text": "Creating the file."})
        self.assertEqual(events[commentary - 1]["type"], "native.delta")
        self.assertEqual(events[commentary + 1]["type"], "item.started")
        started = [event["item"] for event in events if event["type"] == "item.started"]
        self.assertEqual(started[0]["changes"], [{"path": "/w/a.txt", "kind": "update"}])
        self.assertEqual(started[1]["command"], "touch b.txt")
        failed = events[-1]["item"]
        self.assertEqual((failed["type"], failed["status"]), ("command_execution", "failed"))
        self.assertIn("denied permission", failed["error"])

    def test_prose_before_the_result_object_stays_commentary(self):
        # Recorded from gpt-oss-120b-medium: prose and the result object in one response.
        answer = '**status:** ok  \n**answer:** blue\n{"answer":"blue","status":"ok","toolAction":"Finishing"}'
        _, events = translate([INIT, step(3, "DONE", "agent_response", text_delta=answer),
                               {"event": "result", "result": {"conversation_id": CONVERSATION, "status": "SUCCESS",
                                "response": answer, "structured_output": {"answer": "blue", "status": "ok"}}}])
        self.assertIn({"type": "native.commentary", "text": "**status:** ok  \n**answer:** blue"}, events)
        self.assertEqual(json.loads(events[-1]["item"]["text"]), {"answer": "blue", "status": "ok"})

    def test_result_text_streams_as_final_preview(self):
        translator = Events()
        translator.translate(INIT)
        with mock.patch.object(translator.deltas, "interval", 0):
            events = translator.translate(step(3, "ACTIVE", "agent_response", text_delta='{"resultText":"Hel'))
            events += translator.translate(step(3, "DONE", "agent_response", text_delta='lo","status":"completed"}'))
        self.assertEqual("".join(event["text"] for event in events if event.get("stream") == "final"), "Hello")

    def test_failures_and_stale_results_are_rejected(self):
        cases = {
            # A resumed turn whose command was denied still reports the prior turn's structured_output.
            "denied permissions: command": {"status": "SUCCESS", "response": "", "structured_output": {"content": "hello"},
                                            "denied_actions": [{"action": "command", "display_name": "RunCommand"}]},
            "No capacity": {"status": "ERROR", "error": "API error (attempt 1): UNAVAILABLE (code 503): No capacity available",
                            "response": ANSWER, "structured_output": {"content": "hello", "status": "ok"}},
            "structured_output": {"status": "SUCCESS", "response": "done"},
        }
        for message, result in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                translate([INIT, {"event": "result", "result": {"conversation_id": CONVERSATION, **result}}])
        with self.assertRaisesRegex(ValueError, "different conversation"):
            translate([INIT], expected_session="0f521ee7-ac66-47ba-8569-8453ae3620d1")
        self.assertIsNone(split_answer('{"content":"old"}', {"content": "hello"}))

    def test_goal_turn_finishes_the_run_and_reports_completion(self):
        # Recorded shape (gemini-3.8-flash-low): the request turn answers, then /goal continues and marks completion.
        first = '{"resultText":"Created goal2.txt containing pending.","status":"success","toolAction":"Finishing task"}'
        final = ('{"resultText":"Updated goal2.txt to done. <!-- GOAL_COMPLETE -->","status":"success",'
                 '"toolAction":"Finishing task"}')
        def result(response, structured, **counts):
            return {"event": "result", "result": {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": response,
                                                  "structured_output": structured, "usage": usage(**counts)}}
        translator, events = translate([
            INIT, step(0, "DONE", "user_input"), step(1, "DONE", "agent_response", text_delta=first),
            result(first, {"resultText": "Created goal2.txt containing pending.", "status": "success"}, input_tokens=10),
            step(5, "DONE", "user_input"), step(9, "DONE", "agent_response", text_delta=final),
            result(final, {"resultText": "Updated goal2.txt to done. <!-- GOAL_COMPLETE -->", "status": "success"},
                   input_tokens=30)], turns=["request", "goal"])
        self.assertIn({"type": "native.commentary", "text": "Created goal2.txt containing pending."}, events)
        self.assertEqual([event["type"] for event in events].count("turn.completed"), 1)
        self.assertEqual(json.loads(events[-1]["item"]["text"]), {"resultText": "Updated goal2.txt to done.", "status": "success"})
        self.assertTrue(translator.goal_complete)
        # A Goal turn that only confirms the condition keeps the request turn's answer.
        confirm = "Verified goal2.txt says done.\n\n<!-- GOAL_COMPLETE -->"
        translator, events = translate([
            INIT, step(1, "DONE", "agent_response", text_delta=first),
            result(first, {"resultText": "Created goal2.txt containing pending.", "status": "success"}),
            step(9, "DONE", "agent_response", text_delta=confirm),
            result(confirm, {"resultText": "Created goal2.txt containing pending.", "status": "success"})],
            turns=["request", "goal"])
        self.assertEqual(json.loads(events[-1]["item"]["text"])["resultText"], "Created goal2.txt containing pending.")
        self.assertIn({"type": "native.commentary", "text": "Verified goal2.txt says done."}, events)
        self.assertTrue(translator.goal_complete)
        # With the lean agent the marker sits inside the result JSON, escaped in the raw response.
        inside = {"resultText": "Verified done.\n\n<!-- GOAL_COMPLETE -->", "status": "success"}
        # agy (Go) escapes `<` and `>` in JSON text, as recorded from gemini-3.8-flash.
        raw = json.dumps({**inside, "toolAction": "Finishing goal"}).replace("<", "\\u003c").replace(">", "\\u003e")
        self.assertNotIn("<!-- GOAL_COMPLETE -->", raw)
        translator, events = translate([INIT, step(1, "DONE", "agent_response", text_delta=first),
                                        result(first, {"resultText": "Created goal2.txt containing pending.", "status": "success"}),
                                        step(9, "DONE", "agent_response", text_delta=raw), result(raw, inside)],
                                       turns=["request", "goal"])
        self.assertTrue(translator.goal_complete)
        self.assertEqual(json.loads(events[-1]["item"]["text"])["resultText"], "Verified done.")
        # A /goal clear setup turn stays invisible and does not finish the run.
        translator, events = translate([
            INIT, step(1, "DONE", "agent_response", text_delta="Cleared. <!-- GOAL_CANCELLED -->"),
            {"event": "result", "result": {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": "Cleared."}},
            *RECORDED[1:]], turns=["setup", "request"])
        self.assertNotIn("Cleared.", json.dumps(events))
        self.assertTrue(translator.finished)
        self.assertFalse(translator.goal_complete)

    def test_plan_phase_result_is_not_published_as_terminal(self):
        _, events = translate(RECORDED, terminal=False)
        self.assertNotIn("agent_message", [event.get("item", {}).get("type") for event in events])


class AntigravityCapabilityTests(unittest.TestCase):
    def test_effort_levels_come_from_the_model_listing(self):
        from adapters.antigravity import capabilities
        listing = ("Fetching available models...\ngemini-3.8-flash-high\tGemini\ngemini-3.8-flash-low\tGemini\n"
                   "gemini-3.1-pro-high\tGemini\ngemini-3.1-pro-low\tGemini\nclaude-sonnet-4-6\tClaude\n")
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(capabilities, "_identity", return_value={"path": "agy"}), \
                mock.patch.object(capabilities.subprocess, "run", return_value=mock.Mock(stdout=listing)) as run:
            levels = capabilities.effort_levels("agy", runtime_home=home)
            self.assertEqual(levels, {"gemini-3.8-flash": ["high", "low"], "gemini-3.1-pro": ["high", "low"]})
            self.assertEqual(capabilities.effort_levels("agy", runtime_home=home), levels)
            self.assertEqual(run.call_count, 1)  # The listing is cached briefly.
        with mock.patch.object(capabilities.subprocess, "run", side_effect=OSError("missing")):
            self.assertEqual(capabilities.effort_levels("agy", runtime_home=None), {})

    def test_help_on_stderr_satisfies_the_probe(self):
        from adapters.antigravity import capabilities
        help_text = "Usage of agy:\n" + "\n".join(f"  {flag}  x" for flag in capabilities.REQUIRED_OPTIONS)
        completed = mock.Mock(returncode=0, stdout="", stderr=help_text)
        with mock.patch.object(capabilities.subprocess, "run", return_value=completed):
            result = capabilities._probe("agy")
        self.assertIsNone(result["diagnostic"])
        self.assertEqual((result["submit"]["goal"], result["submit"]["images"], result["submit"]["plan"]), (True, False, True))
        self.assertEqual(antigravity.session_fields("agy")["provider"], "antigravity")


if __name__ == "__main__":
    unittest.main()
