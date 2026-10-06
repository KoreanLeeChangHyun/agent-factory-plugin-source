"""Canonical retrieval through managed attempts; no model or Agent launches."""
import argparse
import hashlib
import json
from pathlib import Path
import threading
from unittest import mock
import pytest
from execution import document_context as flow
from execution.prompts import PromptParts
from storage.errors import ContractError
from tasks import binding
from search_documents import read_document
from native_fixtures import runtime


def package(root, scope='plugin', kind='processed', body='needle', name='example'):
    path = root / ('docs/skills' if kind == 'specification' else 'docs/refined/analysis') / name
    path.mkdir(parents=True)
    source = path / 'SKILL.md'
    source.write_text(f'---\ndocument-type: {kind}\ncategory: analysis\ndomain: null\nname: {name}\nlanguage: en\nscope: {scope}\n---\n\n# Entry\n\n- {body}\n', encoding='utf-8')
    return source


def collect(root, **req):
    return flow.collect(root, root, req, run_id='run-test', task_id='T1')


def test_partial_and_explicit_cross_scope_preserve_source(tmp_path):
    source = package(tmp_path, scope='other')
    bundle, metrics = collect(tmp_path, query='needle absent', scope='plugin', allowPartial=True)
    assert not bundle['reads'] and metrics['searchMisses'] == 2
    bundle, metrics = collect(tmp_path, query='needle absent', scope='plugin', allowPartial=True, discoverScopes=True)
    assert [event['match'] for event in metrics['events'] if event['kind'] == 'search'] == ['all','any','all','any']
    assert metrics['searchMisses'] == 3
    assert bundle['reads'][0]['document']['metadata']['scope'] == 'other'
    assert bundle['candidates'][0]['discoveryOnly'] and bundle['reads'][0]['discoveryOnly']
    assert bundle['reads'][0]['text'] in source.read_text()
    assert metrics['modelInputTokens'] is None and metrics['cache'] is None


def test_required_entry_exception_and_reference_are_not_ranked_away(tmp_path):
    entry = package(tmp_path, body='MUST follow [rule](references/rule.md#exception).\n\n- [Optional](references/optional.md).')
    refs = entry.parent / 'references'; refs.mkdir()
    rule = refs / 'rule.md'
    rule.write_text('# Rule\n\n## Exception\n\n- Unless Human approves, preserve files.\n\n## Detail\n\n- unrelated.\n')
    (refs / 'optional.md').write_text('# Optional\n\n- extra.\n')
    selected = refs / 'selected.md'
    selected.write_text('# Source\n\n## Needed\n\n- needle exact.\n\n## Other\n\n- outside selection.\n')
    bundle, metrics = collect(tmp_path, query='nothing matches', required=[{'path':str(selected.relative_to(tmp_path)), 'anchor':'needed'}])
    paths = [read['path'] for read in bundle['reads']]
    assert str(rule.relative_to(tmp_path)) in paths
    assert str((refs / 'optional.md').relative_to(tmp_path)) not in paths
    assert bundle['reads'][0]['entryText'] == entry.read_text()
    assert any('preserve files' in read['text'] for read in bundle['reads'])
    assert any(gap['reason'] == 'reference-applicability-unassessed' for gap in bundle['unresolved'])
    assert bundle['dependencyCompleteness'] == 'unproven'
    assert metrics['snapshotReadBytes'] > metrics['reads'][0]['textBytes']
    assert metrics['catalogReadBytes'] is None


@pytest.mark.parametrize('mutation', ['body','dependency'])
def test_stale_revision_fails_closed_and_persists_evidence(tmp_path, mutation):
    entry = package(tmp_path)
    refdir = entry.parent / 'references'; refdir.mkdir()
    dep = refdir / 'detail.md'; dep.write_text('# Detail\n- original')
    relative = str(entry.relative_to(tmp_path))
    rev = read_document(tmp_path, relative)['revision']
    target = entry if mutation == 'body' else dep
    target.write_text(target.read_text() + '\n- changed')
    captured = []
    class Recorder:
        def __call__(self, bodies): pass
        def finish(self, metrics): captured.append(metrics)
    with pytest.raises(ContractError, match='Stale document revision'):
        flow.collect(tmp_path,tmp_path,{'required':[{'path':relative,'revision':rev}]},run_id='run-stale',observer=Recorder())
    assert captured[0]['events'][0]['kind'] == 'read-error'
    assert captured[0]['snapshotReadBytes'] > 0
    refreshed, _ = collect(tmp_path, required=[{'path':relative}])
    assert refreshed['reads'][0]['revision'] != rev


