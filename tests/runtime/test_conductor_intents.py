"""Intent DAG, concurrent admission, persistent recovery and Work Unit adapter checks."""
import runtime_test_home  # noqa: F401
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from storage import paths
from storage.errors import ContractError
from conductor import intents
from conductor.runtime import FakeConductor, ManagedExecutor, Runtime
from conductor.work_units import WorkUnits
from tasks import workspaces
from test_conductor_queue import QueueTest, Recorder, POLICY


def intent(key, scope=(), mode='read-only', depends=()):
    return {'key': key, 'title': key, 'reason': 'independently assessable goal ' + key, 'dependsOn': list(depends),
            'spec': {'goal': key, 'completionCriteria': key + ' checked', 'writeScope': list(scope), 'workspace': {'mode': mode}}}


class IntentValidation(unittest.TestCase):
    def test_disjoint_goals_dependencies_and_conflicts(self):
        plan = intents.validate([intent('a'), intent('b'), intent('c', depends=['a'])])
        self.assertEqual([i['parallelCandidate'] for i in plan], [True, True, False])
        code = {'mode': 'code', 'repositories': [{'path': '.', 'targetBranch': 'main', 'checks': [['true']]}]}
        a, b = intent('a', ['src/a.py'], 'code'), intent('b', ['src/b.py'], 'code')
        for item in (a, b):
            item['spec']['workspace'] = code
        self.assertTrue(all(i['parallelCandidate'] for i in intents.validate([a, b])))
        b['spec']['writeScope'] = ['src/*.py']
        overlap = intents.validate([a, b])
        self.assertEqual(overlap[1]['dependsOn'], ['a'])
        self.assertFalse(any(i['parallelCandidate'] for i in overlap))
        shared = intents.validate([intent('a', ['a'], 'shared'), intent('b', ['b'], 'shared')])
        self.assertEqual(shared[1]['dependsOn'], ['a'])  # one shared index

    def test_invalid_or_ambiguous_plan_does_not_guess(self):
        for items in ([intent('a'), intent('a')], [intent('a', depends=['b']), intent('b', depends=['a'])],
                      [intent('a', ['../secret'], 'shared'), intent('b')], [intent('a', depends=['missing']), intent('b')]):
            with self.subTest(items=items), self.assertRaises(ContractError):
                intents.validate(items)


class IntentLedger(QueueTest):
    def split(self, items):
        self.say('two independent goals', relation='new')
        self.pump()
        parent = self.one('SELECT * FROM requests')
        ctx, result = self.turn(parent['conductor_id'], [{'type': 'split', 'requestId': parent['id'], 'intents': items}])
        self.assertNotIn('rejected', result['applied'][0])
        self.pump()
        return parent, [dict(r) for r in self.store.connection().execute('SELECT * FROM requests WHERE parent_id=? ORDER BY created_at', (parent['id'],))]

    def delegate(self, children):
        for child in children:
            self.turn(child['conductor_id'], [{'type': 'delegate', 'requestId': child['id'], 'title': child['title'], 'spec': {}}])
        return self.pump()

    def test_two_intents_run_together_and_followup_keeps_exact_owner(self):
        parent, children = self.split([intent('a'), intent('b')])
        recorder = self.delegate(children)
        self.assertEqual(len(recorder.started), 2)
        self.assertEqual(len({c['conductor_id'] for c in children}), 2)
        plan = next(row['payload'] for row in self.store.ui_snapshot('chat')['state'] if row['kind'] == 'plan')
        self.assertEqual(plan['count'], 2)
        self.say('refine a', requestId=children[0]['id'])
        self.pump()
        last = self.one('SELECT * FROM mailbox ORDER BY seq DESC LIMIT 1')
        self.assertEqual((last['request_id'], last['conductor_id']), (children[0]['id'], children[0]['conductor_id']))
        # Replaying the parent turn cannot make more children.
        self.assertEqual(self.one('SELECT COUNT(*) n FROM requests')['n'], 3)
        self.say('refine the combined request', requestId=parent['id'])
        self.pump()
        self.assertEqual(self.one('SELECT status FROM inputs ORDER BY seq DESC LIMIT 1')['status'], 'held')

    def test_dependency_and_shared_scope_wait_survive_restart(self):
        parent, children = self.split([intent('a', ['src/a'], 'shared'), intent('b', ['src/a'], 'shared')])
        self.assertEqual(len(self.delegate(children).started), 1)
        restarted = self.open()
        self.assertEqual(restarted.connection().execute('SELECT COUNT(*) FROM attempt_waits').fetchone()[0], 1)
        d = self.one('SELECT * FROM delegations WHERE request_id=?', children[0]['id'])
        self.worker(d['id'], 'completed', result={'ok': True})
        self.pump()
        self.turn(children[0]['conductor_id'], [{'type': 'reply', 'requestId': children[0]['id'], 'text': 'done', 'final': True}])
        self.assertEqual(restarted.schedule_waiting(), 1)
        recorder = self.pump(store=restarted)
        self.assertEqual(len(recorder.started), 1)
        self.assertEqual(restarted.schedule_waiting(), 0)

    def test_cancel_parent_and_one_failure_do_not_cancel_other_independent_request(self):
        parent, children = self.split([intent('a'), intent('b')])
        self.delegate(children)
        a, b = [self.one('SELECT * FROM delegations WHERE request_id=?', child['id']) for child in children]
        self.worker(a['id'], 'failed', error={'code': 'injected', 'retryable': False})
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', b['id'])['status'], 'dispatched')
        self.control(action='cancel', requestId=parent['id'], expectedVersion=self.one('SELECT version FROM requests WHERE id=?', parent['id'])['version'])
        recorder = self.pump()
        self.assertEqual(recorder.cancelled, [b['attempt_id']])
        self.worker(b['id'], 'completed', result={'race': 'finished'})
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', b['id'])['status'], 'completed')

    def test_fake_single_message_split_completes_and_synthesizes(self):
        self.say('independent: report 1 task 0.05s\nindependent: tests 1 task 0.05s', relation='new')
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0))
        self.assertTrue(runtime.drain(10))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM requests')['n'], 3)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM requests WHERE status='completed'")['n'], 3)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM delegations WHERE status='completed'")['n'], 2)


