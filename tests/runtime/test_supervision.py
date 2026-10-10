"""Supervision judges unfinished loops and records due reports without changing them."""
import runtime_test_home  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import operation_records as cli
from storage import paths as runtime_paths
from tasks import supervision

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
SETTINGS = dict(supervision.DEFAULTS)


def ago(minutes):
    return supervision.stamp(NOW - timedelta(minutes=minutes))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class JudgeTests(unittest.TestCase):
    def test_four_verdicts(self):
        running = {"status": {"state": "running"}, "phase": "work-running", "startedAt": ago(90),
                   "lastActivityAt": ago(1)}
        cases = {
            "normal": running,
            "delayed": {**running, "lastActivityAt": ago(25)},
            "stuck": {**running, "recovery": {"code": "lock_busy", "attempts": 3, "observedAt": ago(2)}},
            "decision-needed": {**running, "status": {"state": "waiting-decision"}, "waitingSince": ago(5),
                                "decision": {"kind": "approval", "target": "plugin/scripts/loop.py"}},
        }
        for expected, evidence in cases.items():
            with self.subTest(expected):
                self.assertEqual(supervision.judge(evidence, SETTINGS, NOW)["verdict"], expected)
        self.assertEqual(supervision.judge({**running, "lastActivityAt": ago(61)}, SETTINGS, NOW)["reasons"],
                         ["no-progress"])
        # A repetition that stopped recurring long ago is history, not a current stall.
        old = {**running, "recovery": {"code": "lock_busy", "attempts": 9, "observedAt": ago(30)}}
        self.assertEqual(supervision.judge(old, SETTINGS, NOW)["verdict"], "normal")


class TickTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "project"
        root.mkdir()
        self.agents = Path(runtime_paths.resolve(root, create=True)["agentsRoot"])
        for agent, status in (("work-run", "running"), ("work-wait", "needs-human-decision")):
            write_json(self.agents / agent / "runs" / "run-one" / "state.json",
                       {"agentId": agent, "runId": "run-one", "role": "work", "status": status,
                        "updatedAt": ago(3), "taskBinding": {"workflowId": "flow", "taskId": "task"}})
        base = {"execution": {"taskMode": "work"}, "createdAt": ago(40), "updatedAt": ago(3),
                "workflow": {"id": "flow", "title": "Goal", "index": 0, "tasks": [
                    {"id": "task", "title": "Long task", "workRunId": "run-one", "workStatus": "running"}]}}
        write_json(self.agents / "work-run" / "loops" / "loop-run" / "state.json",
                   {**base, "loopId": "loop-run", "status": "active", "phase": "work-running",
                    "workAgentId": "work-run"})
        write_json(self.agents / "work-wait" / "loops" / "loop-wait" / "state.json",
                   {**base, "loopId": "loop-wait", "status": "needs-human-decision", "phase": "work-running",
                    "workAgentId": "work-wait", "pendingDecisionId": "decision-one",
                    "controlPlaneError": {"code": "needs-human-decision"},
                    "decisions": {"decision-one": {"id": "decision-one", "status": "pending", "kind": "approval",
                                                   "createdAt": ago(4)}}})
        self.before = {path: path.read_bytes() for path in self.agents.rglob("state.json")}

    def tick(self, now):
        return supervision.tick(self.agents, settings=SETTINGS, now=now, failure_class=cli.failure_class)

    def test_alert_at_once_periodic_by_interval_and_records_untouched(self):
        first = self.tick(NOW)
        verdicts = {item["loopId"]: item["verdict"] for item in first["verdicts"]}
        self.assertEqual(verdicts, {"loop-run": "normal", "loop-wait": "decision-needed"})
        self.assertEqual([item["loopId"] for item in first["alerts"]], ["loop-wait"])
        self.assertIn("[decision-needed] Long task", first["alerts"][0]["line"])
        self.assertEqual(len(first["reports"]), 2)
        # Within the interval nothing repeats, including the already reported alert.
        self.assertEqual(self.tick(NOW + timedelta(minutes=5))["reports"], [])
        # Active loops repeat every interval; a stopped loop's alert is not repeated.
        later = self.tick(NOW + timedelta(minutes=10))
        self.assertEqual([(item["loopId"], item["trigger"]) for item in later["reports"]], [("loop-run", "periodic")])
        log = self.agents / "work-wait" / "loops" / "loop-wait" / supervision.REPORT_LOG
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual({path: path.read_bytes() for path in self.agents.rglob("state.json")}, self.before)


if __name__ == "__main__":
    unittest.main()
