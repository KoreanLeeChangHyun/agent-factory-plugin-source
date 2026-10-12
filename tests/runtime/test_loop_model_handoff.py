from __future__ import annotations

import runtime_test_home  # noqa: F401  Isolate all runtime subprocesses from the real home.
import domain_fixtures  # Workers are placed in a domain before dispatch.

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from test_agent_loop import FakeRuntime, load_modules


class CancellingRuntime(FakeRuntime):
    """FakeRuntime plus the exec `cancel` call the handoff uses to stop the prior writer."""

    def __init__(self, root, agent_exec):
        super().__init__(root, agent_exec)
        self.calls = []

    def call(self, arguments, *, idempotent=False):
        self.calls.append(arguments)
        if arguments[0] == "cancel":
            run = self.runs[(arguments[2], arguments[4])]
            if run["status"] in {"completed", "failed", "cancelled", "needs-human-decision"}:
                raise self.agent_exec.ContractError("run_terminal", "run is already terminal")
            run["status"] = "cancelled"
            return {"kind": "ack", "status": "cancelling"}
        raise AssertionError(arguments)


def detected(*models):
    observed = datetime.now(timezone.utc).isoformat()
    return {"schemaVersion": 1, "checkedAt": observed, "availability": "provider-catalog-observed",
            "candidates": [{"id": model, "provider": "claude" if model.startswith("claude-") else "codex",
                            "detail": {"fetchedAt": observed}} for model in models]}


