"""Domain performers on the queue ledger: employment, sessions, leases, skills, memory, effects and the resolver."""
import runtime_test_home  # noqa: F401
import json
import threading

from storage.errors import ContractError
from conductor import queue as cq
from conductor import domains
from conductor.runtime import ManagedExecutor
from test_conductor_queue import QueueTest, POLICY, Recorder

RESOLVER = {'model': 'resolver-model', 'provider': 'claude', 'reasoningEffort': 'high', 'fast': False}


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value


class PerformerTest(QueueTest):
    def setUp(self):
        self.clock = Clock()
        super().setUp()
        self.policy = {**POLICY, 'employment': True, 'sandbox': 'read-only'}
        self.store.ensure_chat('pool', self.policy)
        self.domain = self.area['domainId']
        # Generous pool limits: these tests exercise performer facts, not pool admission (tested elsewhere).
        roomy = {'domainLimit': 50, 'subdomainLimit': 50}
        self.store.pool.configure({'worker': roomy, 'conductor': roomy}, 'fixture Human setting')

    def poke(self, request_id):
        """A follow-up gives the request's conductor a mailbox item, so a test turn can start."""
        self.seq += 1
        self.store.ingest('pool', 'conversation', {'clientMessageId': 'poke%d' % self.seq, 'text': 'continue',
                                                   'requestId': request_id})
        self.pump()

    def open(self):
        store = cq.Store(self.db, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def command(self, store=None, **fields):
        self.seq += 1
        command_id = 'control-%d' % self.seq
        (store or self.store).ingest('pool', 'control', {'commandId': command_id, **fields})
        self.pump(store)
        return command_id

    def outcome(self, command_id):
        row = self.one("SELECT payload FROM events WHERE type='role-pool-control-result' AND payload LIKE ?", '%' + command_id + '"%')
        return json.loads(row['payload'])['outcome'] if row else None

    def rejected(self, command_id):
        input_id = self.one('SELECT input_id FROM inbox WHERE dedup_key=?', 'chat:pool:control:' + command_id)['input_id']
        return self.one('SELECT status, reason FROM inputs WHERE id=?', input_id)

    def delegation(self, title='part', area=None, assignment_key=None, **spec):
        self.seq += 1
        self.store.ingest('pool', 'conversation', {'clientMessageId': 'pm%d' % self.seq, 'text': title, 'relation': 'new',
                                                   'area': area or self.area})
        self.pump()
        req = self.one('SELECT * FROM requests ORDER BY rowid DESC LIMIT 1')
        command = {'type': 'delegate', 'requestId': req['id'], 'title': title, 'spec': spec}
        if assignment_key:
            command['assignmentKey'] = assignment_key
        self.turn(req['conductor_id'], [command])
        executor = self.pump()
        return self.one('SELECT * FROM delegations WHERE request_id=?', req['id']), executor

    def event(self, d, kind, event_id=None, attempt=None, cls='lifecycle', **data):
        return self.store.ingest('pool', cls, {'delegationId': d['id'], 'attemptId': attempt or self.attempt(d['id']),
                                               'workerEventId': event_id or kind, 'kind': kind, **data})

    def current(self, d):
        return self.one('SELECT * FROM delegations WHERE id=?', d['id'])

    def projection(self):
        return self.store.performers.projection(self.store.connection())

    def domain_row(self, domain_id=None):
        return next(d for d in self.projection()['domains'] if d['domainId'] == (domain_id or self.domain))

    def pump(self, store=None, executor=None):
        store = store or self.store
        executor = super().pump(store, executor)
        store.consume('memory', store.performers.memory_handler)
        return executor


class Employment(PerformerTest):
    def test_hire_needs_the_human_and_each_domain_has_exactly_one_active_performer(self):
        with self.store.tx() as c:
            with self.assertRaises(ContractError) as caught:
                self.store.performers.hire(c, self.domain, 'ai', 'conductor proposal')
        self.assertEqual(caught.exception.code, 'employment_requires_human')
        self.assertEqual(self.domain_row()['status'], 'unhired')
        self.assertIsNone(self.domain_row()['performer'])
        first = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        again = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        self.assertEqual((again['performerId'], again['reused']), (first['performerId'], True))
        row = self.domain_row()
        self.assertEqual((row['status'], row['performer']['id']), ('hired', first['performerId']))
        self.assertTrue(row['employment']['decisionRef'].startswith('human-control:'))

    def test_existing_human_role_hire_records_the_domain_employment_agreement(self):
        settings = {'model': 'work-model', 'provider': 'codex', 'sandbox': 'read-only', 'approvalPolicy': 'bypass'}
        command = self.command(action='hire-role', area=self.area, profile='work', settings=settings)
        outcome = self.outcome(command)
        self.assertIn('employment', outcome, self.rejected(command))
        self.assertEqual(self.domain_row()['performer']['id'], outcome['employment']['performerId'])

    def test_sessions_are_listed_inside_one_performer_and_never_counted_as_performers(self):
        self.command(action='hire-performer', domainId=self.domain)
        child = domains.subdomain(self.db.parent, self.domain, 'Second', 'human', 'fixture', write_scope=[])['subdomain']['id']
        first, _ = self.delegation('first')
        second, _ = self.delegation('second', area={'domainId': self.domain, 'subdomainId': child})
        row = self.domain_row()
        self.assertEqual(len([d for d in self.projection()['domains'] if d['domainId'] == self.domain]), 1)
        self.assertEqual(len(row['sessions']), 2)
        self.assertEqual({s['task']['delegationId'] for s in row['sessions']}, {first['id'], second['id']})
        self.assertIn(row['currentTask']['delegationId'], {first['id'], second['id']})
        self.assertEqual(row['currentTask']['waitReason'], 'worker-start')
        board = self.store.ui_board()
        self.assertEqual(board['performers']['domains'][0]['performer']['id'], row['performer']['id'])
        self.assertEqual(board['logicalConductor']['id'], 'logical-conductor')
        self.assertEqual(len(board['logicalConductor']['sessions']), 2 * board['logicalConductor']['instanceCount'])

    def test_replacement_keeps_history_and_lets_the_running_attempt_finish_under_its_employment(self):
        hired = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        running, _ = self.delegation('running work')
        self.event(running, 'started')
        self.pump()
        replaced = self.outcome(self.command(action='replace-performer', domainId=self.domain))
        self.assertEqual(replaced['cancelledDelegations'], [])
        self.assertEqual(self.current(running)['status'], 'running')
        later, _ = self.delegation('new work')
        bindings = {r['attempt_id']: r for r in self.store.connection().execute('SELECT * FROM attempt_bindings')}
        self.assertEqual(bindings[running['attempt_id']]['employment_id'], hired['employmentId'])
        self.assertEqual(bindings[later['attempt_id']]['employment_id'], replaced['employmentId'])
        self.event(running, 'completed', result={'summary': 'done'})
        self.pump()
        self.assertEqual(self.current(running)['status'], 'completed')
        history = self.domain_row()['history']
        self.assertEqual([h['status'] for h in history], ['replaced', 'active'])
        self.assertEqual(self.domain_row()['performer']['id'], replaced['performerId'])

    def test_required_employment_is_an_opt_in_setting_that_holds_unhired_work(self):
        self.command(action='configure-performer-policy', policy={'requireEmployment': True})
        d, executor = self.delegation('held')
        self.assertEqual(executor.started, [])
        self.assertEqual(self.one('SELECT reason FROM attempt_waits WHERE attempt_id=?', d['attempt_id'])['reason'], 'employment-required')
        self.command(action='hire-performer', domainId=self.domain)
        self.store.schedule_waiting()
        executor = self.pump()
        self.assertEqual(executor.started, [d['attempt_id']])

    def test_undecided_policies_default_to_safe_existing_behavior(self):
        settings = self.projection()['policy']
        self.assertEqual((settings['requireEmployment'], settings['replacement'], settings['skillRevocation'],
                          settings['memorySharing'], settings['memoryRetention'], settings['resolverAuto']),
                         (False, 'keep-running', 'block-new-attempts', 'domain-isolated', 'keep', 'off'))
        rejected = self.command(action='configure-performer-policy', policy={'resolverAuto': 'always'})
        self.assertEqual(self.rejected(rejected)['reason'], 'policy_invalid')


class Assignment(PerformerTest):
    def test_concurrent_conductor_sessions_serialize_assignment_without_conflict(self):
        child = domains.subdomain(self.db.parent, self.domain, 'Parallel', 'human', 'fixture', write_scope=[])['subdomain']['id']
        for index, area in enumerate((self.area, {'domainId': self.domain, 'subdomainId': child})):
            self.seq += 1
            self.store.ingest('pool', 'conversation', {'clientMessageId': 'c%d' % self.seq, 'text': 'r%d' % index,
                                                       'relation': 'new', 'area': area})
        self.pump()
        rows = [dict(r) for r in self.store.connection().execute('SELECT * FROM requests ORDER BY rowid')]
        contexts = [self.store.begin_turn(r['conductor_id']) for r in rows]
        self.assertEqual(len({c['conductorId'] for c in contexts}), 2)
        barrier, errors = threading.Barrier(2), []

        def finish(context, request):
            store = self.open()
            try:
                token = {k: context[k] for k in ('conductorId', 'slot', 'epoch', 'turnId')}
                barrier.wait()
                store.end_turn(token, [{'type': 'delegate', 'requestId': request['id'], 'title': 'task',
                                        'spec': {'writeScope': [], 'workspace': {'mode': 'read-only'}}, 'assignmentKey': 'k'}])
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)
            finally:
                store.close()
        threads = [threading.Thread(target=finish, args=pair) for pair in zip(contexts, rows)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        log = [dict(r) for r in self.store.connection().execute('SELECT * FROM assignment_log ORDER BY seq')]
        self.assertEqual(len(log), 2)
        self.assertEqual(log[1]['seq'], log[0]['seq'] + 1)
        self.assertEqual({entry['conductor_id'] for entry in log}, {r['conductor_id'] for r in rows})
        self.assertTrue(all(entry['turn_id'] and entry['slot'] and entry['epoch'] is not None for entry in log))

    def test_the_same_assignment_key_cannot_be_owned_twice_in_one_request(self):
        d, _ = self.delegation('first', assignment_key='doc')
        self.poke(d['request_id'])
        context, result = self.turn(d['conductor_id'], [{'type': 'delegate', 'requestId': d['request_id'], 'title': 'dup',
                                                         'assignmentKey': 'doc', 'spec': {}}])
        self.assertEqual(result['applied'][0]['rejected'], 'assignment_conflict')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM delegations WHERE request_id=?', d['request_id'])['n'], 1)


class LeasesAndRecovery(PerformerTest):
    def lease(self, d):
        return self.one('SELECT * FROM attempt_bindings WHERE attempt_id=?', d['attempt_id'])

    def test_claim_pins_a_lease_that_worker_events_renew(self):
        d, _ = self.delegation()
        binding = self.lease(d)
        self.assertTrue(binding['claim_token'] and binding['manifest_id'] and binding['snapshot_id'])
        self.assertEqual(binding['lease_until'], self.clock.value + 900)
        self.clock.value += 600
        self.event(d, 'progress', 'p1', cls='progress', step='s')
        self.pump()
        self.assertEqual(self.lease(d)['lease_until'], self.clock.value + 900)
        self.assertTrue(self.store.performers.heartbeat(d['attempt_id'], binding['claim_token']))
        self.assertFalse(self.store.performers.heartbeat(d['attempt_id'], 'claim-other'))

    def test_lease_expiry_fences_the_silent_attempt_and_its_late_result(self):
        d, _ = self.delegation()
        first = d['attempt_id']
        self.event(d, 'started')
        self.pump()
        self.clock.value += 901
        self.assertEqual(self.store.performers.expire_leases(), 1)
        executor = self.pump()
        d = self.current(d)
        self.assertNotEqual(d['attempt_id'], first)
        self.assertEqual(self.one('SELECT status FROM attempts WHERE id=?', first)['status'], 'lost')
        self.assertIn(d['attempt_id'], executor.started)
        epochs = [r['owner_epoch'] for r in self.store.connection().execute(
            'SELECT owner_epoch FROM attempt_bindings WHERE delegation_id=? ORDER BY rowid', (d['id'],))]
        self.assertEqual(epochs, [1, 2])
        self.event(d, 'completed', 'late', attempt=first, result={'summary': 'stale'})
        self.event(d, 'completed', result={'summary': 'fresh'})
        self.pump()
        d = self.current(d)
        self.assertEqual(json.loads(d['result'])['summary'], 'fresh')
        self.assertEqual(self.one('SELECT result_attempt_id FROM task_owners WHERE delegation_id=?', d['id'])['result_attempt_id'], d['attempt_id'])
        self.assertEqual(self.one("SELECT COUNT(*) n FROM events WHERE type='attempt-late-event'")['n'], 1)

    def test_lease_expiry_without_retries_dead_letters_and_rejects_the_lost_result(self):
        d, _ = self.delegation()
        with self.store.tx() as c:
            c.execute('UPDATE delegations SET max_attempts=1 WHERE id=?', (d['id'],))
        self.event(d, 'started')
        self.pump()
        self.clock.value += 901
        self.store.performers.expire_leases()
        self.pump()
        self.assertEqual(self.current(d)['status'], 'failed')
        self.assertEqual(self.store.quarantined()['deadLetters'][0]['reason'], 'lease-expired')
        self.event(d, 'completed', 'late', result={'summary': 'too late'})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'failed')
        late = json.loads(self.one("SELECT payload FROM events WHERE type='attempt-late-event'")['payload'])
        self.assertEqual(late['reason'], 'lease-lost')

    def test_stale_owner_epoch_in_a_result_is_recorded_but_cannot_complete_the_task(self):
        d, _ = self.delegation()
        self.event(d, 'completed', 'old-epoch', ownerEpoch=0, result={'summary': 'old'})
        self.pump()
        self.assertNotEqual(self.current(d)['status'], 'completed')
        self.event(d, 'completed', 'good', ownerEpoch=1, result={'summary': 'ok'})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'completed')

    def test_retry_backoff_is_a_configured_delay_before_the_new_attempt_starts(self):
        self.command(action='configure-performer-policy', policy={'retryBackoffSeconds': 30})
        d, _ = self.delegation()
        self.event(d, 'failed', 'boom', error={'code': 'transient', 'retryable': True})
        executor = self.pump()
        d = self.current(d)
        self.assertNotIn(d['attempt_id'], executor.started)
        self.assertEqual(self.one('SELECT reason FROM attempt_waits WHERE attempt_id=?', d['attempt_id'])['reason'], 'backoff')
        self.clock.value += 31
        self.store.schedule_waiting()
        executor = self.pump()
        self.assertIn(d['attempt_id'], executor.started)

    def test_exhausted_retries_fail_visibly_with_a_dead_letter(self):
        d, _ = self.delegation()
        self.event(d, 'failed', 'one', error={'code': 'transient', 'retryable': True})
        self.pump()
        d = self.current(d)
        self.event(d, 'failed', 'two', error={'code': 'transient', 'retryable': True})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'failed')
        self.assertEqual([x['reason'] for x in self.store.quarantined()['deadLetters']], ['retries-exhausted'])

    def test_external_effect_intent_survives_a_restart_as_unknown_and_is_never_repeated(self):
        d, _ = self.delegation()
        self.event(d, 'started')
        self.pump()
        intent = self.store.performers.effect_intent(d['attempt_id'], 'github:pr/1', 'comment-1')
        self.assertEqual((intent['status'], intent['proceed']), ('intent', True))
        self.store.close()
        self.store = self.open()  # Crash between the external call and its recorded outcome.
        report = self.store.recover()
        self.assertEqual(report['unknownEffects'], [intent['effectKey']])
        again = self.store.performers.effect_intent(d['attempt_id'], 'github:pr/1', 'comment-1')
        self.assertEqual((again['status'], again['proceed']), ('outcome-unknown', False))
        self.store.performers.effect_outcome(intent['effectKey'], 'succeeded', {'checkedBy': 'idempotency key lookup'})
        self.assertEqual(self.store.performers.effect_outcome(intent['effectKey'], 'failed')['status'], 'succeeded')
        self.assertFalse(self.store.performers.effect_intent(d['attempt_id'], 'github:pr/1', 'comment-1')['proceed'])

    def test_cancelled_or_superseded_attempts_cannot_start_new_effects(self):
        d, _ = self.delegation()
        self.event(d, 'started')
        self.pump()
        with self.store.tx() as c:
            c.execute('UPDATE delegations SET cancel_requested=1 WHERE id=?', (d['id'],))
        with self.assertRaises(ContractError) as caught:
            self.store.performers.effect_intent(d['attempt_id'], 'fs:write', 'k')
        self.assertEqual(caught.exception.code, 'effect_fenced')

    def test_legacy_ledgers_reopen_without_rewriting_and_gain_ownership_lazily(self):
        d, _ = self.delegation()
        with self.store.tx() as c:  # A ledger written before performer records existed.
            for table in ('attempt_bindings', 'task_owners', 'assignment_log', 'execution_manifests', 'memory_snapshots', 'exec_sessions'):
                c.execute('DELETE FROM ' + table)
        before = self.one('SELECT * FROM delegations WHERE id=?', d['id'])
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.one('SELECT * FROM delegations WHERE id=?', d['id']), before)
        self.assertEqual(self.one("SELECT value FROM meta WHERE key='relay:memory'")['value'], '0')
        self.event(d, 'completed', result={'summary': 'legacy done'})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'completed')
        self.assertEqual(self.domain_row()['recentResult']['ownerEpoch'], 1)