@pytest.mark.parametrize('target', ['references/missing.md','../../../outside.md','references/rule.md#absent'])
def test_required_dependency_failure_blocks(tmp_path, target):
    entry = package(tmp_path, body=f'MUST follow [rule]({target}).')
    if 'absent' in target:
        (entry.parent / 'references').mkdir()
        (entry.parent / 'references/rule.md').write_text('# Rule\n')
    with pytest.raises(ContractError):
        collect(tmp_path, required=[{'path':str(entry.relative_to(tmp_path))}])


def test_required_specification_cycle(tmp_path):
    entry = package(tmp_path, kind='specification', body='MUST read [self](SKILL.md).')
    bundle, metrics = collect(tmp_path, query='not found', required=[{'path':str(entry.relative_to(tmp_path))}])
    assert len(bundle['reads']) <= 2
    assert bundle['reads'][0]['document']['documentType'] == 'specification'
    assert bundle['reads'][0]['text'] == entry.read_text()
    assert metrics['runId'] == 'run-test' and metrics['taskId'] == 'T1'


def test_task_and_cli_bind_per_request(tmp_path):
    req = {'query':'needle','scope':'plugin','allowPartial':True}
    document = {'id':'workflow','title':'Work','tasks':[{'id':'T1','title':'Task','description':'bounded','completionCriteria':'done','documentContext':req}]}
    _, bound = binding.resolve(document,'T1','a'*64)
    assert bound['documentContext'] == req
    context = tmp_path / 'requirements.json'; context.write_text(json.dumps(req))
    args = runtime.parse_args(['send','--agent','main','--message','bounded','--document-context-file',str(context)])
    assert runtime.requested_execution(args)['documentContext'] == req
    assert 'documentContext' not in runtime.requested_execution(argparse.Namespace())
    with pytest.raises(ContractError): flow.validate({'required':[{'path':'../outside.md'}]})


def test_managed_attempt_prepares_context_without_model_launch(tmp_path):
    source = package(tmp_path, body='needle exact')
    request = b'Authorized bounded task; do not delete files.'
    policy = {'schemaVersion':1,'sandboxPolicy':{'type':'danger-full-access','network_access':True},'approvalPolicy':'never'}
    session = {'role':'work','maxAttempts':1,'projectRoot':str(tmp_path),'executionPolicy':policy,'provider':'codex'}
    state = runtime.create_run(project_root=tmp_path,agent_id='work-doc',actor='main',request=request,session=session,
        execution_options={'documentContext':{'query':'needle exact','scope':'plugin'}},task_binding={'taskId':'T1'})
    adapter = mock.Mock(); adapter.check.return_value = {'passed':True}; adapter.uses_prompt_parts.return_value = True
    adapter.build_command.return_value = ['fake-provider']
    captured = []
    encoded = []
    def encode(parts):
        encoded.append(parts.dynamic)
        return json.dumps({'version':1,'fixed':parts.fixed,'dynamic':parts.dynamic})
    def capture(*args, **kwargs):
        current = runtime.safe_read_json(Path(state['statePath']))
        assert current['documentContext']['reads'][0]['revision']
        captured.append(current)
        raise OSError('fixture stops before provider launch')
    with mock.patch.object(runtime.adapters,'for_session',return_value=adapter), mock.patch.object(runtime,'spawn_contained_process',side_effect=capture), mock.patch.object(flow,'prepare',wraps=flow.prepare) as prepare, mock.patch.object(PromptParts,'encode',autospec=True,side_effect=encode):
        with pytest.raises(runtime.AttemptFailure, match='codex exec could not start'):
            runtime.run_codex_attempt(project_root=tmp_path,session=session,state=state,attempt=1,heartbeat=mock.Mock(),cancel_event=threading.Event(),expected_agent_id='work-doc',expected_run_id=state['runId'])
        assert prepare.call_count == 1
    assert 'needle exact' in encoded[0]
    assert captured[0]['requestHash'] == hashlib.sha256(request).hexdigest()
    assert captured[0]['executionPolicy'] == policy
    assert runtime.public_state(captured[0])['documentContext']['taskId'] == 'T1'
    parts = PromptParts('fixed',request.decode())
    enriched = flow.prepare(runtime,tmp_path,tmp_path,state,2,parts)
    assert enriched.fixed == 'fixed' and enriched.dynamic.startswith(request.decode())
    assert 'needle exact' in enriched.dynamic and str(source.relative_to(tmp_path)) in enriched.dynamic
    assert flow.prepare(runtime,tmp_path,tmp_path,{'executionOptions':{}},1,parts) is parts
    persisted = runtime.safe_read_json(Path(state['statePath']))
    assert set(persisted['documentContextAttempts']) == {'1','2'}
    assert persisted['documentContext']['deliveredContextBytes'] > 0
    assert persisted['documentContext']['modelInputTokens'] is None


