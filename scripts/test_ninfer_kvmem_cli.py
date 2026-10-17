"""Launch a built NInfer worker with the public KVMem CLI and check real requests.

Uses an existing local artifact and explicit GPU UUID. Owns only its temporary
loopback server; records argv, responses and memory gauges for review.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--gpu', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    command = [str(args.server.resolve()), str(args.model.resolve()),
               '--host', '127.0.0.1', '--port', str(port), '--device', '0', '--device-profile', 'off',
               '--max-context', '8192', '--prefill-chunk', '128', '--kv-dtype', 'int8',
               '--no-thinking', '--kvmem', '--kvmem-budget', '384', '--kvmem-gen-reserve', '128',
               '--kvmem-host-mib', '512', '--kvmem-sessions', '1',
               '--kvmem-disk-mib', '0', '--kvmem-lrfu', '--no-kvmem-lrfu']
    evidence = {'gpu': args.gpu, 'command': command, 'checks': [], 'responses': []}

    def check(name, condition):
        evidence['checks'].append({'name': name, 'pass': bool(condition)})
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        if not condition:
            raise AssertionError(name)

    def request(path, payload=None):
        req = urllib.request.Request(base + path, headers={'Content-Type': 'application/json'},
                                     data=None if payload is None else json.dumps(payload).encode())
        return opener.open(req, timeout=180)

    def get(path):
        with request(path) as response:
            return json.load(response)

    def settled():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            usage = get('/props')['kvmem']['usage']
            if usage['active_sessions'] == 0:
                check('inactive session count within public limit', usage['retained_sessions'] <= 1)
                check('inactive limit reported independently', usage['retained_sessions_limit'] == 1)
                check('native payload within shared H', usage['host_payload_bytes'] <= (512 << 20))
                check('SSD explicitly disabled', usage['disk_payload_budget_bytes'] == 0)
                return usage
            time.sleep(0.03)
        raise AssertionError('request did not retire')

    with (args.output / 'server.log').open('wb') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log,
                                env=dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID'),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 240
            while True:
                if proc.poll() is not None:
                    raise RuntimeError('owned NInfer server exited; inspect server.log')
                try:
                    if get('/health').get('status') == 'ok':
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('NInfer startup')
                time.sleep(0.2)
            config = get('/props')['kvmem']
            check('explicit KVMem mode and B/R reach runtime', config['enabled'] and
                  config['selected_tokens'] == 384 and config['reserve_tokens'] == 128)
            check('shared payload H reaches runtime', config['host_payload_budget_bytes'] == (512 << 20))
            check('fresh worker has no retained histories', settled()['retained_sessions'] == 0)
            with request('/v1/chat/completions', {
                    'messages': [{'role': 'user', 'content': 'Reply with the word READY.'}],
                    'max_tokens': 16, 'temperature': 0}) as response:
                body = json.load(response)
            evidence['responses'].append(body)
            check('chat returns generated content', bool(body['choices'][0]['message']['content'].strip()))
            check('chat reports generated tokens', body['usage']['completion_tokens'] > 0)
            settled()
            active_seen = False
            text = ''
            done = False
            with request('/v1/chat/completions', {
                    'messages': [{'role': 'user', 'content':
                        'Write every integer from 1 to 10000, separated by commas. Do not abbreviate or stop early.'}],
                    'max_tokens': 192, 'temperature': 0, 'stream': True}) as stream:
                for line in stream:
                    if not line.startswith(b'data: '):
                        continue
                    data = line[6:].strip()
                    if data == b'[DONE]':
                        done = True
                        continue
                    event = json.loads(data)
                    evidence['responses'].append(event)
                    for choice in event.get('choices', []):
                        text += choice.get('delta', {}).get('content', '') or ''
                    if text and not active_seen:
                        active_seen = get('/props')['kvmem']['usage']['active_sessions'] > 0
            check('stream returns content and completion marker', bool(text.strip()) and done)
            check('active request is reported separately', active_seen)
            settled()
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=30)
            (args.output / 'results.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
