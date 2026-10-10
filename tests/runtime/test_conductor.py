"""Conductor durability, authority and existing managed execution boundary."""
import runtime_test_home
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from storage import paths
from storage.errors import ContractError
from tasks.conductor import Conductor
from tasks.conductor_backend import ManagedBackend

ROOT=Path(__file__).resolve().parents[2]
SETTINGS={role:{'provider':'codex','model':'gpt-test','reasoningEffort':'medium','fast':False} for role in ('conversation','work')}
POLICY={'approvalPolicy':'bypass','sandbox':'workspace-write','reference':'Human bounded authorization',
        'retryAuthority':{'reference':'bounded','classes':['contract','transient'],'maxAttempts':1}}
INSPECTION={'basis':'Expected milestone observations; two unchanged evidence samples trigger owner inquiry',
            'noProgressObservations':2,'repeatObservations':2}

class Backend:
    def __init__(self): self.calls=[];self.accepted={};self.lose_ack=False
    def submit(self,request):
        self.calls.append(copy.deepcopy(request))
        v={'runId':'managed-'+str(len(self.calls)),'status':'running','sessionId':'native-'+str(len(self.calls))}
        self.accepted[request['run']['dispatchId']]=v
        if self.lose_ack: raise OSError('lost acknowledgement')
        return v
    def lookup(self,request): return self.accepted.get(request['run']['dispatchId'])

class ConductorTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        paths.resolve(self.root,create=True);self.c=Conductor(self.root,'chat');self.c.initialize(SETTINGS,POLICY)
        self.backend=Backend()
    def message(self,mid='m',tid='t',deps=None):
        return {'id':mid,'kind':'task','text':'Build goal','taskId':tid,
                'task':{'goal':'Build goal','inputs':['input-ref'],'completionCriteria':'Accepted output and checks',
                        'writeScope':['src/owned.py'],'dependencies':deps or []}}
    def task(self,mid='m',tid='t',deps=None): return self.c.receive(self.message(mid,tid,deps))
    def start(self,m):
        r=self.c.reserve(m['sessionId'])['run'];self.c.dispatch(r['id'],self.backend)
        return self.c.read()['runs'][r['id']]
    def obs(self,r,oid='o',status='running',stopped=False,**data):
        return self.c.observe(r['id'],{'id':oid,'runId':r['managedRunId'],'status':status,**data},termination_confirmed=stopped)
    def proposal(self,scope='scope-expansion'):
        return {'kind':'approval','scope':scope,'target':'extra.py','reason':'Extra requirement',
                'effect':'Exact scope extension','alternatives':['Keep scope']}
    def response(self,d,answer='approved'):
        return {**{k:d[k] for k in ('taskId','runId','taskRevision','questionHash')},'decisionId':d['id'],
                'actor':'human','reference':'Exact Human response','answer':answer}

    def test_routing_dedup_concurrent_reservation_and_lost_ack_restart(self):
        m=self.task();self.assertEqual(self.c.receive(self.message()),m)
        with self.assertRaises(ContractError):self.c.receive({**self.message(),'text':'changed'})
        with ThreadPoolExecutor(max_workers=2) as p:
            outcomes=list(p.map(lambda _:self.c.reserve(m['sessionId']),range(2)))
        self.assertEqual(sorted(v['disposition'] for v in outcomes),['busy','reserved'])
        rid=self.c.read()['sessions'][m['sessionId']]['activeRunId'];self.backend.lose_ack=True
        with self.assertRaises(OSError):self.c.dispatch(rid,self.backend)
        restored=Conductor(self.root,'chat');restored.dispatch(rid,self.backend)
        self.assertEqual(len(self.backend.calls),1)
        q=restored.receive({'id':'q','kind':'question','text':'Independent'})
        self.assertNotEqual(q['sessionId'],m['sessionId'])
        self.assertEqual(restored.reserve(q['sessionId'])['disposition'],'reserved')
        q2=restored.receive({'id':'q2','kind':'question','text':'Explain','taskId':'t','taskRevision':1})
        self.assertEqual(q2['sessionId'],m['sessionId']);self.assertEqual(q2['authority'],'read-only')
        self.assertEqual(restored.reserve(q2['sessionId'])['disposition'],'busy')

    def test_stale_result_dependency_and_termination_confirmation(self):
        m=self.task();r=self.start(m);dependent=self.task('m2','t2',['t'])
        self.assertEqual(self.c.reserve(dependent['sessionId'])['dependencies'],['t'])
        follow=self.c.receive({'id':'r','kind':'rework','text':'New requirement','taskId':'t','taskRevision':1})
        with self.assertRaises(ContractError):self.c.receive({'id':'r2','kind':'rework','text':'Old','taskId':'t','taskRevision':1})
        self.obs(r,status='completed',checks='passed',taskCompleted=True,resultPath='old.md')
        self.assertEqual(self.c.snapshot()['tasks'][0]['status'],'pending')
        self.assertEqual(self.c.reserve(m['sessionId'])['disposition'],'busy')
        self.obs(r,'dead',status='completed',stopped=True,checks='passed',taskCompleted=True)
        fresh=self.start(follow)
        self.obs(fresh,status='completed',stopped=True,checks='passed',taskCompleted=True,resultPath='new.md')
        task=self.c.snapshot()['tasks'][0];self.assertEqual(task['kanban'],'done');self.assertEqual(len(task['results']),2)
        self.assertEqual(self.c.reserve(dependent['sessionId'])['disposition'],'reserved')

    def test_question_does_not_complete_work_and_checks_are_separate(self):
        m=self.task();r=self.start(m);self.obs(r,status='completed',stopped=True,resultPath='output.md')
        self.assertEqual(self.c.snapshot()['tasks'][0]['status'],'awaiting-acceptance')
        q=self.c.receive({'id':'q','kind':'question','text':'Explain','taskId':'t','taskRevision':1})
        qr=self.start(q);self.obs(qr,status='completed',stopped=True,checks='passed',taskCompleted=True)
        self.assertEqual(self.c.snapshot()['tasks'][0]['status'],'awaiting-acceptance')

    def test_handoff_preserves_history_and_never_reuses_provider_native_id(self):
        m=self.task();r=self.start(m);config={'provider':'claude','model':'claude-test','reasoningEffort':'medium'}
        with self.assertRaises(ContractError):self.c.switch_provider('t',1,config)
        self.obs(r,status='failed',stopped=True,error={'code':'native_backend_error'},failureClass='provider',resultPath='partial.md')
        self.c.receive({'id':'r','kind':'rework','text':'Revise','taskId':'t','taskRevision':1})
        new=self.c.switch_provider('t',2,config)
        self.assertIsNone(new['nativeSessionId']);self.assertFalse(new['managedCreated'])
        self.assertEqual(new['handoff']['results'][0]['resultPath'],'partial.md')
        self.assertEqual(new['handoff']['requirements'],['Revise']);self.assertEqual(new['handoff']['policy'],POLICY)
        self.assertIn(m['sessionId'],self.c.read()['sessions'])
        self.assertEqual(self.c.read()['messages'][-1]['sessionId'],new['id'])

    def test_decision_binding_idempotency_resume_and_bypass(self):
        m=self.task();r=self.start(m)
        self.assertEqual(self.c.decision(r['id'],self.proposal('task-execution'))['disposition'],'already-authorized')
        d=self.c.decision(r['id'],self.proposal());answer=self.response(d)
        with self.assertRaises(ContractError):self.c.answer({**answer,'taskRevision':2})
        self.c.answer(answer);self.c.answer(answer)
        self.assertEqual(sum(m['id']=='answer-'+d['id'] for m in self.c.read()['messages']),1)
        self.assertEqual(self.c.reserve(m['sessionId'])['disposition'],'busy')
        self.obs(r,status='needs-human-decision',stopped=True)
        self.assertEqual(self.c.reserve(m['sessionId'])['disposition'],'reserved')
        with self.assertRaises(ContractError):self.c.answer({**answer,'answer':'declined'})

    def test_required_approval_and_protected_input(self):
        c=Conductor(self.root,'required');c.initialize(SETTINGS,{**POLICY,'approvalPolicy':'required'})
        m=c.receive(self.message());r=c.reserve(m['sessionId'])['run']
        with self.assertRaises(ContractError):c.dispatch(r['id'],self.backend)
        d=c.decision(r['id'],self.proposal('task-execution'));c.answer(self.response(d));c.dispatch(r['id'],self.backend)
        self.assertEqual(len(self.backend.calls),1)
        p={**self.proposal('external-prerequisite'),'kind':'clarification','requiresCredential':True,
           'protectedInputReference':'existing-provider-login'}
        d=c.decision(r['id'],p)
        with self.assertRaises(ContractError):c.answer(self.response(d,'secret'))
        self.assertNotIn('secret',c.path.read_text())

    def test_inspection_all_assessments_dedup_and_completion_chat_race(self):
        r=self.start(self.task());self.obs(r,heartbeat=1,tokens=100)
        self.assertEqual(self.c.inspect(r['id'],INSPECTION)['report']['assessment'],'indeterminate')
        self.obs(r,'o2',artifacts=['design.md'],goalEvidence='Milestone one')
        report=self.c.inspect(r['id'],INSPECTION)
        self.assertEqual(report['report']['assessment'],'progressing');self.assertEqual(self.c.inspect(r['id'],INSPECTION)['id'],report['id'])
        self.obs(r,'o3',changes=['code.py'],goalEvidence='Milestone two')
        self.assertEqual(self.c.inspect(r['id'],INSPECTION)['report']['assessment'],'progressing')
        self.obs(r,'o4',changes=['code.py'],goalEvidence='Milestone two',milestoneExceeded=True)
        self.assertEqual(self.c.inspect(r['id'],INSPECTION)['report']['assessment'],'suspected-stall')
        self.obs(r,'o5',blocker='Missing input');self.assertEqual(self.c.inspect(r['id'],INSPECTION)['report']['assessment'],'waiting')
        self.obs(r,'o6',offGoal=True,goalEvidence='Outside goal');self.assertEqual(self.c.inspect(r['id'],INSPECTION)['report']['assessment'],'direction-deviation')
        cursor=self.c.reports('ui')['events'][-1]['sequence']
        self.obs(r,'o7',status='completed',stopped=True,checks='passed',taskCompleted=True)
        self.c.receive({'id':'new','kind':'question','text':'New conversation'})
        pending=self.c.reports('ui',cursor)['events']
        self.assertEqual([e['kind'] for e in pending],['execution-ended','message-received'])
        self.assertEqual(Conductor(self.root,'chat').reports('ui')['events'],pending)

    def test_retry_failure_class_authority_budget_and_outputs(self):
        r=self.start(self.task());self.obs(r,status='failed',error={'code':'worker_failure'},failureClass='transient',resultPath='partial.md')
        with self.assertRaises(ContractError):self.c.retry('t',1,reference='retry',failure_class='transient')
        self.obs(r,'dead',status='failed',stopped=True,error={'code':'worker_failure'},failureClass='transient')
        with self.assertRaises(ContractError):self.c.retry('t',1,reference='retry',failure_class='provider')
        self.c.retry('t',1,reference='bounded',failure_class='transient')
        with self.assertRaises(ContractError):self.c.retry('t',1,reference='bounded',failure_class='transient')
        self.assertEqual(self.c.snapshot()['tasks'][0]['results'][0]['resultPath'],'partial.md')

    def test_managed_bridge_passes_original_settings_and_bound_task(self):
        m=self.task();r=self.c.reserve(m['sessionId'])['run'];backend=ManagedBackend(self.c)
        with mock.patch.object(backend,'call',return_value={'runId':'managed','status':'running'}) as call:
            self.c.dispatch(r['id'],backend);argv=call.call_args.args[0]
        self.assertEqual(argv[0],'submit');self.assertIn('--no-fast',argv)
        doc=json.loads(Path(argv[argv.index('--task-list-file')+1]).read_text());self.assertEqual(doc['tasks'][0]['id'],'t')
        req=json.loads(Path(argv[argv.index('--request-file')+1]).read_text());self.assertEqual(req['task']['spec']['writeScope'],['src/owned.py'])

    def test_real_cli_roundtrip_and_restart(self):
        def call(command,payload=None,extra=()):
            argv=[sys.executable,str(ROOT/'scripts/conductor.py'),command,'--project-root',str(self.root),'--chat-id','cli',*extra]
            if payload is not None:
                p=self.root/'input.json';p.write_text(json.dumps(payload));argv.extend(['--input',str(p)])
            value=subprocess.run(argv,capture_output=True,text=True,stdin=subprocess.DEVNULL)
            self.assertEqual(value.returncode,0,value.stdout+value.stderr);return json.loads(value.stdout)
        call('init',{'settings':SETTINGS,'policy':POLICY})
        m=call('receive',{'id':'q','kind':'question','text':'Independent'})
        r=call('reserve',extra=('--session-id',m['sessionId']))
        self.assertEqual(call('status')['runs'][0]['id'],r['run']['id'])
        self.assertEqual(call('reports',extra=('--consumer','ui'))['events'][0]['kind'],'message-received')
        call('reports',extra=('--consumer','ui','--ack','1'))
        self.assertEqual(call('reports',extra=('--consumer','ui'))['events'],[])

    def test_real_managed_acceptance_observation_receipt_rework_and_auto_report(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('exec',ROOT/'scripts/exec.py')
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        self.c=Conductor(self.root,'auto')
        self.c.initialize(SETTINGS,{**POLICY,'supervision':INSPECTION})
        backend=ManagedBackend(self.c)
        def actual_call(argv):
            argv=[*argv,'--project-root',str(self.root)]
            if argv[0]=='submit': argv.extend(['--codex','/bin/true'])
            captured=[]
            with mock.patch.object(runtime,'emit',side_effect=captured.append):
                if argv[0] in {'submit','send'}:
                    runtime.submit(runtime.parse_args(argv),argv[0]=='submit')
                else:
                    self.assertEqual(runtime.main(argv),0,str(captured))
            return captured[-1]
        parent=runtime.create_run(project_root=self.root,agent_id='main-host',actor='main',request=b'Bounded conductor request',
                                  session={'role':'main','conversationId':'main-conversation','maxAttempts':1})
        parent_path=Path(parent['statePath'])
        parent.update(status='running',taskAnnouncementContract=1,
                      executionPolicy=runtime_test_home.policy('workspace-write',self.root),humanApprovalPolicy='bypass')
        runtime.atomic_write_json(parent_path,parent)
        runtime.atomic_write_json(runtime.session_file(self.root,'main-host'),
                                  {'role':'main','agentId':'main-host','projectRoot':str(self.root),'conversationId':'main-conversation',
                                   'executionPolicy':parent['executionPolicy'],'humanApprovalPolicy':'bypass'})
        with mock.patch.dict(sys.modules,{'exec':runtime}), \
             mock.patch.dict(os.environ,{'AGENT_FACTORY_EXECUTION_POLICY':json.dumps(runtime_test_home.policy('workspace-write',self.root)),
                                         'AGENT_FACTORY_PARENT_STATE':str(parent_path)}), \
             mock.patch.object(runtime.native_codex,'inspect_capabilities',return_value={'submit':{'goal':True},'send':{'goal':True},'diagnostic':None}), \
             mock.patch.object(runtime,'spawn_worker',return_value=0), \
             mock.patch.object(backend,'call',side_effect=actual_call):
            m=self.task();r=self.c.reserve(m['sessionId'])['run']
            accepted=self.c.dispatch(r['id'],backend)
            session=self.c.read()['sessions'][m['sessionId']]
            raw=runtime.find_run(self.root,session['agentId'],accepted['managedRunId'])
            self.assertEqual(raw['taskBinding']['taskId'],'t')
            self.assertEqual(raw['executionOptions']['reasoningEffort'],'medium')
            self.assertFalse(raw['executionOptions']['fast'])
            runtime.mark_terminal(Path(raw['statePath']),'completed')
            raw=runtime.find_run(self.root,session['agentId'],raw['runId'])
            Path(raw['resultPath']).write_text('Completed bounded goal')
            receipt={'schemaVersion':'0.1.0','kind':'work-receipt','runId':raw['runId'],
                     'requestHash':raw['receiptRequestHash'],'outcome':'completed','changedPaths':['src/owned.py'],
                     'addressedFindingIds':[],'tests':{'run':True,'reason':'Own fixture check passed'}}
            Path(raw['receiptPath']).write_text(json.dumps(receipt))
            backend.observe(r['id'])
            self.assertEqual(self.c.snapshot()['tasks'][0]['status'],'awaiting-acceptance')
            backend.observe(r['id'],{'runId':raw['runId'],'taskRevision':1,'source':raw['resultPath'],
                                      'checks':'passed','goalEvidence':'Completion criterion checked'})
            self.assertEqual(self.c.snapshot()['tasks'][0]['status'],'completed')
            reports=self.c.reports('ui')['events']
            self.assertIn('inspection-report',[e['kind'] for e in reports])
            self.assertEqual(reports[-1]['report']['assessment'],'progressing')
            # Send through the actual managed allocation-preservation check.
            follow=self.c.receive({'id':'follow','kind':'rework','taskId':'t','taskRevision':1,'text':'Refine existing goal'})
            next_run=self.c.reserve(follow['sessionId'])['run'];self.c.dispatch(next_run['id'],backend)
            self.assertEqual(self.c.read()['sessions'][m['sessionId']]['agentId'],session['agentId'])

    def test_rework_supersedes_unstarted_reservation_and_denial_releases_it(self):
        m=self.task();old=self.c.reserve(m['sessionId'])['run']
        follow=self.c.receive({'id':'revision','kind':'rework','taskId':'t','taskRevision':1,'text':'New requirement'})
        with self.assertRaises(ContractError):self.c.dispatch(old['id'],self.backend)
        self.assertEqual(self.c.reserve(follow['sessionId'])['disposition'],'reserved')
        c=Conductor(self.root,'deny');c.initialize(SETTINGS,{**POLICY,'approvalPolicy':'required'})
        m=c.receive(self.message());r=c.reserve(m['sessionId'])['run']
        d=c.decision(r['id'],self.proposal('task-execution'));c.answer(self.response(d,'declined'))
        self.assertIsNone(c.read()['sessions'][m['sessionId']]['activeRunId'])
        self.assertEqual(c.read()['runs'][r['id']]['status'],'cancelled')

    def test_auto_reports_ignore_heartbeat_duplicates_and_failed_start_stays_owned(self):
        self.c=Conductor(self.root,'auto2');self.c.initialize(SETTINGS,{**POLICY,'supervision':INSPECTION})
        r=self.start(self.task());self.obs(r,heartbeat=1)
        self.obs(r,'h2',heartbeat=2)
        count=len([e for e in self.c.reports('ui')['events'] if e['kind']=='inspection-report'])
        self.obs(r,'h3',heartbeat=3)
        self.assertEqual(count,len([e for e in self.c.reports('ui')['events'] if e['kind']=='inspection-report']))
        self.assertEqual(self.c.reports('ui')['events'][-1]['report']['assessment'],'indeterminate')
        q=self.c.receive({'id':'q','kind':'question','text':'Question'})
        reservation=self.c.reserve(q['sessionId'])['run']
        with mock.patch.object(self.backend,'submit',side_effect=OSError('unknown acceptance')):
            with self.assertRaises(OSError):self.c.dispatch(reservation['id'],self.backend)
        self.assertEqual(self.c.dispatch(reservation['id'],self.backend)['disposition'],'waiting')
        self.assertEqual(self.c.reserve(q['sessionId'])['disposition'],'busy')
        self.assertEqual(self.c.reports('ui')['events'][-1]['kind'],'execution-start-uncertain')

    def test_repeated_action_and_error_require_owner_evidence(self):
        r=self.start(self.task())
        for oid in ('repeat1','repeat2'):
            self.obs(r,oid,action='same failed command',error={'code':'worker_failure'})
        report=self.c.inspect(r['id'],INSPECTION)['report']
        self.assertEqual(report['assessment'],'suspected-stall')
        self.assertIn('No goal-linked output/change/check evidence',report['uncertainty'])
        self.assertEqual(report['nextStep'],'request-owner-evidence')
        self.assertEqual(report['originalGoal'],'Build goal')

    def test_question_boundary_and_new_provider_command_never_forward_native_id(self):
        m=self.task();r=self.start(m);self.obs(r,status='completed',stopped=True)
        q=self.c.receive({'id':'q','kind':'question','text':'Explain','taskId':'t','taskRevision':1})
        qr=self.c.reserve(q['sessionId'])['run'];backend=ManagedBackend(self.c)
        with mock.patch.object(backend,'call',return_value={'runId':'question-run','status':'running'}) as call:
            self.c.dispatch(qr['id'],backend);argv=call.call_args.args[0]
        self.assertEqual(argv[0],'send')
        binding=json.loads(Path(argv[argv.index('--capability-binding-file')+1]).read_text())
        self.assertEqual(binding['bindings'][0]['allowedEffects'],['read','respond'])
        qr=self.c.read()['runs'][qr['id']];self.obs(qr,status='completed',stopped=True)
        self.c.receive({'id':'rework','kind':'rework','text':'Continue','taskId':'t','taskRevision':1})
        session=self.c.switch_provider('t',2,{'provider':'claude','model':'claude-test','reasoningEffort':'medium'})
        new=self.c.reserve(session['id'])['run']
        with mock.patch.object(backend,'call',return_value={'runId':'claude-run','status':'running'}) as call:
            self.c.dispatch(new['id'],backend);argv=call.call_args.args[0]
        self.assertEqual(argv[0],'submit');self.assertEqual(argv[argv.index('--provider')+1],'claude')
        self.assertNotIn('native-1',argv)
        request=json.loads(Path(argv[argv.index('--request-file')+1]).read_text())
        self.assertEqual(request['handoff']['fromSessionId'],m['sessionId'])
