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


if __name__ == '__main__':
    unittest.main()