def test_search_failure_is_measured_and_stops_preparation(tmp_path):
    source = package(tmp_path)
    source.write_text('---\ndocument-type: processed\n---\n# Invalid')
    metrics = []
    class Recorder:
        def __call__(self, bodies): pass
        def finish(self, value): metrics.append(value)
    with pytest.raises(ContractError) as raised:
        flow.collect(tmp_path,tmp_path,{'query':'needle'},run_id='failure',observer=Recorder())
    assert raised.value.code == 'document_context_search_failed'
    assert metrics[0]['searchFailures'] == 1
    assert metrics[0]['events'][0]['kind'] == 'search-error'


def test_no_partial_fallback_without_explicit_request(tmp_path):
    package(tmp_path)
    bundle, metrics = collect(tmp_path,query='needle absent')
    assert not bundle['reads'] and metrics['searches'] == 1 and metrics['requeries'] == 0


def test_submit_captures_context_in_real_run_without_worker_start(tmp_path):
    req = {'query':'needle','scope':'plugin','allowPartial':True}
    file = tmp_path / 'context.json'; file.write_text(json.dumps(req))
    args = runtime.parse_args(['submit','--project-root',str(tmp_path),'--agent','main-doc','--role','main',
        '--message','Authorized bounded task','--codex','/bin/true','--no-goal-mode',
        '--document-context-file',str(file)])
    with mock.patch.object(runtime,'spawn_worker',return_value=123), mock.patch.object(runtime,'emit'):
        runtime.submit(args,True)
    state = next(runtime.iter_run_states(tmp_path,'main-doc'))
    assert state['executionOptions']['documentContext'] == req
    assert state['dispatchTuple']['executionOptions']['documentContext'] == req
    assert 'documentContext' not in runtime.load_session(tmp_path,'main-doc')
    file.write_text(json.dumps({'query':'changed'}))
    assert runtime.safe_read_json(Path(state['statePath']))['executionOptions']['documentContext'] == req


def test_lesson_state_and_cross_scope_discovery_survive_runtime_flow(tmp_path):
    from lessons import operate
    operate(tmp_path,'record',dict(id='lesson-flow',category='error',title='needle lesson',language='en',
        occurrenceId='test-flow',source='test',scope='other',symptom='needle',cause='unknown',solution='unresolved',verification='not checked'))
    bundle, _ = collect(tmp_path,query='needle',scope='plugin',discoverScopes=True)
    hit = bundle['candidates'][0]
    assert hit['scope'] == 'other' and hit['status'] == 'unresolved' and hit['discoveryOnly']
    source = bundle['reads'][0]
    assert source['document']['scope'] == 'other' and source['document']['status'] == 'unresolved'
    operate(tmp_path,'resolve',dict(id='lesson-flow',cause='known',solution='fixed',verification='own check',evidence='test'))
    with pytest.raises(ContractError,match='Stale'):
        collect(tmp_path,required=[{'path':source['path'],'revision':source['revision']}])
    fresh, _ = collect(tmp_path,query='needle',scope='plugin',discoverScopes=True)
    assert fresh['reads'][0]['document']['status'] == 'resolved'
    assert fresh['reads'][0]['revision'] != source['revision']


def test_managed_flow_reads_all_search_pages(tmp_path):
    for index in range(21):
        package(tmp_path,name=f'example-{index}',body='needle pagination')
    bundle, metrics = collect(tmp_path,query='needle pagination',scope='plugin')
    assert len(bundle['candidates']) == 21 and len(bundle['reads']) == 21
    assert metrics['searches'] == 2 and metrics['requeries'] == 0
    assert [event['offset'] for event in metrics['events']] == [0,20]
