"""Startup must not require document-only third-party dependencies."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/exec.py'


@pytest.mark.parametrize('arguments', [['--help'], ['submit', '--help'], ['location']])
def test_runtime_starts_without_site_packages(arguments, tmp_path):
    result = subprocess.run(
        [sys.executable, '-S', str(SCRIPT), *arguments], cwd=tmp_path,
        capture_output=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stderr
    if arguments == ['location']:
        assert json.loads(result.stdout)['kind'] == 'runtime-location'
