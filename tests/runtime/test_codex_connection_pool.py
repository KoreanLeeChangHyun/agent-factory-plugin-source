"""Retained connections keep exact sessions and clean up on owner/client loss."""
import runtime_test_home
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from native_fixtures import native, runtime, native_fixture

FAKE = '''#!/usr/bin/env python3
import json,sys,os,time
from pathlib import Path
log=Path(__file__).with_suffix('.log')
def out(v): print(json.dumps(v),flush=True)
for line in sys.stdin:
 v=json.loads(line); m=v.get('method'); p=v.get('params',{})
 with log.open('a') as f: f.write(json.dumps([os.getpid(),m,p])+'\\n')
 if 'id' not in v: continue
 r={}
 if m=='turn/start': r={'turn':{'id':'turn-one'}}
 if m in ('thread/start','thread/resume'): r={'thread':{'id':p.get('threadId','thread-exact')}}
 out({'id':v['id'],'result':r})
 if m=='turn/start':
  if any(i.get('text')=='hold' for i in p.get('input',[])):
   while not Path(__file__).with_suffix('.release').exists(): time.sleep(.01)
  result={'status':'completed','resultPath':p['outputSchema']['properties']['resultPath']['const'],'resultText':'ok','decisionKind':None}
  out({'method':'turn/started','params':{'threadId':'thread-exact','turn':{'id':'turn-one'}}})
  out({'method':'item/completed','params':{'threadId':'thread-exact','item':{'type':'agentMessage','text':json.dumps(result)}}})
  out({'method':'turn/completed','params':{'threadId':'thread-exact','turn':{'id':'turn-one','status':'completed'}}})
'''