class QueueWorkUnits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        workspaces.worktrees.git(self.root, 'init', '-b', 'main')
        workspaces.worktrees.git(self.root, 'config', 'user.name', 'Fixture')
        workspaces.worktrees.git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        (self.root / 'a.txt').write_text('base\n')
        workspaces.worktrees.git(self.root, 'add', 'a.txt')
        workspaces.worktrees.git(self.root, 'commit', '-m', 'base')
        location = paths.resolve(self.root, create=True)
        self.adapter = WorkUnits(self.root, Path(location['runtimeRoot']) / 'conductor-queue.sqlite3')
        self.request = {'session': {'agentId': 'work-cq-fixture'}, 'task': {'id': 'del-fixture', 'spec': {
            'goal': 'edit a', 'completionCriteria': 'checked', 'writeScope': ['a.txt'],
            'workspace': {'mode': 'code', 'repositories': [{'path': str(self.root), 'targetBranch': 'main',
                        'checks': [[sys.executable, '-c', 'pass']]}]}}}}

    def test_binding_duplicate_prepare_mixed_index_merge_and_cleanup(self):
        binding = self.adapter.prepare(self.request)
        self.assertEqual(self.adapter.prepare(copy.deepcopy(self.request)), binding)
        import exec as runtime
        args = runtime.parse_args(['submit', '--agent', 'work-cq-fixture', '--role', 'work', '--task-id', 'del-fixture', '--task-workspace-file', str(binding)])
        self.assertEqual(workspaces.dispatch_binding(runtime, args, self.root)['mode'], 'code')
        unit = self.adapter.snapshot('del-fixture')['repositories'][0]
        path = Path(unit['path'])
        self.assertNotEqual(workspaces.worktrees.git(path, 'rev-parse', '--git-path', 'index').stdout,
                            workspaces.worktrees.git(self.root, 'rev-parse', '--git-path', 'index').stdout)
        (self.root / 'shared.txt').write_text('shared staged')
        workspaces.worktrees.git(self.root, 'add', 'shared.txt')
        before = workspaces.worktrees.git(self.root, 'diff', '--cached').stdout
        (path / 'a.txt').write_text('isolated\n')
        with self.assertRaises(ContractError):
            self.adapter.action('del-fixture', 'integrate')
        result = self.adapter.action('del-fixture', 'integrate', work={'status': 'completed', 'runId': 'fixture'},
            receipt={'tests': {'run': True}, 'changedPaths': ['a.txt']}, authorization='fixture-human-exact-merge')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(workspaces.worktrees.git(self.root, 'diff', '--cached').stdout, before)
        self.assertEqual((self.root / 'a.txt').read_text(), 'isolated\n')
        self.assertTrue(path.exists())
        result = self.adapter.action('del-fixture', 'cleanup', authorization='fixture-human-exact-cleanup')
        self.assertTrue(result['workspace']['repositories'][0]['cleaned'])

    def test_creation_and_checks_failure_preserve_exact_worktree(self):
        with mock.patch.object(workspaces, 'validate_unit', side_effect=ContractError('injected', 'creation interrupted')):
            with self.assertRaises(ContractError):
                self.adapter.prepare(self.request)
        snapshot = self.adapter.snapshot('del-fixture')
        self.assertEqual(snapshot['phase'], 'creation-failed')
        self.assertTrue(Path(snapshot['repositories'][0]['path']).exists())
        self.adapter.prepare(self.request)
        state_path = self.adapter.state_path('del-fixture')
        state = json.loads(state_path.read_text())
        state['taskWorkspaces']['del-fixture']['repositories'][0]['checks'] = [[sys.executable, '-c', 'raise SystemExit(7)']]
        from storage import files
        files.atomic_write_json(state_path, state)
        with self.assertRaises(ContractError):
            self.adapter.action('del-fixture', 'integrate', work={'status': 'completed', 'runId': 'fixture'},
                receipt={'tests': {'run': True}, 'changedPaths': []}, authorization='fixture-exact-action')
        self.assertTrue(Path(snapshot['path']).exists())
        self.assertEqual(self.adapter.snapshot('del-fixture')['repositories'][0]['phase'], 'check-failed')

    def test_merge_conflict_and_cleanup_failure_are_retained(self):
        self.adapter.prepare(self.request)
        path = Path(self.adapter.snapshot('del-fixture')['path'])
        for root, text in ((self.root, 'target\n'), (path, 'worker\n')):
            (root / 'a.txt').write_text(text)
            workspaces.worktrees.git(root, 'add', 'a.txt')
            workspaces.worktrees.git(root, 'commit', '-m', text.strip())
        result = self.adapter.action('del-fixture', 'integrate', work={'status': 'completed', 'runId': 'fixture'},
            receipt={'tests': {'run': True}, 'changedPaths': []}, authorization='fixture-exact-merge')
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['files'], ['a.txt'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'target\n')
        with mock.patch.object(workspaces, 'cleanup_unit', side_effect=ContractError('injected_cleanup', 'retained')):
            with self.assertRaises(ContractError):
                self.adapter.action('del-fixture', 'cleanup', authorization='fixture-exact-cleanup')
        self.assertTrue(path.exists())
        self.assertEqual(self.adapter.snapshot('del-fixture')['error']['code'], 'injected_cleanup')


