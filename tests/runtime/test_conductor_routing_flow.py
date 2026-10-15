"""Conductor–performer routing acceptance scenarios (design-agent-routing-flow, behavior §10) over the queue ledger."""
import runtime_test_home  # noqa: F401
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

from conductor import queue as cq
from conductor.runtime import FakeConductor, FakeExecutor, Runtime
from test_conductor_queue import QueueTest, POLICY


def intent(key, depends=()):
    return {'key': key, 'title': key, 'reason': 'independent goal ' + key, 'dependsOn': list(depends),
            'spec': {'goal': key, 'completionCriteria': key + ' checked', 'writeScope': [], 'workspace': {'mode': 'read-only'}}}


class RoutingFlow(QueueTest):
    def count(self, sql, *args):
        return self.one(sql, *args)['n']

    def mailbox(self, kind):
        return [dict(r) for r in self.store.connection().execute("SELECT * FROM mailbox WHERE kind=? ORDER BY seq", (kind,))]

    def chat_rows(self):
        return self.store.ui_snapshot('chat')['chat']

    def test_resent_input_reuses_the_existing_state_without_a_new_session_or_assignment(self):
        req, d, _ = self.request_with_delegation()
        sent = self.one("SELECT payload FROM inputs WHERE class='conversation'")
        again = self.store.ingest('chat', 'conversation', json.loads(sent['payload']))
        self.pump()
        self.assertEqual(again['disposition'], 'duplicate')
        self.assertEqual((self.count('SELECT COUNT(*) n FROM requests'), self.count('SELECT COUNT(*) n FROM conductors'),
                          self.count('SELECT COUNT(*) n FROM delegations')), (1, 1, 1))

    def test_short_ambiguous_followup_is_held_and_never_attributed_to_the_latest_request(self):
        self.say('write the report', relation='new')
        self.pump()
        held = self.say('that one too')  # no explicit relation or target
        self.pump()
        row = self.one('SELECT status, reason, request_id FROM inputs WHERE id=?', held['inputId'])
        self.assertEqual((row['status'], row['reason'], row['request_id']), ('held', 'relation-ambiguous', None))
        self.assertEqual(self.count("SELECT COUNT(*) n FROM mailbox WHERE kind='user-message'"), 1)
        question = [e for e in self.chat_rows() if e['payload'].get('question') == 'relation']
        self.assertEqual(len(question[0]['payload']['candidates']), 1)

    def test_explicit_followup_continues_the_same_conductor_and_revision(self):
        req, d, _ = self.request_with_delegation()
        self.say('also add a table', requestId=req['id'])
        self.pump()
        items = [r for r in self.mailbox('user-message') if r['request_id'] == req['id']]
        self.assertEqual([r['conductor_id'] for r in items], [req['conductor_id']] * 2)
        self.assertEqual(self.store.public_request(self.one('SELECT * FROM requests WHERE id=?', req['id']))['revision'], 1)

    def test_direct_answer_goes_to_output_without_any_performer(self):
        self.say('what is the default branch?', relation='new')
        self.pump()
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type': 'reply', 'requestId': req['id'], 'text': 'main', 'final': True}])
        self.pump()
        public = self.store.public_request(self.one('SELECT * FROM requests'))
        self.assertEqual((public['status'], public['output']), ('completed', 'completed'))
        self.assertEqual(self.count('SELECT COUNT(*) n FROM delegations'), 0)
        self.assertTrue(any(e['payload']['role'] == 'assistant' and e['payload']['final'] for e in self.chat_rows()))

    def test_multi_domain_request_keeps_original_dependencies_and_traceable_assignments(self):
        self.say('research and then document the result', relation='new')
        self.pump()
        parent = self.one('SELECT * FROM requests')
        self.turn(parent['conductor_id'], [{'type': 'split', 'requestId': parent['id'],
                                            'intents': [intent('research'), intent('document', ['research'])]}])
        self.pump()
        children = {json.loads(r['payload'])['key']: r['request_id'] for r in
                    self.store.connection().execute('SELECT * FROM request_intents')}
        original = self.store.revisions(self.store.connection(), parent['id'])
        self.assertEqual((original[0]['revision'], original[0]['text']), (1, 'research and then document the result'))
        doc = self.store.public_request(self.one('SELECT * FROM requests WHERE id=?', children['document']))
        self.assertEqual((doc['parentId'], doc['intent']['dependencies']), (parent['id'], [children['research']]))
        child = self.one('SELECT * FROM requests WHERE id=?', children['research'])
        self.turn(child['conductor_id'], [{'type': 'delegate', 'requestId': child['id'], 'title': 'research', 'spec': {}}])
        self.pump()
        d = self.one('SELECT * FROM delegations WHERE request_id=?', child['id'])
        correlation = self.store.public_delegation(self.store.connection(), d)['correlation']
        turn = self.one("SELECT id FROM inputs WHERE class='conversation'")['id']
        self.assertEqual({k: correlation[k] for k in ('rootTaskId', 'turnId', 'revision', 'subtaskId', 'assignmentId', 'attempt')},
                         {'rootTaskId': parent['id'], 'turnId': turn, 'revision': 1, 'subtaskId': child['id'],
                          'assignmentId': d['id'], 'attempt': 1})
        self.assertTrue(correlation['conductorSessionId'].startswith(child['conductor_id'] + '-'))

    def test_performer_question_returns_to_the_original_conductor_without_widening_scope(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d['id'], 'started')
        self.worker(d['id'], 'question', question={'text': 'Which file is in scope?'})
        self.pump()
        item = self.mailbox('worker-question')[0]
        self.assertEqual((item['conductor_id'], item['request_id']), (req['conductor_id'], req['id']))
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', d['id'])['status'], 'awaiting-answer')
        self.assertEqual(self.count('SELECT COUNT(*) n FROM delegations'), 1)

    def test_duplicate_result_is_integrated_once_and_conflicting_content_is_flagged(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d['id'], 'started')
        self.worker(d['id'], 'completed', 'done', result={'summary': 'first'})
        self.assertEqual(self.worker(d['id'], 'completed', 'done', result={'summary': 'first'})['disposition'], 'duplicate')
        conflict = self.worker(d['id'], 'completed', 'done', result={'summary': 'different'})
        again = self.worker(d['id'], 'completed', 'done', result={'summary': 'different'})
        self.pump()
        self.assertEqual((conflict['disposition'], again['disposition'], again['conflictId']), ('conflict', 'conflict', conflict['conflictId']))
        self.assertEqual(json.loads(self.one('SELECT result FROM delegations WHERE id=?', d['id'])['result']), {'summary': 'first'})
        self.assertEqual(len(self.mailbox('delegation-result')), 1)
        flagged = self.mailbox('result-conflict')
        self.assertEqual((len(flagged), flagged[0]['conductor_id']), (1, req['conductor_id']))
        self.assertEqual(self.count('SELECT COUNT(*) n FROM input_conflicts'), 1)
        self.assertTrue(any(e['payload'].get('code') == 'result-conflict' for e in self.chat_rows()))

    def test_revision_stops_unstarted_work_and_keeps_late_results_out_of_the_current_answer(self):
        self.say('build A', relation='new')
        self.pump()
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'running', 'spec': {}}])
        self.pump()
        running = self.one("SELECT * FROM delegations WHERE title='running'")
        self.worker(running['id'], 'started')
        self.pump()
        self.say('also part two', requestId=req['id'])  # a new message is not a cancellation or revision
        self.say('and something unrelated?')  # held for the Human, never attributed or cancelling
        self.pump()
        self.assertEqual(self.one('SELECT status, cancel_requested FROM delegations WHERE id=?', running['id']),
                         {'status': 'running', 'cancel_requested': 0})
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'unstarted', 'spec': {}}])
        unstarted = self.one("SELECT * FROM delegations WHERE title='unstarted'")
        req = self.one('SELECT * FROM requests')
        self.control(action='revise', requestId=req['id'], expectedVersion=req['version'], text='build B instead')
        self.store.route_batch()  # the revision is routed before any worker start of the new assignment
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', unstarted['id'])['status'], 'cancelled')
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', running['id'])['status'], 'running')
        history = self.store.revisions(self.store.connection(), req['id'])
        self.assertEqual([(h['revision'], h['text']) for h in history], [(1, 'build A'), (2, 'build B instead')])
        revised = [json.loads(r['payload']) for r in self.mailbox('user-message') if json.loads(r['payload']).get('revision')]
        self.assertEqual((revised[0]['revision'], revised[0]['supersedesRevision']), (2, 1))
        self.worker(running['id'], 'completed', result={'summary': 'A done'})
        self.pump()
        row = self.one('SELECT * FROM delegations WHERE id=?', running['id'])
        self.assertEqual((row['status'], json.loads(row['result'])), ('completed', {'summary': 'A done'}))  # kept on record
        late = self.mailbox('late-result')
        self.assertEqual(len(late), 1)
        self.assertEqual(json.loads(late[0]['payload'])['admission']['reason'], 'stale-revision')
        self.assertEqual([r for r in self.mailbox('delegation-result') if json.loads(r['payload'])['delegation']['id'] == running['id']], [])
        # A new-revision assignment is current again.
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'B', 'spec': {}}])
        current = self.one("SELECT * FROM delegations WHERE title='B'")
        admission = self.store.public_delegation(self.store.connection(), current)['admission']
        self.assertEqual((admission['current'], admission['revision']), (True, 2))

    def test_stale_revision_control_is_rejected(self):
        req, d, _ = self.request_with_delegation()
        self.control(action='revise', requestId=req['id'], expectedVersion=req['version'] - 1, text='other')
        self.pump()
        rejected = json.loads(self.one("SELECT payload FROM events WHERE type='control-rejected'")['payload'])
        self.assertEqual(rejected['reason'], 'version-conflict')
        self.assertEqual(self.store.revision(self.store.connection(), req['id']), 1)

    def test_completion_after_a_cancel_request_is_recorded_but_not_current(self):
        req, d, _ = self.request_with_delegation()
        self.worker(d['id'], 'started')
        self.pump()
        self.control(action='cancel', delegationId=d['id'], expectedVersion=self.one('SELECT version FROM delegations WHERE id=?', d['id'])['version'])
        self.pump()
        self.worker(d['id'], 'completed', result={'summary': 'finished anyway'})
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', d['id'])['status'], 'completed')
        self.assertEqual(json.loads(self.mailbox('late-result')[0]['payload'])['admission']['reason'], 'cancel-requested')
        self.assertEqual(self.mailbox('delegation-result'), [])

    def test_partial_output_is_delivered_but_not_completed_while_work_remains(self):
        self.say('two parts please', relation='new')
        self.pump()
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'rest', 'spec': {}},
                                        {'type': 'reply', 'requestId': req['id'], 'text': 'first part', 'final': False}])
        self.pump()
        public = self.store.public_request(self.one('SELECT * FROM requests'))
        self.assertEqual((public['output'], public['status']), ('delivered', 'delegated'))
        state = [r for r in self.store.ui_snapshot('chat')['state'] if r['key'] == 'request:' + req['id']][0]
        self.assertEqual(state['payload']['output'], 'delivered')
        d = self.one('SELECT * FROM delegations')
        self.worker(d['id'], 'completed', result={'summary': 'rest'})
        self.pump()
        self.turn(req['conductor_id'], [{'type': 'reply', 'requestId': req['id'], 'text': 'all', 'final': True}])
        self.assertEqual(self.store.public_request(self.one('SELECT * FROM requests'))['output'], 'completed')

    def test_restart_restores_completed_records_and_only_reports_unconfirmed_assignments(self):
        req, done, _ = self.request_with_delegation('first')
        self.worker(done['id'], 'started')
        self.worker(done['id'], 'completed', result={'summary': 'ok'})
        self.pump()
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'second', 'spec': {}}])
        executor = self.pump()
        open_ = self.one("SELECT * FROM delegations WHERE title='second'")
        self.store.close()
        restarted = self.open()  # a new process over the same ledger
        report = restarted.recover()
        self.assertNotIn(done['attempt_id'], report['uncertainAttempts'])
        self.assertIn(open_['attempt_id'], report['uncertainAttempts'])
        self.assertEqual(json.loads(self.one('SELECT result FROM delegations WHERE id=?', done['id'], store=restarted)['result']), {'summary': 'ok'})
        # recover() only reports: nothing is started again by the ledger itself.
        self.assertEqual(self.one('SELECT attempt_count FROM delegations WHERE id=?', open_['id'], store=restarted)['attempt_count'], 1)
        self.assertEqual(executor.started.count(open_['attempt_id']), 1)

    def test_end_to_end_with_the_runtime_loop(self):
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0), executors={'fake': FakeExecutor(self.store, step_count=1)})
        self.say('보고서 초안 작업 1개 0초', relation='new')
        self.assertTrue(runtime.drain(30))
        req = self.store.public_request(self.one('SELECT * FROM requests'))
        d = self.one('SELECT * FROM delegations')
        self.assertEqual((req['status'], req['output'], d['status']), ('completed', 'completed', 'completed'))
        self.assertEqual(self.store.public_delegation(self.store.connection(), d)['correlation']['rootTaskId'], req['id'])