class Skills(PerformerTest):
    def register(self, skill_id, layer, scope, digest, approve=True):
        result = self.outcome(self.command(action='register-skill', skillId=skill_id, layer=layer, scopeId=scope,
                                           sourceRef='docs/skills/%s/SKILL.md' % skill_id, contentHash=digest))
        if approve:
            self.command(action='approve-skill', skillRevisionId=result['skillRevisionId'])
        return result['skillRevisionId']

    def manifest(self, d):
        binding = self.one('SELECT * FROM attempt_bindings WHERE attempt_id=?', d['attempt_id'])
        return [s['skillId'] for s in json.loads(self.one('SELECT skills FROM execution_manifests WHERE id=?', binding['manifest_id'])['skills'])]

    def test_attempts_pin_approved_revisions_by_layer_and_revocation_blocks_only_new_attempts(self):
        hired = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        other = domains.create(self.db.parent, 'Other area', 'human', 'fixture')['domainId']
        self.register('safety', 'platform', None, 'a' * 40)
        domain_skill = self.register('queue-design', 'domain', self.domain, 'b' * 40)
        self.register('own-steps', 'performer', hired['performerId'], 'c' * 40)
        self.register('foreign', 'domain', other, 'd' * 40)
        self.register('draft', 'domain', self.domain, 'e' * 40, approve=False)
        d, _ = self.delegation()
        self.assertEqual(self.manifest(d), ['safety', 'queue-design', 'own-steps'])
        authority = json.loads(self.one('SELECT authority FROM execution_manifests').__getitem__('authority'))
        self.assertFalse(authority['skillsGrantAuthority'])
        self.command(action='revoke-skill', skillRevisionId=domain_skill, reason='superseded procedure')
        self.assertEqual(self.manifest(d), ['safety', 'queue-design', 'own-steps'])  # The running attempt keeps its pin.
        self.assertNotEqual(self.current(d)['status'], 'cancelled')
        later, _ = self.delegation('later')
        self.assertEqual(self.manifest(later), ['safety', 'own-steps'])

    def test_only_the_human_publishes_skills_and_employment_scope_limits_them(self):
        candidate = self.outcome(self.command(action='register-skill', skillId='x', layer='domain', scopeId=self.domain,
                                              sourceRef='docs/x.md', contentHash='f' * 40))
        with self.store.tx() as c:
            with self.assertRaises(ContractError) as caught:
                self.store.performers.approve_skill(c, candidate['skillRevisionId'], 'ai', 'auto')
        self.assertEqual(caught.exception.code, 'skill_publication_requires_human')
        self.command(action='approve-skill', skillRevisionId=candidate['skillRevisionId'])
        self.register('y', 'domain', self.domain, '1' * 40)
        self.command(action='hire-performer', domainId=self.domain, skillScope={'skills': ['y']})
        d, _ = self.delegation()
        self.assertEqual(self.manifest(d), ['y'])

    def test_procedural_memory_becomes_a_skill_candidate_only(self):
        hired = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        with self.store.tx() as c:
            entry = self.store.performers.write_memory(c, 'pool', {
                'kind': 'procedural', 'scopeKind': 'performer', 'scopeId': hired['performerId'], 'domainId': self.domain,
                'text': 'Run the queue suite before the browser check', 'sourceRef': 'attempt:att-x', 'writer': 'session-1'},
                {'kind': 'session', 'sessionId': 's1', 'performerId': hired['performerId'], 'domainId': self.domain})
        self.pump()
        self.assertEqual(self.one('SELECT status FROM memory_entries WHERE id=?', entry)['status'], 'candidate')
        proposed = self.outcome(self.command(action='propose-skill', entryId=entry, skillId='queue-check'))
        self.assertEqual(proposed['status'], 'candidate')
        d, _ = self.delegation()
        self.assertEqual(self.manifest(d), [])


