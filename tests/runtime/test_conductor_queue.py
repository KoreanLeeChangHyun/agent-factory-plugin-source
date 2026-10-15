"""Multi-conductor queue: durable ingress, fair routing, fencing, delivery faults and UI projection."""
import runtime_test_home  # noqa: F401  Isolate runtime subprocesses from the real home.
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from storage import paths
from storage.errors import ContractError
from conductor import queue as cq
from conductor import domains
from conductor.runtime import FakeConductor, FakeExecutor, Runtime

ROOT = Path(__file__).resolve().parents[2]
POLICY = {"approvalPolicy": "bypass", "reference": "test authority", "executor": "fake", "conductor": "fake"}


class Recorder:
    """Executor backend that records effects instead of running workers."""

    def __init__(self):
        self.started, self.cancelled = [], []

    def start(self, ctx):
        self.started.append(ctx["attemptId"])

    def cancel(self, ctx):
        self.cancelled.append(ctx["attemptId"])

    def recover(self, ctx):
        self.started.append(ctx["attemptId"])


class QueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.db = Path(paths.resolve(self.project, create=True)["runtimeRoot"]) / "queue.sqlite3"
        self.store = self.open()
        self.store.ensure_chat("chat", POLICY)
        self.seq = 0
        domain = domains.create(self.db.parent, 'Queue tests', 'human', 'fixture')['domainId']
        child = domains.subdomain(self.db.parent, domain, 'Runtime', 'human', 'fixture', write_scope=[])['subdomain']
        self.area = {'domainId': domain, 'subdomainId': child['id']}

    def open(self):
        store = cq.Store(self.db)
        self.addCleanup(store.close)
        return store

    def say(self, text, store=None, **extra):
        self.seq += 1
        return (store or self.store).ingest("chat", "conversation", {"clientMessageId": "m%d" % self.seq, "text": text,
                                                                     **extra}, sent_at=time.time())

    def control(self, store=None, **payload):
        self.seq += 1
        return (store or self.store).ingest("chat", "control", {"commandId": "c%d" % self.seq, **payload})

    def worker(self, d, kind, event_id=None, cls="lifecycle", attempt=None, store=None, **data):
        return (store or self.store).ingest("chat", cls, {"delegationId": d, "attemptId": attempt or self.attempt(d),
                                                          "workerEventId": event_id or kind, "kind": kind, **data})

    def attempt(self, d):
        return self.store.connection().execute("SELECT attempt_id FROM delegations WHERE id=?", (d,)).fetchone()[0]

    def one(self, sql, *args, store=None):
        row = (store or self.store).connection().execute(sql, args).fetchone()
        return dict(row) if row else None

    def pump(self, store=None, executor=None):
        store = store or self.store
        executor = executor or Recorder()
        for _ in range(20):
            handled = store.publish_pending() + store.route_batch()
            handled += store.consume("conductor", store.conductor_handler)
            handled += store.consume("executor", store.executor_handler(executor))
            handled += store.consume("ui", store.ui_handler)
            if not handled:
                break
        return executor

    def turn(self, conductor_id, commands, usage=None, store=None):
        store = store or self.store
        context = store.begin_turn(conductor_id)
        token = {k: context[k] for k in ("conductorId", "slot", "epoch", "turnId")}
        declared = commands(context) if callable(commands) else commands
        for command in declared:
            if command.get('type') == 'delegate':
                command.setdefault('spec', {}).setdefault('writeScope', [])
                command['spec'].setdefault('workspace', {'mode': 'read-only'})
        return context, store.end_turn(token, declared, usage)

    def request_with_delegation(self, text="build it", **extra):
        self.say(text, relation="new", **extra)
        self.pump()
        req = self.one("SELECT * FROM requests ORDER BY created_at DESC LIMIT 1")
        self.turn(req["conductor_id"], [{"type": "delegate", "requestId": req["id"], "title": "part", "spec": {}}])
        executor = self.pump()
        d = self.one("SELECT * FROM delegations WHERE request_id=?", req["id"])
        return req, d, executor


