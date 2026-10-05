"""Exercise contract-listed document moves: backup gating, preservation, conflicts and reruns."""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MIGRATE = ROOT / "scripts/migrate_document_paths.py"


def run(project, operations, *args):
    return subprocess.run(
        [sys.executable, str(MIGRATE), "--project-root", str(project), "--operations", str(operations), *args],
        capture_output=True,
        text=True,
    )


def setup(tmp_path):
    project = tmp_path / "project"
    old = project / "docs/processed"
    (old / "analyze-alpha/assets").mkdir(parents=True)
    (old / "analyze-alpha/SKILL.md").write_text(
        "# Alpha\n\n- [Beta](../analyze-beta/SKILL.md#top)\n- [Asset](assets/data.csv)\n", encoding="utf-8"
    )
    (old / "analyze-alpha/assets/data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (old / "analyze-beta").mkdir()
    (old / "analyze-beta/SKILL.md").write_text("# 베타\n\n- 원문 보존\n", encoding="utf-8")
    os.chmod(old / "analyze-beta/SKILL.md", 0o640)
    (project / "docs/unrelated-empty").mkdir()
    operations = tmp_path / "operations.csv"
    with operations.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(["taskIds", "operation", "path", "destination"])
        for relative in ("analyze-alpha/SKILL.md", "analyze-alpha/assets/data.csv", "analyze-beta/SKILL.md"):
            writer.writerow(["T3", "move", f"docs/processed/{relative}", f"docs/refined/{relative}"])
    return project, operations, tmp_path / "backup"


def test_apply_requires_backup_then_moves_preserving_content(tmp_path):
    project, operations, backup = setup(tmp_path)
    beta_before = (project / "docs/processed/analyze-beta/SKILL.md").read_bytes()

    preview = run(project, operations)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["summary"]["pending"] == 3

    refused = run(project, operations, "--apply", "--backup-dir", str(backup))
    assert refused.returncode == 1
    assert "completed backup manifest" in refused.stderr
    assert not (project / "docs/refined").exists()

    identity = tmp_path / "runtime-identity"
    identity.mkdir()
    backed_up = run(identity, operations, "--documents-root", str(project), "--backup", "--backup-dir", str(backup))
    assert backed_up.returncode == 0, backed_up.stderr
    assert (backup / "files/docs/processed/analyze-beta/SKILL.md").read_bytes() == beta_before

    applied = run(identity, operations, "--documents-root", str(project), "--apply", "--backup-dir", str(backup))
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["applied"]["moved"] == 3
    refined = project / "docs/refined"
    assert (refined / "analyze-beta/SKILL.md").read_bytes() == beta_before
    assert (refined / "analyze-beta/SKILL.md").stat().st_mode & 0o777 == 0o640
    assert (refined / "analyze-alpha/assets/data.csv").read_text() == "a,b\n1,2\n"
    # Relative links between moved packages still resolve after the move.
    assert "(../analyze-beta/SKILL.md#top)" in (refined / "analyze-alpha/SKILL.md").read_text()
    assert not (project / "docs/processed").exists()
    assert (project / "docs/unrelated-empty").is_dir()
    assert not list(refined.rglob(".migrate-*"))

    assert not (identity / "docs").exists()
    rerun = run(identity, operations, "--documents-root", str(project), "--apply", "--backup-dir", str(backup))
    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(rerun.stdout)["applied"] == {"moved": 0, "completedInterrupted": 0, "unchanged": 3}


def test_collision_and_partial_destination_never_overwrite(tmp_path):
    project, operations, backup = setup(tmp_path)
    assert run(project, operations, "--backup", "--backup-dir", str(backup)).returncode == 0
    partial = project / "docs/refined/analyze-beta/SKILL.md"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"# half-writ")
    result = run(project, operations, "--apply", "--backup-dir", str(backup))
    assert result.returncode == 1
    assert "Destination collision" in result.stderr
    assert partial.read_bytes() == b"# half-writ"
    assert (project / "docs/processed/analyze-beta/SKILL.md").is_file()
    assert not (project / "docs/refined/analyze-alpha").exists()


