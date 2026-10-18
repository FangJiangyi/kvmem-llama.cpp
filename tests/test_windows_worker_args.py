"""Exercise PowerShell binding and the argv received by a real child process."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELLS = [path for name in ('powershell.exe', 'pwsh.exe') if (path := shutil.which(name))]


@unittest.skipUnless(os.name == 'nt' and SHELLS, 'Windows PowerShell launchers')
class WorkerArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kvmem-argv-')
        self.folder = Path(self.tmp.name)
        self.model = self.folder / "model & ' spaces.ninfer"
        # The Python executable acts as a worker and reads the model argument as
        # its script. It reports argv without interpreting any native options.
        self.model.write_text(
            "import json, os, sys\nprint(json.dumps({'received': sys.argv[1:], "
            "'gpu': os.environ.get('CUDA_VISIBLE_DEVICES')}))\n", encoding='utf-8')
        self.file = self.folder / "args & ' spaces.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_launcher(self, shell, entry, extra, *, dry=True):
        args = [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                str(ROOT / 'scripts/windows' / entry), '-Model', str(self.model),
                '-Worker', sys.executable, '-Gpu', 'GPU-test']
        if entry == 'start-backend.ps1':
            args += ['-Backend', 'ninfer']
        elif entry == 'start-ninfer.ps1':
            args += ['-NoVision', '-MtpDrafts', '0']
        if dry:
            args += ['-DryRun']
        return subprocess.run(args + extra, capture_output=True, encoding='utf-8', errors='replace', timeout=45)

    def plan(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout[result.stdout.index('{'):])

    def test_file_invocation_collects_multiple_native_arguments(self):
        native = ['--default-reasoning-effort', 'medium', '--preserve-thinking']
        for shell in SHELLS:
            for entry in ('start-backend.ps1', 'start-ninfer.ps1'):
                with self.subTest(shell=shell, entry=entry):
                    plan = self.plan(self.run_launcher(shell, entry, ['-WorkerArgs', *native]))
                    self.assertEqual(plan['argv'][-len(native):], native)

    def test_file_preserves_exact_child_argv(self):
        native = ['--custom', 'two words', '', '"quoted"', 'C:\\folder with spaces\\',
                  'literal; & | $(Write-Output bad)', '中文']
        self.file.write_text(json.dumps(native, ensure_ascii=False), encoding='utf-8-sig')
        for shell in SHELLS:
            for entry in ('start-backend.ps1', 'start-ninfer.ps1'):
                with self.subTest(shell=shell, entry=entry):
                    result = self.plan(self.run_launcher(shell, entry, ['-WorkerArgsFile', str(self.file)], dry=False))
                    self.assertEqual(result['received'][-len(native):], native)
                    self.assertEqual(result['gpu'], 'GPU-test')

    def test_empty_and_singleton_arrays(self):
        for native in ([], ['one argument with spaces']):
            self.file.write_text(json.dumps(native), encoding='utf-8')
            for shell in SHELLS:
                for entry in ('start-backend.ps1', 'start-ninfer.ps1'):
                    with self.subTest(shell=shell, entry=entry, native=native):
                        plan = self.plan(self.run_launcher(shell, entry, ['-WorkerArgsFile', str(self.file)]))
                        if native:
                            self.assertEqual(plan['argv'][-1:], native)
                        self.assertNotIn(None, plan['argv'])

    def test_invalid_inputs_and_setting_conflicts_fail_before_worker_start(self):
        cases = [(text, ['-WorkerArgsFile', str(self.file)]) for text in
                 ('{}', 'null', '"string"', '[1]', '[null]', '["\\u0000"]', '[broken', '[],"extra":[]')]
        cases += [
            ('[]', ['-WorkerArgsFile', str(self.file), '-WorkerArgs', '--threads', '2']),
            ('[]', ['-UnknownLauncherSetting', 'value']),
            ('["--default-reasoning-effort","low"]', ['-ReasoningEffort', 'medium', '-WorkerArgsFile', str(self.file)]),
            ('["--preserve-thinking"]', ['-PreserveThinking', '-WorkerArgsFile', str(self.file)]),
            ('[]', ['-ThinkingMode', 'off', '-ReasoningEffort', 'medium']),
            ('[]', ['-ReasoningEffort', 'unsupported']),
        ]
        # Validation is shared by both entry points; run malformed-file cases on
        # the common launcher and cover wrapper conflicts separately above.
        for shell in SHELLS:
            for text, extra in cases:
                with self.subTest(shell=shell, text=text, extra=extra):
                    self.file.write_text(text, encoding='utf-8')
                    result = self.run_launcher(shell, 'start-backend.ps1', extra, dry=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('"received"', result.stdout)

    def test_named_thinking_options_and_api_redaction(self):
        for shell in SHELLS:
            for entry in ('start-backend.ps1', 'start-ninfer.ps1'):
                with self.subTest(shell=shell, entry=entry):
                    plan = self.plan(self.run_launcher(shell, entry, [
                        '-ThinkingBudget', '256', '-ReasoningEffort', 'medium', '-PreserveThinking',
                        '-ApiKey', 'test-only-key', '-ModelAlias', 'two words']))
                    argv = plan['argv']
                    self.assertEqual(argv[argv.index('--default-thinking-budget') + 1], '256')
                    self.assertEqual(argv[argv.index('--default-reasoning-effort') + 1], 'medium')
                    self.assertIn('--preserve-thinking', argv)
                    self.assertEqual(argv[argv.index('--model-id') + 1], 'two words')
                    self.assertNotIn('test-only-key', json.dumps(plan))

    def test_dflash2_profile_reaches_the_child_without_mtp_adaptation(self):
        expected = {
            '--max-context': '262144', '--kvmem-budget': '36864',
            '--kvmem-gen-reserve': '10240', '--kvmem-host-mib': '12288',
            '--prefill-chunk': '256', '--draft-tokens': '5',
            '--ngram-draft-tokens': '0', '--cuda-graph-allowance-mib': '64',
            '--kv-dtype': 'int8', '--vision-residency': 'cpu',
            '--vision-max-merged': '1024', '--max-concurrency': '1',
        }
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.plan(self.run_launcher(shell, 'start-dflash2.ps1', [], dry=False))
                argv = result['received']
                for flag, value in expected.items():
                    self.assertEqual(argv[argv.index(flag) + 1], value, flag)
                spec = len(argv) - 1 - argv[::-1].index('--spec')
                self.assertEqual(argv[spec + 1], 'dflash2')
                self.assertIn('--gdn-state-fp16', argv)
                self.assertNotIn('--adaptive-mtp', argv)
                self.assertEqual(result['gpu'], 'GPU-test')
                override = self.plan(self.run_launcher(shell, 'start-dflash2.ps1', ['-Context', '131072', '-Port', '18201']))
                self.assertEqual(override['argv'][override['argv'].index('--max-context') + 1], '131072')
                self.assertEqual(override['argv'][override['argv'].index('--port') + 1], '18201')


if __name__ == '__main__':
    unittest.main()
