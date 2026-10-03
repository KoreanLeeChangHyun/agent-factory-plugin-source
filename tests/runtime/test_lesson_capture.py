"""Real subprocess capture, redaction, pending failures and duplicate delivery."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import subprocess

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
    records = list((tmp_path / 'docs/lessons-learned').glob('*.json'))
    assert len(records) == 1
    text = records[0].read_text()
    assert 'DO_NOT_SAVE' not in text
    assert len(json.loads(text)['occurrences']) == 1
    assert capture.audit(state) == []
    event['item']['exit_code'] = 0
    assert capture.observe(tmp_path, state, event, 1) is None


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
    assert len(list((tmp_path / 'docs/lessons-learned').glob('*.json'))) == 2


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

    with patch.object(capture.subprocess, 'run', side_effect=flaky), patch.object(capture.time, 'sleep') as sleep:
        capture.replay(tmp_path, state)
    assert len(calls) == 2 and sleep.call_count == 1
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
