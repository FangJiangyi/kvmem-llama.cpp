#!/usr/bin/env python3
"""Select the first ninfer configuration passing the existing NIAH and Task 2 tests.

Python 3.11+, Linux, one NVIDIA GPU, one text lane. Uses an existing worker and
artifact. Never builds CUDA, downloads weights, or stops an unrelated service.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request

from ninfer_kvmem_resources import (
    MIB, Monitor, Nvml, atomic_json, check_port, file_digest, hardware,
)
from task2_workload import Task2Failure, run_task2

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = 262144
MANIFEST = ROOT / 'backends/ninfer/examples/cli/manifest.json'
DEFAULT_B, DEFAULT_R, DEFAULT_P = 32768, 1024, 1024


class Failure(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Candidate:
    budget: int | None
    reserve: int
    prefill: int

    def flags(self):
        args = ['--kvmem-gen-reserve', str(self.reserve), '--prefill-chunk', str(self.prefill)]
        if self.budget is not None:
            args += ['--kvmem-budget', str(self.budget)]
        return args


def classify(text, *, phase):
    """Only classify explicit evidence. The legacy combined planner error stays combined."""
    lower = text.lower()
    if 'kvmem auto: no b/r/prefill plan fits' in lower:
        return 'planner_rejected_unspecified'
    if any(x in lower for x in ('cuda_error_out_of_memory', 'cudaerrormemoryallocation',
                                'cuda out of memory', 'cuda error: out of memory')):
        return 'gpu_memory'
    if any(x in lower for x in ('host payload budget exceeded', 'host/disk capacity',
                                'host budget is too small', 'host/ssd payload budget',
                                'kvmem history exceeds', 'kvmem host budget',
                                'kvmem retained payload budget')):
        return 'host_budget'
    if any(x in lower for x in ('std::bad_alloc', 'cannot allocate memory')):
        return 'host_memory'
    return 'startup_error' if phase == 'startup' else 'execution_error'


def load_niah(manifest=MANIFEST):
    entry = next(c for c in json.loads(manifest.read_text())['cases'] if c['name'] == 'long_niah_256k')
    path = manifest.parent / entry['messages']
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['messages_sha256'] or entry['oracle']['mode'] != 'exact':
        raise ValueError('NIAH fixture identity or exact-answer contract changed')
    return entry, json.loads(raw)


def task2_usage(usage):
    """Translate OpenAI cache accounting for the unchanged llama-style Task 2 checker.

    Keep the raw usage in the HTTP record and wire log. Missing or contradictory
    statistics are errors, never inferred from timing or successful generation.
    """
    if not isinstance(usage, dict):
        raise Failure('protocol_error', 'stream has no usage')
    nested = (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
    flat = usage.get('prompt_cache_hit_tokens')
    cached = flat if flat is not None else nested
    if (not isinstance(cached, int) or cached < 0 or cached > usage.get('prompt_tokens', -1)
            or flat is not None and nested is not None and flat != nested):
        raise Failure('protocol_error', 'missing or contradictory prompt cache token accounting')
    return {**usage, 'prompt_cache_hit_tokens': cached}


def next_candidates(current, kind, args):
    """Delegate B pricing/search to the built native planner, never a Python cost model."""
    if kind not in {'planner_rejected_unspecified', 'gpu_memory', 'quality', 'quality_budget_floor'}:
        return []
    if kind == 'quality' and args.budget is not None:
        return []
    candidates = []
    if kind == 'quality':
        # A measured quality miss is not repaired by reducing B. First try more history.
        b = min(args.max_budget, (current.budget * 5 // 4 + 63) // 64 * 64)
        if b > current.budget:
            candidates.append(Candidate(b, current.reserve, current.prefill))
    elif args.budget is None and kind != 'gpu_memory':
        candidates.append(Candidate(None, current.reserve, current.prefill))
    # Native B fitting at smaller workspaces can retain more history on the same GPU.
    p = current.prefill
    while p > 128:
        p = max(128, p // 2 // 128 * 128)
        r = current.reserve if args.reserve is not None else min(current.reserve, p)
        if args.prefill is not None:
            break
        if p <= r:
            candidates.append(Candidate(args.budget, r, p))
    if current.prefill == 128 and args.reserve is None and current.reserve > 128:
        candidates.append(Candidate(args.budget, 128, 128))
    if kind == 'gpu_memory' and not candidates and args.budget is None and current.budget:
        b = current.budget * 3 // 4 // 64 * 64
        if b >= args.min_budget:
            candidates.append(Candidate(b, current.reserve, current.prefill))
    return list(dict.fromkeys(candidates))


class AutoConfigure:
    def __init__(self, args):
        self.args = args
        self.server = args.server.resolve(strict=True)
        self.model = args.model.resolve(strict=True)
        self.out = args.out.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        if any(self.out.iterdir()):
            raise ValueError('use an empty output directory; earlier evidence is never overwritten')
        self.entry, self.messages = load_niah()
        self.base = f'http://127.0.0.1:{args.port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.deadline = time.monotonic() + args.total_timeout
        self.process = self.monitor = self.attempt = None
        self.quality_budget_floor = args.min_budget
        self.report = {'schema': 1, 'status': 'running', 'context': CONTEXT,
                       'objective': 'first complete acceptance, no speed ranking',
                       'options': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                       'workloads': {'task2': 'scripts/task2_workload.py (shared original replay)',
                                     'niah': self.entry},
                       'attempts': [], 'selected': None,
                       'limitations': ['One GPU, one text lane; no MTP.',
                           'Each candidate restarts the worker and reloads real weights.',
                           'The built planner has no plan-only or structured rejection interface.',
                           'Memory figures are sampled; NIAH is one middle-depth recall task.',
                           'Task 2 checks history execution, not final program semantics.']}
        self.previous = None
        if args.resume_report:
            self.previous = json.loads(args.resume_report.read_text())
            if self.previous.get('selected') or not self.previous.get('attempts'):
                raise ValueError('resume requires a non-selected run with at least one attempted candidate')
            fixed = ('server', 'model', 'budget', 'reserve', 'prefill', 'host_mib', 'min_budget',
                     'max_budget', 'kv_dtype', 'no_graphs', 'gpu_floor_mib', 'ram_floor_mib')
            if (self.previous.get('context') != CONTEXT
                    or any(self.previous['options'].get(key) != self.report['options'].get(key) for key in fixed)
                    or self.previous.get('workloads') != self.report['workloads']):
                raise ValueError('resume requires the same model, execution settings, floors and workloads')
            self.report['resume_from'] = str(args.resume_report.resolve())
        self.save()

    def save(self):
        atomic_json(self.out / 'report.json', self.report)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise Failure('timeout', 'total configuration deadline exceeded')
        if self.monitor:
            try:
                self.monitor.check()
            except RuntimeError as error:
                summary = self.monitor.summary
                if summary['minimum_host_available_bytes'] < self.args.ram_floor_mib * MIB:
                    kind = 'host_memory'
                elif summary['minimum_gpu_free_bytes'] < self.args.gpu_floor_mib * MIB:
                    kind = 'gpu_memory'
                else:
                    kind = 'monitor_error'
                raise Failure(kind, str(error)) from error

    @contextmanager
    def timeout(self, seconds):
        """A wall-clock deadline also interrupts a blocked or indefinitely streaming request."""
        elapsed = threading.Event()
        process = self.process
        def expired():
            elapsed.set()
            if process is not None and process.poll() is None:
                process.terminate()
        duration = max(.01, min(seconds, self.deadline - time.monotonic()))
        timer = threading.Timer(duration, expired)
        timer.daemon = True
        timer.start()
        try:
            yield duration
        finally:
            timer.cancel()
            timer.join()
            if elapsed.is_set():
                raise Failure('timeout', f'operation exceeded its {duration:.1f}s deadline')

    def request(self, path, body=None, *, label=None):
        self.check()
        record = None
        if label:
            atomic_json(self.folder / (label + '.request.json'), body)
            record = {'label': label, 'status': None, 'complete': False}
            self.attempt['requests'].append(record)
            self.save()
        req = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            with self.timeout(self.args.request_timeout) as seconds:
                with self.opener.open(req, timeout=seconds) as response:
                    if record is not None:
                        record['status'] = response.status
                    if body and body.get('stream'):
                        chunks, done, first_token = [], False, None
                        with (self.folder / (label + '.response.txt')).open('w') as wire_log:
                            for wire in response:
                                self.check()
                                line = wire.decode('utf-8')
                                wire_log.write(line)
                                wire_log.flush()
                                if line.strip() == 'data: [DONE]':
                                    done = True
                                    break
                                if not line.startswith('data: {'):
                                    continue
                                chunk = json.loads(line[6:])
                                if 'error' in chunk:
                                    raise Failure(classify(json.dumps(chunk), phase='runtime'), json.dumps(chunk))
                                if first_token is None and any(c.get('delta', {}).get('content') or
                                        c.get('delta', {}).get('reasoning_content') for c in chunk.get('choices', [])):
                                    first_token = time.monotonic() - started
                                chunks.append(chunk)
                        if not done:
                            raise Failure('protocol_error', 'SSE ended without [DONE]')
                        usage = next((c['usage'] for c in reversed(chunks) if c.get('usage')), None)
                        if not usage:
                            raise Failure('protocol_error', 'stream has no usage')
                        record.update(usage=usage, ttft_s=first_token,
                            content=''.join(c.get('delta', {}).get('content') or '' for chunk in chunks
                                            for c in chunk.get('choices', [])))
                        result = record
                    else:
                        result = json.load(response)
                        if record is not None:
                            atomic_json(self.folder / (label + '.response.json'), result)
                            record.update(usage=result.get('usage'), response=result)
            self.check()
            if record is not None:
                record.update(complete=True, elapsed_s=time.monotonic() - started)
                usage = record.get('usage') or {}
                if usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0) > CONTEXT:
                    raise Failure('protocol_error', 'usage exceeded the declared context')
                props = self.request('/props')['kvmem']
                record['kvmem_usage'] = props['usage']
                if props['usage']['host_payload_bytes'] > props['host_payload_budget_bytes']:
                    raise Failure('host_budget', 'observed Host payload exceeded H')
                self.save()
                print(label, usage.get('prompt_tokens'), usage.get('completion_tokens'),
                      f'{record["elapsed_s"]:.2f}s', flush=True)
            return result
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors='replace')
            if record is not None:
                record.update(status=error.code, error=text)
                atomic_json(self.folder / (label + '.http-error.json'), {'status': error.code, 'body': text})
            raise Failure(classify(text, phase='runtime'), f'{path}: HTTP {error.code}: {text}') from error
        except (OSError, ValueError) as error:
            self.check()
            tail = (self.folder / 'server.log').read_text(errors='replace')[-12000:] if self.attempt else ''
            kind = classify(tail, phase='runtime')
            raise Failure(kind, f'{path}: {type(error).__name__}: {error}; {tail[-2000:]}') from error
        finally:
            if record is not None:
                self.save()

    @contextmanager
    def worker(self, candidate):
        self.attempt = None
        self.check()
        check_port(self.args.port)
        busy = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid',
                                       '--format=csv,noheader,nounits'], text=True).strip()
        if busy:
            raise Failure('gpu_busy', f'GPU has other compute processes; none stopped: {busy}')
        index = len(self.report['attempts'])
        self.folder = self.out / f'{index:03d}'
        self.folder.mkdir()
        argv = [str(self.server), str(self.model), '--host', '127.0.0.1', '--port', str(self.args.port),
                '--max-context', str(CONTEXT), '--max-concurrency', '1', '--kv-dtype', self.args.kv_dtype,
                '--request-log-jsonl', str(self.folder / 'requests.jsonl'),
                '--no-thinking', '--device-profile', 'off', '--kvmem-auto',
                '--kvmem-auto-vram-reserve-mib', str(self.args.gpu_floor_mib),
                '--kvmem-auto-ram-reserve-mib', str(self.args.ram_floor_mib), *candidate.flags()]
        if self.args.host_mib is not None:
            argv += ['--kvmem-host-mib', str(self.args.host_mib)]
        if self.args.no_graphs:
            argv.append('--no-cuda-graph')
        self.attempt = {'index': index, 'candidate': asdict(candidate), 'argv': argv,
                        'requests': [], 'status': 'starting'}
        self.report['attempts'].append(self.attempt)
        self.save()
        print('CANDIDATE', index, asdict(candidate), flush=True)
        started = time.monotonic()
        log_path = self.folder / 'server.log'
        with log_path.open('w') as log:
            self.process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
            self.attempt['pid'] = self.process.pid
            self.monitor = Monitor(self.process, self.nvml,
                                   self.args.gpu_floor_mib * MIB, self.args.ram_floor_mib * MIB)
            self.monitor.thread.start()
            try:
                with self.timeout(self.args.startup_timeout):
                    while True:
                        self.check()
                        if self.process.poll() is not None:
                            text = log_path.read_text(errors='replace')
                            raise Failure(classify(text, phase='startup'), text[-6000:])
                        # Startup's ordinary 503 is loading, not a candidate failure.
                        try:
                            with self.opener.open(self.base + '/health', timeout=1) as response:
                                if response.status == 200:
                                    break
                        except (OSError, urllib.error.HTTPError):
                            pass
                        time.sleep(.25)
                self.attempt['startup_seconds'] = time.monotonic() - started
                props = self.request('/props')
                self.attempt['props'] = props
                p = props['kvmem']
                actual = Candidate(p['selected_tokens'], p['reserve_tokens'], p['prefill_chunk'])
                if (props.get('backend') != 'ninfer' or props.get('total_slots') != 1 or not p.get('automatic')
                        or actual.reserve != candidate.reserve or actual.prefill != candidate.prefill
                        or candidate.budget is not None and actual.budget != candidate.budget
                        or self.args.host_mib is not None and p['host_payload_budget_bytes'] != self.args.host_mib * MIB
                        or not p['capabilities'].get('history_reuse') or p['capabilities'].get('mtp_drafts')):
                    raise Failure('configuration_mismatch', 'worker did not apply the requested fixed configuration')
                context = props['default_generation_settings']['n_ctx']
                if context != CONTEXT:
                    raise Failure('configuration_mismatch', 'worker changed the logical context')
                self.attempt.update(actual=asdict(actual), status='validating')
                if actual.budget < self.quality_budget_floor:
                    raise Failure('quality_budget_floor', f'native B={actual.budget} is below required {self.quality_budget_floor}')
                self.save()
                yield actual
                self.check()
            finally:
                self.monitor.stop.set()
                self.monitor.thread.join(timeout=2)
                self.attempt['memory'] = dict(self.monitor.summary)
                self.attempt['monitor_error'] = self.monitor.error
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=10)
                self.attempt['exit_code'] = self.process.returncode
                self.attempt['elapsed_seconds'] = time.monotonic() - started
                text = log_path.read_text(errors='replace')
                self.attempt['native_plan_log'] = [line for line in text.splitlines() if 'KVMem auto:' in line]
                self.process = self.monitor = None
                self.save()
                if self.attempt.get('passed') and (not self.attempt['memory']['samples']
                        or self.attempt['monitor_error'] or self.attempt['exit_code'] not in (0, -15)
                        or re.search(r'\bFATAL\b', text)):
                    self.attempt['passed'] = False
                    raise Failure('cleanup_error', 'completed workload had missing monitoring or abnormal worker shutdown')

    def task2_post(self, label, messages, *, extra):
        payload = {'messages': messages, 'max_tokens': 128, 'temperature': 0,
                   'seed': 42, 'enable_thinking': False, **extra,
                   'stream_options': {'include_usage': True}}
        if 'reasoning_budget_tokens' in payload:
            payload['thinking_budget'] = payload.pop('reasoning_budget_tokens')
        record = self.request('/v1/chat/completions', payload, label=label)
        normalized = task2_usage(record['usage'])
        record['task2_usage'] = normalized
        self.save()
        return {**record, 'usage': normalized}

    def protocol_preflight(self):
        folder = self.folder / 'protocol-preflight'
        folder.mkdir()
        def post(label, messages, *, extra):
            return self.task2_post('protocol-' + label, messages, extra=extra)
        try:
            result = run_task2(post, folder, context=CONTEXT, max_tool_rounds=2)
        except Task2Failure as error:
            raise Failure('protocol_error', f'Task 2 protocol preflight: {error}') from error
        if result['validated']:
            raise Failure('protocol_error', 'partial protocol preflight was incorrectly labeled complete')
        self.attempt['protocol_preflight'] = result
        self.save()

    def reusable_niah(self):
        if not self.previous:
            return None
        for source in reversed(self.previous['attempts']):
            valid = self.verified_niah_source(source)
            if valid:
                return valid
        return None

    def verified_niah_source(self, source):
        if (source.get('actual') != self.attempt['actual'] or not source.get('niah', {}).get('correct')
                or source.get('failure', {}).get('kind') in ('gpu_memory', 'host_memory', 'quality')):
            return None
        record = next((r for r in source['requests'] if r['label'] == 'niah-256k-cold' and r.get('complete')), None)
        memory = source.get('memory', {})
        if (not record or not memory.get('samples') or source.get('monitor_error')
                or memory.get('minimum_gpu_free_bytes', 0) < self.args.gpu_floor_mib * MIB
                or memory.get('minimum_host_available_bytes', 0) < self.args.ram_floor_mib * MIB):
            return None
        response = record.get('response', {})
        usage = response.get('usage', {})
        answer = response.get('choices', [{}])[0].get('message', {}).get('content', '')
        if (record.get('status') != 200 or answer.strip() != self.entry['oracle']['stdout']
                or not 250000 <= usage.get('prompt_tokens', 0)
                or usage['prompt_tokens'] + self.entry['max_new'] > CONTEXT
                or (response.get('timings') or {}).get('cache_n', 0)
                or task2_usage(usage)['prompt_cache_hit_tokens']
                or record.get('kvmem_usage', {}).get('host_payload_bytes', 1 << 63)
                    > self.attempt['props']['kvmem']['host_payload_budget_bytes']):
            return None
        path = Path(source['evidence_directory']) / 'niah-256k-cold.response.json'
        if not path.is_file() or json.loads(path.read_text()) != response:
            raise Failure('resume_mismatch', 'saved completed NIAH response is missing or inconsistent')
        return source

    def validate(self):
        # Exercise the existing two-round diagnostic before an expensive cold request.
        self.protocol_preflight()
        source = self.reusable_niah()
        if source:
            self.attempt['niah'] = {**source['niah'], 'reused_from': source['evidence_directory']}
            self.attempt['niah_token_check'] = source['niah_token_check']
            self.attempt['niah_memory_evidence'] = source['memory']
            self.save()
            print('NIAH evidence reused from completed cold run:', source['evidence_directory'], flush=True)
        else:
            self.validate_niah()
        try:
            result = run_task2(self.task2_post, self.folder, context=CONTEXT)
        except Task2Failure as error:
            raise Failure('task2_incomplete', str(error)) from error
        if not result['validated']:
            raise Failure('task2_incomplete', 'diagnostic/partial Task 2 is not a 256K pass')
        self.attempt['task2'] = result
        self.attempt['passed'] = True

    def validate_niah(self):
        # Use the committed NIAH prompt and oracle. No new long-document generator.
        rendered = self.request('/apply-template', {'messages': self.messages, 'enable_thinking': False})['prompt']
        tokens = len(self.request('/tokenize', {'content': rendered})['tokens'])
        self.attempt['niah_token_check'] = {'actual': tokens, 'manifest_reference': self.entry['prompt_tokens']}
        if not 250000 <= tokens or tokens + self.entry['max_new'] > CONTEXT:
            raise Failure('workload_error', f'existing 256K NIAH fixture has incompatible token count: {tokens}')
        payload = {'messages': self.messages, 'temperature': 0, 'seed': 42,
                   'enable_thinking': False, 'max_tokens': self.entry['max_new']}
        response = self.request('/v1/chat/completions', payload, label='niah-256k-cold')
        if response['usage']['prompt_tokens'] != tokens:
            raise Failure('protocol_error', 'NIAH prepared and observed token counts differ')
        if ((response.get('timings') or {}).get('cache_n', 0)
                or task2_usage(response['usage'])['prompt_cache_hit_tokens']):
            raise Failure('protocol_error', 'first NIAH did not run cold')
        answer = response['choices'][0]['message'].get('content') or ''
        correct = answer.strip() == self.entry['oracle']['stdout']
        self.attempt['niah'] = {'correct': correct, 'actual': answer, 'expected': self.entry['oracle']['stdout']}
        self.save()
        if not correct:
            raise Failure('quality', f'NIAH exact-answer miss: {answer!r}')

    def execute(self):
        try:
            self.report['hardware'] = hw = hardware()
            self.nvml = Nvml(hw['gpu'][1])
            self.report['fingerprint'] = {'server_sha256': file_digest(self.server),
                                          'model_sha256': file_digest(self.model),
                                          'task2_sha256': file_digest(ROOT / 'scripts/task2_workload.py')}
            self.save()
            queue = [Candidate(self.args.budget or DEFAULT_B, self.args.reserve or DEFAULT_R,
                               self.args.prefill or DEFAULT_P)]
            tried = set()
            if self.previous:
                if (self.previous.get('fingerprint') != self.report['fingerprint']
                        or self.previous.get('hardware') != hw):
                    raise Failure('resume_mismatch', 'worker, model, Task 2 or hardware changed since previous run')
                self.report['attempts'] = list(self.previous['attempts'])
                for attempt in self.report['attempts']:
                    attempt.setdefault('evidence_directory',
                        str(self.args.resume_report.resolve().parent / f'{attempt["index"]:03d}'))
                    if attempt.get('failure', {}).get('kind') == 'quality':
                        self.quality_budget_floor = max(self.quality_budget_floor, attempt['actual']['budget'] + 64)
                last = self.report['attempts'][-1]
                # Repeat all acceptance gates; a partial previous pass is never promoted.
                queue = [Candidate(**last.get('actual', last['candidate']))]
                tried = {Candidate(**a['candidate']) for a in self.report['attempts'][:-1]}
                tried.discard(queue[0])  # An explicit resume may repeat the same shape after several interruptions.
                self.save()
            while queue and len(self.report['attempts']) < self.args.max_attempts:
                self.check()
                candidate = queue.pop(0)
                if candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    with self.worker(candidate) as actual:
                        self.validate()
                    selected_argv = list(self.attempt['argv'])
                    if '--kvmem-budget' not in selected_argv:
                        selected_argv += ['--kvmem-budget', str(actual.budget)]
                    selected = {'status': 'validated_256k', 'plan': asdict(actual),
                                'host_payload_budget_bytes': self.attempt['props']['kvmem']['host_payload_budget_bytes'],
                                'context': CONTEXT, 'fingerprint': self.report['fingerprint'],
                                'hardware': hw, 'attempt': self.attempt['index'], 'argv': selected_argv,
                                'host_policy': 'explicit' if self.args.host_mib is not None else 'native_auto_after_weights',
                                'niah': self.attempt['niah'], 'task2': self.attempt['task2'],
                                'memory': self.attempt['memory'], 'limitations': self.report['limitations']}
                    if 'niah_memory_evidence' in self.attempt:
                        selected['niah_source_worker_memory'] = self.attempt['niah_memory_evidence']
                    atomic_json(self.out / 'selected-config.json', selected)
                    (self.out / 'selected-command.txt').write_text(shlex.join(selected_argv) + '\n')
                    self.attempt['status'] = 'passed'
                    self.report.update(status='validated_256k', selected=selected)
                    self.save()
                    print('SELECTED', asdict(actual), flush=True)
                    return 0
                except Failure as error:
                    if self.attempt is None:
                        raise
                    self.attempt.update(status='rejected', failure={'kind': error.kind, 'message': str(error)})
                    self.save()
                    print('REJECTED', error.kind, str(error)[-1000:], flush=True)
                    actual = Candidate(**self.attempt.get('actual', asdict(candidate)))
                    if error.kind == 'quality':
                        self.quality_budget_floor = max(self.quality_budget_floor, actual.budget + 64)
                    new = next_candidates(actual, error.kind, self.args)
                    if not new:
                        self.report.update(status='failed', failure=self.attempt['failure'])
                        self.save()
                        return 2
                    queue = list(dict.fromkeys(new + queue))
            self.report.update(status='failed', failure={'kind': 'search_exhausted',
                'message': 'No complete pass within the bounded native-planner search; no configuration published.'})
            self.save()
            return 2
        except BaseException as error:
            self.report.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                               failure={'kind': getattr(error, 'kind', 'environment_error'), 'message': str(error)})
            self.save()
            raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--resume-report', type=Path,
                        help='retain earlier evidence, recheck fingerprints, and fully retry the last candidate')
    parser.add_argument('--port', type=int, default=19240)
    parser.add_argument('--budget', type=int, help='lock B; otherwise start at the worker default 32768')
    parser.add_argument('--reserve', type=int, help='lock R; otherwise start at the worker default 1024')
    parser.add_argument('--prefill', type=int, help='lock prefill; otherwise start at the worker default 1024')
    parser.add_argument('--host-mib', type=int, help='lock H; otherwise use built native auto-H')
    parser.add_argument('--min-budget', type=int, default=8192, help='declared B search floor, not a quality guarantee')
    parser.add_argument('--max-budget', type=int, default=65536, help='upper bound for quality-driven B increases')
    parser.add_argument('--kv-dtype', choices=('int8', 'bf16', 'fp8', 'nvfp4', 'k8v4'), default='int8')
    parser.add_argument('--no-graphs', action='store_true')
    parser.add_argument('--gpu-floor-mib', type=int, default=1024)
    parser.add_argument('--ram-floor-mib', type=int, default=2048)
    parser.add_argument('--startup-timeout', type=float, default=300)
    parser.add_argument('--request-timeout', type=float, default=1800)
    parser.add_argument('--total-timeout', type=float, default=10800)
    parser.add_argument('--max-attempts', type=int, default=12)
    args = parser.parse_args(argv)
    b, r, p = args.budget or DEFAULT_B, args.reserve or DEFAULT_R, args.prefill or DEFAULT_P
    if (any(value is not None and value <= 0 for value in (args.budget, args.reserve, args.prefill, args.host_mib))
            or min(b, r, p) < 128 or b % 64 or r % 64 or p % 128 or p > r or b + r > CONTEXT
            or args.min_budget < 128 or args.min_budget % 64 or args.max_budget % 64
            or args.min_budget > args.max_budget or b > args.max_budget
            or args.budget is not None and args.budget < args.min_budget
            or min(args.gpu_floor_mib, args.ram_floor_mib) < 0 or args.max_attempts < 1
            or not 0 < args.port < 65536
            or any(not math.isfinite(v) or v <= 0 for v in
                   (args.startup_timeout, args.request_timeout, args.total_timeout))):
        parser.error('invalid candidate alignment, limits, resource floors or timeouts')
    return args


def main(argv=None):
    return AutoConfigure(parse_args(argv)).execute()


if __name__ == '__main__':
    raise SystemExit(main())
