import runtime_test_home  # noqa: F401 - imported for its side effect: isolates the runtime home
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from test_agent_exec import load_module


class TaskBindingTests(unittest.TestCase):
    def test_large_brief_retains_scope_completion_and_multibyte_tail(self):
        from tasks.binding import brief_document, resolve
        request = '# Goal\nSame result\n# Scope\n' + '한글😀 dependency\n' * 10000 + '# Done\nKeep tail permission and required checks.'
        document = brief_document(request)
        self.assertEqual(document['tasks'][0]['description'], request)
        _, binding = resolve(document, document['tasks'][0]['id'], hashlib.sha256(request.encode()).hexdigest())
        self.assertEqual(binding['description'], request)

    def setUp(self):
        self.runtime = load_module()
        capability = mock.patch.object(self.runtime.native_codex, "inspect_capabilities",
            return_value={"submit": {"goal": True}, "send": {"goal": True}, "diagnostic": None})
        capability.start()
        self.addCleanup(capability.stop)
        from tasks import binding as task_binding
        self.binding = task_binding
        self.request = 'Implement the named change'
        self.digest = hashlib.sha256(self.request.encode()).hexdigest()
        self.document = {'id': 'flow-one', 'title': 'User changes', 'tasks': [
            {'id': 'task-one', 'title': 'Fix scroll', 'description': self.request,
             'completionCriteria': 'Scroll remains at the bottom', 'requestHash': self.digest}]}

    def test_rejects_missing_fields_unknown_task_and_mismatched_request(self):
        for key in ('id', 'title', 'description', 'completionCriteria', 'requestHash'):
            document = json.loads(json.dumps(self.document))
            del document['tasks'][0][key]
            with self.subTest(key=key), self.assertRaises(self.runtime.ContractError):
                self.binding.validate(document, 'task-one', self.digest)
        for task, digest in [('missing', self.digest), ('task-one', '0' * 64)]:
            with self.assertRaises(self.runtime.ContractError):
                self.binding.validate(self.document, task, digest)

    def test_submit_blocks_before_launch_and_persists_accepted_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.runtime.parse_args(['submit', '--project-root', directory,
                '--agent', 'work-task', '--role', 'work', '--codex', '/bin/true', '--message', self.request])
            with mock.patch.object(self.runtime, 'spawn_worker', return_value=123) as spawn, mock.patch.object(self.runtime, 'emit') as emit:
                with self.assertRaises(self.runtime.ContractError) as error:
                    self.runtime.submit(args, True)
                self.assertEqual(error.exception.code, 'task_binding_required')
                spawn.assert_not_called()
                path = root / 'tasks.json'
                path.write_text(json.dumps(self.document))
                self.document['tasks'][0]['allocation'] = self.allocation()
                path.write_text(json.dumps(self.document))
                args.task_list_file, args.task_id = path, 'task-one'
                self.runtime.submit(args, True)
                spawn.assert_called_once()
                state = json.loads(Path(emit.call_args.args[0]['statePath']).read_text())
                self.assertEqual(state['taskBinding']['taskId'], 'task-one')
                self.assertEqual(state['taskBinding']['allocation'], self.allocation())
                self.assertEqual(state['dispatchTuple']['taskBinding'], state['taskBinding'])
                self.assertEqual(state['requestHash'], self.digest)
                self.assertEqual(self.runtime.public_state(state)['taskBinding'], state['taskBinding'])
                self.assertEqual(self.runtime.main(['status', '--project-root', directory,
                    '--agent', 'work-task', '--run-id', state['runId'], '--document', 'state',
                    '--field', '/taskBinding/allocation', '--offset', '0', '--length', '4000']), 0)
                page = emit.call_args.args[0]
                self.assertEqual(json.loads(page['text']), self.allocation())
                self.runtime.mark_terminal(Path(state['statePath']), 'completed')
                send = self.runtime.parse_args(['send', '--project-root', directory, '--agent', 'work-task',
                    '--message', 'Correct the same accepted task', '--task-list-file', str(path), '--task-id', 'task-one'])
                self.document['tasks'][0]['allocation']['unitReason'] = 'changed decision'
                path.write_text(json.dumps(self.document))
                with self.assertRaises(self.runtime.ContractError) as changed:
                    self.runtime.submit(send, False)
                self.assertEqual(changed.exception.code, 'task_binding_changed')
                self.document['tasks'][0]['allocation'] = self.allocation()
                path.write_text(json.dumps(self.document))
                self.runtime.submit(send, False)
                resumed = json.loads(Path(emit.call_args.args[0]['statePath']).read_text())
                self.assertEqual(resumed['taskBinding']['allocation'], state['taskBinding']['allocation'])
                from runs.attempt import run_codex_attempt
                captured = []
                def stop_before_provider(runtime, root, cwd, run, attempt, parts):
                    captured.append(parts)
                    raise runtime.ContractError('test_stop', 'No provider launch in this own check')
                with mock.patch('execution.document_context.prepare', side_effect=stop_before_provider):
                    with self.assertRaises(self.runtime.AttemptFailure):
                        run_codex_attempt(self.runtime, project_root=root,
                            session=self.runtime.load_session(root, 'work-task'), state=resumed, attempt=1,
                            heartbeat=mock.Mock(), cancel_event=threading.Event(),
                            expected_agent_id='work-task', expected_run_id=resumed['runId'])
                self.assertEqual(len(captured), 1)
                self.assertEqual(json.loads(captured[0].dynamic.splitlines()[-1])['allocation'], self.allocation())
                self.assertNotIn('Accepted Main allocation evidence', captured[0].fixed)


    def test_loop_rejects_before_creating_or_dispatching(self):
        from test_agent_loop import load_modules
        runtime, loop = load_modules()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'request.md'
            request.write_text(self.request)
            tasks = root / 'tasks.json'
            tasks.write_text(json.dumps(self.document))
            args = loop.build_parser().parse_args(['start', '--project-root', directory,
                '--work-agent', 'work-task', '--task-mode', 'work', '--request-file', str(request),
                '--task-list-file', str(tasks), '--task-id', 'absent'])
            with mock.patch.object(loop, 'AgentRuntime') as child, mock.patch.object(loop, 'loop_directory') as create:
                with self.assertRaises(runtime.ContractError):
                    loop.start_loop(args)
                child.assert_not_called()
                create.assert_not_called()

    def test_omitted_hash_is_normalized_without_mutating_source(self):
        document = json.loads(json.dumps(self.document))
        del document['tasks'][0]['requestHash']
        snapshot, binding = self.binding.resolve(document, 'task-one', self.digest)
        self.assertNotIn('requestHash', document['tasks'][0])
        self.assertEqual(snapshot['tasks'][0]['requestHash'], self.digest)
        self.assertEqual(binding['requestHash'], self.digest)
        for value in [None, '', '0' * 64, 'not-a-hash']:
            document['tasks'][0]['requestHash'] = value
            with self.subTest(value=value):
                snapshot, binding = self.binding.resolve(document, 'task-one', self.digest)
                self.assertEqual(binding['requestHash'], self.digest)
                self.assertEqual(snapshot['tasks'][0]['requestHash'], self.digest)
                self.assertEqual(document['tasks'][0]['requestHash'], value)

    def test_exact_document_assignment_is_captured_and_cannot_overlap_or_escape(self):
        document = json.loads(json.dumps(self.document))
        path = 'docs/refined/research/assigned/SKILL.md'
        document['tasks'][0]['documentPaths'] = [path]
        _, binding = self.binding.resolve(document, 'task-one', self.digest)
        self.assertEqual(binding['documentPaths'], [path])
        binding['documentPaths'].append('another')
        self.assertEqual(document['tasks'][0]['documentPaths'], [path])
        document['tasks'].append({**document['tasks'][0], 'id': 'task-two'})
        with self.assertRaises(self.runtime.ContractError) as error:
            self.binding.resolve(document, 'task-one', self.digest)
        self.assertEqual(error.exception.code, 'task_document_owner_conflict')
        document['tasks'].pop()
        for invalid in ('src/a.py', 'docs/skills/rule-owned/SKILL.md', 'docs/refined/research/a/../../x.md'):
            document['tasks'][0]['documentPaths'] = [invalid]
            with self.assertRaises(self.runtime.ContractError):
                self.binding.resolve(document, 'task-one', self.digest)

    def test_direct_submit_automatic_hash_preserves_exact_bytes_and_retry_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = '한글 요청\r\n\n'.encode('utf-8')
            request = root / 'request.md'
            request.write_bytes(content)
            document = json.loads(json.dumps(self.document))
            document['tasks'][0]['requestHash'] = 'obsolete caller value'
            tasks = root / 'tasks.json'
            original = json.dumps(document).encode()
            tasks.write_bytes(original)
            args = self.runtime.parse_args(['submit', '--project-root', directory,
                '--agent', 'automatic-work', '--role', 'work', '--codex', '/bin/true',
                '--request-file', str(request), '--task-list-file', str(tasks),
                '--task-id', 'task-one', '--dispatch-id', 'dispatch-auto'])
            with mock.patch.object(self.runtime, 'spawn_worker', return_value=123) as spawn, mock.patch.object(self.runtime, 'emit') as emit:
                self.runtime.submit(args, True)
                state = json.loads(Path(emit.call_args.args[0]['statePath']).read_text())
                digest = hashlib.sha256(content).hexdigest()
                self.assertEqual(state['requestHash'], digest)
                self.assertEqual(state['taskBinding']['requestHash'], digest)
                self.assertEqual(Path(state['requestPath']).read_bytes(), content)
                self.assertEqual(tasks.read_bytes(), original)
                self.runtime.submit(args, True)
                self.assertEqual(spawn.call_count, 1)
                request.write_bytes(content + b'changed')
                with self.assertRaises(self.runtime.ContractError):
                    self.runtime.submit(args, True)
                self.assertEqual(Path(state['requestPath']).read_bytes(), content)

    def test_capabilities_advertise_automatic_hashes_for_both_operations(self):
        native = {'model': True, 'reasoning': True, 'fast': False, 'goal': False}
        with tempfile.TemporaryDirectory() as directory:
            self.runtime.runtime_paths.resolve(Path(directory), create=True)
            with mock.patch.object(self.runtime.native_codex, 'inspect_capabilities', return_value={
                    'schemaVersion': '0.1.0', 'kind': 'execution-capabilities',
                    'submit': dict(native), 'send': dict(native)}), mock.patch.object(self.runtime, 'emit') as emit:
                self.assertEqual(self.runtime.main(['capabilities', '--project-root', directory, '--codex', '/bin/true']), 0)
                for operation in ('submit', 'send'):
                    self.assertIs(emit.call_args.args[0][operation]['automaticRequestHash'], True)
                    self.assertIs(emit.call_args.args[0][operation]['taskAllocation'], True)

    @staticmethod
    def allocation():
        return {'schemaVersion': 1, 'unitReason': 'One independent fix with observable completion',
                'profile': {'id': 'work', 'reason': 'Integration uncertainty'},
                'session': {'strategy': 'new', 'reason': 'Prior sessions own other tasks'},
                'inputs': [{'source': 'completed report', 'revision': 'run-one',
                            'capturedAt': '2026-10-06T18:38:37Z', 'confirmed': True}],
                'dependencies': [], 'readScope': ['src/'], 'writeScopeReason': 'Brief Scope only',
                'sharedResources': [{'resource': 'shared parser', 'ownerTaskId': 'self',
                                     'confirmed': True, 'evidence': 'Main assignment'}],
                'parallelCandidate': False}

    def test_allocation_brief_snapshot_and_same_task_preservation(self):
        from tasks.allocation import preserve
        decision = self.allocation()
        document = self.binding.brief_document(self.request, decision)
        task_id = document['tasks'][0]['id']
        _, binding = self.binding.resolve(document, task_id, self.digest)
        decision['profile']['reason'] = 'caller changed'
        self.assertEqual(binding['allocation'], self.allocation())
        _, revision = self.binding.resolve(document, task_id, 'a' * 64)
        preserve(binding, revision)
        revision['allocation']['session']['strategy'] = 'reuse'
        with self.assertRaises(self.runtime.ContractError):
            preserve(binding, revision)
        revision.pop('allocation')
        with self.assertRaises(self.runtime.ContractError):
            preserve(binding, revision)
        self.assertNotIn('allocation', self.binding.brief_document(self.request)['tasks'][0])

    def test_allocation_rejects_unconfirmed_parallel_and_malformed_evidence(self):
        for field, value in [('schemaVersion', True), ('profile', {'id': [], 'reason': 'x'}),
                             ('inputs', [{}]), ('readScope', ['']), ('session', {'strategy': [], 'reason': 'x'}),
                             ('sharedResources', [{'resource': 'r', 'ownerTaskId': 'missing',
                                                    'confirmed': True, 'evidence': 'assignment'}])]:
            document = json.loads(json.dumps(self.document))
            document['tasks'][0]['allocation'] = {**self.allocation(), field: value}
            with self.subTest(field=field), self.assertRaises(self.runtime.ContractError):
                self.binding.resolve(document, 'task-one', self.digest)
        for field in ('inputs', 'dependencies', 'sharedResources'):
            value = self.allocation()
            if field == 'dependencies':
                value[field] = [dict(value['inputs'][0])]
            value[field][0]['confirmed'] = False
            document = json.loads(json.dumps(self.document))
            document['tasks'][0]['allocation'] = value
            self.binding.resolve(document, 'task-one', self.digest)  # Unknown is retained, never ready.
            value['parallelCandidate'] = True
            with self.subTest(field=field), self.assertRaises(self.runtime.ContractError):
                self.binding.resolve(document, 'task-one', self.digest)

    def test_allocation_checks_dependency_order_resource_owners_and_existing_writes(self):
        document = json.loads(json.dumps(self.document))
        first = document['tasks'][0]
        second = {**first, 'id': 'task-two', 'allocation': self.allocation()}
        document['tasks'].append(second)
        dependency = {**self.allocation()['inputs'][0], 'taskId': 'task-one'}
        second['allocation']['dependencies'] = [dependency]
        self.binding.resolve(document, 'task-one', self.digest)
        for bad in ('task-two', 'missing'):
            dependency['taskId'] = bad
            with self.subTest(dependency=bad), self.assertRaises(self.runtime.ContractError):
                self.binding.resolve(document, 'task-one', self.digest)
        dependency['taskId'] = 'task-one'
        first['allocation'] = self.allocation()
        with self.assertRaises(self.runtime.ContractError):  # two owners of one shared resource
            self.binding.resolve(document, 'task-one', self.digest)
        second['allocation']['sharedResources'][0]['ownerTaskId'] = 'task-one'
        self.binding.resolve(document, 'task-one', self.digest)
        first['requiredFileOperations'] = [{'operation': 'modify', 'path': 'src/parser.py'}]
        second['requiredFileOperations'] = [{'operation': 'modify', 'path': 'src/parser.py'}]
        first['allocation']['parallelCandidate'] = True
        with self.assertRaises(self.runtime.ContractError):
            self.binding.resolve(document, 'task-one', self.digest)
        second['requiredFileOperations'][0]['path'] = 'src/client.py'
        self.binding.resolve(document, 'task-one', self.digest)
