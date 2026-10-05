"""Real subprocess capture, redaction, pending failures and duplicate delivery."""
import importlib.util
import json
from pathlib import Path
import shutil
from unittest.mock import patch
import subprocess
import sys
import runtime_test_home  # noqa: F401
from storage import lessons as body_store


def read_record(path, root=None):
    return body_store.read(root or path.parents[3], path, path.parents[3])

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('lesson_capture_test_module', ROOT / 'runtime/execution/lessons.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def test_capture_nonzero_and_ignore_success(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r1', 'agentId': 'a1'}
    event = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'item1', 'exit_code': 1, 'command': 'secret=DO_NOT_SAVE'}}
    assert capture.observe(tmp_path, state, event, 1)['saved']
    assert capture.observe(tmp_path, state, event, 1)['saved']
    records = list((tmp_path / 'docs/lessons-learned').glob('errors/*.md'))
    assert len(records) == 1
    text = records[0].read_text()
    assert 'DO_NOT_SAVE' not in text
    assert len(read_record(records[0])['occurrences']) == 1
    assert capture.audit(state) == []
    success = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'other', 'exit_code': 0, 'command': 'ls'}}
    assert capture.observe(tmp_path, state, success, 1) is None


def test_pending_on_storage_failure(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r2', 'agentId': 'a1'}
    with patch.object(capture.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
        result = capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'launch_failed'})
    assert result['saved'] is False
    assert len(capture.audit(state)) == 1
    assert capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'launch_failed'})['saved']
    assert capture.audit(state) == []


def test_permanent_storage_failure_stops_same_run_retries_and_preserves_occurrences(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'blocked', 'agentId': 'a1'}
    diagnostic = {'error': 'layout mismatch', 'code': 'lesson_storage_incompatible', 'retryable': False}
    failure = subprocess.CompletedProcess([], 1, json.dumps(diagnostic), '')
    with patch.object(capture.subprocess, 'run', return_value=failure) as cli:
        first = capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'first'})
        second = capture.observe(tmp_path, dict(state), {'type': 'runtime.failure', 'code': 'second'})
        capture.replay(tmp_path, dict(state))
    assert cli.call_count == 1
    assert first['diagnostic'] == diagnostic and second['reason'] == 'lesson-storage-blocked'
    assert len(capture.audit(state)) == 2
    assert not (tmp_path / 'docs').exists()
    # A new run is allowed to retry after repair; no failed tool is re-executed.
    next_run = tmp_path / 'next-run'
    next_run.mkdir()
    next_state = {**state, 'statePath': str(next_run / 'state.json'), 'runId': 'repaired'}
    for pending in capture.captures(state):
        assert capture.record(tmp_path, next_state, pending)['saved']
    assert capture.audit(state) == []


def test_pending_sweep_stops_at_first_permanent_storage_failure(tmp_path):
    base = tmp_path / 'agents/a/runs'
    old = base / 'old'
    current = base / 'current'
    old.mkdir(parents=True)
    current.mkdir()
    state = {'statePath': str(old / 'state.json'), 'runId': 'old', 'agentId': 'a',
             'executionPolicy': {'sandboxPolicy': {'type': 'read-only'}}}
    for code in ('first', 'second'):
        capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': code})
    writer = {'statePath': str(current / 'state.json'), 'runId': 'current', 'agentId': 'a'}
    failure = subprocess.CompletedProcess([], 1, json.dumps({'code': 'lesson_storage_incompatible',
                                         'retryable': False, 'error': 'incompatible'}), '')
    with patch.object(capture.subprocess, 'run', return_value=failure) as cli:
        assert capture.apply_pending(tmp_path, writer) == 0
        assert capture.apply_pending(tmp_path, dict(writer)) == 0
    assert cli.call_count == 1 and len(capture.audit(state)) == 2


def test_replay_and_unidentified_errors(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r3', 'agentId': 'a1'}
    event = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'status': 'failed'}}
    with patch.object(capture.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
        capture.observe(tmp_path, state, event)
        capture.observe(tmp_path, state, event)
    assert len(capture.audit(state)) == 2
    capture.replay(tmp_path, state)
    assert capture.audit(state) == []
    records = list((tmp_path / 'docs/lessons-learned').glob('errors/*.md'))
    assert len(records) == 1  # Both occurrences share the tool-error signature.
    assert len(read_record(records[0])['occurrences']) == 2


def test_read_only_never_writes_project(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r4', 'agentId': 'a1',
             'executionPolicy': {'sandboxPolicy': {'type': 'read-only'}}}
    result = capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'failed'})
    assert result['reason'] == 'project-read-only'
    capture.replay(tmp_path, state)
    assert not (tmp_path / 'docs').exists()
    assert len(capture.audit(state)) == 1