class IngressAndRouting(QueueTest):
    def test_duplicate_input_is_idempotent_and_conflicting_content_rejected(self):
        first = self.store.ingest("chat", "conversation", {"clientMessageId": "x", "text": "hello"})
        again = self.store.ingest("chat", "conversation", {"clientMessageId": "x", "text": "hello"})
        self.assertEqual((again["disposition"], again["inputId"]), ("duplicate", first["inputId"]))
        with self.assertRaises(ContractError) as caught:
            self.store.ingest("chat", "conversation", {"clientMessageId": "x", "text": "changed"})
        self.assertEqual(caught.exception.code, "input_conflict")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM inputs")["n"], 1)

    def test_independent_requests_get_separate_conductors_and_followups_keep_the_owner(self):
        self.say("first", relation="new")
        self.say("second", relation="new")
        self.pump()
        owners = [r["conductor_id"] for r in self.store.connection().execute("SELECT conductor_id FROM requests")]
        self.assertEqual(len(set(owners)), 2)
        first = self.one("SELECT * FROM requests ORDER BY created_at LIMIT 1")
        self.say("more on first", requestId=first["id"])
        self.pump()
        item = self.one("SELECT * FROM mailbox ORDER BY seq DESC LIMIT 1")
        self.assertEqual((item["conductor_id"], item["request_id"]), (first["conductor_id"], first["id"]))

    def test_ambiguous_reference_is_held_visibly_until_the_human_resolves_it(self):
        self.say("first", relation="new")
        self.say("second", relation="new")
        self.pump()
        held = self.say("continue that")
        self.pump()
        self.assertEqual(self.one("SELECT status FROM inputs WHERE id=?", held["inputId"])["status"], "held")
        question = [e for e in self.store.ui_snapshot("chat")["chat"] if e["payload"]["role"] == "question"]
        self.assertEqual(len(question[0]["payload"]["candidates"]), 2)
        target = question[0]["payload"]["candidates"][1]["requestId"]
        self.control(action="resolve-input", inputId=held["inputId"], relation="followup", requestId=target)
        self.pump()
        routed = self.one("SELECT * FROM inputs WHERE id=?", held["inputId"])
        self.assertEqual((routed["status"], routed["request_id"]), ("routed", target))

    def test_progress_flood_does_not_starve_conversation_or_cancel(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d["id"], "started")
        self.pump()
        for index in range(3000):
            self.worker(d["id"], "progress", "p%d" % index, cls="progress", step="s", completed=index, total=3000)
        self.say("a new independent question?", relation="new")
        self.control(action="cancel", delegationId=d["id"],
                     expectedVersion=self.one("SELECT version FROM delegations WHERE id=?", d["id"])["version"])
        started = time.time()
        self.store.route_batch()  # One fair pass serves every class.
        elapsed = time.time() - started
        statuses = {r["class"]: r["status"] for r in self.store.connection().execute(
            "SELECT class, status FROM inputs WHERE class IN ('conversation','control') ORDER BY seq")}
        self.assertEqual(statuses, {"conversation": "routed", "control": "processed"})
        self.assertEqual(self.one("SELECT status FROM delegations WHERE id=?", d["id"])["status"], "cancel-requested")
        self.assertGreater(self.one("SELECT COUNT(*) n FROM inputs WHERE status='pending'")["n"], 0)
        self.assertLess(elapsed, 5)
        self.pump()
        self.assertEqual(self.one("SELECT COUNT(*) n FROM inputs WHERE class='progress' AND status='processed'")["n"], 3000)
        # Coalesced screen updates, every raw input kept.
        self.assertLess(self.one("SELECT COUNT(*) n FROM events WHERE type='attempt-progress'")["n"], 3000)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM inputs WHERE class='progress'")["n"], 3000)


