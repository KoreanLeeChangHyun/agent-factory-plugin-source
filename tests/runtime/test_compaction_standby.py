"""Main conversations switch to a compacted fork instead of compacting inline."""
import runtime_test_home  # Isolate all runtime subprocesses from the real home.
import json
import os
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from native_fixtures import runtime, native_fixture
from execution import handoff
from adapters.codex import transport


def usage(session_id, used, turn="turn-x"):
    return {"schemaVersion": 1, "availability": "observed", "usedTokens": used, "contextWindowTokens": 100,
            "usedPercent": used, "sessionId": session_id, "turnId": turn, "observedAt": runtime.now(),
            "provider": "codex", "estimated": True, "observationPoint": "last-inference-boundary"}


def dead_identity():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    identity = runtime.process_identity(process.pid)
    process.kill()
    process.wait()
    return identity


class StandbyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.launches = []
        patches = [
            mock.patch.object(runtime, "spawn_worker", return_value=123),
            mock.patch.object(runtime, "emit"),
            mock.patch.object(handoff, "launch_standby", side_effect=self.launch),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.directory.cleanup)

    def launch(self, runtime_, root, agent, standby_id):
        self.launches.append(standby_id)
        return runtime.process_identity(os.getpid())

    def send(self, message, first=False):
        before = {value["runId"] for value in runtime.iter_run_states(self.root, "main-agent")}
        arguments = ["submit", "--role", "main"] if first else ["send"]
        runtime.submit(runtime.parse_args([*arguments[:1], "--project-root", str(self.root), "--agent", "main-agent",
                                           *arguments[1:], "--message", message, "--codex", sys.executable]), first)
        [run] = [value for value in runtime.iter_run_states(self.root, "main-agent") if value["runId"] not in before]
        if first:
            path = runtime.session_file(self.root, "main-agent")
            runtime.update_json(path, path.parent / ".session-state.lock",
                                lambda value: value.update(sessionId="thread-a", backend="app-server", provider="codex"))
        # The worker resumes the persisted session and binds the run to it when the thread starts.
        target = self.session()["sessionId"]
        state = Path(run["statePath"])
        runtime.update_json(state, state.parent / ".state.lock", lambda value: value.update(sessionId=target))
        return runtime.safe_read_json(state)

    def finish(self, run, used, status="completed"):
        path = Path(run["statePath"])
        runtime.atomic_write(Path(run["resultPath"]), f"answer for {run['runId']}".encode())
        runtime.update_json(path, path.parent / ".state.lock",
                            lambda value: value.update(contextUsage=usage(value["sessionId"], used, "turn-" + value["runId"])))
        runtime.mark_terminal(path, status)
        return runtime.safe_read_json(path)

    def session(self):
        return runtime.load_session(self.root, "main-agent")

    def prepare(self, fork="thread-b", error=None):
        record = self.session()["standby"]
        result = {"threadId": fork, "usage": {"usedTokens": 30, "contextWindowTokens": 100}}
        with mock.patch.object(transport, "compact_fork_process", side_effect=error, return_value=result) as compact:
            code = handoff.prepare_standby(runtime, runtime.parse_args([
                "_standby", "--project-root", str(self.root), "--agent", "main-agent", "--standby-id", record["id"]]))
        return code, compact

    def test_full_thread_is_not_compacted_inline_after_preparation(self):
        first = self.finish(self.send("first", first=True), 94)
        record = self.session()["standby"]
        # Preparation forks through the completed turn; the current session keeps answering.
        self.assertEqual((record["status"], record["sourceSessionId"], record["sourceTurnId"]),
                         ("preparing", "thread-a", "turn-" + first["runId"]))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.session()["sessionId"], "thread-a")
        code, compact = self.prepare()
        self.assertEqual(code, 0)
        compact.assert_called_once()
        self.assertEqual(compact.call_args.args[2:], ("thread-a", "turn-" + first["runId"]))
        self.assertEqual(self.session()["standby"]["status"], "ready")
        second = self.send("second")
        # Before the fix the next Human message went to the full thread-a and waited for its compaction.
        self.assertEqual(second["sessionId"], "thread-b")
        self.assertNotIn("standbyContinuity", second)
        session = self.session()
        self.assertNotIn("standby", session)
        self.assertEqual([item["status"] for item in session["standbySwitches"]], ["adopted"])
        self.assertTrue(Path(second["requestPath"]).read_bytes().startswith(b"second"))

    def test_turns_answered_during_preparation_follow_as_originals_until_delivered(self):
        self.finish(self.send("first", first=True), 80)
        busy = self.send("second")  # Arrives while the fork is compacting: no wait, current session answers.
        self.assertEqual(busy["sessionId"], "thread-a")
        self.finish(busy, 85)
        self.assertEqual(len(self.launches), 1)  # A live preparer is not duplicated.
        self.prepare()
        third = self.send("third")
        self.assertEqual(third["sessionId"], "thread-b")
        continuity = third["standbyContinuity"]
        self.assertEqual([item["runId"] for item in continuity["deltaRuns"]], [busy["runId"]])
        self.assertEqual(continuity["deltaRuns"][0]["requestPath"], busy["requestPath"])
        self.assertEqual(continuity["deltaRuns"][0]["resultPath"], busy["resultPath"])
        self.assertEqual(runtime.safe_read_json(Path(third["statePath"]))["standbyContinuity"], continuity)
        # A failed first turn on the fork does not lose the references; a completed one ends delivery.
        self.finish(third, 35, status="failed")
        fourth = self.send("fourth")
        self.assertEqual(fourth["standbyContinuity"]["deltaRuns"], continuity["deltaRuns"])
        self.finish(fourth, 36)
        fifth = self.send("fifth")
        self.assertNotIn("standbyContinuity", fifth)
        self.assertEqual(fifth["sessionId"], "thread-b")

    def test_unready_failed_or_dead_preparation_keeps_current_session(self):
        self.finish(self.send("first", first=True), 75)
        waiting = self.send("waiting")
        self.assertEqual(waiting["sessionId"], "thread-a")
        self.finish(waiting, 77)
        code, _ = self.prepare(error=RuntimeError("provider failed"))
        self.assertEqual(code, 1)
        self.assertEqual(self.session()["standby"]["status"], "failed")
        after_failure = self.send("after failure")
        self.assertEqual(after_failure["sessionId"], "thread-a")
        self.finish(after_failure, 78)  # A failed record is retried at the next qualifying turn.
        self.assertEqual(len(self.launches), 2)
        path = runtime.session_file(self.root, "main-agent")
        dead = dead_identity()
        runtime.update_json(path, path.parent / ".session-state.lock",
                            lambda value: value["standby"].update(preparer=dead, scheduler=dead))
        restarted = self.send("restart")
        self.assertEqual(restarted["sessionId"], "thread-a")
        self.finish(restarted, 79)
        self.assertEqual(len(self.launches), 3)
        self.assertEqual([item["status"] for item in self.session()["standbyHistory"]], ["superseded", "superseded"])

    def test_late_ready_for_a_replaced_session_is_recorded_but_never_adopted(self):
        self.finish(self.send("first", first=True), 90)
        path = runtime.session_file(self.root, "main-agent")
        replace = lambda: runtime.update_json(path, path.parent / ".session-state.lock",
                                              lambda value: value.update(sessionId="thread-new"))
        replace()  # Replaced before the preparer starts: no fork is made.
        _, compact = self.prepare()
        compact.assert_not_called()
        self.assertNotIn("standby", self.session())
        self.assertEqual(self.session()["standbyHistory"][-1]["status"], "superseded")
        self.assertEqual(self.finish(self.send("next"), 90)["sessionId"], "thread-new")
        record = self.session()["standby"]
        result = {"threadId": "thread-late", "usage": None}
        def compacting(*arguments):
            runtime.update_json(path, path.parent / ".session-state.lock", lambda value: value.update(sessionId="thread-reset"))
            return result
        with mock.patch.object(transport, "compact_fork_process", side_effect=compacting):
            handoff.prepare_standby(runtime, runtime.parse_args([
                "_standby", "--project-root", str(self.root), "--agent", "main-agent", "--standby-id", record["id"]]))
        session = self.session()
        # The late fork is kept on record, never adopted.
        self.assertEqual(session["standbyHistory"][-1]["forkSessionId"], "thread-late")
        self.assertNotIn("standby", session)
        self.assertEqual(self.send("after")["sessionId"], "thread-reset")

    def test_pending_decision_goal_experiment_and_self_compaction_keep_or_discard(self):
        first = self.finish(self.send("first", first=True), 90)
        self.prepare()
        path = Path(first["statePath"])
        runtime.update_json(path, path.parent / ".state.lock", lambda value: value.update(status="needs-human-decision"))
        decision = self.send("decision answer")
        self.assertEqual(decision["sessionId"], "thread-a")
        self.finish(decision, 40)  # The full thread compacted itself during that turn.
        after = self.send("after")
        self.assertEqual(after["sessionId"], "thread-a")
        self.assertNotIn("standby", self.session())
        self.assertEqual(self.session()["standbyHistory"][-1]["status"], "discarded")
        self.finish(after, 50)
        for field in ({"goalMode": True}, {"handoffExperiment": True}, {"provider": "claude"}):
            self.assertFalse(handoff.standby_eligible({**self.session(), **field}))

    def test_competing_change_between_read_and_adoption_keeps_current_session(self):
        self.finish(self.send("first", first=True), 90)
        self.prepare()
        session = self.session()
        path = runtime.session_file(self.root, "main-agent")
        runtime.update_json(path, path.parent / ".session-state.lock", lambda value: value["standby"].update(id="other"))
        self.assertIsNone(handoff.adopt_standby(runtime, self.root, "main-agent", session))
        self.assertEqual(self.session()["sessionId"], "thread-a")


