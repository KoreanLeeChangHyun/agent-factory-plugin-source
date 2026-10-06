"""Deletion of one selected sidebar agent, with real isolated runtime files."""
import runtime_test_home  # noqa: F401
import tempfile
import os
import unittest
from pathlib import Path
from unittest import mock

from test_agent_exec import load_module
from runs import deletion as agent_deletion


class AgentDeletionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_module()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime.runtime_paths.resolve(self.root, create=True)
        self.agent = self.runtime.agent_directory(self.root, "main-one", create=True)
        self.session = {"agentId": "main-one", "projectRoot": str(self.root), "role": "main", "sessionId": None}
        self.runtime.atomic_write_json(self.agent / "session.json", self.session)
        self.run = self.runtime.run_directory(self.root, "main-one", "run-one", create=True)
        self.state = {"agentId": "main-one", "runId": "run-one", "role": "main", "status": "completed"}
        self.runtime.atomic_write_json(self.run / "state.json", self.state)
        self.runtime.atomic_write(self.run / "events.jsonl", b"conversation")
        self.args = self.runtime.parse_args(["delete-agent", "--project-root", str(self.root), "--agent", "main-one",
            "--actor", "human", "--authorization-reference", "sidebar-trash:main-one"])

    def delete(self):
        return agent_deletion.delete(self.runtime, self.args)

    def test_real_removal_survives_fresh_lookup_and_preserves_unrelated_data(self):
        other = self.runtime.agent_directory(self.root, "main-other", create=True)
        self.runtime.atomic_write_json(other / "session.json", {**self.session, "agentId": "main-other"})
        source = self.root / "source.txt"
        source.write_text("keep")
        provider = self.root / "provider.json"
        provider.write_text("shared session")
        result = self.delete()
        self.assertEqual(result["kind"], "agent-deleted")
        self.assertEqual(result["agentId"], "main-one")
        self.assertFalse(self.agent.exists())
        self.assertEqual(source.read_text(), "keep")
        self.assertEqual(provider.read_text(), "shared session")
        self.assertTrue((other / "session.json").exists())
        fresh = load_module()
        self.assertNotIn("main-one", [item.name for item in fresh.iter_agent_directories(self.root)])
        self.assertTrue(self.delete()["alreadyDeleted"])

    def test_active_status_live_process_containment_and_dispatch_lock_protect_records(self):
        for status in ("running", "starting", "needs-human-decision"):
            self.runtime.atomic_write_json(self.run / "state.json", {**self.state, "status": status})
            with self.assertRaises(self.runtime.ContractError):
                self.delete()
            self.assertTrue(self.agent.exists())
        self.runtime.atomic_write_json(self.run / "state.json", {**self.state, "workerPid": os.getpid()})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.runtime.atomic_write_json(self.run / "state.json", {**self.state, "workerIdentity": {"pid": 123}})
        for identity in ("match", "unknown"):
            with mock.patch.object(self.runtime, "process_identity_status", return_value=identity), self.assertRaises(self.runtime.ContractError):
                self.delete()
        self.runtime.atomic_write_json(self.run / "state.json", {**self.state, "containment": {"kind": "test"}})
        with mock.patch.object(self.runtime, "containment_is_empty", return_value=False), self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.runtime.atomic_write_json(self.run / "state.json", self.state)
        with self.runtime.file_lock(self.agent / ".dispatch.lock"), self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.assertTrue((self.run / "state.json").exists())

    def test_shared_children_verifiers_and_foreign_loop_refs_block_before_deletion(self):
        other = self.runtime.run_directory(self.root, "work-other", "run-other", create=True)
        for extra in ({"parentAgentId": "main-one", "parentRunId": "run-one"}, {"verifiedWorkRunId": "run-one"}):
            self.runtime.atomic_write_json(other / "state.json", {"agentId": "work-other", "runId": "run-other", "status": "completed", **extra})
            with self.assertRaises(self.runtime.ContractError) as error:
                self.delete()
            self.assertEqual(error.exception.code, "agent_delete_shared")
            self.assertTrue((self.run / "events.jsonl").exists())
        self.runtime.atomic_write_json(other / "state.json", {"agentId": "work-other", "runId": "run-other", "status": "completed"})
        loop = other.parent.parent / "loops" / "loop-one"
        self.runtime.atomic_write_json(loop / "state.json", {"status": "completed", "parentStatePath": str(self.run / "state.json")})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.assertTrue(loop.exists())

    def test_work_unit_and_workspace_recovery_contract_refuses_deletion(self):
        for extra in ({"worktree": {"workUnit": True, "phase": "merged"}}, {"taskWorkspace": {"mode": "code", "path": "/retained"}}):
            self.runtime.atomic_write_json(self.agent / "session.json", {**self.session, **extra})
            with self.assertRaises(self.runtime.ContractError) as error:
                self.delete()
            self.assertEqual(error.exception.code, "agent_delete_work_unit")
        self.runtime.atomic_write_json(self.agent / "session.json", self.session)
        loop = self.agent / "loops" / "loop-one"
        self.runtime.atomic_write_json(loop / "state.json", {"status": "completed", "taskWorkspaces": {"task-one": {"mode": "code"}}})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.assertTrue(loop.exists())

    def test_active_loop_driver_and_incomplete_records_are_rejected(self):
        loop = self.agent / "loops" / "loop-one"
        self.runtime.atomic_write_json(loop / "state.json", {"status": "active"})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.runtime.atomic_write_json(loop / "state.json", {"status": "completed"})
        with self.runtime.file_lock(loop / ".driver.lock"), self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.assertTrue(self.agent.exists())
        # A partially created run lacks the ownership/termination evidence needed
        # for destructive cleanup, even when other runs are ended.
        incomplete = self.runtime.run_directory(self.root, "main-one", "run-incomplete", create=True)
        self.runtime.atomic_write(incomplete / "request.md", b"unknown writer")
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.assertTrue(incomplete.exists())

    def test_unresolved_dispatch_reservation_and_active_goal_are_preserved(self):
        self.runtime.atomic_write_json(self.agent / "session.json", {**self.session, "goal": {"status": "active"}})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.runtime.atomic_write_json(self.agent / "session.json", self.session)
        dispatch = self.agent / "dispatches" / "dispatch-one.json"
        self.runtime.atomic_write_json(dispatch, {"kind": "dispatch-reservation"})
        with self.assertRaises(self.runtime.ContractError) as error:
            self.delete()
        self.assertEqual(error.exception.code, "agent_delete_incomplete")
        self.assertTrue(dispatch.exists())

    def test_target_binding_symlinks_authority_and_io_failures_never_ack_success(self):
        self.args.authorization_reference = ""
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.args.authorization_reference = "sidebar-trash:main-one"
        self.args.agent = "../source"
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.args.agent = "main-one"
        self.runtime.atomic_write_json(self.agent / "session.json", {**self.session, "agentId": "main-other"})
        with self.assertRaises(self.runtime.ContractError):
            self.delete()
        self.runtime.atomic_write_json(self.agent / "session.json", self.session)
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        link = self.run / "attachment"
        link.symlink_to(outside)
        with self.assertRaises((ValueError, self.runtime.ContractError)):
            self.delete()
        self.assertEqual(outside.read_text(), "keep")
        link.unlink()
        with mock.patch.object(agent_deletion.shutil, "rmtree", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            self.delete()
        self.assertTrue(self.agent.exists())
