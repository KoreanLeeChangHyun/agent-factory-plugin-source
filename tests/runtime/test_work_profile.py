import runtime_test_home  # noqa: F401 - imported for its side effect: isolates the runtime home
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from test_agent_exec import load_module


class WorkProfileRecordTests(unittest.TestCase):
    """--work-profile records Main's Expert/Worker choice; it selects nothing."""

    def setUp(self):
        self.runtime = load_module()
        capability = mock.patch.object(self.runtime.native_codex, "inspect_capabilities",
            return_value={"submit": {"goal": True}, "send": {"goal": True}, "diagnostic": None})
        capability.start()
        self.addCleanup(capability.stop)
        self.request = "Implement the named change"
        self.document = {"id": "flow-one", "title": "User changes", "tasks": [
            {"id": "task-one", "title": "Fix scroll", "description": self.request,
             "completionCriteria": "Scroll remains at the bottom",
             "requestHash": hashlib.sha256(self.request.encode()).hexdigest()}]}

    def arguments(self, directory, agent, extra=()):
        tasks = Path(directory) / "tasks.json"
        tasks.write_text(json.dumps(self.document))
        return ["submit", "--project-root", directory, "--agent", agent, "--role", "work", "--codex", "/bin/true",
                "--message", self.request, "--task-list-file", str(tasks), "--task-id", "task-one", *extra]

    def submit(self, directory, agent, extra=()):
        args = self.runtime.parse_args(self.arguments(directory, agent, extra))
        with mock.patch.object(self.runtime, "spawn_worker", return_value=123), \
                mock.patch.object(self.runtime, "emit") as emit:
            self.runtime.submit(args, True)
        return json.loads(Path(emit.call_args.args[0]["statePath"]).read_text())

    def test_submit_records_the_label_in_run_state_dispatch_tuple_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            for profile in ("work", "workLight"):
                with self.subTest(profile=profile):
                    state = self.submit(directory, "agent-" + profile.lower(), ["--work-profile", profile])
                    self.assertEqual(state["workProfile"], profile)
                    self.assertEqual(self.runtime.public_state(state)["workProfile"], profile)
                    self.assertNotIn("workProfile", state["executionOptions"])
            plain = self.submit(directory, "agent-plain")
            labelled = self.submit(directory, "agent-labelled", ["--work-profile", "workLight", "--dispatch-id", "dispatch-labelled"])
            self.assertEqual(labelled["dispatchTuple"]["workProfile"], "workLight")
            # Omitting the flag leaves the record exactly as before.
            for record in (plain, self.runtime.public_state(plain)):
                self.assertNotIn("workProfile", record)
            # A label only: the model, effort, policy and approval settings are those of an unlabelled run.
            for key in ("executionOptions", "executionPolicy", "humanApprovalPolicy", "taskMode", "provider"):
                self.assertEqual(labelled.get(key), plain.get(key), key)

    def test_the_label_is_part_of_the_immutable_dispatch_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            extra = ["--dispatch-id", "dispatch-profile", "--work-profile", "workLight"]
            first = self.submit(directory, "agent-retry", extra)
            self.assertEqual(self.submit(directory, "agent-retry", extra)["runId"], first["runId"])
            with self.assertRaises(self.runtime.ContractError) as raised:
                self.submit(directory, "agent-retry", ["--dispatch-id", "dispatch-profile", "--work-profile", "work"])
            self.assertEqual(raised.exception.code, "dispatch_id_collision")

    def test_invalid_values_and_other_roles_are_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in ("light", "heavy", "worklight", ""):
                with self.subTest(value=value), self.assertRaises(self.runtime.ContractError) as raised:
                    self.runtime.parse_args(self.arguments(directory, "agent-invalid", ["--work-profile", value]))
                self.assertEqual(raised.exception.code, "invalid_arguments")
            for role, extra in (("main", []), ("verification", ["--task-mode", "verification"])):
                args = self.runtime.parse_args(["submit", "--project-root", directory, "--agent", role + "-agent",
                    "--role", role, "--codex", "/bin/true", "--message", self.request, "--work-profile", "work", *extra])
                with self.subTest(role=role), mock.patch.object(self.runtime, "spawn_worker") as spawn, \
                        self.assertRaises(self.runtime.ContractError) as raised:
                    self.runtime.submit(args, True)
                self.assertEqual(raised.exception.code, "work_profile_role_invalid")
                spawn.assert_not_called()

    def test_capabilities_report_the_label_for_both_operations(self):
        native = {"model": True, "reasoning": True, "fast": False, "goal": False}
        with tempfile.TemporaryDirectory() as directory:
            self.runtime.runtime_paths.resolve(Path(directory), create=True)
            with mock.patch.object(self.runtime.native_codex, "inspect_capabilities", return_value={
                    "schemaVersion": "0.1.0", "kind": "execution-capabilities",
                    "submit": dict(native), "send": dict(native)}), mock.patch.object(self.runtime, "emit") as emit:
                self.assertEqual(self.runtime.main(["capabilities", "--project-root", directory, "--codex", "/bin/true"]), 0)
            for operation in ("submit", "send"):
                self.assertIs(emit.call_args.args[0][operation]["workProfile"], True)
                # Advertised together: per-class failure guidance and runs that finish with pending captures.
                self.assertIs(emit.call_args.args[0][operation]["failureClass"], True)
                self.assertIs(emit.call_args.args[0][operation]["pendingLessons"], True)
                # The newest Work response contract (receipt in the structured final output) and the
                # structured revision-limit pause a host renders as a decision view.
                self.assertEqual(emit.call_args.args[0][operation]["responseContract"], 2)
                self.assertIs(emit.call_args.args[0][operation]["revisionLimitPause"], True)
                # Hosts pass the Human's Work isolation toggle only to a runtime that accepts it.
                self.assertIs(emit.call_args.args[0][operation]["workIsolation"], True)

    def test_work_isolation_is_captured_only_from_the_host(self):
        with tempfile.TemporaryDirectory() as directory:
            for value, expected in (("on", True), ("off", False)):
                args = self.runtime.parse_args(["submit", "--project-root", directory, "--agent", "main-agent",
                    "--role", "main", "--codex", "/bin/true", "--message", self.request, "--work-isolation", value])
                self.assertIs(self.runtime.requested_execution(args)["workIsolation"], expected)
            args = self.runtime.parse_args(["submit", "--project-root", directory, "--agent", "main-agent",
                "--role", "main", "--codex", "/bin/true", "--message", self.request])
            self.assertNotIn("workIsolation", self.runtime.requested_execution(args))
            args.work_isolation = "on"
            with mock.patch.dict(os.environ, {self.runtime.execution_policy.PARENT_STATE_ENV: str(Path(directory) / "parent.json")}), \
                    self.assertRaises(self.runtime.ContractError) as raised:
                self.runtime.requested_execution(args)
            self.assertEqual(raised.exception.code, "work_isolation_invalid")

    def test_loop_start_and_exec_agree_on_the_labelled_brief_dispatch(self):
        """The loop's expected tuple and the run exec records must match, or every labelled dispatch fails."""
        from test_agent_loop import load_modules
        agent_exec, agent_loop = load_modules()
        with tempfile.TemporaryDirectory() as directory:
            brief = Path(directory) / "brief.md"
            brief.write_text("Goal: rename one label.\n", encoding="utf-8")

            def bridge(runtime, arguments):
                with mock.patch.object(agent_exec, "spawn_worker", return_value=123), \
                        mock.patch.object(agent_exec, "emit") as emit:
                    agent_exec.main([*arguments, "--project-root", directory])
                response = emit.call_args.args[0]
                if response.get("kind") == "error":
                    raise agent_exec.ContractError(response["error"]["code"], response["error"]["message"])
                return response

            def start(agent, extra):
                args = agent_loop.build_parser().parse_args([
                    "start", "--project-root", directory, "--task-mode", "work", "--work-agent", agent,
                    "--request-file", str(brief), "--codex", "/bin/true", *extra])
                return agent_loop.start_loop(args)

            with mock.patch.object(agent_exec.native_codex, "inspect_capabilities",
                    return_value={"submit": {"goal": True}, "send": {"goal": True}, "diagnostic": None}), \
                    mock.patch.object(agent_loop.AgentRuntime, "_call", bridge):
                for agent, extra, expected in (("light-agent", ["--work-profile", "workLight"], "workLight"),
                                               ("expert-agent", ["--work-profile", "work"], "work"),
                                               ("plain-agent", [], None)):
                    with self.subTest(agent=agent):
                        started = start(agent, extra)
                        self.assertEqual(started["phase"], "work-running")
                        self.assertEqual(started.get("workProfile"), expected)
                        run = agent_exec.safe_read_json(
                            agent_exec.state_file(Path(directory), agent, started["latestWorkRunId"]))
                        self.assertEqual(run.get("workProfile"), expected)
                        self.assertEqual(run["dispatchTuple"].get("workProfile"), expected)


if __name__ == "__main__":
    unittest.main()