class LaunchTests(unittest.TestCase):
    def test_preparer_runs_in_its_own_unit_not_the_finishing_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime.agent_directory(root, "main-agent", create=True)
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(handoff.sys, "platform", "linux"), \
                    mock.patch.object(runtime, "systemd_manager_usable", return_value=True), \
                    mock.patch.object(runtime, "_systemd_command", return_value=completed) as systemd, \
                    mock.patch.object(runtime, "spawn_contained_process") as spawn:
                self.assertIsNone(handoff.launch_standby(runtime, root, "main-agent", "standby-1"))
            spawn.assert_not_called()
            command = systemd.call_args.args[0]
            self.assertEqual(command[:2], ("systemd-run", "--user"))
            self.assertTrue(command[2].startswith("--unit=agent-factory-standby-"))
            self.assertIn("--property=KillMode=control-group", command)
            self.assertEqual(command[command.index("--") + 3], "_standby")
            self.assertEqual(command[-2:], ("--standby-id", "standby-1"))
            with mock.patch.object(handoff.sys, "platform", "linux"), \
                    mock.patch.object(runtime, "systemd_manager_usable", return_value=True), \
                    mock.patch.object(runtime, "_systemd_command", return_value=subprocess.CompletedProcess([], 1, "", "denied")):
                with self.assertRaises(OSError):
                    handoff.launch_standby(runtime, root, "main-agent", "standby-2")

    def test_launch_failure_is_recorded_and_the_turn_stays_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session = {"agentId": "main-agent", "role": "main", "provider": "codex", "backend": "app-server",
                       "sessionId": "thread-a", "projectRoot": str(root)}
            runtime.atomic_write_json(runtime.agent_directory(root, "main-agent", create=True) / "session.json", session)
            run = {"runId": "run-1", "agentId": "main-agent", "status": "completed", "sessionId": "thread-a",
                   "acceptedAt": runtime.now(), "contextUsage": usage("thread-a", 90)}
            with mock.patch.object(handoff, "launch_standby", side_effect=OSError("no service")):
                self.assertIsNone(handoff.schedule_standby(runtime, root, run))
            record = runtime.load_session(root, "main-agent")["standby"]
            self.assertEqual((record["status"], record["error"]), ("failed", "no service"))


