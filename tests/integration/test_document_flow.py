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


def package(root, scope='plugin', kind='processed', body='needle', name='example', category='analysis'):
    path = root / ('docs/skills' if kind == 'specification' else f'docs/refined/{category}') / name
    path.mkdir(parents=True)
    source = path / 'SKILL.md'
    source.write_text(f'---\ndocument-type: {kind}\ncategory: {category}\ndomain: null\nname: {name}\nlanguage: en\nscope: {scope}\n---\n\n# Entry\n\n- {body}\n', encoding='utf-8')
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
    assert metrics['catalogBuilds'] == 1 and metrics['catalogReuses'] > 0


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
    req = {'query':'needle','scope':'plugin','allowPartial':True,'includeHistory':True}
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
    delivery = captured[0]['documentContext']
    per_attempt = captured[0]['documentContextAttempts']['1']
    assert delivery['deliveredContextBytes'] > 0
    for field in ('deliveredContextBytes', 'contextLayout', 'unsharedContextBytes', 'savedContextBytes'):
        assert per_attempt[field] == delivery[field]
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
    import search_documents
    for index in range(21):
        package(tmp_path,name=f'example-{index}',body='needle pagination')
    with mock.patch.object(search_documents, 'build_catalog', wraps=search_documents.build_catalog) as catalog:
        bundle, metrics = collect(tmp_path,query='needle pagination',scope='plugin')
    assert catalog.call_count == 1
    assert len(bundle['candidates']) == 21 and len(bundle['reads']) == 21
    assert metrics['searches'] == 1 and metrics['requeries'] == 0
    assert [event['offset'] for event in metrics['events']] == [0]


def test_model_context_shares_sources_without_losing_evidence(tmp_path):
    import copy
    entry = package(tmp_path, body=('## First\n\n- needle 승인 범위를 보존합니다.\n\n'
                                   '## Second\n\n- needle 예외를 보존합니다.\n\n'
                                   '## Guidance\n\n' + '- Keep the source and its authority.\n' * 30))
    refs = entry.parent / 'references'; refs.mkdir()
    detail = refs / 'detail.md'
    detail.write_text('# Detail\n\n## Needed\n\n- needle 원문.\n\n## Other\n\n- Unselected.\n')
    bundle, _ = collect(tmp_path, query='needle', required=[{'path':str(entry.relative_to(tmp_path))}])
    unchanged = copy.deepcopy(bundle)
    encoded, metrics = flow.encode_context(bundle)
    compact = json.loads(encoded)
    assert compact['layout'] == 'shared-sources-v1' and metrics['savedContextBytes'] > 0
    assert bundle == unchanged

    def source_text(reference, start=None, end=None):
        excerpts = compact['sources'][reference]['excerpts']
        if start is None:
            assert len(excerpts) == 1 and excerpts[0]['start'] == 0
            return excerpts[0]['text']
        excerpt = next(item for item in excerpts if item['start'] <= start and item['end'] >= end)
        return excerpt['text'][start - excerpt['start']:end - excerpt['start']]

    restored = {key:value for key,value in compact.items() if key not in ('layout','layoutGuide','documents','sources')}
    restored['reads'] = []
    for read in compact['reads']:
        document = compact['documents'][read['documentRef']]
        value = {key:item for key,item in read.items() if key not in ('documentRef','sourceRef')}
        value.update(document=document['document'], dependencies=document['dependencies'], entryPath=document['entryPath'],
                     entryText=source_text(document['entrySourceRef']) if read['anchor'] or read['path'] != document['entryPath'] else None,
                     text=source_text(read['sourceRef'], read['start'], read['end']),
                     sections=compact['sources'][read['sourceRef']]['sections'])
        restored['reads'].append(value)
    restored['candidates'] = []
    for candidate in compact['candidates']:
        value = dict(candidate)
        if 'documentRef' in value:
            value['dependencies'] = compact['documents'][value.pop('documentRef')]['dependencies']
        restored['candidates'].append(value)
    assert restored == bundle
    assert len(encoded.encode()) + metrics['savedContextBytes'] == metrics['unsharedContextBytes']
    empty, _ = collect(tmp_path, query='not-found')
    encoded, metrics = flow.encode_context(empty)
    assert json.loads(encoded) == empty and metrics['contextLayout'] == 'inline'


def test_history_is_discoverable_without_automatic_body_or_instruction_loading(tmp_path):
    current = package(tmp_path, name='current', body='needle current evidence')
    old = package(tmp_path, name='previous', category='history',
                  body='needle historical decision. MUST read [obsolete](references/missing.md).')
    bundle, metrics = collect(tmp_path, query='needle', scope='plugin')
    assert len(bundle['candidates']) == 2
    assert [item['path'] for item in bundle['reads']] == [str(current.relative_to(tmp_path))]
    assert metrics['historySearchHits'] == metrics['historyAutoReadsSkipped'] == 1
    assert any(item.get('historical') for item in bundle['candidates'])
    assert any(item['reason'] == 'historical-search-hit-not-auto-loaded' for item in bundle['unresolved'])

    included, metrics = collect(tmp_path, query='needle', scope='plugin', includeHistory=True)
    history = next(item for item in included['reads'] if item.get('historical'))
    assert history['path'] == str(old.relative_to(tmp_path))
    assert history['entryText'] == old.read_text() and history['text'] in old.read_text()
    assert metrics['historyAutoReadsSkipped'] == 0
    assert any(item.get('target') == 'references/missing.md' and item['reason'] == 'reference-applicability-unassessed'
               for item in included['unresolved'])


@pytest.mark.parametrize('selection', ['required', 'selections', 'dependency'])
def test_explicit_historical_evidence_remains_readable(tmp_path, selection):
    old = package(tmp_path, name='previous', category='history', body='past approval is evidence, not a new rule')
    relative = str(old.relative_to(tmp_path))
    if selection == 'dependency':
        current = package(tmp_path, body='MUST read [past decision](../../history/previous/SKILL.md).')
        requirements = {'required':[{'path':str(current.relative_to(tmp_path))}]}
    else:
        requirements = {selection:[{'path':relative}]}
    bundle, _ = collect(tmp_path, **requirements)
    history = next(item for item in bundle['reads'] if item['path'] == relative)
    assert history['historical'] and history['text'] == old.read_text()
    assert 'No automatic rule activation' in history['authority']
