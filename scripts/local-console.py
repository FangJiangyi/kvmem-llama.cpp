"""Local, independently hosted configuration UI and owned inference-process controller.

Uses the selected checkout's canonical launcher in DryRun mode; never constructs
shell command strings. No third-party Python dependencies. Windows launch support.
"""
import argparse
import copy
import ctypes
from ctypes import wintypes
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = dict(Backend='ninfer', Model='', Worker='', Gpu='', Context=262144,
                Budget=32768, Reserve=1024, HostMiB=16384, Prefill=128,
                MaxTokens=1024, Port=18200, ListenHost='127.0.0.1',
                SinkTokens=12288, RecentTokens=1024)
STRINGS = {'Backend', 'Model', 'Worker', 'Gpu', 'ListenHost', 'KvType', 'DiskPath',
           'VisionResidency',
           'DeviceProfile', 'ScoreAggregation'}
INTEGERS = {'Context', 'Budget', 'Reserve', 'HostMiB', 'Prefill', 'MaxTokens', 'Port',
            'MtpDrafts', 'NgramDrafts', 'NgramMinMatch', 'Concurrency', 'RetainedSessions',
            'VisionTokens', 'ThinkingBudget', 'SinkTokens', 'RecentTokens', 'DiskMiB'}
BOOLEANS = {'AdaptiveMtp', 'FastPrefill', 'Vision', 'NoLrfu'}
DECIMALS = {'LrfuLambda', 'LrfuTopFraction'}
# Deliberately limited native escape hatch. Common options must not be duplicated.
NATIVE = {'llamacpp': {'--threads', '--threads-batch', '--n-gpu-layers'},
          'ninfer': {'--threads'}}


def normalize(data, backends):
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
            or data.get('schema_version') != 1 or set(data) != {'schema_version', 'parameters'}):
        raise ValueError('Unsupported configuration schema')
    params = data.get('parameters')
    if not isinstance(params, dict):
        raise ValueError('parameters must be an object')
    allowed = STRINGS | INTEGERS | BOOLEANS | DECIMALS | {'StageLayers', 'WorkerArgs'}
    if set(params) - allowed:
        raise ValueError('Unknown parameters: ' + ', '.join(sorted(set(params) - allowed)))
    p = copy.deepcopy(params)
    if p.get('Backend') not in NATIVE or p.get('Backend') not in backends:
        raise ValueError('This backend is not installed in the selected backend checkout')
    for key, value in p.items():
        if key in STRINGS and (not isinstance(value, str) or '\x00' in value or len(value) > 8192):
            raise ValueError(f'{key} must be a string without NUL characters')
        if key in INTEGERS and (type(value) is not int or not 0 <= value <= 2147483647):
            raise ValueError(f'{key} must be a nonnegative integer')
        if key in BOOLEANS and type(value) is not bool:
            raise ValueError(f'{key} must be boolean')
        if key in DECIMALS and (type(value) not in (int, float) or not 0 <= value <= 1):
            raise ValueError(f'{key} must be between 0 and 1')
    if p.get('ListenHost', '127.0.0.1') not in {'127.0.0.1', '0.0.0.0'}:
        raise ValueError('ListenHost must be 127.0.0.1 or 0.0.0.0')
    if not 1 <= p.get('Port', 18200) <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    gpu = p.get('Gpu', '')
    if gpu and not all(re.fullmatch(r'GPU-[0-9a-fA-F-]{8,64}', x) for x in gpu.split(',')):
        raise ValueError('Select GPUs by UUID, separated by commas')
    layers = p.get('StageLayers', [])
    if not isinstance(layers, list) or len(layers) > 8 or any(type(x) is not int or x < 1 for x in layers):
        raise ValueError('StageLayers must contain positive integers')
    args = p.get('WorkerArgs', [])
    if not isinstance(args, list) or len(args) > 20 or any(not isinstance(x, str) for x in args):
        raise ValueError('Native arguments must be an array of strings')
    if len(args) % 2:
        raise ValueError('Native arguments must be option/value pairs')
    seen = set()
    for flag, value in zip(args[::2], args[1::2]):
        if flag not in NATIVE[p['Backend']] or flag in seen or not re.fullmatch(r'\d{1,6}', value):
            raise ValueError(f'Unsupported or duplicate native option: {flag}')
        seen.add(flag)
    return {'schema_version': 1, 'parameters': p}


