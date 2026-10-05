"""Orchestrator-mode Main tool guard shared by the Codex and Antigravity hooks."""
import runtime_test_home  # noqa: F401 - imported for its side effect: isolates the runtime home
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tasks import orchestrator_guard as guard
from adapters.codex import policy as codex_policy
from adapters.antigravity import command as agy_command

PLUGIN = str(guard.PLUGIN_ROOT)
SCRIPT = f"python3 {PLUGIN}/scripts/loop.py start --task-mode work"


class GuardDecisionTests(unittest.TestCase):
    def setUp(self):
        self.run_directory = tempfile.mkdtemp()
        self.config = {"pluginRoots": [PLUGIN], "writeRoot": self.run_directory}

    def decide(self, event, armed=True, config=None):
        environment = {guard.ENV: json.dumps(config or self.config)} if armed else {}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch("sys.stdin", io.StringIO(json.dumps(event))):
            if not armed:
                os.environ.pop(guard.ENV, None)
            output = io.StringIO()
            with redirect_stdout(output):
                guard.main()
        text = output.getvalue().strip()
        if "toolCall" in event:
            return json.loads(text)["decision"] == "allow"
        return not text

    def bash(self, command):
        return self.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"})

    def test_codex_shell_allows_reads_and_plugin_scripts_only(self):
        for command in ("cat README.md", "sed -n '1,20p' a.py", "rg foo | head -5", "git status --short",
                        "git log --oneline -3", "ls -la", SCRIPT, f"bash -lc 'cat {PLUGIN}/README.md'"):
            self.assertTrue(self.bash(command), command)
        for command in ("touch a", "rm -rf x", "git commit -m x", "git add .", "git status && rm x", "cat a > b",
                        "sed -i s/a/b/ f", "find . -delete", SCRIPT + " > out", SCRIPT + " | tee x", "echo $(id)",
                        "python3 other.py", "FOO=1 cat a", "npm install", "git branch -D main", "git diff --output=x"):
            self.assertFalse(self.bash(command), command)

    def test_reported_bypasses_are_denied(self):
        # Code review run-20261005T113416561608Z-46bbdf1f: each of these wrote or executed through an allowed command.
        for command in ("command rm -rf src", "command -v git; rm x",
                        "sed -n '1e touch pwned' f", "sed 's/a/b/w out.txt' f", "sed -e 's/a/b/e' f",
                        "sed -n -e 'p' -e '1w out' f", "sed --expression='1e id' f", "sed 'w out' f",
                        "sed '1r /etc/passwd' f", "sed -f script.sed f", "sed 's/[/]/x/w out' f",
                        "git grep --open-files-in-pager=touch x", "git grep -O touch x", "git grep -lOtouch x",
                        "git grep --open=touch x",
                        "sort -o out.txt f", "sort f -o out.txt", "sort --output=out f", "sort --out=out f",
                        "sort --compress-program=sh f", "sort --comp=sh f", "sort -T /tmp f",
                        "find . -fprint0 out", "find . -fprint out", "find . -fprintf out %p", "find . -fls out",
                        "find . -exec rm {} ;", "find . -execdir id ;", "find . -ok rm {} ;", "find . -delete",
                        "rg --pre bash x", "rg --pre=bash x", "rg --hostname-bin=id x"):
            self.assertFalse(self.bash(command), command)

    def test_same_class_bypasses_in_other_commands_are_denied(self):
        for command in ("uniq in.txt out.txt", "tree -o out", "tree -R -H . -o out", "file -C -m magic",
                        "git branch --set-upstream-to=origin/main", "git branch --set-up=x", "git branch new",
                        "git branch --delete x", "git log --o=x", "git -c core.pager=sh log",
                        "/usr/bin/git status", "./cat x", "python3 -c 1", "bash -c 'cat a' | tee x"):
            self.assertFalse(self.bash(command), command)

    def test_shell_expansions_that_hide_options_are_denied(self):
        # Each turns into `--pre=bash` (or hides it) in the shell while the guard would read another word.
        for command in ("rg a#b --pre=bash f", "rg x # --pre=bash", "rg x ${HOME:+--pre=bash}", "rg x \"${X}\"",
                        "rg x $'\\x2d-pre=bash'", "rg x *", "rg x {--pre=bash,f}", "rg x --pr[e]=bash",
                        "rg x {=''},--pre=bash}", "rg --pr\\\ne=bash x", "cat a\nrm b", "cat 'unclosed"):
            self.assertFalse(self.bash(command), command)

    def test_read_only_forms_stay_allowed(self):
        for command in ("sed 's/a/b/g' f", "sed -n '/foo/,/bar/p' f", "sed -n -e '1p' -e '$p' f",
                        "sed -E 's/(a)[0-9]+/\\1/;s|x|y|2' f", "sed -n '5,10{p;}' f", "sed -n '\\|a|Ip' f",
                        "sed -n '/^[[:alpha:]_]/p' f", "sed '0,/x/d' f", "sed -n '$=' f", "sed 'y/abc/xyz/' f",
                        "sort -rn -k2,2 f", "sort -t: -k1 f | uniq -c", "sort -u f", "uniq -c f",
                        "find . -name '*.py' -type f -maxdepth 2", "find . -mtime -1 -print", "find . ! -path './.git/*'",
                        "git branch", "git branch -vv", "git branch -a", "git branch --show-current",
                        "git branch --list 'feat*'", "git diff --stat HEAD~1", "git diff --name-only -- a",
                        "git grep -n -o foo", "git log --format=%h -- a", "git log HEAD@{1} -1", "git show HEAD:a.py",
                        "git blame -L 1,5 a.py", "git rev-parse --show-toplevel", "git ls-files -o",
                        "command -v git", "tree -L 2 -I node_modules", "file -b --mime-type a", "rg -n 'TODO' runtime",
                        "rg -g '*.py' -l foo", "ls ./*.py", "ls runtime/*.py", "jq '.a' f.json", "wc -l f", "echo a#b",
                        "printf '%s\\n' a", "grep -rn foo .", "cat ~/x", "date +%F", f"python3 {PLUGIN}/scripts/exec.py status"):
            self.assertTrue(self.bash(command), command)

    def test_plugin_scripts_only_from_the_armed_roots(self):
        with tempfile.TemporaryDirectory() as copy:
            # A directory shaped like a plugin (as Main could make in its run directory) is not a plugin root.
            (Path(copy) / "skills" / "agent").mkdir(parents=True)
            (Path(copy) / "skills" / "agent" / "SKILL.md").write_text("x")
            (Path(copy) / "scripts").mkdir()
            (Path(copy) / "scripts" / "exec.py").write_text("x")
            self.assertFalse(self.bash(f"python3 {copy}/scripts/exec.py submit"))
            self.config = {**self.config, "pluginRoots": [PLUGIN, copy]}
            self.assertTrue(self.bash(f"python3 {copy}/scripts/exec.py submit"))
            self.assertFalse(self.bash(f"python3 {copy}/other/exec.py"))
            self.assertFalse(self.bash(f"python3 {copy}/scripts/../skills/x.py"))
        self.assertFalse(self.bash("python3 /tmp/scripts/evil.py"))

    def test_arming_fixes_this_copy_and_codex_installed_copies(self):
        with tempfile.TemporaryDirectory() as codex_home:
            installed = Path(codex_home) / "plugins" / "cache" / "market" / "agent-factory" / "1.0.0"
            (installed / "skills" / "agent").mkdir(parents=True)
            (installed / "skills" / "agent" / "SKILL.md").write_text("x")
            (installed / "scripts").mkdir()
            (installed / "scripts" / "exec.py").write_text("x")
            (Path(codex_home) / "plugins" / "cache" / "market" / "agent-factory" / "partial").mkdir()
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                config = json.loads(guard.environment({"statePath": f"{self.run_directory}/state.json"})[guard.ENV])
        self.assertEqual(config, {"pluginRoots": [os.path.realpath(PLUGIN), os.path.realpath(installed)],
                                  "writeRoot": self.run_directory})

    def test_codex_patches_only_inside_run_directory(self):
        inside = f"*** Begin Patch\n*** Add File: {self.run_directory}/task.md\n+x\n*** End Patch"
        outside = "*** Begin Patch\n*** Update File: /tmp/project/a.py\n@@\n-a\n+b\n*** End Patch"
        self.assertTrue(self.decide({"tool_name": "apply_patch", "tool_input": {"command": inside}, "cwd": "/tmp"}))
        self.assertFalse(self.decide({"tool_name": "apply_patch", "tool_input": {"command": outside}, "cwd": "/tmp"}))

    def test_antigravity_tools(self):
        call = lambda name, **args: {"toolCall": {"name": name, "args": args}, "workspacePaths": ["/tmp"]}
        self.assertTrue(self.decide(call("view_file", AbsolutePath="/etc/hosts")))
        self.assertTrue(self.decide(call("run_command", CommandLine=SCRIPT, Cwd="/tmp")))
        self.assertTrue(self.decide(call("write_to_file", TargetFile=f"{self.run_directory}/request.md")))
        self.assertFalse(self.decide(call("write_to_file", TargetFile="/tmp/project/a.py")))
        self.assertFalse(self.decide(call("run_command", CommandLine="touch a", Cwd="/tmp")))
        self.assertFalse(self.decide(call("generate_image", Prompt="x")))
        self.assertFalse(self.decide(call("search_web", query="x")))

    def test_unarmed_guard_allows_everything(self):
        self.assertTrue(self.decide({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, armed=False))
        self.assertTrue(self.decide({"toolCall": {"name": "write_to_file", "args": {"TargetFile": "/x"}}}, armed=False))
        self.assertTrue(self.decide({"tool_name": "spawn_agent", "tool_input": {"message": "x"}}, armed=False))

    def test_main_rules_ignore_the_sub_agent_tools_the_shared_matcher_adds(self):
        # The matcher now also routes sub-agent tools here; Main's decision for them stays "allow".
        for tool in ("spawn_agent", "multi_agent_v1resume_agent", "multi_agent_v1wait_agent"):
            self.assertTrue(self.decide({"tool_name": tool, "tool_input": {"message": "x"}, "cwd": "/tmp"}), tool)


class WorkDecisionTests(unittest.TestCase):
    """Codex Work arming: no sub-agent can be started; everything else is the run's own business."""
    config = json.loads(guard.WORK_ENVIRONMENT[guard.ENV])
    decide = GuardDecisionTests.decide

    def work(self, tool, **arguments):
        return self.decide({"tool_name": tool, "tool_input": arguments, "cwd": "/tmp"})

    def test_work_denies_every_sub_agent_start(self):
        # Codex exposes no read-only exploration type, so naming one changes nothing.
        for tool, arguments in (("spawn_agent", {"message": "review my work"}),
                                ("spawn_agent", {"message": "search", "agent_type": "explorer"}),
                                ("multi_agent_v1spawn_agent", {"message": "x"}),
                                ("spawn_agents_on_csv", {"csv_path": "a.csv"}),
                                ("multi_agent_v1resume_agent", {"id": "agent-1"})):
            self.assertFalse(self.work(tool, **arguments), tool)

    def test_work_keeps_its_own_tools(self):
        for tool, arguments in (("Bash", {"command": "rm -rf build && git commit -m x"}),
                                ("apply_patch", {"command": "*** Begin Patch\n*** Add File: /tmp/project/a.py\n+x\n*** End Patch"}),
                                ("multi_agent_v1wait_agent", {"targets": []}), ("mcp__docs__search", {"query": "x"})):
            self.assertTrue(self.work(tool, **arguments), tool)

    def test_work_denial_names_the_work_rule_and_fails_closed(self):
        environment = dict(guard.WORK_ENVIRONMENT)
        with mock.patch.dict(os.environ, environment), mock.patch("sys.stdin", io.StringIO(
                json.dumps({"tool_name": "spawn_agent", "tool_input": {}}))), redirect_stdout(io.StringIO()) as output:
            guard.main()
        decision = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual((decision["permissionDecision"], decision["permissionDecisionReason"]), ("deny", "[guard_profile_scope] " + guard.WORK_REASON))
        with mock.patch.dict(os.environ, environment), mock.patch("sys.stdin", io.StringIO('["not an event"]')), \
                redirect_stdout(io.StringIO()) as output:
            guard.main()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


class ProfileDecisionTests(unittest.TestCase):
    """Explorer and Scribe Work runs: Main's read rules, their own write root, never a sub-agent."""
    decide = GuardDecisionTests.decide

    def setUp(self):
        self.docs = tempfile.mkdtemp()
        self.explore = {"role": "work", "profile": "explore", "pluginRoots": [PLUGIN],
                        "scripts": list(guard.PROFILE_SCRIPTS["explore"]), "writeRoot": None}
        self.scribe = {**self.explore, "profile": "scribe", "scripts": list(guard.PROFILE_SCRIPTS["scribe"]),
                       "writeRoot": self.docs}

    def codex(self, config, tool, **arguments):
        return self.decide({"tool_name": tool, "tool_input": arguments, "cwd": "/tmp"}, config=config)

    def agy(self, config, name, **arguments):
        return self.decide({"toolCall": {"name": name, "args": arguments}, "workspacePaths": ["/tmp"]}, config=config)

    def patch(self, path):
        return f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch"

    def test_explorer_reads_and_writes_nothing(self):
        self.assertTrue(self.codex(self.explore, "Bash", command="git log --oneline -3"))
        self.assertTrue(self.codex(self.explore, "Bash", command=f"python3 {PLUGIN}/scripts/search_documents.py --query x"))
        for command in ("touch a", "cat a > b", f"python3 {PLUGIN}/scripts/loop.py start", f"python3 {PLUGIN}/scripts/exec.py submit",
                        f"python3 {PLUGIN}/scripts/sync_documents.py"):
            self.assertFalse(self.codex(self.explore, "Bash", command=command), command)
        self.assertFalse(self.codex(self.explore, "apply_patch", command=self.patch(f"{self.docs}/a.md")))
        self.assertFalse(self.codex(self.explore, "apply_patch", command=self.patch("/tmp/.agent-factory-run/a.md")))
        self.assertTrue(self.agy(self.explore, "search_web", query="x"))
        self.assertTrue(self.agy(self.explore, "read_url_content", Url="https://example.com"))
        self.assertTrue(self.agy(self.explore, "view_file", AbsolutePath="/etc/hosts"))
        self.assertFalse(self.agy(self.explore, "write_to_file", TargetFile=f"{self.docs}/a.md"))
        self.assertFalse(self.agy(self.explore, "generate_image", Prompt="x"))

    def test_scribe_writes_only_inside_docs_without_web(self):
        self.assertTrue(self.codex(self.scribe, "apply_patch", command=self.patch(f"{self.docs}/refined/a.md")))
        self.assertFalse(self.codex(self.scribe, "apply_patch", command=self.patch("/tmp/project/src/a.py")))
        self.assertFalse(self.codex(self.scribe, "apply_patch", command=self.patch(f"{self.docs}/../src/a.py")))
        self.assertTrue(self.codex(self.scribe, "Bash", command=f"python3 {PLUGIN}/scripts/catalog_documents.py --project-root /tmp"))
        self.assertFalse(self.codex(self.scribe, "Bash", command=f"python3 {PLUGIN}/scripts/loop.py start"))
        self.assertFalse(self.codex(self.scribe, "Bash", command=f"echo x > {self.docs}/a.md"))
        self.assertTrue(self.agy(self.scribe, "replace_file_content", TargetFile=f"{self.docs}/a.md"))
        self.assertFalse(self.agy(self.scribe, "write_to_file", TargetFile="/tmp/project/a.py"))
        self.assertFalse(self.agy(self.scribe, "search_web", query="x"))
        self.assertFalse(self.agy(self.scribe, "read_url_content", Url="https://example.com"))

    def test_profiles_never_start_sub_agents_and_name_their_rule(self):
        for config in (self.explore, self.scribe):
            for tool in ("spawn_agent", "multi_agent_v1spawn_agent", "multi_agent_v1resume_agent"):
                self.assertFalse(self.codex(config, tool, message="x"), tool)
        with mock.patch.dict(os.environ, {guard.ENV: json.dumps(self.scribe)}), mock.patch("sys.stdin", io.StringIO(
                json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}))), redirect_stdout(io.StringIO()) as output:
            guard.main()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecisionReason"],
                         "[guard_profile_scope] " + guard.PROFILE_REASONS["scribe"])

    def test_lesson_queries_do_not_authorize_writes_or_shell_expansion(self):
        for action in ("retrieve", "audit"):
            command = f"python3 {PLUGIN}/scripts/lessons.py {action} --project-root /tmp --input-json '{{}}'"
            self.assertTrue(self.codex(self.explore, "Bash", command=command))
            self.assertTrue(self.agy(self.explore, "run_command", CommandLine=command))
        for args in ("record --project-root /tmp --input-json '{}'", "audit record --project-root /tmp --input x",
                     "audit --project-r /tmp --input x", "audit --project-root /tmp --input x --input-json '{}'",
                     "audit --project-root /tmp --input-json '{}' ; touch x",
                     'retrieve --project-root "$PWD" --input x'):
            self.assertFalse(self.codex(self.explore, "Bash", command=f"python3 {PLUGIN}/scripts/lessons.py {args}"), args)
        self.assertTrue(self.codex(self.scribe, "Bash", command=f"python3 {PLUGIN}/scripts/lessons.py record --project-root /tmp --input-json '{{}}'"))

    def test_specific_reasons_distinguish_correctable_format_from_scope(self):
        for command, phrase in (("pwd\nnl -ba a.py", "each read command separately"),
                                ('cat "$PWD/a.py"', "literal absolute paths"),
                                ("cat a > b", "redirects")):
            code, reason = guard.denial_detail({"tool_name": "Bash", "tool_input": {"command": command}}, self.scribe, "scope")
            self.assertEqual(code, "guard_command_format")
            self.assertIn(phrase, reason)
        self.assertTrue(self.codex(self.scribe, "Bash", command="nl -ba a.py\n"))
        text = guard.profile_instruction({"workProfile": "scribe"}, "/tmp/project", "/tmp/worktree")
        self.assertIn("--input-json", text)
        self.assertIn("guard_command_format", text)
        self.assertIn("Scribe may use the Document CLIs", text)

    def test_scribe_root_is_docs_or_an_isolated_documents_repository(self):
        with tempfile.TemporaryDirectory() as project:
            with self.assertRaises(ValueError):
                guard.scribe_root({"projectRoot": project})
            (Path(project) / "docs").mkdir()
            self.assertEqual(guard.scribe_root({"projectRoot": project}), os.path.realpath(Path(project) / "docs"))
            with tempfile.TemporaryDirectory() as worktree:
                self.assertEqual(guard.scribe_root({"projectRoot": project, "workingDirectory": worktree}),
                                 os.path.realpath(worktree))


