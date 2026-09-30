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