def test_hook_rejection_persists_without_provider_events_and_explore_never_writes(tmp_path):
    import os
    from tasks import orchestrator_guard as guard
    root, run = tmp_path / 'project', tmp_path / 'run'
    root.mkdir(); run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r-hook', 'agentId': 'a-hook', 'provider': 'codex',
             'role': 'work', 'workProfile': 'explore', 'executionPolicy': {'sandboxPolicy': {'type': 'danger-full-access'}}}
    (run / 'state.json').write_text(json.dumps(state))
    config = json.loads(guard.profile_environment(state, {'projectRoot': str(root)})[guard.ENV])
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'pwd\nnl -ba SECRET_PATH'}}
    result = subprocess.run([sys.executable, '-B', str(Path(guard.__file__))], input=json.dumps(event),
                            env={**os.environ, guard.ENV: json.dumps(config)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    reason = json.loads(result.stdout)['hookSpecificOutput']['permissionDecisionReason']
    assert 'guard_command_format' in reason
    assert len(capture.audit(state)) == 1
    pending = Path(capture.audit(state)[0])
    assert 'SECRET_PATH' not in pending.read_text()
    assert 'SECRET_PATH' not in reason
    capture.replay(root, state)
    assert len(capture.audit(state)) == 1
    assert not (root / 'docs').exists()
    # A write-authorized owner can later persist this exact occurrence idempotently.
    capture.replay(root, {**state, 'workProfile': 'scribe'})
    assert capture.audit(state) == []
    records = list((root / 'docs/lessons-learned/errors').glob('*.md'))
    assert len(records) == 1
    capture.replay(root, {**state, 'workProfile': 'scribe'})
    assert len(list((root / 'docs/lessons-learned/errors').glob('*.md'))) == 1


def test_agent_authored_inputs_are_not_pending_captures(tmp_path):
    # Incident: runs failed lesson_recording_incomplete although every runtime capture was
    # saved, because the Agent kept its own resolve/audit inputs in lesson-capture/.
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r5', 'agentId': 'a1'}
    event = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'item1', 'exit_code': 1}}
    assert capture.observe(tmp_path, state, event, 1)['saved']
    directory = run / 'lesson-capture'
    (directory / 'resolve-item1.json').write_text(json.dumps({'id': 'runtime-x', 'cause': 'c'}))
    (directory / 'audit.json').write_text(json.dumps({'occurrenceIds': []}))
    assert capture.audit(state) == []
    with patch.object(capture.subprocess, 'run') as run_cli:
        capture.replay(tmp_path, state)
    run_cli.assert_not_called()


def test_unsaved_runtime_capture_stays_pending_beside_agent_files(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r6', 'agentId': 'a1'}
    with patch.object(capture.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
        capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'launch_failed'})
    (run / 'lesson-capture' / 'notes.json').write_text('{}')
    pending = capture.audit(state)
    assert len(pending) == 1 and capture.CAPTURE_NAME.fullmatch(Path(pending[0]).name)


def test_replay_retries_a_transient_recording_failure(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r7', 'agentId': 'a1'}
    failure = subprocess.CompletedProcess([], 1, '', 'busy')
    with patch.object(capture.subprocess, 'run', return_value=failure):
        capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'launch_failed'})
    real_run = subprocess.run
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        return failure if len(calls) == 1 else real_run(*args, **kwargs)

    # Replace only this module's `time`: the real subprocess.run above polls the child with
    # the global time.sleep, so patching that one counts a timing-dependent number of polls.
    with patch.object(capture.subprocess, 'run', side_effect=flaky), patch.object(capture, 'time') as clock:
        capture.replay(tmp_path, state)
    assert len(calls) == 2
    clock.sleep.assert_called_once_with(capture.REPLAY_BACKOFF_SECONDS)
    assert capture.audit(state) == []