def test_interrupted_move_completes_only_when_both_copies_verify(tmp_path):
    project, operations, backup = setup(tmp_path)
    assert run(project, operations, "--backup", "--backup-dir", str(backup)).returncode == 0
    source = project / "docs/processed/analyze-beta/SKILL.md"
    destination = project / "docs/refined/analyze-beta/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    result = run(project, operations, "--apply", "--backup-dir", str(backup))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"]["completedInterrupted"] == 1
    assert not source.exists()


def test_changed_source_after_backup_is_rejected(tmp_path):
    project, operations, backup = setup(tmp_path)
    assert run(project, operations, "--backup", "--backup-dir", str(backup)).returncode == 0
    (project / "docs/processed/analyze-beta/SKILL.md").write_text("# edited\n", encoding="utf-8")
    result = run(project, operations, "--apply", "--backup-dir", str(backup))
    assert result.returncode == 1
    assert "does not match" in result.stderr or "changed" in result.stderr
    assert not (project / "docs/refined").exists()


def test_symlinks_and_escaping_paths_are_rejected(tmp_path):
    project, operations, backup = setup(tmp_path)
    (project / "docs/refined").symlink_to(tmp_path)
    result = run(project, operations, "--backup", "--backup-dir", str(backup))
    assert result.returncode == 1
    assert "Symlinks are not supported" in result.stderr

    escaping = tmp_path / "escape.csv"
    escaping.write_text("taskIds,operation,path,destination\nT3,move,../outside.md,docs/x.md\n", encoding="utf-8")
    result = run(project, escaping)
    assert result.returncode == 1
    assert "Unsafe source path" in result.stderr


