"""Real pinned renderer and filesystem/CLI boundary checks (no automatic downloads)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from system import archify

SCRIPT = Path(__file__).parents[2] / "scripts" / "archify.py"


@pytest.fixture
def package(tmp_path):
    folder = tmp_path / "docs" / "refined" / "analysis" / "sample"
    assets = folder / "assets"
    assets.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    output = tmp_path / "docs" / "artifact" / "preview" / "sample"
    output.mkdir(parents=True)
    return tmp_path, assets, output


def model(kind="architecture"):
    data = {"schema_version": 1, "diagram_type": kind,
            "meta": {"title": "한글 검증 예시", "output": "authored.html", "animation": "none"}}
    if kind == "architecture":
        data["components"] = [{"id": "a", "type": "backend", "label": "원본 검증", "pos": [80, 80]}]
    else:
        data["participants"] = [{"id": "a", "type": "frontend", "label": "사용자"},
                                {"id": "b", "type": "backend", "label": "렌더러"}]
        data["messages"] = [{"from": "a", "to": "b", "y": 180, "label": "검증 요청"}]
    return data


def input_file(assets, data, name="input.json"):
    path = assets / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def tool():
    value = os.environ.get("ARCHIFY_TEST_TOOL_DIR")
    if not value:
        pytest.skip("Set ARCHIFY_TEST_TOOL_DIR to an install of the pinned renderer for real integration checks.")
    path = Path(value)
    archify.renderer(path)
    return path


def cli(root, tool, source, output=None):
    argv = [sys.executable, str(SCRIPT), "render" if output else "validate", "--project-root", str(root),
            "--tool-dir", str(tool), str(source)]
    if output:
        argv += ["--output", str(output)]
    process = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    return process.returncode, json.loads(process.stdout)


@pytest.mark.parametrize("kind", ["architecture", "sequence"])
def test_real_validation_delivery_and_no_overwrite(package, tool, kind):
    root, assets, folder = package
    # Shell metacharacters are literal argv/path text, never commands.
    document = model(kind)
    document["meta"]["output"] = "nested/$(touch injected) `echo no`.html"
    source = input_file(assets, document, "한글 $(touch injected) `echo no`.json")
    before = source.read_bytes()
    output = folder / "한글 $(touch injected) `echo no`.html"
    status, report = cli(root, tool, source, output)
    assert status == 0, report
    assert report["validation"]["ok"] and report["delivery"]["ok"]
    assert report["inputSha256"] == hashlib.sha256(before).hexdigest()
    html = output.read_bytes()
    assert report["htmlSha256"] == hashlib.sha256(html).hexdigest()
    assert "한글 검증 예시" in html.decode()
    assert source.read_bytes() == before
    assert not (root / "injected").exists()
    assert not (root / "authored.html").exists()
    assert not (assets / "nested").exists()
    assert list(folder.iterdir()) == [output]
    status, report = cli(root, tool, source, output)
    assert status == 1 and "already exists" in report["error"]
    assert output.read_bytes() == html and source.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"schema_version": 999}, {"extra": True}, {"components": "wrong type"},
    {"components": []}, {"meta": {"title": "bad", "output": "../escape.html"}},
    {"meta": {"title": "bad", "output": "/absolute.html"}},
    {"meta": {"title": "bad", "output": "C:\\escape.html"}},
])
def test_official_schema_failures_preserve_input(package, tool, change):
    root, assets, folder = package
    data = model()
    data.update(copy.deepcopy(change))
    source = input_file(assets, data)
    before = source.read_bytes()
    status, report = cli(root, tool, source, folder / "bad.html")
    assert status == 1 and "Official Archify failed" in report["error"]
    assert source.read_bytes() == before and list(folder.iterdir()) == []


@pytest.mark.parametrize("data,expected", [
    ("{", "Invalid JSON"), (json.dumps({"diagram_type": "erd"}), "Unsupported diagram_type"),
    (json.dumps({"diagram_type": "workflow"}), "Unsupported diagram_type"),
    (json.dumps([]), "Unsupported diagram_type"),
])
def test_bad_inputs_are_actionable_without_tool(package, data, expected):
    root, assets, folder = package
    source = assets / "bad.json"
    source.write_text(data)
    status, report = cli(root, root / "missing-tool", source, folder / "bad.html")
    assert status == 1 and expected in report["error"]
    assert source.read_text() == data and list(folder.iterdir()) == []


def test_missing_tool_node_and_tampering(package, tool, monkeypatch, tmp_path):
    root, assets, folder = package
    source = input_file(assets, model())
    status, report = cli(root, root / "absent", source, folder / "new.html")
    assert status == 1 and "install" in report["error"]
    monkeypatch.setattr(archify.shutil, "which", lambda _: None)
    with pytest.raises(archify.ArchifyError, match="Node.js >=18"):
        archify.renderer(tool)
    wrong = tmp_path / "changed-renderer"
    wrong.mkdir()
    (wrong / "package.json").write_text('{"version":"3.0.1"}')
    with pytest.raises(archify.ArchifyError, match="integrity mismatch"):
        archify.renderer(wrong)


def test_output_and_input_boundaries(package, tmp_path):
    root, assets, folder = package
    source = input_file(assets, model())
    for output in [source, root / "outside.html", folder / "missing" / "new.html",
                   folder / ".." / "escape.html", folder / "new.json"]:
        with pytest.raises(ValueError):
            archify.artifact_path(output, root)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = folder / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        archify.artifact_path(linked / "new.html", root)
    alias = folder / "new.html"
    alias.symlink_to(outside / "victim.html")
    with pytest.raises(ValueError, match="Symlink"):
        archify.artifact_path(alias, root)
    with pytest.raises(ValueError, match="Document package|JSON asset"):
        archify.document_input(folder / "input.json", root)
    assert not (outside / "victim.html").exists()


def test_publish_does_not_follow_replaced_parent(package, tmp_path):
    _, _, folder = package
    path = folder / "new.html"
    other = tmp_path / "other"
    other.mkdir()
    folder.rmdir()
    folder.symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        archify.publish_html(path, b"unsafe")
    assert not (other / "new.html").exists()


def test_install_checks_archive_and_preserves_targets(tmp_path, monkeypatch):
    target = tmp_path / "tool"
    monkeypatch.setattr(archify.urllib.request, "urlopen", lambda *a, **kw: __import__("io").BytesIO(b"tampered"))
    with pytest.raises(archify.ArchifyError, match="checksum mismatch"):
        archify.install(target)
    assert not target.exists()
    target.mkdir()
    existing = target / "original"
    existing.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="already exists"):
        archify.install(target)
    assert existing.read_bytes() == b"preserve"


def test_browser_uses_literal_uri_and_reports_failures(package, monkeypatch):
    root, _, folder = package
    path = folder / "한글 $(touch injected).html"
    path.write_text("<html></html>")
    seen = []
    monkeypatch.setattr(archify.sys, "platform", "linux")
    monkeypatch.setattr(archify.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(archify, "run_process", lambda argv: seen.append(argv) or
                        SimpleNamespace(returncode=0, stderr=""))
    report = archify.open_artifact(path)
    assert report["opened"] and seen == [["/usr/bin/xdg-open", path.as_uri()]]
    monkeypatch.setattr(archify.shutil, "which", lambda name: None)
    with pytest.raises(archify.ArchifyError, match="unavailable"):
        archify.open_artifact(path)
    assert path.exists() and not (root / "injected").exists()


def test_browser_failure_after_delivery_keeps_html_and_other_files(package, tool, monkeypatch):
    root, assets, folder = package
    source = input_file(assets, model())
    other = folder / "new.html.delivery.json"
    other.write_bytes(b"unrelated sidecar")
    original = source.read_bytes()

    def fail_open(path):
        raise archify.ArchifyError("Desktop browser is unavailable")

    monkeypatch.setattr(archify, "open_artifact", fail_open)
    output = folder / "new.html"
    report = archify.execute(SimpleNamespace(command="render", project_root=root, tool_dir=tool,
                                             input=source, output=output, open=True))
    assert not report["ok"] and not report["opened"] and "unavailable" in report["error"]
    assert output.is_file() and report["delivery"]["ok"]
    assert source.read_bytes() == original and other.read_bytes() == b"unrelated sidecar"


@pytest.mark.parametrize("kind", ["architecture", "sequence"])
def test_editor_preview_from_stdin_uses_official_renderer_without_project_writes(package, tool, kind):
    root, _, folder = package
    document = model(kind)
    document["meta"]["output"] = "nested/$(touch injected).html"
    before = sorted(str(p) for p in root.rglob("*"))
    process = subprocess.run([sys.executable, str(SCRIPT), "preview", "--tool-dir", str(tool)],
                             input=json.dumps(document, ensure_ascii=False), capture_output=True, text=True,
                             cwd=root, check=False)
    report = json.loads(process.stdout)
    assert process.returncode == 0, report
    assert report["type"] == kind and report["title"] == "한글 검증 예시"
    assert report["svg"].startswith("<svg") and "</svg>" in report["svg"]
    assert ".t-primary" in report["styles"]
    assert "<script" not in report["svg"]
    assert sorted(str(p) for p in root.rglob("*")) == before
    assert list(folder.iterdir()) == []