class ManagedAdmission(QueueTest):
    def test_submissions_overlap_lost_ack_is_looked_up_and_zombie_does_not_stop_peer(self):
        import threading
        policy = {**POLICY, 'executor': 'managed', 'sandbox': 'workspace-write', 'work': {'model': 'stub'}}
        self.store.ensure_chat('managed', policy)
        self.store.ingest('managed', 'conversation', {'clientMessageId': 'one', 'text': 'independent', 'relation': 'new', 'area': self.area})
        self.pump()
        req = self.one("SELECT * FROM requests WHERE chat_id='managed'")
        self.turn(req['conductor_id'], [ {'type': 'delegate', 'requestId': req['id'], 'title': name,
            'spec': {'goal': name, 'writeScope': [], 'workspace': {'mode': 'read-only'}}} for name in ('lost-ack', 'zombie')])
        from conductor.pool import DEFAULT
        configuration = copy.deepcopy(DEFAULT)
        configuration['worker']['subdomainLimit'] = 2
        self.store.pool.configure(configuration, 'fixture setting')
        barrier = threading.Barrier(2)
        class Backend:
            def __init__(self):
                self.runs, self.submissions = {}, []
            def lookup(self, request):
                return self.runs.get(request['run']['dispatchId'])
            def submit(self, request):
                self.submissions.append(request)
                barrier.wait(timeout=3)
                run = {'runId': request['run']['dispatchId']}
                self.runs[run['runId']] = run
                if request['message']['text'] == 'lost-ack':
                    raise ContractError('managed_response_invalid', 'injected lost acknowledgement')
                return run
            def call(self, args):
                return {'run': {'status': 'running' if args[-1] == self.submissions[1]['run']['dispatchId'] else 'completed'}}
            def health(self, external):
                return {'stopped': True, 'zombie': external['runId'] == self.submissions[1]['run']['dispatchId']}
        backend = Backend()
        executor = ManagedExecutor(self.store, self.project, backend=backend, poll_seconds=0.001)
        runtime = Runtime(self.store, executors={'managed': executor, 'fake': Recorder()}, conductor=FakeConductor(delay=0, chunk_delay=0))
        self.assertTrue(runtime.drain(10))
        self.assertEqual(len(backend.submissions), 2)
        states = [r['status'] for r in self.store.connection().execute("SELECT * FROM delegations WHERE chat_id='managed'")]
        self.assertCountEqual(states, ['completed', 'failed'])
        runtime.recover()
        self.assertEqual(len(backend.submissions), 2)

    def test_worker_question_answer_recovery_and_heartbeat_timeout_are_observed(self):
        from conductor.runtime import FakeExecutor
        req, delegation, _ = self.request_with_delegation()
        self.worker(delegation['id'], 'question', question={'text': 'Which format?', 'decisionKind': 'clarification'})
        self.pump()
        snapshot = self.store.ui_snapshot('chat')
        question = next(e['payload'] for e in snapshot['chat'] if e['payload'].get('question') == 'worker')
        self.assertEqual((question['text'], question['delegationId']), ('Which format?', delegation['id']))
        current = self.one('SELECT * FROM delegations WHERE id=?', delegation['id'])
        self.control(action='answer-worker', delegationId=delegation['id'], expectedVersion=current['version'], text='table')
        self.pump()
        # Crash before the operator effect runs. Recovered queue keeps the exact answer once.
        restarted = self.open()
        executor = FakeExecutor(restarted)
        runtime = Runtime(restarted, executors={'fake': executor}, conductor=FakeConductor(delay=0, chunk_delay=0))
        runtime.recover()
        self.assertTrue(runtime.drain(10))
        self.assertEqual(self.one("SELECT COUNT(*) n FROM operator_effects WHERE status='done'")['n'], 1)
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', delegation['id'])['status'], 'completed')
        self.assertEqual(json.loads(self.one('SELECT result FROM delegations WHERE id=?', delegation['id'])['result'])['summary'], 'table')
        self.assertEqual(restarted.schedule_waiting(), 0)

        # Slow live process remains running: timeout is an observation, never a forced failure.
        self.say('new', relation='new')
        self.pump()
        request = self.one('SELECT * FROM requests ORDER BY created_at DESC LIMIT 1')
        self.turn(request['conductor_id'], [{'type': 'delegate', 'requestId': request['id'], 'title': 'slow', 'spec': {}}])
        self.pump()
        slow = self.one('SELECT * FROM delegations WHERE request_id=?', request['id'])
        self.store.record_external(slow['attempt_id'], {'runId': 'slow-run', 'agentId': 'stub'})
        class Backend:
            count = 0
            def call(self, args):
                return {'run': {'status': 'running' if self.count == 0 else 'completed'}}
            def health(self, external):
                self.count += 1
                return {'stopped': self.count > 1, 'heartbeatStale': self.count == 1}
        managed = ManagedExecutor(self.store, self.project, backend=Backend(), poll_seconds=0.001)
        managed.observe(self.store.attempt_context(slow['attempt_id']))
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', slow['id'])['status'], 'completed')
        observations = [json.loads(r['payload']) for r in self.store.connection().execute("SELECT payload FROM events WHERE type='worker-observation'")]
        self.assertTrue(any(o['reason'] == 'heartbeat-timeout' for o in observations))

    def test_cancellation_of_paused_question_finishes_without_another_dispatch(self):
        req, delegation, _ = self.request_with_delegation()
        self.worker(delegation['id'], 'question', question={'text': 'Choose format'})
        self.pump()
        current = self.one('SELECT * FROM delegations WHERE id=?', delegation['id'])
        self.control(action='cancel', delegationId=delegation['id'], expectedVersion=current['version'])
        recorder = self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', delegation['id'])['status'], 'cancelled')
        self.assertEqual(recorder.started, [])
        self.assertEqual(recorder.cancelled, [])

    def test_restart_keeps_a_paused_question_without_restarting_its_worker(self):
        req, delegation, _ = self.request_with_delegation()
        self.worker(delegation['id'], 'started')
        self.worker(delegation['id'], 'question', question={'text': 'Which format?'})
        self.pump()
        restarted = self.open()
        report = restarted.recover()
        self.assertNotIn(delegation['attempt_id'], report['uncertainAttempts'])
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', delegation['id'])['status'], 'awaiting-answer')
