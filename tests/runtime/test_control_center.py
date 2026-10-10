"""Conductor projections consume records without executing or accepting work."""
import copy
import unittest

from tasks.control_center import snapshot


class ControlCenterTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "loopId": "loop-one", "status": "active", "phase": "work-running",
            "stateRevision": 2, "execution": {"taskMode": "work"},
            "workAgentId": "worker", "parentStatePath": "/runtime/agents/main/runs/main-run/state.json",
            "originalRequestPath": "/runtime/goal.md", "originalRequestHash": "abc",
            "workflow": {"id": "flow", "title": "Goal", "index": 0, "tasks": [
                {"id": "first", "title": "Implement", "description": "Requirement",
                 "completionCriteria": "Checks pass", "workRunId": "run-one", "workStatus": "running"},
                {"id": "second", "title": "Consume", "workAgentId": "other", "workStatus": "pending",
                 "allocation": {"dependencies": [{"taskId": "first", "source": "Human", "confirmed": True}],
                                "profile": {"id": "work"}}}]}}
        self.run = {"agentId": "worker", "runId": "run-one", "role": "work", "status": "running",
                    "taskBinding": {"workflowId": "flow", "taskId": "first"},
                    "parentAgentId": "main", "parentRunId": "main-run",
                    "resultPath": "/runtime/result.md", "receiptPath": "/runtime/receipt.json"}

    def view(self):
        return snapshot(self.state, lambda agent, run: self.run)

    def test_goal_assignment_dependency_running_and_processed_result(self):
        before = copy.deepcopy((self.state, self.run))
        view = self.view()
        first, second = view["tasks"]
        self.assertEqual(view["goal"]["workflowId"], "flow")
        self.assertEqual(first["assignedBy"]["agentId"], "main")
        self.assertEqual(second["dependencies"][0]["taskId"], "first")
        self.assertEqual(second["stages"]["work"]["disposition"], "planned")
        stage = first["stages"]["work"]
        self.assertEqual(stage["runStatus"], "running")
        self.assertIsNone(stage["resultPath"])
        self.assertEqual(stage["disposition"], "dispatched")
        self.run["status"] = "completed"
        self.assertEqual(self.view()["tasks"][0]["stages"]["work"]["disposition"], "dispatched")
        self.state["workflow"]["tasks"][0]["workStatus"] = "completed"
        stage = self.view()["tasks"][0]["stages"]["work"]
        self.assertEqual(stage["disposition"], "processed-completed")
        self.assertEqual(stage["resultPath"], self.run["resultPath"])
        self.assertEqual(stage["receiptPath"], self.run["receiptPath"])
        view["tasks"][1]["allocation"]["dependencies"].clear()
        self.assertEqual(self.state["workflow"]["tasks"][1], before[0]["workflow"]["tasks"][1])

    def test_errors_waiting_cancellation_and_missing_run_are_observations(self):
        for status in ("accepted", "queued", "needs-human-decision", "failed", "runtime-error", "cancelling", "cancelled"):
            with self.subTest(status=status):
                self.run.update(status=status, decisionKind="clarification", decisionScope="external-prerequisite",
                                error={"code": "fixture"}, cancelRequested=status == "cancelling")
                stage = self.view()["tasks"][0]["stages"]["work"]
                self.assertEqual(stage["runStatus"], status)
                self.assertEqual(stage["disposition"], "dispatched")
                self.assertEqual(stage["error"]["code"], "fixture")
                self.assertEqual(stage["decisionKind"], "clarification")
        def missing(agent, run):
            raise FileNotFoundError("removed history")
        self.state.update(status="cancelled", phase="cancelled", stopPending=True)
        self.state["workflow"]["tasks"][0]["workStatus"] = "completed"
        view = snapshot(self.state, missing)
        self.assertEqual(view["progress"]["status"], "cancelled")
        self.assertTrue(view["stopPending"])
        self.assertEqual(view["tasks"][0]["stages"]["work"]["observation"], "unavailable")
        self.assertEqual(view["tasks"][0]["stages"]["work"]["disposition"], "dispatched")

    def test_mismatched_run_cannot_supply_result_or_completion(self):
        self.run["status"] = "completed"
        self.state["workflow"]["tasks"][0]["workStatus"] = "completed"
        for field, value in (("taskBinding", {"workflowId": "flow", "taskId": "second"}),
                             ("parentRunId", "other-main-run"), ("role", "verification")):
            with self.subTest(field=field):
                saved = copy.deepcopy(self.run)
                self.run[field] = value
                stage = self.view()["tasks"][0]["stages"]["work"]
                self.assertEqual(stage["error"]["code"], "control_center_run_binding")
                self.assertEqual(stage["observation"], "unavailable")
                self.assertIsNone(stage["resultPath"])
                self.run = saved

    def test_plan_completion_skip_decision_retry_and_legacy(self):
        self.state["execution"]["taskMode"] = "plan-work"
        self.state["workflow"]["tasks"][0]["workStatus"] = "completed"
        self.run["status"] = "completed"
        view = self.view()
        self.assertEqual(view["deliveryKind"], "plan")
        self.assertEqual(view["tasks"][0]["stages"]["work"]["disposition"], "plan-completed")
        self.assertEqual(view["tasks"][0]["stages"]["verification"]["disposition"], "not-requested")
        self.state["execution"]["taskMode"] = "work-verification"
        self.state.update(humanSkip={"source": "Human"}, pendingDecisionId="choice",
                          decisions={"choice": {"status": "pending", "scope": "task-execution"}},
                          pendingDispatch={"dispatchId": "retry"}, integrationWait={"reason": "busy"},
                          lastVerificationDecision="fail", pendingFindingIds=["finding-one"], revisionCount=1)
        view = self.view()
        self.assertEqual(view["tasks"][0]["stages"]["verification"]["disposition"], "human-skipped")
        self.assertEqual(view["pendingDispatch"]["dispatchId"], "retry")
        self.assertEqual(view["pendingDecision"]["status"], "pending")
        self.assertEqual(view["integrationWait"]["reason"], "busy")
        self.assertEqual(view["lastVerificationDecision"], "fail")
        self.assertEqual(view["pendingFindingIds"], ["finding-one"])
        self.assertEqual(view["revisionCount"], 1)
        self.state.pop("workflow")
        self.state.pop("parentStatePath")
        self.assertEqual(self.view()["tasks"], [])
        self.assertIsNone(self.view()["conductor"])


if __name__ == "__main__":
    unittest.main()