class ServeCommandLine(unittest.TestCase):
    """The real conductor_queue.py serve process (fake drivers): ingress → conductor → performer → result → output."""

    def test_revision_through_the_serve_channel_keeps_the_late_result_out_of_the_answer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths.resolve(tmp.name, create=True)
        command = [sys.executable, str(Path(__file__).resolve().parents[2] / 'scripts' / 'conductor_queue.py'), 'serve',
                   '--project-root', tmp.name, '--conductor-delay', '0.05']
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                   env={k: v for k, v in os.environ.items() if k != 'AGENT_FACTORY_CODEX_POOL'})
        self.addCleanup(lambda: process.poll() is None and process.kill())
        lines = []
        threading.Thread(target=lambda: [lines.append(json.loads(line)) for line in process.stdout], daemon=True).start()

        def send(value):
            process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            process.stdin.flush()

        def entries():
            return [e for line in list(lines) if line.get('type') == 'feed' for e in line['entries']]

        def wait(predicate, limit=60):
            end = time.time() + limit
            while time.time() < end:
                found = [value for value in entries() + list(lines) if predicate(value)]
                if found:
                    return found[0]
                time.sleep(0.02)
            self.fail('timed out; saw %s' % [e['payload'].get('role') or e['key'] for e in entries()][-30:])
        wait(lambda l: l.get('role') == 'leader')
        send({'op': 'ensure-chat', 'id': 1, 'chatId': 'c', 'policy': POLICY})
        wait(lambda l: l.get('type') == 'reply' and l.get('id') == 1 and l.get('ok'))
        send({'op': 'ingest', 'id': 2, 'chatId': 'c', 'class': 'conversation',
              'payload': {'clientMessageId': 'm1', 'text': '보고서 작업 1개 2초', 'relation': 'new'}})
        wait(lambda e: e.get('kind') == 'state' and e['key'].startswith('worker:') and e['payload']['status'] == 'running')
        send({'op': 'snapshot', 'id': 3, 'chatId': 'c'})
        snapshot = wait(lambda l: l.get('type') == 'reply' and l.get('id') == 3)['result']
        request = [r['payload'] for r in snapshot['state'] if r['key'].startswith('request:')][0]
        send({'op': 'ingest', 'id': 4, 'chatId': 'c', 'class': 'control', 'payload': {
            'commandId': 'revise-1', 'action': 'revise', 'requestId': request['id'], 'expectedVersion': request['version'],
            'text': '방향을 B로 변경'}})
        wait(lambda e: e.get('kind') == 'chat' and e['payload'].get('status') == 'revised')
        late = wait(lambda e: e.get('kind') == 'chat' and e['payload'].get('role') == 'status' and e['payload'].get('delegationId')
                    and (e['payload'].get('admission') or {}).get('current') is False)
        self.assertEqual(late['payload']['admission']['reason'], 'stale-revision')
        final = wait(lambda e: e.get('kind') == 'chat' and e['payload'].get('role') == 'assistant' and e['payload'].get('final'))
        self.assertIn('반영하지 않음(stale-revision)', final['payload']['text'])
        send({'op': 'snapshot', 'id': 5, 'chatId': 'c'})
        snapshot = wait(lambda l: l.get('type') == 'reply' and l.get('id') == 5)['result']
        request = [r['payload'] for r in snapshot['state'] if r['key'] == 'request:' + request['id']][0]
        self.assertEqual((request['status'], request['revision'], request['output']), ('completed', 2, 'completed'))
        worker = [r['payload'] for r in snapshot['state'] if r['key'].startswith('worker:')][0]
        self.assertEqual((worker['correlation']['rootTaskId'], worker['correlation']['revision']), (request['id'], 1))
        process.stdin.close()
        self.assertEqual(process.wait(10), 0)


if __name__ == '__main__':
    unittest.main()
