"""Keep lessons and generated artifacts in the workspace docs, not in this repository."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("name", ["docs", "out"])
def test_no_stray_docs_or_out_directory(name):
    assert not (ROOT / name).exists(), (
        f"{name}/ must not exist in plugin/; move its files to the workspace "
        "docs/lessons-learned or docs/artifact/<topic>/")