class LifecycleAndRaces(QueueTest):
    def test_duplicate_completion_and_late_failure_of_a_superseded_attempt(self):
        req, d, executor = self.request_with_delegation()
        first = d["attempt_id"]
        self.worker(d["id"], "started")
        self.worker(d["id"], "failed", "boom", error={"code": "transient", "retryable": True})
        executor = self.pump()
        d = self.one("SELECT * FROM delegations WHERE id=?", d["id"])
        self.assertNotEqual(d["attempt_id"], first)
        self.assertEqual((d["status"], d["attempt_count"]), ("dispatched", 2))
        self.assertIn(d["attempt_id"], executor.started)
        self.worker(d["id"], "completed", result={"ok": 1})
        self.assertEqual(self.worker(d["id"], "completed", result={"ok": 1})["disposition"], "duplicate")
        self.worker(d["id"], "completed", "completed-again", result={"ok": 2})
        self.worker(d["id"], "failed", "late", attempt=first, error={"code": "late", "retryable": True})
        self.pump()
        d = self.one("SELECT * FROM delegations WHERE id=?", d["id"])
        self.assertEqual((d["status"], json.loads(d["result"])), ("completed", {"ok": 1}))
        self.assertEqual(self.one("SELECT COUNT(*) n FROM events WHERE type='delegation-completed'")["n"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM events WHERE type='attempt-late-event'")["n"], 1)
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", req["id"])["status"], "awaiting-synthesis")

    def test_cancel_racing_completion_keeps_the_finished_effect(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d["id"], "started")
        self.pump()
        version = self.one("SELECT version FROM delegations WHERE id=?", d["id"])["version"]
        self.control(action="cancel", delegationId=d["id"], expectedVersion=version)
        executor = self.pump()
        self.assertEqual(executor.cancelled, [d["attempt_id"]])
        self.worker(d["id"], "completed", result={"done": True})  # The worker finished before honouring cancel.
        self.pump()
        row = self.one("SELECT * FROM delegations WHERE id=?", d["id"])
        self.assertEqual((row["status"], row["cancel_requested"]), ("completed", 1))
        self.control(action="cancel", delegationId=d["id"], expectedVersion=row["version"])
        self.pump()
        rejected = self.one("SELECT payload FROM events WHERE type='control-rejected' ORDER BY seq DESC LIMIT 1")
        self.assertEqual(json.loads(rejected["payload"])["reason"], "already-terminal")
        self.assertEqual(self.one("SELECT status FROM delegations WHERE id=?", d["id"])["status"], "completed")

    def test_approval_and_cancel_from_the_same_version_have_one_deterministic_winner(self):
        for winner in ("approve", "cancel"):
            with self.subTest(winner=winner):
                store = cq.Store(self.db)
                self.addCleanup(store.close)
                chat = "approval-" + winner
                store.ensure_chat(chat, {**POLICY, "approvalPolicy": "required"})
                store.ingest(chat, "conversation", {"clientMessageId": "m", "text": "needs approval", "relation": "new"})
                self.pump(store)
                req = self.one("SELECT * FROM requests WHERE chat_id=?", chat)
                self.turn(req["conductor_id"], [{"type": "delegate", "requestId": req["id"], "title": "t", "spec": {}}], store=store)
                executor = self.pump(store)
                d = self.one("SELECT * FROM delegations WHERE request_id=?", req["id"])
                self.assertEqual((d["status"], executor.started), ("awaiting-approval", []))
                order = ("approve", "cancel") if winner == "approve" else ("cancel", "approve")
                for action in order:
                    store.ingest(chat, "control", {"commandId": action, "action": action, "delegationId": d["id"],
                                                   "expectedVersion": d["version"]})
                executor = self.pump(store)
                row = self.one("SELECT * FROM delegations WHERE id=?", d["id"])
                rejected = json.loads(self.one("SELECT payload FROM events WHERE chat_id=? AND type='control-rejected'",
                                               chat)["payload"])
                if winner == "approve":
                    self.assertEqual((row["status"], executor.started), ("dispatched", [d["attempt_id"]]))
                    self.assertEqual((rejected["action"], rejected["reason"]), ("cancel", "version-conflict"))
                else:
                    self.assertEqual((row["status"], executor.started), ("cancelled", []))
                    self.assertEqual(rejected["action"], "approve")
                    self.assertIn(rejected["reason"], {"already-terminal", "version-conflict"})

    def test_request_cancel_settles_after_workers_report_and_reaches_the_owner(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d["id"], "started")
        self.pump()
        req = self.one("SELECT * FROM requests WHERE id=?", req["id"])
        self.control(action="cancel", requestId=req["id"], expectedVersion=req["version"])
        self.pump()
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", req["id"])["status"], "cancelling")
        self.worker(d["id"], "cancelled")
        self.pump()
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", req["id"])["status"], "cancelled")
        kinds = [r[0] for r in self.store.connection().execute(
            "SELECT kind FROM mailbox WHERE conductor_id=? ORDER BY seq", (req["conductor_id"],))]
        self.assertIn("request-cancelled", kinds)


class FencingAndOwnership(QueueTest):
    def test_compaction_switch_fences_late_chunk_delegation_and_final_answer(self):
        store = cq.Store(self.db)
        self.addCleanup(store.close)
        store.ensure_chat("compact", {**POLICY, "compaction": {"preparePercent": 50, "targetPercent": 60}})
        store.ingest("compact", "conversation", {"clientMessageId": "m", "text": "long work", "relation": "new"})
        self.pump(store)
        req = self.one("SELECT * FROM requests WHERE chat_id='compact'")
        context, result = self.turn(req["conductor_id"], [{"type": "reply", "requestId": req["id"], "text": "ok"}],
                                    usage={"usedTokens": 70, "contextWindowTokens": 100}, store=store)
        self.assertEqual(result["switched"]["epoch"], 1)
        old = {k: context[k] for k in ("conductorId", "slot", "epoch", "turnId")}
        checkpoint = json.loads(self.one("SELECT checkpoint FROM conductors WHERE id=?", req["conductor_id"])["checkpoint"])
        self.assertEqual(checkpoint["decisions"][0]["requestId"], req["id"])
        store.ingest("compact", "conversation", {"clientMessageId": "m2", "text": "more", "requestId": req["id"]})
        self.pump(store)
        fresh = store.begin_turn(req["conductor_id"])
        self.assertEqual((fresh["slot"], fresh["epoch"]), ("B", 1))
        stale = {**old, "turnId": fresh["turnId"]}  # Even with the live turn ID, the old slot/epoch is fenced.
        with self.assertRaises(cq.StaleOwner):
            store.stream(stale, req["id"], "late chunk", "k")
        with self.assertRaises(cq.StaleOwner):
            store.end_turn(stale, [{"type": "delegate", "requestId": req["id"], "title": "dup", "spec": {}},
                                   {"type": "reply", "requestId": req["id"], "text": "late", "final": True}])
        self.assertEqual(self.one("SELECT COUNT(*) n FROM delegations")["n"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM events WHERE type='stale-owner-rejected'")["n"], 2)
        self.assertEqual(store.metrics("compact")["observations"]["stale-owner-rejected"], 2)
        token = {k: fresh[k] for k in ("conductorId", "slot", "epoch", "turnId")}
        acknowledgements = [{'type': 'ack-instruction', 'requestId': req['id'], 'instructionId': row['id'], 'taskIds': [], 'evidence': 'Applied follow-up from successor context'} for row in fresh['responsibility'][req['id']]['pendingInstructions']]
        store.end_turn(token, [*acknowledgements, {"type": "reply", "requestId": req["id"], "text": "done", "final": True}])
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", req["id"])["status"], "completed")

    def test_reassign_mid_turn_rejects_the_former_owner_per_write(self):
        self.say("one", relation="new")
        self.say("two", relation="new")
        self.pump()
        first, second = [dict(r) for r in self.store.connection().execute("SELECT * FROM requests ORDER BY created_at")]
        # Both requests owned by one conductor for this scenario.
        self.store.connection().execute("UPDATE requests SET conductor_id=? WHERE id=?", (first["conductor_id"], second["id"]))
        self.store.connection().execute("UPDATE mailbox SET conductor_id=? WHERE request_id=?",
                                        (first["conductor_id"], second["id"]))
        context = self.store.begin_turn(first["conductor_id"])
        token = {k: context[k] for k in ("conductorId", "slot", "epoch", "turnId")}
        version = self.one("SELECT version FROM requests WHERE id=?", second["id"])["version"]
        self.control(action="reassign", requestId=second["id"], expectedVersion=version)
        self.pump()
        moved = self.one("SELECT * FROM requests WHERE id=?", second["id"])
        self.assertNotEqual(moved["conductor_id"], first["conductor_id"])
        self.assertEqual(moved["owner_epoch"], 2)
        with self.assertRaises(cq.StaleOwner):
            self.store.stream(token, second["id"], "chunk", "k")
        result = self.store.end_turn(token, [
            {"type": "delegate", "requestId": second["id"], "title": "stale", "spec": {}},
            {"type": "reply", "requestId": second["id"], "text": "stale final", "final": True},
            {"type": "reply", "requestId": first["id"], "text": "legit", "final": True}])
        self.assertEqual([r.get("rejected") for r in result["applied"]], ["stale_owner", "stale_owner", None])
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", first["id"])["status"], "completed")
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", second["id"])["status"], "accepted")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM mailbox WHERE conductor_id=? AND status='queued'",
                                  moved["conductor_id"])["n"], 1)

    def test_invalid_final_while_delegations_are_open_is_rejected_without_wedging(self):
        req, d, _ = self.request_with_delegation()
        self.say("anything new?", requestId=req["id"])
        self.pump()
        _, result = self.turn(req["conductor_id"], [{"type": "reply", "requestId": req["id"], "text": "x", "final": True}])
        self.assertEqual(result["applied"][0]["rejected"], "delegations_open")
        self.assertEqual(self.one("SELECT status FROM conductors WHERE id=?", req["conductor_id"])["status"], "idle")


class DeliveryFaults(QueueTest):
    def test_relay_crash_after_publish_republishes_harmlessly(self):
        self.say("hello", relation="new")
        self.store.faults["after-publish"] = lambda seq: seq == 1
        with self.assertRaises(cq.SimulatedCrash):
            self.store.publish_pending()
        restarted = self.open()
        self.pump(restarted)
        self.assertEqual(self.one("SELECT published FROM events WHERE seq=1")["published"], 1)
        users = [e for e in restarted.ui_snapshot("chat")["chat"] if e["payload"]["role"] == "user"]
        self.assertEqual(len(users), 1)

    def test_consumer_crash_after_commit_before_ack_is_redelivered_once(self):
        self.say("hello", relation="new")
        self.store.publish_pending()
        self.store.faults["after-consume-commit"] = lambda subscription, seq: subscription == "ui"
        with self.assertRaises(cq.SimulatedCrash):
            self.store.consume("ui", self.store.ui_handler)
        restarted = self.open()
        self.assertEqual(self.one("SELECT status FROM deliveries WHERE subscription='ui' AND event_seq=1")["status"], "ready")
        self.pump(restarted)
        self.assertEqual(self.one("SELECT status FROM deliveries WHERE subscription='ui' AND event_seq=1")["status"], "acked")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM ui_feed WHERE kind='chat'")["n"], 1)

    def test_executor_crash_between_commit_and_start_recovers_the_uncertain_attempt(self):
        self.say("work", relation="new")
        self.pump()
        req = self.one("SELECT * FROM requests")
        self.turn(req["conductor_id"], [{"type": "delegate", "requestId": req["id"], "title": "t", "spec": {"durationMs": 50}}])
        self.store.publish_pending()
        self.store.faults["after-consume-commit"] = lambda subscription, seq: subscription == "executor"
        executor = Recorder()
        with self.assertRaises(cq.SimulatedCrash):
            self.store.consume("executor", self.store.executor_handler(executor))
        self.assertEqual(executor.started, [])  # The commit happened; the external start did not.
        restarted = self.open()
        runtime = Runtime(restarted, conductor=FakeConductor(delay=0), executors={"fake": FakeExecutor(restarted, step_count=2)})
        report = runtime.recover()
        self.assertEqual(len(report["uncertainAttempts"]), 1)
        self.assertTrue(runtime.drain(20))
        self.assertEqual(self.one("SELECT status FROM delegations")["status"], "completed")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM consumer_inbox WHERE subscription='executor'")["n"], 1)

    def test_poison_event_is_quarantined_while_later_events_flow_and_can_be_requeued(self):
        self.say("hello", relation="new")
        with self.store.tx() as c:
            self.store.event(c, "chat", "request-status", {"broken": True})
        self.say("after poison", relation="new")
        for _ in range(cq.POISON_ATTEMPTS + 2):
            self.store.publish_pending()
            self.store.route_batch()
            self.store.consume("ui", self.store.ui_handler)
            time.sleep(0.45)
        quarantined = self.store.quarantined()["deliveries"]
        self.assertEqual([(q["subscription"], q["type"]) for q in quarantined], [("ui", "request-status")])
        users = [e for e in self.store.ui_snapshot("chat")["chat"] if e["payload"]["role"] == "user"]
        self.assertEqual(len(users), 2)
        self.store.connection().execute("UPDATE events SET payload=? WHERE seq=?", (json.dumps({"request": {
            "id": "r", "chat_id": "chat"}}), quarantined[0]["eventSeq"]))
        self.store.requeue_quarantined("ui", quarantined[0]["eventSeq"])
        self.assertEqual(self.one("SELECT status FROM deliveries WHERE subscription='ui' AND event_seq=?",
                                  quarantined[0]["eventSeq"])["status"], "ready")

    def test_driver_failing_on_the_same_input_quarantines_it_after_retries(self):
        class Broken(FakeConductor):
            def turn(self, context, stream):
                raise RuntimeError("model crashed")
        self.say("poison", relation="new")
        runtime = Runtime(self.store, conductor=Broken(delay=0), executors={"fake": Recorder()})
        runtime.drain(10)
        item = self.one("SELECT status, failures FROM mailbox")
        self.assertEqual((item["status"], item["failures"]), ("quarantined", cq.POISON_ATTEMPTS))
        self.assertEqual(self.one("SELECT status FROM conductors")["status"], "idle")


class RecoveryAndProjection(QueueTest):
    def test_restart_requeues_an_abandoned_turn_and_rejects_its_late_result(self):
        self.say("hello?", relation="new")
        self.pump()
        req = self.one("SELECT * FROM requests")
        context = self.store.begin_turn(req["conductor_id"])
        token = {k: context[k] for k in ("conductorId", "slot", "epoch", "turnId")}
        restarted = self.open()
        report = restarted.recover()
        self.assertEqual(report["abandonedTurns"], [context["turnId"]])
        self.assertEqual(self.one("SELECT status FROM mailbox")["status"], "queued")
        with self.assertRaises(cq.StaleOwner):
            restarted.end_turn(token, [{"type": "reply", "requestId": req["id"], "text": "late", "final": True}])
        runtime = Runtime(restarted, conductor=FakeConductor(delay=0, chunk_delay=0), executors={"fake": FakeExecutor(restarted, step_count=1)})
        self.assertTrue(runtime.drain(20))
        self.assertEqual(self.one("SELECT status FROM requests")["status"], "completed")

    def test_ui_and_conductor_both_receive_lifecycle_events(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d["id"], "completed", result={"x": 1})
        self.pump()
        seq = self.one("SELECT seq FROM events WHERE type='delegation-completed'")["seq"]
        acked = {r[0] for r in self.store.connection().execute(
            "SELECT subscription FROM deliveries WHERE event_seq=? AND status='acked'", (seq,))}
        self.assertEqual(acked, {"conductor", "ui"})
        self.assertEqual(self.one("SELECT kind FROM mailbox ORDER BY seq DESC LIMIT 1")["kind"], "delegation-result")
        worker = next(s for s in self.store.ui_snapshot("chat")["state"] if s["key"] == "worker:" + d["id"])
        self.assertEqual(worker["payload"]["status"], "completed")

    def test_snapshot_and_replay_reconnect_without_gaps_or_duplicates(self):
        req, d, _ = self.request_with_delegation()
        snapshot = self.store.ui_snapshot("chat")
        self.worker(d["id"], "started")
        for index in range(5):
            self.worker(d["id"], "progress", "p%d" % index, cls="progress", step="s%d" % index, completed=index, total=5)
            self.pump()
        replay = self.store.ui_replay("chat", snapshot["cursor"])
        keys = [e["key"] for e in replay["entries"] if e["kind"] == "state"]
        self.assertEqual(len(keys), len(set(keys)))  # coalesced: latest version per key
        worker = next(e for e in replay["entries"] if e["key"] == "worker:" + d["id"])
        self.assertEqual(worker["payload"]["progress"]["completed"], 4)
        self.assertTrue(all(e["id"] > snapshot["cursor"] for e in replay["entries"]))
        self.assertEqual(self.store.ui_replay("chat", replay["cursor"])["entries"], [])
        full = self.store.ui_replay("chat", 0, coalesce=False)
        self.assertEqual(len({e["id"] for e in full["entries"]}), len(full["entries"]))

    def test_project_board_spans_chats_and_shares_the_chat_versions(self):
        req, d, _ = self.request_with_delegation()
        self.store.ensure_chat("other", POLICY)
        self.store.ingest("other", "conversation", {"clientMessageId": "o", "text": "other chat", "relation": "new"})
        self.pump()
        board = self.store.ui_board()
        chats = {row["payload"].get("chatId") for row in board["state"] if row["kind"] == "request"}
        self.assertEqual(chats, {"chat", "other"})
        self.assertFalse(any(row["kind"] == "input" for row in board["state"]))
        worker = next(row for row in board["state"] if row["key"] == "worker:" + d["id"])
        same = next(row for row in self.store.ui_snapshot("chat")["state"] if row["key"] == "worker:" + d["id"])
        self.assertEqual((worker["version"], worker["payload"]), (same["version"], same["payload"]))

    def test_end_to_end_with_fake_drivers_runs_two_requests_concurrently(self):
        conductor = FakeConductor(delay=0.4, chunk_delay=0.01)
        active, overlap = set(), []
        original = conductor.turn

        def observed(context, stream):
            active.add(context["conductorId"])
            overlap.append(len(active))
            try:
                return original(context, stream)
            finally:
                active.discard(context["conductorId"])
        conductor.turn = observed
        runtime = Runtime(self.store, conductor=conductor, executors={"fake": FakeExecutor(self.store, step_count=2)})
        self.say("first 작업 2개 0.2초", relation="new")
        self.say("second 작업 1개 0.2초", relation="new")
        self.assertTrue(runtime.drain(30))
        self.assertEqual(max(overlap), 2)
        statuses = [r[0] for r in self.store.connection().execute("SELECT status FROM requests")]
        self.assertEqual(statuses, ["completed", "completed"])
        metrics = self.store.metrics("chat")
        self.assertEqual(metrics["finalResponse"]["count"], 2)
        self.assertGreater(metrics["uiProjection"]["count"], 0)
        finals = [e for e in self.store.ui_snapshot("chat")["chat"] if e["payload"].get("final")]
        self.assertEqual(len(finals), 2)


class ManagedExecutorBridge(QueueTest):
    def test_delegation_uses_the_existing_exec_lifecycle_without_resubmitting_a_known_start(self):
        from conductor.runtime import ManagedExecutor

        class Backend:
            def __init__(self):
                self.submitted, self.calls, self.statuses = [], [], ["running", "completed"]

            def lookup(self, request):
                return None

            def submit(self, request):
                self.submitted.append(request)
                return {"runId": "run-1", "status": "running"}

            def call(self, arguments):
                self.calls.append(arguments)
                if arguments[0] == "status":
                    return {"run": {"runId": "run-1", "status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0],
                                    "resultPath": "/r/result.md", "receiptPath": "/r/receipt.json"}}
                return {}
        policy = {**POLICY, "executor": "managed", "sandbox": "workspace-write",
                  "work": {"provider": "codex", "model": "gpt-test", "reasoningEffort": "medium"}}
        with self.assertRaises(ContractError):
            self.store.ensure_chat("managed-missing", {**POLICY, "executor": "managed"})
        self.store.ensure_chat("managed", policy)
        self.store.ingest("managed", "conversation", {"clientMessageId": "m", "text": "real work", "relation": "new", "area": self.area})
        self.pump()
        req = self.one("SELECT * FROM requests WHERE chat_id='managed'")
        self.turn(req["conductor_id"], [{"type": "delegate", "requestId": req["id"], "title": "t",
                                         "spec": {"goal": "Do it", "completionCriteria": "Done"}}])
        backend = Backend()
        executor = ManagedExecutor(self.store, self.project, poll_seconds=0.01, backend=backend)
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0), executors={"managed": executor})
        self.assertTrue(runtime.drain(20))
        request = backend.submitted[0]
        self.assertEqual((request["session"]["role"], request["session"]["settings"]["model"]), ("work", "gpt-test"))
        self.assertEqual(request["policy"], {"sandbox": "read-only", "approvalPolicy": "bypass", "reference": "test authority"})
        self.assertEqual(request["task"]["spec"]["completionCriteria"], "Done")
        d = self.one("SELECT * FROM delegations WHERE request_id=?", req["id"])
        self.assertEqual(d["status"], "completed")
        self.assertEqual(json.loads(self.one("SELECT external FROM attempts WHERE id=?", d["attempt_id"])["external"])["runId"], "run-1")
        ctx = self.store.attempt_context(d["attempt_id"])
        executor.recover(ctx)  # A known accepted run is observed again, never submitted twice.
        time.sleep(0.05)
        self.assertEqual(len(backend.submitted), 1)
        executor.cancel(ctx)
        self.assertEqual(backend.calls[-1][:2], ["cancel", "--agent"])


