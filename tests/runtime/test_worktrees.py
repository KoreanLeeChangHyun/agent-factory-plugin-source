from __future__ import annotations

import runtime_test_home
import importlib.util
import io
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/exec.py"
spec = importlib.util.spec_from_file_location("worktree_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
from execution import worktrees


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Worktree Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "file.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        runtime.runtime_paths.resolve(self.root, create=True)
        self.session("main-one")

    def git(self, *args, root=None):
        return worktrees.git(root or self.root, *args).stdout.decode().strip()

    def session(self, agent):
        path = runtime.agent_directory(self.root, agent, create=True) / "session.json"
        runtime.atomic_write_json(path, {"agentId": agent, "role": "main", "projectRoot": str(self.root),
            "sessionId": "thread-kept", "maxAttempts": 1})

    def command(self, action, *extra, agent="main-one"):
        args = runtime.parse_args(["worktree", "--project-root", str(self.root), "--agent", agent, action, *extra])
        out = io.StringIO()
        original = runtime.emit
        runtime.emit = lambda value: out.write(json.dumps(value))
        try:
            worktrees.command(runtime, args)
        finally:
            runtime.emit = original
        return json.loads(out.getvalue())

    def test_create_isolated_edit_merge_restore_and_history(self):
        created = self.command("create")
        path = Path(created["workingDirectory"])
        self.assertFalse(path.is_relative_to(self.root))
        self.assertEqual(path.parent.name, "worktrees")
        self.assertEqual(self.command("create")["workingDirectory"], str(path))
        (path / "file.txt").write_text("isolated\n")
        self.assertEqual((self.root / "file.txt").read_text(), "base\n")
        self.git("add", ".", root=path)
        self.git("commit", "-m", "isolated", root=path)
        result = self.command("merge")
        self.assertEqual(result["workingDirectory"], str(self.root))
        self.assertEqual(result["worktree"]["phase"], "merged")
        self.assertEqual((self.root / "file.txt").read_text(), "isolated\n")
        self.assertTrue(path.exists())
        self.assertEqual(runtime.load_session(self.root, "main-one")["sessionId"], "thread-kept")

    def test_conflict_stays_in_worktree_until_resolved_and_retried(self):
        path = Path(self.command("create")["workingDirectory"])
        for root, text in ((self.root, "workspace\n"), (path, "isolated\n")):
            (root / "file.txt").write_text(text)
            self.git("add", ".", root=root)
            self.git("commit", "-m", text.strip(), root=root)
        result = self.command("merge")
        self.assertEqual(result["worktree"]["phase"], "conflict")
        self.assertEqual(result["conflicts"], ["file.txt"])
        self.assertEqual(result["workingDirectory"], str(path))
        self.assertFalse(worktrees.dirty(self.root))
        self.assertFalse(worktrees.merge_pending(self.root))
        with self.assertRaisesRegex(runtime.ContractError, "Resolve conflicts"):
            self.command("merge")
        (path / "file.txt").write_text("both\n")
        self.git("add", ".", root=path)
        self.git("commit", "--no-edit", root=path)
        self.assertEqual(self.command("merge")["workingDirectory"], str(self.root))
        self.assertEqual((self.root / "file.txt").read_text(), "both\n")

    def test_copy_preserves_source_and_merge_refuses_dirty_target(self):
        (self.root / "file.txt").write_text("uncommitted\n")
        (self.root / "new.bin").write_bytes(b"\0\1")
        before = self.git("status", "--porcelain")
        with self.assertRaisesRegex(runtime.ContractError, "Choose whether"):
            self.command("create")
        path = Path(self.command("create", "--changes", "copy")["workingDirectory"])
        self.assertEqual((path / "file.txt").read_text(), "uncommitted\n")
        self.assertEqual((path / "new.bin").read_bytes(), b"\0\1")
        self.assertEqual(self.git("status", "--porcelain"), before)
        with self.assertRaisesRegex(runtime.ContractError, "workspace changes"):
            self.command("merge")
        self.assertEqual(self.command("status")["workingDirectory"], str(path))

    def test_multiple_conversations_busy_guard_and_run_capture(self):
        first = self.command("create")
        self.session("main-two")
        second = self.command("create", agent="main-two")
        self.assertNotEqual(first["workingDirectory"], second["workingDirectory"])
        session = runtime.load_session(self.root, "main-one")
        state = runtime.create_run(project_root=self.root, agent_id="main-one", actor="human", request=b"edit", session=session)
        self.assertEqual(state["workingDirectory"], first["workingDirectory"])
        with self.assertRaisesRegex(runtime.ContractError, "active runs"):
            self.command("merge")
        self.assertEqual(self.command("status", agent="main-two")["workingDirectory"], second["workingDirectory"])

    def test_changed_branch_and_missing_directory_fail_closed(self):
        path = Path(self.command("create")["workingDirectory"])
        self.git("checkout", "-b", "unexpected", root=path)
        with self.assertRaisesRegex(runtime.ContractError, "Restore.*branch"):
            worktrees.checked_path(runtime.load_session(self.root, "main-one"))
        path.rename(path.with_name(path.name + "-moved"))
        with self.assertRaisesRegex(runtime.ContractError, "missing"):
            worktrees.checked_path(runtime.load_session(self.root, "main-one"))

    def test_workspace_permissions_follow_the_conversation_and_return(self):
        session_path = runtime.session_file(self.root, "main-one")
        session = runtime.load_session(self.root, "main-one")
        session["executionPolicy"] = runtime_test_home.policy("workspace-write", self.root)
        session["sandbox"] = "workspace-write"
        runtime.atomic_write_json(session_path, session)
        path = Path(self.command("create")["workingDirectory"])
        session = runtime.load_session(self.root, "main-one")
        self.assertEqual(session["executionPolicy"]["sandboxPolicy"]["writable_roots"], [str(path)])
        environment = {key: value for key, value in os.environ.items() if key not in ("AGENT_FACTORY_EXECUTION_POLICY", "AGENT_FACTORY_PARENT_STATE", "CODEX_THREAD_ID")}
        with mock.patch.dict(os.environ, environment, clear=True):
            args = runtime.parse_args(["send", "--agent", "main-one"])
            self.assertEqual(runtime.resolve_execution_policy(args, self.root, session), session["executionPolicy"])
            args = runtime.parse_args(["send", "--agent", "main-one", "--sandbox", "workspace-write", "--approval-policy", "never"])
            self.assertEqual(runtime.resolve_execution_policy(args, self.root, session)["sandboxPolicy"]["writable_roots"], [str(path)])
        self.command("merge")
        self.assertEqual(runtime.load_session(self.root, "main-one")["executionPolicy"]["sandboxPolicy"]["writable_roots"], [str(self.root)])

    def test_cli_restart_restores_location_and_new_conversation_needs_no_turn(self):
        with mock.patch.object(runtime.native_codex, "inspect_capabilities", return_value={"submit": {}}):
            created = self.command("create", "--codex", "/bin/true", agent="main-new")
        self.assertEqual(list(runtime.iter_run_states(self.root, "main-new")), [])
        result = subprocess.run([os.sys.executable, str(SCRIPT), "worktree", "--project-root", str(self.root),
            "--agent", "main-new", "status"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["workingDirectory"], created["workingDirectory"])

    def test_new_claude_conversation_can_start_in_a_worktree(self):
        from adapters import claude
        capabilities = {"submit": {"worktrees": True}, "send": {}, "diagnostic": None}
        with mock.patch.object(claude, "inspect_capabilities", return_value=capabilities), \
                mock.patch.object(runtime.native_codex, "inspect_capabilities") as codex_probe:
            created = self.command("create", "--claude", "/bin/true", "--model", "claude-sonnet-5",
                                   "--sandbox", "danger-full-access", "--approval-policy", "never", agent="main-claude")
            codex_probe.assert_not_called()
        session = runtime.load_session(self.root, "main-claude")
        self.assertEqual((session["provider"], session["model"]), ("claude", "claude-sonnet-5"))
        self.assertEqual(Path(created["workingDirectory"]), Path(session["worktree"]["path"]))

    def test_both_provider_transports_use_worktree_without_replacing_thread(self):
        from native_fixtures import native_fixture
        path = Path(self.command("create")["workingDirectory"])
        with contextlib.redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(self.root, goal=False)
            bridge.session["workingDirectory"] = str(path)
            bridge.setup("Continue in this worktree")
        resumed = next(params for method, params in rpc.calls if method == "thread/resume")
        turn = next(params for method, params in rpc.calls if method == "turn/start")
        self.assertEqual(resumed["cwd"], str(path))
        self.assertEqual(resumed["threadId"], "thread-exact")
        self.assertEqual(turn["cwd"], str(path))
        session = {**bridge.session, "codex": "codex"}
        session.pop("backend", None)
        command = runtime.build_codex_command(session, state, "thread-exact")
        self.assertEqual(command[command.index("--cd") + 1], str(path))
        self.assertEqual(command[-2], "thread-exact")

    def test_custom_location_keep_changes_and_target_branch_guard(self):
        (self.root / "file.txt").write_text("keep here\n")
        path = Path(self.temp.name) / "custom worktree"
        self.command("create", "--changes", "keep", "--path", str(path))
        self.assertEqual((path / "file.txt").read_text(), "base\n")
        self.assertEqual((self.root / "file.txt").read_text(), "keep here\n")
        self.git("checkout", "-b", "different")
        with self.assertRaisesRegex(runtime.ContractError, "original workspace branch"):
            self.command("merge")

    def test_ignored_local_file_is_not_overwritten_during_merge(self):
        (self.root / ".gitignore").write_text("local.txt\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore local file")
        path = Path(self.command("create")["workingDirectory"])
        (path / "local.txt").write_text("incoming\n")
        self.git("add", "-f", "local.txt", root=path)
        self.git("commit", "-m", "add local", root=path)
        (self.root / "local.txt").write_text("preserve me\n")
        with self.assertRaises(runtime.ContractError):
            self.command("merge")
        self.assertEqual((self.root / "local.txt").read_text(), "preserve me\n")
        self.assertEqual(self.command("status")["workingDirectory"], str(path))

    def test_child_inheritance_tracks_parent_return_without_moving_history(self):
        self.command("create")
        parent = runtime.load_session(self.root, "main-one")
        child = {"projectRoot": str(self.root), "role": "work", "sessionId": "child-thread",
                 "executionPolicy": runtime_test_home.policy("workspace-write", self.root)}
        isolated = worktrees.inherit(child, parent)
        self.assertEqual(worktrees.checked_path(isolated), Path(parent["worktree"]["path"]))
        self.assertEqual(isolated["sessionId"], "child-thread")
        self.command("merge")
        restored = worktrees.inherit(isolated, runtime.load_session(self.root, "main-one"))
        self.assertEqual(worktrees.checked_path(restored), self.root)
        self.assertEqual(restored["executionPolicy"]["sandboxPolicy"]["writable_roots"], [str(self.root)])

    def unit(self, name="feature", **kwargs):
        return self.command("create", "--name", name, "--branch", name, "--base", "main", "--changes", "keep", **kwargs)

    def test_unit_uses_selected_base_and_preserves_dirty_source(self):
        self.git("checkout", "-b", "source")
        (self.root / "file.txt").write_text("source commit")
        self.git("commit", "-am", "source")
        (self.root / "file.txt").write_text("uncommitted")
        before = self.git("status", "--porcelain")
        result = self.unit()
        self.assertEqual((Path(result["workingDirectory"]) / "file.txt").read_text(), "base\n")
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(result["worktree"]["targetBranch"], "main")

    def test_duplicate_branch_rejected_without_suffix_or_binding(self):
        self.git("branch", "taken")
        with self.assertRaisesRegex(runtime.ContractError, "already exists"):
            self.unit("taken")
        self.assertNotIn("worktree", runtime.load_session(self.root, "main-one"))
        self.assertEqual(self.git("branch", "--list", "taken*"), "taken")

    def test_unit_merge_cleans_and_archives_without_squashing(self):
        value = self.unit(); path = Path(value["workingDirectory"])
        (path / "new.txt").write_text("new")
        self.git("add", ".", root=path); self.git("commit", "-m", "new", root=path)
        commit = self.git("rev-parse", "HEAD", root=path)
        result = self.command("merge", "--target", "main")
        self.assertTrue(result["worktree"]["cleaned"])
        self.assertFalse(path.exists())
        self.assertFalse(self.git("branch", "--list", "feature"))
        self.git("merge-base", "--is-ancestor", commit, "main")
        with self.assertRaisesRegex(runtime.ContractError, "read-only"):
            worktrees.checked_path(runtime.load_session(self.root, "main-one"))
        with self.assertRaisesRegex(runtime.ContractError, "read-only"):
            self.unit("another")

    def test_unit_cleanup_preserves_ignored_data_and_can_retry(self):
        path = Path(self.unit()["workingDirectory"])
        (path / ".gitignore").write_text("private.txt\n")
        self.git("add", ".gitignore", root=path); self.git("commit", "-m", "ignore", root=path)
        (path / "private.txt").write_text("keep")
        with self.assertRaisesRegex(runtime.ContractError, "unpreserved"):
            self.command("merge")
        self.assertTrue((path / "private.txt").exists())
        self.assertEqual(self.command("status")["worktree"]["phase"], "merged")
        (path / "private.txt").rename(Path(self.temp.name) / "preserved.txt")
        self.assertTrue(self.command("merge")["worktree"]["cleaned"])

    def test_unit_conflict_never_cleans_and_resolution_can_finish(self):
        path = Path(self.unit()["workingDirectory"])
        for root,text in ((path,"feature"),(self.root,"main")):
            (root / "file.txt").write_text(text)
            self.git("commit", "-am", text, root=root)
        with self.assertRaises(runtime.ContractError): self.command("merge")
        self.assertTrue(path.exists())
        self.assertTrue(worktrees.merge_pending(self.root))
        (self.root / "file.txt").write_text("resolved")
        self.git("add", "."); self.git("commit", "--no-edit")
        self.assertTrue(self.command("merge")["worktree"]["cleaned"])

    def test_discovery_stops_at_repository_and_ignores_symlink(self):
        nested = self.root / "nested"; nested.mkdir(); self.git("init", "-b", "main", root=nested)
        self.assertEqual([x["path"] for x in worktrees.repositories(self.root)], [str(self.root)])
        home = Path(self.temp.name) / "home"; home.mkdir()
        (home / "linked").symlink_to(self.root, target_is_directory=True)
        child = home / "child"; child.mkdir(); self.git("init", "-b", "main", root=child)
        self.assertEqual([x["path"] for x in worktrees.repositories(home)], [str(child)])

    def test_unit_rejects_repository_outside_project(self):
        with self.assertRaisesRegex(runtime.ContractError, "Select a repository"):
            self.command("create", "--name", "unit", "--branch", "unit", "--repository", self.temp.name)

    def test_unit_obeys_target_merge_configuration(self):
        path = Path(self.unit()["workingDirectory"])
        (path / "added").write_text("value"); self.git("add", ".", root=path); self.git("commit", "-m", "unit", root=path)
        self.git("config", "merge.ff", "false")
        self.command("merge")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()), 3)

    def test_project_home_supports_same_branch_name_in_distinct_repositories(self):
        first = self.root
        second = Path(self.temp.name) / "second"; second.mkdir()
        self.git("init", "-b", "main", root=second)
        self.git("config", "user.name", "Test", root=second)
        self.git("config", "user.email", "test@example.invalid", root=second)
        self.git("commit", "--allow-empty", "-m", "base", root=second)
        self.root = Path(self.temp.name)
        runtime.runtime_paths.resolve(self.root, create=True)
        self.session("main-first"); self.session("main-second")
        repositories = self.command("repositories")["repositories"]
        self.assertEqual({x["path"] for x in repositories}, {str(first), str(second)})
        one = self.command("create", "--name", "same", "--branch", "same", "--base", "main", "--repository", str(first), "--changes", "keep", agent="main-first")
        two = self.command("create", "--name", "same", "--branch", "same", "--base", "main", "--repository", str(second), "--changes", "keep", agent="main-second")
        self.assertNotEqual(one["workingDirectory"], two["workingDirectory"])
        self.assertEqual(one["worktree"]["branch"], two["worktree"]["branch"])
        self.assertEqual(worktrees.checked_path(runtime.load_session(self.root, "main-first")), Path(one["workingDirectory"]))