def test_storage_layout_lossless_preview_apply_and_resume(tmp_path, monkeypatch):
    import runtime_test_home  # noqa: F401
    from storage import document_migration as migration, paths
    from catalog_documents import build_catalog
    from search_documents import search
    import lessons
    root, physical = tmp_path / 'identity', tmp_path / 'workspace'
    root.mkdir()
    physical.mkdir()
    paths.resolve(root, create=True)
    folder = physical / 'docs/lessons-learned'
    folder.mkdir(parents=True)
    record = dict(schemaVersion=1, id='old', category='judgment', title='선택의 차이', language='ko',
                  scope='document-storage', status='unresolved', occurrences=[dict(occurrenceId='o1',
                  source='actual:run', humanJudgment='유형을 나눕니다.', humanReason='목적이 다릅니다.',
                  aiJudgment='JSON 하나', aiReason='조회 편의', difference='저장 방식', reflection='정본 소유권 차이',
                  outcome='분리 승인', signature=dict(provider='codex', code='original-error'), recordedAt='2026-01-01')],
                  applications=[dict(runId='r1', outcome='correction', evidence='적용 근거')],
                  candidates=[dict(ruleText='# 이전 규칙\n\n## 1. 조건\n\n- 확인합니다.', trigger='조건', exceptions='예외', evaluations=[dict(evidence='기존 점검')])],
                  publications=[dict(path='docs/skills/rule-existing/SKILL.md', fileHash='abc', status='active')],
                  resolutions=[dict(cause='과거 원인', solution='과거 해결', evidence='당시 근거')])
    record['occurrences'][0]['diagnostic'] = {'textRef': '원문 진단'}
    record['occurrences'][0]['extra'] = {'a/b': '슬래시 키', 'a': {'b': '별도 중첩 키'}}
    old = folder / 'old.json'
    rule = physical / 'docs/skills/rule-existing/SKILL.md'
    rule.parent.mkdir(parents=True)
    rule.write_text('---\nmetadata:\n  lesson: ../../lessons-learned/old.json\n---\n# 규칙\n\n- 기존 규칙입니다.\n')
    record['publications'][-1]['fileHash'] = migration.digest(rule.read_bytes())
    old.write_text(json.dumps(record, ensure_ascii=False))
    package = physical / 'docs/refined/other-event'
    package.mkdir(parents=True)
    (package / 'references').mkdir()
    (package / 'assets').mkdir()
    (package / 'assets/empty').mkdir()
    entry = package / 'SKILL.md'
    entry.write_text('---\ndocument-type: processed\ncategory: other\ndomain: null\nname: event\nlanguage: ko\nsource-id: original-1\n---\n# 과거 연구\n\n## 1. 근거\n\n- [상세](references/detail.md)\n- [자료](assets/data.bin)\n- [판단](../../lessons-learned/old.json)\n')
    detail = package / 'references/detail.md'
    detail.write_text('# 상세\n\n## 1. 판단\n\n- [사건](../../../lessons-learned/old.json)\n- 당시에는 JSON 하나로 저장했습니다.\n')
    (package / 'assets/data.bin').write_bytes(b'\x00\xfforiginal')
    incoming = physical / 'docs/incoming.md'
    incoming.write_text('# 링크\n\n- [연구](refined/other-event/SKILL.md)\n')
    before = {str(p.relative_to(physical)): p.read_bytes() for p in physical.rglob('*') if p.is_file()}
    unclassified = migration.preview(root, physical)
    assert len(unclassified['unclassified']) == 1
    categories = {'docs/refined/other-event': 'research'}
    plan = migration.preview(root, physical, categories)
    assert plan['conflicts'] == [] and plan['unclassified'] == []
    assert plan['languages'] == {'ko': 2}
    assert before == {str(p.relative_to(physical)): p.read_bytes() for p in physical.rglob('*') if p.is_file()}
    run_storage = Path(paths.resolve(root)['agentsRoot']) / 'migration-test/runs/run-fixture'
    run_storage.mkdir(parents=True)
    backup = run_storage / 'backup-layout'
    original_write = paths.write
    def interrupted(target, data):
        if target.name == 'old.json':
            raise OSError('simulated metadata failure')
        original_write(target, data)
    monkeypatch.setattr(paths, 'write', interrupted)
    with pytest.raises(OSError, match='simulated'):
        migration.apply(root, physical, backup, categories)
    assert old.exists() and entry.exists()  # No source is retired before verification.
    monkeypatch.setattr(paths, 'write', original_write)
    migration.apply(root, physical, backup, categories)
    joined = lessons.find(root, 'old', physical)
    assert joined['publications'][-1].pop('currentFileHash') == migration.digest(rule.read_bytes())
    assert joined == record
    assert 'judgment-differences/' in rule.read_text()
    assert not old.exists() and not package.exists()
    target = physical / 'docs/refined/research/event'
    assert (target / 'assets/data.bin').read_bytes() == b'\x00\xfforiginal'
    assert (target / 'assets/empty').is_dir()
    assert '당시에는 JSON 하나로 저장했습니다.' in (target / 'references/detail.md').read_text()
    assert '../../lessons-learned/old.json' not in (target / 'SKILL.md').read_text()
    assert 'refined/research/event/SKILL.md' in incoming.read_text()
    assert len(build_catalog(root, physical)['documents']) == 2
    assert search(root, '정본 소유권', documents_root=physical, scope='document-storage')['count'] == 1
    metadata = lessons.body_store.metadata_path(root, 'old').read_text()
    assert '정본 소유권' not in metadata and '과거 원인' not in metadata and '이전 규칙' not in metadata
    migration.apply(root, physical, backup, categories)  # Manifest-owned replay is idempotent.
    (target / 'references/detail.md').write_text('사용자님의 후속 수정')
    with pytest.raises(ValueError, match='Destination changed'):
        migration.apply(root, physical, backup, categories)
    assert (target / 'references/detail.md').read_text() == '사용자님의 후속 수정'
    assert not (root / 'docs').exists()


