"""Exercise persisted lesson lifecycle with real document synchronization."""
import importlib.util
from pathlib import Path
import sys

import pytest
import runtime_test_home  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('lesson_lifecycle', SCRIPTS / 'lessons.py')
lessons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lessons)


def seed(root, category='error'):
    value = dict(id='demo', category=category, title='사례', language='ko', occurrenceId='run-1',
                 source='run:1', scope='project-a', symptom='failure', cause='미확인', solution='미해결',
                 verification='미검증', humanJudgment='간단히', humanReason='미확인', aiJudgment='자세히',
                 aiReason='배경 설명 필요', difference='길이', reflection='선호 차이 추정', outcome='간단히 수정')
    lessons.operate(root, 'record', value)
    return value


def test_inline_queries_create_no_input_file_or_write_lock(tmp_path, monkeypatch, capsys):
    import json
    def forbidden_lock(*_args):
        raise AssertionError('Queries must not acquire a write lock')
    monkeypatch.setattr(lessons, 'locked', forbidden_lock)
    for action, payload in (('retrieve', {'query': 'example', 'scope': 'runtime'}),
                            ('audit', {'occurrenceIds': ['unknown']})):
        monkeypatch.setattr(sys, 'argv', ['lessons.py', '--project-root', str(tmp_path), action, '--input-json', json.dumps(payload)])
        assert lessons.main() == 0
        data = json.loads(capsys.readouterr().out)
        assert data.get('count') == 0 if action == 'retrieve' else data['missing'] == ['unknown']
    assert list(tmp_path.iterdir()) == []


def candidate(root):
    return lessons.operate(root, 'candidate', dict(id='demo', ruleName='rule-demo', ruleText='# 규칙\n\n## 1. 실행\n\n- 조건을 확인합니다.',
        trigger='관련 작업에 적용합니다.', exceptions='다른 프로젝트 제외', scope='project-a', authority='human-request:LL-001'))['candidateHash']


def test_full_lifecycle_and_actual_export(tmp_path):
    value = seed(tmp_path)
    lessons.operate(tmp_path, 'record', value)
    assert len(lessons.find(tmp_path, 'demo')['occurrences']) == 1
    value['occurrenceId'] = 'run-2'
    lessons.operate(tmp_path, 'record', value)
    assert len(lessons.find(tmp_path, 'demo')['occurrences']) == 2
    assert lessons.operate(tmp_path, 'audit', {'occurrenceIds': ['run-1', 'run-3']})['missing'] == ['run-3']
    hash_ = candidate(tmp_path)
    with pytest.raises(ValueError, match='missing'):
        lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    for kind in ('original', 'held-out'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind=kind, caseId=kind, passed=True, evidence='test:' + kind))
    lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    assert (tmp_path / '.codex/skills/rule-demo/SKILL.md').read_bytes() == (tmp_path / 'docs/skills/rule-demo/SKILL.md').read_bytes()
    lessons.operate(tmp_path, 'apply', dict(id='demo', runId='run-4', outcome='recurrence', evidence='test:failed'))
    found = lessons.operate(tmp_path, 'retrieve', {'query': '사례', 'scope': 'project-a'})
    assert found['records'][0]['applications'][0]['version'] == 1
    assert lessons.operate(tmp_path, 'retrieve', {'query': '사례', 'scope': 'project-b'})['count'] == 0
    lessons.operate(tmp_path, 'retire', {'id': 'demo', 'reason': '재발'})
    assert '적용하지 않습니다' in (tmp_path / '.codex/skills/rule-demo/SKILL.md').read_text()
    with pytest.raises(ValueError, match='active'):
        lessons.operate(tmp_path, 'apply', dict(id='demo', runId='r5', outcome='success', evidence='x'))