class ManagedConductorDriver(QueueTest):
    """The LLM conductor through the existing exec.py lifecycle, with a stub backend (no provider runs)."""

    class Backend:
        def __init__(self, root, outputs):
            self.root, self.outputs, self.requests, self.runs = root, list(outputs), [], {}

        def lookup(self, request):
            return self.runs.get(request["run"]["dispatchId"])

        def submit(self, request):
            self.requests.append(request)
            session = self.root / "agents" / request["session"]["agentId"] / "session.json"
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text("{}")
            result = self.root / ("%s.result.md" % request["run"]["dispatchId"])
            result.write_text(self.outputs.pop(0), encoding="utf-8")
            run = {"runId": "run-%d" % len(self.requests), "status": "running", "resultPath": str(result),
                   "contextUsage": {"usedTokens": 70, "contextWindowTokens": 100}}
            self.runs[request["run"]["dispatchId"]] = run
            return run

        def call(self, arguments):
            run = next(r for r in self.runs.values() if r["runId"] == arguments[arguments.index("--run-id") + 1])
            return {"run": {**run, "status": "completed"}}

    def test_turns_use_captured_settings_resume_the_slot_session_and_switch_with_the_checkpoint(self):
        from conductor.runtime import ManagedConductor
        settings = {"provider": "claude", "model": "claude-test", "reasoningEffort": "medium"}
        policy = {**POLICY, "conductor": "managed", "conductorSettings": settings,
                  "compaction": {"preparePercent": 50, "targetPercent": 60}}
        with self.assertRaises(ContractError):
            self.store.ensure_chat("llm-missing", {**POLICY, "conductor": "managed"})
        self.store.ensure_chat("llm", policy)
        self.store.ingest("llm", "conversation", {"clientMessageId": "m1", "text": "plan it", "relation": "new", "area": self.area})
        self.pump()
        req = self.one("SELECT * FROM requests WHERE chat_id='llm'")
        outputs = ['Here is my plan: {"commands": [{"type": "reply", "requestId": "%s", "text": "Planning."},'
                   ' {"type": "delegate", "requestId": "%s", "title": "Part 1", "spec": {"goal": "g",'
                   ' "completionCriteria": "c", "writeScope": [], "workspace": {"mode": "read-only"}}}]}' % (req["id"], req["id"]),
                   '{"commands": [{"type": "reply", "requestId": "%s", "text": "Done.", "final": true}]}' % req["id"]]
        instruction = self.one('SELECT id FROM responsibility_instructions WHERE request_id=?', req['id'])['id']
        final = json.loads(outputs[1])
        final['commands'].insert(0, {'type': 'ack-instruction', 'requestId': req['id'], 'instructionId': instruction, 'taskIds': [], 'evidence': 'Stub applied the exact accepted input and Part 1 result'})
        outputs[1] = json.dumps(final)
        backend = self.Backend(self.db.parent, outputs)
        driver = ManagedConductor(self.project, self.store, poll_seconds=0.001, backend=backend)
        executor = Recorder()
        runtime = Runtime(self.store, conductors={"managed": driver}, executors={"fake": executor})
        self.assertTrue(runtime.drain(20))
        first = backend.requests[0]
        self.assertEqual((first["session"]["settings"], first["session"]["managedCreated"], first["session"]["role"]),
                         (settings, False, "conversation"))
        self.assertEqual(first["policy"], {"sandbox": "read-only", "approvalPolicy": "bypass", "reference": "test authority"})
        self.assertIn('"requestId"', first["message"]["text"])
        d = self.one("SELECT * FROM delegations WHERE request_id=?", req["id"])
        self.assertEqual(executor.started, [d["attempt_id"]])
        # Usage 70% crossed the switch target: the next turn belongs to slot B, a fresh session with the checkpoint.
        self.assertEqual(json.loads(self.one("SELECT handoff FROM conductors WHERE id=?", req["conductor_id"])["handoff"])["owner"], "B")
        self.store.ingest("llm", "lifecycle", {"delegationId": d["id"], "attemptId": d["attempt_id"], "workerEventId": "done",
                                               "kind": "completed", "result": {"ok": True}})
        self.assertTrue(runtime.drain(20))
        second = backend.requests[1]
        self.assertNotEqual(second["session"]["agentId"], first["session"]["agentId"])
        self.assertFalse(second["session"]["managedCreated"])
        payload = json.loads(second["message"]["text"].split("\n\n", 1)[1])
        self.assertEqual(payload["checkpoint"]["openDelegations"][0]["delegationId"], d["id"])
        self.assertEqual(self.one("SELECT status FROM requests WHERE id=?", req["id"])["status"], "completed")

    def test_invalid_conductor_output_is_retried_then_quarantined_without_effects(self):
        from conductor.runtime import ManagedConductor, parse_commands
        for bad in ("no json here", '{"commands": "x"}', '{"commands": [{"type": "shell", "cmd": "rm"}]}'):
            with self.assertRaises(ContractError):
                parse_commands(bad)
        self.store.ensure_chat("llm-bad", {**POLICY, "conductor": "managed", "conductorSettings": {"model": "m"}})
        self.store.ingest("llm-bad", "conversation", {"clientMessageId": "m", "text": "x", "relation": "new", "area": self.area})
        backend = self.Backend(self.db.parent, ["not json"] * cq.POISON_ATTEMPTS)
        runtime = Runtime(self.store, conductors={"managed": ManagedConductor(self.project, self.store, poll_seconds=0.001,
                                                                               backend=backend)}, executors={"fake": Recorder()})
        runtime.drain(20)
        self.assertEqual(self.one("SELECT status FROM mailbox")["status"], "quarantined")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM delegations")["n"], 0)