class Memory(PerformerTest):
    def write(self, entry, writer):
        with self.store.tx() as c:
            entry_id = self.store.performers.write_memory(c, 'pool', entry, writer)
        self.pump()
        return entry_id

    def test_ownership_scopes_govern_reads_and_writes(self):
        other = domains.create(self.db.parent, 'Other area', 'human', 'fixture')['domainId']
        session = {'kind': 'session', 'sessionId': 's1', 'performerId': 'p1', 'domainId': self.domain}
        working = self.write({'kind': 'working', 'scopeKind': 'session', 'scopeId': 's1', 'text': 'scratch plan',
                              'sourceRef': 'turn:1', 'writer': 's1'}, session)
        with self.store.tx() as c, self.assertRaises(ContractError) as caught:
            self.store.performers.write_memory(c, 'pool', {'kind': 'working', 'scopeKind': 'session', 'scopeId': 's2',
                                                           'text': 'x', 'sourceRef': 'turn:2', 'writer': 's1'}, session)
        self.assertEqual(caught.exception.code, 'memory_scope_denied')
        with self.store.tx() as c, self.assertRaises(ContractError) as caught:
            self.store.performers.write_memory(c, 'pool', {'kind': 'semantic', 'scopeKind': 'domain', 'scopeId': other,
                                                           'domainId': other, 'text': 'x', 'sourceRef': 'r', 'writer': 's1'}, session)
        self.assertEqual(caught.exception.code, 'memory_scope_denied')
        with self.store.tx() as c, self.assertRaises(ContractError) as caught:
            self.store.performers.write_memory(c, 'pool', {'kind': 'episodic', 'scopeKind': 'task', 'scopeId': 'd1',
                                                           'domainId': self.domain, 'text': 'no source', 'writer': 's1'}, session)
        self.assertEqual(caught.exception.code, 'memory_provenance_required')
        fact = self.write({'kind': 'semantic', 'scopeKind': 'domain', 'scopeId': other, 'domainId': other,
                           'text': 'other domain fact', 'sourceRef': 'decision:1', 'writer': 'human'}, {'kind': 'human'})
        own = {'kind': 'performer', 'performerId': 'p1', 'domainId': self.domain}
        self.assertEqual([e['id'] for e in self.store.performers.search(session, 'scratch')], [working])
        self.assertEqual(self.store.performers.search(own, 'scratch'), [])
        self.assertEqual(self.store.performers.search(own, 'domain fact'), [])
        self.command(action='configure-performer-policy', policy={'memorySharing': 'shared-semantic'})
        self.assertEqual([e['id'] for e in self.store.performers.search(own, 'domain fact')], [fact])
        self.assertEqual(len(self.store.performers.search({'kind': 'conductor'}, 'fact')), 1)

    def test_shared_knowledge_from_performers_needs_human_approval(self):
        writer = {'kind': 'performer', 'performerId': 'p1', 'domainId': self.domain}
        entry = self.write({'kind': 'semantic', 'scopeKind': 'domain', 'scopeId': self.domain, 'domainId': self.domain,
                            'text': 'Queue ack follows the ledger commit', 'sourceRef': 'attempt:a1', 'writer': 'p1',
                            'confidence': 0.7}, writer)
        self.assertEqual(self.one('SELECT status FROM memory_entries WHERE id=?', entry)['status'], 'candidate')
        self.command(action='approve-memory', entryId=entry)
        self.assertEqual(self.one('SELECT status FROM memory_entries WHERE id=?', entry)['status'], 'active')

    def test_corrections_supersede_contradictions_are_marked_and_the_index_rebuilds(self):
        human = {'kind': 'human'}
        base = {'kind': 'semantic', 'scopeKind': 'domain', 'scopeId': self.domain, 'domainId': self.domain, 'writer': 'human'}
        first = self.write({**base, 'text': 'Lease is five minutes', 'sourceRef': 'decision:1'}, human)
        other = self.write({**base, 'text': 'Lease is fifteen minutes', 'sourceRef': 'decision:2'}, human)
        with self.store.tx() as c:
            corrected = self.store.performers.correct_memory(c, 'pool', first, 'Lease is fifteen minutes by default',
                                                             human, 'decision:3')
        self.pump()
        with self.store.tx() as c:
            self.store.performers.contradict_memory(c, other, corrected, 'review:1')
        rows = {r['id']: dict(r) for r in self.store.connection().execute('SELECT * FROM memory_entries')}
        self.assertEqual(rows[first]['status'], 'superseded')
        self.assertEqual(rows[first]['text'], 'Lease is five minutes')  # The original stays recorded.
        self.assertEqual(rows[corrected]['supersedes'], first)
        self.assertTrue(rows[other]['contradiction'].startswith('contested-with:'))
        before = sorted(map(tuple, self.store.connection().execute('SELECT term, entry_id FROM memory_index')))
        with self.store.tx() as c:
            c.execute('DELETE FROM memory_index')
        self.assertEqual(self.store.performers.reindex(), 3)
        after = sorted(map(tuple, self.store.connection().execute('SELECT term, entry_id FROM memory_index')))
        self.assertEqual(before, after)

    def test_results_become_episodic_memory_through_the_outbox_and_redelivery_is_harmless(self):
        d, _ = self.delegation()
        self.event(d, 'completed', result={'summary': 'built the ledger'})
        QueueTest.pump(self)  # Ledger and UI only: the memory intent is committed but not yet applied.
        self.assertEqual(self.one('SELECT COUNT(*) n FROM memory_entries')['n'], 0)
        self.store.consume('memory', self.store.performers.memory_handler)
        entry = self.one('SELECT * FROM memory_entries')
        self.assertEqual((entry['kind'], entry['scope_id'], entry['source_ref']), ('episodic', d['id'], 'attempt:' + d['attempt_id']))
        seq = self.one("SELECT seq FROM events WHERE type='memory-update-intent'")['seq']
        with self.store.tx() as c:
            c.execute("UPDATE deliveries SET status='ready' WHERE subscription='memory' AND event_seq=?", (seq,))
            c.execute("DELETE FROM consumer_inbox WHERE subscription='memory'")
        self.store.consume('memory', self.store.performers.memory_handler)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM memory_entries')['n'], 1)

    def test_attempt_snapshots_pin_readable_memory_and_restore_flags_later_corrections(self):
        hired = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        human = {'kind': 'human'}
        entry = self.write({'kind': 'semantic', 'scopeKind': 'domain', 'scopeId': self.domain, 'domainId': self.domain,
                            'text': 'Use the stable effect key', 'sourceRef': 'decision:1', 'writer': 'human'}, human)
        self.write({'kind': 'working', 'scopeKind': 'session', 'scopeId': 'someone-else', 'text': 'private',
                    'sourceRef': 'turn:1', 'writer': 'x'}, {'kind': 'session', 'sessionId': 'someone-else'})
        d, _ = self.delegation()
        binding = self.one('SELECT * FROM attempt_bindings WHERE attempt_id=?', d['attempt_id'])
        self.assertEqual(binding['performer_id'], hired['performerId'])
        with self.store.tx() as c:
            self.store.performers.correct_memory(c, 'pool', entry, 'Use the stable effect key per task revision', human, 'decision:2')
        self.pump()
        restored = self.store.performers.restore(self.store.connection(), binding['snapshot_id'])
        self.assertEqual([e['id'] for e in restored['entries']], [entry])
        self.assertEqual(restored['entries'][0]['text'], 'Use the stable effect key')
        self.assertTrue(restored['entries'][0]['supersededBy'])


