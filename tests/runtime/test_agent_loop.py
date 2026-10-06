from __future__ import annotations

import runtime_test_home  # Isolate all runtime subprocesses from the real home.

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
EXEC_SCRIPT = ROOT / "scripts" / "exec.py"
LOOP_SCRIPT = ROOT / "scripts" / "loop.py"


def load_modules():
    exec_spec = importlib.util.spec_from_file_location("exec", EXEC_SCRIPT)
    if exec_spec is None or exec_spec.loader is None:
        raise RuntimeError("cannot load exec runtime")
    agent_exec = importlib.util.module_from_spec(exec_spec)
    exec_spec.loader.exec_module(agent_exec)
    sys.modules["exec"] = agent_exec
    loop_spec = importlib.util.spec_from_file_location("loop", LOOP_SCRIPT)
    if loop_spec is None or loop_spec.loader is None:
        raise RuntimeError("cannot load loop runtime")
    agent_loop = importlib.util.module_from_spec(loop_spec)
    loop_spec.loader.exec_module(agent_loop)
    return agent_exec, agent_loop


class FakeRuntime:
    def __init__(self, root: Path, agent_exec) -> None:
        self.root = root
        self.agent_exec = agent_exec
        self.runs: dict[tuple[str, str], dict] = {}
        self.dispatches: list[dict] = []
        self.next_run = 1
        self.fail_before_call = False
        self.lose_ack = False
        self.stale_checks: list[str] = []
        self.stale_check_error: Exception | None = None

    def dispatch(self, **values):
        if self.fail_before_call:
            raise self.agent_exec.ContractError("child_runtime_failure", "crash before call")
        run_id = f"run-{self.next_run}"
        self.next_run += 1
        request = Path(values["request_file"]).read_bytes()
        request_hash = hashlib.sha256(request).hexdigest()
        binding_hash = (
            hashlib.sha256(Path(values["capability_binding_file"]).read_bytes()).hexdigest()
            if values.get("capability_binding_file") else None
        )
        dispatch_tuple = {
            "agentId": values["agent_id"],
            "role": values["role"],
            "actor": "main",
            "requestHash": request_hash,
            "receiptRequestHash": values["request_hash"],
            "verifiedWorkRunId": values["verified_work_run_id"],
            "operation": values["operation"],
            "humanApprovalPolicy": values["human_approval_policy"],
        }
        if values["execution"].get("taskBinding"):
            dispatch_tuple["taskBinding"] = values["execution"]["taskBinding"]
            if "documentContext" in values["execution"]["taskBinding"]:
                dispatch_tuple.setdefault("executionOptions", {})["documentContext"] = values["execution"]["taskBinding"]["documentContext"]
        if "executionPolicy" in values["execution"]:
            dispatch_tuple["executionPolicy"] = values["execution"]["executionPolicy"]
        permission = values["execution"].get("agentPermissions", {}).get(values["role"])
        if permission:
            dispatch_tuple["executionPolicy"] = permission["policy"]
            dispatch_tuple["humanApprovalPolicy"] = permission["humanApprovalPolicy"]
        workspace_file = values["execution"].get("taskWorkspacePath")
        workspace = self.agent_exec.safe_read_json(Path(workspace_file)) if workspace_file else None
        if workspace:
            dispatch_tuple["executionPolicy"] = self.agent_exec.worktrees.relocate_policy(dispatch_tuple["executionPolicy"], self.root, Path(workspace["path"]))
            dispatch_tuple["taskWorkspaceId"] = workspace["id"]
        if values["operation"] == "submit" and values["execution"].get("model"):
            dispatch_tuple.setdefault("executionOptions", {})["model"] = values["execution"]["model"]
        profile = values["execution"].get("agentModels", {}).get(values["role"], {})
        if profile:
            dispatch_tuple.setdefault("executionOptions", {}).update(profile)
        if values["role"] == "work" and values["execution"].get("taskMode"):
            dispatch_tuple.setdefault("executionOptions", {})["taskMode"] = values["execution"]["taskMode"]
        if values["role"] == "work":
            from tasks.modes import work_goal_options
            dispatch_tuple["executionOptions"] = work_goal_options(
                dispatch_tuple.get("executionOptions", {}), request.decode("utf-8"))
        if binding_hash is not None:
            dispatch_tuple["capabilityBindingHash"] = binding_hash
        work_profile = values["execution"].get("workProfile") if values["role"] == "work" else None
        if work_profile:
            dispatch_tuple["workProfile"] = work_profile
        directory = self.agent_exec.agent_root(self.root) / values["agent_id"] / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        session_id = f"session-{values['agent_id']}"
        run = {
            "runId": run_id,
            "agentId": values["agent_id"],
            "role": values["role"],
            "status": "accepted",
            "requestHash": request_hash,
            "receiptRequestHash": values["request_hash"],
            "verifiedWorkRunId": values["verified_work_run_id"],
            "dispatchId": values["dispatch_id"],
            "dispatchTuple": dispatch_tuple,
            "statePath": str(directory / "state.json"),
            "resultPath": str(directory / "result.md"),
            "receiptPath": str(directory / "receipt.json"),
            "receiptSchemaPath": str(directory / "receipt.schema.json"),
            "sessionId": session_id,
        }
        if work_profile:
            run["workProfile"] = work_profile
        if workspace:
            run.update(workingDirectory=workspace["path"], taskWorkspace=workspace)
        self.agent_exec.atomic_write_json(directory / "state.json", run)
        self.agent_exec.atomic_write_json(directory / "receipt.schema.json", {})
        session = self.agent_exec.session_file(self.root, values["agent_id"])
        session.parent.mkdir(parents=True, exist_ok=True)
        if not session.exists():
            self.agent_exec.atomic_write_json(
                session, {"role": values["role"], "sessionId": session_id}
            )
        if workspace:
            current = self.agent_exec.safe_read_json(session)
            self.agent_exec.atomic_write_json(session, {**current, "projectRoot": str(self.root), "taskWorkspace": workspace,
                "executionPolicy": dispatch_tuple["executionPolicy"]})
        self.runs[(values["agent_id"], run_id)] = run
        self.dispatches.append(values)
        if self.lose_ack:
            self.lose_ack = False
            raise self.agent_exec.ContractError("child_runtime_failure", "ack lost")
        return {"runId": run_id}

    def status(self, agent_id, run_id):
        run = self.runs[(agent_id, run_id)]
        if run["status"] == "needs-human-decision" and not Path(run["resultPath"]).exists():
            Path(run["resultPath"]).write_text("Provide the missing project choice.")
        return run

    def reconcile_stale(self, agent_id):
        self.stale_checks.append(agent_id)
        if self.stale_check_error is not None:
            raise self.stale_check_error
        return []

    def status_dispatch(self, agent_id, dispatch_id):
        matches = [
            run for (managed, _run_id), run in self.runs.items()
            if managed == agent_id and run["dispatchId"] == dispatch_id
        ]
        if not matches:
            raise self.agent_exec.ContractError("dispatch_not_found", "not dispatched")
        return matches[0]

    def complete_work(self, agent_id: str, run_id: str, addressed: list[str] | None = None):
        run = self.runs[(agent_id, run_id)]
        receipt = {
            "schemaVersion": "0.1.0", "kind": "work-receipt", "runId": run_id,
            "requestHash": run["receiptRequestHash"], "outcome": "completed",
            "changedPaths": ["changed.txt"], "addressedFindingIds": addressed or [],
            "tests": {"run": False, "reason": "work-agent-prohibited"},
        }
        Path(run["resultPath"]).write_text("work result\n", encoding="utf-8")
        Path(run["receiptPath"]).write_text(json.dumps(receipt), encoding="utf-8")
        run["status"] = "completed"
        return run

    def complete_verification(self, agent_id: str, run_id: str, decision: str):
        run = self.runs[(agent_id, run_id)]
        findings = [] if decision == "pass" else [{
            "id": "finding-1", "path": "changed.txt", "location": "1",
            "problem": "incorrect", "evidence": "observed mismatch", "correction": "fix it",
        }]
        receipt = {
            "schemaVersion": "0.1.0", "kind": "verification-receipt", "runId": run_id,
            "verifiedWorkRunId": run["verifiedWorkRunId"],
            "verifiedRequestHash": run["receiptRequestHash"],
            "decision": decision, "findings": findings,
        }
        Path(run["resultPath"]).write_text("verification result\n", encoding="utf-8")
        Path(run["receiptPath"]).write_text(json.dumps(receipt), encoding="utf-8")
        run["status"] = "completed"
        return run


class AgentLoopContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_exec, self.agent_loop = load_modules()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request = self.root / "request.md"
        self.request.write_text("bounded work\n", encoding="utf-8")
        self.tasks = self.root / "tasks.json"
        self.tasks.write_text(json.dumps({"id": "flow-one", "title": "Bounded work", "tasks": [
            {"id": "task-one", "title": "Implement work", "description": "bounded work",
             "completionCriteria": "Work passes checks", "requestHash": hashlib.sha256(self.request.read_bytes()).hexdigest()}]}))
        self.runtime = FakeRuntime(self.root, self.agent_exec)
        self.runtime_class = self.agent_loop.AgentRuntime
        self.runtime_patch = mock.patch.object(self.agent_loop, "AgentRuntime", return_value=self.runtime)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def test_allocation_survives_brief_capture_revision_and_resume(self):
        from test_task_binding import TaskBindingTests
        allocation = TaskBindingTests.allocation()
        allocation_path = self.root / "allocation.json"
        allocation_path.write_text(json.dumps(allocation))
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--work-agent", "work-agent", "--verification-agent", "verification-agent",
            "--task-mode", "work-verification", "--allocation-file", str(allocation_path),
            "--receipt-recovery", "manual", "--max-revisions", "1", "--codex", "/bin/true"])
        started = self.agent_loop.start_loop(args)
        binding = self.agent_exec.safe_read_json(Path(started["statePath"]))["execution"]["taskBinding"]
        self.assertEqual(binding["allocation"], allocation)
        allocation_path.write_text("{}")  # Revisions must use the accepted runtime snapshot.
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        verifying = self.reconcile(started)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "fail")
        revised = self.reconcile(verifying)
        self.assertEqual(self.agent_exec.safe_read_json(Path(revised["statePath"]))["execution"]["taskBinding"], binding)
        self.runtime.complete_work("work-agent", revised["latestWorkRunId"], ["finding-1"])
        verifying = self.reconcile(revised)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "fail")
        stopped = self.reconcile(verifying)
        resumed = self.extend_revisions(stopped)
        stored = self.agent_exec.safe_read_json(Path(resumed["statePath"]))
        self.assertEqual(stored["execution"]["taskBinding"], binding)
        for dispatch in self.runtime.dispatches:
            captured = self.agent_exec.safe_read_json(Path(dispatch["execution"]["taskListPath"]))
            self.assertEqual(captured["tasks"][0]["allocation"], allocation)

    def start(self, extra: list[str] | None = None):
        arguments = [
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-list-file", str(self.tasks), "--task-id", "task-one",
            "--work-agent", "work-agent", "--verification-agent", "verification-agent",
            "--codex", "/bin/true",
            # Most tests exercise the explicit recovery contract; automatic recovery is opted into.
            "--receipt-recovery", "manual",
        ]
        arguments.extend(extra or [])
        args = self.agent_loop.build_parser().parse_args(arguments)
        return self.agent_loop.start_loop(args)

    def drive(self, started, on_sleep):
        args = self.agent_loop.build_parser().parse_args(["drive", "--project-root", str(self.root),
            "--work-agent", "work-agent", "--loop-id", started["loopId"]])
        with mock.patch.object(self.agent_loop.time, "sleep", side_effect=on_sleep):
            return self.agent_loop.drive_loop(args)

    def extend_revisions(self, started, actor="human", evidence="Human asked for one more round", additional="1"):
        args = self.agent_loop.build_parser().parse_args([
            "extend-revisions", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", started["loopId"], "--actor", actor,
            "--authorization-reference", "test-request", "--decision-evidence", evidence,
            "--additional", additional,
        ])
        return self.agent_loop.extend_revisions(args)

    def fail_verification_round(self, state):
        """Complete the current Work, then fail its Verification; returns the reconciled state."""
        addressed = self.agent_exec.safe_read_json(Path(state["statePath"]))["pendingFindingIds"]
        self.runtime.complete_work("work-agent", state["latestWorkRunId"], addressed)
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "fail")
        return self.reconcile(state)

    def close(self, started):
        args = self.agent_loop.build_parser().parse_args([
            "close", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", started["loopId"], "--actor", "human",
            "--authorization-reference", "test-request", "--decision-evidence", "Close failed flow",
        ])
        return self.agent_loop.close_loop(args)

    def stop_task(self, started, **values):
        args = self.agent_loop.build_parser().parse_args([
            "stop-task", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", started["loopId"], "--actor", values.get("actor", "human"),
            "--authorization-reference", "test-request", "--decision-evidence", "Stop one task",
            "--workflow-id", values.get("workflow_id", "flow-one"), "--task-id", values.get("task_id", "task-one"),
        ])
        return self.agent_loop.stop_task(args)

    def test_stop_task_cancels_exact_work_or_verification_and_never_dispatches_again(self):
        for role in ("work", "verification"):
            with self.subTest(role=role):
                started = self.start()
                if role == "verification":
                    self.runtime.complete_work("work-agent", started["latestWorkRunId"])
                    started = self.reconcile(started)
                saved = self.agent_exec.safe_read_json(Path(started["statePath"]))
                child = saved["currentChild"]
                def cancel(arguments):
                    stopped = self.agent_exec.safe_read_json(Path(started["statePath"]))  # noqa: B023 - the closure is only called within this iteration
                    self.assertEqual(stopped["status"], "cancelled")
                    self.assertTrue(stopped["stopPending"])
                    self.assertEqual(arguments, ["cancel", "--agent", child["agentId"], "--run-id", child["runId"]])  # noqa: B023 - the closure is only called within this iteration
                    self.runtime.runs[(child["agentId"], child["runId"])]["status"] = "cancelled"  # noqa: B023 - the closure is only called within this iteration
                    return {"kind": "ack"}
                with mock.patch.object(self.runtime, "call", create=True, side_effect=cancel) as command:
                    result = self.stop_task(started)
                    self.assertEqual(result["status"], "cancelled")
                    self.assertFalse(result["stopPending"])
                    before = len(self.runtime.dispatches)
                    self.assertEqual(self.reconcile(started)["status"], "cancelled")
                    self.stop_task(started)
                    self.assertEqual(len(self.runtime.dispatches), before)
                    command.assert_called_once()

    def test_stop_task_rejects_wrong_binding_multiple_tasks_and_uncertain_dispatch_without_mutation(self):
        started = self.start()
        path = Path(started["statePath"])
        original = self.agent_exec.safe_read_json(path)
        for values in ({"task_id": "other"}, {"workflow_id": "other"}, {"actor": "main"}):
            with self.assertRaises(self.agent_exec.ContractError): self.stop_task(started, **values)
            self.assertEqual(self.agent_exec.safe_read_json(path), original)
        for key in ("multi", "pending"):
            state = json.loads(json.dumps(original))
            if key == "multi": state["workflow"]["tasks"].append({"id": "other", "workStatus": "pending"})
            else: state["pendingDispatch"] = {"dispatchId": "uncertain"}
            self.agent_exec.atomic_write_json(path, state)
            with self.assertRaises(self.agent_exec.ContractError): self.stop_task(started)
            self.assertEqual(self.agent_exec.safe_read_json(path), state)

    def test_stop_task_failure_keeps_engine_stopped_and_retry_cancels_only_original_child(self):
        started = self.start()
        with mock.patch.object(self.runtime, "call", create=True, side_effect=self.agent_exec.ContractError("cancel_failed", "fixture failure")):
            with self.assertRaises(self.agent_exec.ContractError): self.stop_task(started)
        result = self.reconcile(started)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["stopPending"])
        with mock.patch.object(self.runtime, "call", create=True, return_value={"kind": "ack"}) as cancel:
            self.assertFalse(self.stop_task(started)["stopPending"])
            cancel.assert_called_once()

    def test_document_requirements_survive_durable_dispatch_reconciliation(self):
        document = json.loads(self.tasks.read_text())
        requirements = {"query": "needle", "scope": "plugin", "allowPartial": True}
        document["tasks"][0]["documentContext"] = requirements
        self.tasks.write_text(json.dumps(document))
        started = self.start(["--task-mode", "work"])
        self.assertEqual(started["status"], "active")
        state = json.loads(Path(started["statePath"]).read_text())
        self.assertEqual(state["execution"]["taskBinding"]["documentContext"], requirements)
        run = next(iter(self.runtime.runs.values()))
        self.assertEqual(run["dispatchTuple"]["executionOptions"]["documentContext"], requirements)

    def test_orchestrator_brief_starts_without_a_task_list(self):
        self.request.write_text("Create hello.txt with one line\n\nScope: no commits\nDone: file exists\n", encoding="utf-8")
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-mode", "work", "--work-agent", "work-agent", "--codex", "/bin/true"])
        started = self.agent_loop.start_loop(args)
        state = json.loads(Path(started["statePath"]).read_text())
        self.assertEqual(state["workflow"]["title"], "Create hello.txt with one line")
        self.assertEqual(len(state["workflow"]["tasks"]), 1)
        self.assertEqual(state["execution"]["taskBinding"]["taskId"], state["workflow"]["tasks"][0]["id"])
        self.assertEqual(state["execution"]["taskBinding"]["requestHash"], hashlib.sha256(self.request.read_bytes()).hexdigest())
        self.assertEqual(len(self.runtime.dispatches), 1)
        with self.assertRaises(self.agent_exec.ContractError) as error:
            self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([
                "start", "--project-root", str(self.root), "--request-file", str(self.request), "--task-id", "task-one",
                "--task-mode", "work", "--work-agent", "work-agent", "--codex", "/bin/true"]))
        self.assertEqual(error.exception.code, "task_binding_required")

    def test_contract_detail_public_identity_is_copied_without_dispatch(self):
        started = self.start()
        state = json.loads(Path(started["statePath"]).read_text())
        state["contract"] = {"id": "WC-test", "version": 2}
        before = len(self.runtime.dispatches)
        result = self.agent_loop.public_state(state)
        self.assertEqual(result["contract"], state["contract"])
        self.assertEqual(result["updatedAt"], state.get("updatedAt"))
        result["contract"]["version"] = 9
        self.assertEqual(state["contract"]["version"], 2)
        self.assertEqual(len(self.runtime.dispatches), before)

    def test_progress_failure_does_not_block_dispatch_and_refresh_never_dispatches(self):
        with mock.patch.object(self.agent_loop.loop_progress, "publish", side_effect=OSError("disk full")):
            started = self.start()
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(started["progressProjection"], "missing")
        path = Path(started["statePath"])
        before = path.read_bytes()
        args = self.agent_loop.build_parser().parse_args([
            "refresh-progress", "--project-root", str(self.root),
            "--work-agent", "work-agent", "--loop-id", started["loopId"],
        ])
        result = self.agent_loop.refresh_progress(args)
        self.assertEqual(result["projection"]["status"], "current")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(self.agent_loop.refresh_progress(args), result)
        with mock.patch.object(self.agent_loop, "emit") as output:
            exit_code = self.agent_loop.main([
                "refresh-progress", "--project-root", str(self.root),
                "--work-agent", "work-agent", "--loop-id", started["loopId"],
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output.call_args.args[0]["projection"]["status"], "current")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_progress_history_preserves_revisions_and_detects_stale_view(self):
        started = self.start(["--task-mode", "work"])
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        history = path.parent / "progress-history"
        originals = {p.name: p.read_bytes() for p in history.glob("*.json")}
        self.assertGreaterEqual(len(originals), 2)
        self.assertEqual(self.agent_loop.loop_progress.health(path, state), "current")
        (path.parent / "progress.md").write_text("stale")
        self.assertEqual(self.agent_loop.loop_progress.health(path, state), "stale")
        self.agent_loop.publish_progress(path, state)
        self.assertIn("not requested", (path.parent / "progress.md").read_text())
        self.assertEqual({p.name: p.read_bytes() for p in history.glob("*.json")}, originals)
        state["phase"] = "test-next-transition"
        self.agent_loop.save_loop_state(path, state)
        self.assertEqual(len(list(history.glob("*.json"))), len(originals) + 1)
        for name, content in originals.items():
            self.assertEqual((history / name).read_bytes(), content)

    def test_progress_interrupted_publication_repairs_from_committed_state(self):
        started = self.start()
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        state["phase"] = "next-committed-phase"
        state["contract"] = {"id": "WC-TEST", "version": 2}
        atomic_write = self.agent_exec.atomic_write
        def interrupted(target, content):
            if target.name == "progress-state.json":
                raise OSError("simulated interruption after markdown write")
            atomic_write(target, content)
        with mock.patch.object(self.agent_exec, "atomic_write", side_effect=interrupted):
            self.agent_loop.save_loop_state(path, state)
        committed = json.loads(path.read_text())
        self.assertEqual(committed["phase"], "next-committed-phase")
        self.assertEqual(self.agent_loop.loop_progress.health(path, committed), "stale")
        before = path.read_bytes()
        self.assertEqual(self.agent_loop.publish_progress(path, committed)["status"], "current")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.agent_loop.loop_progress.health(path, committed), "current")
        view = json.loads((path.parent / "progress-state.json").read_text())
        self.assertEqual(view["contract"], {"id": "WC-TEST", "version": 2})
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_progress_human_skip_never_claims_verification_pass(self):
        started = self.start()
        state = json.loads(Path(started["statePath"]).read_text())
        state["humanSkip"] = {"actor": "human", "authorizationReference": "test"}
        state["workflow"]["tasks"][0].update(workStatus="completed", verificationStatus="pending")
        rendered = self.agent_loop.loop_progress.render(self.agent_loop.loop_progress.snapshot(state))
        self.assertIn("| completed | skipped |", rendered)
        self.assertNotIn("| completed | completed |", rendered)

    def test_progress_conflict_is_reported_without_overwriting_history(self):
        started = self.start()
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        history = path.parent / "progress-history" / ("revision-%08d.json" % state["stateRevision"])
        history.write_text("corrupt existing evidence")
        result = self.agent_loop.publish_progress(path, state)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(history.read_text(), "corrupt existing evidence")
        self.assertEqual(self.agent_loop.loop_progress.health(path, state), "stale")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_progress_legacy_refresh_preserves_cancelled_and_unverified_states(self):
        started = self.start()
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        state.pop("stateRevision")
        state.update(status="cancelled", phase="ended")
        state["workflow"]["tasks"][0].update(workStatus="cancelled", verificationStatus="cancelled")
        result = self.agent_loop.publish_progress(path, state)
        self.assertEqual(result["stateRevision"], 0)
        view = json.loads((path.parent / "progress-state.json").read_text())
        self.assertEqual(view["tasks"][0]["verificationStatus"], "cancelled")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_linux_driver_runs_outside_main_containment(self):
        args = self.agent_loop.build_parser().parse_args([
            "drive", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", "loop-test",
        ])
        directory = self.agent_loop.loop_directory(self.root, "work-agent", "loop-test", create=True)
        result = {"loopId": "loop-test", "statePath": str(directory / "state.json")}
        launched = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(self.agent_loop.sys, "platform", "linux"), \
             mock.patch.object(self.agent_exec, "systemd_manager_usable", return_value=True), \
             mock.patch.dict(os.environ, {"AGENT_FACTORY_FIXTURE": 'quoted "value"'}), \
             mock.patch.object(self.agent_exec, "_systemd_command", return_value=launched) as command, \
             mock.patch.object(self.agent_loop.subprocess, "Popen") as popen:
            self.agent_loop.launch_driver(args, result)
        options = command.call_args.args[0]
        self.assertIn("--user", options)
        self.assertIn("--service-type=exec", options)
        self.assertIn("--property=Restart=on-failure", options)
        self.assertIn("--property=KillMode=control-group", options)
        self.assertIn(str(self.root), options)
        popen.assert_not_called()
        # Restart=on-failure rereads EnvironmentFile after the launcher exits: it must outlive this process.
        environment = directory / self.agent_loop.DRIVER_ENVIRONMENT_FILE
        self.assertIn(f"--property=EnvironmentFile={environment}", options)
        self.assertFalse(any("/proc/" in option for option in options))
        self.assertIn(b'AGENT_FACTORY_FIXTURE="quoted \\"value\\""\n', environment.read_bytes())
        if os.name == "posix":
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)

    def test_linux_driver_fails_closed_without_user_service(self):
        args = self.agent_loop.build_parser().parse_args([
            "drive", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", "loop-test",
        ])
        with mock.patch.object(self.agent_loop.sys, "platform", "linux"), \
             mock.patch.object(self.agent_exec, "systemd_manager_usable", return_value=False), \
             mock.patch.object(self.agent_loop.subprocess, "Popen") as popen:
            with self.assertRaises(OSError):
                self.agent_loop.launch_driver(args, {"loopId": "loop-test", "statePath": str(self.root / "state.json")})
        popen.assert_not_called()

    def test_driver_child_uses_announcing_main_run(self):
        original = str(self.root / "original-main-state.json")
        runtime = self.runtime_class(self.root, original)
        response = mock.Mock(returncode=0, stdout='{"status":"completed"}\n')
        key = self.agent_exec.execution_policy.PARENT_STATE_ENV
        with mock.patch.dict(os.environ, {key: str(self.root / "later-main-state.json")}), \
             mock.patch.object(self.agent_loop.subprocess, "run", return_value=response) as command:
            runtime.call(["status", "--agent", "work-agent"])
        self.assertEqual(command.call_args.kwargs["env"][key], original)

    def test_contract_scope_conflict_is_rejected_before_work_dispatch(self):
        document = json.loads(self.tasks.read_text())
        document["contract"] = {
            "id": "C1", "version": 1,
            "progress": {"path": "docs/progress/C1/progress.md", "owner": "main"},
            "fileOperations": [{"taskIds": ["task-one"], "operation": "modify", "path": "docs/progress/C1/progress.md"}],
        }
        document["tasks"][0]["requiredFileOperations"] = [{"operation": "modify", "path": "unlisted.md"}]
        self.tasks.write_text(json.dumps(document))
        with self.assertRaisesRegex(self.agent_exec.ContractError, "outside its contract"):
            self.start()
        self.assertEqual(self.runtime.dispatches, [])

    def test_human_can_close_waiting_loop_without_dispatch(self):
        started = self.start()
        self.runtime.runs[("work-agent", started["latestWorkRunId"])]["status"] = "needs-human-decision"
        waiting = self.reconcile(started)
        self.assertEqual(waiting["status"], "needs-human-decision")
        closed = self.close(waiting)
        self.assertEqual(closed["status"], "cancelled")
        self.assertEqual(closed["latestWorkRunId"], started["latestWorkRunId"])
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_close_failed_preserves_evidence_and_cannot_resume(self):
        started = self.start()
        failed = self.fail_work_receipt(started)
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        state["workflow"]["tasks"].append({"id": "later", "workStatus": "pending", "verificationStatus": "pending"})
        self.agent_exec.atomic_write_json(path, state)
        closed = self.close(started)
        self.assertEqual(closed["status"], "cancelled")
        self.assertEqual(closed["controlPlaneError"], failed["controlPlaneError"])
        self.assertEqual(closed["latestWorkRunId"], failed["latestWorkRunId"])
        self.assertEqual(closed["workflow"]["tasks"][0]["workStatus"], "failed")
        self.assertEqual(closed["workflow"]["tasks"][1]["workStatus"], "cancelled")
        self.assertEqual(closed["terminalReason"]["authorizationReference"], "test-request")
        self.assertEqual(self.close(started), closed)
        self.assertEqual(self.reconcile(started), closed)
        self.assertEqual(self.recover_receipt(started), closed)
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(self.runtime.runs[("work-agent", started["latestWorkRunId"])]["status"], "failed")

    def test_close_rejects_active_and_uncertain_dispatch(self):
        started = self.start()
        with self.assertRaises(self.agent_exec.ContractError):
            self.close(started)
        self.fail_work_receipt(started)
        path = Path(started["statePath"])
        state = json.loads(path.read_text())
        state["pendingDispatch"] = {"dispatchId": "uncertain"}
        self.agent_exec.atomic_write_json(path, state)
        with self.assertRaises(self.agent_exec.ContractError):
            self.close(started)
        self.assertEqual(json.loads(path.read_text())["status"], "runtime-error")

    def test_close_rechecks_child_before_closing(self):
        started = self.start()
        self.fail_work_receipt(started)
        self.runtime.runs[("work-agent", started["latestWorkRunId"])]["status"] = "running"
        with self.assertRaises(self.agent_exec.ContractError):
            self.close(started)

    def reconcile(self, started):
        args = self.agent_loop.build_parser().parse_args([
            "reconcile", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", started["loopId"],
        ])
        deadline = time.monotonic() + 20
        while True:
            result = self.agent_loop.reconcile_loop(args)
            operations = [unit.get(stage + "CheckOperation", {}) for value in result.get("taskWorkspaces", {}).values()
                          for unit in value["repositories"] for stage in ("work", "integration")]
            if result["status"] != "active" or not any(op.get("status") in {"starting", "running"} for op in operations):
                return result
            self.assertLess(time.monotonic(), deadline, "Detached fixture check did not finish")
            threading.Event().wait(0.02)

    def recover_receipt(self, started):
        args = self.agent_loop.build_parser().parse_args([
            "recover-receipt", "--project-root", str(self.root),
            "--work-agent", "work-agent", "--loop-id", started["loopId"],
        ])
        return self.agent_loop.recover_receipt(args)

    def fail_work_receipt(self, started, code="receipt_path_contract_invalid", message=None):
        run = self.runtime.runs[("work-agent", started["latestWorkRunId"])]
        run.update({
            "status": "failed",
            "error": {
                "code": code,
                "message": message or "changedPaths must contain only project-root-relative paths",
            },
        })
        return self.reconcile(started)

    def add_second_task(self):
        request = self.root / "second.md"
        request.write_text("second independent task\n")
        document = json.loads(self.tasks.read_text())
        document["tasks"].append({"id": "task-two", "title": "Second task", "description": "Second request",
            "completionCriteria": "Second checks pass", "requestFile": str(request),
            "requestHash": hashlib.sha256(request.read_bytes()).hexdigest()})
        self.tasks.write_text(json.dumps(document))

    def assign_second_task(self):
        self.add_second_task()
        document = json.loads(self.tasks.read_text())
        document["tasks"][1].update(workAgentId="work-api", verificationAgentId="verify-api")
        self.tasks.write_text(json.dumps(document))

    def test_task_assignees_preserve_loop_owner_and_revision_sessions(self):
        self.assign_second_task()
        started = self.start()
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        verifying = self.reconcile(started)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "pass")
        second = self.reconcile(verifying)
        self.assertEqual(second["workAgentId"], "work-agent")
        self.assertEqual(second["currentChild"]["agentId"], "work-api")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "submit")
        self.runtime.complete_work("work-api", second["latestWorkRunId"])
        verifying = self.reconcile(second)
        self.assertEqual(verifying["currentChild"]["agentId"], "verify-api")
        self.assertEqual(self.runtime.dispatches[-1]["verified_work_run_id"], second["latestWorkRunId"])
        self.runtime.complete_verification("verify-api", verifying["latestVerificationRunId"], "fail")
        revision = self.reconcile(verifying)
        self.assertEqual(revision["currentChild"]["agentId"], "work-api")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.runtime.complete_work("work-api", revision["latestWorkRunId"], addressed=["finding-1"])
        verifying = self.reconcile(revision)
        self.runtime.complete_verification("verify-api", verifying["latestVerificationRunId"], "pass")
        ended = self.reconcile(verifying)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual([task["workAgentId"] for task in ended["workflow"]["tasks"]], ["work-agent", "work-api"])
        count = len(self.runtime.dispatches)
        self.reconcile(ended)
        self.assertEqual(len(self.runtime.dispatches), count)

    def test_invalid_assignments_rejected_before_any_dispatch(self):
        self.add_second_task()
        original = json.loads(self.tasks.read_text())
        for key, value in (("workAgentId", "verification-agent"), ("workAgentId", "../bad"),
                           ("verificationAgentId", "work-agent")):
            document = json.loads(json.dumps(original))
            document["tasks"][1][key] = value
            self.tasks.write_text(json.dumps(document))
            with self.subTest(key=key, value=value), self.assertRaises(self.agent_exec.ContractError):
                self.start()
            self.assertEqual(self.runtime.dispatches, [])

    def review(self, started, decision, actor="human", note=None):
        return self.agent_loop.review_draft(self.agent_loop.build_parser().parse_args([
            "review", "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", started["loopId"],
            "--actor", actor, "--authorization-reference", "test-request", "--decision-evidence", "Human reviewed the draft",
            "--decision", decision, *(["--note", note] if note else [])]))

    def test_scribe_draft_waits_for_the_human_review_decision(self):
        # Human decision 2026-10-06: a Scribe's changes are drafts until the Human accepts, revises or discards them.
        started = self.start(["--task-mode", "work", "--work-profile", "scribe"])
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        completed = self.reconcile(started)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["draftReview"]["status"], "pending")
        self.assertEqual(completed["draftReview"]["paths"], ["changed.txt"])
        self.assertEqual(completed["draftReview"]["workRunId"], started["latestWorkRunId"])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.review(started, "accepted", actor="main")
        self.assertEqual(raised.exception.code, "draft_review_unauthorized")
        reviewed = self.review(started, "changes-requested", note="Shorten the summary")
        self.assertEqual((reviewed["draftReview"]["status"], reviewed["draftReview"]["note"]), ("changes-requested", "Shorten the summary"))
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.review(started, "accepted")
        self.assertEqual(raised.exception.code, "draft_review_unavailable")

    def test_only_scribe_loops_carry_a_draft_review(self):
        started = self.start(["--task-mode", "work", "--work-profile", "workLight"])
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        completed = self.reconcile(started)
        self.assertEqual(completed["status"], "completed")
        self.assertNotIn("draftReview", completed)
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.review(started, "accepted")
        self.assertEqual(raised.exception.code, "draft_review_unavailable")

    def test_assigned_worker_receipt_recovery_uses_its_session(self):
        self.assign_second_task()
        started = self.start(["--task-mode", "work"])
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        second = self.reconcile(started)
        run = self.runtime.runs[("work-api", second["latestWorkRunId"])]
        run.update(status="failed", error={"code": "receipt_path_contract_invalid", "message": "invalid path"})
        failed = self.reconcile(second)
        recovered = self.recover_receipt(failed)
        self.assertEqual(recovered["currentChild"]["agentId"], "work-api")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertEqual(recovered["receiptRecovery"]["failedWorkRunId"], second["latestWorkRunId"])

    def test_assignment_snapshot_tampering_blocks_next_dispatch(self):
        self.assign_second_task()
        started = self.start(["--task-mode", "work"])
        state = json.loads(Path(started["statePath"]).read_text())
        snapshot = Path(state["execution"]["taskListPath"])
        document = json.loads(snapshot.read_text())
        document["tasks"][1]["workAgentId"] = "different-worker"
        snapshot.write_text(json.dumps(document))
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        with self.assertRaises(self.agent_exec.ContractError):
            self.reconcile(started)
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_automatic_hashes_snapshot_all_tasks_without_rewriting_input(self):
        self.add_second_task()
        first_content = '첫 요청\r\n둘째 줄\n마지막 줄\r\n'.encode('utf-8')
        second_content = '다음 요청\r\n원문 유지\r\n'.encode('utf-8')
        self.request.write_bytes(first_content)
        document = json.loads(self.tasks.read_text())
        Path(document['tasks'][1]['requestFile']).write_bytes(second_content)
        for task in document['tasks']:
            del task['requestHash']
        self.tasks.write_text(json.dumps(document))
        original = self.tasks.read_bytes()
        started = self.start(['--task-mode', 'work'])
        for task, content in zip(started['workflow']['tasks'], (first_content, second_content), strict=False):
            snapshot = Path(task['requestPath']).read_bytes()
            self.assertEqual(snapshot, content)
            self.assertEqual(task['requestHash'], hashlib.sha256(snapshot).hexdigest())
        self.assertEqual(self.tasks.read_bytes(), original)
        self.assertEqual(self.request.read_bytes(), first_content)
        first_run = self.runtime.status('work-agent', started['latestWorkRunId'])
        self.assertEqual(first_run['dispatchTuple']['executionOptions'], {
            'taskMode': 'work', 'goalMode': True, 'goalObjective': first_content.decode('utf-8')})
        second = started['workflow']['tasks'][1]
        Path(document['tasks'][1]['requestFile']).write_text('changed after acceptance')
        self.runtime.complete_work('work-agent', started['latestWorkRunId'])
        advanced = self.reconcile(started)
        self.assertEqual(json.loads(Path(advanced['statePath']).read_text())['originalRequestHash'], second['requestHash'])
        self.assertEqual(Path(self.runtime.dispatches[-1]['request_file']).read_bytes(), Path(second['requestPath']).read_bytes())
        second_run = self.runtime.status('work-agent', advanced['latestWorkRunId'])
        self.assertEqual(second_run['dispatchTuple']['executionOptions'], {
            'taskMode': 'work', 'goalMode': True, 'goalObjective': second_content.decode('utf-8')})
        self.assertEqual(Path(second['requestPath']).read_bytes(), second_content)
        self.assertEqual(self.tasks.read_bytes(), original)
        self.assertNotEqual(first_run['runId'], second_run['runId'])
        self.assertEqual([call['operation'] for call in self.runtime.dispatches], ['submit', 'send'])

    def test_normalized_goal_text_is_rejected_even_when_request_hash_matches(self):
        content = '원문 요청\r\n다음 줄\r\n'.encode('utf-8')
        self.request.write_bytes(content)
        original_list = self.tasks.read_bytes()
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start(['--task-mode', 'work'])
        directory = next((self.agent_exec.agent_root(self.root) / 'work-agent' / 'loops').iterdir())
        state = self.agent_exec.safe_read_json(directory / 'state.json')
        pending = state['pendingDispatch']
        run = self.runtime.status_dispatch(pending['agentId'], pending['dispatchId'])
        self.assertEqual(run['dispatchTuple']['requestHash'], hashlib.sha256(content).hexdigest())
        run['dispatchTuple']['executionOptions']['goalObjective'] = content.decode('utf-8').replace('\r\n', '\n')
        with self.assertRaises(self.agent_exec.ContractError) as error:
            self.reconcile({'loopId': state['loopId']})
        self.assertEqual(error.exception.code, 'dispatch_binding_invalid')
        self.assertEqual(len(self.runtime.dispatches), 1, 'A mismatched accepted run must not be redispatched')
        self.assertEqual(self.request.read_bytes(), content)
        self.assertEqual(self.tasks.read_bytes(), original_list)
        retained = self.agent_exec.safe_read_json(directory / 'state.json')
        self.assertEqual(retained['pendingDispatch']['dispatchId'], pending['dispatchId'])

    def test_caller_hashes_do_not_gate_submission(self):
        self.add_second_task()
        document = json.loads(self.tasks.read_text())
        document['tasks'][0]['requestHash'] = 'obsolete'
        document['tasks'][1]['requestHash'] = None
        original = json.dumps(document)
        self.tasks.write_text(original)
        started = self.start(['--task-mode', 'work'])
        state = json.loads(Path(started['statePath']).read_text())
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(self.tasks.read_text(), original)
        for task in state['workflow']['tasks']:
            self.assertEqual(task['requestHash'], hashlib.sha256(Path(task['requestPath']).read_bytes()).hexdigest())
        self.runtime.complete_work('work-agent', started['latestWorkRunId'])
        self.reconcile(started)
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_entire_list_visible_and_work_only_advances_once(self):
        self.add_second_task()
        started = self.start(["--task-mode", "work"])
        self.assertEqual(len(started["workflow"]["tasks"]), 2)
        self.assertEqual(started["workflow"]["tasks"][1]["workStatus"], "pending")
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        second = self.reconcile(started)
        self.assertEqual(second["workflow"]["index"], 1)
        self.assertEqual(second["workflow"]["tasks"][0]["workStatus"], "completed")
        self.assertEqual(second["workflow"]["tasks"][1]["workStatus"], "running")
        self.reconcile(second)
        self.assertEqual(len(self.runtime.dispatches), 2)
        self.runtime.complete_work("work-agent", second["latestWorkRunId"])
        self.assertEqual(self.reconcile(second)["status"], "completed")

    def test_verification_failure_blocks_next_task_until_pass(self):
        self.add_second_task()
        started = self.start()
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        verifying = self.reconcile(started)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "fail")
        revision = self.reconcile(verifying)
        self.assertEqual(revision["workflow"]["index"], 0)
        self.assertEqual(revision["workflow"]["tasks"][1]["workStatus"], "pending")
        self.runtime.complete_work("work-agent", revision["latestWorkRunId"], addressed=["finding-1"])
        verifying = self.reconcile(revision)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "pass")
        second = self.reconcile(verifying)
        self.assertEqual(second["workflow"]["index"], 1)
        self.assertEqual(second["workflow"]["tasks"][0]["verificationStatus"], "completed")
        self.assertEqual([item["role"] for item in self.runtime.dispatches], ["work", "verification", "work", "verification", "work"])

    def six_task_document(self):
        document = json.loads(self.tasks.read_text())
        document["tasks"][0]["requestFile"] = str(self.request)
        for index in range(2, 7):
            request = self.root / f'request-{index}.md'
            request.write_text(f'Bounded task {index}\n')
            document['tasks'].append({'id': f'task-{index}', 'title': f'Task {index}',
                'description': f'Bounded task {index}', 'completionCriteria': f'Task {index} delivered',
                'requestFile': str(request)})
        self.tasks.write_text(json.dumps(document))
        return document

    def test_six_tasks_reuse_session_with_distinct_runs_and_restore_without_duplicate(self):
        document = self.six_task_document()
        contents = []
        for index, task in enumerate(document['tasks'], 1):
            content = f'작업 {index}\r\n원문 유지\n마지막 줄\r\n'.encode('utf-8')
            Path(task['requestFile']).write_bytes(content)
            task.pop('requestHash', None)
            contents.append(content)
        self.tasks.write_text(json.dumps(document))
        original_list = self.tasks.read_bytes()
        snapshot = self.start(['--task-mode', 'work'])
        run_ids = []
        session_ids = set()
        for index in range(6):
            tasks = snapshot['workflow']['tasks']
            self.assertEqual([task['id'] for task in tasks], [task['id'] for task in document['tasks']])
            self.assertEqual([task['workStatus'] for task in tasks],
                ['completed'] * index + ['running'] + ['pending'] * (5 - index))
            run_id = tasks[index]['workRunId']
            run_ids.append(run_id)
            self.assertEqual(tasks[index]['workAgentId'], 'work-agent')
            self.assertEqual(self.runtime.dispatches[-1]['execution']['taskBinding']['taskId'], tasks[index]['id'])
            child = self.runtime.status('work-agent', run_id)
            session_ids.add(child['sessionId'])
            self.assertEqual(child['dispatchTuple']['executionOptions'], {
                'taskMode': 'work', 'goalMode': True,
                'goalObjective': contents[index].decode('utf-8')})
            self.assertEqual(child['dispatchTuple']['requestHash'], hashlib.sha256(contents[index]).hexdigest())
            self.assertEqual(Path(tasks[index]['requestPath']).read_bytes(), contents[index])
            # Every reconcile reloads the persisted snapshot, as a reconnect does.
            restored = self.reconcile(snapshot)
            self.assertEqual(restored['workflow'], snapshot['workflow'])
            self.assertEqual(len(self.runtime.dispatches), index + 1)
            self.runtime.complete_work('work-agent', run_id)
            if index == 2:
                # Lose the fourth task's acknowledgement after acceptance, then
                # recover that exact dispatch with its original Goal text.
                self.runtime.lose_ack = True
                with self.assertRaises(self.agent_exec.ContractError):
                    self.reconcile(snapshot)
                pending = self.agent_exec.safe_read_json(Path(snapshot['statePath']))['pendingDispatch']
                accepted = self.runtime.status_dispatch(pending['agentId'], pending['dispatchId'])
                accepted_tuple = json.loads(json.dumps(accepted['dispatchTuple']))
                snapshot = self.reconcile(snapshot)
                self.assertEqual(snapshot['workflow']['tasks'][3]['workRunId'], accepted['runId'])
                self.assertEqual(accepted['dispatchTuple'], accepted_tuple)
                self.assertIsNone(snapshot['pendingDispatch'])
                self.assertEqual(len(self.runtime.dispatches), 4)
                continue
            snapshot = self.reconcile(snapshot)
        self.assertEqual(len(set(run_ids)), 6)
        self.assertEqual(len(session_ids), 1)
        self.assertEqual(self.tasks.read_bytes(), original_list)
        for task, content in zip(document['tasks'], contents, strict=False):
            self.assertEqual(Path(task['requestFile']).read_bytes(), content)
        self.assertEqual([task['workRunId'] for task in snapshot['workflow']['tasks']], run_ids)
        self.assertEqual(snapshot['status'], 'completed')
        self.assertEqual([item['operation'] for item in self.runtime.dispatches], ['submit'] + ['send'] * 5)
        self.assertEqual(self.reconcile(snapshot)['workflow'], snapshot['workflow'])
        self.assertEqual(len(self.runtime.dispatches), 6)

    def test_lost_ack_for_second_task_recovers_failed_run_without_dispatching_again(self):
        self.six_task_document()
        started = self.start(['--task-mode', 'work'])
        self.runtime.complete_work('work-agent', started['latestWorkRunId'])
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.reconcile(started)
        state = json.loads(Path(started['statePath']).read_text())
        pending = state['pendingDispatch']
        child = self.runtime.status_dispatch(pending['agentId'], pending['dispatchId'])
        child['status'] = 'failed'
        restored = self.reconcile(started)
        self.assertEqual([task['workStatus'] for task in restored['workflow']['tasks']],
                         ['completed', 'failed'] + ['pending'] * 4)
        self.assertEqual(restored['workflow']['tasks'][1]['workRunId'], child['runId'])
        stopped = self.reconcile(restored)
        self.assertEqual(stopped['status'], 'runtime-error')
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_readonly_status_projects_blocked_exact_run_without_completing_waiting_tasks(self):
        self.six_task_document()
        started = self.start(['--task-mode', 'work'])
        child = self.runtime.status('work-agent', started['latestWorkRunId'])
        child['status'] = 'needs-human-decision'
        original = Path(started['statePath']).read_bytes()
        args = self.agent_loop.build_parser().parse_args(['status', '--project-root', str(self.root),
            '--work-agent', 'work-agent', '--loop-id', started['loopId']])
        snapshot = self.agent_loop.status_loop(args)
        self.assertEqual([task['workStatus'] for task in snapshot['workflow']['tasks']], ['blocked'] + ['pending'] * 5)
        self.assertEqual(Path(started['statePath']).read_bytes(), original)
        child['status'] = 'completed'
        # Receipt handling has not happened; status alone must not claim completion.
        self.assertEqual(self.agent_loop.status_loop(args)['workflow']['tasks'][0]['workStatus'], 'running')
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_driver_advances_without_main_or_panel(self):
        self.add_second_task()
        started = self.start(["--task-mode", "work"])
        args = self.agent_loop.build_parser().parse_args(["drive", "--project-root", str(self.root),
            "--work-agent", "work-agent", "--loop-id", started["loopId"]])
        def complete_active(_seconds):
            state = json.loads(Path(started["statePath"]).read_text())
            self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        with mock.patch.object(self.agent_loop.time, "sleep", side_effect=complete_active):
            ended = self.agent_loop.drive_loop(args)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_driver_stops_and_preserves_error_without_retry(self):
        started = self.start()
        args = self.agent_loop.build_parser().parse_args(["drive", "--project-root", str(self.root),
            "--work-agent", "work-agent", "--loop-id", started["loopId"]])
        with mock.patch.object(self.agent_loop, "reconcile_loop", side_effect=self.agent_exec.ContractError("test_failure", "stop here")) as reconcile:
            ended = self.agent_loop.drive_loop(args)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(ended["status"], "runtime-error")
        self.assertEqual(ended["workflow"]["tasks"][0]["workStatus"], "blocked")
        self.assertEqual(ended["controlPlaneError"]["code"], "test_failure")

    def test_all_requests_validated_before_any_dispatch(self):
        self.add_second_task()
        (self.root / "second.md").write_text("")
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        self.assertEqual(self.runtime.dispatches, [])

    def test_work_mode_completes_without_verification_or_human_skip(self):
        started = self.start(["--task-mode", "work"])
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        ended = self.reconcile(started)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(ended["terminalReason"]["code"], "work-completed")
        self.assertIsNone(ended["humanSkip"])
        self.assertIsNone(ended["latestVerificationRunId"])
        self.assertEqual([item["role"] for item in self.runtime.dispatches], ["work"])

    def test_plan_work_needs_no_verification_identity_and_completes(self):
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-list-file", str(self.tasks), "--task-id", "task-one",
            "--work-agent", "work-agent", "--task-mode", "plan-work", "--codex", "/bin/true",
        ])
        started = self.agent_loop.start_loop(args)
        work = self.runtime.runs[("work-agent", started["latestWorkRunId"])]
        self.assertEqual(work["dispatchTuple"]["executionOptions"]["taskMode"], "plan-work")
        self.runtime.complete_work("work-agent", work["runId"])
        ended = self.reconcile(started)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(ended["terminalReason"]["code"], "work-completed")
        self.assertIsNone(ended["humanSkip"])
        self.assertIsNone(ended["latestVerificationRunId"])
        self.assertEqual([item["role"] for item in self.runtime.dispatches], ["work"])

    def test_plan_work_rejects_verification_dispatch_and_skip(self):
        started = self.start(["--task-mode", "plan-work"])
        path = Path(started["statePath"])
        state = self.agent_exec.safe_read_json(path)
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.prepare_dispatch(state, path, role="verification", request_file=self.request)
        self.assertEqual(raised.exception.code, "graph_transition_invalid")
        args = self.agent_loop.build_parser().parse_args([
            "skip", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", started["loopId"], "--actor", "human",
            "--authorization-reference", "explicit-input", "--decision-evidence", "skip",
        ])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.skip_loop(args)
        self.assertEqual(raised.exception.code, "verification_not_requested")

    def test_plan_work_noncompleted_child_never_finishes_or_dispatches_verification(self):
        for status in ("failed", "cancelled", "needs-human-decision"):
            with self.subTest(status=status):
                started = self.start(["--task-mode", "plan-work"])
                self.runtime.runs[("work-agent", started["latestWorkRunId"])]["status"] = status
                ended = self.reconcile(started)
                self.assertEqual(ended["status"], "needs-human-decision" if status == "needs-human-decision" else "runtime-error")
                self.assertIsNone(ended["latestVerificationRunId"])
                self.assertTrue(all(item["role"] == "work" for item in self.runtime.dispatches))

    def test_plan_mode_is_bound_to_work_dispatch_and_not_verification(self):
        started = self.start(["--task-mode", "plan-work-verification"])
        work = self.runtime.runs[("work-agent", started["latestWorkRunId"])]
        self.assertEqual(work["dispatchTuple"]["executionOptions"]["taskMode"], "plan-work-verification")
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        checking = self.reconcile(started)
        verification = self.runtime.runs[("verification-agent", checking["latestVerificationRunId"])]
        self.assertNotIn("taskMode", verification["dispatchTuple"].get("executionOptions", {}))
        self.assertEqual(verification["verifiedWorkRunId"], work["runId"])

    def test_work_profile_label_survives_revision_and_stays_off_verification(self):
        state = self.start(["--work-profile", "workLight"])
        self.assertEqual(state["workProfile"], "workLight")
        stored = self.agent_exec.safe_read_json(Path(state["statePath"]))
        self.assertEqual(stored["execution"]["workProfile"], "workLight")
        # A label only: it adds no model, effort or permission to the dispatch.
        self.assertEqual(stored["execution"]["agentModels"], {"work": {}, "verification": {}})
        self.assertEqual(stored["execution"]["agentPermissions"], {})
        self.assertIsNone(stored["execution"]["model"])
        first = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertEqual(first["workProfile"], "workLight")
        self.assertEqual(first["dispatchTuple"]["workProfile"], "workLight")
        state = self.fail_verification_round(state)
        verification = self.runtime.runs[("verification-agent", state["latestVerificationRunId"])]
        self.assertNotIn("workProfile", verification)
        self.assertNotIn("workProfile", verification["dispatchTuple"])
        revision = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertNotEqual(revision["runId"], first["runId"])
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertEqual(revision["workProfile"], "workLight")
        self.assertEqual(revision["dispatchTuple"]["workProfile"], "workLight")
        status = self.agent_loop.status_loop(self.agent_loop.build_parser().parse_args([
            "status", "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", state["loopId"]]))
        self.assertEqual(status["workProfile"], "workLight")

    def test_work_profile_label_survives_receipt_recovery(self):
        state = self.start(["--work-profile", "work"])
        self.assertEqual(state["workProfile"], "work")
        state = self.recover_receipt(self.fail_work_receipt(state))
        recovery = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertEqual(state["receiptRecovery"]["recoveryWorkRunId"], recovery["runId"])
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertEqual(recovery["workProfile"], "work")
        self.assertEqual(recovery["dispatchTuple"]["workProfile"], "work")
        self.assertEqual(state["workProfile"], "work")

    def test_omitted_work_profile_keeps_loop_and_run_records_unlabelled(self):
        state = self.start()
        self.assertNotIn("workProfile", state)
        self.assertNotIn("workProfile", self.agent_exec.safe_read_json(Path(state["statePath"]))["execution"])
        run = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertNotIn("workProfile", run)
        self.assertNotIn("workProfile", run["dispatchTuple"])

    def test_invalid_work_profile_is_rejected_before_any_dispatch(self):
        for value in ("light", "heavy", "worklight", ""):
            with self.subTest(value=value), self.assertRaises(self.agent_exec.ContractError) as raised:
                self.start(["--work-profile", value])
            self.assertEqual(raised.exception.code, "invalid_arguments")
        # A caller that bypasses argparse meets the same rule.
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-list-file", str(self.tasks), "--task-id", "task-one",
            "--work-agent", "work-agent", "--verification-agent", "verification-agent", "--codex", "/bin/true"])
        args.work_profile = "light"
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.start_loop(args)
        self.assertEqual(raised.exception.code, "work_profile_invalid")
        self.assertEqual(self.runtime.dispatches, [])
        self.assertEqual(self.runtime.runs, {})

    def test_runtime_passes_work_profile_to_work_dispatch_only(self):
        runtime = self.runtime_class(self.root)
        execution = {"codex": "/bin/true", "workProfile": "workLight"}
        with mock.patch.object(runtime, "call", return_value={"runId": "run-1"}) as call:
            for role in ("work", "verification"):
                runtime.dispatch(
                    operation="send", agent_id=role + "-agent", role=role, request_file=self.request,
                    request_hash="0" * 64, dispatch_id="dispatch-profile",
                    verified_work_run_id=None if role == "work" else "run-1",
                    execution=execution, capability_binding_file=None, human_approval_policy="required")
            runtime.dispatch(
                operation="send", agent_id="work-agent", role="work", request_file=self.request,
                request_hash="0" * 64, dispatch_id="dispatch-plain", verified_work_run_id=None,
                execution={"codex": "/bin/true"}, capability_binding_file=None, human_approval_policy="required")
        labelled, verification, plain = (item.args[0] for item in call.call_args_list)
        self.assertEqual(labelled[labelled.index("--work-profile") + 1], "workLight")
        self.assertNotIn("--work-profile", verification)
        self.assertNotIn("--work-profile", plain)
        # The label never becomes a model or reasoning option.
        self.assertNotIn("--model", labelled)
        self.assertNotIn("--reasoning-effort", labelled)

    def test_plan_mode_failure_reuses_both_sessions(self):
        state = self.start(["--task-mode", "plan-work-verification"])
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        first_verification = state["latestVerificationRunId"]
        self.runtime.complete_verification("verification-agent", first_verification, "fail")
        state = self.reconcile(state)
        revision = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertEqual(revision["dispatchTuple"]["executionOptions"]["taskMode"], "plan-work-verification")
        self.runtime.complete_work("work-agent", state["latestWorkRunId"], ["finding-1"])
        state = self.reconcile(state)
        self.assertEqual(self.runtime.dispatches[-1]["agent_id"], "verification-agent")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertNotEqual(state["latestVerificationRunId"], first_verification)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "pass")
        self.assertEqual(self.reconcile(state)["status"], "completed")

    def test_historical_loop_without_mode_retains_verification_route(self):
        started = self.start()
        path = Path(started["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        stored["execution"].pop("taskMode")
        self.agent_exec.atomic_write_json(path, stored)
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        checking = self.reconcile(started)
        self.assertEqual(checking["taskMode"], "work-verification")
        self.assertEqual(self.runtime.dispatches[-1]["role"], "verification")

    def test_start_rejects_unsafe_request_paths_before_dispatch(self) -> None:
        final_link = self.root / "linked.md"
        final_link.symlink_to(self.request)
        parent_link = self.root / "linked-parent"
        parent_link.symlink_to(self.root, target_is_directory=True)
        source = self.root / "source"
        source.mkdir()
        for path in (final_link, parent_link / "request.md",
                     source / ".." / "request.md"):
            with self.subTest(path=path):
                with self.assertRaises(self.agent_exec.ContractError) as raised:
                    self.start(["--request-file", str(path)])
                self.assertEqual(raised.exception.code, "capability_binding_invalid")
                self.assertEqual(self.runtime.dispatches, [])

    def test_start_preserves_valid_absolute_and_relative_request_bytes(self) -> None:
        for path in (self.request, Path("request.md")):
            with self.subTest(path=path), mock.patch("pathlib.Path.cwd", return_value=self.root):
                started = self.start(["--request-file", str(path)])
                state = self.agent_exec.safe_read_json(Path(started["statePath"]))
                self.assertEqual(Path(state["originalRequestPath"]).read_bytes(), b"bounded work\n")
                self.assertEqual(state["originalRequestHash"], hashlib.sha256(b"bounded work\n").hexdigest())
                self.assertEqual(self.runtime.dispatches[-1]["role"], "work")

    def test_legacy_execution_upgrade_binds_current_authority_without_rewriting_child(self) -> None:
        started = self.start()
        path = Path(started["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        child_before = dict(self.runtime.runs[(started["currentChild"]["agentId"], started["currentChild"]["runId"])])
        stored["execution"] = {"codex": "/bin/true", "sandbox": "danger-full-access", "model": None}
        self.agent_exec.atomic_write_json(path, stored)
        self.reconcile(started)
        execution = self.agent_exec.safe_read_json(path)["execution"]
        self.assertEqual(execution["executionPolicy"], runtime_test_home.policy("danger-full-access"))
        self.assertEqual(self.agent_exec.safe_read_json(Path(execution["executionPolicyPath"])), execution["executionPolicy"])
        self.assertEqual(self.runtime.runs[(started["currentChild"]["agentId"], started["currentChild"]["runId"])], child_before)

    def test_legacy_execution_upgrade_fails_without_mutating_state_on_invalid_authority(self) -> None:
        started = self.start()
        path = Path(started["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        stored["execution"] = {"codex": "/bin/true", "sandbox": "danger-full-access", "model": None}
        self.agent_exec.atomic_write_json(path, stored)
        before = path.read_bytes()
        with mock.patch.dict(self.agent_exec.os.environ, {"AGENT_FACTORY_EXECUTION_POLICY": "invalid"}):
            with self.assertRaises(self.agent_exec.ContractError) as raised:
                self.reconcile(started)
        self.assertEqual(raised.exception.code, "execution_policy_invalid")
        self.assertEqual(path.read_bytes(), before)

    def test_role_profiles_bind_through_work_verification_revision_loop(self):
        state = self.start(["--work-model", "worker", "--work-reasoning-effort", "high", "--work-fast",
                            "--verification-model", "reviewer", "--verification-reasoning-effort", "low", "--no-verification-fast"])
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "fail")
        state = self.reconcile(state)
        self.runtime.complete_work("work-agent", state["latestWorkRunId"], ["finding-1"])
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "pass")
        state = self.reconcile(state)
        self.assertEqual(state["status"], "completed")
        for run in self.runtime.runs.values():
            expected = ("worker", "high", True) if run["role"] == "work" else ("reviewer", "low", False)
            options = run["dispatchTuple"]["executionOptions"]
            self.assertEqual((options["model"], options["reasoningEffort"], options["fast"]), expected)

    def test_complete_graph_reuses_work_and_verification_sessions(self) -> None:
        state = self.start()
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        first_verification = state["latestVerificationRunId"]
        self.runtime.complete_verification("verification-agent", first_verification, "fail")
        state = self.reconcile(state)
        revised_work = state["latestWorkRunId"]
        self.assertEqual(self.runtime.dispatches[-1]["agent_id"], "work-agent")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.runtime.complete_work("work-agent", revised_work, ["finding-1"])
        state = self.reconcile(state)
        second_verification = state["latestVerificationRunId"]
        self.assertNotEqual(first_verification, second_verification)
        self.assertEqual(self.runtime.dispatches[-1]["agent_id"], "verification-agent")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.runtime.complete_verification("verification-agent", second_verification, "pass")
        state = self.reconcile(state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["terminalReason"]["code"], "pass")

    def test_loop_preserves_capability_binding_for_child_dispatch(self) -> None:
        binding = self.root / "binding.json"
        binding.write_text(json.dumps({
            "schemaVersion": "0.1.0",
            "bindings": [{
                "capabilityId": "git.cli.inspect",
                "authority": {"kind": "native-executable", "reference": "executable:git"},
                "invocationRoute": "git",
                "exactTarget": str(self.root),
                "allowedEffects": [],
                "allowedScopes": ["repository:read"],
                "approvalReference": None,
            }],
        }), encoding="utf-8")
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-list-file", str(self.tasks), "--task-id", "task-one",
            "--work-agent", "work-agent", "--verification-agent", "verification-agent",
            "--codex", "/bin/true", "--work-capability-binding-file", str(binding),
        ])
        state = self.agent_loop.start_loop(args)
        dispatched = self.runtime.dispatches[-1]["capability_binding_file"]
        self.assertIsNotNone(dispatched)
        self.assertEqual(Path(dispatched).parent.name, state["loopId"])

    def test_loop_start_rejects_symlinked_capability_binding(self) -> None:
        binding = self.root / "binding.json"
        binding.write_text("{}", encoding="utf-8")
        linked = self.root / "binding-link.json"
        linked.symlink_to(binding)
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request),
            "--task-list-file", str(self.tasks), "--task-id", "task-one",
            "--work-agent", "work-agent", "--verification-agent", "verification-agent",
            "--codex", "/bin/true", "--work-capability-binding-file", str(linked),
        ])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.start_loop(args)
        self.assertEqual(raised.exception.code, "capability_binding_invalid")

    def test_human_skip_records_evidence_and_never_dispatches_verification(self) -> None:
        state = self.start()
        args = self.agent_loop.build_parser().parse_args([
            "skip", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", state["loopId"], "--actor", "human",
            "--authorization-reference", "human-message-7", "--decision-evidence", "skip verification",
        ])
        state = self.agent_loop.skip_loop(args)
        self.assertEqual(state["status"], "active")
        self.assertIsNone(state["terminalReason"])
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        self.assertEqual(state["terminalReason"]["code"], "human-skip")
        self.assertEqual(state["humanSkip"]["authorizationReference"], "human-message-7")
        self.assertEqual([call["role"] for call in self.runtime.dispatches], ["work"])

    def test_human_skip_after_revision_starts_no_additional_verification(self) -> None:
        state = self.start()
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        self.runtime.complete_verification(
            "verification-agent", state["latestVerificationRunId"], "fail"
        )
        state = self.reconcile(state)
        args = self.agent_loop.build_parser().parse_args([
            "skip", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", state["loopId"], "--actor", "human",
            "--authorization-reference", "human-message-8",
            "--decision-evidence", "skip additional verification",
        ])
        state = self.agent_loop.skip_loop(args)
        self.assertEqual(state["status"], "active")
        self.assertIsNone(state["terminalReason"])
        self.runtime.complete_work(
            "work-agent", state["latestWorkRunId"], ["finding-1"]
        )
        state = self.reconcile(state)
        self.assertEqual(state["terminalReason"]["code"], "human-skip")
        self.assertEqual(
            [call["role"] for call in self.runtime.dispatches],
            ["work", "verification", "work"],
        )

    def test_non_human_skip_is_rejected(self) -> None:
        state = self.start()
        args = self.agent_loop.build_parser().parse_args([
            "skip", "--project-root", str(self.root), "--work-agent", "work-agent",
            "--loop-id", state["loopId"], "--actor", "main",
            "--authorization-reference", "main-claim", "--decision-evidence", "skip",
        ])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.skip_loop(args)
        self.assertEqual(raised.exception.code, "human_skip_unauthorized")

    def test_skip_missing_decision_evidence_is_rejected(self) -> None:
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.build_parser().parse_args([
                "skip", "--project-root", str(self.root), "--work-agent", "work-agent",
                "--loop-id", "loop-one", "--actor", "human",
                "--authorization-reference", "human-message-7",
            ])
        self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_ack_loss_recovers_same_dispatch_without_duplicate(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        loops = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        state = self.agent_exec.safe_read_json(loops / "state.json")
        dispatch_id = state["pendingDispatch"]["dispatchId"]
        state = self.reconcile({"loopId": state["loopId"]})
        self.reconcile(state)
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(self.runtime.runs[("work-agent", state["latestWorkRunId"])]["dispatchId"], dispatch_id)

    def test_dispatch_binds_required_human_approval_policy(self) -> None:
        state = self.start()
        dispatched = self.runtime.dispatches[-1]
        run = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        self.assertEqual(dispatched["human_approval_policy"], "required")
        self.assertEqual(run["dispatchTuple"]["humanApprovalPolicy"], "required")

    def test_role_permissions_survive_verification_and_revision(self) -> None:
        state = self.start(["--work-execution-mode", "bypass",
                            "--verification-execution-mode", "workspace-write",
                            "--work-model", "gpt-5.6-sol", "--verification-model", "gpt-5.6-sol"])
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "fail")
        state = self.reconcile(state)
        self.runtime.complete_work("work-agent", state["latestWorkRunId"], ["finding-1"])
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "pass")
        state = self.reconcile(state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.runtime.dispatches), 4)
        for run in self.runtime.runs.values():
            binding = run["dispatchTuple"]
            self.assertEqual(binding["humanApprovalPolicy"], "bypass" if run["role"] == "work" else "required")
            self.assertEqual(binding["executionOptions"]["model"], "gpt-5.6-sol")

    def test_bypass_pending_ack_is_recovered_without_redispatch(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start(["--work-execution-mode", "bypass"])
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stored = self.agent_exec.safe_read_json(directory / "state.json")
        child = next(iter(self.runtime.runs.values()))
        recovered = self.reconcile({"loopId": stored["loopId"]})
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(recovered["currentChild"]["runId"], child["runId"])

    def test_parent_linked_pending_ack_recovers_without_redispatch(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start(["--task-mode", "work", "--work-execution-mode", "bypass",
                        "--work-model", "gpt-5.6-sol"])
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stored = self.agent_exec.safe_read_json(directory / "state.json")
        child = next(iter(self.runtime.runs.values()))
        parent = {"parentAgentId": "main-original", "parentRunId": "run-original"}
        child.update(parent)
        child["dispatchTuple"].update(parent)
        public = self.agent_exec.public_state(child)
        self.assertEqual({key: public[key] for key in parent}, parent)
        self.runtime.runs[("work-agent", child["runId"])] = public
        # Adoption must use persisted provenance, independent of this caller.
        with mock.patch.object(self.agent_exec, "managed_parent_identity", return_value={"agentId": "main-other", "runId": "run-other"}):
            recovered = self.reconcile(stored)
        self.assertEqual(recovered["currentChild"]["runId"], child["runId"])
        self.runtime.complete_work("work-agent", child["runId"])
        ended = self.reconcile(recovered)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_parent_linkage_mismatch_is_rejected(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stored = self.agent_exec.safe_read_json(directory / "state.json")
        child = next(iter(self.runtime.runs.values()))
        child.update(parentAgentId="main-original", parentRunId="run-original")
        for parent in ({"parentAgentId": "main-other", "parentRunId": "run-original"},
                       {"parentAgentId": "main-original"},
                       {"parentAgentId": "main-original", "parentRunId": "../invalid"}):
            with self.subTest(parent=parent):
                child["dispatchTuple"].pop("parentRunId", None)
                child["dispatchTuple"].update(parent)
                with self.assertRaises(self.agent_exec.ContractError) as raised:
                    self.reconcile(stored)
                self.assertEqual(raised.exception.code, "dispatch_binding_invalid")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_legacy_pending_ack_without_human_approval_policy_is_adopted(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stored = self.agent_exec.safe_read_json(directory / "state.json")
        child = next(iter(self.runtime.runs.values()))
        child["dispatchTuple"].pop("humanApprovalPolicy")

        recovered = self.reconcile({"loopId": stored["loopId"]})

        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(recovered["currentChild"]["runId"], child["runId"])

    def test_pending_ack_rejects_bypass_human_approval_policy(self) -> None:
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stored = self.agent_exec.safe_read_json(directory / "state.json")
        child = next(iter(self.runtime.runs.values()))
        child["dispatchTuple"]["humanApprovalPolicy"] = "bypass"

        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.reconcile({"loopId": stored["loopId"]})

        self.assertEqual(raised.exception.code, "dispatch_binding_invalid")
        persisted = self.agent_exec.safe_read_json(directory / "state.json")
        self.assertEqual(persisted["pendingDispatch"]["dispatchId"], child["dispatchId"])

    def legacy_pending_ack_fixture(self):
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        directory = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        path = directory / "state.json"
        stored = self.agent_exec.safe_read_json(path)
        stored["execution"] = {"codex": "/bin/true", "sandbox": "danger-full-access", "model": None}
        # Both sides must predate the Goal contract. Keeping the modern pending
        # marker while removing the child's Goal options is a real mismatch.
        stored["pendingDispatch"].pop("workGoal")
        stored.pop("workflow")
        child = next(iter(self.runtime.runs.values()))
        child["dispatchTuple"].pop("executionPolicy")
        # Historical loop and child fixtures must both predate task-mode options.
        child["dispatchTuple"].pop("executionOptions")
        child["dispatchTuple"].pop("taskBinding")
        self.agent_exec.atomic_write_json(Path(child["statePath"]), child)
        self.agent_exec.atomic_write_json(path, stored)
        return path, stored, child

    def test_legacy_pending_ack_recovers_original_tuple_without_redispatch(self) -> None:
        path, stored, child = self.legacy_pending_ack_fixture()
        original_tuple = dict(child["dispatchTuple"])
        original_child = Path(child["statePath"]).read_bytes()
        dispatch_id = stored["pendingDispatch"]["dispatchId"]
        request = Path(stored["pendingDispatch"]["requestPath"]).read_bytes()
        recovered = self.reconcile({"loopId": stored["loopId"]})
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(child["dispatchTuple"], original_tuple)
        self.assertEqual(child["dispatchId"], dispatch_id)
        self.assertEqual(Path(child["statePath"]).read_bytes(), original_child)
        self.assertEqual(Path(stored["pendingDispatch"]["requestPath"]).read_bytes(), request)
        self.assertNotIn("executionOptions", child["dispatchTuple"])
        self.assertEqual(recovered["currentChild"]["runId"], child["runId"])
        self.assertEqual(recovered["currentChild"]["agentId"], child["agentId"])
        self.assertIsNone(recovered["pendingDispatch"])
        restored = self.reconcile(recovered)
        self.assertEqual(restored["currentChild"], recovered["currentChild"])
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_goal_marked_pending_cannot_accept_a_pre_goal_child(self):
        path, stored, child = self.legacy_pending_ack_fixture()
        stored["pendingDispatch"]["workGoal"] = True
        self.agent_exec.atomic_write_json(path, stored)
        original_child = Path(child["statePath"]).read_bytes()
        with self.assertRaises(self.agent_exec.ContractError) as error:
            self.reconcile({"loopId": stored["loopId"]})
        self.assertEqual(error.exception.code, "dispatch_binding_invalid")
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(Path(child["statePath"]).read_bytes(), original_child)
        self.assertTrue(self.agent_exec.safe_read_json(path)["pendingDispatch"]["workGoal"])

    def test_legacy_pending_ack_still_rejects_tampered_identity_and_options(self):
        path, stored, child = self.legacy_pending_ack_fixture()
        original = json.loads(json.dumps(child))
        for field, value in (("dispatchId", "dispatch-wrong"), ("agentId", "other-worker"),
                             ("requestHash", "0" * 64), ("executionOptions", {"goalMode": True})):
            child.clear()
            child.update(json.loads(json.dumps(original)))
            if field == "dispatchId":
                child[field] = value
            else:
                child["dispatchTuple"][field] = value
            # Model a corrupted response to the exact dispatch lookup, not a new request.
            with self.subTest(field=field), mock.patch.object(self.runtime, "status_dispatch", return_value=child):
                with self.assertRaises(self.agent_exec.ContractError) as error:
                    self.reconcile({"loopId": stored["loopId"]})
                self.assertEqual(error.exception.code, "dispatch_binding_invalid")
                self.assertEqual(len(self.runtime.dispatches), 1)
                retained = self.agent_exec.safe_read_json(path)
                self.assertEqual(retained["pendingDispatch"]["dispatchId"], stored["pendingDispatch"]["dispatchId"])
                self.assertIsNone(retained["currentChild"])

    def test_crash_before_call_reuses_durable_dispatch_id(self) -> None:
        self.runtime.fail_before_call = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start()
        loops = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        persisted = self.agent_exec.safe_read_json(loops / "state.json")
        dispatch_id = persisted["pendingDispatch"]["dispatchId"]
        self.runtime.fail_before_call = False
        state = self.reconcile({"loopId": persisted["loopId"]})
        self.assertEqual(self.runtime.dispatches[0]["dispatch_id"], dispatch_id)
        self.assertIsNone(state["pendingDispatch"])

    def loop_main(self, command, loop_id, *extra):
        with mock.patch.object(self.agent_loop, "emit") as emit:
            self.assertEqual(self.agent_loop.main([
                command, "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", loop_id, *extra]), 0)
        return emit.call_args.args[0]

    def test_start_dispatch_failure_stops_the_loop_instead_of_leaving_it_driverless(self) -> None:
        self.runtime.fail_before_call = True
        with mock.patch.object(self.agent_loop, "launch_driver") as launch, mock.patch.object(self.agent_loop, "emit") as emit:
            self.assertEqual(self.agent_loop.main([
                "start", "--project-root", str(self.root), "--request-file", str(self.request),
                "--task-list-file", str(self.tasks), "--task-id", "task-one", "--work-agent", "work-agent",
                "--verification-agent", "verification-agent", "--codex", "/bin/true"]), 2)
        launch.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["error"]["code"], "child_runtime_failure")
        loops = next((self.agent_exec.agent_root(self.root) / "work-agent" / "loops").iterdir())
        stopped = self.agent_exec.safe_read_json(loops / "state.json")
        self.assertEqual((stopped["status"], stopped["phase"]), ("runtime-error", "control-plane-error"))
        self.assertEqual(stopped["controlPlaneError"]["code"], "child_runtime_failure")
        self.assertEqual(stopped["workflow"]["tasks"][0]["workStatus"], "blocked")
        # The durable intent survives; an explicit reconcile completes it and gives the loop a driver.
        self.assertIsNotNone(stopped["pendingDispatch"])
        self.runtime.fail_before_call = False
        with mock.patch.object(self.agent_loop, "launch_driver") as launch:
            resumed = self.loop_main("reconcile", stopped["loopId"])
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(self.runtime.dispatches[0]["dispatch_id"], stopped["pendingDispatch"]["dispatchId"])
        launch.assert_called_once()

    def test_reactivating_commands_relaunch_a_driver_only_when_none_runs(self) -> None:
        started = self.start()
        path = Path(started["statePath"])
        with mock.patch.object(self.agent_loop, "launch_driver") as launch:
            with self.agent_exec.file_lock(path.parent / ".driver.lock"):
                self.assertEqual(self.loop_main("reconcile", started["loopId"])["status"], "active")
            launch.assert_not_called()
            self.assertEqual(self.loop_main("reconcile", started["loopId"])["status"], "active")
            launch.assert_called_once()
        stopped = self.agent_exec.safe_read_json(path)
        stopped.update(status="runtime-error", phase="control-plane-error",
                       controlPlaneError={"code": "child_runtime_failure", "message": "fixture"})
        self.agent_exec.atomic_write_json(path, stopped)
        with mock.patch.object(self.agent_loop, "launch_driver", side_effect=OSError("unit exists")) as launch:
            skipped = self.loop_main("skip", started["loopId"], "--actor", "human",
                                     "--authorization-reference", "test-request", "--decision-evidence", "Skip Verification")
        launch.assert_called_once()
        # A failed relaunch never stops the loop: an explicit reconcile still advances it.
        self.assertEqual(skipped["status"], "active")
        self.assertEqual(self.agent_exec.safe_read_json(path)["status"], "active")

    def test_driver_removes_its_environment_file_when_the_loop_stops(self) -> None:
        started = self.start()
        path = Path(started["statePath"])
        environment = path.parent / self.agent_loop.DRIVER_ENVIRONMENT_FILE
        environment.write_text("NAME=value\n")
        stopped = self.agent_exec.safe_read_json(path)
        stopped.update(status="runtime-error")
        self.agent_exec.atomic_write_json(path, stopped)
        self.assertEqual(self.loop_main("drive", started["loopId"])["status"], "runtime-error")
        self.assertFalse(environment.exists())

    def test_outside_graph_role_is_rejected(self) -> None:
        state = self.start()
        path = Path(state["statePath"])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.prepare_dispatch(state, path, role="review", request_file=self.request)
        self.assertEqual(raised.exception.code, "graph_role_invalid")

    def test_outside_graph_transition_is_rejected(self) -> None:
        state = self.start()
        path = Path(state["statePath"])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.prepare_dispatch(
                state, path, role="verification", request_file=self.request,
                verified_work_run_id="run-not-latest",
            )
        self.assertEqual(raised.exception.code, "graph_transition_invalid")

    def test_child_failure_is_control_plane_error_not_graph_end(self) -> None:
        state = self.start()
        self.runtime.runs[("work-agent", state["latestWorkRunId"])]["status"] = "failed"
        state = self.reconcile(state)
        self.assertEqual(state["status"], "runtime-error")
        self.assertEqual(state["phase"], "control-plane-error")
        self.assertIsNone(state["terminalReason"])

    def test_receipt_recovery_preserves_failed_run_and_reaches_verification(self) -> None:
        state = self.start()
        failed_run_id = state["latestWorkRunId"]
        failed = dict(self.runtime.runs[("work-agent", failed_run_id)])
        state = self.fail_work_receipt(state)

        state = self.recover_receipt(state)
        recovery_run_id = state["latestWorkRunId"]
        self.assertNotEqual(recovery_run_id, failed_run_id)
        self.assertEqual(self.runtime.runs[("work-agent", failed_run_id)], {
            **failed,
            "status": "failed",
            "error": {
                "code": "receipt_path_contract_invalid",
                "message": "changedPaths must contain only project-root-relative paths",
            },
        })
        recovery = state["receiptRecovery"]
        self.assertEqual(recovery["failedWorkRunId"], failed_run_id)
        self.assertEqual(recovery["recoveryWorkRunId"], recovery_run_id)
        dispatched = self.runtime.dispatches[-1]
        self.assertEqual(dispatched["operation"], "send")
        stored = self.agent_exec.safe_read_json(Path(state["statePath"]))
        self.assertEqual(dispatched["request_hash"], stored["originalRequestHash"])
        self.assertEqual(dispatched["execution"]["executionPolicy"], stored["execution"]["executionPolicy"])
        recovery_request = Path(recovery["requestPath"]).read_text(encoding="utf-8")
        self.assertIn("Do not repeat any already performed tool effect", recovery_request)
        self.assertIn(f"Failed Work run: {failed_run_id}", recovery_request)

        self.runtime.complete_work("work-agent", recovery_run_id)
        state = self.reconcile(state)
        self.assertEqual(state["currentChild"]["role"], "verification")
        verification = self.runtime.runs[("verification-agent", state["latestVerificationRunId"])]
        self.assertEqual(verification["verifiedWorkRunId"], recovery_run_id)
        dispatch_count = len(self.runtime.dispatches)
        repeated = self.recover_receipt(state)
        self.assertEqual(repeated["latestVerificationRunId"], state["latestVerificationRunId"])
        self.assertEqual(len(self.runtime.dispatches), dispatch_count)

    def test_receipt_recovery_preserves_valid_capability_evidence(self) -> None:
        binding = self.root / "binding.json"
        binding.write_text(json.dumps({
            "schemaVersion": "0.1.0",
            "bindings": [{
                "capabilityId": "git.cli.inspect",
                "authority": {"kind": "native-executable", "reference": "executable:git"},
                "invocationRoute": "git", "exactTarget": str(self.root),
                "allowedEffects": [], "allowedScopes": ["repository:read"],
                "approvalReference": None,
            }],
        }), encoding="utf-8")
        state = self.fail_work_receipt(self.start([
            "--work-capability-binding-file", str(binding),
        ]))
        recovered = self.recover_receipt(state)
        stored = self.agent_exec.safe_read_json(Path(recovered["statePath"]))
        self.assertEqual(
            str(self.runtime.dispatches[-1]["capability_binding_file"]),
            stored["capabilityBindings"]["work"]["path"],
        )
        recovery_run = self.runtime.runs[("work-agent", recovered["latestWorkRunId"])]
        request = self.agent_loop.verification_request(
            stored, recovery_run, Path(recovered["statePath"]).parent
        ).read_text(encoding="utf-8")
        self.assertIn(stored["receiptRecovery"]["failedReceiptPath"], request)

    def test_receipt_recovery_preserves_revision_findings(self) -> None:
        state = self.start()
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        state = self.reconcile(state)
        self.runtime.complete_verification(
            "verification-agent", state["latestVerificationRunId"], "fail"
        )
        state = self.reconcile(state)
        state = self.fail_work_receipt(state)

        recovered = self.recover_receipt(state)
        request = Path(recovered["receiptRecovery"]["requestPath"]).read_text(encoding="utf-8")
        self.assertIn('Required addressed finding IDs: ["finding-1"]', request)
        self.runtime.complete_work(
            "work-agent", recovered["latestWorkRunId"], ["finding-1"]
        )
        reconciled = self.reconcile(recovered)
        self.assertEqual(reconciled["currentChild"]["role"], "verification")

    def test_receipt_recovery_reuses_durable_dispatch_after_ack_loss(self) -> None:
        state = self.fail_work_receipt(self.start())
        self.runtime.lose_ack = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.recover_receipt(state)
        dispatch_count = len(self.runtime.dispatches)
        stored = self.agent_exec.safe_read_json(Path(state["statePath"]))
        dispatch_id = stored["pendingDispatch"]["dispatchId"]

        recovered = self.recover_receipt(stored)
        self.assertEqual(len(self.runtime.dispatches), dispatch_count)
        self.assertEqual(
            self.runtime.runs[("work-agent", recovered["latestWorkRunId"])]["dispatchId"],
            dispatch_id,
        )

    def test_legacy_changed_path_error_remains_recoverable_without_capabilities(self) -> None:
        state = self.fail_work_receipt(
            self.start(), "receipt_invalid", "changedPaths must be bounded relative paths"
        )
        failed_run_id = state["latestWorkRunId"]
        recovered = self.recover_receipt(state)
        self.assertEqual(recovered["receiptRecovery"]["failedWorkRunId"], failed_run_id)

    def test_receipt_recovery_fails_closed_for_active_unsafe_or_wrong_session(self) -> None:
        active = self.start()
        with self.assertRaises(self.agent_exec.ContractError) as unavailable:
            self.recover_receipt(active)
        self.assertEqual(unavailable.exception.code, "receipt_recovery_unavailable")

        unsafe = self.fail_work_receipt(active, "sandbox_unavailable")
        with self.assertRaises(self.agent_exec.ContractError) as rejected:
            self.recover_receipt(unsafe)
        self.assertEqual(rejected.exception.code, "receipt_recovery_unsafe")

        receipt_error = {
            "code": "receipt_path_contract_invalid",
            "message": "changedPaths must contain only project-root-relative paths",
        }
        run = self.runtime.runs[("work-agent", unsafe["latestWorkRunId"])]
        run["error"] = receipt_error
        path = Path(unsafe["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        stored["controlPlaneError"] = receipt_error
        self.agent_exec.atomic_write_json(path, stored)
        session_path = self.agent_exec.session_file(self.root, "work-agent")
        session = self.agent_exec.safe_read_json(session_path)
        session["sessionId"] = "different-session"
        self.agent_exec.atomic_write_json(session_path, session)
        with self.assertRaises(self.agent_exec.ContractError) as mismatch:
            self.recover_receipt(stored)
        self.assertEqual(mismatch.exception.code, "receipt_recovery_session_invalid")

    def test_receipt_recovery_rejects_test_and_capability_evidence_failures(self) -> None:
        state = self.fail_work_receipt(
            self.start(), "receipt_tests_invalid", "Work receipt must prove Work ran no tests"
        )
        with self.assertRaises(self.agent_exec.ContractError) as tests_error:
            self.recover_receipt(state)
        self.assertEqual(tests_error.exception.code, "receipt_recovery_unsafe")

        failed_run = self.runtime.runs[("work-agent", state["latestWorkRunId"])]
        capability_error = {
            "code": "receipt_capability_invalid",
            "message": "capability outcome binding is invalid",
        }
        failed_run["error"] = capability_error
        path = Path(state["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        stored["controlPlaneError"] = capability_error
        self.agent_exec.atomic_write_json(path, stored)
        before = dict(failed_run)
        with self.assertRaises(self.agent_exec.ContractError) as capability:
            self.recover_receipt(stored)
        self.assertEqual(capability.exception.code, "receipt_recovery_unsafe")
        self.assertEqual(failed_run, before)

    def test_rejected_legacy_recovery_does_not_publish_policy_or_change_bytes(self) -> None:
        state = self.start()
        path = Path(state["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        self.assertTrue((path.parent / ".loop.lock").is_file())
        policy_path = Path(stored["execution"]["executionPolicyPath"])
        policy_path.unlink()
        stored["execution"] = {
            "codex": "/bin/true", "sandbox": "danger-full-access", "model": None,
        }
        self.agent_exec.atomic_write_json(path, stored)
        directory = path.parent
        before_files = {
            item.relative_to(directory): item.read_bytes()
            for item in directory.rglob("*") if item.is_file()
        }
        before_directories = {
            item.relative_to(directory) for item in directory.rglob("*") if item.is_dir()
        }

        with self.assertRaises(self.agent_exec.ContractError) as rejected:
            self.recover_receipt(stored)
        self.assertEqual(rejected.exception.code, "receipt_recovery_unavailable")
        self.assertEqual(before_files, {
            item.relative_to(directory): item.read_bytes()
            for item in directory.rglob("*") if item.is_file()
        })
        self.assertEqual(before_directories, {
            item.relative_to(directory) for item in directory.rglob("*") if item.is_dir()
        })

    # --- stabilization: automatic receipt recovery, bounded revisions, stale children ---

    def test_missing_receipt_gets_one_automatic_repair_turn_and_completes(self) -> None:
        # Incident: a light Work model finished the task but published no receipt.
        started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
        failed_run_id = started["latestWorkRunId"]
        state = self.fail_work_receipt(started, "receipt_missing", "Agent did not publish its receipt")
        self.assertEqual(state["status"], "active")
        self.assertNotEqual(state["latestWorkRunId"], failed_run_id)
        self.assertEqual(state["receiptRecovery"]["failedWorkRunId"], failed_run_id)
        self.assertIs(state["receiptRecovery"]["automatic"], True)
        self.assertIsNone(state["controlPlaneError"])
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        request = Path(state["receiptRecovery"]["requestPath"]).read_text(encoding="utf-8")
        self.assertIn("Failure: receipt_missing", request)
        self.assertIn("Do not repeat any already performed tool effect", request)
        self.assertEqual(self.runtime.runs[("work-agent", failed_run_id)]["status"], "failed")

        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        ended = self.reconcile(state)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(ended["terminalReason"]["code"], "work-completed")
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_automatic_receipt_recovery_is_bounded_to_one_per_task(self) -> None:
        started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
        recovering = self.fail_work_receipt(started, "receipt_missing", "Agent did not publish its receipt")
        stopped = self.fail_work_receipt(recovering, "receipt_missing", "Agent did not publish its receipt")
        self.assertEqual(stopped["status"], "runtime-error")
        self.assertEqual(stopped["controlPlaneError"]["code"], "receipt_missing")
        self.assertEqual(stopped["failureClass"], "contract")
        self.assertEqual(len(self.runtime.dispatches), 2)
        self.assertEqual(self.reconcile(stopped)["status"], "runtime-error")
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_automatic_receipt_recovery_keeps_unsafe_failures_stopped(self) -> None:
        for code, message in (
            ("sandbox_unavailable", "Codex filesystem sandbox is unavailable"),
            ("receipt_tests_invalid", "Work receipt must record own checks or a reason they were not run"),
            ("receipt_binding_invalid", "work receipt binding is invalid"),
            ("lesson_recording_incomplete", "Runtime error captures remain unsaved"),
        ):
            with self.subTest(code=code):
                self.runtime.dispatches.clear()
                self.request.write_text(f"bounded work for {code}\n", encoding="utf-8")
                document = json.loads(self.tasks.read_text())
                document["tasks"][0]["requestHash"] = hashlib.sha256(self.request.read_bytes()).hexdigest()
                self.tasks.write_text(json.dumps(document))
                started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
                stopped = self.fail_work_receipt(started, code, message)
                self.assertEqual(stopped["status"], "runtime-error")
                self.assertEqual(stopped["controlPlaneError"]["code"], code)
                self.assertIsNone(stopped["receiptRecovery"])
                self.assertEqual(len(self.runtime.dispatches), 1)
                self.close(stopped)
                session = self.agent_exec.session_file(self.root, "work-agent")
                session.unlink()

    def test_loop_without_recovery_setting_keeps_explicit_recovery(self) -> None:
        started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
        path = Path(started["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        del stored["execution"]["receiptRecovery"]  # A loop persisted before the setting existed.
        self.agent_exec.atomic_write_json(path, stored)
        stopped = self.fail_work_receipt(started, "receipt_missing", "Agent did not publish its receipt")
        self.assertEqual(stopped["status"], "runtime-error")
        self.assertEqual(len(self.runtime.dispatches), 1)
        recovered = self.recover_receipt(stopped)
        self.assertEqual(recovered["status"], "active")
        self.assertNotIn("automatic", recovered["receiptRecovery"])

    def test_driver_repairs_missing_receipt_without_main(self) -> None:
        started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
        first_run = started["latestWorkRunId"]

        def advance(_seconds):
            state = json.loads(Path(started["statePath"]).read_text())
            run_id = state["latestWorkRunId"]
            if run_id == first_run:
                self.runtime.runs[("work-agent", run_id)].update(status="failed", error={
                    "code": "receipt_missing", "message": "Agent did not publish its receipt"})
            else:
                self.runtime.complete_work("work-agent", run_id)

        ended = self.drive(started, advance)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(ended["terminalReason"]["code"], "work-completed")
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_revision_limit_stops_for_a_human_and_extension_resumes(self) -> None:
        # Incident: a rejected Work result looped through revisions for 23 minutes.
        state = self.start(["--max-revisions", "1"])
        self.assertEqual(state["maxRevisions"], 1)
        state = self.fail_verification_round(state)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["revisionCount"], 1)
        dispatched = len(self.runtime.dispatches)

        stopped = self.fail_verification_round(state)
        self.assertEqual(stopped["status"], "needs-human-decision")
        self.assertEqual(stopped["phase"], "waiting-human")
        self.assertEqual(stopped["controlPlaneError"]["code"], "revision_limit_reached")
        self.assertIn("finding-1", stopped["controlPlaneError"]["message"])
        self.assertEqual(stopped["failureClass"], "human")
        self.assertEqual(stopped["workflow"]["tasks"][0]["verificationStatus"], "blocked")
        self.assertEqual(len(self.runtime.dispatches), dispatched + 1)  # only the Verification
        self.assertEqual(self.reconcile(stopped)["status"], "needs-human-decision")
        self.assertEqual(len(self.runtime.dispatches), dispatched + 1)

        with self.assertRaises(self.agent_exec.ContractError) as unauthorized:
            self.extend_revisions(stopped, actor="main")
        self.assertEqual(unauthorized.exception.code, "revision_extension_unauthorized")
        with self.assertRaises(self.agent_exec.ContractError) as empty:
            self.extend_revisions(stopped, evidence=" ")
        self.assertEqual(empty.exception.code, "revision_extension_unauthorized")

        resumed = self.extend_revisions(stopped)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["currentChild"]["role"], "work")
        self.assertEqual(resumed["maxRevisions"], 2)
        self.assertEqual(resumed["revisionCount"], 2)
        self.assertIsNone(resumed["controlPlaneError"])
        stored = self.agent_exec.safe_read_json(Path(resumed["statePath"]))
        self.assertEqual(stored["revisionExtensions"][0]["actor"], "human")
        self.assertEqual(stored["revisionExtensions"][0]["decisionEvidence"], "Human asked for one more round")
        with self.assertRaises(self.agent_exec.ContractError) as active:
            self.extend_revisions(resumed)
        self.assertEqual(active.exception.code, "revision_extension_unavailable")

        self.runtime.complete_work("work-agent", resumed["latestWorkRunId"], ["finding-1"])
        verifying = self.reconcile(resumed)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "pass")
        self.assertEqual(self.reconcile(verifying)["status"], "completed")

    def test_revision_limit_stop_is_exposed_as_a_structured_pause(self) -> None:
        state = self.start(["--max-revisions", "1"])
        self.assertIsNone(state["pause"])
        self.assertTrue(state["createdAt"])
        state = self.fail_verification_round(state)
        self.assertIsNone(state["pause"])  # A revision is running: nothing for a Human to decide.
        stopped = self.fail_verification_round(state)
        self.assertEqual(stopped["pause"], {
            "code": "revision_limit_reached", "revisionCount": 1, "maxRevisions": 1,
            "pendingFindingIds": ["finding-1"],
            "findings": [{"id": "finding-1", "path": "changed.txt", "problem": "incorrect"}]})
        # A read-only status reports the same pause; the text message is unchanged beside it.
        status_args = self.agent_loop.build_parser().parse_args([
            "status", "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", stopped["loopId"]])
        status = self.agent_loop.status_loop(status_args)
        self.assertEqual(status["pause"], stopped["pause"])
        self.assertIn("loop.py extend-revisions", status["controlPlaneError"]["message"])
        self.assertEqual(status["createdAt"], state["createdAt"])

        # A loop stopped by a runtime that recorded no summaries still reports the identifiers.
        path = Path(stopped["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        del stored["revisionLimitFindings"]
        self.agent_exec.atomic_write_json(path, stored)
        legacy = self.agent_loop.status_loop(status_args)["pause"]
        self.assertEqual((legacy["pendingFindingIds"], legacy["findings"]), (["finding-1"], []))

        # Continue: the Human's three further revisions resume the loop and clear the pause.
        resumed = self.extend_revisions(stopped, additional="3")
        self.assertEqual((resumed["status"], resumed["maxRevisions"], resumed["pause"]), ("active", 4, None))
        self.assertEqual(self.runtime.dispatches[-1]["role"], "work")

    def test_revision_limit_pause_ends_when_the_human_stops_the_loop(self) -> None:
        state = self.fail_verification_round(self.start(["--max-revisions", "1"]))
        stopped = self.fail_verification_round(state)
        dispatched = len(self.runtime.dispatches)
        closed = self.close(stopped)
        self.assertEqual((closed["status"], closed["pause"]), ("cancelled", None))
        self.assertEqual(closed["terminalReason"]["code"], "human-closed")
        self.assertEqual(len(self.runtime.dispatches), dispatched)
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.extend_revisions(closed)
        self.assertEqual(raised.exception.code, "revision_extension_unavailable")

    def test_other_stops_report_no_pause(self) -> None:
        stopped = self.fail_work_receipt(self.start(), "receipt_missing", "Agent did not publish its receipt")
        self.assertEqual((stopped["status"], stopped["pause"]), ("runtime-error", None))

    def test_loop_captures_its_response_contract_and_old_loops_keep_the_file_contract(self) -> None:
        started = self.start()
        stored = self.agent_exec.safe_read_json(Path(started["statePath"]))
        self.assertEqual(stored["execution"]["responseContract"], 2)
        self.assertEqual(self.runtime.dispatches[0]["execution"]["responseContract"], 2)
        runtime = self.runtime_class(self.root)
        with mock.patch.object(runtime, "call", return_value={"runId": "run-1"}) as call:
            for execution in ({"codex": "/bin/true", "responseContract": 2}, {"codex": "/bin/true"}):
                for role in ("work", "verification"):
                    runtime.dispatch(
                        operation="send", agent_id=role + "-agent", role=role, request_file=self.request,
                        request_hash="0" * 64, dispatch_id="dispatch-contract",
                        verified_work_run_id=None if role == "work" else "run-1",
                        execution=execution, capability_binding_file=None, human_approval_policy="required")
        current, current_verification, legacy, legacy_verification = (item.args[0] for item in call.call_args_list)
        self.assertEqual(current[current.index("--response-contract") + 1], "2")
        # A loop persisted before the field finishes every later Work run under the file contract.
        self.assertEqual(legacy[legacy.index("--response-contract") + 1], "1")
        for arguments in (current_verification, legacy_verification):
            self.assertNotIn("--response-contract", arguments)

    def test_structured_run_without_receipt_fields_gets_the_automatic_repair_turn(self) -> None:
        # What a contract-2 run records when its final output lacks the fields (see test_runtime_response).
        started = self.start(["--task-mode", "work", "--receipt-recovery", "auto"])
        failed_run_id = started["latestWorkRunId"]
        recovered = self.fail_work_receipt(started, "receipt_missing", "Final output omitted the receipt fields: tests")
        request = Path(self.runtime.dispatches[-1]["request_file"]).read_text(encoding="utf-8")
        self.assertEqual(recovered["status"], "active")
        self.assertIs(recovered["receiptRecovery"]["automatic"], True)
        self.assertEqual(recovered["receiptRecovery"]["failedWorkRunId"], failed_run_id)
        # The recovery run stays on the loop's captured contract; one repair turn per task as before.
        self.assertEqual(self.runtime.dispatches[-1]["execution"]["responseContract"], 2)
        self.assertEqual(len(self.runtime.dispatches), 2)
        self.assertIn("Final output omitted the receipt fields: tests", request)
        self.assertIn("delivered the way this\nrun's instructions require", request)
        self.assertIn("Preserved receipt (absent when none was published)", request)

    def test_revision_limit_defaults_to_three_and_zero_is_unlimited(self) -> None:
        state = self.start()
        self.assertEqual(state["maxRevisions"], self.agent_loop.DEFAULT_MAX_REVISIONS)
        self.assertEqual(self.agent_loop.DEFAULT_MAX_REVISIONS, 3)
        for expected in (1, 2, 3):
            state = self.fail_verification_round(state)
            self.assertEqual((state["status"], state["revisionCount"]), ("active", expected))
        self.assertEqual(self.fail_verification_round(state)["status"], "needs-human-decision")

        self.close(self.agent_exec.safe_read_json(Path(state["statePath"])))
        self.request.write_text("unbounded work\n", encoding="utf-8")
        document = json.loads(self.tasks.read_text())
        document["tasks"][0]["requestHash"] = hashlib.sha256(self.request.read_bytes()).hexdigest()
        self.tasks.write_text(json.dumps(document))
        unlimited = self.start(["--max-revisions", "0"])
        for _ in range(5):
            unlimited = self.fail_verification_round(unlimited)
            self.assertEqual(unlimited["status"], "active")
        with self.assertRaises(self.agent_exec.ContractError) as negative:
            self.start(["--max-revisions", "-1"])
        self.assertEqual(negative.exception.code, "revision_limit_invalid")

    def test_loop_persisted_without_revision_limit_stays_unbounded(self) -> None:
        state = self.start()
        path = Path(state["statePath"])
        stored = self.agent_exec.safe_read_json(path)
        del stored["execution"]["maxRevisions"]
        del stored["revisionCount"]
        self.agent_exec.atomic_write_json(path, stored)
        for _ in range(5):
            state = self.fail_verification_round(state)
            self.assertEqual(state["status"], "active")
        self.assertIsNone(state["maxRevisions"])

    def test_next_task_starts_with_a_fresh_revision_budget(self) -> None:
        self.add_second_task()
        state = self.start(["--max-revisions", "1"])
        state = self.fail_verification_round(state)
        self.runtime.complete_work("work-agent", state["latestWorkRunId"], ["finding-1"])
        state = self.reconcile(state)
        self.runtime.complete_verification("verification-agent", state["latestVerificationRunId"], "pass")
        second = self.reconcile(state)
        self.assertEqual(second["workflow"]["index"], 1)
        self.assertEqual(second["revisionCount"], 0)
        self.assertEqual(self.fail_verification_round(second)["status"], "active")

    def test_revision_verification_request_names_addressed_findings(self) -> None:
        state = self.start()
        self.runtime.complete_work("work-agent", state["latestWorkRunId"])
        first = self.reconcile(state)
        first_request = Path(self.runtime.dispatches[-1]["request_file"]).read_text(encoding="utf-8")
        self.assertNotIn("Revision context", first_request)
        self.runtime.complete_verification("verification-agent", first["latestVerificationRunId"], "fail")
        revision = self.reconcile(first)
        self.runtime.complete_work("work-agent", revision["latestWorkRunId"], ["finding-1"])
        self.reconcile(revision)
        request = Path(self.runtime.dispatches[-1]["request_file"]).read_text(encoding="utf-8")
        self.assertIn("Revision context", request)
        self.assertIn('addressed: ["finding-1"]', request)
        self.assertIn(first["latestVerificationRunId"], request)
        self.assertIn("keeps its original id", request)
        self.assertIn("the rest of the request", request)  # context, never a licence to skip

    def test_driver_asks_exec_to_settle_a_stale_child(self) -> None:
        started = self.start(["--task-mode", "work"])
        ticks = []

        def advance(_seconds):
            ticks.append(1)
            if len(ticks) == 2:
                self.runtime.complete_work("work-agent", started["latestWorkRunId"])

        with mock.patch.object(self.agent_loop, "STALE_CHECK_SECONDS", 0.0):
            self.runtime.stale_check_error = self.agent_exec.ContractError("child_runtime_failure", "busy")
            ended = self.drive(started, advance)
        self.assertEqual(ended["status"], "completed")  # a failed check never stops the driver
        self.assertEqual(self.runtime.stale_checks, ["work-agent", "work-agent"])

    def test_public_state_classifies_the_recorded_stop(self) -> None:
        classify = self.agent_loop.failure_class
        self.assertIsNone(classify(None))
        self.assertEqual(classify({"code": "receipt_missing"}), "contract")
        # Retired 2026-10-03: unsaved lesson captures no longer stop a run. Stored stops stay readable.
        self.assertEqual(classify({"code": "lesson_recording_incomplete"}), "contract")
        self.assertNotIn("lesson_recording_incomplete", self.agent_loop.FAILURE_CLASSES["contract"])
        self.assertEqual(classify({"code": "child_runtime_failure"}), "transient")
        self.assertEqual(classify({"code": "native_backend_error"}), "provider")
        self.assertEqual(classify({"code": "sandbox_unavailable"}), "environment")
        for code in ("task_repository_invalid", "parent_session_invalid", "task_workspace_binding", "task_target_busy_timeout"):
            self.assertEqual(classify({"code": code}), "environment")
        for code in ("task_target_busy", "task_workspace_busy", "lock_busy"):
            self.assertEqual(classify({"code": code}), "transient")
        self.assertEqual(classify({"code": "revision_limit_reached"}), "human")
        self.assertEqual(classify({"code": "something_new"}), "unknown")
        codes = [code for values in self.agent_loop.FAILURE_CLASSES.values() for code in values]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIsNone(self.start()["failureClass"])


    def code_workspace_start(self, mode="work"):
        self.init_fixture_repository()
        workspace_file = self.root / "workspace.json"
        workspace_file.write_text(json.dumps({"mode": "code", "repositories": [{"path": str(self.root), "targetBranch": "develop",
            "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('file.txt').exists()"]]}]}))
        extra = ["--task-mode", mode, "--workspace-file", str(workspace_file)]
        started = self.start(extra)
        return started, extra

    def init_fixture_repository(self):
        from tasks import workspaces
        workspaces.worktrees.git(self.root, "init", "-b", "develop")
        workspaces.worktrees.git(self.root, "config", "user.name", "Fixture")
        workspaces.worktrees.git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text("*.md\n*.json\n")
        (self.root / "file.txt").write_text("base\n")
        workspaces.worktrees.git(self.root, "add", ".gitignore", "file.txt")
        workspaces.worktrees.git(self.root, "commit", "-m", "base")

    def checked_code_work(self, started):
        run = self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        receipt = json.loads(Path(run["receiptPath"]).read_text())
        receipt.update(changedPaths=["file.txt"], tests={"run": True, "reason": "fixture own checks passed"})
        self.agent_exec.atomic_write_json(Path(run["receiptPath"]), receipt)
        self.agent_exec.atomic_write_json(Path(run["statePath"]), run)
        return run

    def test_code_workspace_precedes_dispatch_duplicate_start_and_checked_completion(self):
        started, extra = self.code_workspace_start()
        workspace = started["taskWorkspaces"]["task-one"]
        path = Path(workspace["path"])
        self.assertTrue(path.exists())
        self.assertEqual(self.runtime.runs[("work-agent", started["latestWorkRunId"])]["workingDirectory"], str(path))
        before = len(self.runtime.dispatches)
        duplicate = self.start(extra)
        self.assertEqual(duplicate["loopId"], started["loopId"])
        self.assertEqual(len(self.runtime.dispatches), before)
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual((self.root / "file.txt").read_text(), "result")
        self.assertEqual(len(self.runtime.dispatches), 1)
        self.assertEqual(complete["taskWorkspaces"]["task-one"]["verification"], "not requested")

    def target_owner(self, agent="other-worker", status="running", cwd=None):
        owner = {"agentId": agent, "runId": "run-owner", "role": "work", "status": status,
                 "workingDirectory": str(cwd or self.root)}
        path = self.agent_exec.state_file(self.root, agent, owner["runId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_exec.atomic_write_json(path, owner)
        return owner, path

    def test_busy_target_driver_waits_then_integrates_without_redispatch(self):
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        owner, owner_path = self.target_owner(cwd=self.root / "subdirectory")
        waiting = self.reconcile(started)
        self.assertEqual((waiting["status"], waiting["phase"]), ("active", "integrating"))
        self.assertEqual(waiting["workflow"]["tasks"][0]["workStatus"], "completed")
        self.assertEqual(waiting["integrationWait"]["owners"][0]["runId"], owner["runId"])
        self.assertEqual((self.root / "file.txt").read_text(), "base\n")
        def release(_seconds):
            self.agent_exec.atomic_write_json(owner_path, {**owner, "status": "completed"})
        complete = self.drive(waiting, release)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual((self.root / "file.txt").read_text(), "result")
        self.assertIsNone(complete["integrationWait"])
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_historical_busy_stop_resumes_same_completed_work(self):
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        state_path = Path(started["statePath"])
        state = self.agent_exec.safe_read_json(state_path)
        state.update(status="runtime-error", phase="integrating", controlPlaneError={"code": "task_target_busy", "message": "old busy"})
        self.agent_exec.atomic_write_json(state_path, state)
        complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertIsNone(complete["controlPlaneError"])
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_integration_lock_contention_is_nonblocking_and_resumable(self):
        from execution import worktrees
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        common = Path(worktrees.git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip())
        with self.agent_exec.file_lock(common / ".agent-factory-integration.lock"):
            waiting = self.reconcile(started)
            again = self.reconcile(waiting)
        self.assertEqual(waiting["integrationWait"]["code"], "lock_busy")
        self.assertEqual(again["integrationWait"]["attempts"], 2)
        complete = self.reconcile(again)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_only_exact_accepted_main_is_exempt_from_target_busy(self):
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        work = self.checked_code_work(started)
        parent, parent_path = self.target_owner("main-parent")
        parent["role"] = "main"
        self.agent_exec.atomic_write_json(parent_path, parent)
        parent_bytes = parent_path.read_bytes()
        work.update(parentAgentId=parent["agentId"], parentRunId=parent["runId"])
        self.agent_exec.atomic_write_json(Path(work["statePath"]), work)
        state_path = Path(started["statePath"])
        state = self.agent_exec.safe_read_json(state_path)
        state["parentStatePath"] = str(parent_path)
        self.agent_exec.atomic_write_json(state_path, state)
        other, other_path = self.target_owner("main-unrelated")
        other["role"] = "main"
        self.agent_exec.atomic_write_json(other_path, other)
        waiting = self.reconcile(started)
        self.assertEqual([o["agentId"] for o in waiting["integrationWait"]["owners"]], [other["agentId"]])
        self.agent_exec.atomic_write_json(other_path, {**other, "status": "completed"})
        complete = self.reconcile(waiting)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(parent_path.read_bytes(), parent_bytes)

    def test_run_owner_requires_positive_empty_evidence(self):
        from tasks import workspaces
        owner = {"agentId": "old-worker", "status": "running", "workerIdentity": {"pid": 1}, "codexIdentity": {"pid": 2}}
        with mock.patch.object(self.agent_exec, "load_session", return_value={}), \
                mock.patch.object(self.agent_exec, "heartbeat_stale", return_value=True), \
                mock.patch.object(self.agent_exec, "process_identity_status", return_value="dead") as identity:
            self.assertFalse(workspaces.run_owns_checkout(self.agent_exec, owner, self.root))
            for status in ("match", "mismatch", "unknown"):
                identity.return_value = status
                self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, owner, self.root))
            identity.return_value = "dead"
            for status in ("accepted", "queued"):
                self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, {**owner, "status": status}, self.root))
            with mock.patch.object(self.agent_exec, "heartbeat_stale", return_value=False):
                self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, owner, self.root))
            self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, {"agentId": "old-worker", "status": "running"}, self.root))
            contained = {**owner, "containment": {"kind": "test"}}
            with mock.patch.object(self.agent_exec, "validate_state_containment_fields"), \
                    mock.patch.object(self.agent_exec, "_validate_state_containment", return_value={}), \
                    mock.patch.object(self.agent_exec, "containment_is_empty", return_value=True) as empty:
                self.assertFalse(workspaces.run_owns_checkout(self.agent_exec, contained, self.root))
                empty.return_value = False
                self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, contained, self.root))
                empty.side_effect = self.agent_exec.ContractError("containment_identity_mismatch", "unknown")
                self.assertTrue(workspaces.run_owns_checkout(self.agent_exec, contained, self.root))

    def test_busy_wait_limit_preserves_isolated_result_and_receipt(self):
        started = self.isolated_brief_start(self.isolated_repository())
        # Historical loops retain their captured finite uncertain-owner budget.
        stored = self.agent_exec.safe_read_json(Path(started["statePath"]))
        stored.pop("lifecycleVersion")
        self.agent_exec.atomic_write_json(Path(started["statePath"]), stored)
        task_id = next(iter(started["taskWorkspaces"]))
        path = Path(started["taskWorkspaces"][task_id]["path"])
        (path / "file.txt").write_text("result")
        work = self.isolated_checked_work(started)
        receipt = Path(work["receiptPath"]).read_bytes()
        self.target_owner()
        with mock.patch.object(self.agent_loop, "INTEGRATION_WAIT_ATTEMPTS", 2):
            waiting = self.reconcile(started)
            preserved = self.reconcile(waiting)
        self.assert_preserved(preserved, task_id, path, "task_target_busy_timeout")
        self.assertEqual(Path(work["receiptPath"]).read_bytes(), receipt)
        self.assertEqual((path / "file.txt").read_text(), "result")
        self.assertEqual((self.root / "file.txt").read_text(), "base\n")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_verified_live_owner_wait_does_not_exhaust_uncertain_budget(self):
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        owner, owner_path = self.target_owner()
        owner["workerIdentity"] = {"pid": 1}
        self.agent_exec.atomic_write_json(owner_path, owner)
        with mock.patch.object(self.agent_loop, "INTEGRATION_WAIT_ATTEMPTS", 1), \
                mock.patch.object(self.agent_exec, "process_identity_status", return_value="match"):
            for _ in range(3):
                waiting = self.reconcile(started)
                self.assertEqual(waiting["status"], "active")
                self.assertTrue(waiting["integrationWait"]["verifiedLive"])
                self.assertEqual(waiting["integrationWait"]["attempts"], 0)
        self.agent_exec.atomic_write_json(owner_path, {**owner, "status": "completed"})
        self.assertEqual(self.reconcile(waiting)["status"], "completed")

    def test_stale_dead_target_owner_is_ignored_without_rewriting_its_run(self):
        started, _ = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        self.checked_code_work(started)
        owner, owner_path = self.target_owner()
        owner["workerIdentity"] = {"pid": 1}
        self.agent_exec.atomic_write_json(owner_path, owner)
        before = owner_path.read_bytes()
        with mock.patch.object(self.agent_exec, "load_session", return_value={}), \
                mock.patch.object(self.agent_exec, "heartbeat_stale", return_value=True), \
                mock.patch.object(self.agent_exec, "process_identity_status", return_value="dead"):
            complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(owner_path.read_bytes(), before)

    def test_nonisolated_busy_timeout_stops_with_work_evidence_preserved(self):
        started, _ = self.code_workspace_start()
        stored = self.agent_exec.safe_read_json(Path(started["statePath"]))
        stored.pop("lifecycleVersion")
        self.agent_exec.atomic_write_json(Path(started["statePath"]), stored)
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("result")
        work = self.checked_code_work(started)
        self.target_owner(cwd=path)
        with mock.patch.object(self.agent_loop, "INTEGRATION_WAIT_ATTEMPTS", 1):
            stopped = self.reconcile(started)
        self.assertEqual((stopped["status"], stopped["phase"]), ("runtime-error", "integrating"))
        self.assertEqual(stopped["controlPlaneError"]["code"], "task_workspace_busy_timeout")
        self.assertEqual(stopped["workflow"]["tasks"][0]["workStatus"], "completed")
        self.assertTrue(Path(work["receiptPath"]).exists())
        self.assertTrue(path.exists())
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_target_changes_remain_bounded_when_revision_limit_is_zero(self):
        from tasks import workspaces
        started, _ = self.code_workspace_start()
        self.checked_code_work(started)
        state_path = Path(started["statePath"])
        state = self.agent_exec.safe_read_json(state_path)
        state["execution"].update(workIsolation=True, maxRevisions=0)
        self.agent_exec.atomic_write_json(state_path, state)
        with mock.patch.object(workspaces, "integrate", return_value={"status": "target-changed"}):
            for _ in range(self.agent_loop.DEFAULT_MAX_REVISIONS):
                self.assertEqual(self.reconcile(started)["status"], "active")
            stopped = self.reconcile(started)
        self.assertEqual(stopped["terminalReason"]["cause"], "task_target_changed")
        self.assertEqual(len(self.runtime.dispatches), 1)

    def test_code_conflict_revision_reuses_session_and_unclear_resolution_stops(self):
        from execution import worktrees
        started, _extra = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("work")
        (self.root / "file.txt").write_text("target")
        worktrees.git(self.root, "commit", "-am", "target")
        self.checked_code_work(started)
        revised = self.reconcile(started)
        self.assertEqual(revised["status"], "active")
        self.assertEqual(self.runtime.dispatches[-1]["operation"], "send")
        self.assertEqual(self.runtime.dispatches[-1]["agent_id"], "work-agent")
        self.assertEqual(Path(revised["taskWorkspaces"]["task-one"]["path"]), path)
        request = self.runtime.dispatches[-1]["request_file"].read_text()
        self.assertIn("Git stages 1/2/3", request)
        self.assertIn("file.txt", request)
        self.checked_code_work(revised)  # No invented semantics and no staged resolution.
        paused = self.reconcile(revised)
        self.assertEqual(paused["status"], "needs-human-decision")
        self.assertEqual(paused["controlPlaneError"]["code"], "task_conflict_unresolved")
        self.assertTrue(path.exists())
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_requested_verification_precedes_code_integration(self):
        started, _extra = self.code_workspace_start("work-verification")
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("verified result")
        self.checked_code_work(started)
        verifying = self.reconcile(started)
        self.assertEqual((self.root / "file.txt").read_text(), "base\n")
        run = self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "pass")
        self.agent_exec.atomic_write_json(Path(run["statePath"]), run)
        complete = self.reconcile(verifying)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["taskWorkspaces"]["task-one"]["verification"], "pass")
        self.assertEqual((self.root / "file.txt").read_text(), "verified result")

    def test_code_conflict_resolution_rechecks_and_integrates_same_task(self):
        from execution import worktrees
        started, _extra = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("work")
        (self.root / "file.txt").write_text("target")
        worktrees.git(self.root, "commit", "-am", "target")
        self.checked_code_work(started)
        revised = self.reconcile(started)
        first_session = self.agent_exec.safe_read_json(self.agent_exec.session_file(self.root, "work-agent"))["sessionId"]
        (path / "file.txt").write_text("both meanings")
        worktrees.git(path, "add", "file.txt")
        self.checked_code_work(revised)
        completed = self.reconcile(revised)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.agent_exec.safe_read_json(self.agent_exec.session_file(self.root, "work-agent"))["sessionId"], first_session)
        self.assertEqual((self.root / "file.txt").read_text(), "both meanings")
        unit = completed["taskWorkspaces"]["task-one"]["repositories"][0]
        self.assertEqual(unit["integrationChecks"][0]["exitCode"], 0)
        self.assertTrue(unit["cleaned"])
        self.assertEqual([d["role"] for d in self.runtime.dispatches], ["work", "work"])

    def test_cancellation_preserves_code_workspace_and_never_integrates(self):
        from execution import worktrees
        started, _extra = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("partial result")
        before = worktrees.git(self.root, "rev-parse", "HEAD").stdout
        def cancel(arguments):
            self.runtime.runs[("work-agent", started["latestWorkRunId"])]["status"] = "cancelled"
            return {"kind": "ack"}
        with mock.patch.object(self.runtime, "call", create=True, side_effect=cancel):
            stopped = self.stop_task(started)
        self.assertEqual(stopped["status"], "cancelled")
        self.assertTrue(path.exists())
        self.assertEqual(worktrees.git(self.root, "rev-parse", "HEAD").stdout, before)
        self.assertEqual(len(self.runtime.dispatches), 1)

    def isolated_brief_start(self, plan, agent="work-agent"):
        workspace_file = self.root / "isolated-workspace.json"
        workspace_file.write_text(json.dumps(plan))
        self.request.write_text("Goal: change file.txt\n\nScope: file.txt only\nDone: file changed\n", encoding="utf-8")
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request), "--task-mode", "work",
            "--work-agent", agent, "--codex", "/bin/true", "--work-isolation", "--workspace-file", str(workspace_file)])
        return self.agent_loop.start_loop(args)

    def isolated_repository(self):
        from execution import worktrees
        self.init_fixture_repository()
        worktrees.git(self.root, "checkout", "-b", "feature")
        return {"mode": "code", "repositories": [{"path": str(self.root),
                "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('file.txt').exists()"]]}]}

    def isolated_checked_work(self, started, agent="work-agent"):
        run = self.runtime.complete_work(agent, started["latestWorkRunId"])
        receipt = json.loads(Path(run["receiptPath"]).read_text())
        receipt.update(changedPaths=["file.txt"], tests={"run": True, "reason": "fixture own checks passed"})
        self.agent_exec.atomic_write_json(Path(run["receiptPath"]), receipt)
        self.agent_exec.atomic_write_json(Path(run["statePath"]), run)
        return run

    def test_work_isolation_brief_targets_current_branch_and_merges_with_bilingual_messages(self):
        from execution import worktrees
        plan = self.isolated_repository()
        for invalid, code in ((None, "task_workspace_required"), ({"mode": "shared"}, "task_workspace_invalid")):
            with self.subTest(code=code), self.assertRaises(self.agent_exec.ContractError) as raised:
                if invalid is None:
                    self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([
                        "start", "--project-root", str(self.root), "--request-file", str(self.request), "--task-mode", "work",
                        "--work-agent", "work-agent", "--codex", "/bin/true", "--work-isolation"]))
                else:
                    self.isolated_brief_start(invalid)
            self.assertEqual(raised.exception.code, code)
        started = self.isolated_brief_start(plan)
        state = json.loads(Path(started["statePath"]).read_text())
        self.assertIs(state["execution"]["workIsolation"], True)
        workspace = started["taskWorkspaces"][state["execution"]["taskBinding"]["taskId"]]
        self.assertEqual(workspace["repositories"][0]["targetBranch"], "feature")
        (Path(workspace["path"]) / "file.txt").write_text("isolated result")
        self.isolated_checked_work(started)
        complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual((self.root / "file.txt").read_text(), "isolated result")
        self.assertTrue(complete["taskWorkspaces"][workspace["taskId"]]["repositories"][0]["cleaned"])
        subjects = [worktrees.git(self.root, "log", "-1", "--format=%s", ref).stdout.decode().strip() for ref in ("HEAD", "HEAD^2")]
        name = workspace["workflowId"] + "/" + workspace["taskId"]
        self.assertEqual(subjects, ["Merge task " + name + " / 작업 " + name + " 병합", "Task " + name + " / 작업 " + name])

    def isolated_conflict(self):
        from execution import worktrees
        started = self.isolated_brief_start(self.isolated_repository())
        task_id = next(iter(started["taskWorkspaces"]))
        path = Path(started["taskWorkspaces"][task_id]["path"])
        (path / "file.txt").write_text("work")
        (self.root / "file.txt").write_text("target")
        worktrees.git(self.root, "commit", "-am", "target")
        self.isolated_checked_work(started)
        revised = self.reconcile(started)
        self.assertEqual(revised["status"], "active")
        request = self.runtime.dispatches[-1]["request_file"].read_text()
        self.assertIn("never return needs-human-decision", request)
        self.assertNotIn("Return needs-human-decision with specific unresolved choices", request)
        return revised, task_id, path

    def assert_preserved(self, result, task_id, path, cause):
        from execution import worktrees
        stored = self.agent_exec.safe_read_json(Path(result["statePath"]))
        self.assertEqual(result["status"], "runtime-error" if stored.get("lifecycleVersion") else "completed")
        if stored.get("lifecycleVersion"):
            self.assertIn(result["completion"]["integration"], {"partial", "preserved"})
        self.assertIsNone(result["controlPlaneError"])
        self.assertEqual(result["terminalReason"]["code"], "integration_preserved")
        self.assertEqual(result["terminalReason"]["cause"], cause)
        unit = result["taskWorkspaces"][task_id]["repositories"][0]
        self.assertEqual(result["terminalReason"]["preserved"][0]["branch"], unit["branch"])
        self.assertIn(unit["branch"], result["terminalReason"]["message"])
        self.assertIn(str(path), result["terminalReason"]["message"])
        self.assertTrue(path.exists())
        self.assertFalse(unit.get("cleaned"))
        self.assertEqual(worktrees.git(self.root, "show-ref", "--verify", "--quiet", "refs/heads/" + unit["branch"], check=False).returncode, 0)

    def test_work_isolation_unresolved_conflict_preserves_branch_without_human_decision(self):
        revised, task_id, path = self.isolated_conflict()
        self.isolated_checked_work(revised)  # Conflict stages left unchanged.
        preserved = self.reconcile(revised)
        self.assert_preserved(preserved, task_id, path, "task_conflict_unresolved")
        self.assertEqual(preserved["terminalReason"]["files"], ["file.txt"])
        self.assertEqual((self.root / "file.txt").read_text(), "target")
        self.assertEqual(len(self.runtime.dispatches), 2)

    def test_work_isolation_conflict_revision_asking_a_human_preserves_branch(self):
        revised, task_id, path = self.isolated_conflict()
        self.runtime.runs[("work-agent", revised["latestWorkRunId"])]["status"] = "needs-human-decision"
        self.assert_preserved(self.reconcile(revised), task_id, path, "task_conflict_unresolved")

    def test_work_isolation_conflict_revision_limit_preserves_branch(self):
        revised, task_id, path = self.isolated_conflict()
        state_path = Path(revised["statePath"])
        state = json.loads(state_path.read_text())
        state["execution"]["maxRevisions"] = 1
        state["lastIntegrationConflict"] = "an earlier conflict"
        self.agent_exec.atomic_write_json(state_path, state)
        self.isolated_checked_work(revised)
        self.assert_preserved(self.reconcile(revised), task_id, path, "task_conflict_revision_limit")

    def test_work_isolation_dirty_target_preserves_branch_when_the_merge_would_change_its_files(self):
        from execution import worktrees
        started = self.isolated_brief_start(self.isolated_repository())
        task_id = next(iter(started["taskWorkspaces"]))
        path = Path(started["taskWorkspaces"][task_id]["path"])
        (path / "file.txt").write_text("isolated result")
        (self.root / "file.txt").write_text("Human work in progress")
        before = worktrees.git(self.root, "rev-parse", "HEAD").stdout
        self.isolated_checked_work(started)
        preserved = self.reconcile(started)
        self.assert_preserved(preserved, task_id, path, "task_target_dirty")
        self.assertEqual(preserved["terminalReason"]["files"], ["file.txt"])
        self.assertIn("file.txt", preserved["terminalReason"]["message"])
        self.assertEqual(worktrees.git(self.root, "rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.root / "file.txt").read_text(), "Human work in progress")

    def test_work_isolation_merges_past_unrelated_uncommitted_target_files(self):
        from execution import worktrees
        started = self.isolated_brief_start(self.isolated_repository())
        task_id = next(iter(started["taskWorkspaces"]))
        path = Path(started["taskWorkspaces"][task_id]["path"])
        (path / "file.txt").write_text("isolated result")
        (self.root / "unrelated.txt").write_text("Human work in progress")
        (self.root / ".gitignore").write_text("*.md\n*.json\n# Human edit in progress\n")
        self.isolated_checked_work(started)
        complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["terminalReason"]["code"], "work-completed")
        self.assertEqual(worktrees.git(self.root, "show", "HEAD:file.txt").stdout.decode(), "isolated result")
        self.assertEqual((self.root / "file.txt").read_text(), "isolated result")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "Human work in progress")
        self.assertIn("# Human edit in progress", (self.root / ".gitignore").read_text())
        self.assertEqual(sorted(worktrees.git(self.root, "status", "--porcelain").stdout.decode().splitlines()),
                         [" M .gitignore", "?? unrelated.txt"])
        self.assertTrue(complete["taskWorkspaces"][task_id]["repositories"][0]["cleaned"])

    def test_work_isolation_commits_runtime_lessons_with_the_task(self):
        from execution import worktrees
        plan = self.isolated_repository()
        (self.root / ".gitignore").write_text("*.md\n*.json\n!docs/lessons-learned/errors/*.md\n")
        worktrees.git(self.root, "commit", "-qam", "Track lessons")
        started = self.isolated_brief_start(plan)
        task_id = next(iter(started["taskWorkspaces"]))
        path = Path(started["taskWorkspaces"][task_id]["path"])
        (path / "file.txt").write_text("isolated result")
        run = self.isolated_checked_work(started)
        event = {"type": "item.completed", "item": {"type": "command_execution", "id": "command-1", "exit_code": 1}}
        saved = self.agent_exec.lesson_capture.observe(self.root, run, event)
        self.assertTrue(saved["saved"])
        self.assertTrue((path / saved["path"]).is_file())
        self.assertFalse((self.root / "docs").exists())  # The target checkout stays clean.
        complete = self.reconcile(started)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(worktrees.git(self.root, "status", "--porcelain").stdout, b"")
        self.assertIn(saved["path"], worktrees.git(self.root, "ls-files").stdout.decode().splitlines())
        self.assertTrue(complete["taskWorkspaces"][task_id]["repositories"][0]["cleaned"])

    def test_work_isolation_follows_the_captured_main_selection(self):
        parent = self.root / "parent-state.json"
        args = self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.request), "--work-agent", "work-agent"])
        self.assertIs(self.agent_loop.work_isolation(args), False)
        for captured, requested, expected in ((True, None, True), (False, None, False), (None, True, True), (True, True, True)):
            parent.write_text(json.dumps({"executionOptions": {} if captured is None else {"workIsolation": captured}}))
            args.work_isolation = requested
            with self.subTest(captured=captured, requested=requested), \
                    mock.patch.dict(os.environ, {self.agent_exec.execution_policy.PARENT_STATE_ENV: str(parent)}):
                self.assertIs(self.agent_loop.work_isolation(args), expected)
        parent.write_text(json.dumps({"executionOptions": {"workIsolation": False}}))
        args.work_isolation = True
        with mock.patch.dict(os.environ, {self.agent_exec.execution_policy.PARENT_STATE_ENV: str(parent)}), \
                self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.work_isolation(args)
        self.assertEqual(raised.exception.code, "work_isolation_mismatch")

    def test_same_worker_moves_from_cleaned_code_task_to_read_only_task(self):
        second = self.root / "second.md"
        second.write_text("read-only follow-up")
        document = json.loads(self.tasks.read_text())
        document["tasks"].append({"id": "task-two", "title": "Read-only", "description": "inspect result", "completionCriteria": "report result",
                                  "requestFile": str(second), "workspace": {"mode": "read-only"}})
        self.tasks.write_text(json.dumps(document))
        started, _extra = self.code_workspace_start()
        path = Path(started["taskWorkspaces"]["task-one"]["path"])
        (path / "file.txt").write_text("code result")
        self.checked_code_work(started)
        following = self.reconcile(started)
        self.assertFalse(path.exists())
        self.assertEqual(self.runtime.runs[("work-agent", following["latestWorkRunId"])]["workingDirectory"], str(self.root))
        self.assertEqual(set(following["taskWorkspaces"]), {"task-one"})
        self.runtime.complete_work("work-agent", following["latestWorkRunId"])
        complete = self.reconcile(following)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(set(complete["taskWorkspaces"]), {"task-one"})


