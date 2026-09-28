"""Mocked Windows contracts; they do not prove native Windows execution."""
import runtime_test_home

import ctypes
import importlib.util
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/exec.py'
spec = importlib.util.spec_from_file_location('windows_exec_test', SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
from system import portable
from system import containment as process_containment
from system import sandbox as sandbox_diagnostics
from system import windows as windows_process

IDENTITY = {'pid': 4242, 'bootId': 'windows', 'startTicks': 133_000_000_000_000_000}


def windows_job(identity=IDENTITY):
    return {'kind': 'windows-job', 'identity': dict(identity), 'weakerDescendantContainment': False}


def test_windows_is_a_supported_managed_platform():
    assert sandbox_diagnostics.platform_issue('win32') is None
    assert sandbox_diagnostics.platform_issue('cygwin')['code'] == 'managed_platform_unsupported'
    assert 'win32' in portable.SUPPORTED_PLATFORMS


def test_windows_diagnostics_report_job_containment_without_probe():
    with mock.patch.object(sys, 'platform', 'win32'), \
            mock.patch.object(windows_process, 'process_identity', return_value=IDENTITY), \
            mock.patch.object(sandbox_diagnostics.subprocess, 'run') as run:
        result = sandbox_diagnostics.diagnose(probe=True)
    assert result['issue'] is None
    assert result['containmentBackend'] == 'windows-job'
    assert result['weakerDescendantContainment'] is False
    assert result['windowsProcessIdentityAvailable'] is True
    assert result['probe'] == {'status': 'not-applicable'}
    run.assert_not_called()


def test_windows_sandbox_failure_message_no_longer_claims_unsupported_host():
    message = sandbox_diagnostics.sandbox_failure('Windows sandbox setup failed')
    assert message and 'not supported' not in message


def test_job_limit_layout_matches_the_win64_abi():
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on 64-bit Windows.
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(windows_process._BasicLimits) == 64
        assert ctypes.sizeof(windows_process._ExtendedLimits) == 144


def test_detached_launch_breaks_away_and_nested_launch_does_not():
    detached = windows_process.creation_flags(detach=True)
    nested = windows_process.creation_flags(detach=False)
    assert detached & windows_process.CREATE_BREAKAWAY_FROM_JOB
    assert not nested & windows_process.CREATE_BREAKAWAY_FROM_JOB
    for flags in (detached, nested):
        assert flags & windows_process.CREATE_NEW_PROCESS_GROUP
        assert flags & windows_process.CREATE_NO_WINDOW


def test_windows_job_containment_identity_is_strict():
    assert process_containment.validate_containment(windows_job())['kind'] == 'windows-job'
    for broken in (
        {**windows_job(), 'weakerDescendantContainment': True},
        {**windows_job(), 'identity': {'pid': 1}},
        {**windows_job(), 'extra': 1},
    ):
        with pytest.raises(runtime.ContractError):
            process_containment.validate_containment(broken)


@pytest.mark.parametrize('status, empty', [('dead', True), ('match', False)])
def test_windows_job_is_empty_exactly_when_its_root_is_dead(status, empty):
    with mock.patch.object(process_containment, 'WINDOWS', True), \
            mock.patch.object(process_containment, 'process_identity_status', return_value=status), \
            mock.patch.object(process_containment, 'process_group_exists', side_effect=AssertionError('no group query')):
        assert process_containment.containment_is_empty(windows_job()) is empty


def test_windows_job_mismatch_fails_closed():
    with mock.patch.object(process_containment, 'WINDOWS', True), \
            mock.patch.object(process_containment, 'process_identity_status', return_value='mismatch'):
        with pytest.raises(runtime.ContractError):
            process_containment.containment_is_empty(windows_job())


def test_windows_stop_terminates_only_the_verified_root():
    statuses = iter(['match', 'dead'])
    with mock.patch.object(process_containment, 'WINDOWS', True), \
            mock.patch.object(process_containment, 'windows_process', create=True) as native, \
            mock.patch.object(process_containment, 'process_identity_status', side_effect=lambda _identity: next(statuses)), \
            mock.patch.object(process_containment.os, 'killpg', side_effect=AssertionError('no POSIX signal'), create=True):
        process_containment.request_containment_stop(windows_job())
    native.terminate_identity.assert_called_once_with(IDENTITY)


def test_windows_stop_refuses_an_unverified_identity():
    with mock.patch.object(process_containment, 'WINDOWS', True), \
            mock.patch.object(process_containment, 'windows_process', create=True) as native, \
            mock.patch.object(process_containment, 'process_identity_status', return_value='unknown'):
        with pytest.raises(runtime.ContractError):
            process_containment.force_containment_stop(windows_job())
    native.terminate_identity.assert_not_called()


def test_windows_pid_probe_never_uses_os_kill():
    # os.kill(pid, 0) terminates the target on Windows.
    with mock.patch.object(portable, 'WINDOWS', True), \
            mock.patch.object(process_containment, 'process_group_exists', return_value=True) as exists, \
            mock.patch.object(runtime.os, 'kill', side_effect=AssertionError('os.kill on Windows')):
        assert runtime.pid_alive(4242) is True
    exists.assert_called_once_with(4242)


def test_fallback_worker_binds_windows_job_containment(tmp_path):
    process = mock.Mock(pid=IDENTITY['pid'])
    recorded = {}

    def update(_path, _lock, change):
        state = {}
        change(state)
        recorded.update(state)

    with mock.patch.object(portable, 'WINDOWS', True), \
            mock.patch.object(runtime, 'spawn_contained_process', return_value=(process, IDENTITY, 7)) as spawn, \
            mock.patch.object(runtime, 'release_contained_process'), \
            mock.patch.object(runtime, 'state_file', return_value=tmp_path / 'state.json'), \
            mock.patch.object(runtime, 'update_json', side_effect=update):
        runtime._launch_fallback_worker(tmp_path, 'agent', 'run', ['worker'])
    assert spawn.call_args.kwargs['detach'] is True
    assert recorded['containment'] == windows_job()
    process_containment.validate_containment(recorded['containment'])


def test_portable_lock_is_exclusive_and_bounded(tmp_path):
    path = tmp_path / 'lock'
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        portable.lock_descriptor(first)
        with pytest.raises(BlockingIOError):
            portable.lock_descriptor(second, blocking=False)
        with pytest.raises(BlockingIOError):
            portable.lock_descriptor(second, timeout=0.05)
        portable.unlock_descriptor(first)
        portable.lock_descriptor(second, blocking=False)
    finally:
        os.close(first)
        os.close(second)


def test_npm_codex_shim_resolves_to_the_vendored_native_binary(tmp_path):
    shim = tmp_path / 'codex.cmd'
    shim.write_text('@echo off\n')
    native = tmp_path / 'node_modules/@openai/codex/vendor/x86_64-pc-windows-msvc/codex/codex.exe'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'MZ')
    with mock.patch.object(portable, 'WINDOWS', True), mock.patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64'}):
        assert portable.native_executable(str(shim)) == str(native)
        assert portable.native_executable(str(native)) == str(native)
    assert portable.native_executable(str(shim)) == str(shim)  # POSIX hosts are unchanged.