def test_judgment_and_stale_evaluation(tmp_path):
    seed(tmp_path, 'judgment')
    first = candidate(tmp_path)
    candidate(tmp_path)
    with pytest.raises(ValueError, match='Stale'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=first, kind='held-out', caseId='case', passed=True, evidence='x'))
    record = lessons.find(tmp_path, 'demo')
    assert record['occurrences'][0]['humanReason'] == '미확인'
    assert record['occurrences'][0]['aiJudgment'] == '자세히'


def test_unowned_rule_and_symlink_preserved(tmp_path):
    seed(tmp_path)
    hash_ = candidate(tmp_path)
    for kind in ('original', 'held-out'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind=kind, caseId=kind, passed=True, evidence='test'))
    path = tmp_path / 'docs/skills/rule-demo/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('human-owned')
    with pytest.raises(ValueError, match='unowned'):
        lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    assert path.read_text() == 'human-owned'
    with pytest.raises(ValueError):
        lessons.operate(tmp_path, 'record', {**seed(tmp_path), 'id': '../escape'})


def test_failed_evaluation_and_sync_recovery(tmp_path):
    seed(tmp_path)
    hash_ = candidate(tmp_path)
    lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind='original', caseId='same', passed=False, evidence='failure'))
    with pytest.raises(ValueError, match='failing'):
        lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    hash_ = candidate(tmp_path)
    for kind in ('original', 'held-out'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind=kind, caseId=kind, passed=True, evidence='test'))
    conflict = tmp_path / '.codex/skills/rule-demo'
    conflict.mkdir(parents=True)
    (conflict / 'SKILL.md').write_text('unowned')
    with pytest.raises(ValueError):
        lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    assert lessons.find(tmp_path, 'demo')['status'] == 'sync-pending'
    # Simulate the owner resolving the reported destination collision.
    conflict.rename(tmp_path / 'owner-backup')
    lessons.operate(tmp_path, 'sync', {'id': 'demo'})
    assert lessons.find(tmp_path, 'demo')['status'] == 'active'
    assert (tmp_path / 'owner-backup/SKILL.md').read_text() == 'unowned'


def test_applied_rule_must_match_published_version(tmp_path):
    seed(tmp_path)
    hash_ = candidate(tmp_path)
    for kind in ('original', 'held-out'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind=kind, caseId=kind, passed=True, evidence='test'))
    lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    (tmp_path / 'docs/skills/rule-demo/SKILL.md').write_text('independent edit')
    with pytest.raises(ValueError, match='version changed'):
        lessons.operate(tmp_path, 'apply', dict(id='demo', runId='r2', outcome='success', evidence='test'))
    assert lessons.find(tmp_path, 'demo')['applications'] == []


def test_split_storage_and_legacy_update_guard(tmp_path):
    seed(tmp_path)
    path = next((tmp_path / 'docs/lessons-learned/errors').glob('*.md'))
    assert path.is_file()
    record = lessons.find(tmp_path, 'demo')
    meta = lessons.body_store.metadata_path(tmp_path, 'demo')
    assert '미확인' not in meta.read_text()  # Prose has only the Markdown source.
    assert '미확인' in path.read_text()
    legacy = path.parent.parent / 'error-demo/assets'
    legacy.mkdir(parents=True)
    (legacy / 'lesson.json').write_text(__import__('json').dumps(record))
    with pytest.raises(ValueError, match='Duplicate'):
        lessons.records(tmp_path)
    path.unlink()
    assert lessons.find(tmp_path, 'demo')['id'] == 'demo'
    with pytest.raises(ValueError, match='migrated'):
        lessons.operate(tmp_path, 'resolve', dict(id='demo', cause='known', solution='fix', verification='pass', evidence='test'))
    assert not path.exists()


def test_input_errors_name_the_expected_action(tmp_path):
    with pytest.raises(ValueError, match='scope must be a nonempty string, not dict'):
        lessons.operate(tmp_path, 'retrieve', dict(query='failure', scope={'name': 'project-a'}))
    seed(tmp_path)
    with pytest.raises(ValueError, match='Only active rules may be applied; record a recurrence'):
        lessons.operate(tmp_path, 'apply', dict(id='demo', runId='r1', outcome='recurrence', evidence='test'))


