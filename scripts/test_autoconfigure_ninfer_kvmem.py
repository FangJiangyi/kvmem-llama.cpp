"""Acceptance/control-flow regressions; synthetic outcomes do not claim GPU validation."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import autoconfigure_ninfer_kvmem as auto
from task2_workload import Task2Failure, run_task2


class Task2Tests(unittest.TestCase):
    def replay(self, folder, *, miss_cache=False, truncated=False, early_output=False, pilot=0):
        saved = []
        prompt = 180

        def post(label, history, *, extra):
            nonlocal prompt
            self.assertEqual(extra['max_tokens'], 512)
            self.assertEqual(extra['reasoning_budget_tokens'], 128)
            self.assertEqual(extra['tool_choice'], 'none')
            self.assertEqual(extra['temperature'], 1.)
            if len(saved):
                tool = history[-1]
                call = history[-2]['tool_calls'][0]
                self.assertEqual(tool['role'], 'tool')
                self.assertEqual(call['id'], tool['tool_call_id'])
                lines = tool['content'].count('value = value + 1\n')
                prompt += 7 * lines + 64
                self.assertIn(f'# batch_id={len(saved)-1:04d} checksum=', tool['content'])
                if len(saved) == 1:
                    self.assertTrue(tool['content'].startswith('# batch_id=0000 checksum=007391\n'))
            usage = {'prompt_tokens': prompt, 'completion_tokens': 32 if early_output else 512,
                     'prompt_cache_hit_tokens': 0 if miss_cache else max(0, prompt - 8192)}
            if truncated and len(saved) > 3:
                usage['prompt_tokens'] = 12000
            saved.append({'label': label, 'history': copy.deepcopy(history), 'extra': extra, 'usage': usage})
            return {'label': label, 'usage': usage}

        result = run_task2(post, folder, max_tool_rounds=pilot)
        return result, saved

    def test_original_256k_workload_reaches_final_and_preserves_fixed_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            result, requests = self.replay(folder)
            self.assertTrue(result['validated'])
            self.assertFalse(result['semantic_quality_checked'])
            self.assertEqual(len(requests), 33)
            self.assertEqual(result['rounds'][-1]['path'], 'final.py')
            self.assertGreaterEqual(result['rounds'][-1]['prompt_tokens'], 262144-1024)
            self.assertLess(result['rounds'][-1]['prompt_tokens']+512, 262144)
            self.assertEqual(len(requests[-1]['history']), 66)
            self.assertEqual(json.loads(Path(folder, 'long-context.json').read_text()), result)

    def test_partial_replay_is_never_a_256k_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            result, requests = self.replay(folder, pilot=2)
            self.assertFalse(result['validated'])
            self.assertEqual(len(requests), 3)

    def test_cache_loss_truncation_and_early_final_generation_fail(self):
        for options in ({'miss_cache': True}, {'truncated': True}, {'early_output': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(Task2Failure):
                    self.replay(folder, **options)


class AutoTests(unittest.TestCase):
    def args(self, folder):
        root = Path(folder)
        (root / 'worker').write_bytes(b'worker')
        (root / 'model').write_bytes(b'model')
        return auto.parse_args(['--server', str(root / 'worker'), '--model', str(root / 'model'),
                                '--out', str(root / 'results')])

    def simulated(self, folder, outcomes, resume=None, output_name='resumed'):
        class Simulated(auto.AutoConfigure):
            @contextmanager
            def worker(self, candidate):
                self.attempt = {'index': len(self.report['attempts']), 'candidate': auto.asdict(candidate),
                                'argv': ['worker', *candidate.flags()], 'props': {'kvmem': {'host_payload_budget_bytes': 9000}},
                                'memory': {'samples': 10}, 'status': 'starting'}
                self.report['attempts'].append(self.attempt)
                outcome = outcomes[len(self.report['attempts']) - 1]
                if outcome == 'planner_rejected_unspecified':
                    raise auto.Failure(outcome, 'legacy combined planner failure')
                actual = auto.Candidate(candidate.budget or 20032, candidate.reserve, candidate.prefill)
                self.attempt['actual'] = auto.asdict(actual)
                self.outcome = outcome
                yield actual

            def validate(self):
                if self.outcome != 'pass':
                    raise auto.Failure(self.outcome, 'controlled validation outcome')
                self.attempt.update(niah={'correct': True}, task2={'validated': True}, passed=True)

        args = self.args(folder)
        if resume:
            args.resume_report = resume
            args.out = Path(folder) / output_name
        return Simulated(args)

    def execute(self, instance):
        with patch.object(auto, 'hardware', return_value={'gpu': ['0', 'uuid']}), \
             patch.object(auto, 'Nvml', return_value=object()):
            return instance.execute()

    def test_default_pass_stops_before_a_second_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = self.simulated(folder, ['pass', 'quality'])
            self.assertEqual(self.execute(instance), 0)
            self.assertEqual(len(instance.report['attempts']), 1)
            self.assertEqual(instance.report['selected']['plan'], {'budget':32768, 'reserve':1024, 'prefill':1024})
            self.assertTrue((instance.out/'selected-config.json').is_file())

    def test_combined_rejection_remains_unknown_and_native_fit_is_frozen_on_output(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = self.simulated(folder, ['planner_rejected_unspecified', 'pass', 'quality'])
            self.assertEqual(self.execute(instance), 0)
            self.assertEqual(len(instance.report['attempts']), 2)
            self.assertEqual(instance.report['attempts'][0]['failure']['kind'], 'planner_rejected_unspecified')
            self.assertIsNone(instance.report['attempts'][1]['candidate']['budget'])
            argv = instance.report['selected']['argv']
            self.assertEqual(argv[argv.index('--kvmem-budget')+1], '20032')

    def test_quality_failure_increases_history_instead_of_shrinking_it(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = self.simulated(folder, ['quality', 'pass'])
            self.assertEqual(self.execute(instance), 0)
            self.assertGreater(instance.report['selected']['plan']['budget'], 32768)
            self.assertGreater(instance.quality_budget_floor, 32768)

    def test_host_or_execution_failure_does_not_publish_or_shrink_b(self):
        for kind in ('host_budget', 'host_memory', 'execution_error', 'timeout', 'task2_incomplete'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                instance = self.simulated(folder, [kind, 'pass'])
                self.assertEqual(self.execute(instance), 2)
                self.assertEqual(len(instance.report['attempts']), 1)
                self.assertFalse((instance.out/'selected-config.json').exists())

    def test_explicit_parameters_are_not_changed_in_resource_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.args(folder)
            args.budget, args.reserve, args.prefill = 32768, 1024, 1024
            plans = auto.next_candidates(auto.Candidate(32768, 1024, 1024), 'gpu_memory', args)
            self.assertEqual(plans, [])
            args.prefill = None
            plans = auto.next_candidates(auto.Candidate(32768, 1024, 1024), 'gpu_memory', args)
            self.assertTrue(plans)
            self.assertTrue(all(p.budget == 32768 and p.reserve == 1024 for p in plans))

    def test_native_planner_error_is_not_guessed_as_gpu_or_host_failure(self):
        self.assertEqual(auto.classify('KVMem auto: no B/R/prefill plan fits available VRAM/RAM', phase='startup'),
                         'planner_rejected_unspecified')
        self.assertEqual(auto.classify('CUDA error: out of memory', phase='runtime'), 'gpu_memory')
        self.assertEqual(auto.classify('unknown native exception', phase='runtime'), 'execution_error')

    def test_runtime_gpu_failure_changes_workspace_instead_of_repeating_same_native_fit(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.args(folder)
            plans = auto.next_candidates(auto.Candidate(17216, 1024, 1024), 'gpu_memory', args)
            self.assertEqual((plans[0].prefill, plans[0].reserve), (512, 512))
            self.assertNotIn(auto.Candidate(None, 1024, 1024), plans)
            small = auto.next_candidates(auto.Candidate(16000, 128, 128), 'gpu_memory', args)
            self.assertLess(small[0].budget, 16000)

    def test_niah_uses_committed_fixture_and_exact_oracle(self):
        entry, messages = auto.load_niah()
        self.assertEqual(entry['oracle']['stdout'], 'ORCHID=493817; COLOR=COBALT')
        self.assertEqual(entry['prompt_tokens'], 260096)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertGreater(len(messages[1]['content']), 1000000)

    def test_task2_adapter_explicitly_requests_streamed_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = auto.AutoConfigure(self.args(folder))
            instance.attempt = {}
            instance.folder = instance.out
            def request(path, body, label=None):
                if path == '/apply-template':
                    return {'prompt': 'rendered'}
                if path == '/tokenize':
                    return {'tokens': [0] * 260096}
                if body.get('stream'):
                    self.assertEqual(body.get('stream_options'), {'include_usage': True})
                    self.assertEqual(body.get('thinking_budget'), 128)
                    self.assertNotIn('reasoning_budget_tokens', body)
                    return {'usage': {'prompt_tokens': 396, 'completion_tokens': 512,
                                      'prompt_tokens_details': {'cached_tokens': 0}}}
                return {'usage': {'prompt_tokens': 260096, 'prompt_tokens_details': {'cached_tokens': 0}}, 'choices': [
                    {'message': {'content': 'ORCHID=493817; COLOR=COBALT'}}]}
            instance.request = request
            def task2(post, *args, **kwargs):
                post('long-base', [], extra={'stream': True, 'reasoning_budget_tokens': 128})
                return {'validated': not kwargs.get('max_tool_rounds')}
            with patch.object(auto, 'run_task2', side_effect=task2):
                instance.validate()
            self.assertTrue(instance.attempt['passed'])

    def test_native_cache_statistics_are_mapped_without_changing_raw_usage(self):
        raw = {'prompt_tokens': 9000, 'prompt_tokens_details': {'cached_tokens': 8000}}
        normalized = auto.task2_usage(raw)
        self.assertEqual(normalized['prompt_cache_hit_tokens'], 8000)
        self.assertNotIn('prompt_cache_hit_tokens', raw)
        for bad in ({'prompt_tokens': 9000},
                    {**raw, 'prompt_cache_hit_tokens': 7},
                    {'prompt_tokens': 9000, 'prompt_cache_hit_tokens': 10000}):
            with self.assertRaises(auto.Failure):
                auto.task2_usage(bad)

    def test_completed_niah_reuse_requires_identical_shape_evidence_and_safe_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = auto.AutoConfigure(self.args(folder))
            evidence = Path(folder) / 'evidence'
            evidence.mkdir()
            actual = {'budget': 17216, 'reserve': 1024, 'prefill': 1024}
            response = {'usage': {'prompt_tokens': 260096, 'prompt_tokens_details': {'cached_tokens': 0}},
                        'choices': [{'message': {'content': 'ORCHID=493817; COLOR=COBALT'}}]}
            path = evidence / 'niah-256k-cold.response.json'
            path.write_text(json.dumps(response))
            source = {'actual': actual, 'niah': {'correct': True}, 'evidence_directory': str(evidence),
                      'memory': {'samples': 10, 'minimum_gpu_free_bytes': 2048 * auto.MIB,
                                 'minimum_host_available_bytes': 4096 * auto.MIB},
                      'requests': [{'label': 'niah-256k-cold', 'complete': True, 'status': 200,
                                    'response': response, 'kvmem_usage': {'host_payload_bytes': 1000}}]}
            instance.previous = {'attempts': [source]}
            instance.attempt = {'actual': dict(actual), 'props': {'kvmem': {'host_payload_budget_bytes': 5000}}}
            self.assertIs(instance.reusable_niah(), source)
            instance.previous['attempts'].append({'actual': actual, 'niah': {}})
            self.assertIs(instance.reusable_niah(), source)
            instance.attempt['actual']['budget'] = 18000
            self.assertIsNone(instance.reusable_niah())
            instance.attempt['actual'] = dict(actual)
            source['requests'][0]['complete'] = False
            self.assertIsNone(instance.reusable_niah())
            source['requests'][0]['complete'] = True
            source['memory']['minimum_gpu_free_bytes'] = 512 * auto.MIB
            self.assertIsNone(instance.reusable_niah())
            source['memory']['minimum_gpu_free_bytes'] = 2048 * auto.MIB
            path.write_text('{}')
            with self.assertRaises(auto.Failure):
                instance.reusable_niah()

    def test_resume_preserves_failure_evidence_and_repeats_all_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.simulated(folder, ['protocol_error'])
            self.assertEqual(self.execute(first), 2)
            resumed = self.simulated(folder, ['unused', 'pass'], first.out / 'report.json')
            self.assertEqual(self.execute(resumed), 0)
            self.assertEqual(len(resumed.report['attempts']), 2)
            self.assertEqual(resumed.report['attempts'][0]['failure']['kind'], 'protocol_error')
            self.assertEqual(resumed.report['attempts'][0]['evidence_directory'], str(first.out/'000'))
            self.assertEqual(resumed.report['attempts'][1]['actual'], resumed.report['attempts'][0]['actual'])

    def test_resume_rejects_changed_binary_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.simulated(folder, ['protocol_error'])
            self.execute(first)
            resumed = self.simulated(folder, ['unused', 'pass'], first.out / 'report.json')
            resumed.previous['fingerprint']['server_sha256'] = 'different'
            with self.assertRaisesRegex(auto.Failure, 'changed since previous run'):
                self.execute(resumed)
            self.assertFalse((resumed.out / 'selected-config.json').exists())

    def test_multiple_interruptions_allow_explicit_retry_of_the_same_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.simulated(folder, ['protocol_error'])
            self.assertEqual(self.execute(first), 2)
            second = self.simulated(folder, ['unused', 'protocol_error'], first.out / 'report.json')
            self.assertEqual(self.execute(second), 2)
            third = self.simulated(folder, ['unused', 'unused', 'pass'], second.out / 'report.json', 'resumed-again')
            self.assertEqual(self.execute(third), 0)
            self.assertEqual(len(third.report['attempts']), 3)
            self.assertEqual(third.report['selected']['plan'], third.report['attempts'][0]['actual'])


if __name__ == '__main__':
    unittest.main()