if __name__ == "__main__":
    unittest.main()


class RuntimeCallRetryTests(unittest.TestCase):
    """Status reads are idempotent and retried; a dispatch is never replayed."""

    def setUp(self):
        self.agent_exec, self.loop = load_modules()
        self.runtime = object.__new__(self.loop.AgentRuntime)
        self.runtime.project_root = Path("/tmp/project")
        self.runtime.script = Path("/tmp/exec.py")
        self.runtime.parent_state_path = None
        sleep = mock.patch.object(self.loop.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        paths = mock.patch.object(self.agent_exec.runtime_paths, "arguments", return_value=[])
        paths.start()
        self.addCleanup(paths.stop)

    def response(self, document, code=0):
        return mock.Mock(returncode=code, stdout=json.dumps(document) + "\n")

    def test_status_read_survives_a_transient_timeout(self):
        timeout = self.loop.subprocess.TimeoutExpired("exec.py", 30)
        with mock.patch.object(self.loop.subprocess, "run", side_effect=[
                timeout, self.response({"kind": "status", "run": {"status": "running"}})]) as command:
            self.assertEqual(self.runtime.status("work-agent", "run-1"), {"status": "running"})
        self.assertEqual(command.call_count, 2)
        self.sleep.assert_called_once_with(self.loop.STATUS_READ_BACKOFF_SECONDS)

    def test_status_read_gives_up_after_bounded_attempts(self):
        with mock.patch.object(self.loop.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")) as command:
            with self.assertRaises(self.agent_exec.ContractError) as failure:
                self.runtime.status("work-agent", "run-1")
        self.assertEqual(failure.exception.code, "child_runtime_failure")
        self.assertEqual(command.call_count, self.loop.STATUS_READ_ATTEMPTS)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.5, 1.0])

    def test_contract_answers_are_not_retried(self):
        missing = self.response({"kind": "error", "error": {"code": "dispatch_not_found", "message": "none"}}, 2)
        with mock.patch.object(self.loop.subprocess, "run", return_value=missing) as command:
            with self.assertRaises(self.agent_exec.ContractError) as failure:
                self.runtime.status_dispatch("work-agent", "dispatch-1")
        self.assertEqual(failure.exception.code, "dispatch_not_found")
        self.assertEqual(command.call_count, 1)
        self.sleep.assert_not_called()

    def test_dispatch_is_never_replayed(self):
        timeout = self.loop.subprocess.TimeoutExpired("exec.py", 30)
        with mock.patch.object(self.loop.subprocess, "run", side_effect=timeout) as command:
            with self.assertRaises(self.loop.subprocess.TimeoutExpired):
                self.runtime.call(["send", "--agent", "work-agent"])
            with self.assertRaises(self.loop.subprocess.TimeoutExpired):
                self.runtime.reconcile_stale("work-agent")
        self.assertEqual(command.call_count, 2)
        self.sleep.assert_not_called()