def test_submodule_root_is_refused_when_workspace_keeps_lessons(tmp_path):
    (tmp_path / '.gitmodules').write_text('[submodule "extension"]\n\tpath = extension\n\turl = git@example:ext.git\n')
    (tmp_path / 'extension').mkdir()
    (tmp_path / 'tools').mkdir()
    seed(tmp_path)
    with pytest.raises(ValueError, match='submodule'):
        seed(tmp_path / 'extension')
    assert not (tmp_path / 'extension/docs').exists()
    seed(tmp_path / 'tools')
    with pytest.raises(ValueError, match='--project-root'):
        lessons.operate(tmp_path / 'extension', 'retrieve', {'query': '사례', 'scope': 'project-a'})


def test_submodule_root_without_workspace_lessons_is_allowed(tmp_path):
    (tmp_path / '.gitmodules').write_text('[submodule "extension"]\n\tpath = extension\n')
    (tmp_path / 'extension').mkdir()
    seed(tmp_path / 'extension')
    assert len(list((tmp_path / 'extension/docs/lessons-learned/errors').glob('*.md'))) == 1


def test_record_marks_a_known_occurrence_recovered_once(tmp_path):
    value = seed(tmp_path)
    lessons.operate(tmp_path, 'record', {**value, 'recovered': True, 'recoveredBy': 'item-9'})
    occurrence = lessons.find(tmp_path, 'demo')['occurrences'][0]
    assert (occurrence['recovered'], occurrence['recoveredBy']) == (True, 'item-9')
    stamp = occurrence['recoveredAt']
    lessons.operate(tmp_path, 'record', {**value, 'recovered': True, 'recoveredBy': 'item-10'})
    again = lessons.find(tmp_path, 'demo')['occurrences']
    assert len(again) == 1 and (again[0]['recoveredBy'], again[0]['recoveredAt']) == ('item-9', stamp)


def test_markdown_edits_search_and_candidate_evidence(tmp_path):
    from search_documents import search
    seed(tmp_path)
    hash_ = candidate(tmp_path)
    for kind in ('original', 'held-out'):
        lessons.operate(tmp_path, 'evaluate', dict(id='demo', candidateHash=hash_, kind=kind,
                        caseId=kind, passed=True, evidence='실제 점검'))
    body = next((tmp_path / 'docs/lessons-learned/errors').glob('*.md'))
    body.write_text(body.read_text().replace('조건을 확인합니다.', '수정된 조건을 확인합니다.') + '\n사용자님의 추가 메모\n')
    assert search(tmp_path, '수정된 조건', scope='project-a')['count'] == 1
    with pytest.raises(ValueError, match='Stale'):
        lessons.operate(tmp_path, 'publish', {'id': 'demo'})
    lessons.operate(tmp_path, 'resolve', dict(id='demo', cause='확인된 원인', solution='수정', verification='통과', evidence='점검'))
    assert '사용자님의 추가 메모' in body.read_text()
    assert 'resolved' in body.read_text()
    assert lessons.find(tmp_path, 'demo')['resolutions'][0]['cause'] == '확인된 원인'


