"""Claude Goal: /goal setup before the request, recorded outcome and runtime controls."""
import runtime_test_home  # Isolate all runtime subprocesses from the real home.

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock
from contextlib import redirect_stdout

from native_fixtures import runtime
from adapters import claude
from adapters.claude import control
from adapters.contracts import GoalServices
from execution.prompts import PromptParts


class ClaudeGoalSetupTests(unittest.TestCase):
    def test_objective_follows_goal_mode_role_and_length(self):
        main = {"role": "main", "goalMode": True}
        self.assertEqual(control.goal_objective(main, {}, b" Ship it "), "Ship it")
        self.assertEqual(control.goal_objective(main, {"executionOptions": {"goalObjective": "Explicit"}}, b"req"), "Explicit")
        self.assertEqual(control.goal_objective({**main, "goal": {"objective": "Stored"}}, {}, b"req"), "Stored")
        self.assertEqual(control.goal_objective(main, {}, ("x" * 4001).encode()), control.BOUNDED_OBJECTIVE)
        self.assertIsNone(control.goal_objective({**main, "goalMode": False}, {}, b"req"))
        self.assertIsNone(control.goal_objective({**main, "role": "verification"}, {}, b"req"))
        self.assertIsNone(control.goal_objective(main, {"executionOptions": {"taskMode": "plan"}}, b"req"))
        self.assertEqual(control.goal_objective({"role": "main", "goal": {"objective": "Again"}}, {"goalAction": "reopen"}, b"x"), "Again")

    def test_commands_set_the_goal_or_clear_a_stale_one(self):
        self.assertEqual(control.goal_commands({}, {"goalObjective": "Done"}), ["/goal Done"])
        self.assertEqual(control.goal_commands({"goal": {"status": "paused"}}, {}), ["/goal clear"])
        self.assertEqual(control.goal_commands({"goal": {"status": "complete"}}, {}), [])
        self.assertEqual(control.goal_commands({}, {}), [])
        self.assertEqual(claude.command.setup_messages({}, {"goalObjective": "Done"}, "plan"), [])
        message = claude.command.setup_messages({}, {"goalObjective": "Done"})[0]
        self.assertEqual(message["message"], {"role": "user", "content": "/goal Done"})
        self.assertEqual(str(uuid.UUID(message["uuid"])), message["uuid"])


class ClaudeGoalTransportTests(unittest.TestCase):
    def run_transport(self, root, stream, objective="File has 3", goal=None):
        prepared = runtime.create_run(project_root=root, agent_id="main-goal", actor="human", request=b"make it 3",
                                      session={"role": "main", "maxAttempts": 1})
        state = runtime.safe_read_json(Path(prepared["statePath"]))
        state = {**state, "runtimeBinding": {}, "role": "main", "goalObjective": objective}
        session = {"role": "main", "goalMode": objective is not None, "projectRoot": str(root), "goal": goal}
        session_path = runtime.session_file(root, "main-goal")
        runtime.atomic_write_json(session_path, {"agentId": "main-goal", "goal": goal})
        request = {"type": "user", "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": "make it 3"}}
        written = []
        process = mock.Mock()
        process.stdin.write.side_effect = lambda line: written.append(json.loads(line))
        process.wait.return_value = 0
        process.poll.return_value = 0

        def lines():
            # Evaluated on first read, after the transport wrote its messages.
            setup = written[0]["uuid"] if len(written) > 1 else None
            yield from (json.dumps(event) for event in stream(setup, request["uuid"]))

        def popen(*args, **kwargs):
            process.stdout = lines()
            return process

        output = io.StringIO()
        with mock.patch.object(claude.transport, "safe_read_json", side_effect=[state, session]), \
                mock.patch.object(claude.transport.runtime_paths, "bind"), \
                mock.patch.object(claude.transport, "cli_command", return_value=(["claude"], request)), \
                mock.patch.object(claude.transport.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(claude.transport.sys, "argv", ["transport", "state", "session"]), \
                mock.patch.object(claude.transport.sys, "stdin", io.StringIO(PromptParts("fixed", "make it 3").encode())), \
                redirect_stdout(output):
            code = claude.transport.main()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, written, events, runtime.safe_read_json(session_path)

    @staticmethod
    def said(session_id, text):
        return {"type": "assistant", "session_id": session_id, "message": {"content": [{"type": "text", "text": text}]}}

    def test_goal_is_set_before_the_request_and_recorded_complete(self):
        session_id = str(uuid.uuid4())

        def stream(setup, request):
            replay = lambda identity: {"type": "user", "uuid": identity, "session_id": session_id,
                                       "message": {"role": "user", "content": "replay"}}
            return [
                {"type": "system", "subtype": "init", "session_id": session_id},
                self.said(session_id, "Goal set: File has 3"),
                replay(setup),
                self.said(session_id, "Working toward the goal"),
                # A goal-only turn may end before the queued request; it must not finish the run.
                {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
                 "structured_output": {"resultText": "goal only"}},
                replay(request),
                {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
                 "duration_ms": 4200, "structured_output": {"status": "completed", "resultText": "3 written"},
                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "output_tokens": 7}},
            ]

        with tempfile.TemporaryDirectory() as directory:
            code, written, events, session = self.run_transport(Path(directory), stream)
        self.assertEqual(code, 0)
        self.assertEqual([line["message"]["content"] for line in written], ["/goal File has 3", "make it 3"])
        goals = [event["goal"] for event in events if event["type"] == "goal.updated"]
        self.assertEqual([goal["status"] for goal in goals], ["active", "complete"])
        self.assertEqual((goals[-1]["tokensUsed"], goals[-1]["timeUsedSeconds"], goals[-1]["threadId"]), (22, 4, session_id))
        kinds = [event["type"] for event in events]
        self.assertLess(kinds.index("goal.updated"), kinds.index("native.commentary"))
        final = [event for event in events if event.get("item", {}).get("type") == "agent_message"]
        self.assertEqual([json.loads(event["item"]["text"])["resultText"] for event in final], ["3 written"])
        self.assertEqual(session["goal"]["status"], "complete")

    def test_rejected_goal_is_reported_and_not_recorded(self):
        session_id = str(uuid.uuid4())

        def stream(setup, request):
            return [
                {"type": "system", "subtype": "init", "session_id": session_id},
                self.said(session_id, "Goal condition is limited to 4000 characters"),
                {"type": "user", "uuid": setup, "session_id": session_id, "message": {"role": "user", "content": "/goal"}},
                {"type": "user", "uuid": request, "session_id": session_id, "message": {"role": "user", "content": "x"}},
                {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
                 "structured_output": {"status": "completed", "resultText": "done"}},
            ]

        with tempfile.TemporaryDirectory() as directory:
            code, _, events, session = self.run_transport(Path(directory), stream)
        self.assertEqual(code, 0)
        self.assertIn("Claude did not set the Goal", next(event["message"] for event in events if event["type"] == "goal.error"))
        self.assertEqual([event["goal"] for event in events if event["type"] == "goal.updated"], [None])
        self.assertIsNone(session["goal"])
        self.assertIn("did not set", session["goalError"])

    def test_stale_goal_is_cleared_when_goal_mode_is_off(self):
        session_id = str(uuid.uuid4())

        def stream(setup, request):
            return [
                {"type": "system", "subtype": "init", "session_id": session_id},
                self.said(session_id, "Goal cleared: old"),
                {"type": "user", "uuid": setup, "session_id": session_id, "message": {"role": "user", "content": "/goal clear"}},
                # The local command ends without a model turn; the request's turn initializes again.
                {"type": "system", "subtype": "init", "session_id": session_id},
                {"type": "user", "uuid": request, "session_id": session_id, "message": {"role": "user", "content": "x"}},
                {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
                 "structured_output": {"status": "completed", "resultText": "done"}},
            ]

        stale = control.goal_record("old-session", "old", "paused")
        with tempfile.TemporaryDirectory() as directory:
            code, written, events, session = self.run_transport(Path(directory), stream, objective=None, goal=stale)
        self.assertEqual(code, 0)
        self.assertEqual(written[0]["message"]["content"], "/goal clear")
        self.assertEqual([event["goal"] for event in events if event["type"] == "goal.updated"], [None])
        self.assertIsNone(session["goal"])


