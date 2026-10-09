"""Single-owner handoff, stale reads, delayed results and repeated alternation."""
import json
import io
import tempfile
from pathlib import Path
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock
import runtime_test_home
import unittest

from execution.handoff import configure, transition, authorized
from execution.usage import context_observation
from storage.errors import ContractError
from native_fixtures import runtime
from execution.handoff import command, accept_run, observe_runtime, snapshot, check_dispatch, finish_run, begin_preparation


class HandoffTests(unittest.TestCase):
    def test_repeated_persisted_transfer_catches_queue_and_late_children(self):
        for prepare, target in ((60, 80), (70, 85), (80, 90)):
            with self.subTest(prepare=prepare), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                reference = root / "original.json"
                reference.write_text('{"goal":"retain decisions and approvals"}')
                def allocate(agent, native):
                    session = dict(agentId=agent, role="main", projectRoot=str(root), sessionId=native,
                        provider="codex", model="fixed-model", reasoningEffort="high", fast=False,
                        executionPolicy=runtime_test_home.policy("danger-full-access"), humanApprovalPolicy="bypass",
                        maxAttempts=1, handoffExperiment=True)
                    runtime.atomic_write_json(runtime.agent_directory(root, agent, create=True) / "session.json", session)
                    return session
                main = allocate("main-a", "thread-0")
                allocate("donor-0", "thread-1")
                def apply(action, payload=None):
                    path = root / "control.json"
                    path.write_text(json.dumps(payload or {}))
                    with redirect_stdout(io.StringIO()):
                        command(runtime, Namespace(project_root=root, agent="main-a", action=action, input=path))
                    return runtime.load_session(root, "main-a")
                main = apply("configure", dict(donorAgentId="donor-0", preparePercent=prepare,
                    targetPercent=target, maxAgeSeconds=60, references=[str(reference)]))
                for cycle in range(6):
                    main = runtime.load_session(root, "main-a")
                    state = main["handoff"]
                    binding = dict(slot=state["owner"], epoch=state["epoch"])
                    run = runtime.create_run(project_root=root, agent_id="main-a", actor="human",
                        request=f"input-{cycle}".encode(), session=main)
                    accept_run(runtime, root, main, run)
                    observe_runtime(runtime, root, run, {"type":"turn.started", "turn_id":f"turn-{cycle}"})
                    observe_runtime(runtime, root, run, {"type":"item.started", "item":{"id":"tool", "type":"command_execution"}})
                    observe_runtime(runtime, root, run, {"type":"item.started", "item":{"id":"tool", "type":"command_execution"}})
                    with self.assertRaises(ContractError):
                        apply("switch", binding)
                    observe_runtime(runtime, root, run, {"type":"item.completed", "item":{"id":"tool", "type":"command_execution"}})
                    observe_runtime(runtime, root, run, {"type":"item.completed", "item":{"id":"tool", "type":"command_execution"}})
                    observe_runtime(runtime, root, run, {"type":"provider.context", "usedTokens":target,
                        "contextWindowTokens":100, "session_id":main["sessionId"], "turn_id":f"turn-{cycle}", "observedAt":runtime.now()})
                    observe_runtime(runtime, root, run, {"type":"turn.completed", "turn_id":f"turn-{cycle}"})
                    runtime.atomic_write(Path(run["resultPath"]), f"answer-{cycle}".encode())
                    runtime.mark_terminal(Path(run["statePath"]), "completed")
                    donor_id = f"donor-{cycle}"
                    donor = runtime.load_session(root, donor_id) if cycle == 0 else allocate(donor_id, f"thread-{cycle+1}")
                    check_dispatch(donor, preparation_for="main-a", runtime=runtime, root=root)
                    begin_preparation(runtime, root, donor, "main-a")
                    def prepare_receipt():
                        current = runtime.load_session(root, "main-a")["handoff"]
                        prepared = runtime.create_run(project_root=root, agent_id=donor_id, actor="main",
                            request=b"read originals", session=donor)
                        runtime.atomic_write(Path(prepared["resultPath"]), json.dumps(snapshot(current)).encode())
                        runtime.update_json(Path(prepared["statePath"]), Path(prepared["statePath"]).parent / ".state.lock",
                            lambda value: value.update(status="completed", sessionId=donor["sessionId"]))
                        apply("ready", dict(donorAgentId=donor_id, donorRunId=prepared["runId"], epoch=cycle))
                    prepare_receipt()
                    apply("event", {**binding, "id":f"queue-{cycle}", "kind":"pending-input",
                        "original":{"text":f"next-{cycle}", "approval":"changed", "attachments":[]}})
                    child = runtime.create_run(project_root=root, agent_id="child-a", actor="main", request=b"child original",
                        session=allocate("child-a", f"child-{cycle}"), parent_agent_id="main-a", parent_run_id=run["runId"])
                    runtime.atomic_write(Path(child["resultPath"]), b"late original")
                    runtime.mark_terminal(Path(child["statePath"]), "completed")
                    with self.assertRaises(ContractError):
                        apply("switch", binding)
                    check_dispatch(runtime.load_session(root, donor_id), preparation_for="main-a", runtime=runtime, root=root)
                    begin_preparation(runtime, root, donor, "main-a")
                    with self.assertRaises(ContractError):
                        apply("switch", binding)
                    prepare_receipt()
                    main = apply("switch", binding)
                    self.assertEqual(main["sessionId"], f"thread-{cycle+1}")
                    self.assertEqual(main["handoff"]["epoch"], cycle+1)
                    self.assertEqual(len(main["handoff"]["switches"]), cycle+1)
                    self.assertEqual(len(main["handoff"]["events"]), 5*(cycle+1))
                    self.assertEqual(Path(run["requestPath"]).read_bytes(), f"input-{cycle}".encode())
                    with self.assertRaises(ContractError):
                        apply("event", {**binding, "id":"stale", "reference":str(reference)})

    def test_persisted_native_transfer_keeps_agent_and_original_records_and_reserves_donor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sessions = {}
            for agent, native in (("main-a", "thread-a"), ("main-b", "thread-b")):
                session = dict(agentId=agent, role="main", projectRoot=str(root), sessionId=native,
                    provider="codex", model="fixed-model", reasoningEffort="high", fast=False,
                    executionPolicy=runtime_test_home.policy("danger-full-access"), humanApprovalPolicy="bypass",
                    maxAttempts=1, handoffExperiment=True)
                path = runtime.agent_directory(root, agent, create=True) / "session.json"
                runtime.atomic_write_json(path, session)
                sessions[agent] = session
            def apply(action, payload=None):
                path = root / "input.json"
                path.write_text(json.dumps(payload or {}))
                args = Namespace(project_root=root, agent="main-a", action=action, input=path)
                with redirect_stdout(io.StringIO()):
                    command(runtime, args)
                return runtime.load_session(root, "main-a")
            references = []
            for name in ("skills", "task", "decision", "approval"):
                path = root / name
                path.write_text(name + " original")
                references.append(str(path))
            session = apply("configure", dict(donorAgentId="main-b", preparePercent=60,
                targetPercent=80, maxAgeSeconds=60, references=references))
            self.assertNotIn("handoffReservedFor", runtime.load_session(root, "main-b"))
            run = runtime.create_run(project_root=root, agent_id="main-a", actor="human", request=b"same input",
                                     session=session)
            accept_run(runtime, root, session, run)
            observe_runtime(runtime, root, run, {"type": "turn.started", "turn_id": "turn"})
            observe_runtime(runtime, root, run, {"type": "provider.context", "usedTokens": 80,
                "contextWindowTokens": 100, "session_id": "thread-a", "turn_id": "turn", "observedAt": runtime.now()})
            with self.assertRaises(ContractError):
                apply("switch", dict(slot="A", epoch=0))
            observe_runtime(runtime, root, run, {"type": "turn.completed", "turn_id": "turn"})
            runtime.atomic_write(Path(run["resultPath"]), b"original answer")
            def finish_before_terminal(*arguments):
                # A competing transfer still sees the active run until its final
                # original/processing positions have been durably recorded.
                with self.assertRaises(ContractError):
                    apply("switch", dict(slot="A", epoch=0))
                return finish_run(*arguments)
            with mock.patch("execution.handoff.finish_run", side_effect=finish_before_terminal):
                runtime.mark_terminal(Path(run["statePath"]), "completed")
            state = runtime.load_session(root, "main-a")["handoff"]
            read_receipt = snapshot(state)
            donor_run = runtime.create_run(project_root=root, agent_id="main-b", actor="main", request=b"read originals",
                                           session=sessions["main-b"])
            runtime.update_json(Path(donor_run["statePath"]), Path(donor_run["statePath"]).parent / ".state.lock",
                                lambda value: value.update(sessionId="thread-b", status="completed"))
            runtime.atomic_write(Path(donor_run["resultPath"]), json.dumps(read_receipt).encode())
            apply("ready", dict(donorAgentId="main-b", donorRunId=donor_run["runId"], epoch=0))
            with self.assertRaises(ContractError):
                check_dispatch(runtime.load_session(root, "main-b"))
            original = Path(references[-1]).read_text()
            Path(references[-1]).write_text("approval changed after preparation")
            with self.assertRaises(ContractError):
                apply("switch", dict(slot="A", epoch=0))
            self.assertEqual(runtime.load_session(root, "main-a")["sessionId"], "thread-a")
            Path(references[-1]).write_text(original)
            transferred = apply("switch", dict(slot="A", epoch=0))
            self.assertEqual(transferred["agentId"], "main-a")
            self.assertEqual(transferred["sessionId"], "thread-b")
            self.assertEqual(transferred["handoff"]["owner"], "B")
            self.assertEqual(transferred["handoff"]["preparation"]["slot"], "A")
            self.assertEqual(transferred["handoff"]["retiredSessions"], ["thread-a"])
            self.assertEqual(Path(run["requestPath"]).read_bytes(), b"same input")
            self.assertEqual(Path(run["resultPath"]).read_bytes(), b"original answer")
            check_dispatch(runtime.load_session(root, "main-a"))
            with self.assertRaises(ContractError):
                apply("switch", dict(slot="A", epoch=0))

    def test_repeated_handoffs_preserve_originals_and_fence_previous_owner(self):
        for prepare, target in ((60, 80), (70, 85), (80, 90)):
            with self.subTest(prepare=prepare, target=target):
                state = configure(sessions={"A": "a", "B": "b"}, prepare_percent=prepare,
                                  target_percent=target, max_age_seconds=30,
                                  references=["shared-skills", "task-original", "decision-original", "approval-original"])
                now = "2026-10-07T18:00:00+00:00"
                def act(action_type, **fields):
                    nonlocal state
                    state = transition(state, {"type": action_type, "slot": state["owner"], "epoch": state["epoch"], **fields})
                def observe(percent):
                    obs = context_observation({"type": "provider.context", "usedTokens": percent,
                        "contextWindowTokens": 100, "session_id": state["sessions"][state["owner"]],
                        "turn_id": "turn"}, provider="codex", observed_at=now)
                    act("observe", observation=obs, now=now, turnId="turn")
                for cycle in range(6):
                    act("event", id=f"message-{cycle}", reference=f"original/message/{cycle}")
                    act("turn-start", id="turn")
                    observe(prepare)
                    prep = state["preparation"]
                    act("ready", slot=prep["slot"], cursor=state["cursor"], references=state["references"], receipt="read-result")
                    # In-flight tools block switching and invalidate occupancy.
                    act("tool-start", id="tool")
                    with self.assertRaises(ContractError):
                        act("switch", now=now)
                    act("tool-end", id="tool")
                    # A late result / decision / approval change invalidates readiness.
                    act("event", id=f"result-{cycle}", reference=f"original/result/{cycle}", kind="result")
                    act("processed", id=f"message-{cycle}")
                    act("turn-end", id="turn")
                    observe(target)
                    with self.assertRaises(ContractError):
                        act("switch", now=now)
                    act("prepare-failed", slot=prep["slot"], receipt="failure-result")
                    with self.assertRaises(ContractError):
                        act("switch", now=now)
                    act("ready", slot=prep["slot"], cursor=state["cursor"], references=state["references"], receipt="caught-up-result")
                    # Restoration preserves cursor, tool boundaries and readiness.
                    state = json.loads(json.dumps(state))
                    old_owner, old_epoch = state["owner"], state["epoch"]
                    act("switch", now=now)
                    self.assertFalse(authorized(state, old_owner, old_epoch))
                    with self.assertRaises(ContractError):
                        act("processed", slot=old_owner, epoch=old_epoch, id=f"result-{cycle}")
                    act("processed", id=f"result-{cycle}")
                self.assertEqual(len(state["switches"]), 6)
                self.assertEqual(len(state["events"]), 12)
                self.assertEqual(len(state["processed"]), 12)
                self.assertEqual(state["owner"], "A")

    def test_unknown_and_stale_observations_never_start_preparation(self):
        state = configure(sessions={"A": "a", "B": "b"}, prepare_percent=60,
                          target_percent=80, max_age_seconds=30, references=["original"])
        for observation in (None, context_observation({"type": "provider.context", "usedTokens": 90,
                "contextWindowTokens": 100, "session_id": "a", "turn_id": "turn"}, provider="codex",
                observed_at="2026-10-07T18:00:00+00:00")):
            current = transition(state, {"type": "observe", "slot": "A", "epoch": 0,
                "observation": observation, "now": "2026-10-07T18:01:00+00:00", "turnId": "turn"})
            self.assertIsNone(current["preparation"])


if __name__ == "__main__":
    unittest.main()