class TaskWorkspaceTests(unittest.TestCase):
    setUp = WorktreeTests.setUp
    git = WorktreeTests.git
    session = WorktreeTests.session

    def setup_task(self, selected=None):
        from tasks import workspaces
        import sys
        self.ws = workspaces
        selected = selected or [{"path": str(self.root), "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('file.txt').exists()"]]}]
        self.state = {"projectRoot": str(self.root), "loopId": "loop-fixture", "workAgentId": "work-task",
                      "execution": {"taskMode": "work", "taskBinding": {"taskId": "task-fixture", "workflowId": "workflow-fixture"},
                                    "workspacePlan": workspaces.plan(self.root, {"mode": "code", "repositories": selected})}}
        self.loop_path = runtime.agent_directory(self.root, "work-task", create=True) / "loops" / "loop-fixture" / "state.json"
        self.save = lambda: runtime.atomic_write_json(self.loop_path, self.state)
        self.save()
        self.value = workspaces.prepare(runtime, self.state, self.loop_path, self.save)
        self.unit = self.value["repositories"][0]
        self.path = Path(self.unit["path"])
        self.work = {"status": "completed", "runId": "run-fixture"}
        self.receipt = {"tests": {"run": True}, "changedPaths": ["file.txt"]}

    def integrate(self):
        return self.ws.integrate(runtime, self.state, self.work, self.receipt, self.save)

    def test_isolation_dirty_source_policy_binding_and_revision_reuse(self):
        (self.root / "file.txt").write_text("private source")
        (self.root / "private").write_text("untracked source")
        before = self.git("status", "--porcelain")
        self.setup_task()
        self.assertEqual((self.path / "file.txt").read_text(), "base\n")
        self.assertFalse((self.path / "private").exists())
        self.assertTrue(self.unit["sourceDirty"])
        self.assertFalse(self.unit["changesIncluded"])
        self.assertEqual(self.git("status", "--porcelain"), before)
        again = self.ws.prepare(runtime, self.state, self.loop_path, self.save)
        self.assertEqual(again["id"], self.value["id"])
        policy = runtime_test_home.policy("workspace-write", self.root)
        session = self.ws.bind({"projectRoot": str(self.root), "executionPolicy": policy, "sessionId": "same-thread"}, self.value)
        self.assertEqual(worktrees.checked_path(session), self.path)
        self.assertEqual(session["sessionId"], "same-thread")
        self.assertEqual(session["executionPolicy"]["sandboxPolicy"]["writable_roots"], [str(self.path)])
        (self.path / "file.txt").write_text("task result\n")
        # The merge would change file.txt, which the target holds uncommitted.
        with self.assertRaisesRegex(runtime.ContractError, r"Target checkout.*\(file\.txt\)"):
            self.integrate()
        self.assertEqual(self.unit["targetOverlap"], ["file.txt"])
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual((self.root / "file.txt").read_text(), "private source")

    def test_ordinary_merge_receipt_history_and_duplicate_completion(self):
        self.setup_task()
        (self.path / "file.txt").write_text("task result\n")
        self.assertEqual(self.integrate()["status"], "complete")
        self.assertEqual((self.root / "file.txt").read_text(), "task result\n")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertTrue(self.unit["resultCommit"])
        self.assertEqual(self.unit["mergeCommit"], self.git("rev-parse", "HEAD"))
        self.assertTrue(self.unit["integrationChecks"])
        self.assertEqual(self.value["verification"], "not requested")
        self.assertFalse(self.path.exists())
        first = self.git("rev-parse", "HEAD")
        self.assertEqual(self.integrate()["status"], "complete")
        self.assertEqual(self.git("rev-parse", "HEAD"), first)

    def test_target_update_nonconflicting_merge(self):
        self.setup_task()
        (self.path / "file.txt").write_text("task")
        (self.root / "other").write_text("latest target")
        self.git("add", "other"); self.git("commit", "-m", "target advanced")
        target = self.git("rev-parse", "HEAD")
        self.integrate()
        self.git("merge-base", "--is-ancestor", target, "HEAD")
        self.assertEqual((self.root / "other").read_text(), "latest target")

    def test_conflict_is_preserved_then_explicit_resolution_rechecked(self):
        self.setup_task()
        (self.path / "file.txt").write_text("work version\n")
        (self.root / "file.txt").write_text("target version\n")
        self.git("commit", "-am", "target")
        result = self.integrate()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["files"], ["file.txt"])
        self.assertFalse(worktrees.merge_pending(self.root))
        self.assertTrue(worktrees.merge_pending(self.path))
        self.assertEqual(self.integrate()["status"], "conflict")
        (self.path / "file.txt").write_text("both meanings\n")
        self.git("add", "file.txt", root=self.path)
        self.assertEqual(self.integrate()["status"], "complete")
        self.assertEqual((self.root / "file.txt").read_text(), "both meanings\n")
        self.assertEqual(self.unit["integrationChecks"][0]["exitCode"], 0)

    def test_failure_cancel_verification_and_unreported_changes_never_integrate(self):
        self.setup_task()
        before = self.git("rev-parse", "HEAD")
        for status in ("failed", "cancelled", "needs-human-decision"):
            self.work["status"] = status
            with self.assertRaises(runtime.ContractError): self.integrate()
        self.work["status"] = "completed"
        self.receipt["tests"]["run"] = False
        with self.assertRaises(runtime.ContractError): self.integrate()
        self.receipt["tests"]["run"] = True
        self.state["execution"]["taskMode"] = "work-verification"
        with self.assertRaises(runtime.ContractError): self.integrate()
        self.state["execution"]["taskMode"] = "work"
        (self.path / "unexpected").write_text("preserve")
        with self.assertRaisesRegex(runtime.ContractError, "Unreported"):
            self.integrate()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertTrue(self.path.exists())

    def test_integration_check_failure_and_recovery_without_duplicate_commit(self):
        import sys
        self.setup_task()
        (self.path / "file.txt").write_text("task")
        self.unit["checks"] = [[sys.executable, "-c", "raise SystemExit(2)"]]
        before = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(runtime.ContractError, "check failed"):
            self.integrate()
        self.assertEqual(self.unit["phase"], "check-failed")
        commit = self.unit["resultCommit"]
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.unit["checks"] = [[sys.executable, "-c", "pass"]]
        self.integrate()
        self.assertEqual(self.unit["resultCommit"], commit)

    def test_cleanup_preserves_ignored_data_and_active_run(self):
        self.setup_task()
        (self.path / ".gitignore").write_text("private\n")
        (self.path / "private").write_text("unsaved")
        self.receipt["changedPaths"].append(".gitignore")
        self.integrate()
        self.assertEqual(self.unit["phase"], "merged")
        self.assertTrue((self.path / "private").exists())
        self.assertIn("unpreserved", self.unit["cleanupPending"])
        self.assertFalse(self.unit.get("cleaned", False))

    def test_selection_branch_collision_detached_and_non_git(self):
        from tasks import workspaces
        for value in ({"mode": "code"}, {"mode": "shared", "repositories": [{}]}, {"mode": "unknown"}):
            with self.assertRaises(runtime.ContractError): workspaces.plan(self.root, value)
        self.setup_task()
        unit = self.unit
        alternate = {**self.state, "taskWorkspaces": {}}
        with self.assertRaisesRegex(runtime.ContractError, "collision"):
            workspaces.prepare(runtime, alternate, self.loop_path, self.save)
        self.git("checkout", "--detach")
        # Existing captured target is explicit and remains usable in detached source.
        self.assertEqual(workspaces.plan(self.root, {"mode": "code", "repositories": [{"path": str(self.root), "targetBranch": "main", "checks": unit["checks"]}]})["repositories"][0]["targetBranch"], "main")
        with self.assertRaises(runtime.ContractError): workspaces.plan(Path(self.temp.name), {"mode": "code", "repositories": [{"path": self.temp.name, "checks": unit["checks"]}]})

    def test_multiple_repositories_partial_failure_then_resume(self):
        import sys
        first = self.root
        second = Path(self.temp.name) / "second"; second.mkdir()
        self.git("init", "-b", "develop", root=second)
        self.git("config", "user.name", "Fixture", root=second)
        self.git("config", "user.email", "fixture@example.invalid", root=second)
        (second / "file.txt").write_text("base")
        self.git("add", ".", root=second); self.git("commit", "-m", "base", root=second)
        self.root = Path(self.temp.name)
        runtime.runtime_paths.resolve(self.root, create=True)
        self.setup_task([{"path": str(first), "checks": [[sys.executable, "-c", "pass"]]},
                         {"path": str(second), "targetBranch": "develop", "checks": [[sys.executable, "-c", "raise SystemExit(3)"]]}])
        self.receipt["changedPaths"] = ["repo/file.txt", "second/file.txt"]
        for unit in self.value["repositories"]:
            (Path(unit["path"]) / "file.txt").write_text("result")
        with self.assertRaises(runtime.ContractError): self.integrate()
        one, two = self.value["repositories"]
        self.assertEqual(one["phase"], "merged")
        self.assertEqual(two["phase"], "check-failed")
        first_commit = self.git("rev-parse", "HEAD", root=first)
        self.assertTrue(Path(two["path"]).exists())
        two["checks"] = [[sys.executable, "-c", "pass"]]
        self.integrate()
        self.assertEqual(self.git("rev-parse", "HEAD", root=first), first_commit)
        self.assertEqual((second / "file.txt").read_text(), "result")
        self.assertTrue(all(unit["cleaned"] for unit in self.value["repositories"]))

    def test_latest_target_change_during_checks_rechecks_and_active_cleanup_pauses(self):
        self.setup_task()
        (self.path / "file.txt").write_text("task")
        original = self.ws.check
        moved = False
        def advance(unit, save, stage):
            nonlocal moved
            original(unit, save, stage)
            if stage == "integration" and not moved:
                moved = True
                (self.root / "new-target").write_text("new target")
                self.git("add", "new-target"); self.git("commit", "-m", "concurrent target")
        with mock.patch.object(self.ws, "check", side_effect=advance):
            self.assertEqual(self.integrate()["status"], "target-changed")
        self.assertTrue(self.path.exists())
        active = {"status": "running", "workingDirectory": str(self.path)}
        with mock.patch.object(runtime, "iter_run_states", side_effect=lambda root: iter([active])):
            with self.assertRaisesRegex(runtime.ContractError, "active run"):
                self.integrate()
        with mock.patch.object(self.ws, "cleanup"):
            self.integrate()
        with mock.patch.object(runtime, "iter_run_states", side_effect=lambda root: iter([active])):
            self.ws.cleanup(runtime, self.state, self.value, self.save)
        self.assertEqual((self.root / "new-target").read_text(), "new target")
        self.assertTrue(self.path.exists())
        self.assertFalse(self.unit.get("cleaned", False))

    def test_runtime_dispatch_binding_rejects_wrong_agent_and_preserves_cwd_identity(self):
        self.setup_task()
        args = runtime.parse_args(["submit", "--agent", "work-task", "--role", "work", "--task-id", "task-fixture",
            "--task-workspace-file", self.state["execution"]["taskWorkspacePath"], "--message", "fixture"])
        self.assertEqual(self.ws.dispatch_binding(runtime, args, self.root)["path"], str(self.path))
        args.agent = "wrong-task"
        with self.assertRaises(runtime.ContractError): self.ws.dispatch_binding(runtime, args, self.root)

    def test_real_run_record_and_policy_use_isolated_cwd_keep_original_runtime_root(self):
        self.setup_task()
        args = runtime.parse_args(["submit", "--agent", "work-task", "--role", "work", "--task-id", "task-fixture",
            "--task-workspace-file", self.state["execution"]["taskWorkspacePath"], "--message", "fixture"])
        inherited = runtime_test_home.policy("workspace-write", self.root)
        with mock.patch.dict(os.environ, {"AGENT_FACTORY_EXECUTION_POLICY": json.dumps(inherited)}):
            selected = runtime.resolve_execution_policy(args, self.root)
        self.assertEqual(selected["sandboxPolicy"]["writable_roots"], [str(self.path)])
        session = self.ws.bind({"projectRoot": str(self.root), "agentId": "work-task", "role": "work", "maxAttempts": 1,
            "executionPolicy": selected}, self.value)
        run = runtime.create_run(project_root=self.root, agent_id="work-task", actor="main", request=b"fixture", session=session)
        self.assertEqual(run["workingDirectory"], str(self.path))
        self.assertEqual(run["runtimeBinding"]["projectRoot"], str(self.root))
        self.assertEqual(run["taskWorkspace"]["id"], self.value["id"])
        self.assertTrue(Path(run["statePath"]).is_relative_to(Path(runtime.runtime_paths.resolve(self.root)["runtimeRoot"])))

    def test_crash_after_target_update_is_recovered_without_second_merge(self):
        self.setup_task()
        (self.path / "file.txt").write_text("task")
        original_git = worktrees.git
        interrupted = False
        def crash(root, *args, **kwargs):
            nonlocal interrupted
            result = original_git(root, *args, **kwargs)
            if str(root) == str(self.root) and args[:2] == ("merge", "--ff-only") and not interrupted:
                interrupted = True
                raise OSError("fixture crash after target update")
            return result
        with mock.patch.object(worktrees, "git", side_effect=crash):
            with self.assertRaises(OSError): self.integrate()
        merged = self.git("rev-parse", "HEAD")
        self.assertEqual(self.integrate()["status"], "complete")
        self.assertEqual(self.git("rev-parse", "HEAD"), merged)

    def test_unchecked_target_uses_managed_checkout_preserves_dirty_source(self):
        self.git("checkout", "-b", "source")
        (self.root / "file.txt").write_text("source-only dirty changes")
        before = self.git("status", "--porcelain")
        self.setup_task()
        (self.path / "file.txt").write_text("task result")
        self.assertEqual(self.integrate()["status"], "complete")
        self.assertEqual(self.git("branch", "--show-current"), "source")
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.git("show", "main:file.txt"), "task result")
        self.assertTrue(self.unit["integrationCleaned"])
        self.assertFalse(Path(self.unit["integrationDirectory"]).exists())

    def test_no_change_task_does_not_identify_unrelated_commit_for_rollback(self):
        self.setup_task()
        before = self.git("rev-parse", "HEAD")
        self.receipt["changedPaths"] = []
        self.integrate()
        self.assertTrue(self.unit["noChanges"])
        self.assertIsNone(self.unit["mergeCommit"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)



if __name__ == "__main__":
    unittest.main()
