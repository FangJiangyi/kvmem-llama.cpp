"""Core HTTP regression for a rewritten trailing message (no timing assertions)."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=os.environ.get('KVMEM_TEST_MODEL'))
    parser.add_argument('--gpu', default=os.environ.get('KVMEM_TEST_GPU'), help='GPU UUID')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if not args.model or not args.gpu or not args.exe.is_file() or not args.model.is_file():
        return 77
    args.out.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    argv = [str(args.exe), '-m', str(args.model), '--host', '127.0.0.1', '--port', str(port),
            '-c', '8192', '-ngl', '99', '-b', '128', '-ub', '128', '--no-ui', '--no-think',
            '--kvmem', '--kvmem-budget', '512', '--kvmem-gen-reserve', '128',
            '--kvmem-sessions', '4',
            '--kvmem-block-tokens', '64', '--kv-dtype', 'q8_0', '--spec-type', 'none', '--kvmem-trace']
    env = {k: v for k, v in os.environ.items() if not k.startswith(('KVMEM_', 'LLAMA_ARG_', 'GGML_'))}
    env.update(CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER='PCI_BUS_ID', GGML_DISABLE_VULKAN='1')
    env['PATH'] = str(args.exe.parent) + os.pathsep + env['PATH']
    (args.out / 'argv.json').write_text(json.dumps({'argv': argv, 'gpu_uuid': args.gpu}, indent=2))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{port}'

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        with opener.open(urllib.request.Request(base + path, data=data,
                         headers={'Content-Type': 'application/json'}), timeout=120) as response:
            return json.load(response)

    log_path = args.out / 'server.log'
    with log_path.open('wb') as log:
        child = subprocess.Popen(argv, env=env, stdout=log, stderr=log,
                                 creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            deadline = time.monotonic() + 120
            while True:
                if child.poll() is not None:
                    raise RuntimeError(f'server exited {child.returncode}')
                try:
                    call('/health')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('server readiness')
                    time.sleep(0.25)
            common = [{'role': 'system', 'content': 'Answer briefly using the archive.'},
                      {'role': 'user', 'content': 'The violet observatory studies pulsars and cosmic rays. ' * 160}]
            history = list(common)
            hits = []
            for turn in range(1, 7):
                offset = log_path.stat().st_size
                messages = history + [{'role': 'user', 'content': f'Tool-call turn {turn} of 100. State one fact.'}]
                response = call('/v1/chat/completions', {'messages': messages, 'max_tokens': 8,
                                'temperature': 0, 'seed': 42, 'ignore_eos': True})
                assert response['usage']['completion_tokens'] == 8, response
                trace = log_path.read_bytes()[offset:].decode(errors='replace')
                found = re.findall(r'prefix_hit_rows=(\d+)', trace)
                assert found, 'missing native prefix reuse evidence'
                hits.append(int(found[-1]))
                history += [{'role': 'assistant', 'content': response['choices'][0]['message']['content'] or 'Recorded.'},
                            {'role': 'tool', 'content': 'Observation: the observatory studies pulsars.'}]
                print(f'turn={turn} reused={hits[-1]}', flush=True)
            assert all(hit > 0 for hit in hits[2:]), hits
            (args.out / 'results.json').write_text(json.dumps({'prefix_hits': hits}, indent=2))
        finally:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
