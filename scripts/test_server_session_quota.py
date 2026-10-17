"""Real-model checks of the inactive-history quota, independent of lane count.

Uses only an explicitly selected local model/GPU and its own temporary server.
Checks cache ownership, eviction, overlapping requests and disconnect cleanup;
does not compare generated prose or benchmark performance.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--gpu', required=True, help='GPU UUID')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for lanes, limit in [(1, 2), (2, 1)]:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        command = [str(args.server.resolve()), '-m', str(args.model.resolve()), '--device', 'CUDA0',
                   '--host', '127.0.0.1', '--port', str(port), '--parallel', str(lanes),
                   '--threads-http', '8', '--ctx-size', '8192', '-b', '128', '-ub', '128',
                   '--kvmem', '--kvmem-budget', '1024', '--kvmem-gen-reserve', '512',
                   '--kvmem-host-mib', '512', '--kvmem-sessions', str(limit),
                   '--kvmem-disk-mib', '0', '--no-kvmem-lrfu', '-fa', 'on',
                   '--reasoning-effort', 'none', '--temp', '0', '--no-webui', '-t', '4', '-tb', '4']
        evidence = {'lanes': lanes, 'limit': limit, 'gpu': args.gpu, 'command': command, 'checks': []}
        results.append(evidence)

        def check(name, condition):
            evidence['checks'].append({'name': name, 'pass': bool(condition)})
            print(f'{"PASS" if condition else "FAIL"} C={lanes} N={limit}: {name}', flush=True)
            if not condition:
                raise AssertionError(name)

        def request(path, payload=None):
            req = urllib.request.Request(base + path, headers={'Content-Type': 'application/json'},
                                         data=None if payload is None else json.dumps(payload).encode())
            with opener.open(req, timeout=120) as response:
                return json.load(response)

        def stats():
            return request('/props')['kvmem']['usage']

        def settled():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = stats()
                if current['active_sessions'] == 0:
                    check('inactive quota after request completion', current['retained_sessions'] <= limit)
                    check('configured limit reported as inactive histories', current['retained_sessions_limit'] == limit)
                    return current
                time.sleep(0.03)
            raise AssertionError('active sessions did not retire')

        def messages(name):
            return [{'role': 'user', 'content': f'Conversation {name}. ' +
                     ' '.join(f'Record {i}: the value is {name}.' for i in range(80)) +
                     ' Reply with OK.'}]

        def ask(name, history=None, tokens=24):
            return request('/v1/chat/completions', {
                'messages': history or messages(name), 'kvmem': {'conversation_id': name},
                'max_tokens': tokens, 'temperature': 0, 'reasoning_effort': 'none'})

        with (args.output / f'lanes-{lanes}.log').open('wb') as log:
            proc = subprocess.Popen(command, stdout=log, stderr=log,
                                    env=dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID'),
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic() + 180
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError('owned test server exited; inspect log')
                    try:
                        if request('/health').get('status') == 'ok':
                            break
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError('test server startup')
                    time.sleep(0.15)
                check('empty lanes are not retained histories', stats()['retained_sessions'] == 0)
                first = ask('A')
                settled()
                followup = messages('A') + [first['choices'][0]['message'], {'role': 'user', 'content': 'Reply OK again.'}]
                reused = ask('A', followup)
                check('same-session checkpoint reused', reused['usage']['prompt_cache_hit_tokens'] > 0)
                settled()
                for name in ['B', 'C', 'D']:
                    response = ask(name)
                    check('unrelated named history is isolated', response['usage']['prompt_cache_hit_tokens'] == 0)
                    settled()
                evicted = ask('A', followup)
                check('oldest inactive history evicted', evicted['usage']['prompt_cache_hit_tokens'] == 0)
                settled()
                if lanes == 2:
                    long_prompt = [{'role': 'user', 'content': 'Write every integer from 1 to 10000, separated by commas. Never abbreviate.'}]
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        jobs = [pool.submit(ask, name, long_prompt, 512) for name in ['E', 'F']]
                        peak = 0
                        queued = None
                        while not all(job.done() for job in jobs):
                            current = stats()
                            peak = max(peak, current['active_sessions'])
                            if current['active_sessions'] == 2 and queued is None:
                                # Its cached preview may be evicted while it waits.
                                # Admission must replan safely, not hold stale KV.
                                queued = pool.submit(ask, 'A', followup)
                            if current['retained_sessions'] > limit:
                                raise AssertionError('active requests broke inactive quota')
                            time.sleep(0.01)
                        for job in jobs:
                            job.result()
                        check('queued history survives eviction or safely recomputes',
                              queued is not None and bool(queued.result()['choices']))
                    check('two active requests with only one retained-history allowance', peak == 2)
                    settled()
                before = stats()['retained_sessions']
                check('current llama output capability is reported', request('/props')['kvmem']['generation_limit'] == 512)
                try:
                    ask('invalid', tokens=1.5)
                    raise AssertionError('invalid output limit was accepted')
                except urllib.error.HTTPError as error:
                    check('invalid output limit fails before execution', error.code == 400)
                check('rejected request does not change retained histories', settled()['retained_sessions'] == before)
                req = urllib.request.Request(base + '/v1/chat/completions',
                    data=json.dumps({'messages': [{'role': 'user', 'content': 'Count from 1 to 10000 without abbreviating.'}],
                                     'kvmem': {'conversation_id': 'disconnect'}, 'max_tokens': 512,
                                     'stream': True, 'reasoning_effort': 'none'}).encode(),
                    headers={'Content-Type': 'application/json'})
                with opener.open(req, timeout=120) as stream:
                    stream.readline()
                settled()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=30)
                (args.output / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
