"""One logical conductor: attributed physical sessions, bounded admission and fixed decisions."""
import runtime_test_home  # noqa: F401
import copy
import json
import types
from unittest import mock

from storage.errors import ContractError
from conductor import domains
from conductor.backend import ManagedBackend
from conductor.pool import DEFAULT
from conductor.runtime import FakeConductor, Runtime, ManagedConductor
from test_conductor_queue import QueueTest, POLICY


class Pools(QueueTest):
    def setUp(self):
        super().setUp()
        self.policy = {**POLICY, 'employment': True, 'conductorSettings': {'model': 'stub', 'provider': 'codex'}}
        self.store.ensure_chat('pool', self.policy)
        self.area_b = {'domainId': self.area['domainId'], 'subdomainId': domains.subdomain(self.db.parent, self.area['domainId'], 'Browser', 'human', 'fixture', write_scope=[])['subdomain']['id']}

    def send(self, text='bounded work', area=None, **extra):
        self.seq += 1
        value = {'clientMessageId': 'pool-%s' % self.seq, 'text': text, 'relation': 'new', **extra}
        if area:
            value['area'] = area
        result = self.store.ingest('pool', 'conversation', value)
        self.pump()
        return result

    def finish(self, delegation, kind='completed', **extra):
        self.store.ingest('pool', 'lifecycle', {'delegationId': delegation['id'], 'attemptId': delegation['attempt_id'],
                         'workerEventId': kind, 'kind': kind, **extra})
        self.pump()

    def command(self, **fields):
        self.seq += 1
        self.store.ingest('pool', 'control', {'commandId': 'control-%d' % self.seq, **fields})
        self.pump()

    def config(self, **worker):
        value = copy.deepcopy(DEFAULT)
        value['worker'].update(worker)
        self.store.pool.configure(value, 'fixture Human setting')

    def test_unknown_area_asks_before_any_session_is_hired_and_resolution_preserves_input(self):
        message = self.send()
        self.assertEqual(self.one('SELECT COUNT(*) n FROM conductors')['n'], 0)
        self.assertEqual(self.one('SELECT reason FROM inputs WHERE id=?', message['inputId'])['reason'], 'area-required')
        questions = [r['payload'] for r in self.store.ui_snapshot('pool')['state'] if r['kind'] == 'question']
        self.assertEqual(questions[0]['kind'], 'area')
        self.command(action='resolve-area', inputId=message['inputId'], area=self.area)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM conductors')['n'], 1)
        request = self.one('SELECT * FROM requests')
        decision = next(d for d in self.store.pool.decisions(self.store.connection(), request['id']) if d['kind'] == 'assignment')
        self.assertTrue(decision['source'].startswith('control:'))
        self.assertEqual(decision['actor'], 'human')

    def test_conductor_limit_queues_all_followups_then_reuses_the_same_instance(self):
        # The limit bounds conductor turns running at once: with one slot, a second request waits only while the
        # first instance is actually in a turn (not merely while it owns an open request).
        value = copy.deepcopy(DEFAULT)
        value['conductor'].update(domainLimit=1, subdomainLimit=1)
        self.store.pool.configure(value, 'Human one-turn cap')
        self.send(area=self.area)
        first = self.one('SELECT * FROM requests')
        running = self.store.begin_turn(first['conductor_id'])
        second = self.send('second', self.area)
        waiting = self.one("SELECT * FROM requests WHERE status='queued-for-instance'")
        self.send('additional requirement', requestId=waiting['id'], relation='followup')
        self.assertEqual(len(json.loads(self.one('SELECT message FROM request_waits')['message'])), 2)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM conductors')['n'], 1)
        self.store.end_turn({k: running[k] for k in ('conductorId', 'slot', 'epoch', 'turnId')},
                            [{'type': 'reply', 'requestId': first['id'], 'text': 'done', 'final': True}])
        self.assertEqual(self.store.schedule_instances(), 1)
        self.pump()
        next_request = self.one('SELECT * FROM requests WHERE id=?', waiting['id'])
        self.assertEqual(next_request['conductor_id'], first['conductor_id'])
        self.assertEqual(self.one("SELECT COUNT(*) n FROM mailbox WHERE request_id=? AND status='queued'", waiting['id'])['n'], 2)
        snapshot = self.store.ui_snapshot('pool')
        self.assertEqual(snapshot['logicalConductor']['instanceCount'], 1)
        self.assertEqual(snapshot['logicalConductor']['id'], 'logical-conductor')
        self.assertEqual(self.one('SELECT status FROM inputs WHERE id=?', second['inputId'])['status'], 'routed')

    def test_worker_reuse_is_first_choice_and_cap_wait_survives_restart(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': name,
                    'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}} for name in ('one', 'two')])
        recorder = self.pump()
        self.assertEqual(len(recorder.started), 1)
        delegations = [dict(r) for r in self.store.connection().execute('SELECT * FROM delegations ORDER BY created_at')]
        one, two = delegations
        worker = self.store.attempt_context(one['attempt_id'])['worker']['id']
        self.assertEqual(self.one('SELECT reason FROM attempt_waits')['reason'], 'employment-limit:subdomain')
        restarted = self.open()
        self.assertEqual(restarted.connection().execute('SELECT COUNT(*) FROM attempt_workers').fetchone()[0], 1)
        self.finish(one, result={'ok': True})
        self.assertEqual(restarted.schedule_waiting(), 1)
        self.pump(store=restarted)
        self.assertEqual(restarted.attempt_context(two['attempt_id'])['worker']['id'], worker)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM pool_members WHERE role='worker'")['n'], 1)
        self.finish(one, 'failed', error={'code': 'late', 'retryable': False})
        self.assertEqual(self.one('SELECT state FROM pool_members WHERE id=?', worker)['state'], 'busy')

    def test_two_child_areas_execute_together_and_one_failure_does_not_stop_the_other(self):
        self.send('independent goals', self.area)
        root = self.one('SELECT * FROM requests')
        plan = [{'key': str(n), 'title': str(n), 'reason': 'separate goal', 'spec': {'goal': 'part%d 1 task 0.05s' % n,
                   'completionCriteria': 'checked', 'writeScope': [], 'workspace': {'mode': 'read-only'}, 'area': area}}
                for n, area in enumerate((self.area, self.area_b))]
        self.turn(root['conductor_id'], [{'type': 'split', 'requestId': root['id'], 'intents': plan}])
        self.pump()
        children = [dict(r) for r in self.store.connection().execute('SELECT * FROM requests WHERE parent_id=?', (root['id'],))]
        for child in children:
            self.turn(child['conductor_id'], [{'type': 'delegate', 'requestId': child['id'], 'title': child['title'], 'spec': {}}])
        self.assertEqual(len(self.pump().started), 2)
        jobs = [dict(r) for r in self.store.connection().execute('SELECT * FROM delegations')]
        self.finish(jobs[0], 'failed', error={'code': 'injected', 'retryable': False})
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?', jobs[1]['id'])['status'], 'dispatched')
        self.assertEqual(self.one("SELECT COUNT(*) n FROM pool_members WHERE role='worker'")['n'], 2)
        instances = [r['payload'] for r in self.store.ui_snapshot('pool')['state'] if r['kind'] == 'conductor']
        self.assertTrue(all(c['role'] == 'conductor-instance' and c['logicalConductorId'] == 'logical-conductor' for c in instances))

    def test_domain_limit_and_scope_arbitration_are_centralized(self):
        value = copy.deepcopy(DEFAULT)
        value['worker']['domainLimit'] = 1
        self.store.pool.configure(value, 'fixture domain cap')
        self.send(area=self.area)
        self.send(area=self.area_b)
        requests = [dict(r) for r in self.store.connection().execute('SELECT * FROM requests')]
        for req in requests:
            self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': req['title'],
                         'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}}])
        self.assertEqual(len(self.pump().started), 1)
        self.assertEqual(self.one('SELECT reason FROM attempt_waits')['reason'], 'employment-limit:domain')
        first = self.one('SELECT * FROM delegations ORDER BY created_at LIMIT 1')
        self.finish(first, result={'ok': True})
        self.assertEqual(self.store.schedule_waiting(), 1)  # Idle sessions do not consume concurrent execution slots.
        self.assertEqual(len(self.pump().started), 1)

    def test_delegate_requires_paths_or_explicit_recorded_subdomain_scope(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        context = self.store.begin_turn(req['conductor_id'])
        token = {k: context[k] for k in ('conductorId', 'slot', 'epoch', 'turnId')}
        result = self.store.end_turn(token, [{'type': 'delegate', 'requestId': req['id'], 'title': 'missing', 'spec': {'goal': 'g'}}])
        self.assertEqual(result['applied'][0]['rejected'], 'scope_required')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM delegations')['n'], 0)
        # A recorded subdomain declaration resolves exactly its approved paths, never the whole tree.
        self.store.enqueue(self.store.connection(), req['conductor_id'], req['id'], 'user-message', {'text': 'use area'}, 'fixture')
        ctx = self.store.begin_turn(req['conductor_id'])
        self.store.end_turn({k: ctx[k] for k in token}, [{'type': 'delegate', 'requestId': req['id'], 'title': 'declared',
                                    'spec': {'scopeBySubdomain': self.area, 'workspace': {'mode': 'read-only'}}}])
        self.assertEqual(json.loads(self.one('SELECT spec FROM delegations')['spec'])['writeScope'], [])

    def test_confirmed_decisions_and_unanswered_question_survive_slot_handoff(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        self.command(action='confirm-decision', requestId=req['id'], expectedVersion=req['version'], decisionKind='scope', value={'paths': ['approved.py']})
        before = self.store.pool.decisions(self.store.connection(), req['id'])
        original_epoch = json.loads(self.one('SELECT handoff FROM conductors')['handoff'])['epoch']
        self.turn(req['conductor_id'], [{'type': 'ask', 'requestId': req['id'], 'text': 'Which output?'}], {'usedTokens': 80, 'contextWindowTokens': 100})
        question = self.one("SELECT * FROM questions WHERE kind='conductor'")
        self.assertEqual((question['slot'], question['epoch']), ('B', original_epoch + 1))
        con = self.one('SELECT * FROM conductors')
        checkpoint = json.loads(con['checkpoint'])
        self.assertEqual(checkpoint['fixedDecisions'], before)
        self.assertEqual(checkpoint['pendingQuestions'][0]['id'], question['id'])
        self.command(action='answer-question', requestId=req['id'], expectedVersion=self.one('SELECT version FROM requests')['version'], questionId=question['id'], text='table')
        context = self.store.begin_turn(req['conductor_id'])
        self.assertEqual((context['slot'], context['epoch']), ('B', original_epoch + 1))
        self.assertEqual(context['fixedDecisions'], before)
        self.assertEqual(context['pendingQuestions'], [])
        request = ManagedConductor(self.project, self.store, backend=object()).request(context)
        payload = json.loads(request['message']['text'].split('\n\n', 1)[1])
        self.assertEqual(payload['fixedDecisions'], before)
        self.assertIn('ONE logical conductor', request['message']['text'])
        with self.assertRaises(ContractError):
            d = before[0]
            self.store.pool.decide(self.store.connection(), req['id'], d['kind'], {'changed': True}, 'different', 'human', 'area')

    def test_subdomain_addition_preserves_protected_domain_names_and_assignments(self):
        legacy = domains.read(self.db.parent)
        domains.assign(self.db.parent, 'work-human', self.area['domainId'], 'human', 'fixture')
        old = domains.read(self.db.parent)
        other = domains.create(self.db.parent, 'Other tests', 'human', 'fixture')['domainId']
        child = domains.subdomain(self.db.parent, other, 'Child', 'human', 'fixture')['subdomain']['id']
        with self.assertRaisesRegex(ContractError, 'Human placed'):
            domains.assign_subdomain(self.db.parent, 'work-human', other, child, 'ai', 'queue')
        current = domains.read(self.db.parent)
        self.assertEqual(current['assignments']['work-human'], old['assignments']['work-human'])
        self.assertEqual(current['domains'][:len(legacy['domains'])], legacy['domains'])

    def test_explicit_existing_idle_session_enrollment_is_reused_without_model_change(self):
        policy = {**self.policy, 'executor': 'managed', 'sandbox': 'workspace-write', 'work': {'model': 'stub', 'provider': 'codex', 'reasoningEffort': 'medium'}}
        self.store.ensure_chat('native', policy)
        agent = 'work-existing'
        session = self.db.parent / 'agents' / agent / 'session.json'
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({'agentId': agent, 'role': 'work', 'model': 'stub', 'provider': 'codex', 'reasoningEffort': 'medium',
                                       'sandbox': 'workspace-write', 'humanApprovalPolicy': 'bypass'}))
        self.store.pool.enroll_worker('native', agent, self.area, 'Human exact enrollment')
        self.store.ingest('native', 'conversation', {'clientMessageId': 'native', 'text': 'bounded', 'relation': 'new', 'area': self.area})
        self.pump()
        req = self.one("SELECT * FROM requests WHERE chat_id='native'")
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'bounded',
                                       'spec': {'writeScope': [], 'workspace': {'mode': 'shared'}}}])
        self.pump()
        attempt = self.one("SELECT a.id FROM attempts a JOIN delegations d ON d.attempt_id=a.id WHERE d.request_id=?", req['id'])['id']
        from conductor.runtime import ManagedExecutor
        context = self.store.attempt_context(attempt)
        request = ManagedExecutor(self.store, self.project, backend=object()).request(context)
        self.assertEqual((request['session']['agentId'], request['session']['managedCreated']), (agent, True))
        self.assertEqual(request['session']['settings'], policy['work'])
        backend = ManagedBackend(types.SimpleNamespace(root=self.project, path=self.db, chat_id='conductor-queue'))
        with mock.patch.object(backend, 'call', return_value={'run': {'runId': 'stub'}}) as called, mock.patch.dict('os.environ', {}, clear=True):
            backend.submit(request)
        self.assertEqual(called.call_args.args[0][0], 'send')
        self.assertEqual(json.loads(session.read_text())['model'], 'stub')
        placement = domains.read(self.db.parent)
        self.assertEqual(placement['subdomainAssignments'][agent]['subdomainId'], self.area['subdomainId'])

    def test_three_independent_areas_make_progress_under_two_instance_slots(self):
        third = {'domainId': self.area['domainId'], 'subdomainId': domains.subdomain(self.db.parent, self.area['domainId'], 'Storage', 'human', 'fixture', write_scope=[])['subdomain']['id']}
        self.send('three goals', self.area)
        root = self.one('SELECT * FROM requests')
        items = [{'key': str(n), 'title': str(n), 'reason': 'independent bounded goal', 'spec': {
            'goal': 'part%d 1 task 0.05s' % n, 'completionCriteria': 'checked', 'writeScope': [],
            'workspace': {'mode': 'read-only'}, 'area': area}} for n, area in enumerate((self.area, self.area_b, third))]
        self.turn(root['conductor_id'], [{'type': 'split', 'requestId': root['id'], 'intents': items}])
        # Instances waiting for work hold no slot, so no child waits for an instance; turns are bounded at start.
        self.assertEqual(self.one("SELECT COUNT(*) n FROM requests WHERE status='queued-for-instance'")['n'], 0)
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0))
        self.assertTrue(runtime.drain(10))
        self.assertEqual(self.one("SELECT COUNT(*) n FROM requests WHERE status='completed'")['n'], 4)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM delegations WHERE status='completed'")['n'], 3)

    def test_missing_scope_overlap_waits_between_instances_and_cap_changes_are_recoverable(self):
        self.send(area=self.area)
        self.send(area=self.area_b)
        requests = [dict(r) for r in self.store.connection().execute('SELECT * FROM requests')]
        for req in requests:
            self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'shared file',
                'spec': {'writeScope': ['shared.py'], 'workspace': {'mode': 'shared'}}}])
        self.assertEqual(len(self.pump().started), 1)
        self.assertTrue(self.one('SELECT reason FROM attempt_waits')['reason'].startswith('resource:'))
        first = self.one('SELECT * FROM delegations ORDER BY created_at LIMIT 1')
        self.finish(first, result={'ok': True})
        self.assertEqual(self.store.schedule_waiting(), 1)
        self.assertEqual(len(self.pump().started), 1)

    def test_idle_retirement_preserves_history_and_reactivates_the_same_session(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'one', 'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}}])
        self.pump()
        first = self.one('SELECT * FROM delegations')
        worker = self.store.attempt_context(first['attempt_id'])['worker']['id']
        self.finish(first, result={'ok': True})
        self.config(idleRetentionSeconds=0)
        with self.store.tx() as c:
            self.store.pool.retire_idle(c)
        self.assertEqual(self.one('SELECT state FROM pool_members WHERE id=?', worker)['state'], 'retired')
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'two', 'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}}])
        self.pump()
        second = self.one('SELECT * FROM delegations ORDER BY created_at DESC LIMIT 1')
        self.assertEqual(self.store.attempt_context(second['attempt_id'])['worker']['id'], worker)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM delegations')['n'], 2)

    def test_real_approval_is_fixed_and_cancelled_questions_are_no_longer_pending(self):
        required = {**self.policy, 'approvalPolicy': 'required'}
        self.store.ensure_chat('approval', required)
        self.store.ingest('approval', 'conversation', {'clientMessageId': 'approval', 'text': 'bounded', 'relation': 'new', 'area': self.area})
        self.pump()
        req = self.one("SELECT * FROM requests WHERE chat_id='approval'")
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'title': 'bounded', 'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}}])
        self.pump()
        job = self.one('SELECT * FROM delegations')
        self.assertEqual(job['status'], 'awaiting-approval')
        self.store.ingest('approval', 'control', {'commandId': 'approve-exact', 'action': 'approve', 'delegationId': job['id'], 'expectedVersion': job['version']})
        self.pump()
        approval = next(d for d in self.store.pool.decisions(self.store.connection(), req['id']) if d['kind'] == 'approval')
        self.assertEqual((approval['actor'], approval['source'], approval['value']['delegationId']), ('human', 'control:approve-exact', job['id']))
        self.store.ingest('approval', 'lifecycle', {'delegationId': job['id'], 'attemptId': job['attempt_id'], 'workerEventId': 'ask', 'kind': 'question', 'question': {'text': 'Choose format'}})
        self.pump()
        current = self.one('SELECT * FROM delegations')
        self.store.ingest('approval', 'control', {'commandId': 'cancel-exact', 'action': 'cancel', 'delegationId': job['id'], 'expectedVersion': current['version']})
        self.pump()
        question = self.one('SELECT * FROM questions')
        self.assertEqual(question['state'], 'cancelled')
        self.assertEqual(self.one("SELECT COUNT(*) n FROM questions WHERE state='pending'")['n'], 0)

    def test_reuse_rank_and_scope_permissions_are_independent_of_other_role_settings(self):
        from conductor.pool import signature
        changed = {**self.policy, 'conductorSettings': {'model': 'another-conductor'}}
        scope = {'workspace': {'mode': 'read-only'}}
        self.assertEqual(signature(self.policy, 'worker', scope), signature(changed, 'worker', scope))
        writable = {**self.policy, 'sandbox': 'workspace-write'}
        self.assertNotEqual(signature(writable, 'worker', scope), signature(writable, 'worker', {'workspace': {'mode': 'shared'}}))
        self.config(domainLimit=2, subdomainLimit=2, reuse='newest-idle')
        with self.store.tx() as c:
            for ident, timestamp in [('work-old', 1), ('work-new', 2)]:
                self.store.pool.member(c, ident, 'worker', self.store.pool.area(self.area), 'sig', 'idle')
                c.execute('UPDATE pool_members SET last_used=? WHERE id=?', (timestamp, ident))
            self.assertEqual(self.store.pool.idle(c, 'worker', self.store.pool.area(self.area), 'sig')['id'], 'work-new')
        self.config(domainLimit=2, subdomainLimit=2, reuse='oldest-idle')
        self.assertEqual(self.store.pool.idle(self.store.connection(), 'worker', self.store.pool.area(self.area), 'sig')['id'], 'work-old')

    def test_external_owner_blocks_idle_reuse_and_retirement_without_stopping_it(self):
        from conductor.pool import signature
        worker = 'work-external'
        self.config(domainLimit=1, subdomainLimit=1, idleRetentionSeconds=0)
        with self.store.tx() as c:
            self.store.pool.member(c, worker, 'worker', self.store.pool.area(self.area), signature(self.policy, 'worker'), 'idle')
        run = self.db.parent / 'agents' / worker / 'runs' / 'run-external' / 'state.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps({'status': 'running', 'createdAt': '2026-10-12T00:00:00Z'}))
        before = run.read_bytes()
        with self.store.tx() as c:
            self.assertIsNone(self.store.pool.idle(c, 'worker', self.store.pool.area(self.area), signature(self.policy, 'worker')))
            self.assertEqual(self.store.pool.room(c, 'worker', self.store.pool.area(self.area)), 'employment-limit:domain')
            self.store.pool.retire_idle(c)
        self.assertEqual(self.one('SELECT state FROM pool_members WHERE id=?', worker)['state'], 'idle')
        self.assertEqual(run.read_bytes(), before)

    def test_area_override_hierarchy_preserves_parent_limits_and_unrelated_settings(self):
        self.store.pool.configure({'domains': {self.area['domainId']: {'worker': {'domainLimit': 1, 'reuse': 'newest-idle'}}}}, 'Human domain setting')
        self.store.pool.configure({'subdomains': {self.area_b['subdomainId']: {'worker': {'subdomainLimit': 2}}}}, 'Human child setting')
        rule = self.store.pool.rule(self.store.connection(), 'worker', self.store.pool.area(self.area_b))
        self.assertEqual((rule['domainLimit'], rule['subdomainLimit'], rule['reuse']), (1, 2, 'newest-idle'))
        with self.assertRaises(ContractError):
            self.store.pool.configure({'subdomains': {self.area_b['subdomainId']: {'worker': {'domainLimit': 10}}}}, 'invalid parent bypass')
        with self.assertRaises(ContractError):
            self.store.pool.configure({'domains': {'unknown': {'worker': {'domainLimit': 1}}}}, 'unknown domain')

    def test_unfinished_parent_keeps_its_chat_and_reactivated_turn_obeys_capacity(self):
        self.send(area=self.area)
        root = self.one('SELECT * FROM requests')
        self.turn(root['conductor_id'], [{'type': 'split', 'requestId': root['id'], 'intents': [
            {'key': str(n), 'title': str(n), 'reason': 'independent goal', 'spec': {'goal': str(n), 'completionCriteria': 'checked',
             'writeScope': [], 'workspace': {'mode': 'read-only'}, 'area': area}}
            for n, area in enumerate((self.area, self.area_b))]}])
        self.pump()
        self.store.ensure_chat('other-chat', self.policy)
        area = self.store.pool.area(self.area)
        from conductor.pool import signature
        with self.store.tx() as c:
            c.execute("UPDATE pool_members SET state='idle' WHERE id=?", (root['conductor_id'],))
            self.assertIsNone(self.store.pool.idle(c, 'conductor', area, signature(self.policy, 'conductor'), chat_id='other-chat'))
        # The resumed parent's turn cannot bypass another session's running turn under a one-turn domain cap.
        value = copy.deepcopy(DEFAULT)
        value['conductor']['domainLimit'] = 1
        self.store.pool.configure(value, 'Human reduced cap')
        other = next(dict(r) for r in self.store.connection().execute('SELECT * FROM requests WHERE parent_id=? AND conductor_id!=?', (root['id'], root['conductor_id'])))
        self.assertIsNotNone(self.store.begin_turn(other['conductor_id']))
        with self.store.tx() as c:
            self.store.enqueue(c, root['conductor_id'], root['id'], 'user-message', {'text': 'resume'}, 'test:resume')
        ctx = self.store.begin_turn(root['conductor_id'])
        self.assertIsNone(ctx)
        self.assertTrue(self.one('SELECT reason FROM instance_waits')['reason'].startswith('conductor-turns:'))
        self.assertEqual(self.one('SELECT chat_id FROM conductors WHERE id=?', root['conductor_id'])['chat_id'], 'pool')