class ConnectionPoolTests(unittest.TestCase):
    def test_retains_process_resumes_exact_thread_and_replaces_changed_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'; fake.write_text(FAKE); fake.chmod(0o700)
            bridge, _, state = native_fixture(root, goal=False)
            session = bridge.session
            session['codex'] = str(fake)
            session['nativeCapabilities'] = {'goal': False}
            state['nativeSessionPath'] = str(Path(state['statePath']).parent / 'native-session.json')
            runtime.atomic_write_json(Path(state['statePath']), state)
            runtime.atomic_write_json(Path(state['nativeSessionPath']), session)
            host = subprocess.Popen([sys.executable, native.__file__, '--connection-host'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                endpoint = json.loads(host.stdout.readline())
                def send(reusable=True):
                    with socket.create_connection(('127.0.0.1',endpoint['port'])) as client:
                        with client.makefile('rwb') as wire:
                            wire.write((json.dumps({'token':endpoint['token'],'statePath':state['statePath'],
                                'prompt':'hello','parts':False})+'\n').encode()); wire.flush()
                            events=[]
                            for line in wire:
                                event=json.loads(line);events.append(event)
                                if 'poolDone' in event: break
                            self.assertEqual(events[-1].get('reusable'), reusable, events)
                            if reusable: self.assertTrue(any(e.get('type')=='item.completed' for e in events),events)
                send(); send()
                calls=[json.loads(line) for line in fake.with_suffix('.log').read_text().splitlines()]
                self.assertEqual(len({row[0] for row in calls}),1)
                self.assertEqual(sum(row[1]=='initialize' for row in calls),1)
                self.assertEqual([row[2]['threadId'] for row in calls if row[1]=='thread/resume'],['thread-exact']*2)
                session['model']='different-model'
                runtime.atomic_write_json(Path(state['nativeSessionPath']),session)
                send()
                calls=[json.loads(line) for line in fake.with_suffix('.log').read_text().splitlines()]
                self.assertEqual(len({row[0] for row in calls}),2)
                state['cancelRequested']=True
                runtime.atomic_write_json(Path(state['statePath']),state)
                send(reusable=False)
                # Cancellation acknowledgement follows containment teardown.
                last_pid=calls[-1][0]
                with self.assertRaises(ProcessLookupError): os.kill(last_pid,0)
                state['cancelRequested']=False
                runtime.atomic_write_json(Path(state['statePath']),state)
                client=socket.create_connection(('127.0.0.1',endpoint['port']))
                wire=client.makefile('rwb')
                wire.write((json.dumps({'token':endpoint['token'],'statePath':state['statePath'],
                    'prompt':'hold','parts':False})+'\n').encode());wire.flush()
                deadline=time.monotonic()+5
                while True:
                    calls=[json.loads(line) for line in fake.with_suffix('.log').read_text().splitlines()]
                    held=[row for row in calls if row[1]=='turn/start' and any(i.get('text')=='hold' for i in row[2].get('input',[]))]
                    if held: break
                    if time.monotonic()>deadline: self.fail('held turn did not start')
                    time.sleep(.01)
                held_pid=held[-1][0]
                wire.close();client.close()
                deadline=time.monotonic()+15
                while True:
                    try: os.kill(held_pid,0)
                    except ProcessLookupError: break
                    if time.monotonic()>deadline: self.fail('disconnected worker still alive')
                    time.sleep(.01)

                # Disconnect cleanup is asynchronous; wait for the lease to be
                # released, retrying only explicit pre-acceptance busy rejections.
                deadline=time.monotonic()+15
                while True:
                    probe=socket.create_connection(('127.0.0.1',endpoint['port']))
                    probe_wire=probe.makefile('rwb')
                    probe_wire.write((json.dumps({'token':endpoint['token'],'statePath':state['statePath'],
                        'prompt':'hello','parts':False})+'\n').encode());probe_wire.flush()
                    first=json.loads(probe_wire.readline())
                    if first.get('type')=='error' and 'already has an active request' in first.get('message',''):
                        probe_wire.close();probe.close()
                        if time.monotonic()>deadline: self.fail('disconnected lease did not release')
                        time.sleep(.01);continue
                    events=[first]
                    for line in probe_wire:
                        event=json.loads(line);events.append(event)
                        if event.get('poolDone'): break
                    probe_wire.close();probe.close()
                    self.assertTrue(events[-1].get('reusable'),events)
                    break
                # Extension shutdown drains an accepted turn instead of cancelling it.
                with socket.create_connection(('127.0.0.1',endpoint['port'])) as client:
                    with client.makefile('rwb') as wire:
                        wire.write((json.dumps({'token':endpoint['token'],'statePath':state['statePath'],
                            'prompt':'hold','parts':False})+'\n').encode());wire.flush()
                        deadline=time.monotonic()+5
                        while True:
                            calls=[json.loads(line) for line in fake.with_suffix('.log').read_text().splitlines()]
                            started=any(row[0]!=held_pid and row[1]=='turn/start' and any(i.get('text')=='hold' for i in row[2].get('input',[])) for row in calls)
                            if started: break
                            if time.monotonic()>deadline: self.fail('drain turn did not start')
                            time.sleep(.01)
                        host.stdin.close()
                        fake.with_suffix('.release').write_text('continue')
                        events=[]
                        for line in wire:
                            event=json.loads(line);events.append(event)
                            if event.get('poolDone'): break
                        self.assertTrue(any(e.get('type')=='item.completed' for e in events),events)
            finally:
                host.stdin.close()
                host.wait(timeout=30)
                diagnostics=host.stderr.read()
                host.stdout.close();host.stderr.close()
            self.assertEqual(host.returncode,0,diagnostics)

    def test_pool_identity_ignores_run_bookkeeping_but_not_authority(self):
        state={'runtimeBinding':{'root':'one'},'agentId':'main'}
        session={'executionPolicy':{'approvalPolicy':'never'},'model':'one'}
        key=native.pool_identity(state,session)
        self.assertEqual(key,native.pool_identity(state,{**session,'lastRunId':'next','sessionId':'thread'}))
        self.assertNotEqual(key,native.pool_identity(state,{**session,'executionPolicy':{'approvalPolicy':'on-request'}}))
        self.assertNotEqual(key,native.pool_identity({**state,'agentId':'other'},session))