def test_replay_stops_after_bounded_attempts(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r8', 'agentId': 'a1'}
    failure = subprocess.CompletedProcess([], 1, '', 'failure')
    with patch.object(capture.subprocess, 'run', return_value=failure) as run_cli, patch.object(capture.time, 'sleep'):
        capture.observe(tmp_path, state, {'type': 'runtime.failure', 'code': 'launch_failed'})
        capture.replay(tmp_path, state)
    assert run_cli.call_count == 1 + capture.REPLAY_ATTEMPTS
    assert len(capture.audit(state)) == 1


def test_isolated_run_records_into_its_work_unit_and_sweeps_wait_for_the_unit(tmp_path):
    project, unit = tmp_path / 'project', tmp_path / 'unit'
    project.mkdir()
    unit.mkdir()
    runs = tmp_path / 'agents/a1/runs'
    workspace = {'id': 'w1', 'mode': 'code', 'repositories': [{'repositoryRoot': str(project), 'path': str(unit)}]}
    isolated = {'statePath': str(runs / 'r5/state.json'), 'runId': 'r5', 'agentId': 'a1', 'taskWorkspace': workspace}
    writer = {'statePath': str(runs / 'r6/state.json'), 'runId': 'r6', 'agentId': 'a1'}
    for state in (isolated, writer):
        Path(state['statePath']).parent.mkdir(parents=True)
        Path(state['statePath']).write_text(json.dumps(state))
    assert capture.record_root(project, {'taskWorkspace': {'mode': 'shared'}}) == project
    nested = {'mode': 'code', 'repositories': [{'repositoryRoot': str(project / 'plugin'), 'path': str(unit)}]}
    assert capture.record_root(project, {'taskWorkspace': nested}) == project
    event = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'item5', 'exit_code': 1}}
    saved = capture.observe(project, isolated, event)
    assert saved['saved']
    assert not (project / 'docs').exists()  # The source checkout the task merges into stays clean.
    assert (unit / saved['path']).is_file()
    assert capture.recorded_paths([isolated, writer], 'w1') == {saved['path']}
    assert capture.recorded_paths([isolated], 'another') == set()
    failure = subprocess.CompletedProcess([], 1, '', 'failure')
    with patch.object(capture.subprocess, 'run', return_value=failure):
        capture.observe(project, isolated, {**event, 'item': {**event['item'], 'id': 'item6'}})
        capture.observe(project, isolated, {**event, 'item': {**event['item'], 'id': 'item7'}})
    capture.replay(project, isolated)
    assert capture.audit(isolated) == []
    records = list((unit / 'docs/lessons-learned').glob('errors/*.md'))
    assert len(records) == 1 and len(read_record(records[0], project)['occurrences']) == 3
    with patch.object(capture.subprocess, 'run', return_value=failure):
        capture.observe(project, isolated, {**event, 'item': {**event['item'], 'id': 'item8'}})
    # Another run's sweep leaves the Unit's capture pending while the Unit exists.
    assert capture.apply_pending(project, writer) == 0
    assert not (project / 'docs').exists()
    assert len(capture.audit(isolated)) == 1
    shutil.copytree(unit / 'docs', project / 'docs')
    shutil.rmtree(unit)  # The merged Unit was cleaned up.
    assert capture.apply_pending(project, writer) == 1
    assert len(list((project / 'docs/lessons-learned').glob('errors/*.md'))) == 1


def failed(identifier, command, exit_code=1, output='boom'):
    return {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': identifier, 'command': command,
                                               'exit_code': exit_code, 'aggregated_output': output}}


def test_same_signature_accumulates_in_one_record_across_runs(tmp_path):
    lessons = tmp_path / 'docs/lessons-learned'
    for run in ('r1', 'r2', 'r3'):
        (tmp_path / run).mkdir()
        state = {'statePath': str(tmp_path / run / 'state.json'), 'runId': run, 'agentId': 'work-a',
                 'provider': 'codex', 'role': 'work'}
        for item, command in (('i1', "/usr/bin/zsh -lc 'uv run pytest -q tests/a.py'"),
                              ('i2', "/usr/bin/zsh -lc 'python3 -m pytest tests/b.py -k secret_TOKEN'")):
            assert capture.observe(tmp_path, state, failed(item, command), 0)['saved']
    records = list(lessons.glob('errors/*.md'))
    assert len(records) == 1
    record = read_record(records[0])
    assert record['id'] == capture.signature_id({'provider': 'codex', 'role': 'work', 'kind': 'test', 'code': 'command-exit-1'})
    assert record['title'] == 'command-exit-1 (test, codex work)'
    assert len(record['occurrences']) == 6 and record['status'] == 'unresolved'
    assert 'secret_TOKEN' not in records[0].read_text() and 'pytest' not in records[0].read_text()
    state['role'] = 'main'
    capture.observe(tmp_path, state, failed('i3', "zsh -lc 'pytest'"), 0)
    capture.observe(tmp_path, state, failed('i4', "zsh -lc 'pytest'", exit_code=2), 0)
    assert len(list(lessons.glob('errors/*.md'))) == 3  # Role and exit code split signatures.


