"""Orchestrator-mode Main tool guard shared by the Codex and Antigravity hooks."""
import runtime_test_home
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
        self.config = {"pluginRoot": PLUGIN, "writeRoot": self.run_directory}

    def decide(self, event, armed=True):
        environment = {guard.ENV: json.dumps(self.config)} if armed else {}
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

    def test_scripts_of_any_installed_plugin_copy(self):
        with tempfile.TemporaryDirectory() as copy:
            (Path(copy) / "skills" / "agent").mkdir(parents=True)
            (Path(copy) / "skills" / "agent" / "SKILL.md").write_text("x")
            (Path(copy) / "scripts").mkdir()
            (Path(copy) / "scripts" / "exec.py").write_text("x")
            self.assertTrue(self.bash(f"python3 {copy}/scripts/exec.py submit"))
            self.assertFalse(self.bash(f"python3 {copy}/other/exec.py"))
        self.assertFalse(self.bash("python3 /tmp/scripts/evil.py"))

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


class ProviderWiringTests(unittest.TestCase):
    state = {"role": "main", "taskMode": "orchestrate", "statePath": "/tmp/agents/main/runs/run-1/state.json"}

    def test_codex_hook_only_for_orchestrate_main(self):
        command, environment = codex_policy.app_server({"codex": "codex"}, self.state)
        self.assertIn("-c", command)
        self.assertIn(guard.HOOK_COMMAND.replace('"', '\\"'), command[command.index("-c") + 1])
        self.assertEqual(json.loads(environment[guard.ENV])["writeRoot"], "/tmp/agents/main/runs/run-1")
        for state in ({**self.state, "taskMode": "direct"}, {**self.state, "role": "work", "taskMode": "work"}):
            with mock.patch.dict(os.environ, {guard.ENV: "stale"}):
                command, environment = codex_policy.app_server({"codex": "codex"}, state)
            self.assertNotIn("-c", command)
            self.assertNotIn(guard.ENV, environment)
        self.assertNotEqual(codex_policy.guard_signature(self.state, {}), codex_policy.guard_signature({**self.state, "taskMode": "direct"}, {}))

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
