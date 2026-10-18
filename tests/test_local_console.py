import importlib.util
import json
import os
from pathlib import Path
import tempfile
import socket
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('console', Path(__file__).resolve().parents[1] / 'scripts/local-console.py')
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = console.Controller(console.ROOT, self.tmp.name, 18199)
        self.config = {'schema_version': 1, 'parameters': dict(console.DEFAULTS)}

    def tearDown(self):
        self.controller.stop()
        self.tmp.cleanup()

    def test_console_limits_external_launchers_to_mainline_backends(self):
        folder = Path(self.tmp.name) / 'external'
        script = folder / 'scripts/windows/start-backend.ps1'
        script.parent.mkdir(parents=True)
        script.write_text("[ValidateSet('llamacpp', 'ninfer', 'strata', 'polystrata')][string]$Backend", encoding='utf-8')
        controller = console.Controller(folder, folder / 'state', 18199)
        self.assertEqual(controller.backends, ['llamacpp', 'ninfer'])
        for backend in ('strata', 'polystrata'):
            bad = {'schema_version': 1, 'parameters': {'Backend': backend}}
            with self.assertRaises(ValueError):
                console.normalize(bad, ['llamacpp', 'ninfer', 'strata', 'polystrata'])

    def test_draft_is_independent_and_survives_controller_restart(self):
        self.controller.save(self.config)
        self.assertIsNone(self.controller.status()['active_config'])
        second = console.Controller(console.ROOT, self.tmp.name, 18199)
        self.assertEqual(second.draft(), self.config)

    def test_credentials_are_never_saved_or_returned_in_runtime_status(self):
        key = 'test-only-credential-123456'
        self.config['parameters']['ApiKey'] = key
        saved = self.controller.save(self.config)
        self.assertNotIn('ApiKey', saved['parameters'])
        self.assertNotIn(key, self.controller.draft_path.read_text(encoding='utf-8'))
        self.controller.active = self.config
        status = self.controller.status()
        self.assertTrue(status['api_key_configured'])
        self.assertNotIn(key, json.dumps(status))
        self.assertEqual(self.controller.monitor_authorization(), 'Bearer ' + key)
        self.controller.redactions.add(key)
        self.controller.log_path.write_text('worker error: ' + key, encoding='utf-8')
        self.assertEqual(self.controller.log_tail(), 'worker error: <redacted>')

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_native_service_mapping_and_private_launch_plan(self):
        key = 'test-only-credential-123456'
        worker = Path(self.tmp.name) / 'worker.exe'; worker.touch()
        template = Path(self.tmp.name) / "chat & ' $literal.jinja"; template.write_text('template', encoding='utf-8')
        for backend, suffix, alias_flag, template_flag, log_flag, log_value, off_flag in (
            ('llamacpp', '.gguf', '--alias', '--chat-template-file', '--verbosity', '5', '--no-think'),
            ('ninfer', '.ninfer', '--model-id', '--chat-template', '--log-level', 'debug', '--no-thinking')
        ):
            model = Path(self.tmp.name) / ('model' + suffix); model.touch()
            self.config['parameters'].update(Backend=backend, Model=str(model), Worker=str(worker),
                Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29', ApiKey=key,
                ModelAlias="friendly & ' $literal", ChatTemplateFile=str(template), LogLevel='debug',
                ThinkingMode='off', Concurrency=2)
            plan = self.controller.plan(self.config)
            args = plan['argv']
            for flag, value in ((alias_flag, "friendly & ' $literal"), (template_flag, str(template)),
                                (log_flag, log_value), ('--api-key', '<redacted>'),
                                ('--parallel' if backend == 'llamacpp' else '--max-concurrency', '2')):
                self.assertEqual(args[args.index(flag)+1], value)
            self.assertIn(off_flag, args)
            self.assertNotIn(key, json.dumps(plan))
            private = self.controller.plan(self.config, reveal_secrets=True)['argv']
            self.assertEqual(private[private.index('--api-key')+1], key)
            self.assertNotIn('ApiKey', self.controller.draft()['parameters'])
            self.assertEqual(list(Path(self.tmp.name).glob('*.json')), [])

    def test_new_fields_validate_native_capabilities_and_single_key(self):
        for key, value in [('ApiKey', 'key,second'), ('ApiKey', 'key with space'), ('ApiKey', 'key\nheader'),
                           ('LogLevel', 'verbose'), ('ThinkingMode', 'on'), ('Concurrency', 9),
                           ('ChatTemplateFile', str(Path(self.tmp.name) / 'missing.jinja'))]:
            bad = json.loads(json.dumps(self.config)); bad['parameters'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                console.normalize(bad, self.controller.backends)
        self.config['parameters'].update(Backend='llamacpp', ThinkingMode='on', Concurrency=4)
        self.assertEqual(console.normalize(self.config, self.controller.backends), self.config)
        self.config['parameters']['Concurrency'] = 5
        with self.assertRaises(ValueError):
            console.normalize(self.config, self.controller.backends)

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_ninfer_thinking_and_native_array_survive_json_bridge(self):
        worker = Path(self.tmp.name) / 'worker.exe'; worker.touch()
        model = Path(self.tmp.name) / 'model with spaces.ninfer'; model.touch()
        self.config['parameters'].update(Model=str(model), Worker=str(worker),
            Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29', ThinkingBudget=256,
            ReasoningEffort='medium', PreserveThinking=True, WorkerArgs=['--threads', '2'])
        argv = self.controller.plan(self.config)['argv']
        self.assertEqual(argv[-2:], ['--threads', '2'])
        self.assertEqual(argv[argv.index('--default-reasoning-effort') + 1], 'medium')
        self.assertEqual(argv[argv.index('--default-thinking-budget') + 1], '256')
        self.assertIn('--preserve-thinking', argv)
        self.assertEqual(list(Path(self.tmp.name).glob('*.json')), [])
        for overrides in ({'ReasoningEffort': 'bad'}, {'ThinkingMode': 'off'},
                          {'Backend': 'llamacpp'}, {'PreserveThinking': 'true'}):
            bad = json.loads(json.dumps(self.config))
            bad['parameters'].update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                console.normalize(bad, self.controller.backends)

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_registered_worker_default_and_explicit_override(self):
        model = Path(self.tmp.name) / 'test.ninfer'; model.touch()
        worker = Path(self.tmp.name) / 'installed worker.exe'; worker.touch()
        explicit = Path(self.tmp.name) / 'explicit worker.exe'; explicit.touch()
        registration = Path(self.tmp.name) / 'worker-paths.json'
        registration.write_text(json.dumps({'ninfer': str(worker)}), encoding='utf-8')
        self.config['parameters'].update(Model=str(model), Worker='',
                                         Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29')
        self.assertEqual(self.controller.plan(self.config)['argv'][0], str(worker))
        self.assertEqual(self.config['parameters']['Worker'], '', 'do not mutate draft')
        self.config['parameters']['Worker'] = str(explicit)
        self.assertEqual(self.controller.plan(self.config)['argv'][0], str(explicit))
        explicit.unlink()
        with self.assertRaisesRegex(ValueError, '指定的后端程序不存在'):
            self.controller.plan(self.config)
        self.config['parameters']['Worker'] = ''
        worker.unlink()
        self.assertFalse(self.controller.worker_defaults()['ninfer']['available'])
        with self.assertRaisesRegex(ValueError, '未找到可用的后端程序'):
            self.controller.plan(self.config)

    def test_packaged_worker_default(self):
        old_root = self.controller.backend_root
        self.controller.backend_root = Path(self.tmp.name)
        worker = self.controller.backend_root / 'workers/ninfer/ninfer-kvmem-server.exe'
        worker.parent.mkdir(parents=True); worker.touch()
        try:
            self.assertEqual(self.controller.worker_defaults()['ninfer'],
                             {'path': str(worker), 'available': True})
        finally:
            self.controller.backend_root = old_root

    def test_worker_discovery_in_installation_root_bin_and_workers(self):
        installation = Path(self.tmp.name) / 'installation'
        self.controller.backend_root = installation
        cases = [('ninfer', 'ninfer-kvmem-server.exe', ''),
                 ('ninfer', 'ninfer-serve.exe', ''),
                 ('ninfer', 'ninfer-serve.exe', 'bin'),
                 ('ninfer', 'ninfer-kvmem-server.exe', 'workers/ninfer'),
                 ('llamacpp', 'llama-kvmem-server.exe', ''),
                 ('llamacpp', 'llama-kvmem-server.exe', 'bin'),
                 ('llamacpp', 'llama-kvmem-server.exe', 'workers/llamacpp')]
        with patch.object(console, 'ROOT', installation), patch.object(console.subprocess, 'run') as run:
            for backend, name, directory in cases:
                with self.subTest(backend=backend, name=name, directory=directory):
                    worker = installation / directory / name
                    worker.parent.mkdir(parents=True, exist_ok=True); worker.touch()
                    self.assertEqual(self.controller.worker_defaults()[backend],
                                     {'path': str(worker), 'available': True})
                    worker.unlink()
            run.assert_not_called()

    def test_worker_discovery_precedence_and_registration_failure(self):
        installation = Path(self.tmp.name) / 'installation'
        installation.mkdir()
        self.controller.backend_root = installation
        canonical = installation / 'ninfer-kvmem-server.exe'; canonical.touch()
        alias = installation / 'ninfer-serve.exe'; alias.touch()
        bin_worker = installation / 'bin/ninfer-kvmem-server.exe'
        bin_worker.parent.mkdir(); bin_worker.touch()
        with patch.object(console, 'ROOT', installation):
            self.assertEqual(self.controller.worker_defaults()['ninfer']['path'], str(canonical))
            canonical.unlink()
            self.assertEqual(self.controller.worker_defaults()['ninfer']['path'], str(alias))
            registered = Path(self.tmp.name) / 'registered.exe'; registered.touch()
            (Path(self.tmp.name) / 'worker-paths.json').write_text(json.dumps({'ninfer': str(registered)}))
            self.assertEqual(self.controller.worker_defaults()['ninfer']['path'], str(registered))
            registered.unlink()
            self.assertEqual(self.controller.worker_defaults()['ninfer'],
                             {'path': str(registered), 'available': False})

    def test_worker_discovery_separate_console_root_and_bounded_scope(self):
        installation = Path(self.tmp.name) / 'console'
        installation.mkdir()
        self.controller.backend_root = Path(self.tmp.name) / 'backend'
        # Plain upstream llama-server and arbitrary/nested EXEs are not KVMem workers.
        (installation / 'llama-server.exe').touch()
        (installation / 'other.exe').touch()
        nested = installation / 'other-build/ninfer-serve.exe'
        nested.parent.mkdir(); nested.touch()
        with patch.object(console, 'ROOT', installation):
            self.assertFalse(self.controller.worker_defaults()['ninfer']['available'])
            self.assertFalse(self.controller.worker_defaults()['llamacpp']['available'])
            worker = installation / 'ninfer-serve.exe'; worker.touch()
            self.assertEqual(self.controller.worker_defaults()['ninfer'],
                             {'path': str(worker), 'available': True})
            selected = self.controller.backend_root / 'ninfer-kvmem-server.exe'
            selected.parent.mkdir(); selected.touch()
            self.assertEqual(self.controller.worker_defaults()['ninfer']['path'], str(selected))

    def test_rejects_command_injection_and_common_native_conflicts(self):
        for key, value in [('WorkerArgs', ['--port', '80']), ('WorkerArgs', ['--threads', '$(calc)']),
                           ('WorkerArgs', ['--threads', '8', '--threads', '4']),
                           ('WorkerArgs', '--threads 8'), ('DryRun', False), ('Port', True),
                           ('ListenHost', 'https://attacker.invalid'), ('Gpu', '0'), ('StageLayers', [False])]:
            bad = json.loads(json.dumps(self.config)); bad['parameters'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                console.normalize(bad, self.controller.backends)

    @unittest.skipUnless(os.name == 'nt', 'Windows native picker')
    def test_model_picker_cancel_validation_and_literal_path(self):
        model = Path(self.tmp.name) / "model & ' $literal.ninfer"
        model.touch()
        request = {'backend': 'ninfer', 'current_path': str(model)}
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"path":null}', stderr='')):
            self.assertEqual(self.controller.browse_model(request), {'path': None})
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'path':str(model)}), stderr='')) as run:
            self.assertEqual(self.controller.browse_model(request)['path'], str(model.resolve()))
            self.assertIn(str(model.parent.resolve()), run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)
        wrong = Path(self.tmp.name) / 'not-a-model.txt'; wrong.touch()
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'path':str(wrong)}), stderr='')):
            with self.assertRaises(ValueError):
                self.controller.browse_model(request)
        self.assertFalse(self.controller.picker_lock.locked())
        with self.assertRaises(ValueError):
            self.controller.browse_model({'backend': 'powershell'})
        self.assertEqual(self.controller.status()['state'], 'stopped')

    @unittest.skipUnless(os.name == 'nt', 'Windows native picker')
    def test_template_picker_returns_literal_file_and_allows_cancel(self):
        template = Path(self.tmp.name) / "chat & ' $literal.jinja"; template.touch()
        request = {'backend': 'llamacpp', 'current_path': str(template)}
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout=json.dumps({'path': str(template)}), stderr='')) as run:
            self.assertEqual(self.controller.browse_model(request, template=True)['path'], str(template))
            self.assertIn('-Template', run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout='{"path":null}', stderr='')):
            self.assertEqual(self.controller.browse_model(request, template=True), {'path': None})
        self.assertFalse(self.controller.picker_lock.locked())

    def test_scan_filters_backend_auxiliary_and_split_files_without_loading(self):
        folder = Path(self.tmp.name)
        nested = folder / 'models'; nested.mkdir()
        for name in ('main.GGUF', 'mmproj-F16.gguf', 'vision-projector.gguf',
                     'split-00001-of-00002.gguf', 'split-00002-of-00002.gguf',
                     'native.ninfer', 'wrong.infer', 'download.gguf.part'):
            (nested / name).write_bytes(b'not parsed or loaded')
        result = self.controller.scan_models({'backend':'llamacpp','directory':str(folder)})
        self.assertEqual({x['name'] for x in result['models']}, {'main.GGUF','split-00001-of-00002.gguf'})
        self.assertFalse(result['limited'])
        result = self.controller.scan_models({'backend':'ninfer','directory':str(folder)})
        self.assertEqual([x['name'] for x in result['models']], ['native.ninfer'])
        self.assertEqual(self.controller.status()['state'], 'stopped')
        with self.assertRaises(ValueError):
            self.controller.scan_models({'backend':'ninfer','directory':folder.anchor})

    def test_scan_deduplicates_hardlinks_and_prefers_library_path(self):
        folder = Path(self.tmp.name)
        canonical = folder / 'ninfer' / 'iq3' / 'iq3.ninfer'
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b'model')
        old = folder / 'iq3.ninfer'
        os.link(canonical, old)
        # Another copy with the same filename and size must remain a separate result.
        other = folder / 'experiment' / 'iq3.ninfer'
        other.parent.mkdir(); other.write_bytes(b'other')
        result = self.controller.scan_models({'backend': 'ninfer', 'directory': str(folder)})
        self.assertEqual({x['path'] for x in result['models']}, {str(canonical), str(other)})
        self.assertFalse(result['limited'])

    def test_scan_is_bounded_and_reports_partial_results(self):
        folder = Path(self.tmp.name)
        for i in range(205):
            (folder / f'model-{i}.ninfer').touch()
        result = self.controller.scan_models({'backend':'ninfer','directory':str(folder)})
        self.assertEqual(len(result['models']), 200)
        self.assertTrue(result['limited'])

    @unittest.skipUnless(os.name == 'nt', 'Windows native picker')
    def test_missing_or_failed_picker_has_readable_error_and_releases_lock(self):
        request = {'backend': 'ninfer', 'current_path': ''}
        with patch.object(console, 'ROOT', Path(self.tmp.name)), patch.object(console.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '选择窗口组件缺失'):
                self.controller.browse_model(request, folder=True)
            run.assert_not_called()
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stderr='bad encoding \ufffd\ufffd PowerShell stack', stdout='')):
            with self.assertRaisesRegex(ValueError, '无法打开系统选择窗口') as error:
                self.controller.browse_model(request, folder=True)
            self.assertNotIn('\ufffd', str(error.exception))
            self.assertNotIn('PowerShell', str(error.exception))
        with patch.object(console.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='not json')):
            with self.assertRaisesRegex(ValueError, '选择窗口返回异常'):
                self.controller.browse_model(request, folder=True)
        self.assertFalse(self.controller.picker_lock.locked())

    def test_model_directories_survive_restart_and_forgetting_keeps_files(self):
        folder = Path(self.tmp.name) / '模型 library & spaces'; folder.mkdir()
        model = folder / 'local.ninfer'; model.write_bytes(b'model')
        self.controller.update_model_directories({'action': 'add', 'path': '"' + str(folder) + '"'})
        second = console.Controller(console.ROOT, self.tmp.name, 18198)
        self.assertEqual(second.model_directories()['directories'], [{'path': str(folder), 'available': True}])
        second.update_model_directories({'action': 'add', 'path': str(folder)})
        self.assertEqual(len(second.model_directories()['directories']), 1)
        second.update_model_directories({'action': 'remove', 'path': str(folder)})
        self.assertTrue(model.is_file(), 'forgetting a folder must never remove model files')
        self.assertEqual(second.model_directories()['directories'], [])
        for path in ('relative-folder', str(folder / 'missing'), folder.anchor, str(model)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                second.update_model_directories({'action': 'add', 'path': path})

    def test_default_scan_finds_models_beside_exe_and_in_ancestor_library(self):
        base = Path(self.tmp.name) / 'fixture/KVMem'
        installation = base / 'monitor/build/bin'; installation.mkdir(parents=True)
        library = base / 'models/ninfer/local'; library.mkdir(parents=True)
        beside = installation / 'model.ninfer'; beside.write_bytes(b'model')
        canonical = library / 'local.ninfer'; canonical.write_bytes(b'model')
        unrelated = installation / 'other-build/unrelated.ninfer'
        unrelated.parent.mkdir(); unrelated.write_bytes(b'model')
        self.controller.backend_root = installation
        with patch.object(console, 'ROOT', installation), patch.object(console.subprocess, 'run') as run:
            result = self.controller.scan_models({'backend': 'ninfer'})
            self.assertEqual({item['path'] for item in result['models']}, {str(beside), str(canonical)})
            run.assert_not_called()

    def test_scan_remembers_folder_reports_other_formats_and_missing_location(self):
        library = Path(self.tmp.name) / 'library'; library.mkdir()
        (library / 'empty.ninfer').touch()
        (library / 'different.gguf').write_bytes(b'model')
        result = self.controller.scan_models({'backend': 'ninfer', 'directory': str(library)})
        self.assertEqual(result['models'][0]['warnings'], ['empty'])
        self.assertEqual(result['other_formats']['gguf'], 1)
        self.assertEqual(result['locations'][0]['status'], 'scanned')
        self.assertEqual(self.controller.model_directories()['directories'][0]['path'], str(library))
        # A saved library on a disconnected/removed volume must stay visible as unavailable.
        missing = Path(self.tmp.name) / 'unavailable library'
        missing.mkdir()
        self.controller.update_model_directories({'action': 'add', 'path': str(missing)})
        missing.rmdir()
        isolated = Path(self.tmp.name) / 'fixture/a/b/c/d/install'; isolated.mkdir(parents=True)
        self.controller.backend_root = isolated
        with patch.object(console, 'ROOT', isolated):
            result = self.controller.scan_models({'backend': 'ninfer'})
        self.assertTrue(any(item['path'] == str(missing) and item['status'] == 'unavailable'
                            for item in result['locations']))
        self.assertEqual(self.controller.status()['state'], 'stopped')

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_canonical_plan_preserves_literal_paths_and_gpu_identity(self):
        model = Path(self.tmp.name) / "model space & ' $literal.ninfer"
        worker = Path(self.tmp.name) / 'worker space.exe'
        model.touch(); worker.touch()
        self.config['parameters'].update(Model=str(model), Worker=str(worker),
                                         Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29')
        plan = self.controller.plan(self.config)
        self.assertEqual(plan['argv'][:2], [str(worker), str(model)])
        self.assertEqual(plan['environment']['CUDA_VISIBLE_DEVICES'], self.config['parameters']['Gpu'])
        self.assertNotIn('DryRun', plan['argv'])
        self.assertEqual(list(Path(self.tmp.name).glob('*.json')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_multigpu_order_and_layers_reach_native_launcher(self):
        model = Path(self.tmp.name) / 'multi.ninfer'; model.touch()
        worker = Path(self.tmp.name) / 'worker.exe'; worker.touch()
        gpu_order = ['GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c',
                     'GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29']
        for devices, layers in [(gpu_order, [40, 24]), (gpu_order[::-1], [24, 40])]:
            self.config['parameters'].update(Model=str(model), Worker=str(worker),
                                             Gpu=','.join(devices), StageLayers=layers)
            self.controller.save(self.config)
            restored = console.Controller(console.ROOT, self.tmp.name, 18199).draft()
            plan = self.controller.plan(restored)
            self.assertEqual(plan['environment']['CUDA_VISIBLE_DEVICES'], ','.join(devices))
            args = plan['argv']
            self.assertEqual(args[args.index('--devices') + 1], '0,1')
            self.assertEqual(args[args.index('--stage-layers') + 1], ','.join(map(str, layers)))

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_independent_llama_kv_types_reach_native_flags(self):
        model = Path(self.tmp.name) / 'split kv.gguf'; model.touch()
        worker = Path(self.tmp.name) / 'worker.exe'; worker.touch()
        base = dict(console.DEFAULTS, Backend='llamacpp', Model=str(model), Worker=str(worker),
                    Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29')
        quantized = ('q8_0', 'q5_0', 'q4_0')
        for key_type in (*quantized, 'f16', 'f32'):
            for value_type in (*quantized, 'f16', 'f32'):
                with self.subTest(k=key_type, v=value_type):
                    config = {'schema_version': 1, 'parameters': dict(base, KvTypeK=key_type, KvTypeV=value_type)}
                    if (key_type in quantized) != (value_type in quantized):
                        with self.assertRaisesRegex(ValueError, 'cannot mix floating-point and quantized'):
                            self.controller.plan(config)
                        continue
                    self.controller.save(config)
                    restored = console.Controller(console.ROOT, self.tmp.name, 18199).draft()
                    self.assertEqual(restored, config)
                    args = self.controller.plan(restored)['argv']
                    self.assertNotIn('--kv-dtype', args)
                    self.assertEqual(args[args.index('--cache-type-k') + 1], key_type)
                    self.assertEqual(args[args.index('--cache-type-v') + 1], value_type)
        for values, expected in [({'KvType': 'q5_0', 'KvTypeK': 'q8_0'}, ('q8_0', 'q5_0')),
                                 ({'KvTypeV': 'q4_0'}, ('q8_0', 'q4_0'))]:
            args = self.controller.plan({'schema_version': 1, 'parameters': dict(base, **values)})['argv']
            self.assertEqual(args[args.index('--cache-type-k') + 1], expected[0])
            self.assertEqual(args[args.index('--cache-type-v') + 1], expected[1])
        legacy = {'schema_version': 1, 'parameters': dict(base, KvType='f16')}
        args = self.controller.plan(legacy)['argv']
        self.assertEqual(args[args.index('--kv-dtype') + 1], 'f16')
        self.assertNotIn('--cache-type-k', args)
        for values in ({'KvTypeK': 'unsupported'}, {'KvTypeV': 'q3_0'}):
            with self.assertRaisesRegex(ValueError, 'K/V types must use'):
                self.controller.plan({'schema_version': 1, 'parameters': dict(base, **values)})
        self.assertIsNone(self.controller.process)

    def test_independent_kv_fields_are_llama_only_and_typed(self):
        for key in ('KvTypeK', 'KvTypeV'):
            for backend, value in [('ninfer', 'q4_0'), ('llamacpp', 4)]:
                with self.subTest(backend=backend, key=key, value=value):
                    with self.assertRaises(ValueError):
                        console.normalize({'schema_version': 1, 'parameters': {'Backend': backend, key: value}}, self.controller.backends)

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical launcher')
    def test_preview_reports_port_occupancy_without_starting(self):
        model = Path(self.tmp.name) / 'port.ninfer'; model.touch()
        worker = Path(self.tmp.name) / 'port.exe'; worker.touch()
        self.config['parameters'].update(Model=str(model), Worker=str(worker),
                                         Gpu='GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29')
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            port = occupied.getsockname()[1]
            self.config['parameters']['Port'] = port
            result = self.controller.plan(self.config)
            self.assertEqual(result['preflight'], {'port': port, 'listen_host': '127.0.0.1', 'port_available': False})
        result = self.controller.plan(self.config)
        self.assertTrue(result['preflight']['port_available'])
        self.config['parameters']['ListenHost'] = '0.0.0.0'
        result = self.controller.plan(self.config)
        self.assertEqual(result['preflight']['listen_host'], '0.0.0.0')
        self.assertTrue(result['preflight']['port_available'])
        self.assertEqual(result['argv'][result['argv'].index('--host') + 1], '0.0.0.0')
        self.assertIsNone(self.controller.process)

    def test_listen_address_validation_and_wildcard_bind(self):
        for host in ('127.0.0.1', '0.0.0.0'):
            self.config['parameters']['ListenHost'] = host
            self.assertEqual(console.normalize(self.config, self.controller.backends), self.config)
        with patch.object(self.controller, 'plan', return_value={'argv': ['unused']}), patch.object(console.socket, 'socket') as factory:
            probe = factory.return_value.__enter__.return_value
            probe.bind.side_effect = OSError('occupied')
            with self.assertRaisesRegex(ValueError, '端口'):
                self.controller.start(self.config)
            probe.bind.assert_called_once_with(('0.0.0.0', 18200))
        self.assertIsNone(self.controller.process)

    def test_wildcard_health_and_local_url_use_loopback(self):
        self.config['parameters']['ListenHost'] = '0.0.0.0'
        self.controller.active = self.config
        self.controller.process = SimpleNamespace(poll=lambda: None, pid=123)
        response = MagicMock()
        response.__enter__.return_value.status = 200
        try:
            with patch.object(console.urllib.request, 'urlopen', return_value=response) as request:
                result = self.controller.status()
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(result['url'], 'http://127.0.0.1:18200')
            request.assert_called_once_with('http://127.0.0.1:18200/health', timeout=0.5)
        finally:
            self.controller.process = None

    def test_same_origin_token_and_static_allowlist(self):
        server = console.Server(('127.0.0.1', 0), console.Handler)
        server.control, server.gpus = self.controller, []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        def request(path, headers=None, data=None):
            req = urllib.request.Request(origin + path, headers=headers or {},
                data=json.dumps(data).encode() if data is not None else None)
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read()
        try:
            code, body = request('/api/launcher/info')
            self.assertEqual(code, 200)
            token = json.loads(body)['token']
            headers = {'Content-Type': 'application/json', 'X-KVMem-Token': token, 'Origin': origin}
            self.assertEqual(request('/api/launcher/save', headers, self.config)[0], 200)
            for bad in [dict(headers, Origin='https://attacker.invalid'), dict(headers, Host='attacker.invalid'),
                        dict(headers, **{'X-KVMem-Token': 'wrong'}),
                        dict(headers, **{'Sec-Fetch-Site': 'cross-site'})]:
                self.assertEqual(request('/api/launcher/start', bad, self.config)[0], 403)
            self.assertEqual(request('/monitor/../../scripts/local-console.py')[0], 404)
            self.assertEqual(request('/api/monitor/snapshot')[0], 503)
            self.assertEqual(request('/monitor/launcher.js')[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_repeated_launch_reuses_only_same_installation_and_state(self):
        server = console.Server(('127.0.0.1', 0), console.Handler)
        server.control, server.gpus = self.controller, []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        port = server.server_port
        try:
            self.assertTrue(console.matching_console(port, console.ROOT, self.tmp.name))
            self.assertFalse(console.matching_console(port, console.ROOT, Path(self.tmp.name) / 'other-state'))
            self.assertFalse(console.matching_console(port, Path(self.tmp.name) / 'other-install', self.tmp.name))
            with patch.object(console.sys, 'argv', ['local-console.py', '--port', str(port),
                              '--state-dir', self.tmp.name, '--open-browser']), \
                 patch.object(console, 'open_console_browser') as browser, \
                 patch.object(console, 'gpu_inventory') as inventory:
                console.main()
                browser.assert_called_once_with(f'http://127.0.0.1:{port}/monitor/')
                inventory.assert_not_called()
            self.assertIsNone(self.controller.process)
            with patch.object(console.sys, 'argv', ['local-console.py', '--port', str(port),
                              '--state-dir', str(Path(self.tmp.name) / 'other-state'), '--open-browser']), \
                 patch.object(console, 'open_console_browser') as browser:
                with self.assertRaises(SystemExit) as error:
                    console.main()
                self.assertEqual(error.exception.code, 1)
                browser.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_existing_non_console_is_not_reused(self):
        opener = MagicMock()
        for body in (b'[]', b'{}', b'not JSON', b'x' * 262145,
                     json.dumps({'console_protocol': 1, 'console_root': str(console.ROOT),
                                 'backend_root': str(console.ROOT), 'state_dir': None}).encode()):
            opener.open.return_value.__enter__.return_value.read.return_value = body
            with patch.object(console.urllib.request, 'build_opener', return_value=opener):
                self.assertFalse(console.matching_console(18199, console.ROOT, self.tmp.name))

    def test_port_prompt_validates_values_and_supports_cancel(self):
        with patch('builtins.input', side_effect=['text', '0', '65536', '18199', '18198']), \
             patch('builtins.print') as output:
            self.assertEqual(console.choose_console_port(18199), 18198)
            self.assertIn('默认端口 18199 不可用', output.call_args_list[0].args[0])
        for value in ('', 'q', 'Q', 'exit', 'quit'):
            with self.subTest(value=value), patch('builtins.input', return_value=value), patch('builtins.print'):
                self.assertIsNone(console.choose_console_port(18199))
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), patch('builtins.input', side_effect=error), patch('builtins.print'):
                self.assertIsNone(console.choose_console_port(18199))

    def test_port_retry_uses_new_port_for_readiness_and_browser(self):
        server = MagicMock(server_port=18197)
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(console.sys, 'argv', ['local-console.py', '--state-dir', self.tmp.name,
                          '--open-browser', '--prompt-port']), \
             patch.object(console, 'Server', side_effect=[OSError('occupied'), OSError('occupied'), server]) as factory, \
             patch.object(console, 'matching_console', return_value=False), \
             patch.object(console, 'choose_console_port', side_effect=[18198, 18197]) as prompt, \
             patch.object(console, 'gpu_inventory', return_value=[]), \
             patch.object(console.threading, 'Thread') as thread:
            console.main()
        self.assertEqual([call.args[0] for call in factory.call_args_list],
                         [('127.0.0.1', 18199), ('127.0.0.1', 18198), ('127.0.0.1', 18197)])
        self.assertEqual([call.args for call in prompt.call_args_list], [(18199, 'zh-CN'), (18198, 'zh-CN')])
        self.assertEqual(server.control.port, 18197)
        thread.assert_called_once_with(target=console.open_console_browser,
                                       args=('http://127.0.0.1:18197/monitor/',), daemon=True)
        server.server_close.assert_called_once()

    def test_english_port_prompt_and_startup_instructions(self):
        with patch('builtins.input', side_effect=['text', '0', '65536', '18199', '18198']) as prompt, \
             patch('builtins.print') as output:
            self.assertEqual(console.choose_console_port(18199, 'en'), 18198)
        messages = '\n'.join(call.args[0] for call in output.call_args_list)
        self.assertIn('Default port 18199 is unavailable', messages)
        self.assertIn('Port must be an integer from 1 to 65535.', messages)
        self.assertIn('Choose a port other than 18199.', messages)
        self.assertTrue(messages.isascii())
        self.assertTrue(all(call.args[0].isascii() for call in prompt.call_args_list))
        for value in ('', 'q', EOFError(), KeyboardInterrupt()):
            options = {'side_effect': value} if isinstance(value, BaseException) else {'return_value': value}
            with self.subTest(value=repr(value)), patch('builtins.input', **options), patch('builtins.print') as output:
                self.assertIsNone(console.choose_console_port(18199, 'en'))
                self.assertIn('Startup cancelled.', output.call_args_list[-1].args[0])
        server = MagicMock(server_port=18198)
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(console.sys, 'argv', ['local-console.py', '--state-dir', self.tmp.name,
                          '--open-browser', '--prompt-port', '--language', 'en']), \
             patch.object(console, 'Server', side_effect=[OSError('occupied'), server]), \
             patch.object(console, 'matching_console', return_value=False), \
             patch.object(console, 'choose_console_port', return_value=18198) as prompt, \
             patch.object(console, 'gpu_inventory', return_value=[]), \
             patch.object(console.threading, 'Thread') as thread, patch('builtins.print') as output:
            console.main()
        prompt.assert_called_once_with(18199, 'en')
        thread.assert_called_once_with(target=console.open_console_browser,
                                       args=('http://127.0.0.1:18198/monitor/?lang=en',), daemon=True)
        messages = '\n'.join(call.args[0] for call in output.call_args_list)
        for expected in ('How to stop:', 'Ctrl+C', 'Stop service', 'Closing only the browser', 'Backends started separately'):
            self.assertIn(expected, messages)
        self.assertTrue(messages.isascii())
        server.server_close.assert_called_once()

    def test_english_entry_reuses_running_console_with_english_browser_hint(self):
        with patch.object(console.sys, 'argv', ['local-console.py', '--state-dir', self.tmp.name,
                          '--open-browser', '--language', 'en']), \
             patch.object(console, 'Server', side_effect=OSError('occupied')), \
             patch.object(console, 'matching_console', return_value=True), \
             patch.object(console, 'open_console_browser') as browser, \
             patch.object(console, 'gpu_inventory') as inventory:
            console.main()
        browser.assert_called_once_with('http://127.0.0.1:18199/monitor/?lang=en')
        inventory.assert_not_called()

    def test_port_prompt_cancel_does_not_start_server_or_browser(self):
        with patch.object(console.sys, 'argv', ['local-console.py', '--state-dir', self.tmp.name, '--prompt-port']), \
             patch.object(console, 'Server', side_effect=OSError('occupied')), \
             patch.object(console, 'matching_console', return_value=False), \
             patch.object(console, 'choose_console_port', return_value=None), \
             patch.object(console, 'gpu_inventory') as inventory, \
             patch.object(console, 'open_console_browser') as browser:
            console.main()
        inventory.assert_not_called()
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