class Resolver(PerformerTest):
    def failed(self):
        d, _ = self.delegation('hard task', goal='fix the flaky lease', completionCriteria='tests pass')
        with self.store.tx() as c:
            c.execute('UPDATE delegations SET max_attempts=1 WHERE id=?', (d['id'],))
        self.event(d, 'started')
        self.event(d, 'failed', error={'code': 'stuck', 'message': 'could not reproduce', 'retryable': True})
        self.pump()
        return self.current(d)

    def configure(self, **settings):
        return self.command(action='configure-resolver', areaId=self.domain,
                            settings={**RESOLVER, 'sandbox': 'read-only', 'approvalPolicy': 'bypass', **settings})

    def test_unselected_resolver_is_reported_and_engagement_is_rejected(self):
        d = self.failed()
        self.assertEqual(self.domain_row()['resolver']['state'], 'selection-required')
        command = self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=d['version'], reason='stuck')
        self.assertEqual(self.rejected(command)['reason'], 'resolver_selection_required')
        self.assertEqual(self.current(d)['status'], 'failed')

    def test_resolver_cannot_change_the_captured_authority(self):
        self.configure(sandbox='danger-full-access')
        d = self.failed()
        command = self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=d['version'], reason='stuck')
        self.assertEqual(self.rejected(command)['reason'], 'resolver_selection_required')

    def test_human_engages_the_resolver_inside_the_same_performer_with_a_full_handoff(self):
        hired = self.outcome(self.command(action='hire-performer', domainId=self.domain))
        self.configure()
        self.assertEqual(self.domain_row()['resolver']['state'], 'configured')
        d = self.failed()
        self.assertEqual(self.one('SELECT status FROM requests WHERE id=?', d['request_id'])['status'], 'awaiting-synthesis')
        command = self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=d['version'], reason='expert attempts stalled')
        outcome = self.outcome(command)
        self.assertIsNotNone(outcome, self.rejected(command))
        d2 = self.current(d)
        self.assertEqual((d2['status'], d2['attempt_id'], json.loads(d2['spec'])['writeScope']),
                         ('dispatched', outcome['attemptId'], json.loads(d['spec'])['writeScope']))
        self.assertEqual(self.one('SELECT status FROM requests WHERE id=?', d['request_id'])['status'], 'delegated')
        ctx = self.store.attempt_context(outcome['attemptId'])
        self.assertEqual((ctx['tier'], ctx['binding']['performerId'], ctx['binding']['ownerEpoch']), ('resolver', hired['performerId'], 2))
        bundle = ctx['resolver']['bundle']
        self.assertEqual([a['status'] for a in bundle['previousAttempts']], ['failed'])
        self.assertEqual(bundle['failureEvidence']['code'], 'stuck')
        self.assertTrue(bundle['authority']['unchanged'])
        self.assertEqual(bundle['scope']['goal'], 'fix the flaky lease')
        row = self.domain_row()
        self.assertEqual(len([s for s in row['sessions'] if s['tier'] == 'resolver']), 1)
        self.assertEqual(row['performer']['id'], hired['performerId'])
        executor = ManagedExecutor.__new__(ManagedExecutor)
        executor.store = self.store
        request = executor.request({**ctx, 'policy': {**ctx['policy'], 'sandbox': 'read-only', 'work': {'model': 'expert-model'}}})
        self.assertEqual(request['session']['settings'], RESOLVER)
        self.assertTrue(request['session']['agentId'].startswith('resolve-cq-'))
        self.assertEqual(request['session']['handoff']['kind'], 'resolver-handoff')
        self.assertEqual(request['session']['area']['domainId'], self.domain)
        decision = [x for x in self.store.pool.decisions(self.store.connection(), d['request_id']) if x['kind'] == 'resolver']
        self.assertEqual(decision[0]['value']['settings']['model'], 'resolver-model')
        self.event(d2, 'completed', 'resolved', result={'summary': 'resolved'})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'completed')
        self.assertEqual(self.domain_row()['recentResult']['tier'], 'resolver')

    def test_the_expert_attempt_context_carries_its_pinned_manifest_and_snapshot(self):
        d, _ = self.delegation()
        ctx = self.store.attempt_context(d['attempt_id'])
        executor = ManagedExecutor.__new__(ManagedExecutor)
        executor.store = self.store
        request = executor.request({**ctx, 'policy': {**ctx['policy'], 'sandbox': 'read-only', 'work': {'model': 'expert-model'}}})
        self.assertEqual(request['session']['handoff']['kind'], 'performer-context')
        self.assertTrue(request['session']['handoff']['performer']['manifest']['id'].startswith('manifest-'))

    def test_running_attempts_must_end_before_the_resolver_takes_over(self):
        self.configure()
        d, _ = self.delegation()
        self.event(d, 'started')
        self.pump()
        command = self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=self.current(d)['version'], reason='slow')
        self.assertEqual(self.rejected(command)['reason'], 'resolver_attempt_active')

    def test_automatic_engagement_is_off_until_the_human_enables_it(self):
        self.configure()
        d = self.failed()
        self.assertEqual(d['status'], 'failed')
        self.command(action='configure-performer-policy', policy={'resolverAuto': 'after-retries-exhausted'})
        d, _ = self.delegation('second hard task')
        with self.store.tx() as c:
            c.execute('UPDATE delegations SET max_attempts=1 WHERE id=?', (d['id'],))
        self.event(d, 'failed', error={'code': 'stuck', 'retryable': True})
        executor = self.pump()
        d = self.current(d)
        self.assertEqual(d['status'], 'dispatched')
        self.assertIn(d['attempt_id'], executor.started)
        handoff = self.one('SELECT * FROM resolver_handoffs WHERE delegation_id=?', d['id'])
        self.assertEqual((handoff['actor'], handoff['source']), ('policy', 'performer-policy:resolverAuto'))
        self.event(d, 'failed', 'resolver-failed', error={'code': 'stuck', 'retryable': True})
        self.pump()
        self.assertEqual(self.current(d)['status'], 'failed')  # One automatic engagement only; no loop.

    def test_conductor_escalation_needs_the_policy_and_respects_required_approval(self):
        self.configure()
        d = self.failed()
        req = self.one('SELECT * FROM requests WHERE id=?', d['request_id'])
        _, result = self.turn(req['conductor_id'], [{'type': 'escalate', 'requestId': req['id'], 'delegationId': d['id'], 'reason': 'x'}])
        self.assertEqual(result['applied'][0]['rejected'], 'resolver_auto_disabled')
        self.command(action='configure-performer-policy', policy={'resolverAuto': 'conductor-may-engage'})
        self.poke(req['id'])
        _, result = self.turn(req['conductor_id'], [{'type': 'escalate', 'requestId': req['id'], 'delegationId': d['id'], 'reason': 'x'}])
        self.assertEqual(result['applied'][0]['type'], 'escalate')
        self.assertEqual(self.current(d)['status'], 'dispatched')

    def test_resolver_engagement_never_reopens_a_cancelled_request(self):
        self.configure()
        d = self.failed()
        req = self.one('SELECT * FROM requests WHERE id=?', d['request_id'])
        with self.store.tx() as c:
            c.execute("UPDATE requests SET status='cancelled' WHERE id=?", (req['id'],))
        command = self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=d['version'], reason='late')
        self.assertEqual(self.rejected(command)['reason'], 'resolver_target_closed')