class ProviderWiringTests(unittest.TestCase):
    state = {"role": "main", "taskMode": "orchestrate", "statePath": "/tmp/agents/main/runs/run-1/state.json"}

    work_state = {**state, "role": "work", "taskMode": "work"}

    def test_codex_hook_only_for_orchestrate_main_and_work(self):
        command, environment = codex_policy.app_server({"codex": "codex"}, self.state)
        self.assertIn("-c", command)
        self.assertIn(guard.HOOK_COMMAND.replace('"', '\\"'), command[command.index("-c") + 1])
        self.assertEqual(json.loads(environment[guard.ENV])["writeRoot"], "/tmp/agents/main/runs/run-1")
        for state in ({**self.state, "taskMode": "direct"}, {**self.state, "role": "verification", "taskMode": "verification"}):
            with mock.patch.dict(os.environ, {guard.ENV: "stale"}):
                command, environment = codex_policy.app_server({"codex": "codex"}, state)
            self.assertNotIn("-c", command)
            self.assertNotIn(guard.ENV, environment)
        self.assertNotEqual(codex_policy.guard_signature(self.state, {}), codex_policy.guard_signature({**self.state, "taskMode": "direct"}, {}))

    def test_codex_work_and_main_share_one_hook_definition(self):
        # Codex keeps one trust hash per session-flag hook key: both roles must pass the identical definition.
        main, main_environment = codex_policy.app_server({"codex": "codex"}, self.state)
        with mock.patch.dict(os.environ, {guard.ENV: main_environment[guard.ENV]}):  # Inherited from a dispatching Main.
            work, work_environment = codex_policy.app_server({"codex": "codex"}, self.work_state)
        self.assertEqual(work, main)
        self.assertEqual(work.count("-c"), 1)
        self.assertEqual(work[-1], codex_policy.guard_hook_toml())
        self.assertRegex("spawn_agent", codex_policy.GUARD_MATCHER)
        for tool in ("Bash", "apply_patch", "multi_agent_v1spawn_agent", "multi_agent_v1resume_agent"):
            self.assertRegex(tool, codex_policy.GUARD_MATCHER)
        for tool in ("update_plan", "mcp__docs__search", "BashOutput"):
            self.assertNotRegex(tool, codex_policy.GUARD_MATCHER)
        # Only the arming variable differs, and it selects the Work rules.
        self.assertEqual(json.loads(work_environment[guard.ENV]), {"role": "work"})
        self.assertNotEqual(codex_policy.guard_signature(self.state, {}), codex_policy.guard_signature(self.work_state, {}))
        # The role may come from the session, as for runs restored from a session file.
        self.assertEqual(codex_policy.guard_environment({"taskMode": "work"}, {"role": "work"}), guard.WORK_ENVIRONMENT)
        self.assertEqual(codex_policy.guard_environment({"role": "verification"}, {}), {})

    def test_antigravity_arms_only_orchestrate_main(self):
        # Antigravity's managed agent lists no sub-agent tool; its Work runs stay unarmed.
        self.assertFalse(guard.orchestrating(self.work_state))
        self.assertTrue(guard.working(self.work_state) and not guard.working(self.state))

    def test_profiles_arm_their_own_rules_for_codex_and_antigravity(self):
        with tempfile.TemporaryDirectory() as project:
            (Path(project) / "docs").mkdir()
            session = {"role": "work", "projectRoot": project}
            for profile in ("explore", "scribe"):
                state = {**self.work_state, "workProfile": profile}
                self.assertEqual(guard.work_profile(state, session), profile)
                arming = json.loads(codex_policy.guard_environment(state, session)[guard.ENV])
                self.assertEqual((arming["role"], arming["profile"]), ("work", profile))
                self.assertEqual(arming["writeRoot"], os.path.realpath(Path(project) / "docs") if profile == "scribe" else None)
                command, _ = codex_policy.app_server({**session, "codex": "codex"}, state)
                self.assertIn(profile, codex_policy.guard_signature(state, session))
                self.assertEqual(command[-1], codex_policy.guard_hook_toml())  # One hook definition for every rule set.
            for state in (self.work_state, {**self.work_state, "workProfile": "workLight"}, {**self.state, "workProfile": "explore"}):
                self.assertIsNone(guard.work_profile(state, session))
            self.assertEqual(codex_policy.guard_environment({**self.work_state, "workProfile": "work"}, session),
                             guard.WORK_ENVIRONMENT)

    def test_codex_trust_is_written_once_and_confirmed(self):
        hook = {"source": "sessionFlags", "eventName": "preToolUse", "command": guard.HOOK_COMMAND,
                "key": "/<session-flags>/config.toml:pre_tool_use:0:0", "currentHash": "sha256:abc"}
        statuses = iter(["untrusted", "trusted"])
        calls = []

        class Rpc:
            def call(self, method, params):
                calls.append((method, params))
                if method == "hooks/list":
                    return {"data": [{"hooks": [{**hook, "trustStatus": next(statuses)}]}]}
                return {"status": "ok"}
        codex_policy.ensure_guard_trusted(Rpc(), "/tmp")
        self.assertEqual(calls[1], ("config/value/write", {"keyPath": "hooks.state", "mergeStrategy": "upsert",
                                    "value": {hook["key"]: {"trusted_hash": "sha256:abc"}}}))

    def test_antigravity_guard_plugin_install_and_prune(self):
        with tempfile.TemporaryDirectory() as home:
            directory = agy_command.install_guard(home)
            hooks = json.loads((directory / "hooks.json").read_text())
            handler = hooks["agent-factory-orchestrator-guard"]["PreToolUse"][0]["hooks"][0]
            self.assertEqual(handler["command"], guard.HOOK_COMMAND)
            stale = directory.parent / (agy_command.AGENT_PREFIX + "000000000000-guard")
            stale.mkdir()
            (stale / "plugin.json").write_text(json.dumps({"description": agy_command.GUARD_DESCRIPTION}))
            (stale / "hooks.json").write_text(json.dumps({"agent-factory-orchestrator-guard": {"PreToolUse": [
                {"hooks": [{"command": "python3 /missing/copy/orchestrator_guard.py"}]}]}}))
            agy_command.install_guard(home)
            self.assertFalse(stale.exists())
            self.assertTrue(directory.exists())


if __name__ == "__main__":
    unittest.main()
