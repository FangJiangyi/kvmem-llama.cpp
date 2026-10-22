"""Resource queries and monitoring for the standalone ninfer configuration runner.

No model loading or parameter selection belongs here. A Monitor may terminate only
its supplied child process when an observation fails or a memory floor is crossed.
"""
import csv
import ctypes as ct
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import threading

MIB = 1 << 20


class ResourceError(RuntimeError):
    """Resource monitoring failed or an observed memory floor was crossed."""


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_port(port):
    # Match the HTTP listener's reuse policy: completed own workers leave TIME_WAIT.
    # SO_REUSEADDR still refuses a port held by an active listener on Linux.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', port))


def host_available():
    info = {k: int(v.split()[0]) * 1024 for k, v in
            (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
    available = info['MemAvailable']
    # Account for every visible cgroup-v2 ancestor, not just the leaf limit.
    groups = Path('/proc/self/cgroup').read_text().splitlines()
    v2 = next((line.split(':', 2)[2] for line in groups if line.startswith('0::')), None)
    if v2 is not None:
        group = Path('/sys/fs/cgroup') / v2.lstrip('/')
        while group.is_relative_to('/sys/fs/cgroup'):
            maximum, current = group / 'memory.max', group / 'memory.current'
            if maximum.exists() and current.exists():
                limit = maximum.read_text().strip()
                if limit != 'max':
                    available = min(available, max(0, int(limit) - int(current.read_text())))
            if group == Path('/sys/fs/cgroup'):
                break
            group = group.parent
    elif any(':memory:' in line or ':memory,' in line for line in groups):
        raise RuntimeError('cgroup-v1 monitoring is not supported; cannot safely validate memory')
    return available


class Nvml:
    class Memory(ct.Structure):
        _fields_ = [('total', ct.c_ulonglong), ('free', ct.c_ulonglong), ('used', ct.c_ulonglong)]

    def __init__(self, uuid):
        self.lib = ct.CDLL('libnvidia-ml.so.1')
        self.lib.nvmlDeviceGetHandleByUUID.argtypes = [ct.c_char_p, ct.POINTER(ct.c_void_p)]
        self.lib.nvmlDeviceGetMemoryInfo.argtypes = [ct.c_void_p, ct.POINTER(self.Memory)]
        self.check(self.lib.nvmlInit_v2())
        self.handle = ct.c_void_p()
        self.check(self.lib.nvmlDeviceGetHandleByUUID(uuid.encode(), ct.byref(self.handle)))

    @staticmethod
    def check(code):
        if code:
            raise RuntimeError(f'NVML error {code}')

    def free(self):
        memory = self.Memory()
        self.check(self.lib.nvmlDeviceGetMemoryInfo(self.handle, ct.byref(memory)))
        return memory.free


class Monitor:
    def __init__(self, process, nvml, gpu_floor, ram_floor):
        self.process, self.nvml = process, nvml
        self.gpu_floor, self.ram_floor = gpu_floor, ram_floor
        self.stop = threading.Event()
        self.error = None
        self.summary = {'samples': 0, 'minimum_gpu_free_bytes': 1 << 63,
                        'minimum_host_available_bytes': 1 << 63, 'peak_rss_bytes': 0}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set() and self.process.poll() is None:
            try:
                gpu, ram = self.nvml.free(), host_available()
                status = Path(f'/proc/{self.process.pid}/status').read_text()
                rss = max((int(line.split()[1]) * 1024 for line in status.splitlines()
                           if line.startswith(('VmRSS:', 'VmHWM:'))), default=0)
                s = self.summary
                s['samples'] += 1
                s['minimum_gpu_free_bytes'] = min(s['minimum_gpu_free_bytes'], gpu)
                s['minimum_host_available_bytes'] = min(s['minimum_host_available_bytes'], ram)
                s['peak_rss_bytes'] = max(s['peak_rss_bytes'], rss)
                if gpu < self.gpu_floor or ram < self.ram_floor:
                    raise ResourceError(f'memory floor crossed: GPU free {gpu / MIB:.1f} MiB; RAM available {ram / MIB:.1f} MiB')
            except FileNotFoundError:
                if self.process.poll() is None:
                    self.error = 'worker status disappeared'
                break
            except Exception as error:
                self.error = str(error)
                if self.process.poll() is None:
                    self.process.terminate()
                break
            self.stop.wait(0.05)

    def check(self):
        if self.error:
            raise ResourceError(self.error)


def hardware():
    result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,name,driver_version,memory.total,pci.bus_id',
                             '--format=csv,noheader,nounits'], capture_output=True, text=True, check=True)
    rows = list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))
    # Initial delivery is explicitly the local one-GPU text route.
    if len(rows) != 1:
        raise RuntimeError('this configuration runner currently requires exactly one physical NVIDIA GPU')
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith('model name')), platform.machine())
    total = next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                 if line.startswith('MemTotal:'))
    return {'gpu': rows[0], 'cpu': cpu, 'host_total_kib': int(total),
            'affinity': sorted(os.sched_getaffinity(0)), 'system': platform.platform(),
            'cuda_visible_devices': os.environ.get('CUDA_VISIBLE_DEVICES', ''),
            'ld_library_path': os.environ.get('LD_LIBRARY_PATH', ''),
            'execution_environment': {k: v for k, v in os.environ.items()
                                      if k.startswith(('NINFER_', 'CUDA_', 'OMP_', 'MKL_'))}}