class ChatResolver(PerformerTest):
    def test_chat_resolver_is_used_when_no_area_setting_and_never_widens_authority(self):
        d, _ = self.delegation()
        with self.store.tx() as c:
            c.execute('UPDATE delegations SET max_attempts=1 WHERE id=?', (d['id'],))
        self.event(d, 'failed', error={'code': 'stuck', 'retryable': True})
        self.pump()
        self.assertEqual(self.domain_row()['resolver']['state'], 'selection-required')
        wide = self.command(action='configure-chat-resolver', settings={**RESOLVER, 'sandbox': 'danger-full-access', 'approvalPolicy': 'bypass'})
        self.assertEqual(self.rejected(wide)['reason'], 'role_permission_invalid')
        chat = {'model': 'claude-fable-5-1', 'provider': 'claude', 'reasoningEffort': 'high', 'sandbox': 'read-only', 'approvalPolicy': 'bypass'}
        command = self.command(action='configure-chat-resolver', settings=chat)
        first = self.outcome(command)
        self.assertIsNotNone(first, self.rejected(command))
        again = self.outcome(self.command(action='configure-chat-resolver', settings=chat))
        self.assertEqual((first['changed'], again['changed']), (True, False))
        state = self.domain_row()['resolver']
        self.assertEqual((state['state'], state['inheritedFrom'], state['settings']['model']), ('configured', 'chat', 'claude-fable-5-1'))
        d = self.current(d)
        outcome = self.outcome(self.command(action='escalate-resolver', delegationId=d['id'], expectedVersion=d['version'], reason='stuck'))
        ctx = self.store.attempt_context(outcome['attemptId'])
        self.assertEqual(ctx['resolver']['settings']['model'], 'claude-fable-5-1')
        # An explicit area setting still wins over the chat row.
        self.command(action='configure-resolver', areaId=self.domain, settings={**RESOLVER, 'sandbox': 'read-only', 'approvalPolicy': 'bypass'})
        self.assertEqual(self.domain_row()['resolver']['settings']['model'], 'resolver-model')
