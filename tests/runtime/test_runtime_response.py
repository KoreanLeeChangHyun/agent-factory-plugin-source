"""Response delivery exercises the real attempt boundary without model file I/O."""
import runtime_test_home
import io
import json
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from contextlib import redirect_stdout
from native_fixtures import runtime as rt, native_fixture


class CapturedInput(io.StringIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class RuntimeResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def new_run(self, role='main', request=b'bounded response', **options):
        return rt.create_run(project_root=self.root, agent_id=role+'-response', actor='main',
                             request=request, session={'role': role, 'maxAttempts': 1}, **options)

    RECEIPT_FIELDS = {'outcome': 'completed', 'changedPaths': ['src/a.py'], 'addressedFindingIds': [],
                      'tests': {'run': True, 'reason': 'unit tests passed'}}

    def envelope(self, state, **values):
        return {'status': 'completed', 'resultPath': state['resultPath'],
                'resultText': '안녕하세요!\n**Answer**\n', **values}

    def attempt(self, state, terminal, *, return_code=0, backend=None, usage_events=(), sandbox='workspace-write'):
        events = [{'type': 'thread.started', 'thread_id': 'session-response'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Progress only'}},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(terminal)}}]
        events.extend(usage_events)
        self.process_input = CapturedInput()
        process = Mock(pid=101, stdin=self.process_input, stderr=io.StringIO(),
                       stdout=io.StringIO(''.join(json.dumps(e)+'\n' for e in events)))
        process.wait.return_value = return_code
        session = {'codex': 'codex', 'role': state['role'], 'projectRoot': str(self.root),
                   'executionPolicy': runtime_test_home.policy(sandbox, self.root),
                   'sessionId': 'session-response', 'startTimeout': 5, 'turnTimeout': 5}
        if backend:
            session['backend'] = backend
        rt.atomic_write_json(rt.session_file(self.root, state['agentId']), session)
        identity = {'pid': 101, 'bootId': 'fixture', 'startTicks': 7}
        with patch.object(rt.execution_preflight, 'check', return_value={'passed': True}), \
             patch.object(rt.native_codex, 'inspect_capabilities', return_value={'send': {'goal': False, 'fast': False, 'plan': False}}), \
             patch.object(rt, 'spawn_contained_process', return_value=(process, identity, 55)), \
             patch.object(rt, 'release_contained_process'), patch.object(rt, 'terminate_attempt_group'):
            return rt.run_codex_attempt(project_root=self.root, session=session, state=state,
                attempt=1, heartbeat=Mock(), cancel_event=threading.Event(),
                expected_agent_id=state['agentId'], expected_run_id=state['runId'])

    def test_attempt_persists_reported_usage_even_when_process_fails(self):
        for return_code in (0, 1):
            with self.subTest(return_code=return_code):
                state = self.new_run()
                event = {"type": "turn.completed", "usage": {
                    "input_tokens": 900, "cached_input_tokens": 700, "output_tokens": 50}}
                if return_code:
                    with self.assertRaises(rt.AttemptFailure):
                        self.attempt(state, self.envelope(state), return_code=return_code, usage_events=[event])
                else:
                    self.attempt(state, self.envelope(state), usage_events=[event])
                saved = rt.public_state(rt.safe_read_json(Path(state["statePath"])))
                self.assertEqual(saved["tokenUsage"]["inputTokens"], 900)
                self.assertEqual(saved["tokenUsage"]["cachedInputTokens"], 700)
                self.assertEqual(saved["tokenUsage"]["outputTokens"], 50)
                self.assertIsNone(saved["tokenUsage"]["reasoningOutputTokens"])
                self.assertEqual(saved["usageAttempts"]["1"]["reports"], 1)

    def test_native_attempt_sends_structured_parts_with_current_inline_request(self):
        from execution.prompts import PromptParts
        state = self.new_run(request=b'current native request')
        self.attempt(state, self.envelope(state), backend='app-server')
        parts = PromptParts.decode(self.process_input.sent)
        self.assertIn('agent-factory-role-prompt', parts.fixed)
        self.assertNotIn(state['runId'], parts.fixed)
        self.assertNotIn(state['requestPath'], parts.fixed)
        self.assertIn('current native request', parts.dynamic)
        self.assertIn(state['resultPath'], parts.dynamic)
        self.assertNotIn('agent-factory-role-prompt', parts.dynamic)

    def test_agent_lesson_inputs_beside_captures_do_not_fail_a_finished_run(self):
        # Incident: finished runs failed lesson_recording_incomplete over the Agent's own CLI inputs.
        state = self.new_run()
        directory = Path(state['statePath']).parent / 'lesson-capture'
        directory.mkdir()
        (directory / 'resolve-item_8.json').write_text(json.dumps({'id': 'runtime-x', 'cause': 'c'}))
        (directory / 'audit.json').write_text(json.dumps({'occurrenceIds': []}))
        self.assertEqual(self.attempt(state, self.envelope(state)), ('completed', 'session-response'))

    def pending(self, state):
        return rt.public_state(rt.safe_read_json(Path(state['statePath'])))['pendingLessons']

    def writable(self, agent='writer'):
        state = rt.create_run(project_root=self.root, agent_id=agent + '-response', actor='main',
                              request=b'later writable run', session={'role': 'main', 'maxAttempts': 1})
        return {**state, 'executionPolicy': runtime_test_home.policy('workspace-write', self.root)}

    def test_unsaved_runtime_capture_never_fails_the_run_and_is_counted(self):
        # Human decision 2026-10-03: captures keep their scope but no longer gate completion.
        state = self.new_run()
        directory = Path(state['statePath']).parent / 'lesson-capture'
        directory.mkdir()
        (directory / ('0' * 24 + '.json')).write_text('{}')
        response = self.envelope(state)
        with patch.object(rt.lesson_capture, 'replay', side_effect=OSError('lesson storage is unavailable')):
            self.assertEqual(self.attempt(state, response), ('completed', 'session-response'))
        self.assertEqual(Path(state['resultPath']).read_text(), response['resultText'])
        self.assertEqual(self.pending(state), 1)

    def test_read_only_run_with_a_failing_command_completes_and_a_later_run_records_it_once(self):
        state = self.new_run()
        state['executionPolicy'] = runtime_test_home.policy('read-only', self.root)
        failing = {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'command-1', 'exit_code': 1,
                                                      'command': 'secret=DO_NOT_SAVE'}}
        self.assertEqual(self.attempt(state, self.envelope(state), usage_events=[failing], sandbox='read-only'),
                         ('completed', 'session-response'))
        self.assertFalse((self.root / 'docs').exists())  # A read-only run never writes the project.
        self.assertEqual(self.pending(state), 1)
        self.assertEqual(rt.lesson_capture.apply_pending(self.root, state), 0)  # Nor does it apply captures.
        self.assertFalse((self.root / 'docs').exists())

        writer = self.writable()
        self.assertEqual(rt.lesson_capture.apply_pending(self.root, writer), 1)
        records = list((self.root / 'docs/lessons-learned').glob('*.json'))
        self.assertEqual(len(records), 1)
        self.assertNotIn('DO_NOT_SAVE', records[0].read_text())
        self.assertEqual(self.pending(state), 0)
        # Idempotent: nothing is pending now, and recording the same occurrence again adds nothing.
        with patch.object(rt.lesson_capture.subprocess, 'run') as cli:
            self.assertEqual(rt.lesson_capture.apply_pending(self.root, writer), 0)
        cli.assert_not_called()
        for receipt in (Path(state['statePath']).parent / 'lesson-capture').glob('*.receipt'):
            receipt.unlink()
        self.assertEqual(rt.lesson_capture.apply_pending(self.root, self.writable('second')), 1)
        self.assertEqual(len(list((self.root / 'docs/lessons-learned').glob('*.json'))), 1)
        self.assertEqual(len(json.loads(records[0].read_text())['occurrences']), 1)

    def test_pending_capture_sweep_is_bounded_and_keeps_failures_pending(self):
        capture = rt.lesson_capture
        state = self.new_run()
        state['executionPolicy'] = runtime_test_home.policy('read-only', self.root)
        for index in range(3):
            capture.observe(self.root, state, {'type': 'item.completed', 'item': {
                'type': 'command_execution', 'id': f'command-{index}', 'exit_code': 2}}, 1)
        self.assertEqual(self.pending(state), 3)
        writer = self.writable()
        with patch.object(capture, 'APPLY_LIMIT', 2):
            self.assertEqual(capture.apply_pending(self.root, writer), 2)  # Count bound; the rest waits.
        self.assertEqual(self.pending(state), 1)
        with patch.object(capture.subprocess, 'run') as cli:
            self.assertEqual(capture.apply_pending(self.root, writer, clock=iter([0.0, capture.APPLY_BUDGET_SECONDS]).__next__), 0)
        cli.assert_not_called()  # Time bound.
        failure = subprocess.CompletedProcess([], 1, '', 'lesson storage is unavailable')
        with patch.object(capture.subprocess, 'run', return_value=failure) as cli:
            for _ in range(capture.APPLY_ATTEMPTS + 2):
                self.assertEqual(capture.apply_pending(self.root, writer), 0)
        self.assertEqual(cli.call_count, capture.APPLY_ATTEMPTS)  # Attempt bound; it stays pending.
        self.assertEqual(self.pending(state), 1)
        with patch.object(capture.subprocess, 'run', side_effect=subprocess.TimeoutExpired('lessons.py', 1)):
            other = self.new_run(role='verification')
            other['executionPolicy'] = runtime_test_home.policy('read-only', self.root)
            capture.observe(self.root, other, {'type': 'runtime.failure', 'code': 'launch_failed'}, 1)
            self.assertEqual(capture.apply_pending(self.root, writer), 0)  # A hung write is not an error.
        self.assertEqual(self.pending(other), 1)

    def test_run_prompt_ends_with_role_completion_checks(self):
        directory = self.root / 'prompt'
        directory.mkdir()
        schema = rt.receipt_schema_document(role='work', run_id='run-one', request_hash='a' * 64, verified_work_run_id=None)
        (directory / 'receipt.schema.json').write_text(json.dumps(schema), encoding='utf-8')

        def dynamic(role, mode, receipt=True):
            return rt.build_prompt_parts(
                agent_id=role + '-agent', role=role, request_path=Path('/managed/request.md'),
                result_path=Path('/managed/result.md'), run_id='run-one', task_mode=mode,
                receipt_path=directory / 'receipt.json' if receipt else None,
                receipt_schema_path=directory / 'receipt.schema.json' if receipt else None).dynamic

        work = dynamic('work', 'work')
        checks = work[work.index('Before you return `completed` for this Work run'):]
        self.assertTrue(work.endswith(checks))  # the last words the Agent reads
        for expected in ('The receipt file above exists', 'recorded with the lesson CLI',
                         'never report a Verification pass', 'do not wait for them'):
            self.assertIn(expected, checks)
        self.assertIn('"runId": "run-one"', work)
        self.assertIn('"requestHash": "' + 'a' * 64 + '"', work)
        self.assertIn('"kind": "work-receipt"', work)
        self.assertNotIn('The receipt file above exists', dynamic('work', 'work', receipt=False))
        self.assertNotIn('Before you return', dynamic('work', 'plan'))
        verification = dynamic('verification', 'work-verification')
        self.assertIn('for this Verification run', verification)
        self.assertNotIn('never report a Verification pass', verification)
        self.assertNotIn('Before you return', dynamic('main', 'orchestrate', receipt=False))
        # A missing or unreadable schema never blocks prompt delivery.
        missing = rt.build_prompt_parts(agent_id='work-agent', role='work', request_path=Path('request.md'),
            result_path=Path('result.md'), run_id='run-one', receipt_path=Path('receipt.json'),
            receipt_schema_path=self.root / 'absent.schema.json').dynamic
        self.assertNotIn('fixed for this run', missing)
        self.assertIn('The receipt file above exists', missing)

    def test_attempt_persists_answer_without_model_file_writes(self):
        for status in ('completed', 'needs-human-decision', 'failed'):
            with self.subTest(status=status):
                state = self.new_run()
                response = self.envelope(state, status=status)
                self.assertFalse(Path(state['resultPath']).exists())
                self.assertEqual(self.attempt(state, response), (status, 'session-response'))
                self.assertEqual(Path(state['resultPath']).read_text(), response['resultText'])
                self.assertEqual(stat.S_IMODE(Path(state['resultPath']).stat().st_mode), 0o600)

    def test_main_attempt_delivers_exact_request_and_retains_record(self):
        request = '  안녕하세요!\r\n`code` <tag> & 요청\n'.encode('utf-8')
        state = self.new_run(request=request)
        self.attempt(state, self.envelope(state))
        prompt = self.process_input.sent
        self.assertIn('<agent-factory-request>\n' + request.decode('utf-8') +
                      '\n</agent-factory-request>', prompt)
        self.assertNotIn('Read the delegated request from', prompt)
        self.assertEqual(Path(state['requestPath']).read_bytes(), request)

    def test_inline_request_byte_boundary_and_managed_role_compatibility(self):
        for role, request, inline in (
            ('main', b'a' * (64 * 1024), True),
            ('main', b'a' * (64 * 1024 + 1), False),
            ('main', ('가' * 22000).encode('utf-8'), False),
            ('work', b'bounded work', False),
            ('verification', b'bounded verification', False),
        ):
            with self.subTest(role=role, size=len(request)):
                prompt = rt.build_prompt(agent_id='test-agent', role=role,
                    request_path=Path('/managed/request.md'), result_path=Path('/managed/result.md'),
                    run_id='run-one', request=request)
                self.assertEqual('<agent-factory-request>' in prompt, inline)
                self.assertEqual('Read the delegated request from' in prompt, not inline)

    def test_changed_request_is_rejected_before_delivery(self):
        state = self.new_run()
        Path(state['requestPath']).write_bytes(b'changed request')
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state))
        self.assertEqual(raised.exception.code, 'request_changed')
        self.assertEqual(self.process_input.getvalue(), '')

    def test_failed_process_does_not_publish_even_valid_answer(self):
        state = self.new_run()
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state), return_code=1)
        self.assertEqual(raised.exception.code, 'codex_failed')
        self.assertFalse(Path(state['resultPath']).exists())

    def test_invalid_envelopes_do_not_publish_or_fall_back_to_file(self):
        state = self.new_run()
        invalid = [self.envelope(state, resultText=value) for value in
                   ('', ' \n', None, 42, '\ud800')]
        invalid += [self.envelope(state, status=[]), self.envelope(state, resultPath='/tmp/wrong'),
                    self.envelope(state, extra=True), {'status':'completed', 'resultPath':state['resultPath']}]
        for response in invalid:
            with self.subTest(response_keys=list(response)):
                with self.assertRaises(rt.ContractError):
                    rt.publish_terminal_result(response, state)
                self.assertFalse(Path(state['resultPath']).exists())

    def test_publish_refuses_symlink_and_nonregular_destination(self):
        state = self.new_run()
        path = Path(state['resultPath'])
        target = self.root / 'unrelated.txt'
        target.write_text('keep')
        path.symlink_to(target)
        with self.assertRaises(rt.ContractError):
            rt.publish_terminal_result(self.envelope(state), state)
        self.assertEqual(target.read_text(), 'keep')
        path.unlink()
        path.mkdir()
        with self.assertRaises(rt.ContractError):
            rt.publish_terminal_result(self.envelope(state), state)

    def test_legacy_schema_preserves_file_and_rejects_new_envelope(self):
        state = self.new_run()
        rt.atomic_write_json(Path(state['responseSchemaPath']),
                             rt.response_schema_document(state['resultPath'], inline=False))
        Path(state['resultPath']).write_text('legacy answer')
        terminal = {'status': 'completed', 'resultPath': state['resultPath']}
        self.assertEqual(self.attempt(state, terminal)[0], 'completed')
        self.assertEqual(Path(state['resultPath']).read_text(), 'legacy answer')
        with self.assertRaises(rt.ContractError):
            rt.publish_terminal_result(self.envelope(state), state)
        schema = rt.safe_read_json(Path(state['responseSchemaPath']))
        schema['additionalProperties'] = True
        rt.atomic_write_json(Path(state['responseSchemaPath']), schema)
        with self.assertRaises(rt.ContractError):
            rt.validate_terminal_result(terminal, state)

    def test_structured_work_run_gets_its_receipt_written_by_the_runtime(self):
        state = self.new_run('work')
        self.assertEqual(state['responseContract'], 2)
        self.assertEqual(rt.public_state(state)['responseContract'], 2)
        schema = rt.safe_read_json(Path(state['responseSchemaPath']))
        self.assertEqual(schema['required'], ['status', 'resultPath', 'resultText', 'decisionKind',
                                              'outcome', 'changedPaths', 'tests', 'addressedFindingIds'])
        # Flat and small: only keywords every provider's output schema accepts.
        self.assertNotIn('pattern', json.dumps(schema))
        self.assertNotIn('uniqueItems', json.dumps(schema))
        terminal = self.envelope(state, decisionKind=None, **self.RECEIPT_FIELDS)
        self.assertEqual(self.attempt(state, terminal)[0], 'completed')
        # The prompt tells the Agent to return the fields, never to write the file.
        self.assertIn('do not write one yourself', self.process_input.sent)
        self.assertNotIn(state['receiptPath'], self.process_input.sent)
        self.assertEqual(rt.safe_read_json(Path(state['receiptPath'])), {
            'schemaVersion': '0.1.0', 'kind': 'work-receipt', 'runId': state['runId'],
            'requestHash': state['requestHash'], **self.RECEIPT_FIELDS})
        self.assertEqual(Path(state['resultPath']).read_text(encoding='utf-8'), terminal['resultText'])
        self.assertEqual(rt.validate_receipt(self.root, rt.safe_read_json(Path(state['statePath'])),
                                             agent_id=state['agentId'], run_id=state['runId'])['changedPaths'], ['src/a.py'])

    def test_structured_work_run_without_receipt_fields_fails_as_receipt_missing(self):
        # The runtime never fills judgment values: an omitted field leaves no receipt and a recoverable code.
        for omitted in (self.RECEIPT_FIELDS, {'tests': 1}, {'changedPaths': 1}):
            with self.subTest(omitted=sorted(omitted)):
                state = self.new_run('work')
                fields = {key: value for key, value in self.RECEIPT_FIELDS.items() if key not in omitted}
                with self.assertRaises(rt.AttemptFailure) as raised:
                    self.attempt(state, self.envelope(state, **fields))
                self.assertEqual(raised.exception.code, 'receipt_missing')
                self.assertFalse(Path(state['receiptPath']).exists())
                # The answer survives as evidence for the recovery turn.
                self.assertTrue(Path(state['resultPath']).read_text(encoding='utf-8').strip())

    def test_structured_receipt_is_validated_by_the_existing_rules_before_it_is_written(self):
        cases = (({'changedPaths': ['/etc/passwd']}, 'receipt_path_contract_invalid'),
                 ({'changedPaths': ['../outside']}, 'receipt_path_contract_invalid'),
                 ({'changedPaths': ['a', 'a']}, 'receipt_invalid'),
                 ({'tests': {'run': True, 'reason': '  '}}, 'receipt_tests_invalid'),
                 ({'tests': {'run': 'yes', 'reason': 'ran'}}, 'receipt_tests_invalid'),
                 ({'outcome': 'verified'}, 'receipt_binding_invalid'),
                 ({'addressedFindingIds': 'finding-1'}, 'receipt_invalid'))
        for override, code in cases:
            with self.subTest(override=override):
                state = self.new_run('work')
                with self.assertRaises(rt.AttemptFailure) as raised:
                    self.attempt(state, self.envelope(state, **{**self.RECEIPT_FIELDS, **override}))
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(Path(state['receiptPath']).exists())

    def test_structured_receipt_fields_are_ignored_unless_the_run_completed(self):
        for status in ('needs-human-decision', 'failed'):
            for fields in ({}, self.RECEIPT_FIELDS):
                with self.subTest(status=status, fields=bool(fields)):
                    state = self.new_run('work')
                    terminal = self.envelope(state, status=status, **fields)
                    self.assertEqual(self.attempt(state, terminal)[0], status)
                    self.assertFalse(Path(state['receiptPath']).exists())
        state = self.new_run('work')
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state, unknown=True, **self.RECEIPT_FIELDS))
        self.assertEqual(raised.exception.code, 'result_invalid')

    def test_response_contract_is_captured_per_run_and_only_where_the_schema_reaches_the_final_turn(self):
        def create(provider, role='work', **options):
            return rt.create_run(project_root=self.root, agent_id=f'{role}-{provider}', actor='main', request=b'bounded',
                                 session={'role': role, 'maxAttempts': 1, 'provider': provider}, **options)
        goal = {'taskMode': 'work', 'goalMode': True}
        for provider in ('claude', 'antigravity', 'codex'):
            self.assertEqual(create(provider, execution_options=goal)['responseContract'], 2, provider)
        # A Codex Goal turn takes no per-turn outputSchema; its adapter asks once more in a turn that does.
        self.assertEqual(create('codex', goal_action='resume')['responseContract'], 2)
        self.assertEqual(create('codex', execution_options={'taskMode': 'work', 'goalMode': False})['responseContract'], 2)
        # The host records a plan-only receipt itself; a caller's captured file contract is kept.
        self.assertNotIn('responseContract', create('claude', execution_options={'taskMode': 'plan', 'goalMode': False}))
        self.assertNotIn('responseContract', create('claude', execution_options=goal, response_contract=1))
        self.assertEqual(create('claude', execution_options=goal, response_contract=2)['responseContract'], 2)
        self.assertEqual(create('codex', execution_options=goal, response_contract=2)['responseContract'], 2)
        self.assertNotIn('responseContract', create('codex', execution_options=goal, response_contract=1))
        self.assertNotIn('responseContract', create('codex', execution_options={'taskMode': 'plan', 'goalMode': True}))
        for role in ('main', 'verification'):
            state = create('claude', role=role)
            self.assertNotIn('responseContract', state)
            self.assertEqual(rt.safe_read_json(Path(state['responseSchemaPath'])),
                             rt.response_schema_document(state['resultPath']))
        with self.assertRaises(rt.ContractError) as raised:
            create('claude', execution_options=goal, response_contract=3)
        self.assertEqual(raised.exception.code, 'response_contract_invalid')

    def test_capability_bound_work_keeps_the_file_contract(self):
        binding = {"schemaVersion": "0.1.0", "kind": "capability-bindings", "bindings": []}
        with patch.object(rt, 'validate_capability_bindings', return_value={"bindings": []}):
            state = rt.create_run(project_root=self.root, agent_id='work-bound', actor='main', request=b'bounded',
                                  session={'role': 'work', 'maxAttempts': 1, 'provider': 'claude'},
                                  capability_bindings=json.dumps(binding).encode())
        self.assertNotIn('responseContract', state)

    def test_structured_run_keeps_its_contract_when_field_descriptions_change_later(self):
        state = self.new_run('work')
        schema = rt.safe_read_json(Path(state['responseSchemaPath']))
        schema['properties']['changedPaths']['description'] = 'reworded by a later runtime'
        rt.atomic_write_json(Path(state['responseSchemaPath']), schema)
        self.assertTrue(rt.inline_result(state))
        del schema['properties']['tests']
        rt.atomic_write_json(Path(state['responseSchemaPath']), schema)
        with self.assertRaises(rt.ContractError) as raised:
            rt.inline_result(state)
        self.assertEqual(raised.exception.code, 'result_schema_invalid')
        state['responseContract'] = 3
        with self.assertRaises(rt.ContractError) as raised:
            rt.structured_receipt(state)
        self.assertEqual(raised.exception.code, 'response_contract_invalid')

    def test_old_contract_work_run_finishes_unchanged_with_the_agent_written_receipt(self):
        # A run created before the contract field: no `responseContract`, the former schema and prompt.
        state = self.new_run('work', response_contract=1)
        self.assertNotIn('responseContract', state)
        self.assertNotIn('responseContract', rt.public_state(state))
        self.assertEqual(rt.safe_read_json(Path(state['responseSchemaPath'])),
                         rt.response_schema_document(state['resultPath']))
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state))
        self.assertEqual(raised.exception.code, 'receipt_missing')
        self.assertIn(f"also write the role-specific machine receipt to\n`{state['receiptPath']}`", self.process_input.sent)
        # Receipt fields belong to contract 2 only; the old envelope stays exact.
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state, **self.RECEIPT_FIELDS))
        self.assertEqual(raised.exception.code, 'result_invalid')
        self.assertFalse(Path(state['receiptPath']).exists())
        rt.atomic_write_json(Path(state['receiptPath']), {
            'schemaVersion': '0.1.0', 'kind': 'work-receipt', 'runId': state['runId'],
            'requestHash': state['requestHash'], 'outcome': 'implemented', 'changedPaths': [],
            'addressedFindingIds': [], 'tests': {'run': False, 'reason': 'not needed'}})
        self.assertEqual(self.attempt(state, self.envelope(state))[0], 'completed')
        self.assertEqual(rt.safe_read_json(Path(state['receiptPath']))['outcome'], 'implemented')

    def test_completed_work_still_requires_bound_receipt(self):
        state = self.new_run('work', response_contract=1)
        with self.assertRaises(rt.AttemptFailure) as raised:
            self.attempt(state, self.envelope(state))
        self.assertIn('receipt', raised.exception.code)
        rt.atomic_write_json(Path(state['receiptPath']), {
            'schemaVersion': '0.1.0', 'kind': 'work-receipt', 'runId': state['runId'],
            'requestHash': state['requestHash'], 'outcome': 'completed', 'changedPaths': [],
            'addressedFindingIds': [], 'tests': {'run': False, 'reason': 'work-agent-prohibited'}})
        self.assertEqual(self.attempt(state, self.envelope(state))[0], 'completed')

    def test_new_prompt_returns_text_and_legacy_prompt_retains_old_contract(self):
        for inline in (True, False):
            prompt = rt.build_prompt(agent_id='main-response', role='main',
                request_path=Path('/managed/request.md'), result_path=Path('/managed/result.md'),
                run_id='run-one', inline_response=inline)
            if inline:
                self.assertIn('Do not write or reread your answer file', prompt)
                self.assertIn('`resultText`', prompt)
                self.assertNotIn('Write the detailed result to', prompt)
            else:
                self.assertIn('Write the detailed result to', prompt)

    def test_native_final_and_goal_control_use_runtime_persistence(self):
        for action in (None, 'clear'):
            with self.subTest(action=action), redirect_stdout(io.StringIO()) as output:
                bridge, _, state = native_fixture(self.root, goal=action is not None, action=action)
                bridge.run('bounded request')
                self.assertFalse(Path(state['resultPath']).exists())
                messages = [event['item']['text'] for line in output.getvalue().splitlines()
                            if (event := json.loads(line)).get('type') == 'item.completed'
                            and event.get('item', {}).get('type') == 'agent_message']
                terminal = json.loads(messages[-1])
                rt.publish_terminal_result(terminal, state)
                self.assertEqual(Path(state['resultPath']).read_text(), terminal['resultText'])
                self.assertEqual(terminal['status'], 'needs-human-decision' if action else 'completed')

    def test_invalid_schema_or_prompt_fails_before_child_launch(self):
        for failure in ('schema', 'prompt'):
            with self.subTest(failure=failure):
                state = self.new_run()
                if failure == 'schema':
                    schema = rt.safe_read_json(Path(state['responseSchemaPath']))
                    schema['additionalProperties'] = True
                    rt.atomic_write_json(Path(state['responseSchemaPath']), schema)
                session = {'codex': 'codex', 'role': 'main', 'projectRoot': str(self.root),
                           'executionPolicy': runtime_test_home.policy('workspace-write', self.root)}
                prompt_error = rt.ContractError('role_invalid', 'missing prompt') if failure == 'prompt' else None
                with patch.object(rt, 'build_prompt_parts', side_effect=prompt_error, wraps=rt.build_prompt_parts), \
                     patch.object(rt.execution_preflight, 'check') as preflight, \
                     patch.object(rt, 'spawn_contained_process') as spawn, \
                     patch.object(rt, 'release_contained_process') as release:
                    with self.assertRaises(rt.AttemptFailure) as raised:
                        rt.run_codex_attempt(project_root=self.root, session=session, state=state,
                            attempt=1, heartbeat=Mock(), cancel_event=threading.Event(),
                            expected_agent_id=state['agentId'], expected_run_id=state['runId'])
                    self.assertEqual(raised.exception.code, 'result_schema_invalid' if failure == 'schema' else 'role_invalid')
                    self.assertFalse(raised.exception.launched)
                    self.assertFalse(raised.exception.started)
                    preflight.assert_not_called()
                    spawn.assert_not_called()
                    release.assert_not_called()
