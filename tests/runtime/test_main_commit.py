"""Receipt-bound commit protocol without executing a Git commit or an agent."""
import runtime_test_home  # noqa: F401
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from storage.errors import ContractError
from storage.files import file_lock
from tasks import commits, orchestrator_guard as guard
import coordination


class MainCommitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        (self.repository / "a.py").write_text("approved")
        self.work_path = self.root / "work-state.json"
        self.work = {"statePath": str(self.work_path), "agentId": "worker", "runId": "run-work",
                     "role": "work", "status": "completed", "parentAgentId": "main", "taskMode": "work"}
        self.work_path.write_text(json.dumps(self.work))
        self.main = {"role": "main", "agentId": "main", "roleBoundaryPolicy": 1}
        self.proposal = {"repository": str(self.repository), "branch": "feature", "head": "abc",
                         "paths": ["a.py"], "message": "Approved local change",
                         "contentHashes": {"a.py": hashlib.sha256(b"approved").hexdigest()},
                         "diffHash": hashlib.sha256(b"approved diff").hexdigest(),
                         "workStatePath": str(self.work_path),
                         "authority": {"decision": "approved", "source": "Human message", "time": "2026-10-07", "scope": "repo/a.py"}}
        self.calls, self.staged = [], False
        self.receipt = {"outcome": "completed", "changedPaths": ["repo/a.py"]}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(commits, "validate_receipt", return_value=self.receipt).start()
        mock.patch.object(commits, "git", side_effect=self.git).start()

    def git(self, root, *arguments):
        self.calls.append(arguments)
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(self.repository).encode()
        if arguments == ("rev-parse", "--git-common-dir"):
            return b".git"
        if arguments == ("symbolic-ref", "--short", "HEAD"):
            return b"feature"
        if arguments == ("rev-parse", "HEAD"):
            return b"abc"
        if arguments[:2] == ("rev-parse", "--git-path"):
            return (".git/" + arguments[2]).encode()
        if arguments[:2] == ("diff", "--binary"):
            return b"approved diff"
        if arguments[:2] == ("ls-tree", "-r"):
            return b"a.py\0"
        if arguments[:2] == ("diff", "--cached"):
            return b"a.py\0" if self.staged else b""
        if arguments[0] == "add":
            self.staged = True
        return b""

    def test_preflight_does_not_stage_or_commit(self):
        result = commits.execute(self.root, self.main, self.proposal)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(any(call[0] in {"add", "commit"} for call in self.calls))

    def test_apply_uses_exact_add_and_ordinary_commit_with_hooks(self):
        result = commits.execute(self.root, self.main, self.proposal, apply=True)
        self.assertEqual(result["status"], "committed")
        mutations = [call for call in self.calls if call[0] in {"add", "commit"}]
        self.assertEqual(mutations, [("add", "--", "a.py"), ("commit", "-m", "Approved local change")])
        self.assertFalse(any(word in {"push", "reset", "restore", "rebase", "--amend", "--no-verify"}
                             for call in self.calls for word in call))

    def test_stale_branch_index_scope_hash_and_unapproved_proposals_block_before_mutation(self):
        for changed in ({"branch": "main"}, {"head": "stale"}, {"paths": ["other.py"]},
                        {"paths": ["../a.py"]}, {"diffHash": "different"},
                        {"contentHashes": {"a.py": "different"}}, {"authority": {"decision": "proposed"}},
                        {"argv": ["commit", "--amend"]}):
            self.calls.clear()
            with self.assertRaises(ContractError):
                commits.execute(self.root, self.main, {**self.proposal, **changed}, apply=True)
            self.assertFalse(any(call[0] in {"add", "commit"} for call in self.calls))
        self.staged = True
        with mock.patch.object(commits, "git", side_effect=lambda root, *args: b"a.py\0other.py\0" if args[:2] == ("diff", "--cached") else self.git(root, *args)):
            with self.assertRaises(ContractError):
                commits.execute(self.root, self.main, self.proposal, apply=True)

    def test_existing_approved_staged_set_can_be_committed(self):
        self.staged = True
        self.assertEqual(commits.execute(self.root, self.main, self.proposal, apply=True)["status"], "committed")

    def test_new_file_hash_remains_bound_before_and_after_staging(self):
        (self.repository / "new.txt").write_text("new approved content")
        self.receipt["changedPaths"].append("repo/new.txt")
        proposal = {**self.proposal, "paths": ["a.py", "new.txt"], "contentHashes": {
            **self.proposal["contentHashes"], "new.txt": hashlib.sha256(b"new approved content").hexdigest()}}
        def git_with_addition(root, *args):
            if args[:2] == ("diff", "--cached"):
                return b"a.py\0new.txt\0" if self.staged else b""
            if args[:2] == ("diff", "--binary"):
                self.assertEqual(args[-2:], ("--", "a.py"))
            return self.git(root, *args)
        with mock.patch.object(commits, "git", side_effect=git_with_addition):
            self.assertEqual(commits.execute(self.root, self.main, proposal)["status"], "ready")
            self.assertEqual(commits.execute(self.root, self.main, proposal, apply=True)["status"], "committed")

    def test_hook_failure_keeps_staged_progress_without_bypass_or_index_repair(self):
        def rejecting_hook(root, *args):
            if args[0] == "commit":
                raise ContractError("commit_git_failed", "hook failed")
            return self.git(root, *args)
        with mock.patch.object(commits, "git", side_effect=rejecting_hook):
            with self.assertRaises(ContractError):
                commits.execute(self.root, self.main, self.proposal, apply=True)
        self.assertTrue(self.staged)
        self.assertFalse(any(call[0] in {"reset", "restore"} for call in self.calls))

    def test_other_roles_and_read_only_parent_cannot_commit(self):
        for state in ({**self.main, "role": "work"}, {**self.main, "role": "verification"},
                      {**self.main, "roleBoundaryPolicy": None},
                      {**self.main, "executionPolicy": {"sandboxPolicy": {"type": "read-only"}}}):
            with self.assertRaises(ContractError):
                commits.execute(self.root, state, self.proposal, apply=True)

    def test_shared_integration_lock_serializes_index_mutation(self):
        with file_lock(self.repository / '.git/.agent-factory-integration.lock'):
            with self.assertRaises(ContractError) as error:
                commits.execute(self.root, self.main, self.proposal, apply=True)
            self.assertEqual(error.exception.code, 'lock_busy')
        self.assertFalse(any(call[0] in {'add', 'commit'} for call in self.calls))

    def test_incomplete_wrong_owner_and_verification_route_require_own_completion(self):
        for update in ({"status": "running"}, {"parentAgentId": "other"}, {"taskMode": "work-verification"},
                       {"taskWorkspace": {"mode": "code"}}, {"workProfile": "scribe"}):
            self.work_path.write_text(json.dumps({**self.work, **update}))
            with self.assertRaises(ContractError):
                commits.execute(self.root, self.main, self.proposal, apply=True)

    def test_verified_route_accepts_only_receipt_for_exact_work_run(self):
        self.work_path.write_text(json.dumps({**self.work, "taskMode": "work-verification"}))
        verification_path = self.root / "verification-state.json"
        verification_path.write_text(json.dumps({"statePath": str(verification_path), "role": "verification",
                                                "status": "completed", "agentId": "verifier", "runId": "verify-one"}))
        proposal = {**self.proposal, "verificationStatePath": str(verification_path)}
        for verified_run in ("run-work", "other-run"):
            with mock.patch.object(commits, "validate_receipt", side_effect=[self.receipt, {
                    "decision": "pass", "verifiedWorkRunId": verified_run}]):
                if verified_run == "run-work":
                    self.assertEqual(commits.execute(self.root, self.main, proposal)["status"], "ready")
                else:
                    with self.assertRaises(ContractError):
                        commits.execute(self.root, self.main, proposal)

    def test_recorded_human_skip_and_scribe_acceptance_use_canonical_loop(self):
        agents_root = self.root / "agents"
        loop_path = agents_root / "worker/loops/loop-one/state.json"
        loop_path.parent.mkdir(parents=True)
        loop = {"statePath": str(loop_path), "loopId": "loop-one", "status": "completed",
                "workAgentId": "worker", "latestWorkRunId": "run-work",
                "humanSkip": {"actor": "human", "authorizationReference": "Human message",
                              "decisionEvidence": "Skip requested", "recordedAt": "2026-10-07"}}
        proposal = {**self.proposal, "loopStatePath": str(loop_path)}
        self.work_path.write_text(json.dumps({**self.work, "taskMode": "work-verification"}))
        with mock.patch.object(commits.runtime_paths, "resolve", return_value={"agentsRoot": str(agents_root)}):
            loop_path.write_text(json.dumps(loop))
            self.assertEqual(commits.execute(self.root, self.main, proposal)["status"], "ready")
            self.work_path.write_text(json.dumps({**self.work, "workProfile": "scribe"}))
            with self.assertRaises(ContractError):
                commits.execute(self.root, self.main, proposal)
            loop["draftReview"] = {"status": "accepted", "workRunId": "run-work",
                                   "authorizationReference": "Human message", "decisionEvidence": "Draft accepted"}
            loop_path.write_text(json.dumps(loop))
            self.assertEqual(commits.execute(self.root, self.main, proposal)["status"], "ready")

    def test_symlink_change_and_existing_history_operation_are_denied(self):
        (self.repository / "a.py").unlink()
        (self.repository / "a.py").symlink_to(self.work_path)
        with self.assertRaises(ContractError):
            commits.execute(self.root, self.main, self.proposal, apply=True)
        (self.repository / "a.py").unlink()
        (self.repository / "a.py").write_text("approved")
        (self.repository / ".git/MERGE_HEAD").write_text("pending")
        with self.assertRaises(ContractError):
            commits.execute(self.root, self.main, self.proposal, apply=True)

    def test_main_guard_allows_managed_commit_but_never_raw_git_mutations(self):
        config = {"pluginRoots": [str(guard.PLUGIN_ROOT)], "writeRoot": str(self.root)}
        self.assertTrue(guard.allowed_command(f"python3 {guard.PLUGIN_ROOT}/scripts/commit.py --input {self.root}/commit.json", config, str(self.root)))
        for command in ("git add a.py", "git commit -m x", "git commit --no-verify -m x", "git commit --amend",
                        "git push", "git push --force", "git reset --hard", "git rebase main", "git branch -D main"):
            self.assertFalse(guard.allowed_command(command, config, str(self.root)), command)