def test_command_kinds():
    kinds = {
        "/usr/bin/zsh -lc 'node tests/browser/chat-rendering.cjs --task-flow-only'": 'test',
        "/usr/bin/zsh -lc 'npm test'": 'test',
        "bash -lc 'python3 /p/scripts/lessons.py --project-root /w record --input x.json'": 'lessons-cli',
        "/usr/bin/zsh -lc 'uv run ruff check .'": 'build',
        "/usr/bin/zsh -lc 'cd x && rg -n foo src'": 'search',
        "/usr/bin/zsh -lc 'git -C extension diff --check'": 'git',
        "/usr/bin/zsh -lc \"python3 - <<'PY'\nprint(1)\nPY\"": 'script',
        "/usr/bin/zsh -lc 'sed -n 1,5p a.py; cat b.py'": 'read',
        "/usr/bin/zsh -lc 'FOO=1 mkdir -p out'": 'other',
        "cd /w; ls; grep -rIl x . 2>/dev/null | head -20": 'read',
    }
    assert {command: capture.command_kind(command) for command in kinds} == kinds


def test_grep_and_rg_without_matches_are_not_recorded(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r1', 'agentId': 'a1', 'provider': 'codex', 'role': 'work'}
    assert capture.observe(tmp_path, state, failed('i1', "/usr/bin/zsh -lc 'rg -n missing src'", output=''), 0) is None
    assert capture.observe(tmp_path, state, failed('i2', "zsh -lc 'cd src && grep -r missing .'", output='\n'), 0) is None
    claude = failed('i3', 'grep -r missing .', output=json.dumps('Exit code 1'))
    assert capture.observe(tmp_path, {**state, 'provider': 'claude'}, claude, 0) is None
    assert not (tmp_path / 'docs').exists() and not (run / 'lesson-capture').exists()
    # Output, a different exit code or a later pipeline command make it an ordinary failure.
    assert capture.observe(tmp_path, state, failed('i4', "zsh -lc 'rg -n x src'", output='rg: src: No such file'), 0)['saved']
    assert capture.observe(tmp_path, state, failed('i5', "zsh -lc 'rg -n x src'", exit_code=2, output=''), 0)['saved']
    assert capture.observe(tmp_path, state, failed('i6', "zsh -lc 'rg -n x src | wc -l'", output=''), 0)['saved']


def test_later_success_of_the_same_command_marks_recovered(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r1', 'agentId': 'a1', 'provider': 'codex', 'role': 'work'}
    red = "/usr/bin/zsh -lc 'uv run pytest -q tests/test_new.py'"
    capture.observe(tmp_path, state, failed('i1', red), 0)
    capture.observe(tmp_path, state, failed('i2', "zsh -lc 'pytest tests/other.py'"), 0)
    green = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'i3', 'command': red, 'exit_code': 0}}
    assert capture.observe(tmp_path, state, green, 0)['saved']
    occurrences = read_record(next((tmp_path / 'docs/lessons-learned').glob('errors/*.md')))['occurrences']
    assert [o.get('recovered') for o in occurrences] == [True, None]
    assert occurrences[0]['recoveredBy'] == 'i3' and 'recoveredAt' in occurrences[0]
    assert capture.observe(tmp_path, state, {**green, 'item': {**green['item'], 'id': 'i4'}}, 0) is None
    assert capture.audit(state) == []
    # Another run's success does not recover this run's failure.
    other = tmp_path / 'other'
    other.mkdir()
    assert capture.observe(tmp_path, {**state, 'statePath': str(other / 'state.json'), 'runId': 'r2'},
                           {**green, 'item': {**green['item'], 'command': "zsh -lc 'pytest tests/other.py'"}}, 0) is None


def test_recovery_of_a_read_only_run_stays_pending(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    state = {'statePath': str(run / 'state.json'), 'runId': 'r1', 'agentId': 'a1',
             'executionPolicy': {'sandboxPolicy': {'type': 'read-only'}}}
    capture.observe(tmp_path, state, failed('i1', 'make'), 0)
    capture.observe(tmp_path, state, {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'i2',
                                                                         'command': 'make', 'exit_code': 0}}, 0)
    pending = json.loads(Path(capture.audit(state)[0]).read_text())
    assert pending['recovered'] is True and not (tmp_path / 'docs').exists()


