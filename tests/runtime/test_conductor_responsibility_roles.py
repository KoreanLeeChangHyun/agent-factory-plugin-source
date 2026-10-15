"""Own checks: real queue/managed bridge, isolated data and fake providers only."""
import runtime_test_home  # noqa: F401
import json
import types
from unittest import mock

from storage.errors import ContractError
from conductor import domains
from conductor.backend import ManagedBackend
from conductor.runtime import FakeConductor, ManagedExecutor, Runtime
from test_conductor_queue import QueueTest, POLICY


class ResponsibilityAndRoles(QueueTest):
    def send(self, text='bounded work', area=None, **extra):
        self.seq += 1
        value = {'clientMessageId': 'pool-%s' % self.seq, 'text': text, 'relation': 'new', **extra}
        if area:
            value['area'] = area
        result = self.store.ingest('pool', 'conversation', value)
        self.pump()
        return result

    def finish(self, delegation, kind='completed', **extra):
        self.store.ingest(delegation['chat_id'], 'lifecycle', {'delegationId': delegation['id'], 'attemptId': delegation['attempt_id'], 'workerEventId': kind, 'kind': kind, **extra})
        self.pump()

    def command(self, **fields):
        self.seq += 1
        self.store.ingest('pool', 'control', {'commandId': 'control-%d' % self.seq, **fields})
        self.pump()

    def setUp(self):
        super().setUp()
        self.policy = {**POLICY, 'employment': True, 'conductorSettings': {'model': 'stub', 'provider': 'codex'}}
        self.store.ensure_chat('pool', self.policy)
        self.area_b = {'domainId': self.area['domainId'], 'subdomainId': domains.subdomain(self.db.parent, self.area['domainId'], 'Browser', 'human', 'fixture', write_scope=[])['subdomain']['id']}
        self.settings = {'model': 'stub', 'provider': 'codex', 'reasoningEffort': 'medium', 'fast': False,
                         'sandbox': 'workspace-write', 'approvalPolicy': 'bypass'}
        self.verified = {**self.settings, 'sandbox': 'read-only'}
        self.native_policy = {**self.policy, 'sandbox': 'workspace-write', 'work': {k: v for k, v in self.settings.items() if k not in ('sandbox', 'approvalPolicy')}}
        self.store.ensure_chat('roles', self.native_policy)

    def roles_request(self, text='bounded', **extra):
        self.seq += 1
        self.store.ingest('roles', 'conversation', {'clientMessageId': str(self.seq), 'text': text, 'relation': 'new', 'area': self.area, **extra})
        self.pump()
        return self.one("SELECT * FROM requests WHERE chat_id='roles' ORDER BY rowid DESC LIMIT 1")

    def manifest(self):
        spec = {'goal': 'output coordination', 'completionCriteria': 'bounded result', 'writeScope': [], 'workspace': {'mode': 'read-only'}, 'durationMs': 10}
        return [{'key': 'output', 'title': 'Output coordination', 'spec': spec},
                {'key': 'docs', 'title': 'Architecture document synchronization', 'spec': {**spec, 'goal': 'synchronize architecture documents'}, 'prerequisites': ['output']}]

    def test_required_followup_is_durable_and_runs_after_exact_receipt_without_human_message(self):
        self.send('approved output plus docs', self.area, requiredTasks=self.manifest())
        req = self.one('SELECT * FROM requests')
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0))
        self.assertTrue(runtime.drain(5))
        tasks = self.store.responsibility.view(self.store.connection(), req['id'])['tasks']
        self.assertEqual([t['state'] for t in tasks], ['completed', 'completed'])
        self.assertEqual(tasks[1]['prerequisites'], [tasks[0]['id']])
        self.assertTrue(all(t['completionEvidence']['receiptPath'] for t in tasks))
        self.assertEqual(self.one('SELECT status FROM requests')['status'], 'completed')
        delegations = [dict(d) for d in self.store.connection().execute('SELECT * FROM delegations ORDER BY created_at')]
        self.assertLessEqual(delegations[0]['updated_at'], delegations[1]['created_at'])
        self.assertEqual(len(delegations), 2)

    def test_missing_receipt_late_instruction_and_scope_manifest_block_completion(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        _, registered = self.turn(req['conductor_id'], [{'type': 'register-required', 'requestId': req['id'], 'tasks': self.manifest()}])
        tasks = registered['applied'][0]['tasks']
        self.store.responsibility.reconcile(self.store.connection())
        self.turn(req['conductor_id'], [{'type': 'delegate', 'requestId': req['id'], 'obligationId': tasks[0]['id'], 'title': 'Output coordination', 'spec': self.manifest()[0]['spec']}])
        self.pump()
        d = self.one('SELECT * FROM delegations')
        self.finish(d, result={'resultPath': 'work-result'})
        self.store.responsibility.reconcile(self.store.connection())
        self.assertEqual(self.one('SELECT state FROM obligations ORDER BY rowid')['state'], 'receipt-missing')
        _, result = self.turn(req['conductor_id'], [{'type': 'reply', 'requestId': req['id'], 'text': 'done', 'final': True}])
        self.assertEqual(result['applied'][0]['rejected'], 'responsibility_open')
        self.send('late approved followup', requestId=req['id'])
        self.assertTrue(self.store.responsibility.view(self.store.connection(), req['id'])['pendingInstructions'])

    def test_registration_idempotence_cycle_rejection_restart_and_unassigned_detection(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        with self.store.tx() as c:
            a = self.store.responsibility.register(c, req['id'], self.manifest(), 'Human approval')
            b = self.store.responsibility.register(c, req['id'], self.manifest(), 'Human approval')
        self.assertEqual(a['tasks'], b['tasks'])
        cycle = self.manifest(); cycle[0]['prerequisites'] = ['docs']
        with self.assertRaisesRegex(ContractError, 'cycle'):
            with self.store.tx() as c:
                self.store.responsibility.register(c, req['id'], cycle, 'Human approval')
        self.store.responsibility.reconcile(self.store.connection())
        self.turn(req['conductor_id'], [])  # A consumed model turn omitted required assignment.
        with self.store.tx() as c:
            self.store.responsibility.reconcile(c)
        self.assertEqual(self.one('SELECT reason FROM obligations ORDER BY rowid')['reason'], 'execution-unassigned')
        from conductor.queue import Store
        self.store.close(); self.store = Store(self.db)
        runtime = Runtime(self.store, conductor=FakeConductor(delay=0, chunk_delay=0))
        self.assertTrue(runtime.drain(5))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM delegations')['n'], 2)

    def test_policy_inheritance_is_captured_only_on_new_requests(self):
        old = self.roles_request()
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['domainId'], 'Human policy', route='work-verification')
        new = self.roles_request('new verified work')
        a = json.loads(self.one('SELECT source FROM request_processing WHERE request_id=?', old['id'])['source'])
        b = json.loads(self.one('SELECT source FROM request_processing WHERE request_id=?', new['id'])['source'])
        self.assertEqual(a['route'], 'work')
        self.assertEqual((b['route'], b['inheritedFrom']), ('work-verification', self.area['domainId']))
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['subdomainId'], 'Human child choice', route='work')
        self.assertEqual(self.store.roles.processing(self.store.connection(), self.area)['route'], 'work')
        self.assertEqual(self.one('SELECT route FROM request_processing WHERE request_id=?', new['id'])['route'], 'work-verification')
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['subdomainId'], 'Human new model', profile='work', settings={**self.settings,'model':'new-selected-model'})
        frozen = self.store.roles.freeze(self.store.connection(),old,{'writeScope':[]},self.area,self.native_policy)
        self.assertEqual(frozen['executionSettings']['model'],'stub')

    def test_role_hire_reuses_idle_and_preserves_role_and_human_membership(self):
        with self.store.tx() as c:
            worker = self.store.roles.hire(c, self.area, 'workLight', self.settings, 'Human role selection')
            expert = self.store.roles.hire(c, self.area, 'work', self.settings, 'Human role selection')
            verifier = self.store.roles.hire(c, self.area, 'verification', self.verified, 'Human role selection')
            repeated = self.store.roles.hire(c, self.area, 'verification', self.verified, 'Human repeat')
        self.assertEqual(verifier['agentId'], repeated['agentId'])
        self.assertEqual(len({worker['agentId'],expert['agentId'],verifier['agentId']}), 3)
        area = next(a for a in self.store.roles.projection(self.store.connection())['areas'] if a['id']==self.area['subdomainId'])
        self.assertEqual([area['counts'][p]['hired'] for p in ('workLight','work','verification')], [1,1,1])
        placement = domains.read(self.db.parent)
        self.assertEqual(placement['assignments'][verifier['agentId']]['setBy']['actor'], 'human')
        with self.assertRaisesRegex(ContractError, 'permissions'):
            with self.store.tx() as c:
                self.store.roles.hire(c, self.area, 'verification', {**self.settings, 'sandbox': 'invalid'}, 'Human choice')

    def test_verifier_capacity_release_reuse_and_result_revision_invalidates_pass(self):
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['subdomainId'], 'Human policy', route='work-verification')
            self.store.roles.hire(c, self.area, 'verification', self.verified, 'Human verifier')
        first = self.roles_request()
        self.turn(first['conductor_id'], [{'type':'delegate','requestId':first['id'],'title':'first','spec':{'writeScope':[]}}]); self.pump()
        d = self.one('SELECT * FROM delegations')
        ctx = self.store.attempt_context(d['attempt_id'])
        self.assertNotEqual(ctx['worker']['id'], ctx['verifier'])
        binding={'taskId':d['id'],'workRunId':'work-1','resultRevision':'r1','verificationRunId':'verify-1'}
        self.store.record_workflow(d['attempt_id'], {'verificationState':'pass','binding':binding})
        self.store.record_workflow(d['attempt_id'], {'verificationState':'pass','binding':{**binding,'resultRevision':'r2'}})
        self.assertEqual(self.store.roles.task(self.store.connection(),d)['verificationState'], 'revision-waiting')
        fresh = {**binding, 'resultRevision':'r2', 'verificationRunId':'verify-2'}
        self.store.record_workflow(d['attempt_id'], {'verificationState':'pass','binding':fresh})
        self.finish(d, result={'resultPath':'result','receiptPath':'receipt','verificationState':'pass','binding':fresh})
        self.turn(first['conductor_id'], [{'type':'reply','requestId':first['id'],'text':'done','final':True}])
        second = self.roles_request('second')
        self.turn(second['conductor_id'], [{'type':'delegate','requestId':second['id'],'title':'second','spec':{'writeScope':[]}}]); self.pump()
        current = self.one('SELECT * FROM delegations ORDER BY rowid DESC LIMIT 1')
        self.assertEqual(self.store.attempt_context(current['attempt_id'])['verifier'], ctx['verifier'])

    def test_managed_bridge_uses_existing_loop_start_and_exact_roles_without_live_provider(self):
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['subdomainId'], 'Human policy', route='work-verification')
            self.store.roles.hire(c, self.area, 'verification', self.verified, 'Human verifier')
        req = self.roles_request()
        self.turn(req['conductor_id'], [{'type':'delegate','requestId':req['id'],'title':'exact','spec':{'writeScope':[]}}]); self.pump()
        d = self.one('SELECT * FROM delegations')
        ctx = self.store.attempt_context(d['attempt_id'])
        backend = ManagedBackend(types.SimpleNamespace(root=self.project,path=self.db,chat_id='conductor-queue'))
        request = ManagedExecutor(self.store,self.project,backend=backend).request(ctx)
        with mock.patch.object(backend.workflows, 'call', return_value={'loopId':'loop-stub'} ) as called, mock.patch.dict('os.environ',{},clear=True):
            external = backend.submit(request)
        argv = called.call_args.args[0]
        self.assertEqual(argv[0], 'start')
        self.assertEqual(argv[argv.index('--task-mode')+1], 'work-verification')
        self.assertEqual(argv[argv.index('--verification-agent')+1], ctx['verifier'])
        self.assertEqual(argv[argv.index('--work-model')+1], self.settings['model'])
        self.assertEqual(backend.lookup(request), external)
        self.assertTrue(external['workflow'])

    def test_real_loop_revision_and_pass_return_to_the_same_queue_task(self):
        import test_agent_loop
        agent_exec, loop = test_agent_loop.load_modules()
        fake = test_agent_loop.FakeRuntime(self.project, agent_exec)
        with self.store.tx() as c:
            self.store.roles.configure(c, self.area['subdomainId'], 'Human policy', route='work-verification')
            self.store.roles.hire(c, self.area, 'verification', self.verified, 'Human verifier')
        req = self.roles_request()
        self.turn(req['conductor_id'], [{'type':'delegate','requestId':req['id'],'title':'exact','spec':{'writeScope':[]}}]); self.pump()
        d = self.one('SELECT * FROM delegations')
        ctx = self.store.attempt_context(d['attempt_id'])
        backend = ManagedBackend(types.SimpleNamespace(root=self.project,path=self.db,chat_id='conductor-queue'))
        request = ManagedExecutor(self.store,self.project,backend=backend).request(ctx)
        def call(argv):
            args = loop.build_parser().parse_args([*argv, '--project-root', str(self.project)])
            return loop.start_loop(args) if argv[0]=='start' else loop.status_loop(args)
        with mock.patch.object(loop, 'AgentRuntime', return_value=fake), mock.patch.object(backend.workflows, 'call', side_effect=call), mock.patch.dict('os.environ', {'AGENT_FACTORY_PARENT_STATE': '', 'AGENT_FACTORY_EXECUTION_POLICY': json.dumps(test_agent_loop.runtime_test_home.policy('read-only', self.project))}):
            external = backend.submit(request)
            args = loop.build_parser().parse_args(['reconcile','--project-root',str(self.project),'--work-agent',ctx['worker']['id'],'--loop-id',external['loopId']])
            state = backend.workflows.observe(external)['workflowState']
            fake.complete_work(ctx['worker']['id'],state['latestWorkRunId'])
            state = loop.reconcile_loop(args)
            self.assertEqual(backend.status(external)['verificationState'], 'verifying')
            fake.complete_verification(ctx['verifier'],state['latestVerificationRunId'],'fail')
            state = loop.reconcile_loop(args)
            self.assertEqual(backend.status(external)['verificationState'], 'revision-waiting')
            fake.complete_work(ctx['worker']['id'],state['latestWorkRunId'],['finding-1'])
            state = loop.reconcile_loop(args)
            fake.complete_verification(ctx['verifier'],state['latestVerificationRunId'],'pass')
            state = loop.reconcile_loop(args)
            result = backend.completion(external)
            marker=self.db.with_name(request['run']['dispatchId']+'.workflow.json')
            original=marker.read_bytes(); marker.write_text('{}')
            # Remove only this acknowledgement fixture; the real loop acceptance is retained.
            marker.unlink()
            recovered=backend.lookup(request)
            marker.write_bytes(original)
            self.assertEqual(recovered['loopId'],external['loopId'])
        self.assertEqual(result['verificationState'], 'pass')
        self.assertEqual(result['binding']['taskId'],d['id'])
        self.assertEqual(result['binding']['workRunId'], state['latestWorkRunId'])
        self.assertEqual(result['binding']['verificationRunId'],state['latestVerificationRunId'])
        self.store.record_workflow(d['attempt_id'], result)
        self.finish(d,result=result)
        self.turn(req['conductor_id'], [{'type':'reply','requestId':req['id'],'text':'done','final':True}])
        self.assertEqual(self.one('SELECT status FROM requests')['status'],'completed')

    def test_independent_task_continues_while_another_waits_for_exact_human_answer(self):
        self.send('first', self.area)
        a = self.one('SELECT * FROM requests')
        self.turn(a['conductor_id'], [{'type':'delegate','requestId':a['id'],'title':'question','spec':{'writeScope':[]}}]); self.pump()
        d = self.one('SELECT * FROM delegations')
        self.finish(d,'question',question={'text':'Credentials needed','decisionKind':'clarification','reason':'credential-missing','impact':'first task only','alternatives':['Provide credential','Cancel task']})
        self.send('independent', self.area_b)
        other = self.one('SELECT * FROM requests ORDER BY rowid DESC LIMIT 1')
        self.turn(other['conductor_id'], [{'type':'reply','requestId':other['id'],'text':'done','final':True}])
        self.assertEqual(self.one('SELECT status FROM requests WHERE id=?',other['id'])['status'],'completed')
        question = self.one("SELECT * FROM questions WHERE delegation_id=? AND state='pending'",d['id'])
        details = self.store.question_view(question)
        self.assertEqual(details['reason'], 'credential-missing')
        self.assertEqual(details['impact'], 'first task only')
        current = self.one('SELECT * FROM delegations WHERE id=?',d['id'])
        self.command(action='answer-worker',delegationId=d['id'],expectedVersion=current['version'],questionId=question['id'],text='exact credential answer')
        self.assertEqual(self.one('SELECT status FROM delegations WHERE id=?',d['id'])['status'],'dispatched')

    def test_bypass_redundant_execution_approval_resumes_but_external_credentials_still_ask(self):
        self.send('bounded', self.area)
        req = self.one('SELECT * FROM requests')
        self.turn(req['conductor_id'], [{'type':'delegate','requestId':req['id'],'title':'bounded','spec':{'writeScope':[]}}]); self.pump()
        d = self.one('SELECT * FROM delegations')
        self.finish(d, 'question', question={'text':'Execute approved task?', 'decisionKind':'approval', 'decisionScope':'task-execution'})
        self.assertEqual(self.one('SELECT status FROM delegations')['status'],'dispatched')
        self.assertEqual(self.one("SELECT COUNT(*) n FROM questions WHERE state='pending'")['n'],0)
        self.store.ingest('pool','lifecycle',{'delegationId':d['id'],'attemptId':d['attempt_id'],'workerEventId':'credentials','kind':'question','question':{'text':'Need credentials','decisionKind':'clarification','decisionScope':'external-prerequisite'}})
        self.pump()
        self.assertEqual(self.one('SELECT status FROM delegations')['status'],'awaiting-answer')

    def test_control_registration_and_cancel_preserve_scope_and_do_not_complete_remaining_tasks(self):
        self.send(area=self.area)
        req = self.one('SELECT * FROM requests')
        self.command(action='register-required',requestId=req['id'],expectedVersion=req['version'],tasks=self.manifest())
        self.assertEqual(self.one('SELECT COUNT(*) n FROM obligations')['n'],2)
        current = self.one('SELECT * FROM requests')
        self.command(action='cancel',requestId=req['id'],expectedVersion=current['version'])
        with self.store.tx() as c:
            self.store.responsibility.reconcile(c)
        self.assertEqual(self.one('SELECT status FROM requests')['status'],'cancelled')
        self.assertEqual(self.one("SELECT COUNT(*) n FROM obligations WHERE state='completed'")['n'],0)

    def test_late_required_registration_reacquires_an_instance_after_cross_chat_reuse(self):
        self.send('first',self.area)
        original = self.one('SELECT * FROM requests')
        self.turn(original['conductor_id'],[{'type':'reply','requestId':original['id'],'text':'done','final':True}])
        self.store.ensure_chat('another',self.policy)
        self.store.ingest('another','conversation',{'clientMessageId':'other','text':'another request','relation':'new','area':self.area})
        self.pump()
        other = self.one("SELECT * FROM requests WHERE chat_id='another'")
        self.assertEqual(other['conductor_id'],original['conductor_id'])
        with self.store.tx() as c:
            self.store.responsibility.register(c,original['id'],self.manifest(),'Human late followup')
        reopened = self.one('SELECT * FROM requests WHERE id=?',original['id'])
        self.assertEqual(reopened['status'],'queued-for-instance')
        self.assertEqual(reopened['conductor_id'],'')
        self.turn(other['conductor_id'],[{'type':'reply','requestId':other['id'],'text':'done','final':True}])
        runtime=Runtime(self.store,conductor=FakeConductor(delay=0,chunk_delay=0))
        self.assertTrue(runtime.drain(5))
        self.assertEqual(self.one('SELECT status FROM requests WHERE id=?',original['id'])['status'],'completed')

    def test_explicit_human_scope_reduction_is_preserved_separately_from_completion(self):
        self.send(area=self.area)
        req=self.one('SELECT * FROM requests')
        with self.store.tx() as c:
            tasks=self.store.responsibility.register(c,req['id'],self.manifest(),'Human scope')['tasks']
        excluded=tasks[1]
        self.command(action='exclude-required',requestId=req['id'],obligationId=excluded['id'],expectedTaskVersion=excluded['version'],reason='Human excluded document synchronization')
        view=self.store.responsibility.view(self.store.connection(),req['id'])
        self.assertEqual(view['tasks'][1]['state'],'excluded')
        self.assertEqual(view['tasks'][1]['completionEvidence']['actor'],'human')
        self.assertEqual(view['tasks'][0]['state'],'registered')

    def test_existing_native_verifier_is_counted_and_reused_only_after_explicit_child_placement(self):
        self.store.project_root = self.project
        agent = 'existing-verifier'
        session = self.db.parent / 'agents' / agent / 'session.json'
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({'agentId':agent,'role':'verification',**self.verified,'humanApprovalPolicy':'bypass'}))
        domains.assign(self.db.parent,agent,self.area['domainId'],'human','original Human placement')
        projection=self.store.roles.projection(self.store.connection())
        parent=next(a for a in projection['areas'] if a['id']==self.area['domainId'])
        child=next(a for a in projection['areas'] if a['id']==self.area['subdomainId'])
        self.assertEqual(parent['counts']['verification']['hired'],1)
        self.assertEqual(child['counts']['verification']['hired'],0)
        self.assertTrue(parent['members'][0]['reuseRequiresArea'])
        domains.assign_subdomain(self.db.parent,agent,self.area['domainId'],self.area['subdomainId'],'human','explicit Human child placement')
        original=session.read_bytes()
        with self.store.tx() as c:
            hired=self.store.roles.hire(c,self.area,'verification',self.verified,'Human reuse request')
        self.assertEqual(hired['agentId'],agent)
        self.assertTrue(hired['reused'])
        self.assertEqual(session.read_bytes(),original)

    def test_role_specific_limits_reject_invalid_values_and_keep_parent_caps(self):
        configured=self.store.pool.configure({'verification':{'domainLimit':1,'subdomainLimit':1}},'Human verifier cap')
        self.assertEqual(configured['verification']['domainLimit'],1)
        with self.assertRaises(ContractError):
            self.store.pool.configure({'subdomains':{self.area['subdomainId']:{'verification':{'domainLimit':5}}}},'Human setting')

    def test_managed_conductor_declares_scope_through_the_real_command_parser(self):
        from conductor.runtime import ManagedConductor
        policy={**self.native_policy,'conductor':'managed'}
        self.store.ensure_chat('scope',policy)
        self.store.ingest('scope','conversation',{'clientMessageId':'scope','text':'output coordination then docs','relation':'new','area':self.area})
        self.pump()
        req=self.one("SELECT * FROM requests WHERE chat_id='scope'")
        context=self.store.begin_turn(req['conductor_id'])
        instruction=context['responsibility'][req['id']]['pendingInstructions'][0]
        response={'commands':[{'type':'register-required','requestId':req['id'],'tasks':self.manifest()}]}
        backend=types.SimpleNamespace(lookup=lambda request:{'runId':'stub','status':'completed','response':json.dumps(response)})
        commands,_=ManagedConductor(self.project,self.store,backend=backend).turn(context,lambda *args:None)
        token={k:context[k] for k in ('conductorId','slot','epoch','turnId')}
        self.store.end_turn(token,commands)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM obligations')['n'],2)
        self.assertEqual(self.one('SELECT state FROM responsibility_instructions WHERE id=?',instruction['id'])['state'],'pending')

    def test_automatic_verifier_hire_keeps_human_settings_and_records_ai_placement(self):
        with self.store.tx() as c:
            self.store.roles.configure(c,self.area['domainId'],'Human verifier settings',profile='verification',settings=self.verified,route='work-verification')
        req=self.roles_request()
        self.turn(req['conductor_id'],[{'type':'delegate','requestId':req['id'],'title':'bounded','spec':{'writeScope':[]}}]); self.pump()
        d=self.one('SELECT * FROM delegations')
        verifier=self.store.attempt_context(d['attempt_id'])['verifier']
        self.assertTrue(verifier)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM area_role_settings WHERE area_id=?',self.area['subdomainId'])['n'],0)
        self.assertEqual(self.one('SELECT source FROM area_role_settings WHERE area_id=?',self.area['domainId'])['source'],'Human verifier settings')
        self.assertEqual(domains.read(self.db.parent)['assignments'][verifier]['setBy']['actor'],'ai')

    def test_deterministic_acceptance_fault_preserves_ownership_without_infinite_lookup_or_new_start(self):
        req=self.roles_request()
        self.turn(req['conductor_id'],[{'type':'delegate','requestId':req['id'],'title':'bounded','spec':{'writeScope':[]}}]); self.pump()
        d=self.one('SELECT * FROM delegations')
        ctx=self.store.attempt_context(d['attempt_id'])
        backend=mock.Mock()
        backend.lookup.side_effect=ContractError('workflow_acceptance_conflict','Captured acceptance disagrees')
        backend.classify_error.return_value='contract'
        ManagedExecutor(self.store,self.project,backend=backend).launch(ctx)
        self.assertEqual(backend.lookup.call_count,2)
        backend.submit.assert_not_called()
        self.assertEqual(self.one('SELECT status FROM attempts')['status'],'starting')
        external=json.loads(self.one('SELECT external FROM attempts')['external'])
        self.assertIn('workflow_acceptance_conflict',external['observation'])

    def test_existing_explorer_profile_is_not_reclassified_as_an_expert(self):
        self.store.project_root=self.project
        agent='existing-explorer'
        session=self.db.parent/'agents'/agent/'session.json'
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({'agentId':agent,'role':'work','workProfile':'explore',**self.settings,'humanApprovalPolicy':'bypass'}))
        domains.assign(self.db.parent,agent,self.area['domainId'],'human','existing explorer placement')
        parent=next(a for a in self.store.roles.projection(self.store.connection())['areas'] if a['id']==self.area['domainId'])
        self.assertEqual(parent['counts']['work']['hired'],0)
        self.assertEqual(parent['members'][0]['profile'],'explore')
