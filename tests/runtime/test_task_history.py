"""Physical task deletion with real files and shared-history preservation."""
import runtime_test_home  # noqa: F401
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_agent_exec import load_module
from tasks import history


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_module()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime.runtime_paths.resolve(self.root, create=True)
        self.main = self.runtime.agent_directory(self.root, "main-one", create=True)
        self.runtime.atomic_write_json(self.main / "session.json", {"role": "main", "sessionId": "shared-native"})
        self.parent = self.runtime.run_directory(self.root, "main-one", "run-parent", create=True)
        self.runtime.atomic_write(self.parent / "events.jsonl", b'Main conversation\n')
        self.runtime.atomic_write_json(self.parent / "state.json", {"role": "main"})
        self.args = self.runtime.parse_args(["delete-task", "--project-root", str(self.root),
            "--main-agent", "main-one", "--workflow-id", "flow-one", "--task-id", "task-one",
            "--actor", "human", "--authorization-reference", "trash-click"])

    def child(self, run="run-one", task="task-one", status="completed", owner="main-one", **extra):
        path = self.runtime.run_directory(self.root, "work-one", run, create=True)
        self.runtime.atomic_write_json(path / "state.json", {"agentId": "work-one", "runId": run,
            "role": "work", "status": status, "parentAgentId": owner, "parentRunId": "run-parent",
            "taskBinding": {"workflowId": "flow-one", "taskId": task}, **extra})
        self.runtime.atomic_write(path / "result.md", b'private result')
        self.runtime.atomic_write_json(self.parent / "children" / (run + ".json"), {"agentId": "work-one", "runId": run})
        return path

    def loop(self, tasks, **extra):
        directory = self.runtime.agent_directory(self.root, "work-one", create=True) / "loops" / "loop-one"
        self.runtime.ensure_directory(directory, self.runtime.find_project_anchor(directory))
        value = {"schemaVersion": "0.1.0", "loopId": "loop-one", "status": "completed", "phase": "ended",
            "projectRoot": str(self.root), "statePath": str(directory / "state.json"),
            "parentStatePath": str(self.parent / "state.json"), "workflow": {"id": "flow-one", "tasks": tasks, "index": 0}, **extra}
        self.runtime.atomic_write_json(directory / "state.json", value)
        self.runtime.atomic_write_json(directory / "task-list.json", {"id": "flow-one", "tasks": tasks})
        return directory

    def test_removes_all_revisions_and_refs_preserves_main_other_task_and_other_owner(self):
        selected = [self.child(), self.child("run-revision")]
        other = self.child("run-other", "task-two")
        another = self.child("run-other-owner", owner="main-two")
        loop = self.loop([{"id": "task-one"}])
        announcement = self.parent / "task-announcements" / "flow-one"
        self.runtime.atomic_write_json(announcement / "task-list.json", {"tasks": [{"id": "task-one"}]})
        result = history.delete(self.runtime, self.args)
        self.assertEqual(len(result["deletedRuns"]), 2)
        for path in selected + [loop, announcement]:
            self.assertFalse(path.exists())
        self.assertEqual(self.runtime.safe_read_json(self.parent / "children" / "run-one.json")["runId"], "run-other")
        for path in [other, another, self.main / "session.json", self.parent / "state.json"]:
            self.assertTrue(path.exists())
        self.assertEqual((self.parent / "events.jsonl").read_bytes(), b'Main conversation\n')
        # A fresh runtime invocation sees physical absence, not a dismissal marker.
        with self.assertRaises(self.runtime.ContractError) as error:
            history.delete(load_module(), self.args)
        self.assertEqual(error.exception.code, "task_delete_missing")

    def test_shared_loop_and_announcement_prune_only_selected_task_and_keep_work_unit(self):
        selected = self.child()
        other = self.child("run-other", "task-two")
        unit = {"workflowId": "flow-one", "taskId": "task-one", "repositories": [{"path": "/retained-worktree", "branch": "retained"}]}
        loop = self.loop([{"id": "task-one", "requestPath": "placeholder"}, {"id": "task-two", "title": "Keep"}], taskWorkspaces={"task-one": unit})
        state = self.runtime.safe_read_json(loop / "state.json")
        state["workflow"]["tasks"][0]["requestPath"] = str(loop / "task-0.md")
        self.runtime.atomic_write_json(loop / "state.json", state)
        self.runtime.atomic_write(loop / "task-0.md", b'delete')
        self.runtime.atomic_write(loop / "task-1.md", b'keep')
        self.runtime.atomic_write(loop / "original-request.md", b'delete')
        self.runtime.atomic_write_json(loop / "progress-history" / "revision-00000000.json", {"tasks": state["workflow"]["tasks"]})
        from tasks import progress
        self.runtime.atomic_write_json(loop / "progress-state.json", progress.snapshot(state))
        self.runtime.atomic_write(loop / "progress.md", progress.render(progress.snapshot(state)).encode())
        announcement = self.parent / "task-announcements" / "flow-one"
        tasks = [{"id": "task-one", "requestFile": str(announcement / "request-0.md")}, {"id": "task-two"}]
        self.runtime.atomic_write_json(announcement / "task-list.json", {"tasks": tasks})
        self.runtime.atomic_write_json(announcement / "announcement.json", {"taskList": {"tasks": tasks}, "parentAgentId": "main-one"})
        self.runtime.atomic_write(announcement / "request-0.md", b'delete')
        history.delete(self.runtime, self.args)
        self.assertFalse(selected.exists())
        self.assertTrue(other.exists())
        for path, accessor in [(loop / "state.json", lambda value: value["workflow"]["tasks"]),
                (loop / "task-list.json", lambda value: value["tasks"]),
                (announcement / "announcement.json", lambda value: value["taskList"]["tasks"]),
                (loop / "progress-history" / "revision-00000000.json", lambda value: value["tasks"])]:
            self.assertEqual([task["id"] for task in accessor(self.runtime.safe_read_json(path))], ["task-two"])
        self.assertEqual(self.runtime.safe_read_json(loop / "state.json")["taskWorkspaces"]["task-one"], unit)
        self.assertEqual((loop / "task-1.md").read_bytes(), b'keep')
        self.assertNotIn("| task-one |", (loop / "progress.md").read_text())
        self.assertIn("| task-two |", (loop / "progress.md").read_text())
        remaining = self.runtime.safe_read_json(loop / "state.json")
        self.assertEqual(progress.health(loop / "state.json", remaining), "current")
        for path in [loop / "task-0.md", loop / "original-request.md", announcement / "request-0.md"]:
            self.assertFalse(path.exists())

    def test_single_task_keeps_workspace_recovery_records_without_task_history(self):
        self.child()
        unit = {"repositories": [{"path": "/retained-worktree", "branch": "retained"}]}
        loop = self.loop([{"id": "task-one"}], taskWorkspaces={"task-one": unit})
        self.runtime.atomic_write_json(loop / "workspace-unit.json", unit)
        self.runtime.atomic_write(loop / "task-0.md", b'delete')
        history.delete(self.runtime, self.args)
        state = self.runtime.safe_read_json(loop / "state.json")
        self.assertNotIn("workflow", state)
        self.assertEqual(state["taskWorkspaces"]["task-one"], unit)
        self.assertTrue((loop / "workspace-unit.json").exists())
        self.assertFalse((loop / "task-0.md").exists())

    def test_active_process_driver_symlink_and_failure_never_acknowledge_deletion(self):
        selected = self.child(status="running")
        with self.assertRaises(self.runtime.ContractError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        state = self.runtime.safe_read_json(selected / "state.json")
        state.update(status="completed", workerIdentity={"pid": 123})
        self.runtime.atomic_write_json(selected / "state.json", state)
        with mock.patch.object(self.runtime, "process_identity_status", return_value="match"), self.assertRaises(self.runtime.ContractError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        state.pop("workerIdentity")
        self.runtime.atomic_write_json(selected / "state.json", state)
        loop = self.loop([{"id": "task-one"}])
        with self.runtime.file_lock(loop / ".driver.lock"), self.assertRaises(self.runtime.ContractError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        outside = self.root / "source.txt"
        outside.write_text("preserve")
        (selected / "linked").symlink_to(outside)
        with self.assertRaises(ValueError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        self.assertEqual(outside.read_text(), "preserve")
        (selected / "linked").unlink()
        with mock.patch.object(history.shutil, "rmtree", side_effect=OSError("disk denied")), self.assertRaises(OSError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        history.delete(self.runtime, self.args)
        self.assertFalse(selected.exists())

    def test_cli_physically_deletes_and_new_process_cannot_retrieve_the_run(self):
        selected = self.child()
        command = [sys.executable, str(Path(__file__).parents[2] / "scripts" / "exec.py"),
            "delete-task", "--project-root", str(self.root), "--main-agent", "main-one",
            "--workflow-id", "flow-one", "--task-id", "task-one", "--actor", "human",
            "--authorization-reference", "trash-click"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"kind":"task-history-deleted"', result.stdout)
        self.assertFalse(selected.exists())
        queried = subprocess.run(command[:2] + ["status", "--project-root", str(self.root),
            "--agent", "work-one", "--run-id", "run-one"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(queried.returncode, 0)
        self.assertIn("file_not_found", queried.stdout)

    def test_dispatch_reservation_removed_but_another_task_reference_blocks_deletion(self):
        selected = self.child()
        directory = selected.parent.parent / "dispatches"
        selected_dispatch = directory / "dispatch-selected.json"
        other_dispatch = directory / "dispatch-other.json"
        self.runtime.atomic_write_json(selected_dispatch, {"dispatchTuple": {"parentAgentId": "main-one",
            "taskBinding": {"workflowId": "flow-one", "taskId": "task-one"}}})
        self.runtime.atomic_write_json(other_dispatch, {"dispatchTuple": {"parentAgentId": "main-one",
            "taskBinding": {"workflowId": "flow-one", "taskId": "task-two"}}})
        other = self.child("run-verifier", "task-two", verifiedWorkRunId="run-one")
        with self.assertRaises(self.runtime.ContractError) as error:
            history.delete(self.runtime, self.args)
        self.assertEqual(error.exception.code, "task_delete_shared_run")
        self.assertTrue(selected.exists())
        state = self.runtime.safe_read_json(other / "state.json")
        state.pop("verifiedWorkRunId")
        self.runtime.atomic_write_json(other / "state.json", state)
        history.delete(self.runtime, self.args)
        self.assertFalse(selected_dispatch.exists())
        self.assertTrue(other_dispatch.exists())

    def test_pending_decision_is_protected_until_its_exact_loop_has_ended(self):
        selected = self.child(status="needs-human-decision")
        with self.assertRaises(self.runtime.ContractError):
            history.delete(self.runtime, self.args)
        loop = self.loop([{"id": "task-one", "workAgentId": "work-one", "workRunId": "run-one"}], status="needs-human-decision")
        with self.assertRaises(self.runtime.ContractError):
            history.delete(self.runtime, self.args)
        self.assertTrue(selected.exists())
        state = self.runtime.safe_read_json(loop / "state.json")
        state["status"] = "cancelled"
        self.runtime.atomic_write_json(loop / "state.json", state)
        history.delete(self.runtime, self.args)
        self.assertFalse(selected.exists())
        self.assertFalse(loop.exists())

    def test_prior_task_deleted_while_sibling_driver_and_shared_session_keep_running(self):
        for mode, ended_status in [("work", "completed"), ("work-verification", "failed")]:
            selected = self.child(status=ended_status)
            other = self.child("run-other", "task-two", status="running")
            loop = self.loop([{"id": "task-one", "workStatus": ended_status}, {"id": "task-two", "workStatus": "running"}],
                status="active", execution={"taskMode": mode, "taskBinding": {"workflowId": "flow-one", "taskId": "task-two"}},
                currentChild={"agentId": "work-one", "runId": "run-other"})
            state = self.runtime.safe_read_json(loop / "state.json")
            state["workflow"]["index"] = 1
            state.update(originalRequestPath=str(loop / "task-1.md"), originalRequestHash="current-task-hash")
            self.runtime.atomic_write(loop / "task-1.md", b'current request')
            self.runtime.atomic_write_json(loop / "state.json", state)
            with self.runtime.file_lock(loop / ".driver.lock"), self.runtime.file_lock(other.parent.parent / ".session.lock"):
                history.delete(self.runtime, self.args)
            remaining = self.runtime.safe_read_json(loop / "state.json")
            self.assertFalse(selected.exists())
            self.assertTrue(other.exists())
            self.assertEqual(remaining["status"], "active")
            self.assertEqual(remaining["workflow"]["index"], 0)
            self.assertEqual(remaining["currentChild"], state["currentChild"])
            self.assertEqual(remaining["execution"], state["execution"])
            self.assertEqual(remaining["originalRequestPath"], state["originalRequestPath"])
            self.assertEqual(remaining["originalRequestHash"], "current-task-hash")
