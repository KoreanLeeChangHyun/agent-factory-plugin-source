"""Merge per-occurrence runtime lessons into signature records: preview by default, apply only on request."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/migrate_runtime_lessons.py'
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('lesson_migration_lessons', ROOT / 'scripts/lessons.py')
lessons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lessons)


def legacy(lessons_dir, key, run, event, code='command-exit-1', status='unresolved', cause='unknown'):
    source = f'agent:main-a/run:{run}/attempt:1/event:{event}'
    occurrence = {'id': f'runtime-{key}', 'category': 'error', 'title': code, 'language': 'en',
                  'occurrenceId': f'main-a:{run}:1:{event}:{code}', 'source': source, 'scope': 'runtime',
                  'symptom': code, 'cause': cause, 'solution': 'unresolved', 'verification': 'not checked',
                  'recordedAt': f'2026-09-2{len(event)}T00:00:00+00:00'}
    record = {'schemaVersion': 1, 'id': f'runtime-{key}', 'category': 'error', 'title': code, 'language': 'en',
              'scope': 'runtime', 'status': status, 'occurrences': [occurrence], 'applications': [],
              'candidates': [], 'publications': []}
    (lessons_dir / f'runtime-{key}.json').write_text(json.dumps(record))


def project(tmp_path):
    root, agents = tmp_path / 'project', tmp_path / 'agents'
    lessons_dir = root / 'docs/lessons-learned'
    lessons_dir.mkdir(parents=True)
    run = agents / 'main-a/runs/r1'
    run.mkdir(parents=True)
    (run / 'state.json').write_text(json.dumps({'role': 'main'}))  # Predates the provider field.
    items = [('e1', "/usr/bin/zsh -lc 'uv run pytest -q'", 1, 'FAILED'),
             ('e2', "/usr/bin/zsh -lc 'pytest tests/b.py'", 1, 'FAILED'),
             ('e3', "/usr/bin/zsh -lc 'rg -n missing src'", 1, ''),
             ('e4', "/usr/bin/zsh -lc 'uv run pytest -q'", 0, 'ok')]
    (run / 'events.jsonl').write_text(''.join(json.dumps({'type': 'item.completed', 'item': {
        'id': key, 'type': 'command_execution', 'command': command, 'exit_code': code, 'aggregated_output': output,
        'status': 'completed' if code == 0 else 'failed'}}) + '\n' for key, command, code, output in items))
    legacy(lessons_dir, 'a' * 24, 'r1', 'e1')
    legacy(lessons_dir, 'b' * 24, 'r1', 'e2')
    legacy(lessons_dir, 'c' * 24, 'r1', 'e3')
    legacy(lessons_dir, 'd' * 24, 'r1', 'e1x', status='resolved')
    legacy(lessons_dir, 'e' * 24, 'r1', 'e1y', cause='Agent diagnosis')
    legacy(lessons_dir, 'f' * 24, 'gone', 'e9')
    return root, agents, lessons_dir


def migrate(root, agents, *extra):
    result = subprocess.run([sys.executable, str(SCRIPT), '--project-root', str(root), '--agents-root', str(agents),
                             *extra], capture_output=True, text=True, timeout=60)
    return result.returncode, json.loads(result.stdout)


def snapshot(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def test_dry_run_reports_the_plan_and_changes_nothing(tmp_path):
    root, agents, lessons_dir = project(tmp_path)
    before = snapshot(lessons_dir)
    code, output = migrate(root, agents, '--list')
    assert code == 0 and output['mode'] == 'dry-run'
    assert output['merge'] == {'signatures': 1, 'newSignatureRecords': 1, 'existingSignatureRecords': 0,
                               'records': 2, 'occurrences': 2, 'recoveredOccurrences': 1}
    assert output['keep'] == {'resolved': 1, 'agent-written': 1, 'aggregate': 0, 'reviewed': 0}
    assert output['excluded'] == {'no-match': 1} and output['undetermined'] == {'run-missing': 1}
    assert output['signatures'][0]['title'] == 'command-exit-1 (test, codex main)'
    assert snapshot(lessons_dir) == before


def test_apply_requires_an_empty_backup_and_merges_in_one_pass(tmp_path):
    root, agents, lessons_dir = project(tmp_path)
    before = snapshot(lessons_dir)
    code, output = migrate(root, agents, '--apply')
    assert code == 1 and 'backup' in output['error'] and snapshot(lessons_dir) == before
    backup = tmp_path / 'backup'
    backup.mkdir()
    code, output = migrate(root, agents, '--apply', '--backup', str(backup))
    assert code == 0 and output['mode'] == 'apply'
    assert sorted(path.name for path in backup.iterdir()) == ['runtime-' + 'a' * 24 + '.json', 'runtime-' + 'b' * 24 + '.json']
    remaining = sorted(path.name for path in lessons_dir.iterdir())
    assert len(remaining) == 5 and not any(name.startswith(('runtime-aaa', 'runtime-bbb')) for name in remaining)
    record = next(r for r in lessons.records(root) if r['occurrences'][0].get('signature'))
    assert [o['legacyId'] for o in record['occurrences']] == ['runtime-' + 'a' * 24, 'runtime-' + 'b' * 24]
    assert [o.get('recoveredBy') for o in record['occurrences']] == ['e4', None]
    assert record['occurrences'][0]['source'] == 'agent:main-a/run:r1/attempt:1/event:e1'
    # A second pass finds nothing left to merge.
    code, output = migrate(root, agents)
    assert output['merge']['records'] == 0 and output['keep']['aggregate'] == 1