def test_shim_without_vendored_binary_is_kept(tmp_path):
    shim = tmp_path / 'codex.cmd'
    shim.write_text('@echo off\n')
    with mock.patch.object(portable, 'WINDOWS', True):
        assert portable.native_executable(str(shim)) == str(shim)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows lifecycle regression')
def test_native_windows_job_kills_descendants_when_root_is_terminated(tmp_path):
    import subprocess
    marker = tmp_path / 'grandchild.pid'
    grandchild = ("import os,time,pathlib;pathlib.Path(r'%s').write_text(str(os.getpid()));time.sleep(60)" % marker)
    child = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',%r]);time.sleep(60)" % grandchild
    process, identity, release_fd = process_containment.spawn_contained_process(
        [sys.executable, '-c', child], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process_containment.release_contained_process(process, identity, release_fd)
    containment = windows_job(identity)
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild_pid = int(marker.read_text())
    process_containment.request_containment_stop(containment)
    assert process_containment.wait_containment_empty(containment, 5)
    deadline = time.monotonic() + 5
    while windows_process.pid_exists(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not windows_process.pid_exists(grandchild_pid)


@pytest.mark.parametrize('layout', [
    'node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe',
    'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe',
    'node_modules/@openai/codex/vendor/x86_64-pc-windows-msvc/codex/codex.exe',
])
def test_npm_codex_shim_resolves_current_hoisted_and_legacy_layouts(tmp_path, layout):
    shim = tmp_path / 'codex.cmd'
    shim.write_text('@echo off\n')
    (tmp_path / 'node_modules/@openai/codex').mkdir(parents=True)
    native = tmp_path / layout
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b'MZ')
    with mock.patch.object(portable, 'WINDOWS', True), mock.patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64'}):
        assert portable.native_executable(str(shim)) == str(native)


def test_pnpm_shim_is_followed_to_its_store_and_claude_shim_to_claude_exe(tmp_path):
    pnpm = tmp_path / 'pnpm'
    package = pnpm / 'global/5/node_modules/@openai/codex'
    native = package / 'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'MZ')
    (pnpm / 'codex.cmd').write_text('@"%~dp0\\global\\5\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    npm = tmp_path / 'npm'
    claude = npm / 'node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    claude.parent.mkdir(parents=True)
    claude.write_bytes(b'MZ')
    (npm / 'claude.cmd').write_text('@echo off\n')
    with mock.patch.object(portable, 'WINDOWS', True), mock.patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64'}):
        assert portable.native_executable(str(pnpm / 'codex.cmd')) == str(native)
        assert portable.native_executable(str(npm / 'claude.cmd'), 'claude') == str(claude)


def test_unknown_future_codex_layout_is_found_by_search(tmp_path):
    shim = tmp_path / 'codex.cmd'
    shim.write_text('@echo off\n')
    native = tmp_path / 'node_modules/@openai/codex/dist/platforms/win/x86_64/codex.exe'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'MZ')
    with mock.patch.object(portable, 'WINDOWS', True), mock.patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64'}):
        assert portable.native_executable(str(shim)) == str(native)


def test_find_cli_falls_back_to_user_install_locations_when_path_is_minimal(tmp_path, monkeypatch):
    if portable.WINDOWS:
        pytest.skip('POSIX discovery')
    local = tmp_path / '.local/bin'
    local.mkdir(parents=True)
    for name in ('codex', 'claude'):
        (local / name).write_text('#!/bin/sh\n')
        (local / name).chmod(0o755)
    monkeypatch.setenv('PATH', '/nonexistent')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(portable, '_login_shell_path', lambda: [])
    assert portable.find_cli('codex') == str(local / 'codex')
    assert portable.find_cli('claude') == str(local / 'claude')
    assert portable.find_cli('definitely-missing-cli') is None