def test_interrupted_write_replay_and_independent_edit_guard(tmp_path, monkeypatch):
    seed(tmp_path)
    original = lessons.runtime_paths.write
    meta = lessons.body_store.metadata_path(tmp_path, 'demo')
    def interrupted(path, value):
        if path == meta:
            raise OSError('simulated interrupted metadata publication')
        return original(path, value)
    value = {**seed(tmp_path), 'occurrenceId': 'run-2'}
    monkeypatch.setattr(lessons.runtime_paths, 'write', interrupted)
    with pytest.raises(OSError):
        lessons.operate(tmp_path, 'record', value)
    monkeypatch.setattr(lessons.runtime_paths, 'write', original)
    with pytest.raises(ValueError, match='Interrupted'):
        lessons.records(tmp_path)
    lessons.operate(tmp_path, 'record', value)
    assert len(lessons.find(tmp_path, 'demo')['occurrences']) == 2
    assert not meta.with_suffix('.pending.json').exists()
    monkeypatch.setattr(lessons.runtime_paths, 'write', interrupted)
    with pytest.raises(OSError):
        lessons.operate(tmp_path, 'record', {**value, 'occurrenceId': 'run-3'})
    monkeypatch.setattr(lessons.runtime_paths, 'write', original)
    body = next((tmp_path / 'docs/lessons-learned/errors').glob('*.md'))
    body.write_text(body.read_text() + '\n사용자님의 독립 수정\n')
    with pytest.raises(ValueError, match='edited during interrupted write'):
        lessons.operate(tmp_path, 'recover', {'id': 'demo'})
    assert '사용자님의 독립 수정' in body.read_text()
    assert meta.with_suffix('.pending.json').exists()


def test_judgment_resolution_and_same_event_links_are_distinct(tmp_path):
    from catalog_documents import build_catalog
    value = seed(tmp_path, 'judgment')
    lessons.operate(tmp_path, 'record', {**value, 'id': 'associated-error', 'category': 'error',
                    'occurrenceId': 'same-event-error', 'relatedIds': ['demo']})
    lessons.operate(tmp_path, 'resolve', dict(id='demo', outcome='사용자님 선택 반영',
                    reflection='목적에 따른 판단 차이입니다.', evidence='대화:1'))
    record = lessons.find(tmp_path, 'demo')
    assert record['category'] == 'judgment' and record['status'] == 'resolved'
    assert record['resolutions'][0]['outcome'] == '사용자님 선택 반영'
    entries = build_catalog(tmp_path)['documents']
    error = next(entry for entry in entries if entry['name'] == 'associated-error')
    assert error['relatedIds'] == ['demo']
    judgment = next(entry for entry in entries if entry['name'] == 'demo')
    assert judgment['contentPath'] in error['links']
    assert '판단 차이입니다.' in (tmp_path / judgment['contentPath']).read_text()


def test_storage_check_is_read_only_and_reports_markdown_support(tmp_path, monkeypatch):
    seed(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(lessons, 'locked', lambda _: pytest.fail('check must not acquire a write lock'))
    result = lessons.operate(tmp_path, 'check', {})
    assert result['compatible'] is True and result['count'] == 1
    assert 'categorized-markdown-with-runtime-metadata-v2' in result['supportedFormats']
    assert result['toolPath'] == str(SCRIPTS / 'lessons.py')
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('broken', ['unknown-directory', 'missing-metadata', 'invalid-body'])
def test_storage_failures_have_actionable_nonretryable_cli_diagnostics(tmp_path, broken):
    import json
    import subprocess
    seed(tmp_path)
    if broken == 'unknown-directory':
        (tmp_path / 'docs/lessons-learned/unrecognized').mkdir()
    elif broken == 'missing-metadata':
        lessons.body_store.metadata_path(tmp_path, 'demo').unlink()
    else:
        next((tmp_path / 'docs/lessons-learned/errors').glob('*.md')).write_text('invalid')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    payload = tmp_path / 'check-input.json'
    payload.write_text('{}')
    result = subprocess.run([sys.executable, str(SCRIPTS / 'lessons.py'), '--project-root', str(tmp_path),
                             'check', '--input', str(payload)], capture_output=True, text=True)
    diagnostic = json.loads(result.stdout)
    assert result.returncode == 1 and not result.stderr
    assert diagnostic['code'] == 'lesson_storage_incompatible' and diagnostic['retryable'] is False
    assert diagnostic['documentRoot'] == str(tmp_path)
    assert diagnostic['supportedFormats'] and 'check' in diagnostic['recovery']
    assert all(path.read_bytes() == content for path, content in before.items())
