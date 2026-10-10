"""Common operations records are read-only projections over existing runtime files."""
import runtime_test_home  # noqa: F401
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import operation_records as cli
from storage import paths as runtime_paths
from tasks import operation_records as records
from tasks.operation_records import classify

HASH = "a" * 64


class ClassifyTests(unittest.TestCase):
    def test_core_states_stay_distinct(self):
        ended = {"current": True, "assigned": True, "requestHash": HASH, "workRunId": "run-work",
                 "workRun": {"status": "completed"}, "workReceipt": {"outcome": "completed", "requestHash": HASH}}
        passed = {**ended, "loopStatus": "completed", "workStatus": "completed", "verificationStatus": "completed",
                  "verificationRunId": "run-check", "verificationRun": {"status": "completed"},
                  "verificationReceipt": {"decision": "pass", "verifiedWorkRunId": "run-work"}}
        stopped = {**ended, "route": "work", "workStatus": "blocked", "workRun": {"status": "needs-human-decision"}}
        cases = {
            "execution-ended": {**ended, "route": "work", "loopStatus": "cancelled", "workStatus": "failed"},
            "work-completed": {**ended, "route": "work", "loopStatus": "completed", "workStatus": "completed"},
            "check-passed": passed,
            "waiting-decision": {**stopped, "loopStatus": "needs-human-decision", "pendingDecision": True},
            "blocked": {**stopped, "loopStatus": "runtime-error", "failureClass": "environment"},
            "unknown": {**ended, "route": "work", "loopStatus": "active", "workStatus": "running", "workRun": None},
        }
        for expected, evidence in cases.items():
            with self.subTest(expected):
                self.assertEqual(classify(evidence)["state"], expected)
        # A Verification receipt bound to another Work run never passes the check.
        foreign = classify({**passed, "verificationReceipt": {"decision": "pass", "verifiedWorkRunId": "run-old"}})
        self.assertEqual((foreign["state"], foreign["check"]), ("work-completed", "unknown"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class QueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        self.agents = Path(runtime_paths.resolve(self.root, create=True)["agentsRoot"])
        write_json(self.agents / "main-one" / "runs" / "run-main" / "state.json",
                   {"agentId": "main-one", "runId": "run-main", "role": "main", "status": "running"})
        run = self.agents / "work-one" / "runs" / "run-work"
        write_json(run / "state.json", {"agentId": "work-one", "runId": "run-work", "role": "work",
                                        "status": "needs-human-decision", "provider": "codex",
                                        "executionOptions": {"model": "model-a"}, "updatedAt": "2026-10-09T11:00:00Z",
                                        "taskBinding": {"workflowId": "flow", "taskId": "task-one"}})
        (run / "request.md").write_text("Fix the sudo password input dialog", encoding="utf-8")
        (run / "result.md").write_text("Asked which kanban layout to keep", encoding="utf-8")
        write_json(self.agents / "work-one" / "loops" / "loop-one" / "state.json", {
            "loopId": "loop-one", "status": "needs-human-decision", "workAgentId": "work-one",
            "parentStatePath": str(self.agents / "main-one" / "runs" / "run-main" / "state.json"),
            "execution": {"taskMode": "work"}, "pendingDecisionId": "decision-one", "updatedAt": "2026-10-09T11:00:00Z",
            "controlPlaneError": {"code": "needs-human-decision"},
            "decisions": {"decision-one": {"id": "decision-one", "status": "pending", "kind": "approval",
                                           "target": "plugin/scripts/loop.py", "reason": "Shared file",
                                           "impact": None, "alternatives": ["wait"],
                                           "agentId": "work-one", "runId": "run-work",
                                           "taskBinding": {"taskId": "task-one"}}},
            "workflow": {"id": "flow", "index": 0, "tasks": [
                {"id": "task-one", "requestHash": HASH, "requestRevision": 2, "workAgentId": "work-one",
                 "title": "Kanban column", "requestPath": str(run / "request.md"),
                 "workRunId": "run-work",
                 "workStatus": "blocked", "verificationStatus": "pending"}]}})

    def test_task_links_session_decision_and_observation(self):
        result = records.query(self.agents, task_id="task-one", failure_class=cli.failure_class)
        [task] = result["tasks"]
        self.assertEqual(task["status"]["state"], "waiting-decision")
        self.assertEqual(task["sessions"]["conductor"], {"agentId": "main-one", "runId": "run-main"})
        self.assertEqual(task["sessions"]["work"], {"agentId": "work-one", "runId": "run-work", "model": "model-a"})
        self.assertEqual([(item["id"], item["decided"]) for item in result["decisions"]], [("decision-one", False)])
        self.assertEqual({key: result["decisions"][0][key] for key in ("target", "reason", "impact", "alternatives")},
                         {"target": "plugin/scripts/loop.py", "reason": "Shared file", "impact": None,
                          "alternatives": ["wait"]})
        self.assertEqual(task["revision"]["number"], 2)
        self.assertEqual(task["observationIds"], ["observation-run-work"])
        self.assertEqual({session["id"]: session["running"] for session in result["sessions"]},
                         {"main-one": True, "work-one": False})

    def test_search_filters_records_then_matches_words_with_sources(self):
        lesson = {"id": "lesson-one", "title": "Sudo prompt hung", "scope": "extension", "path": None,
                  "occurrences": [{"source": str(self.agents / "work-one" / "runs" / "run-work"),
                                   "recordedAt": "2026-10-09T12:00:00+00:00"}]}
        result = records.search(self.agents, text="SUDO", lessons=[lesson], failure_class=cli.failure_class)
        self.assertEqual((result["mode"], sorted((hit["kind"], hit["id"]) for hit in result["hits"])),
                         ("lexical", [("lesson", "lesson-one"), ("task", "task-one")]))
        task = next(hit for hit in result["hits"] if hit["kind"] == "task")
        self.assertEqual(task["path"], str(self.agents / "work-one" / "runs" / "run-work" / "request.md"))
        self.assertIn("sudo password", task["excerpt"])
        [observation] = records.search(self.agents, text="kanban layout", kinds=("observation",))["hits"]
        self.assertTrue(observation["path"].endswith("result.md"))
        self.assertEqual(records.search(self.agents, text="kanban", model="other")["hits"], [])
        self.assertEqual(records.search(self.agents, role="work", model="model-a", since="2026-10-09",
                                        until="2026-10-09T23:00:00Z", kinds=("task",))["total"], 1)
        self.assertEqual(records.search(self.agents, since="2026-10-10", kinds=("task", "lesson"),
                                        lessons=[lesson])["hits"], [])

    def test_cli_query_is_read_only(self):
        before = {str(path): path.stat().st_mtime_ns for path in self.agents.rglob("*")}
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["query", "--project-root", str(self.root), "--session-id", "work-one"])
        [row] = json.loads(stream.getvalue())["tasks"]
        self.assertEqual((code, row["work"], row["state"]), (0, "work-one", "waiting-decision"))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["search", "--project-root", str(self.root), "--text", "sudo", "--kind", "task"])
        self.assertEqual((code, [hit["id"] for hit in json.loads(stream.getvalue())["hits"]]), (0, ["task-one"]))
        self.assertEqual({str(path): path.stat().st_mtime_ns for path in self.agents.rglob("*")}, before)


if __name__ == "__main__":
    unittest.main()
