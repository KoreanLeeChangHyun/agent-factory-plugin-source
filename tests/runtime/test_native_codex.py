"""Focused native transport regressions; intended for independent Verification."""
from __future__ import annotations

import runtime_test_home  # Isolate all runtime subprocesses from the real home.

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from native_fixtures import native, runtime, native_fixture


class NativeCodexTests(unittest.TestCase):
    def test_request_user_input_becomes_interview_question(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, _, _ = native_fixture(Path(directory), goal=False)
            response = bridge.handle_server_request("item/tool/requestUserInput", {"questions": [{
                "id": "interview-2-of-4", "header": "Deploy", "question": "Which deployment?",
                "options": [
                    {"label": "Staged (Recommended)", "description": "Pros: safer rollout; Cons: slower completion"},
                    {"label": "Immediate", "description": "Pros: fastest completion; Cons: larger blast radius"},
                ]
            }]})
            event = json.loads(output.getvalue())
            self.assertEqual(event["type"], "interview.question")
            self.assertEqual((event["question"]["current"], event["question"]["total"]), (2, 4))
            self.assertEqual(event["question"]["recommendedValue"], "1")
            self.assertEqual(response, {"answers": {"interview-2-of-4": {"answers": []}}})

    def test_goal_plain_text_final_is_rejected_with_stage_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, _, _ = native_fixture(Path(directory))
            bridge.goal_started = True
            bridge.goal = {"status": "complete"}
            bridge.last_message = "목표 테스트를 완료했습니다."
            with self.assertRaisesRegex(native.NativeError, "stage=finish_turn"):
                bridge.finish_turn()
            self.assertNotIn('agent_message', output.getvalue())

    @staticmethod
    def invalid_final_then_repair(rpc, state, repaired_text, first_text="작업을 완료했습니다."):
        """First turn ends in prose; the repair turn the bridge starts answers with repaired_text."""
        def turn(identity, text, goal_status="complete"):
            return [
                {"method": "turn/started", "params": {"threadId": "thread-exact", "turn": {"id": identity}}},
                {"method": "item/completed", "params": {"threadId": "thread-exact", "turnId": identity,
                                                        "item": {"type": "agentMessage", "text": text}}},
                {"method": "turn/completed", "params": {"threadId": "thread-exact", "turn": {"id": identity, "status": "completed"}},
                 "testGoalStatus": goal_status},
            ]
        rpc.events = turn("turn-0", first_text) + turn("turn-repair", repaired_text)
        original = rpc.call
        starts = []

        def call(method, params, timeout=15):
            if method == "turn/start":
                starts.append(params)
                rpc.calls.append((method, params))
                return {"turn": {"id": "turn-0" if len(starts) == 1 and not rpc.goal else "turn-repair"}}
            return original(method, params, timeout)
        rpc.call = call
        return starts

    def test_invalid_final_json_gets_one_schema_constrained_repair_turn(self):
        # Incident: Work results surfaced as native_backend_error "Native final result is not valid JSON".
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory), goal=False)
            answer = json.dumps({"status": "completed", "resultPath": state["resultPath"], "resultText": "Repaired answer"})
            starts = self.invalid_final_then_repair(rpc, state, answer)
            bridge.run("Main role")
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(starts), 2)
            repair = starts[1]
            self.assertEqual(repair["outputSchema"], runtime.safe_read_json(Path(state["responseSchemaPath"])))
            self.assertEqual(repair["threadId"], "thread-exact")
            self.assertIn("not valid JSON", repair["input"][0]["text"])
            self.assertIn("Do not use tools", repair["input"][0]["text"])
            finals = [e for e in events if e["type"] == "item.completed" and e["item"].get("type") == "agent_message"]
            self.assertEqual(len(finals), 1)
            self.assertEqual(json.loads(finals[0]["item"]["text"])["resultText"], "Repaired answer")

    def test_result_repair_is_bounded_to_one_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory), goal=False)
            starts = self.invalid_final_then_repair(rpc, state, "여전히 JSON이 아닙니다.")
            with self.assertRaisesRegex(native.NativeError, "not valid JSON"):
                bridge.run("Main role")
            self.assertEqual(len(starts), 2)
            self.assertNotIn('agent_message', output.getvalue())

    def test_goal_run_repairs_plain_text_final_without_reopening_goal(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory))
            answer = json.dumps({"status": "completed", "resultPath": state["resultPath"], "resultText": "Goal answer"})
            starts = self.invalid_final_then_repair(rpc, state, answer)
            bridge.run("Main role")
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(starts), 1)  # Goal activation starts no turn; only the repair does.
            self.assertIn("outputSchema", starts[0])
            final = json.loads(next(e for e in events if e["type"] == "item.completed"
                                    and e["item"].get("type") == "agent_message")["item"]["text"])
            self.assertEqual((final["status"], final["resultText"]), ("completed", "Goal answer"))
            self.assertEqual(rpc.goal["status"], "complete")
            self.assertEqual(sum(method == "thread/goal/set" and params.get("status") == "active"
                                 for method, params in rpc.calls), 1)

    def test_invalid_result_envelope_is_repaired_with_the_contract_reason(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory), goal=False)
            answer = json.dumps({"status": "completed", "resultPath": state["resultPath"], "resultText": "Bound answer"})
            wrong_path = json.dumps({"status": "completed", "resultPath": "/tmp/elsewhere.md", "resultText": "x"})
            starts = self.invalid_final_then_repair(rpc, state, answer, first_text=wrong_path)
            bridge.run("Main role")
            self.assertIn("invalid terminal result", starts[1]["input"][0]["text"])
            self.assertIn("Bound answer", output.getvalue())

    RECEIPT_FIELDS = {"outcome": "completed", "changedPaths": ["src/a.py"],
                      "tests": {"run": True, "reason": "unit tests passed"}, "addressedFindingIds": []}

    def goal_work(self, directory, first, repaired=None, **run_options):
        """A Codex Goal Work run whose Goal turn ends with `first`; a started extra turn answers `repaired`."""
        bridge, rpc, state = native_fixture(Path(directory), role="work", **run_options)

        def text(fields):
            return json.dumps({"status": "completed", "resultPath": state["resultPath"], "resultText": "Work answer",
                               "decisionKind": None, **fields})
        starts = self.invalid_final_then_repair(rpc, state, text(repaired or {}), first_text=text(first))
        return bridge, rpc, state, starts

    @staticmethod
    def final_result(output):
        finals = [json.loads(line) for line in output.getvalue().splitlines()]
        finals = [e for e in finals if e["type"] == "item.completed" and e["item"].get("type") == "agent_message"]
        return [json.loads(e["item"]["text"]) for e in finals]

    def test_goal_work_final_carrying_the_receipt_fields_needs_no_extra_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state, starts = self.goal_work(directory, self.RECEIPT_FIELDS)
            self.assertEqual(state["responseContract"], 2)
            bridge.run("Work role")
            self.assertEqual(starts, [])  # Goal activation starts no turn and the contract was met.
            # The Goal turn got the receipt fields through its instructions, not a per-turn schema.
            resume = [params for method, params in rpc.calls if method == "thread/resume"][-1]
            self.assertIn('"addressedFindingIds"', resume["developerInstructions"])
            (final,) = self.final_result(output)
            self.assertEqual({key: final[key] for key in self.RECEIPT_FIELDS}, self.RECEIPT_FIELDS)
            runtime.publish_terminal_result(final, state)
            receipt = runtime.publish_structured_receipt(Path(directory), state, final,
                                                         agent_id=state["agentId"], run_id=state["runId"])
            self.assertEqual(receipt["changedPaths"], ["src/a.py"])

    def test_goal_work_final_without_receipt_fields_gets_one_schema_constrained_turn(self):
        malformed = {**self.RECEIPT_FIELDS, "tests": "unit tests passed"}
        for first, reason in (({}, "omitted the receipt fields: outcome, changedPaths, tests, addressedFindingIds"),
                              ({"outcome": "completed"}, "omitted the receipt fields: changedPaths, tests"),
                              (malformed, "do not match the output schema: tests")):
            with self.subTest(first=sorted(first)), tempfile.TemporaryDirectory() as directory, \
                    redirect_stdout(io.StringIO()) as output:
                bridge, rpc, state, starts = self.goal_work(directory, first, self.RECEIPT_FIELDS)
                bridge.run("Work role")
                self.assertEqual(len(starts), 1)
                schema = runtime.safe_read_json(Path(state["responseSchemaPath"]))
                self.assertEqual(starts[0]["outputSchema"], schema)
                self.assertLessEqual(set(self.RECEIPT_FIELDS), set(schema["required"]))
                request = starts[0]["input"][0]["text"]
                self.assertIn(reason, request)
                self.assertIn("Do not use tools", request)
                self.assertIn("own checks you actually ran", request)
                (final,) = self.final_result(output)
                self.assertEqual({key: final[key] for key in self.RECEIPT_FIELDS}, self.RECEIPT_FIELDS)
                self.assertEqual(rpc.goal["status"], "complete")

    def test_goal_work_still_lacking_receipt_fields_is_returned_for_receipt_recovery(self):
        # One extra turn only; the runtime never fills the values, so the run ends as receipt_missing.
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state, starts = self.goal_work(directory, {}, {"outcome": "completed"})
            bridge.run("Work role")
            self.assertEqual(len(starts), 1)
            (final,) = self.final_result(output)
            self.assertNotIn("tests", final)
            with self.assertRaises(runtime.ContractError) as raised:
                runtime.publish_structured_receipt(Path(directory), state, final,
                                                   agent_id=state["agentId"], run_id=state["runId"])
            self.assertEqual(raised.exception.code, "receipt_missing")
            self.assertFalse(Path(state["receiptPath"]).exists())

    def test_goal_work_without_a_completed_result_or_under_the_file_contract_gets_no_receipt_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory), role="work")
            failed = json.dumps({"status": "failed", "resultPath": state["resultPath"], "resultText": "Blocked"})
            starts = self.invalid_final_then_repair(rpc, state, failed, first_text=failed)
            bridge.run("Work role")
            self.assertEqual(starts, [])
            self.assertEqual(self.final_result(output)[0]["status"], "failed")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            # A run created under contract 1 finishes under contract 1: the Agent writes receipt.json.
            bridge, rpc, state, starts = self.goal_work(directory, {}, response_contract=1)
            self.assertNotIn("responseContract", state)
            bridge.run("Work role")
            self.assertEqual(starts, [])
            self.assertEqual(self.final_result(output)[0]["status"], "completed")

    def test_work_run_requires_the_trusted_guard_hook_before_any_thread(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory), role="work")
            original = rpc.call

            def call(method, params, **kwargs):
                if method == "hooks/list":
                    rpc.calls.append((method, params))
                    return {"data": [{"hooks": [{"source": "sessionFlags", "eventName": "preToolUse", "key": "k",
                                                 "currentHash": "sha256:new", "trustStatus": "untrusted",
                                                 "command": native.codex_policy.orchestrator_guard.HOOK_COMMAND}]}]}
                if method == "config/value/write":
                    rpc.calls.append((method, params))
                    return {}
                return original(method, params, **kwargs)
            rpc.call = call
            with self.assertRaisesRegex(RuntimeError, "did not trust the Agent Factory guard hook"):
                bridge.run("Work role")
            methods = [method for method, _ in rpc.calls]
            self.assertIn("config/value/write", methods)
            self.assertFalse({"thread/start", "thread/resume", "turn/start"} & set(methods))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory), goal=False)  # Worker-mode Main carries no hook.
            bridge.run("Main role")
            self.assertNotIn("hooks/list", [method for method, _ in rpc.calls])

    def test_codex_capabilities_report_that_work_starts_no_sub_agent(self):
        probed = {"schemaVersion": "0.1.0", "kind": "execution-capabilities", "diagnostic": None}
        for delivery, expected in ((True, "none"), (False, None)):
            fields = {"model": True, "reasoning": True, "fast": True, "goal": True, "plan": True,
                      "instructionDelivery": delivery}
            with mock.patch.object(native, "inspect_capabilities",
                                   return_value={**probed, "submit": dict(fields), "send": dict(fields)}):
                capabilities = runtime.adapters.adapter("codex").inspect_capabilities("codex")
            for operation in ("submit", "send"):
                # The legacy exec backend carries no hook, so it advertises nothing.
                self.assertEqual(capabilities[operation].get("workSubagents"), expected)

    def test_goal_contract_injection_failure_prevents_activation(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory))
            original = rpc.call
            def call(method, params, **kwargs):
                if method == 'thread/inject_items':
                    raise native.NativeError('contract injection unavailable')
                return original(method, params, **kwargs)
            rpc.call = call
            with self.assertRaisesRegex(native.NativeError, 'contract injection unavailable'):
                bridge.setup('current request')
            self.assertEqual(rpc.goal['status'], 'paused')

    def test_turn_start_includes_file_backed_local_image(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory), goal=False)
            state["imageInputs"] = [{"path": str(Path(directory) / "image.png"), "mediaType": "image/png"}]
            bridge = native.Bridge(runtime, bridge.session, state, rpc)
            bridge.setup("bounded Main")
            turn = next(params for method, params in rpc.calls if method == "turn/start")
            self.assertIn({"type": "localImage", "path": state["imageInputs"][0]["path"]}, turn["input"])

    def test_goal_activation_never_silently_drops_local_image(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory), goal=True)
            state["imageInputs"] = [{"path": str(Path(directory) / "image.png"), "mediaType": "image/png"}]
            bridge = native.Bridge(runtime, bridge.session, state, rpc)
            with self.assertRaisesRegex(native.NativeError, "does not support local image"):
                bridge.setup("bounded Main")

    def test_explicit_false_and_inherit_are_distinct_on_submit_and_send(self):
        for command in ("submit", "send"):
            prefix = [command, "--agent", "main-test", "--message", "hi"]
            if command == "submit":
                prefix += ["--role", "main"]
            self.assertEqual(runtime.requested_execution(runtime.parse_args(prefix)), {})
            self.assertEqual(runtime.requested_execution(runtime.parse_args(prefix + ["--no-fast", "--no-goal-mode"])), {"fast": False, "goalMode": False})
            self.assertEqual(runtime.requested_execution(runtime.parse_args(prefix + ["--fast", "--goal-mode"])), {"fast": True, "goalMode": True})

    def test_fast_uses_advertised_tier_and_off_clears_inherited_tier(self):
        models = [{"model": "m", "serviceTiers": [{"id": "accelerated", "name": "Fast"}]}]
        self.assertEqual(native.service_tier(models, "m", True), "accelerated")
        self.assertEqual(native.service_tier(models, "m", False), "default")
        self.assertIsNone(native.service_tier(models, "m", None))
        with self.assertRaisesRegex(native.NativeError, "does not advertise"):
            native.service_tier([{"model": "m", "serviceTiers": []}], "m", True)

    def test_resume_preserves_exact_identity_and_applies_model_reasoning_and_false(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory), fast=False, goal=False)
            bridge.setup("role and request")
            resume = next(params for method, params in rpc.calls if method == "thread/resume")
            turn = next(params for method, params in rpc.calls if method == "turn/start")
            self.assertEqual(resume["threadId"], "thread-exact")
            self.assertNotIn("sandbox", resume)
            self.assertTrue(resume["permissions"].startswith("agent_factory_run_"))
            self.assertEqual(turn["serviceTier"], "default")
            self.assertEqual(turn["model"], "model-one")
            self.assertEqual(turn["effort"], "high")
            self.assertEqual(resume["developerInstructions"], "role and request")
            self.assertIn("outputSchema", turn)

    def test_wrong_resumed_thread_fails_before_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory), mismatch=True)
            with self.assertRaisesRegex(native.NativeError, "different session"):
                bridge.setup("role")
            self.assertFalse(any(method == "turn/start" for method, _ in rpc.calls))

    def test_native_continuation_does_not_complete_goal_on_ordinary_turn_end(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory), statuses=("active", "complete"))
            bridge.run("Main role")
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(sum(method == "turn/start" for method, _ in rpc.calls), 0)
            self.assertEqual(sum(e["type"] == "turn.completed" for e in events), 2)
            self.assertEqual(sum(e["type"] == "item.completed" for e in events), 1)
            self.assertIn("goal.continuing", [e["type"] for e in events])
            self.assertEqual(runtime.safe_read_json(Path(state["statePath"]))["goal"]["status"], "complete")

    def test_paused_blocked_and_limited_goals_are_not_run_completion(self):
        for status in ("paused", "blocked", "usageLimited", "budgetLimited"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
                bridge, _, _ = native_fixture(Path(directory), statuses=(status,))
                bridge.run("Main role")
                final = json.loads(json.loads(output.getvalue().splitlines()[-1])["item"]["text"])
                self.assertEqual(final["status"], "needs-human-decision" if status in {"paused", "blocked"} else "failed")

    def test_pause_and_clear_are_native_controls_with_no_model_turn(self):
        for action in ("pause", "cancel", "disable"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                bridge, rpc, _ = native_fixture(Path(directory), action=action)
                rpc.goal = {"threadId": "thread-exact", "objective": "existing", "status": "active", "tokensUsed": 123, "timeUsedSeconds": 2}
                bridge.run("Main role")
                self.assertFalse(any(method == "turn/start" for method, _ in rpc.calls))
                if action == "pause":
                    self.assertEqual(rpc.goal["status"], "paused")
                    self.assertEqual(rpc.goal["tokensUsed"], 123)
                else:
                    self.assertIsNone(rpc.goal)

    def test_explicit_reopen_preserves_objective_and_usage(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory), action="reopen")
            state.pop("goalObjective")
            rpc.goal = {"threadId": "thread-exact", "objective": "existing", "status": "paused", "tokensUsed": 123, "timeUsedSeconds": 2}
            bridge.setup("Main role")
            self.assertEqual(rpc.goal["status"], "active")
            self.assertEqual(rpc.goal["objective"], "existing")
            self.assertEqual(rpc.goal["tokensUsed"], 123)
            mutation = next(params for method, params in rpc.calls if method == "thread/goal/set")
            self.assertNotIn("objective", mutation)

    def test_native_error_pauses_goal_and_remains_an_error(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory))
            rpc.events = [{"method": "error", "params": {"error": {"message": "quota"}, "willRetry": False}}]
            with self.assertRaisesRegex(native.NativeError, "quota"):
                bridge.run("Main role")
            self.assertEqual(rpc.goal["status"], "paused")

    def test_cancel_request_pauses_before_return(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory))
            runtime.update_json(Path(state["statePath"]), Path(state["statePath"]).parent / ".state.lock", lambda value: value.update({"cancelRequested": True}))
            bridge.run("Main role")
            self.assertEqual(rpc.goal["status"], "paused")
            self.assertTrue(any(method == "turn/interrupt" for method, _ in rpc.calls))

    def test_off_clears_existing_native_goal_before_the_next_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, _ = native_fixture(Path(directory), goal=False)
            rpc.goal = {"threadId": "thread-exact", "objective": "existing", "status": "active", "tokensUsed": 10, "timeUsedSeconds": 1}
            bridge.setup("ordinary request")
            self.assertIsNone(rpc.goal)
            methods = [method for method, _ in rpc.calls]
            self.assertLess(methods.index("thread/goal/clear"), methods.index("turn/start"))

    def test_inheriting_paused_goal_does_not_reopen_it(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            bridge, rpc, state = native_fixture(Path(directory))
            state.pop("goalObjective")
            rpc.goal = {"threadId": "thread-exact", "objective": "existing", "status": "paused", "tokensUsed": 10, "timeUsedSeconds": 1}
            bridge.setup("ordinary follow-up")
            self.assertEqual(rpc.goal["status"], "paused")
            self.assertFalse(any(method == "thread/goal/set" for method, _ in rpc.calls))

    def test_live_pause_uses_rpc_owner_and_acknowledges_control(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, state = native_fixture(Path(directory))
            path = Path(state["statePath"])
            runtime.update_json(path, path.parent / ".state.lock", lambda value: value.update({"goalControl": {"id": "human-one", "action": "pause"}}))
            bridge.run("Main role")
            self.assertEqual(rpc.goal["status"], "paused")
            self.assertIn('"controlId": "human-one"', output.getvalue())
            self.assertTrue(any(method == "turn/interrupt" for method, _ in rpc.calls))

    def test_terminal_goal_notification_after_turn_end_finishes_without_an_extra_turn(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, _ = native_fixture(Path(directory), statuses=("active",))
            rpc.events.append({"method": "thread/goal/updated", "params": {"threadId": "thread-exact", "goal": {
                "threadId": "thread-exact", "objective": "finish", "status": "complete", "tokensUsed": 23, "timeUsedSeconds": 3}}})
            bridge.run("Main role")
            self.assertEqual(sum(method == "turn/start" for method, _ in rpc.calls), 0)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["type"], "item.completed")

    def test_goal_rejected_for_verification_before_process_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            args = runtime.parse_args(["submit", "--project-root", directory, "--agent", "verify-test", "--role", "verification", "--task-mode", "verification", "--message", "hi", "--goal-mode"])
            with mock.patch.object(runtime, "resolve_project_root", return_value=Path(directory)), mock.patch.object(runtime, "spawn_worker") as spawn:
                with self.assertRaisesRegex(runtime.ContractError, "Verification cannot"):
                    runtime.submit(args, True)
                spawn.assert_not_called()

    def test_failed_capability_inspection_is_actionable_not_wrapper_flag_detection(self):
        with mock.patch.object(native.subprocess, "run", side_effect=FileNotFoundError("codex missing")):
            capabilities = native.inspect_capabilities("missing")
        self.assertFalse(capabilities["submit"]["fast"])
        self.assertFalse(capabilities["send"]["goal"])
        self.assertIn("Update/select Codex", capabilities["diagnostic"])

    def test_dispatch_receipt_identity_includes_explicit_execution_options(self):
        with tempfile.TemporaryDirectory() as directory:
            state = runtime.create_run(project_root=Path(directory), agent_id="work-one", actor="main", request=b"hi",
                session={"role": "work", "maxAttempts": 1}, dispatch_id="dispatch-options", dispatch_operation="submit",
                execution_options={"fast": False, "model": "m", "reasoningEffort": "high"})
            self.assertEqual(state["dispatchTuple"]["executionOptions"], {"fast": False, "model": "m", "reasoningEffort": "high"})
            self.assertEqual(state["executionOptions"], state["dispatchTuple"]["executionOptions"])


if __name__ == "__main__":
    unittest.main()