class FakeForkRpc:
    def __init__(self, events, exited=False):
        self.calls, self.events, self.exited = [], list(events), exited
        self.process = mock.Mock()
        self.process.poll.return_value = 1 if exited else None

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {"thread": {"id": "thread-fork"}} if method == "thread/fork" else {}

    def write(self, value):
        self.calls.append((value["method"], None))

    def event(self):
        if not self.events:
            raise queue.Empty
        return self.events.pop(0)


class ForkCompactionTests(unittest.TestCase):
    session = {"projectRoot": "/project", "model": "model-one"}

    def test_compacts_only_the_fork_through_the_completed_turn(self):
        rpc = FakeForkRpc([
            {"method": "turn/started", "params": {"threadId": "thread-source", "turn": {"id": "other"}}},
            {"method": "turn/started", "params": {"threadId": "thread-fork", "turn": {"id": "c"}}},
            {"method": "item/completed", "params": {"threadId": "thread-fork", "item": {"type": "contextCompaction"}}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-fork", "tokenUsage": {
                "last": {"totalTokens": 30}, "modelContextWindow": 100}}},
            {"method": "turn/completed", "params": {"threadId": "thread-fork", "turn": {"id": "c", "status": "completed"}}},
        ])
        result = transport.compact_fork(rpc, self.session, "thread-source", "turn-9")
        self.assertEqual(result, {"threadId": "thread-fork", "usage": {"usedTokens": 30, "contextWindowTokens": 100}})
        fork = dict(rpc.calls)["thread/fork"]
        self.assertEqual((fork["threadId"], fork["lastTurnId"], fork["model"]), ("thread-source", "turn-9", "model-one"))
        self.assertEqual(dict(rpc.calls)["thread/compact/start"], {"threadId": "thread-fork"})
        self.assertNotIn("turn/start", dict(rpc.calls))

    def test_unconfirmed_or_interrupted_compaction_is_not_ready(self):
        rpc = FakeForkRpc([
            {"method": "turn/started", "params": {"threadId": "thread-fork", "turn": {"id": "c"}}},
            {"method": "turn/completed", "params": {"threadId": "thread-fork", "turn": {"id": "c", "status": "interrupted"}}},
        ])
        with self.assertRaises(transport.NativeError):
            transport.compact_fork(rpc, self.session, "thread-source", "turn-9")
        with self.assertRaises(transport.NativeError):
            transport.compact_fork(FakeForkRpc([], exited=True), self.session, "thread-source", "turn-9")

    def test_continuity_references_follow_the_original_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, rpc, state = native_fixture(Path(directory), goal=False)
            state["standbyContinuity"] = {"forkedThroughRunId": "run-1", "deltaRuns": [
                {"runId": "run-2", "status": "completed", "requestPath": "/r/request.md", "resultPath": "/r/result.md",
                 "statePath": "/r/state.json"}]}
            bridge.run("finish")
            turn = next(params for method, params in rpc.calls if method == "turn/start")
            self.assertEqual(turn["input"][0]["text"], "finish")
            self.assertIn("/r/request.md", turn["input"][1]["text"])
            self.assertIn("do not repeat, re-dispatch or re-approve", turn["input"][1]["text"])


if __name__ == "__main__":
    unittest.main()