def test_storage_layout_conflicts_dirty_exclusions_and_language(tmp_path):
    import runtime_test_home  # noqa: F401
    from storage import document_migration as migration, paths
    root = tmp_path / 'project'
    root.mkdir()
    paths.resolve(root, create=True)
    directory = root / 'docs/lessons-learned'
    directory.mkdir(parents=True)
    for name in ('one', 'two'):
        value = dict(schemaVersion=1, id=name, category='error', title='Même titre', language='fr',
                     scope='test', status='unresolved', occurrences=[dict(occurrenceId=name,
                     symptom='Erreur originale', cause='cause inconnue', solution='non résolue', verification='non vérifiée')],
                     applications=[], candidates=[], publications=[])
        (directory / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False))
    first = migration.preview(root, root)
    assert first['languages'] == {'fr': 2}
    assert len({move['destination'] for move in first['moves']}) == 2
    package = root / 'docs/refined/research-topic'
    (package / 'references').mkdir(parents=True)
    (package / 'SKILL.md').write_text('---\ndocument-type: processed\ncategory: research\ndomain: null\nname: topic\nlanguage: fr\n---\n# Recherche\n\n- [Détail](references/detail.md)\n')
    (package / 'references/detail.md').write_text('Modification utilisateur')
    plan = migration.preview(root, root, excluded=('docs/refined/research-topic/references/detail.md',))
    assert plan['excluded'] == ['docs/refined/research-topic']
    assert not any(move['kind'] == 'refined' for move in plan['moves'])
    import lessons
    created = lessons.operate(root, 'record', dict(id='french-body', category='error', title='Erreur originale', language='fr',
                              scope='test', source='test:fr', **{**value['occurrences'][0], 'occurrenceId': 'fr-1'}))
    text = (root / created['path']).read_text()
    assert 'Erreur originale' in text and 'cause inconnue' in text
    assert 'Record and current state' not in text
    assert lessons.find(root, 'french-body')['language'] == 'fr'
    destination = root / first['moves'][0]['destination']
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('Contenu indépendant')
    conflict = migration.preview(root, root)
    assert len(conflict['conflicts']) == 1
    with pytest.raises(ValueError, match='Resolve migration conflicts'):
        migration.apply(root, root, tmp_path / 'conflict-backup')
    assert destination.read_text() == 'Contenu indépendant'
    assert not (tmp_path / 'conflict-backup').exists()


def test_selected_body_language_preserves_original_text_and_source_language(tmp_path):
    import runtime_test_home  # noqa: F401
    from storage import document_migration as migration, paths
    import lessons
    root = tmp_path / 'project'
    root.mkdir()
    paths.resolve(root, create=True)
    folder = root / 'docs/lessons-learned'
    folder.mkdir(parents=True)
    original = dict(schemaVersion=1, id='source-language', category='error', title='original-error', language='en',
                    scope='test', status='unresolved', occurrences=[dict(occurrenceId='original', cause='unknown',
                    symptom='Original diagnostic message', solution='unresolved', verification='not checked')],
                    applications=[], candidates=[], publications=[])
    (folder / 'source-language.json').write_text(json.dumps(original))
    plan = migration.preview(root, root, language='ko')
    assert plan['languages'] == {'ko': 1} and plan['sourceLanguages'] == {'en': 1}
    migration.apply(root, root, tmp_path / 'language-backup', language='ko')
    joined = lessons.find(root, 'source-language')
    assert joined['language'] == 'ko' and joined['sourceLanguage'] == 'en'
    assert joined['occurrences'] == original['occurrences'] and joined['status'] == 'unresolved'
    body = (root / plan['moves'][0]['destination']).read_text()
    assert '현상·근거' in body and '> Original diagnostic message' in body
    with pytest.raises(ValueError, match='Backup language differs'):
        migration.apply(root, root, tmp_path / 'language-backup', language='fr')
