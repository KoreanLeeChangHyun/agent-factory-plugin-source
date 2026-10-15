"""One logical conductor answers an independent conversation in another session while a slow turn is still running."""
import runtime_test_home  # noqa: F401
import copy
import json
import threading
import time

from conductor.pool import DEFAULT
from conductor.runtime import Runtime
from test_conductor_queue import QueueTest, POLICY


class GatedConductor:
    """Fake conductor driver: a turn whose input says "slow" blocks until released; others answer at once."""

    def __init__(self):
        self.slow_started, self.release = threading.Event(), threading.Event()
        self.order, self.lock = [], threading.Lock()

    def turn(self, context, stream):
        texts = [item['payload'].get('text', '') for item in context['items']]
        slow = any('slow' in text for text in texts)
        if slow:
            self.slow_started.set()
            assert self.release.wait(20), 'test never released the slow turn'
        with self.lock:
            self.order.append((context['conductorId'], tuple(texts)))
        # Like a real conductor, a delivered steering instruction is acknowledged before the task's final answer.
        acks = [{'type': 'ack-instruction', 'requestId': item['request_id'], 'instructionId': item['payload']['instructionId'],
                 'taskIds': [], 'evidence': 'answered in this turn'} for item in context['items'] if item['payload'].get('instructionId')]
        return acks + [{'type': 'reply', 'requestId': rid, 'text': 'answer: ' + ' | '.join(texts), 'final': True}
                       for rid in context['requests']], None