class WindowsJob:
    """Keep wrappers and their workers in an owned, kill-on-close process tree."""
    def __init__(self):
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
                      'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
                      'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                      'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
                      'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL)}
        for name, (argtypes, restype) in signatures.items():
            fn = getattr(self.kernel, name); fn.argtypes = argtypes; fn.restype = restype
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED(); limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close(); raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill(); process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Controller:
    def __init__(self, backend_root, state_dir, port):
        self.backend_root = Path(backend_root).resolve()
        self.state_dir = Path(state_dir).resolve(); self.state_dir.mkdir(parents=True, exist_ok=True)
        source = (self.backend_root / 'scripts/windows/start-backend.ps1').read_text(encoding='utf-8-sig')
        match = re.search(r"\[ValidateSet\(([^\n]+)\)\]\[string\]\$Backend", source)
        if not match:
            raise ValueError('Cannot detect supported backends from start-backend.ps1')
        self.backends = [name for name in re.findall(r"'([^']+)'", match[1]) if name in NATIVE]
        if not self.backends:
            raise ValueError('This console requires a NInfer or llama.cpp launcher')
        self.port, self.token = port, secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.picker_lock = threading.Lock()
        self.process = self.job = self.log = None
        self.active = None
        self.exit_code = None
        self.draft_path = self.state_dir / 'launch-draft.json'
        self.log_path = self.state_dir / 'service.log'

    def worker_defaults(self):
        # A source checkout and a runnable installation need not share a directory.
        # Local registrations are explicit; never guess between unrelated builds.
        registration = self.state_dir / 'worker-paths.json'
        paths = json.loads(registration.read_text(encoding='utf-8-sig')) if registration.exists() else {}
        if (not isinstance(paths, dict) or set(paths) - set(self.backends)
                or any(not isinstance(p, str) or not Path(p).is_absolute() or '\x00' in p
                       for p in paths.values())):
            raise ValueError('Invalid worker-paths.json: map backend names to absolute executable paths')
        names = {'ninfer': 'ninfer-kvmem-server.exe', 'llamacpp': 'llama-kvmem-server.exe'}
        result = {}
        for backend in self.backends:
            path = Path(paths.get(backend, self.backend_root / 'workers' / backend / names[backend]))
            result[backend] = {'path': str(path), 'available': path.is_file()}
        return result

    def draft(self):
        if self.draft_path.exists():
            return json.loads(self.draft_path.read_text(encoding='utf-8'))
        return {'schema_version': 1, 'parameters': dict(DEFAULTS)}

    def save(self, data):
        clean = normalize(data, self.backends)
        with self.lock:
            temp = self.draft_path.with_suffix('.tmp')
            temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.draft_path)
        return clean

    def plan(self, data):
        clean = normalize(data, self.backends)
        if os.name != 'nt':
            raise ValueError('Service launch currently requires Windows PowerShell')
        params = clean['parameters']
        if params.get('Port', 18200) == self.port:
            raise ValueError('Inference service and console must use different ports')
        # KVMem enablement is explicit for the llama worker. Its original launcher
        # remains unchanged; this controller is a KVMem-specific caller.
        params = copy.deepcopy(params)
        if not params.get('Worker'):
            worker = self.worker_defaults()[params['Backend']]
            if not worker['available']:
                raise ValueError('未找到可用的后端程序。请在“引擎专属参数”的“后端程序”中填写可执行文件路径，'
                                 '或在控制台 worker-paths.json 中登记安装位置。')
            params['Worker'] = worker['path']
        elif not Path(params['Worker']).is_file():
            raise ValueError('指定的后端程序不存在，请检查“引擎专属参数 → 后端程序”：' + params['Worker'])
        if params['Backend'] == 'llamacpp':
            params['WorkerArgs'] = ['--kvmem'] + params.get('WorkerArgs', [])
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8',
                                         dir=self.state_dir, delete=False) as stream:
            json.dump(params, stream, ensure_ascii=False); name = stream.name
        try:
            powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            run = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(ROOT / 'scripts/windows/describe-launch-json.ps1'),
                                  '-BackendRoot', str(self.backend_root), '-ConfigFile', name],
                                 capture_output=True, encoding='utf-8', errors='replace', timeout=45,
                                 cwd=self.backend_root,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            if run.returncode:
                raise ValueError(run.stderr.strip()[-6000:] or run.stdout.strip()[-6000:])
            # PowerShell warnings may precede the JSON result.
            start = run.stdout.find('{')
            result = json.loads(run.stdout[start:])
            argv = result.get('argv')
            if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\x00' in x for x in argv):
                raise ValueError('Launcher returned invalid argv')
            result['warnings'] = run.stdout[:start].strip()
            return result
        finally:
            Path(name).unlink(missing_ok=True)

    def browse_model(self, data, folder=False):
        if not isinstance(data, dict) or set(data) - {'backend', 'current_path'}:
            raise ValueError('Expected backend and optional current_path')
        backend, current = data.get('backend'), data.get('current_path', '')
        if backend not in self.backends or not isinstance(current, str) or '\x00' in current or len(current) > 8192:
            raise ValueError('Invalid model picker request')
        if os.name != 'nt':
            raise ValueError('The native model picker currently requires Windows')
        if not self.picker_lock.acquire(blocking=False):
            raise ValueError('A model selection window is already open')
        try:
            initial = Path(current).expanduser() if current else self.backend_root / 'models'
            if not initial.is_dir():
                initial = initial.parent
            powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            result = subprocess.run([powershell, '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(ROOT / 'scripts/windows/select-model.ps1'),
                                     '-Backend', backend, '-InitialDirectory', str(initial.resolve())] + (['-Folder'] if folder else []),
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=300,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise ValueError(result.stderr.strip()[-2000:] or 'Cannot open model selection window')
            selected = json.loads(result.stdout).get('path')
            if selected is None:
                return {'path': None}
            if not isinstance(selected, str):
                raise ValueError('Invalid model selection result')
            path = Path(selected).resolve(strict=True)
            if folder:
                if not path.is_dir():
                    raise ValueError('Select a model directory')
                return {'path': str(path)}
            suffix = '.ninfer' if backend == 'ninfer' else '.gguf'
            if not path.is_file() or path.suffix.lower() != suffix:
                raise ValueError(f'{backend} requires a {suffix} model file')
            return {'path': str(path), 'name': path.name}
        except subprocess.TimeoutExpired:
            raise ValueError('Model selection timed out; open the file picker again') from None
        finally:
            self.picker_lock.release()

    def scan_models(self, data):
        if not isinstance(data, dict) or set(data) - {'backend', 'current_path', 'directory'}:
            raise ValueError('Invalid model scan request')
        backend = data.get('backend')
        if backend not in self.backends:
            raise ValueError('Unknown backend')
        for key in ('current_path', 'directory'):
            value = data.get(key, '')
            if not isinstance(value, str) or '\x00' in value or len(value) > 8192:
                raise ValueError('Invalid search path')
        requested = []
        if data.get('directory'):
            folder = Path(data['directory']).expanduser().resolve(strict=True)
            if not folder.is_dir() or folder == Path(folder.anchor) or str(folder).startswith('\\\\'):
                raise ValueError('Select a local model folder, not a drive root or network share')
            requested.append(folder)
        else:
            if data.get('current_path'):
                current = Path(data['current_path']).expanduser()
                requested.append(current if current.is_dir() else current.parent)
            # Conventional model folders only, not whole workspaces or whole drives.
            for base in (self.backend_root, ROOT):
                requested.extend(parent / 'models' for parent in (base, *list(base.parents)[:2]))
        roots = []
        for folder in requested:
            folder = folder.resolve()
            if (folder == Path(folder.anchor) or str(folder).startswith('\\\\')
                    or not folder.is_dir() or folder in roots):
                continue
            roots.append(folder)
        suffix = '.ninfer' if backend == 'ninfer' else '.gguf'
        found, visited, candidates = {}, set(), 0
        skipped, limited = 0, False
        deadline = time.monotonic() + 3
        stack = [(folder, 0) for folder in reversed(roots)]
        while stack:
            if time.monotonic() >= deadline or candidates >= 10000 or len(found) >= 200:
                limited = True; break
            folder, depth = stack.pop()
            identity = os.path.normcase(str(folder))
            if identity in visited:
                continue
            visited.add(identity)
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        candidates += 1
                        if candidates > 10000 or time.monotonic() >= deadline or len(found) >= 200:
                            limited = True; break
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            # Junctions and links can lead outside the chosen folder or into loops.
                            if entry.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.startswith('.') or entry.name.lower() in ('node_modules', '__pycache__'):
                                    continue
                                if depth < 4:
                                    stack.append((Path(entry.path), depth + 1))
                                else:
                                    limited = True
                            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() == suffix:
                                # Auxiliary vision projectors and non-first GGUF shards aren't standalone models.
                                if re.search(r'(?:mmproj|projector)', entry.name, re.I):
                                    continue
                                shard = re.search(r'-(\d{5})-of-\d{5}\.gguf$', entry.name, re.I)
                                if shard and shard[1] != '00001':
                                    continue
                                # Windows DirEntry.stat may omit the file ID. Read it explicitly
                                # so legacy hard links appear only once in the model library.
                                file_stat = os.stat(entry.path, follow_symlinks=False)
                                key = ((file_stat.st_dev, file_stat.st_ino) if file_stat.st_ino
                                       else os.path.normcase(entry.path))
                                candidate = {'path': entry.path, 'name': entry.name,
                                             'bytes': file_stat.st_size, 'format': suffix[1:]}
                                def preference(item):
                                    path = Path(item['path'])
                                    canonical = (path.parent.name.casefold() == path.stem.casefold()
                                                 and path.parent.parent.name.casefold() == suffix[1:])
                                    return (not canonical, len(path.parts), item['path'].casefold())
                                if key not in found or preference(candidate) < preference(found[key]):
                                    found[key] = candidate
                        except OSError:
                            skipped += 1
            except OSError:
                skipped += 1
        return {'models': sorted(found.values(), key=lambda x: (x['name'].casefold(), x['path'])),
                'roots': [str(folder) for folder in roots], 'limited': limited, 'skipped': skipped}

    def status(self):
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            if self.process and not running:
                self.exit_code = self.process.returncode
                self._release()
            result = {'state': 'starting' if running else ('exited' if self.exit_code is not None else 'stopped'),
                      'pid': self.process.pid if running else None, 'exit_code': self.exit_code,
                      'active_config': self.active, 'url': None}
            if running:
                port = self.active['parameters'].get('Port', 18200)
                result['url'] = f'http://127.0.0.1:{port}'
                try:
                    with urllib.request.urlopen(result['url'] + '/health', timeout=0.5) as response:
                        if response.status == 200:
                            result['state'] = 'ready'
                except urllib.error.HTTPError as error:
                    error.close()
                except (OSError, urllib.error.URLError):
                    pass
            return result

    def _release(self):
        if self.job:
            self.job.close(); self.job = None
        if self.log:
            self.log.close(); self.log = None
        self.process = None

    def start(self, data):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError('A managed service is already running; stop it before starting another')
            self._release()
            clean = normalize(data, self.backends)
            result = self.plan(clean)
            port = clean['parameters'].get('Port', 18200)
            with socket.socket() as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((clean['parameters'].get('ListenHost', '127.0.0.1'), port))
                except OSError as error:
                    raise ValueError(f'端口 {port} 无法使用，可能已有服务占用。请在“服务与并发”中更换端口，'
                                     '或先停止原服务。此控制台不会停止外部服务。') from error
            env = dict(os.environ); env.update(result.get('environment', {}))
            self.log = self.log_path.open('wb')
            self.job = WindowsJob()
            try:
                # The gate waits for argv before creating any descendants. Assigning
                # it to the job first closes the spawn-before-job-assignment race.
                self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker-gate'],
                                                cwd=self.backend_root, env=env,
                                                stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                self.job.assign(self.process)
                self.process.stdin.write((json.dumps(result['argv']) + '\n').encode('utf-8'))
                self.process.stdin.close()
            except Exception:
                self._release(); raise
            self.active, self.exit_code = clean, None
            return self.status()

    def stop(self):
        with self.lock:
            process = self.process
            if self.job:
                self.job.close(); self.job = None
            if process:
                process.wait(timeout=10)
            self._release(); self.exit_code = None; self.active = None
            return self.status()

    def log_tail(self):
        if not self.log_path.exists():
            return ''
        with self.log_path.open('rb') as stream:
            stream.seek(0, 2); stream.seek(max(0, stream.tell() - 32768))
            return stream.read().decode('utf-8', errors='replace')


def gpu_inventory():
    try:
        run = subprocess.run(['nvidia-smi', '--query-gpu=uuid,name,memory.total', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return [dict(zip(('uuid', 'name', 'memory_mib'), (x.strip() for x in line.split(','))))
                for line in run.stdout.splitlines() if len(line.split(',')) == 3]
    except (OSError, subprocess.TimeoutExpired):
        return []


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def control(self):
        return self.server.control

    def reply(self, code, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self, mutate=False):
        host = self.headers.get('Host', '')
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        if host not in hosts or (origin is not None and origin != 'http://' + host):
            self.reply(403, {'error': 'Untrusted host or origin'}); return False
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            self.reply(403, {'error': 'Cross-site request rejected'}); return False
        if mutate and not secrets.compare_digest(self.headers.get('X-KVMem-Token', ''), self.control.token):
            self.reply(403, {'error': 'Invalid console token'}); return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/launcher/info':
                self.reply(200, {'token': self.control.token, 'backends': self.control.backends,
                                'backend_root': str(self.control.backend_root), 'gpus': self.server.gpus,
                                'draft': self.control.draft(), 'status': self.control.status(),
                                'workers': self.control.worker_defaults(),
                                'native_options': {k: sorted(v) for k, v in NATIVE.items()}})
            elif path == '/api/launcher/status':
                self.reply(200, self.control.status())
            elif path == '/api/launcher/log':
                self.reply(200, {'text': self.control.log_tail()})
            elif path in ('/api/monitor/snapshot', '/props', '/slots', '/stats'):
                state = self.control.status()
                if not state['url']:
                    self.reply(503, {'error': 'No managed service is running'}); return
                # Fixed read-only routes to the owned service, never an arbitrary URL.
                request = urllib.request.Request(state['url'] + path)
                if self.headers.get('Authorization'):
                    request.add_header('Authorization', self.headers['Authorization'])
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        body = response.read(4 * 1024 * 1024 + 1)
                        if len(body) > 4 * 1024 * 1024:
                            raise ValueError('Monitor response too large')
                        self.reply(response.status, body)
                except urllib.error.HTTPError as error:
                    self.reply(error.code, {'error': 'Inference endpoint unavailable'})
                    error.close()
            elif path in ('/', '/monitor'):
                self.send_response(302)
                self.send_header('Location', '/monitor/')
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif path == '/monitor/':
                self.asset('index.html')
            elif path.startswith('/monitor/') and '/' not in path[len('/monitor/'):]:
                self.asset(path[len('/monitor/'):])
            else:
                self.reply(404, {'error': 'Not found'})
        except (ValueError, OSError, urllib.error.URLError) as error:
            self.reply(503, {'error': str(error)})

    def asset(self, name):
        allowed = {'index.html', 'monitor.css', 'monitor.js', 'i18n.js', 'launcher.css', 'launcher.js',
                   'Strata-LICENSE.txt', 'Outfit-OFL.txt', 'outfit-latin-wght.woff2'}
        if name not in allowed:
            self.reply(404, {'error': 'Not found'}); return
        file = ROOT / 'ui/monitor' / name
        self.reply(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def do_POST(self):
        if not self.trusted(mutate=True):
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected application/json')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            actions = {'/api/launcher/save': self.control.save, '/api/launcher/preview': self.control.plan,
                       '/api/launcher/browse-model': self.control.browse_model,
                       '/api/launcher/browse-model-folder': lambda data: self.control.browse_model(data, folder=True),
                       '/api/launcher/scan-models': self.control.scan_models,
                       '/api/launcher/start': self.control.start, '/api/launcher/stop': lambda _: self.control.stop()}
            action = actions.get(self.path)
            if action is None:
                self.reply(404, {'error': 'Not found'}); return
            self.reply(200, action(data))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.reply(400, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18199)
    parser.add_argument('--backend-root', type=Path, default=ROOT)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'build/local-console')
    args = parser.parse_args()
    control = Controller(args.backend_root, args.state_dir, args.port)
    server = Server(('127.0.0.1', args.port), Handler)
    server.control, server.gpus = control, gpu_inventory()
    print(f'KVMem console: http://127.0.0.1:{server.server_port}/monitor/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        control.stop(); server.server_close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker-gate']:
        argv = json.loads(sys.stdin.buffer.readline(65536))
        sys.exit(subprocess.call(argv, stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW))
    else:
        main()
