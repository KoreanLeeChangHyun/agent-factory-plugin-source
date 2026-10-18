"""Opt-in installed Codex against a loopback-only fake Responses provider."""
import runtime_test_home
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from mock_responses_provider import MockResponsesProvider
from native_fixtures import native, native_fixture
from execution.prompts import PromptParts
from execution.usage import UsageAccumulator


class TokenDeliveryTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AF_VERIFY_LOCAL_CODEX") == "1", "installed Codex fixture opt-in")
    def test_resume_preserves_user_rules_without_repeating_fixed_prompt(self):
        codex = shutil.which("codex")
        self.assertIsNotNone(codex)
        with tempfile.TemporaryDirectory(prefix="af-token-delivery-") as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            home = root / "codex-home"
            home.mkdir()
            fixture, _, state = native_fixture(project, goal=False, existing=False)
            # No tools are emitted by the fake provider. This fixture verifies
            # transport, not OS sandbox enforcement (which has separate tests).
            fixture.session.update(model="fixture-model", sandbox="danger-full-access",
                executionPolicy=runtime_test_home.policy("danger-full-access"), nativeCapabilities={
                "instructionDelivery": True, "goal": False, "fast": False})
            with MockResponsesProvider(state["resultPath"]) as provider:
                provider.mode = "answer"
                (home / "config.toml").write_text(
                    'model = "fixture-model"\nmodel_provider = "fixture"\napproval_policy = "never"\n'
                    'developer_instructions = "USER_RULE_TOKEN_FIXTURE"\n'
                    '[analytics]\nenabled = false\n'
                    '[features]\nenable_request_compression = false\n'
                    '[model_providers.fixture]\nname = "Owned local fixture"\nbase_url = '
                    + json.dumps(provider.base_url) + '\nwire_api = "responses"\nrequires_openai_auth = false\n'
                    'supports_websockets = false\nenv_key = "AF_LOCAL_DUMMY_TOKEN"\n')
                env = {"PATH": os.environ["PATH"], "HOME": str(root), "CODEX_HOME": str(home),
                       "AF_LOCAL_DUMMY_TOKEN": "fixture-only", "LANG": "C.UTF-8"}
                session = dict(fixture.session)
                for index in range(4):
                    if index:
                        state = fixture.runtime.create_run(project_root=project, agent_id=state["agentId"],
                            actor="human", request=b"Follow-up request", session=session)
                    if index == 1:
                        # Existing native sessions/forks can outlive the delivery
                        # sidecar. Its absence must not duplicate native guidance.
                        fixture.runtime.session_file(project, state["agentId"]).with_name(
                            "instruction-delivery.json").unlink()
                    provider.result_path = state["resultPath"]
                    process = subprocess.Popen([codex, "app-server", "--listen", "stdio://"],
                        cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True)
                    timer = threading.Timer(30, process.kill)
                    timer.start()
                    try:
                        rpc = native.Rpc(process)
                        bridge = native.Bridge(fixture.runtime, session, state, rpc)
                        with redirect_stdout(io.StringIO()) as output:
                            rule = "FIXED_RULE_TOKEN_FIXTURE" if index < 2 else "REVISED_RULE_TOKEN_FIXTURE"
                            fixed = ("These are the current Agent Factory fixed instructions.\n"
                                     "<agent-factory-role-prompt>\n" + rule + "\n</agent-factory-role-prompt>")
                            bridge.run(PromptParts(fixed, f"Bounded request {index}"))
                        events = [json.loads(line) for line in output.getvalue().splitlines()]
                        usage = UsageAccumulator()
                        for event in events:
                            usage.observe(event)
                        self.assertEqual(usage.snapshot()["inputTokens"], 100)
                        self.assertEqual(usage.snapshot()["outputTokens"], 10)
                        terminal = json.loads(bridge.last_message)
                        fixture.runtime.publish_terminal_result(terminal, state)
                        self.assertEqual(Path(state["resultPath"]).read_text(), "Native answer")
                        with self.assertRaises(fixture.runtime.ContractError):
                            fixture.runtime.publish_terminal_result({**terminal,
                                "resultPath": str(root / "wrong-result.md")}, state)
                        self.assertFalse((root / "wrong-result.md").exists())
                        session["sessionId"] = bridge.thread_id
                    finally:
                        timer.cancel()
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        process.stdin.close()
                        process.stdout.close()
                self.assertEqual(provider.errors, [])
                self.assertEqual(len(provider.requests), 4)
                self.assertNotIn("const", provider.requests[0]["text"]["format"]["schema"]["properties"]["resultPath"])
                for index, request in enumerate(provider.requests):
                    self.assertEqual(provider.requests[0]["text"]["format"], request["text"]["format"])
                    text = json.dumps(request)
                    # Prior revisions remain native history; the current revision
                    # must arrive once, including after another ordinary resume.
                    self.assertEqual(text.count("USER_RULE_TOKEN_FIXTURE"), 1 if index < 2 else 2)
                    self.assertEqual(text.count("FIXED_RULE_TOKEN_FIXTURE"), 1)
                    self.assertEqual(text.count("REVISED_RULE_TOKEN_FIXTURE"), 0 if index < 2 else 1)