class RoleModelDispatchTests(unittest.TestCase):
    def test_role_flags_are_sent_on_initial_and_revision_turns(self):
        _, loop = load_modules()
        runtime = object.__new__(loop.AgentRuntime)
        runtime.call = mock.Mock(return_value={"status": "accepted"})
        execution = {"codex": "/bin/true", "taskMode": "plan-work-verification", "agentModels": {
            "work": {"model": "work-model", "reasoningEffort": "high", "fast": True},
            "verification": {"model": "verify-model", "reasoningEffort": "low", "fast": False}}}
        for operation in ("submit", "send"):
            for role, model, effort, fast_flag in (("work", "work-model", "high", "--fast"), ("verification", "verify-model", "low", "--no-fast")):
                runtime.dispatch(operation=operation, agent_id=role, role=role,
                    request_file=Path("/tmp/role-request.md"), request_hash="0" * 64,
                    dispatch_id="dispatch-role", verified_work_run_id=None,
                    execution=execution, capability_binding_file=None, human_approval_policy="required")
                args = runtime.call.call_args.args[0]
                self.assertEqual(args[args.index("--model") + 1], model)
                self.assertEqual(args[args.index("--reasoning-effort") + 1], effort)
                self.assertIn(fast_flag, args)
                self.assertEqual(loop.role_model_options(execution, role, operation), execution["agentModels"][role])
                self.assertEqual("--task-mode" in args, role == "work")

    def test_role_flags_parse_and_legacy_shared_model_stays_submit_only(self):
        _, loop = load_modules()
        args = loop.build_parser().parse_args(["start", "--task-list-file", "/tmp/tasks.json", "--task-id", "task-one", "--request-file", "/tmp/request.md", "--work-agent", "work-one",
            "--work-model", "worker", "--work-reasoning-effort", "high", "--work-fast", "--verification-model", "reviewer", "--verification-reasoning-effort", "low", "--no-verification-fast"])
        self.assertEqual(args.work_model, "worker")
        self.assertEqual(args.verification_reasoning_effort, "low")
        self.assertIs(args.work_fast, True)
        self.assertIs(args.verification_fast, False)
        self.assertEqual(loop.role_model_options({"model": "legacy"}, "work", "submit"), {"model": "legacy"})
        self.assertEqual(loop.role_model_options({"model": "legacy"}, "work", "send"), {})

