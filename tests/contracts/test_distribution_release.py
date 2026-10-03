from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "distribution" / "release.py"
SPEC = importlib.util.spec_from_file_location("distribution_release", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_release_version_must_increase() -> None:
    with pytest.raises(SystemExit, match="must increase past 1.0.24"):
        release.validate_version("1.0.24", "1.0.24")


def test_refresh_accepts_only_the_current_version() -> None:
    release.validate_version("1.0.24", "1.0.24", refresh=True)
    with pytest.raises(SystemExit, match="must equal the current source version 1.0.24"):
        release.validate_version("1.0.25", "1.0.24", refresh=True)


def test_refresh_push_count_excludes_the_unchanged_source() -> None:
    assert release.pending_push_count(refresh=True, host_count=3) == 3
    assert release.pending_push_count(refresh=False, host_count=3) == 4


def _push_fixture(tmp_path, monkeypatch, *, changed: bool, origin: str):
    state = {"mode": "release" if changed else "refresh", "version": "1.0.25", "build": "1",
             "source": {"path": str(tmp_path / "source"), "commit": "src-new", "changed": changed},
             "hosts": {"codex": {"path": str(tmp_path / "codex"), "commit": "codex-new"}}}
    state_file = tmp_path / "state.json"
    state_file.write_text(release.json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(release, "STATE_FILE", state_file)
    heads = {str(tmp_path / "source"): "src-new", str(tmp_path / "codex"): "codex-new"}
    pushes: list[tuple[str, str]] = []

    def fake_git(cwd, *args):
        if args == ("rev-parse", "HEAD"):
            return heads[str(cwd)]
        if args == ("rev-parse", "origin/main"):
            return origin
        if args[0] == "push":
            pushes.append((str(cwd), args[2]))
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(release, "git", fake_git)
    return state_file, pushes


def test_release_push_sends_the_unpushed_source_commit(tmp_path, monkeypatch) -> None:
    state_file, pushes = _push_fixture(tmp_path, monkeypatch, changed=True, origin="src-old")
    release.main(["push", "--yes"])
    assert pushes == [(str(tmp_path / "source"), "src-new:refs/heads/main"),
                      (str(tmp_path / "codex"), "codex-new:refs/heads/main")]
    assert not state_file.exists()


def test_refresh_push_requires_the_source_on_origin_main(tmp_path, monkeypatch) -> None:
    state_file, pushes = _push_fixture(tmp_path, monkeypatch, changed=False, origin="src-old")
    with pytest.raises(SystemExit, match="origin/main no longer matches"):
        release.main(["push", "--yes"])
    assert pushes == [] and state_file.exists()