class LoopModelAndHandoffTests(unittest.TestCase):
    def setUp(self):
        self.agent_exec, self.agent_loop = load_modules()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request = self.root / "request.md"
        self.request.write_text("bounded work\n", encoding="utf-8")
        self.runtime = CancellingRuntime(self.root, self.agent_exec)
        from test_model_affinity import TABLE
        from tasks import model_affinity
        runtime_root = Path(self.agent_exec.runtime_paths.resolve(self.root, create=True)["runtimeRoot"])
        self.table = runtime_root / model_affinity.FILE
        self.table.write_text(json.dumps(TABLE), encoding="utf-8")
        patch = mock.patch.object(self.agent_loop, "AgentRuntime", return_value=self.runtime)
        patch.start()
        self.addCleanup(patch.stop)
        domain_fixtures.place_workers(self.root)

    def start(self, task_type=None, extra=(), catalog=None):
        from test_task_binding import TaskBindingTests
        arguments = ["start", "--project-root", str(self.root), "--request-file", str(self.request),
                     "--work-agent", "work-agent", "--verification-agent", "verification-agent",
                     "--receipt-recovery", "manual", "--max-revisions", "1", "--codex", "/bin/true", *extra]
        if task_type:
            allocation = self.root / "allocation.json"
            allocation.write_text(json.dumps({**TaskBindingTests.allocation(), "taskType": task_type}))
            arguments += ["--allocation-file", str(allocation)]
        if catalog is not None:
            path = self.root / "catalog.json"
            path.write_text(json.dumps(catalog))
            arguments += ["--model-catalog-file", str(path)]
        return self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args(arguments))

    def state(self, result):
        return self.agent_exec.safe_read_json(Path(result["statePath"]))

    def handoff(self, result, *extra, agent="work-claude", model="claude-opus-5-5", actor="human"):
        return self.agent_loop.handoff_loop(self.agent_loop.build_parser().parse_args([
            "handoff", "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", result["loopId"],
            "--to-agent", agent, "--to-model", model, "--actor", actor, "--authorization-reference", "human-message-1",
            "--decision-evidence", "Move this task to Claude", "--reason", "Codex stalled", *extra]))

    def reconcile(self, result):
        return self.agent_loop.reconcile_loop(self.agent_loop.build_parser().parse_args([
            "reconcile", "--project-root", str(self.root), "--work-agent", "work-agent", "--loop-id", result["loopId"]]))

    def test_new_allocation_records_and_applies_the_detected_recommendation(self):
        started = self.start("small-change", catalog=detected("gpt-6.1-sol", "gpt-6-luna", "claude-opus-5-5"))
        state = self.state(started)
        record = state["execution"]["taskBinding"]["allocation"]["modelRecommendation"]
        self.assertEqual((record["status"], record["selected"], record["suggestedProfile"]), ("applied", "gpt-6-luna", "workLight"))
        self.assertEqual(started["modelRecommendation"], record)
        self.assertEqual(state["execution"]["agentModels"]["work"], {"model": "gpt-6-luna"})
        self.assertEqual(self.runtime.dispatches[0]["execution"]["agentModels"]["work"]["model"], "gpt-6-luna")
        # The Work run receives the same accepted task list, which still validates.
        stored = self.agent_exec.safe_read_json(Path(state["execution"]["taskListPath"]))
        self.assertEqual(stored["tasks"][0]["allocation"]["modelRecommendation"], record)

    def test_specified_work_model_is_kept_and_undetected_models_are_never_chosen(self):
        started = self.start("design-diagnosis", ["--work-model", "gpt-6-sol"], detected("gpt-6-luna", "claude-opus-5-5"))
        state = self.state(started)
        record = state["execution"]["taskBinding"]["allocation"]["modelRecommendation"]
        self.assertEqual((record["status"], record["selected"], record["recommended"]["model"]),
                         ("human-specified", "gpt-6-sol", "claude-opus-5-5"))
        self.assertEqual(state["execution"]["agentModels"]["work"], {"model": "gpt-6-sol"})
        self.assertIn("gpt-6-astra", record["undetectedPreferences"])
        self.request.write_text("other bounded work\n", encoding="utf-8")
        # The first loop still holds both of its workers, so this task names its own.
        unknown = self.state(self.start("design-diagnosis", ["--work-agent", "work-other", "--verification-agent", "verify"], None))
        record = unknown["execution"]["taskBinding"]["allocation"]["modelRecommendation"]
        self.assertEqual((record["status"], record["selected"]), ("recheck-required", None))
        self.assertEqual(unknown["execution"]["agentModels"]["work"], {})

    def test_without_the_humans_table_nothing_is_recommended_or_created(self):
        self.table.unlink()
        state = self.state(self.start("small-change", catalog=detected("gpt-6-luna")))
        record = state["execution"]["taskBinding"]["allocation"]["modelRecommendation"]
        self.assertEqual((record["status"], record["selected"]), ("no-affinity-table", None))
        self.assertEqual(state["execution"]["agentModels"]["work"], {})
        self.assertFalse(self.table.exists())

    def test_submitted_recommendation_is_rejected(self):
        from test_task_binding import TaskBindingTests
        allocation = self.root / "allocation.json"
        allocation.write_text(json.dumps({**TaskBindingTests.allocation(), "taskType": "research",
                                          "modelRecommendation": {"status": "applied"}}))
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([
                "start", "--project-root", str(self.root), "--request-file", str(self.request), "--work-agent", "work-agent",
                "--task-mode", "work", "--allocation-file", str(allocation), "--codex", "/bin/true"]))
        self.assertEqual(raised.exception.code, "task_allocation_invalid")

    def test_handoff_stops_the_running_writer_and_continues_in_a_linked_new_provider_session(self):
        started = self.start()
        old_run = started["latestWorkRunId"]
        moved = self.handoff(started)
        self.assertEqual(self.runtime.runs[("work-agent", old_run)]["status"], "cancelled")
        handoff = moved["handoff"]
        self.assertEqual((handoff["fromAgentId"], handoff["fromRunId"], handoff["fromProvider"]), ("work-agent", old_run, "codex"))
        self.assertEqual((handoff["toAgentId"], handoff["toProvider"], handoff["toRunId"]),
                         ("work-claude", "claude", moved["latestWorkRunId"]))
        dispatched = self.runtime.dispatches[-1]
        self.assertEqual((dispatched["agent_id"], dispatched["operation"]), ("work-claude", "submit"))
        bundle = Path(dispatched["request_file"]).read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(bundle.encode()).hexdigest(), handoff["bundleSha256"])
        for expected in ("## Original request", "bounded work", "## Progress records", "## Changed paths",
                         "## Remaining work", "work-agent", old_run, "Codex stalled"):
            self.assertIn(expected, bundle)
        state = self.state(moved)
        self.assertEqual(state["workflow"]["tasks"][0]["workAgentId"], "work-claude")
        self.assertEqual(state["execution"]["agentModels"]["work"], {"model": "claude-opus-5-5"})
        self.assertEqual(moved["handoffs"], [handoff])
        # The new session continues the same task, so it joins the prior worker's domain.
        from tasks import domains
        stored = domains.read(Path(self.agent_exec.runtime_paths.resolve(self.root)["runtimeRoot"]))["assignments"]
        self.assertEqual((moved["domainLink"]["basis"], stored["work-claude"]["domainId"]), ("prior-worker", stored["work-agent"]["domainId"]))
        # The new session's result continues the same graph: its run is the one Verification checks.
        self.runtime.complete_work("work-claude", moved["latestWorkRunId"])
        verifying = self.reconcile(moved)
        self.assertEqual(self.runtime.dispatches[-1]["verified_work_run_id"], moved["latestWorkRunId"])
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "pass")
        self.assertEqual(self.reconcile(verifying)["status"], "completed")

    def test_stopped_loop_after_failed_verification_is_handed_off_with_open_findings(self):
        started = self.start()
        self.runtime.complete_work("work-agent", started["latestWorkRunId"])
        verifying = self.reconcile(started)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "fail")
        revised = self.reconcile(verifying)
        self.runtime.complete_work("work-agent", revised["latestWorkRunId"], ["finding-1"])
        verifying = self.reconcile(revised)
        self.runtime.complete_verification("verification-agent", verifying["latestVerificationRunId"], "fail")
        stopped = self.reconcile(verifying)
        self.assertEqual(stopped["status"], "needs-human-decision")
        moved = self.handoff(stopped)
        self.assertEqual((moved["status"], moved["handoff"]["fromRunId"]), ("active", revised["latestWorkRunId"]))
        bundle = Path(moved["handoff"]["bundlePath"]).read_text(encoding="utf-8")
        self.assertIn("finding-1", bundle)
        self.assertIn("changed.txt", bundle)
        self.assertNotIn("cancel", [call[0] for call in self.runtime.calls])

    def test_handoff_requires_human_authority_and_a_new_session(self):
        started = self.start()
        for options, code in ((dict(actor="main"), "loop_handoff_unauthorized"),
                              (dict(agent="work-agent"), "loop_handoff_session_invalid"),
                              (dict(agent="verification-agent"), "loop_handoff_session_invalid")):
            with self.assertRaises(self.agent_exec.ContractError) as raised:
                self.handoff(started, **options)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.runtime.calls, [])
        self.assertNotIn("handoffs", self.state(started))

    def test_handoff_without_a_domain_for_the_new_session_is_refused_before_stopping_the_writer(self):
        from tasks import domains
        started = self.start()
        runtime_root = Path(self.agent_exec.runtime_paths.resolve(self.root)["runtimeRoot"])
        legacy = domains.read(runtime_root)
        del legacy["assignments"]["work-agent"]  # A worker recorded before membership was required.
        (runtime_root / domains.FILE).write_text(json.dumps(legacy))
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.handoff(started)
        self.assertEqual(raised.exception.code, "domain_membership_required")
        self.assertEqual(self.runtime.calls, [], "The running writer is not cancelled")
        self.assertNotIn("handoffs", self.state(started))
        self.assertNotIn("work-claude", domains.read(runtime_root)["assignments"])


if __name__ == "__main__":
    unittest.main()