class CoordinationRecordTests(unittest.TestCase):
    def test_only_main_appends_sourced_records_to_the_bound_execution_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runtime/main/run"
            run.mkdir(parents=True)
            path = "docs/progress/contract-one/contract-v1.md"
            target = root / path
            target.parent.mkdir(parents=True)
            original = b"Accepted contract\n<!-- contract-execution-record -->\n## Execution\n"
            target.write_bytes(original)
            state = {"role": "main", "roleBoundaryPolicy": 1, "agentId": "main", "runId": "run-one",
                     "statePath": str(run / "state.json"), "taskAnnouncementContract": 1,
                     "runtimeBinding": {"runtimeRoot": str(root / "runtime")}}
            (run / "state.json").write_text(json.dumps(state))
            document = {"id": "flow", "title": "Contract", "contract": {
                "id": "contract-one", "version": 1, "progress": {"path": path, "owner": "main"},
                "fileOperations": [{"taskIds": ["one"], "operation": "modify", "path": path}]},
                "tasks": [{"id": "one", "title": "Task", "description": "Bound task",
                           "completionCriteria": "Own result", "requiredFileOperations": []}]}
            announcement = run / "task-announcements/flow/announcement.json"
            announcement.parent.mkdir(parents=True)
            announcement.write_text(json.dumps({"schemaVersion": 1, "kind": "task-announcement", "parentAgentId": "main",
                                               "parentRunId": "run-one", "runtimeBinding": state["runtimeBinding"], "taskList": document}))
            value = {"announcementPath": str(announcement), "record": {"kind": "waiting", "source": "run-worker",
                     "time": "2026-10-07", "scope": "one", "note": "Worker is running"}}
            result = coordination.append_record(root, state, value)
            self.assertEqual(result["path"], path)
            appended = target.read_bytes()
            self.assertTrue(appended.startswith(original))
            for changed_state, changed_value in (
                ({**state, "role": "work"}, value), ({**state, "role": "verification"}, value),
                (state, {**value, "record": {**value["record"], "status": "completed"}}),
                (state, {**value, "record": {**value["record"], "kind": "pass"}}),
            ):
                with self.assertRaises(ContractError):
                    coordination.append_record(root, changed_state, changed_value)
                self.assertEqual(target.read_bytes(), appended)
            target.unlink()
            target.symlink_to(run / "state.json")
            with self.assertRaises(ContractError):
                coordination.append_record(root, state, value)


if __name__ == "__main__":
    unittest.main()
