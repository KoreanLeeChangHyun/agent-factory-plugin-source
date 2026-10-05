"""Fault-oriented checks for persisted continuation and independent check owners."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import test_agent_loop as fixtures
from tasks import checks


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AgentLoopContractTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.loop, self.rt = self.f.agent_loop, self.f.agent_exec

    def waiting(self, started, role="work", **fields):
        run = self.f.runtime.runs[(role + "-agent", started["latest" + role.title() + "RunId"])]
        run.update(status="needs-human-decision", **fields)
        Path(run["resultPath"]).write_text("Choose the authorized target.")
        return self.f.reconcile(started)

    def answer_args(self, state, **changes):
        d = state["pendingDecision"]
        response = {"decisionId": d["id"], "questionHash": d["questionHash"],
                    "projectRoot": str(self.f.root), "loopId": state["loopId"],
                    "taskId": d["taskBinding"]["taskId"], "runId": d["runId"], "answer": "Use the existing target."}
        response.update(changes)
        return self.loop.build_parser().parse_args(["answer", "--project-root", str(self.f.root),
            "--work-agent", "work-agent", "--loop-id", state["loopId"], "--actor", "human",
            "--authorization-reference", "test-answer", "--decision-evidence", "Exact answer click",
            "--response-json", json.dumps(response)])

    def test_answer_rejects_wrong_binding_and_adopts_lost_ack_once(self):
        state = self.waiting(self.f.start(["--task-mode", "work"]))
        prior = Path(state["pendingDecision"]["requestPath"]).read_bytes()
        for fields in ({"questionHash": "0"*64}, {"taskId": "other"}, {"runId": "other"}):
            with self.assertRaises(self.rt.ContractError):
                self.loop.answer_decision(self.answer_args(state, **fields))
        self.f.runtime.lose_ack = True
        args = self.answer_args(state)
        with self.assertRaises(self.rt.ContractError):
            self.loop.answer_decision(args)
        resumed = self.f.reconcile(state)
        self.assertEqual(len(self.f.runtime.dispatches), 2)
        self.assertNotEqual(resumed["latestWorkRunId"], state["latestWorkRunId"])
        self.assertEqual(Path(state["pendingDecision"]["requestPath"]).read_bytes(), prior)
        self.loop.answer_decision(args)
        self.assertEqual(len(self.f.runtime.dispatches), 2)
        stored = self.rt.safe_read_json(Path(state["statePath"]))
        self.assertEqual(stored["decisions"][state["pendingDecision"]["id"]]["status"], "resumed")

    def test_verifier_answer_resumes_same_verifier_and_work_binding(self):
        state = self.f.start()
        self.f.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.f.reconcile(state)
        waiting = self.waiting(state, role="verification")
        resumed = self.loop.answer_decision(self.answer_args(waiting))
        self.assertEqual(resumed["latestWorkRunId"], state["latestWorkRunId"])
        self.assertNotEqual(resumed["latestVerificationRunId"], state["latestVerificationRunId"])
        self.assertEqual(self.f.runtime.dispatches[-1]["role"], "verification")

    def test_bypass_corrects_only_explicit_task_approval_once(self):
        for scope in ("scope-expansion", "external-prerequisite", None, "task-execution"):
            with self.subTest(scope=scope):
                state = self.f.start(["--task-mode", "work"])
                path = Path(state["statePath"])
                stored = self.rt.safe_read_json(path)
                stored["execution"].setdefault("agentPermissions", {})["work"] = {
                    "humanApprovalPolicy": "bypass", "policy": stored["execution"]["executionPolicy"]}
                self.rt.atomic_write_json(path, stored)
                result = self.waiting(state, decisionKind="approval", decisionScope=scope)
                if scope == "task-execution":
                    self.assertEqual(result["status"], "active")
                    self.assertEqual(self.waiting(result, decisionKind="approval", decisionScope=scope)["status"], "needs-human-decision")
                else:
                    self.assertEqual(result["status"], "needs-human-decision")
                self.f.close(result)

    def test_steering_is_queued_until_safe_boundary_and_deduplicated(self):
        state = self.f.start(["--task-mode", "work"])
        args = self.loop.build_parser().parse_args(["steer", "--project-root", str(self.f.root),
            "--work-agent", "work-agent", "--loop-id", state["loopId"], "--actor", "main",
            "--authorization-reference", "addition-1", "--decision-evidence", "User requested the case",
            "--task-id", "task-one", "--run-id", state["latestWorkRunId"], "--message", "Include the restart case."])
        queued = self.loop.steer_loop(args)
        self.assertEqual(queued["steering"][0]["status"], "queued")
        self.loop.steer_loop(args)
        self.assertEqual(len(self.f.runtime.dispatches), 1)
        self.f.runtime.complete_work("work-agent", state["latestWorkRunId"])
        resumed = self.f.reconcile(state)
        self.assertEqual(len(self.f.runtime.dispatches), 2)
        self.assertEqual(resumed["steering"][0]["status"], "delivered")
        self.assertIn("Include the restart case.", self.f.runtime.dispatches[-1]["request_file"].read_text())

    def test_new_busy_wait_has_no_arbitrary_observation_limit(self):
        state, _ = self.f.code_workspace_start()
        path = Path(state["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.f.checked_code_work(state)
        self.f.target_owner(cwd=path)
        with mock.patch.object(self.loop, "INTEGRATION_WAIT_ATTEMPTS", 1):
            for _ in range(3):
                state = self.f.reconcile(state)
                self.assertEqual(state["status"], "active")
        self.assertEqual(len(self.f.runtime.dispatches), 1)

    def test_missing_linked_guidance_blocks_before_model_dispatch(self):
        from execution import worktrees
        plan = self.f.isolated_repository()
        (self.f.root / "AGENTS.md").write_text("[required](missing.md)")
        worktrees.git(self.f.root, "add", "-f", "AGENTS.md")
        worktrees.git(self.f.root, "commit", "-m", "guidance fixture")
        with self.assertRaisesRegex(self.rt.ContractError, "guidance is missing"):
            self.f.isolated_brief_start(plan)
        self.assertEqual(self.f.runtime.dispatches, [])

    def test_detached_check_persists_output_and_cancels_without_duplicate(self):
        state = self.f.start(["--task-mode", "work"])
        directory = Path(state["statePath"]).parent
        marker = directory / "check-started"
        unit = {"path": str(directory), "checkCommit": "same-input",
                "checks": [[sys.executable, "-c",
                    "from pathlib import Path; import time; Path(" + repr(str(marker)) + ").write_text('started'); print('progress', flush=True); time.sleep(60)"]]}
        first = checks.observe(unit, directory, "long")
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists())
        second = checks.observe(unit, directory, "long")
        self.assertEqual(second["owner"], first["owner"])
        self.assertIn("progress", Path(second["outputPath"]).read_text())
        checks.cancel(second["statePath"])
        while time.monotonic() < deadline:
            try:
                final = checks.observe(unit, directory, "long")
            except self.rt.ContractError as error:
                self.assertEqual(error.code, "lock_busy")
                time.sleep(.02)
                continue
            if final["status"] not in {"starting", "running"}:
                break
            time.sleep(.02)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(checks.observe(unit, directory, "long")["owner"], first["owner"])

    def test_dependency_preparation_binds_lockfile_before_dispatch(self):
        from execution import worktrees
        plan = self.f.isolated_repository()
        (self.f.root / "package.json").write_text('{"name":"fixture","version":"1.0.0"}')
        (self.f.root / "package-lock.json").write_text('{"lockfileVersion":3}')
        worktrees.git(self.f.root, "add", "-f", "package.json", "package-lock.json")
        worktrees.git(self.f.root, "commit", "-m", "dependency fixture")
        plan["repositories"][0]["checks"] = [["npm", "--version"]]
        seen = []
        def observe(unit, directory, stage):
            seen.append((unit["path"], unit["checkCommit"], stage))
            return {"status": "starting" if len(seen) == 1 else "completed", "results": [],
                    "inputs": {"commit": unit["checkCommit"]}, "statePath": str(directory / "preparation.json")}
        with mock.patch.object(checks, "observe", side_effect=observe):
            preparing = self.f.isolated_brief_start(plan)
            self.assertEqual(preparing["phase"], "preparing")
            self.assertEqual(len(self.f.runtime.dispatches), 0)
            accepted = self.f.reconcile(preparing)
        self.assertEqual(accepted["preflight"]["status"], "completed")
        self.assertEqual(len(self.f.runtime.dispatches), 1)
        self.assertEqual(seen[0], seen[1])
        self.assertNotEqual(seen[0][0], str(self.f.root))

    def test_failed_integration_check_returns_exact_evidence_to_same_work(self):
        state, _ = self.f.code_workspace_start()
        path = Path(state["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.f.checked_code_work(state)
        state_path = Path(state["statePath"])
        stored = self.rt.safe_read_json(state_path)
        stored["taskWorkspaces"]["task-one"]["repositories"][0]["checks"] = [[sys.executable, "-c", "raise SystemExit(7)"]]
        self.rt.atomic_write_json(state_path, stored)
        revised = self.f.reconcile(state)
        self.assertNotEqual(revised["latestWorkRunId"], state["latestWorkRunId"])
        self.assertEqual(self.f.runtime.dispatches[-1]["agent_id"], "work-agent")
        self.assertIn('"exitCode": 7', self.f.runtime.dispatches[-1]["request_file"].read_text())
        self.assertEqual((self.f.root / "file.txt").read_text(), "base\n")