class RolePermissionDispatchTests(unittest.TestCase):
    def test_role_policy_is_preserved_on_submit_and_revision(self):
        exec_module, loop = load_modules()
        runtime = object.__new__(loop.AgentRuntime)
        runtime.call = mock.Mock(return_value={"status": "accepted"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            profiles = {}
            for role, mode in (("work", "bypass"), ("verification", "workspace-write")):
                policy = exec_module.execution_policy.role_policy(mode, None, root)
                path = root / (role + '.json')
                path.write_text(json.dumps(policy))
                profiles[role] = {"policy": policy, "path": str(path), "humanApprovalPolicy": "bypass" if mode == "bypass" else "required"}
            for operation in ("submit", "send"):
                for role in profiles:
                    runtime.dispatch(operation=operation, agent_id=role, role=role,
                        request_file=root / 'request.md', request_hash='0' * 64,
                        dispatch_id='dispatch', verified_work_run_id=None,
                        execution={"codex": '/bin/true', "agentPermissions": profiles},
                        capability_binding_file=None, human_approval_policy='required')
                    args = runtime.call.call_args.args[0]
                    self.assertEqual(args[args.index('--execution-policy-file') + 1], profiles[role]['path'])
                    self.assertEqual(args[args.index('--human-approval-policy') + 1], profiles[role]['humanApprovalPolicy'])
            Path(profiles['work']['path']).write_text('{}')
            with self.assertRaises(exec_module.ContractError):
                runtime.dispatch(operation='send', agent_id='work', role='work', request_file=root / 'request.md', request_hash='0' * 64,
                    dispatch_id='dispatch', verified_work_run_id=None, execution={"codex": '/bin/true', "agentPermissions": profiles},
                    capability_binding_file=None, human_approval_policy='required')

    def test_permission_snapshot_is_validated_and_cannot_be_minted_by_child(self):
        import argparse
        import os
        exec_module, _ = load_modules()
        with mock.patch.dict(os.environ, {}, clear=True):
            options = exec_module.requested_execution(argparse.Namespace(agent_permissions='{"work":"workspace-write"}'))
            self.assertEqual(options["agentPermissions"], {"work": "workspace-write"})
            for value in ('[]', '{"bad":"bypass"}', '{"work":"root"}'):
                with self.assertRaises(exec_module.ContractError):
                    exec_module.requested_execution(argparse.Namespace(agent_permissions=value))
            os.environ[exec_module.execution_policy.PARENT_STATE_ENV] = '/tmp/parent-state.json'
            with self.assertRaises(exec_module.ContractError):
                exec_module.requested_execution(argparse.Namespace(agent_permissions='{"work":"bypass"}'))


class WorkspacePlanDispatchBindingTests(unittest.TestCase):
    """loop start -> real exec submit -> complete_pending_dispatch with a captured task workspace."""

    def setUp(self) -> None:
        from tasks import workspaces
        self.agent_exec, self.agent_loop = load_modules()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        git = workspaces.worktrees.git
        git(self.root, "init", "-b", "develop")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text("*.md\n*.json\n")
        (self.root / "file.txt").write_text("base\n")
        git(self.root, "add", ".gitignore", "file.txt")
        git(self.root, "commit", "-m", "base")
        self.brief = self.root / "brief.md"
        self.brief.write_text("Goal: change file.txt\n\nScope: file.txt only\nDone: file changed\n", encoding="utf-8")
        agent_exec = self.agent_exec

        def bridge(runtime, arguments):
            with mock.patch.object(agent_exec, "spawn_worker", return_value=123), \
                    mock.patch.object(agent_exec, "emit") as emit:
                agent_exec.main([*arguments, "--project-root", str(self.root)])
            response = emit.call_args.args[0]
            if response.get("kind") == "error":
                raise agent_exec.ContractError(response["error"]["code"], response["error"]["message"])
            return response

        policy = runtime_test_home.policy("workspace-write", self.root)
        for patch in (mock.patch.object(agent_exec.native_codex, "inspect_capabilities",
                          return_value={"submit": {"goal": True}, "send": {"goal": True}, "diagnostic": None}),
                      mock.patch.object(self.agent_loop.AgentRuntime, "_call", bridge),
                      mock.patch.dict(os.environ, {"AGENT_FACTORY_EXECUTION_POLICY": json.dumps(policy)})):
            patch.start()
            self.addCleanup(patch.stop)

    def start(self, agent, plan, extra=()):
        workspace_file = self.root / (agent + "-workspace.json")
        workspace_file.write_text(json.dumps(plan))
        return self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.brief), "--task-mode", "work",
            "--work-agent", agent, "--codex", "/bin/true", "--work-isolation", "--workspace-file", str(workspace_file), *extra]))

    def test_scribe_never_runs_in_an_auto_merged_work_unit(self):
        (self.root / "docs").mkdir()
        code = dict(self.plans())["code-agent"]
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.start("scribe-code-agent", code, ["--work-profile", "scribe"])
        self.assertEqual(raised.exception.code, "scribe_draft_review_required")
        started = self.start("scribe-shared-agent", {"mode": "read-only"}, ["--work-profile", "scribe"])
        self.assertEqual(started["phase"], "work-running")

    def reconcile(self, agent, loop_id):
        return self.agent_loop.reconcile_loop(self.agent_loop.build_parser().parse_args([
            "reconcile", "--project-root", str(self.root), "--work-agent", agent, "--loop-id", loop_id]))

    def plans(self):
        code = {"mode": "code", "repositories": [{"path": str(self.root),
                "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('file.txt').exists()"]]}]}
        return (("read-only-agent", {"mode": "read-only"}), ("code-agent", code))

    def test_workspace_plan_dispatch_binds_to_the_exec_accepted_run(self):
        for agent, plan in self.plans():
            with self.subTest(mode=plan["mode"]):
                started = self.start(agent, plan)
                self.assertEqual(started["phase"], "work-running")
                state = self.agent_exec.safe_read_json(Path(started["statePath"]))
                workspace = self.agent_exec.safe_read_json(Path(state["execution"]["taskWorkspacePath"]))
                run = self.agent_exec.safe_read_json(
                    self.agent_exec.state_file(self.root, agent, started["latestWorkRunId"]))
                self.assertEqual(run["dispatchTuple"]["taskWorkspaceId"], workspace["id"])
                self.assertEqual(run["workingDirectory"], workspace["path"])
                # Read-only Work stays at the project root; code Work is relocated to its worktree.
                roots = run["dispatchTuple"]["executionPolicy"]["sandboxPolicy"]["writable_roots"]
                if plan["mode"] == "read-only":
                    self.assertEqual(workspace["path"], str(self.root))
                    self.assertEqual(roots, [str(self.root)])
                else:
                    self.assertNotEqual(workspace["path"], str(self.root))
                    self.assertEqual(roots, [workspace["path"]])
                self.assertEqual(run["executionPolicy"], run["dispatchTuple"]["executionPolicy"])

    def test_retried_start_returns_the_running_loop_without_stopping_it(self):
        agent, plan = self.plans()[1]
        workspace_file = self.root / (agent + "-workspace.json")
        workspace_file.write_text(json.dumps(plan))
        arguments = ["start", "--project-root", str(self.root), "--request-file", str(self.brief), "--task-mode", "work",
                     "--work-agent", agent, "--codex", "/bin/true", "--work-isolation", "--workspace-file", str(workspace_file)]
        with mock.patch.object(self.agent_loop, "launch_driver") as launch, mock.patch.object(self.agent_loop, "emit") as emit:
            self.assertEqual(self.agent_loop.main(arguments), 0)
        first = emit.call_args.args[0]
        launch.assert_called_once()
        path = Path(first["statePath"])
        runs = self.runs(agent)
        # The running driver holds its lock; a retried start must not launch a second one.
        with mock.patch.object(self.agent_loop, "launch_driver", side_effect=OSError("unit already exists")) as launch, \
                mock.patch.object(self.agent_loop, "emit") as emit, \
                self.agent_exec.file_lock(path.parent / ".driver.lock"):
            self.assertEqual(self.agent_loop.main(arguments), 0)
        launch.assert_not_called()
        self.assertEqual((emit.call_args.args[0]["loopId"], emit.call_args.args[0]["status"]), (first["loopId"], "active"))
        # Even when a relaunch is attempted and refused (same systemd unit name), the loop keeps running.
        with mock.patch.object(self.agent_loop, "launch_driver", side_effect=OSError("unit already exists")) as launch, \
                mock.patch.object(self.agent_loop, "emit") as emit:
            self.assertEqual(self.agent_loop.main(arguments), 0)
        launch.assert_called_once()
        state = self.agent_exec.safe_read_json(path)
        self.assertEqual((state["status"], state["controlPlaneError"]), ("active", None))
        self.assertEqual(self.runs(agent), runs)

    def start_with_lost_acknowledgement(self, agent, plan):
        """Accept the run in exec, then lose the loop's binding as a crash before saving would."""
        pending = {}
        original = self.agent_loop.complete_pending_dispatch

        def lose_ack(state, path, runtime):
            pending.update(state["pendingDispatch"])
            original(state, path, runtime)
            raise self.agent_exec.ContractError("child_runtime_failure", "fixture lost acknowledgement")

        with mock.patch.object(self.agent_loop, "complete_pending_dispatch", side_effect=lose_ack), \
                self.assertRaises(self.agent_exec.ContractError):
            self.start(agent, plan)
        state_path = next((self.agent_exec.agent_root(self.root) / agent / "loops").glob("*/state.json"))
        state = self.agent_exec.safe_read_json(state_path)
        run_id = state["latestWorkRunId"]
        state.update(pendingDispatch=pending, phase="work-dispatching", currentChild=None, latestWorkRunId=None)
        self.agent_exec.atomic_write_json(state_path, state)
        return state, run_id

    def runs(self, agent):
        return sorted((self.agent_exec.agent_root(self.root) / agent / "runs").iterdir())

    def test_accepted_dispatch_rebinds_after_lost_acknowledgement_without_resubmission(self):
        for agent, plan in self.plans():
            with self.subTest(mode=plan["mode"]):
                state, run_id = self.start_with_lost_acknowledgement(agent, plan)
                runs = self.runs(agent)
                bound = self.reconcile(agent, state["loopId"])
                self.assertEqual((bound["phase"], bound["latestWorkRunId"]), ("work-running", run_id))
                self.assertEqual(self.runs(agent), runs)

    def test_run_accepted_before_the_tuple_field_rebinds_only_to_its_recorded_workspace(self):
        agent, plan = self.plans()[0]
        state, run_id = self.start_with_lost_acknowledgement(agent, plan)
        run_path = self.agent_exec.state_file(self.root, agent, run_id)
        legacy = self.agent_exec.safe_read_json(run_path)
        del legacy["dispatchTuple"]["taskWorkspaceId"]
        foreign = {**legacy, "taskWorkspace": {**legacy["taskWorkspace"], "id": "0" * 24}}
        self.agent_exec.atomic_write_json(run_path, foreign)
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.reconcile(agent, state["loopId"])
        self.assertEqual(raised.exception.code, "dispatch_binding_invalid")
        self.agent_exec.atomic_write_json(run_path, legacy)
        bound = self.reconcile(agent, state["loopId"])
        self.assertEqual((bound["phase"], bound["latestWorkRunId"]), ("work-running", run_id))
        self.assertEqual(self.runs(agent), [run_path.parent])
