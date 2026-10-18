"""Prevent artifact directory drift before producers write files."""
from pathlib import Path

import pytest

from storage.artifacts import DOMAINS, artifact_output, layout_errors
from system.archify import artifact_path


@pytest.fixture
def workspace(tmp_path):
    for domain in DOMAINS:
        (tmp_path / "docs" / "artifact" / domain).mkdir(parents=True)
    return tmp_path


def test_flat_output_and_private_scratch(workspace):
    path = Path("docs/artifact/interactive/topic__architecture.html")
    assert artifact_path(path, workspace) == workspace / path
    assert not (workspace / path).exists()
    assert artifact_output(Path("scratch.png"), workspace) == workspace / "scratch.png"
    exported = Path("docs/artifact/output/report.pdf")
    assert artifact_output(exported, workspace) == workspace / exported
    assert layout_errors(workspace / "docs/artifact") == []


@pytest.mark.parametrize("name", ["document/a.html", "preview/a.html", "interactive/topic/a.html", "a.html",
                                  "interactive/../interactive/a.html"])
def test_reject_nested_unknown_root_and_traversal(workspace, name):
    with pytest.raises(ValueError):
        artifact_path(Path("docs/artifact") / name, workspace)


def test_reject_symlink_and_directory_drift(workspace):
    root = workspace / "docs/artifact"
    (root / "interactive/link.html").symlink_to(workspace / "elsewhere.html")
    with pytest.raises(ValueError, match="symlink"):
        artifact_path(root / "interactive/link.html", workspace)
    (root / "evidence/topic").mkdir()
    (root / "unknown").mkdir()
    errors = layout_errors(root)
    assert any("interactive/link.html" in error for error in errors)
    assert any("evidence/topic" in error for error in errors)
    assert any("unknown" in error for error in errors)


def test_missing_domain_is_not_created(workspace):
    (workspace / "docs/artifact/interactive").rmdir()
    with pytest.raises(ValueError, match="already exist"):
        artifact_path(Path("docs/artifact/interactive/a.html"), workspace)
    assert not (workspace / "docs/artifact/interactive").exists()


def test_required_output_cannot_escape(workspace):
    with pytest.raises(ValueError):
        artifact_path(workspace / "outside.html", workspace)