class CommandLine(unittest.TestCase):
    def test_serve_leader_follower_stream_and_takeover(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths.resolve(tmp.name, create=True)
        environment = {k: v for k, v in os.environ.items() if k != "AGENT_FACTORY_CODEX_POOL"}
        command = [sys.executable, str(ROOT / "scripts" / "conductor_queue.py"), "serve", "--project-root", tmp.name,
                   "--conductor-delay", "0.05"]

        def spawn():
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=environment)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            lines = []
            threading.Thread(target=lambda: [lines.append(json.loads(line)) for line in process.stdout], daemon=True).start()
            return process, lines

        def wait(lines, predicate, limit=30):
            end = time.time() + limit
            while time.time() < end:
                found = [line for line in list(lines) if predicate(line)]
                if found:
                    return found[0]
                time.sleep(0.02)
            self.fail("timed out waiting; saw %s" % [line.get("type") for line in lines][-20:])

        def send(process, value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()
        leader, leader_lines = spawn()
        wait(leader_lines, lambda l: l.get("role") == "leader")
        follower, follower_lines = spawn()
        wait(follower_lines, lambda l: l.get("role") == "follower")
        send(follower, {"op": "ensure-chat", "id": 1, "chatId": "c", "policy": POLICY})
        wait(follower_lines, lambda l: l.get("id") == 1 and l.get("ok"))
        send(follower, {"op": "ingest", "id": 2, "chatId": "c", "class": "conversation",
                        "payload": {"clientMessageId": "m", "text": "quick question?", "relation": "new"}, "sentAt": time.time()})
        wait(follower_lines, lambda l: l.get("id") == 2 and l.get("ok"))
        final = lambda l: l.get("type") == "feed" and any(e["payload"].get("final") for e in l["entries"])
        wait(follower_lines, final)  # The follower streams what the leader processed.
        leader.kill()
        leader.wait()
        wait(follower_lines, lambda l: l.get("role") == "leader")  # Takeover with recovery.
        send(follower, {"op": "ingest", "id": 3, "chatId": "c", "class": "conversation",
                        "payload": {"clientMessageId": "m2", "text": "another question?", "relation": "new"}})
        finals = lambda: sum(1 for l in list(follower_lines) if l.get("type") == "feed"
                             for e in l["entries"] if e["payload"].get("final"))
        wait(follower_lines, lambda l: finals() >= 2)
        send(follower, {"op": "snapshot", "id": 4, "chatId": "c"})
        snapshot = wait(follower_lines, lambda l: l.get("id") == 4)["result"]
        self.assertEqual(sum(1 for e in snapshot["chat"] if e["payload"].get("final")), 2)
        follower.stdin.close()
        self.assertEqual(follower.wait(10), 0)


if __name__ == "__main__":
    unittest.main()