class ConductorConcurrency(QueueTest):
    def setUp(self):
        super().setUp()
        self.policy = {**POLICY, 'employment': True, 'conductorSettings': {'model': 'stub', 'provider': 'codex'}}
        self.store.ensure_chat('pool', self.policy)
        self.driver = GatedConductor()
        self.runtime = Runtime(self.store, conductors={'fake': self.driver})
        self.stop = threading.Event()
        self.loop = threading.Thread(target=self.run_loop, daemon=True)
        self.loop.start()
        self.addCleanup(self.shutdown)

    def run_loop(self):
        while not self.stop.is_set():
            self.runtime.tick()
            time.sleep(0.005)

    def shutdown(self):
        self.driver.release.set()
        self.stop.set()
        self.loop.join(5)

    def send(self, client_id, text, **extra):
        return self.store.ingest('pool', 'conversation', {'clientMessageId': client_id, 'text': text, 'area': self.area, **extra})

    def request_of(self, input_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.one('SELECT request_id FROM inputs WHERE id=?', input_id)
            if row and row['request_id']:
                return self.one('SELECT * FROM requests WHERE id=?', row['request_id'])
            time.sleep(0.01)
        self.fail('input was never routed')

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail('condition not reached')

    def test_independent_conversation_is_answered_by_another_session_before_the_slow_turn_ends(self):
        a = self.send('a', 'slow analysis of the repository')
        self.assertTrue(self.driver.slow_started.wait(10))
        a_request = self.request_of(a['inputId'])
        b = self.send('b', 'what is the status of the build?', relation='new')
        b_request = self.request_of(b['inputId'])
        # B completes while A's turn is still running in its own session.
        self.wait_for(lambda: self.one('SELECT status FROM requests WHERE id=?', b_request['id'])['status'] == 'completed')
        self.assertNotEqual(b_request['conductor_id'], a_request['conductor_id'])
        self.assertEqual(self.one('SELECT status FROM conductors WHERE id=?', a_request['conductor_id'])['status'], 'busy')
        self.assertNotEqual(self.one('SELECT status FROM requests WHERE id=?', a_request['id'])['status'], 'completed')
        replies = [json.loads(r['payload']) for r in self.store.connection().execute("SELECT payload FROM events WHERE type='conductor-reply' ORDER BY seq")]
        self.assertEqual([r['requestId'] for r in replies if r['final']], [b_request['id']])
        # A follow-up to the slow task stays with that task and its session; it joins at A's next turn boundary.
        steer = self.send('a2', 'also include the tests', relation='followup', requestId=a_request['id'])
        self.wait_for(lambda: self.one('SELECT status FROM inputs WHERE id=?', steer['inputId'])['status'] == 'routed')
        item = self.one('SELECT * FROM mailbox WHERE source=?', 'input:' + steer['inputId'])
        self.assertEqual((item['conductor_id'], item['request_id'], item['status']), (a_request['conductor_id'], a_request['id'], 'queued'))
        # A duplicate delivery of B creates nothing new.
        self.assertEqual(self.send('b', 'what is the status of the build?', relation='new')['disposition'], 'duplicate')
        self.driver.release.set()
        self.wait_for(lambda: self.one('SELECT status FROM requests WHERE id=?', a_request['id'])['status'] == 'completed')
        self.wait_for(lambda: self.one('SELECT status FROM mailbox WHERE id=?', item['id'])['status'] == 'processed')
        order = [entry for entry in self.driver.order]
        self.assertEqual(order[0][0], b_request['conductor_id'], 'B was answered first')
        self.assertTrue(any('also include the tests' in ' '.join(texts) for cid, texts in order if cid == a_request['conductor_id']))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM requests')['n'], 2, 'the follow-up joined A instead of opening a request')
        self.assertEqual(json.loads(self.one("SELECT policy FROM chats WHERE id='pool'")['policy']), self.policy, 'captured policy unchanged')

    def test_a_full_turn_limit_keeps_the_input_visible_and_starts_it_when_a_slot_frees(self):
        value = copy.deepcopy(DEFAULT)
        value['conductor'].update(domainLimit=1, subdomainLimit=1)
        self.store.pool.configure(value, 'Human one-turn cap')
        self.send('a', 'slow work')
        self.assertTrue(self.driver.slow_started.wait(10))
        b = self.send('b', 'quick question', relation='new')
        b_request = self.request_of(b['inputId'])
        self.wait_for(lambda: self.one('SELECT status FROM requests WHERE id=?', b_request['id'])['status'] == 'queued-for-instance')
        # The waiting request is visible with its reason in the chat projection (applied by the UI subscription).
        visible = self.wait_for(lambda: next((r['payload'] for r in self.store.ui_snapshot('pool')['state']
                                              if r['key'] == 'request:' + b_request['id'] and r['payload']['status'] == 'queued-for-instance'), None))
        self.assertEqual(visible['waitReason'], 'employment-limit')
        self.assertEqual(self.one('SELECT status FROM inputs WHERE id=?', b['inputId'])['status'], 'routed')
        self.driver.release.set()
        self.wait_for(lambda: self.one('SELECT status FROM requests WHERE id=?', b_request['id'])['status'] == 'completed')
        self.assertIn('quick question', ' '.join(' '.join(texts) for _, texts in self.driver.order))


class Recovery(QueueTest):
    def test_a_restart_during_a_turn_requeues_its_input_and_failures_keep_it(self):
        self.policy = {**POLICY, 'employment': True, 'conductorSettings': {'model': 'stub', 'provider': 'codex'}}
        self.store.ensure_chat('pool', self.policy)
        self.store.ingest('pool', 'conversation', {'clientMessageId': 'x', 'text': 'long one', 'area': self.area})
        self.pump()
        req = self.one('SELECT * FROM requests')
        context = self.store.begin_turn(req['conductor_id'])
        self.store.close()
        self.store = self.open()
        report = self.store.recover()
        self.assertEqual(report['abandonedTurns'], [context['turnId']])
        self.assertEqual(self.one("SELECT COUNT(*) n FROM mailbox WHERE request_id=? AND status='queued'", req['id'])['n'], 1)
        retry = self.store.begin_turn(req['conductor_id'])
        self.store.fail_turn({k: retry[k] for k in ('conductorId', 'slot', 'epoch', 'turnId')}, 'ProviderError')
        self.assertEqual(self.one("SELECT COUNT(*) n FROM mailbox WHERE request_id=? AND status='queued'", req['id'])['n'], 1)