def test_claude_and_antigravity_command_failures_share_the_signature_scheme(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    base = {'statePath': str(run / 'state.json'), 'runId': 'r1', 'agentId': 'a1', 'role': 'work'}
    claude = {'type': 'item.completed', 'item': {'id': 't1', 'type': 'command_execution', 'command': 'pytest -q',
                                                 'status': 'failed', 'error': 'Exit code 1', 'exit_code': 1,
                                                 'aggregated_output': json.dumps('Exit code 1\nFAILED')}}
    unknown = {'type': 'item.completed', 'item': {'id': 't2', 'type': 'command_execution', 'command': 'ls /missing',
                                                  'status': 'failed', 'error': 'Permission denied'}}
    antigravity = {'type': 'item.completed', 'item': {'id': 's:1', 'type': 'command_execution', 'command': 'touch b.txt',
                                                      'status': 'failed', 'error': 'denied', 'aggregated_output': ''}}
    assert capture.classify(claude, {**base, 'provider': 'claude'})['code'] == 'command-exit-1'
    assert capture.classify(unknown, {**base, 'provider': 'claude'}) == {
        'provider': 'claude', 'role': 'work', 'kind': 'read', 'code': 'command-failed'}
    assert capture.classify(antigravity, {**base, 'provider': 'antigravity'})['code'] == 'command-failed'
    for event, provider in ((claude, 'claude'), (unknown, 'claude'), (antigravity, 'antigravity')):
        assert capture.observe(tmp_path, {**base, 'provider': provider}, event, 0)['saved']
    titles = sorted(read_record(p)['title'] for p in (tmp_path / 'docs/lessons-learned').glob('errors/*.md'))
    assert titles == ['command-exit-1 (test, claude work)', 'command-failed (other, antigravity work)',
                      'command-failed (read, claude work)']
    # A completed command without an exit code (how both report success) recovers the failure.
    done = {'type': 'item.completed', 'item': {'id': 't3', 'type': 'command_execution', 'command': 'pytest -q',
                                               'status': 'completed'}}
    assert capture.observe(tmp_path, {**base, 'provider': 'claude'}, done, 0)['saved']


def test_code_unit_without_document_owner_keeps_capture_pending(tmp_path):
    project, unit = tmp_path / 'project', tmp_path / 'plugin-unit'
    project.mkdir()
    unit.mkdir()
    run = tmp_path / 'run'
    run.mkdir()
    state = dict(statePath=str(run / 'state.json'), runId='r-unbound-docs', agentId='work-a',
                 taskWorkspace={'mode': 'code', 'repositories': [{'repositoryRoot': str(project / 'plugin'), 'path': str(unit)}]})
    saved = capture.observe(project, state, failed('i1', 'pytest'), 0)
    assert saved['saved'] is False and saved['reason'] == 'document-workspace-unavailable'
    capture.replay(project, state)
    assert len(capture.audit(state)) == 1
    assert not (project / 'docs').exists() and not (unit / 'docs').exists()


def test_document_repository_unit_uses_project_identity_and_physical_docs(tmp_path):
    from storage import paths
    project, workspace = tmp_path / 'project', tmp_path / 'workspace'
    (project / 'docs').mkdir(parents=True)
    (workspace / 'docs').mkdir(parents=True)
    run = tmp_path / 'run'
    run.mkdir()
    state = dict(statePath=str(run / 'state.json'), runId='r-docs', agentId='work-a', language='ko',
                 taskWorkspace={'id': 'docs-unit', 'mode': 'code',
                                'repositories': [{'repositoryRoot': str(project / 'docs'), 'path': str(workspace / 'docs')}]})
    assert capture.record_root(project, state) == workspace
    saved = capture.observe(project, state, failed('i1', 'pytest'), 0)
    assert saved['saved'] is True
    body = workspace / saved['path']
    assert body.is_file() and read_record(body, project)['language'] == 'ko'
    assert list((project / 'docs').iterdir()) == []
    assert capture.recorded_paths([state], 'docs-unit') == {saved['path']}
    assert Path(saved['metadataPath']).parent == Path(paths.resolve(project)['runtimeRoot']) / 'lessons-learned'
    assert paths.resolve(workspace)['registered'] is False
