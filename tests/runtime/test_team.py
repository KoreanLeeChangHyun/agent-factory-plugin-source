"""Persistent workers across tasks: reuse through loop.py start and exact per-run history reads."""
from __future__ import annotations

import runtime_test_home  # noqa: F401  Isolate runtime records from the real home.
import domain_fixtures

import contextlib
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_task_binding
from test_agent_loop import FakeRuntime, load_modules

WORKERS = ("team-worker", "team-other", "team-verifier", "team-verifier-2", "team-verifier-3")


class TeamAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.agent_exec, self.agent_loop = load_modules()
        from tasks import team
        self.team = team
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = FakeRuntime(self.root, self.agent_exec)
        patch = mock.patch.object(self.agent_loop, "AgentRuntime", return_value=self.runtime)
        patch.start()
        self.addCleanup(patch.stop)
        domain_fixtures.place_workers(self.root, WORKERS, name="team runtime")

    # Helpers -----------------------------------------------------------------------------------

    def brief(self, name, strategy=None, text=None):
        request = self.root / f"{name}.md"
        request.write_text(text or f"Goal: {name}\nDeliver {name}.\n", encoding="utf-8")
        allocation = None
        if strategy:
            allocation = test_task_binding.TaskBindingTests.allocation()
            allocation["session"] = {"strategy": strategy, "reason": f"{strategy} for {name}"}
            path = self.root / f"{name}-allocation.json"
            path.write_text(json.dumps(allocation))
            allocation = path
        return request, allocation

    def start(self, name, agent="team-worker", strategy=None, extra=(), verifier=None):
        request, allocation = self.brief(name, strategy)
        arguments = ["start", "--project-root", str(self.root), "--request-file", str(request),
                     "--work-agent", agent, "--task-mode", "work", "--codex", "/bin/true", "--receipt-recovery", "manual"]
        if verifier:
            arguments[arguments.index("work")] = "work-verification"
            arguments += ["--verification-agent", verifier]
        if allocation:
            arguments += ["--allocation-file", str(allocation)]
        return self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([*arguments, *extra]))

    def reconcile(self, started):
        args = self.agent_loop.build_parser().parse_args([
            "reconcile", "--project-root", str(self.root), "--work-agent", started["workAgentId"],
            "--loop-id", started["loopId"]])
        return self.agent_loop.reconcile_loop(args)

    def finish(self, started, text):
        run = self.runtime.complete_work(started["workAgentId"], started["latestWorkRunId"])
        Path(run["resultPath"]).write_text(text, encoding="utf-8")
        return self.reconcile(started)

    def cli(self, *arguments):
        emitted = []
        with mock.patch.object(self.agent_exec, "emit", side_effect=emitted.append):
            code = self.agent_loop.main([arguments[0], "--project-root", str(self.root), *arguments[1:]])
        [document] = emitted
        return code, json.loads(json.dumps(document))

    def loops(self, agent="team-worker"):
        return sorted((self.agent_exec.agent_root(self.root) / agent / "loops").glob("*/state.json"))

    def refused(self, code, name, **values):
        before = (len(self.runtime.dispatches), {agent: len(self.loops(agent)) for agent in WORKERS})
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.start(name, **values)
        self.assertEqual(raised.exception.code, code)
        # Refused before acceptance: no loop, no dispatch, no concurrent run.
        self.assertEqual(before, (len(self.runtime.dispatches), {agent: len(self.loops(agent)) for agent in WORKERS}))
        return raised.exception

    # Reuse and history ---------------------------------------------------------------------------

    def test_same_worker_runs_consecutive_tasks_and_each_run_keeps_its_own_result(self):
        first = self.start("first", strategy="new")
        first = self.finish(first, "first result\n")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["teamAssignment"]["workers"][0]["disposition"], "new")
        code, candidates = self.cli("team-candidates", "--role", "work", "--domain", "team runtime")
        self.assertEqual(code, 0)
        self.assertEqual([item["agentId"] for item in candidates["eligible"]], ["team-worker"])
        self.assertTrue(candidates["eligible"][0]["sameDomain"])

        second = self.start("second", strategy="reuse")
        self.assertEqual(second["status"], "active")
        assignment = second["teamAssignment"]["workers"][0]
        self.assertEqual((assignment["disposition"], assignment["strategy"], assignment["priorTaskCount"]), ("reused", "reuse", 1))
        self.assertEqual([item["operation"] for item in self.runtime.dispatches], ["submit", "send"])
        self.assertEqual(self.runtime.dispatches[1]["execution"]["taskBinding"]["taskId"], second["workflow"]["tasks"][0]["id"])
        second = self.finish(second, "second result\n")

        code, history = self.cli("team-history", "--agent", "team-worker")
        self.assertEqual(code, 0)
        tasks = {item["taskId"]: item for item in history["tasks"]}
        self.assertEqual(len(tasks), 2)
        first_task, second_task = tasks[first["workflow"]["tasks"][0]["id"]], tasks[second["workflow"]["tasks"][0]["id"]]
        for task, loop, text in ((first_task, first, "first result\n"), (second_task, second, "second result\n")):
            self.assertEqual(task["state"], "completed")
            self.assertEqual([run["runId"] for run in task["runs"]], [loop["latestWorkRunId"]])
            run = task["runs"][0]
            self.assertEqual((run["trigger"], run["report"], run["receipt"]), ("initial", "present", "present"))
            self.assertEqual(Path(run["resultPath"]).read_text(encoding="utf-8"), text)
            self.assertEqual(task["loops"][0]["loopId"], loop["loopId"])
            self.assertEqual(run["requestHash"], self.runtime.runs[("team-worker", run["runId"])]["requestHash"])
        self.assertNotEqual(first_task["runs"][0]["resultPath"], second_task["runs"][0]["resultPath"])
        self.assertEqual(history["worker"]["availability"], "idle")
        self.assertEqual(history["worker"]["taskCounts"], {"completed": 2})
        code, one = self.cli("team-history", "--agent", "team-worker", "--workflow-id", first["workflow"]["id"],
                             "--task-id", first_task["taskId"])
        self.assertEqual([item["taskId"] for item in one["tasks"]], [first_task["taskId"]])
        code, missing = self.cli("team-history", "--agent", "team-worker", "--task-id", "no-such-task")
        self.assertEqual((code, missing["error"]["code"]), (2, "team_task_missing"))

    def test_roster_groups_workers_by_domain_with_current_queued_and_paged_output(self):
        document = {"id": "flow-team", "title": "Team flow", "tasks": []}
        for index, agent in enumerate(("team-worker", "team-other")):
            request = self.root / f"flow-{index}.md"
            request.write_text(f"Bounded part {index}\n", encoding="utf-8")
            document["tasks"].append({"id": f"part-{index}", "title": f"Part {index}", "description": f"Bounded part {index}",
                                      "completionCriteria": "Delivered", "requestFile": str(request), "workAgentId": agent})
        task_list = self.root / "flow.json"
        task_list.write_text(json.dumps(document))
        self.agent_loop.start_loop(self.agent_loop.build_parser().parse_args([
            "start", "--project-root", str(self.root), "--request-file", str(self.root / "flow-0.md"),
            "--task-list-file", str(task_list), "--task-id", "part-0", "--work-agent", "team-worker",
            "--task-mode", "work", "--codex", "/bin/true", "--receipt-recovery", "manual"]))
        code, roster = self.cli("team-roster")
        self.assertEqual(code, 0)
        workers = {item["agentId"]: item for item in roster["workers"]}
        self.assertEqual(workers["team-worker"]["availability"], "busy")
        self.assertEqual([task["taskId"] for task in workers["team-worker"]["currentTasks"]], ["part-0"])
        self.assertEqual(workers["team-other"]["availability"], "queued")
        self.assertEqual([task["taskId"] for task in workers["team-other"]["queuedTasks"]], ["part-1"])
        group = next(item for item in roster["domains"] if item["name"] == "team runtime")
        # team-other has no session or run yet; its queued task still lists it in its domain.
        self.assertEqual(group["workers"], ["team-other", "team-worker"])
        self.assertIsNone(workers["team-other"]["role"])
        # Lossless pages reassemble the same document.
        code, page = self.cli("team-roster", "--offset", "0", "--length", "500")
        text, revision = page["text"], page["revision"]
        while page["nextOffset"] is not None:
            code, page = self.cli("team-roster", "--offset", str(page["nextOffset"]), "--length", "500", "--revision", revision)
            text += page["text"]
        self.assertEqual(json.loads(text)["workers"][0]["agentId"], roster["workers"][0]["agentId"])

    # Refusals before acceptance ------------------------------------------------------------------

    def test_busy_waiting_human_and_uncertain_workers_are_refused_without_a_second_run(self):
        running = self.start("running", verifier="team-verifier")
        self.refused("worker_busy", "while-running")
        # A Verification session held by another unfinished task is busy too.
        self.refused("worker_busy", "verifier-busy", agent="team-other", verifier="team-verifier")
        child = self.runtime.runs[("team-worker", running["latestWorkRunId"])]
        child["status"] = "needs-human-decision"
        waiting = self.reconcile(running)
        self.assertEqual(waiting["status"], "needs-human-decision")
        error = self.refused("worker_waiting_human", "while-waiting")
        self.assertIn(running["loopId"], error.message)
        code, candidates = self.cli("team-candidates", "--role", "work")
        reasons = {item["agentId"]: [reason["code"] for reason in item["reasons"]] for item in candidates["excluded"]}
        self.assertEqual(reasons["team-worker"], ["waiting-human"])
        self.assertTrue(candidates["newWorkerReasonRequired"])
        # An unreadable record makes availability uncertain; it is reported, never treated as idle.
        broken = self.agent_exec.agent_root(self.root) / "team-other" / "runs" / "run-broken"
        broken.mkdir(parents=True)
        (broken / "state.json").write_text("{not json")
        self.agent_exec.atomic_write_json(self.agent_exec.session_file(self.root, "team-other"), {"role": "work", "sessionId": "s"})
        self.refused("worker_uncertain", "uncertain", agent="team-other")
        code, roster = self.cli("team-roster")
        self.assertEqual({item["agentId"]: item["availability"] for item in roster["workers"]}["team-other"], "uncertain")
        self.assertTrue(roster["problems"])

    def test_direct_run_waiting_for_a_decision_holds_its_session(self):
        directory = self.agent_exec.agent_root(self.root) / "team-other" / "runs" / "run-direct"
        directory.mkdir(parents=True)
        self.agent_exec.atomic_write_json(self.agent_exec.session_file(self.root, "team-other"), {"role": "work", "sessionId": "s"})
        self.agent_exec.atomic_write_json(directory / "state.json", {"runId": "run-direct", "agentId": "team-other", "role": "work",
            "status": "needs-human-decision", "acceptedAt": "2026-10-11T00:00:00Z", "resultPath": str(directory / "result.md")})
        self.refused("worker_waiting_human", "direct-wait", agent="team-other")

    def test_incompatible_missing_and_contradicting_session_choices_are_refused(self):
        self.finish(self.start("base", strategy="new"), "done\n")
        # A reused worker keeps its provider and model; changes go through loop.py handoff.
        error = self.refused("worker_incompatible", "other-provider", extra=("--work-model", "claude-opus-5-5"))
        self.assertIn("provider", error.message)
        session = self.agent_exec.session_file(self.root, "team-worker")
        self.agent_exec.atomic_write_json(session, {**self.agent_exec.safe_read_json(session), "model": "gpt-5.5"})
        self.refused("worker_incompatible", "other-model", strategy="reuse", extra=("--work-model", "gpt-6-astra"))
        self.refused("worker_incompatible", "other-profile", strategy="reuse", extra=("--work-profile", "scribe"))
        self.refused("worker_reuse_missing", "unknown", agent="team-other", strategy="reuse")
        self.refused("worker_strategy_conflict", "contradiction", strategy="new")
        # Without Main's strategy, a same-provider model change is recorded, not hidden.
        accepted = self.start("recorded", extra=("--work-model", "gpt-6-astra"))
        difference = accepted["teamAssignment"]["workers"][0]["differences"]
        self.assertEqual([(item["code"], item["recorded"], item["required"]) for item in difference],
                         [("model-mismatch", "gpt-5.5", "gpt-6-astra")])
        code, candidates = self.cli("team-candidates", "--role", "work", "--model", "gpt-5.5")
        self.assertEqual(candidates["eligible"], [])
        self.assertTrue(candidates["newWorkerReasonRequired"])

    def test_failed_task_and_retried_start_stay_visible_and_do_not_block_reuse(self):
        self.runtime.fail_before_call = True
        with self.assertRaises(self.agent_exec.ContractError):
            self.start("retry-me", strategy="new")
        self.runtime.fail_before_call = False
        [path] = self.loops()
        self.assertEqual(self.agent_exec.safe_read_json(path)["status"], "runtime-error")
        # Recovery names the exact durable loop; identical text alone cannot merge new tasks.
        failed = self.agent_exec.safe_read_json(path)
        code, roster = self.cli("team-roster")
        worker = next(item for item in roster["workers"] if item["agentId"] == "team-worker")
        self.assertEqual([(item["kind"], item["loopStatus"]) for item in worker["attention"]], [("failed", "runtime-error")])
        retried = self.reconcile(failed)
        code, history = self.cli("team-history", "--agent", "team-worker")
        [task] = history["tasks"]
        self.assertEqual(task["state"], "current")
        self.assertEqual([loop["taskState"] for loop in task["loops"]], ["current"])
        self.assertEqual(len(self.loops()), 1, "Recovery reuses the loop as well as the task")
        revisions = [json.loads(item.read_text()) for item in (path.parent / "progress-history").glob("*.json")]
        self.assertTrue(any(item["status"] == "runtime-error" for item in revisions), "The prior failure remains in the transition history")
        self.assertEqual(history["worker"]["attention"], [])

    def test_missing_report_is_reported_and_never_borrowed_from_a_later_run(self):
        started = self.start("no-report")
        run = self.runtime.runs[("team-worker", started["latestWorkRunId"])]
        run["status"] = "failed"
        self.reconcile(started)
        code, history = self.cli("team-history", "--agent", "team-worker")
        [entry] = history["tasks"][0]["runs"]
        self.assertEqual((entry["status"], entry["report"], entry["receipt"]), ("failed", "missing", "missing"))
        self.assertEqual(history["tasks"][0]["state"], "failed")

    # Steering versus a follow-up task ------------------------------------------------------------

    def test_completed_task_is_not_steered_and_follow_up_is_a_new_tracked_task(self):
        first = self.finish(self.start("finished"), "finished result\n")
        task_id = first["workflow"]["tasks"][0]["id"]
        steer = self.agent_loop.build_parser().parse_args([
            "steer", "--project-root", str(self.root), "--work-agent", "team-worker", "--loop-id", first["loopId"],
            "--actor", "human", "--authorization-reference", "follow-up", "--decision-evidence", "asked",
            "--task-id", task_id, "--run-id", first["latestWorkRunId"], "--message", "One more thing"])
        with self.assertRaises(self.agent_exec.ContractError) as raised:
            self.agent_loop.steer_loop(steer)
        self.assertEqual(raised.exception.code, "steering_binding_invalid")
        follow = self.start("follow-up", strategy="reuse")
        self.assertNotEqual(follow["workflow"]["tasks"][0]["id"], task_id)
        # In-progress steering stays on the same task and is delivered as that task's next run.
        steer = self.agent_loop.build_parser().parse_args([
            "steer", "--project-root", str(self.root), "--work-agent", "team-worker", "--loop-id", follow["loopId"],
            "--actor", "human", "--authorization-reference", "same-task", "--decision-evidence", "asked",
            "--task-id", follow["workflow"]["tasks"][0]["id"], "--run-id", follow["latestWorkRunId"], "--message", "Also cover X"])
        self.agent_loop.steer_loop(steer)
        follow = self.finish(follow, "follow-up result\n")
        self.assertEqual(follow["status"], "active")  # The queued addition reopened the same task.
        follow = self.finish(follow, "follow-up with X\n")
        code, history = self.cli("team-history", "--agent", "team-worker")
        tasks = {item["taskId"]: item for item in history["tasks"]}
        self.assertEqual([run["trigger"] for run in tasks[task_id]["runs"]], ["initial"])
        self.assertEqual([run["trigger"] for run in tasks[follow["workflow"]["tasks"][0]["id"]]["runs"]], ["initial", "steering"])
        self.assertEqual(Path(tasks[task_id]["runs"][0]["resultPath"]).read_text(encoding="utf-8"), "finished result\n")

    # Concurrency and stale observations ----------------------------------------------------------

    def test_concurrent_starts_for_one_idle_worker_accept_exactly_one(self):
        self.finish(self.start("seed"), "seed\n")
        barrier = threading.Barrier(2)
        outcomes = []
        original = self.team.check_start

        def slow_check(*arguments, **values):
            result = original(*arguments, **values)
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=0.5)  # The second start waits on the lock, so this times out once.
            return result

        def run(name):
            try:
                outcomes.append(("accepted", self.start(name)["loopId"]))
            except self.agent_exec.ContractError as error:
                outcomes.append(("refused", error.code))
        with mock.patch.object(self.team, "check_start", side_effect=slow_check):
            threads = [threading.Thread(target=run, args=(name,)) for name in ("race-a", "race-b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["accepted", "refused"])
        self.assertIn(("refused", "worker_busy"), outcomes)
        self.assertEqual(len(self.loops()), 2)

    def test_stale_candidate_list_is_rechecked_at_start(self):
        self.finish(self.start("seed"), "seed\n")
        code, stale = self.cli("team-candidates", "--role", "work")
        self.assertEqual([item["agentId"] for item in stale["eligible"]], ["team-worker"])
        self.start("taken-meanwhile")
        self.refused("worker_busy", "from-stale-list", strategy="reuse")

    # Compatibility -------------------------------------------------------------------------------

    def test_existing_records_without_team_fields_remain_readable(self):
        started = self.start("legacy")
        path = Path(started["statePath"])
        legacy = self.agent_exec.safe_read_json(path)
        legacy.pop("teamAssignment")
        legacy.pop("workflow")
        self.agent_exec.atomic_write_json(path, legacy)
        self.assertNotIn("teamAssignment", self.agent_loop.public_state(legacy))
        code, roster = self.cli("team-roster")
        worker = {item["agentId"]: item for item in roster["workers"]}["team-worker"]
        self.assertEqual(worker["availability"], "busy")
        status = self.agent_loop.status_loop(self.agent_loop.build_parser().parse_args([
            "status", "--project-root", str(self.root), "--work-agent", "team-worker", "--loop-id", started["loopId"]]))
        self.assertEqual(status["loopId"], started["loopId"])

    def test_reads_change_no_records(self):
        self.finish(self.start("observed"), "observed\n")
        records = self.agent_exec.agent_root(self.root)

        def snapshot():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(records.rglob("*")) if path.is_file() and not path.name.endswith(".lock")}
        before = snapshot()
        for arguments in (("team-roster",), ("team-candidates", "--role", "work"), ("team-history", "--agent", "team-worker")):
            self.assertEqual(self.cli(*arguments)[0], 0)
        self.assertEqual(snapshot(), before)

    def test_profile_comparison_never_merges_workers_by_model_name(self):
        profile = {"role": "work", "provider": "claude", "model": "claude-opus-5-5", "lastModel": None,
                   "reasoningEffort": "medium", "fast": None, "workProfile": "work",
                   "humanApprovalPolicy": "bypass", "sandbox": "danger-full-access"}
        self.assertEqual(self.team.compare(profile, {"role": "work", "model": "claude-opus-5-5", "reasoningEffort": "medium",
                                                     "workProfile": "work", "humanApprovalPolicy": "bypass"}), [])
        codes = [item["code"] for item in self.team.compare(copy.deepcopy(profile), {
            "role": "verification", "model": "claude-fable-5-1", "reasoningEffort": "high",
            "workProfile": "explore", "humanApprovalPolicy": "required"})]
        self.assertEqual(codes, ["role-mismatch", "model-mismatch", "effort-mismatch", "profile-mismatch", "permission-mismatch"])


if __name__ == "__main__":
    unittest.main()