class ClaudeGoalControlTests(unittest.TestCase):
    def services(self, root, active=(), stop=None):
        emitted = []
        submitted = []
        services = GoalServices(emitted.append, runtime.agent_directory, runtime.file_lock,
                                lambda _root, _agent: list(active), runtime.update_json, runtime.parse_args,
                                lambda args, new: submitted.append(args) or 0, frozenset({"running"}), "1", stop)
        return services, emitted, submitted

    def test_pause_and_clear_stop_the_active_run_and_update_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime.create_run(project_root=root, agent_id="main-c", actor="human", request=b"x",
                               session={"role": "main", "maxAttempts": 1})
            goal = control.goal_record("s", "Ship", "active")
            path = runtime.session_file(root, "main-c")
            for action, status, mode in (("pause", "paused", True), ("clear", None, False)):
                runtime.atomic_write_json(path, {"agentId": "main-c", "role": "main", "goal": goal, "goalMode": True})
                stopped = []
                services, emitted, _ = self.services(root, [{"runId": "run-1", "status": "running"}],
                                                     lambda agent, run: stopped.append((agent, run)))
                session = runtime.safe_read_json(path)
                args = SimpleNamespace(agent="main-c", action=action)
                self.assertEqual(control.goal_command(services, args, root, session), 0)
                saved = runtime.safe_read_json(path)
                self.assertEqual((saved["goal"] or {}).get("status"), status)
                self.assertEqual(saved["goalMode"], mode)
                self.assertEqual(stopped, [("main-c", "run-1")])
                self.assertEqual((emitted[-1]["kind"], emitted[-1]["stoppedRunId"]), ("goal", "run-1"))

    def test_get_reports_and_reopen_resubmits_the_stored_objective(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            goal = control.goal_record("s", "Ship", "paused")
            session = {"agentId": "main-c", "role": "main", "goal": goal, "sessionId": "s"}
            services, emitted, submitted = self.services(root)
            self.assertEqual(control.goal_command(services, SimpleNamespace(agent="main-c", action="get"), root, session), 0)
            self.assertEqual(emitted[-1]["goal"], goal)
            self.assertEqual(control.goal_command(services, SimpleNamespace(agent="main-c", action="reopen"), root, session), 0)
            self.assertEqual((submitted[0].goal_objective, submitted[0].goal_mode, submitted[0].goal_action), ("Ship", True, "reopen"))
            with self.assertRaisesRegex(runtime.ContractError, "Goal was cleared"):
                control.goal_command(services, SimpleNamespace(agent="main-c", action="reopen"), root, {**session, "goal": None})


if __name__ == "__main__":
    unittest.main()
