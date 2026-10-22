"""Memory and process ownership checks without a GPU or model."""
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ninfer_kvmem_resources as resources


class ResourceTests(unittest.TestCase):
    def monitor(self, free_mib=2048):
        class Process:
            pid = os.getpid()
            terminated = False

            def poll(self):
                return 1 if self.terminated else None

            def terminate(self):
                self.terminated = True

        process = Process()
        nvml = SimpleNamespace(free=lambda: free_mib * resources.MIB)
        return resources.Monitor(process, nvml, 1024 * resources.MIB, 2048 * resources.MIB)

    def test_gpu_or_ram_floor_stops_supplied_process_and_preserves_observation(self):
        for gpu, ram in ((1023, 4096), (2048, 2047)):
            with self.subTest(gpu=gpu, ram=ram):
                monitor = self.monitor(gpu)
                with patch.object(resources, 'host_available', return_value=ram * resources.MIB):
                    monitor.run()
                self.assertTrue(monitor.process.terminated)
                self.assertEqual(monitor.summary['samples'], 1)
                self.assertEqual(monitor.summary['minimum_gpu_free_bytes'], gpu * resources.MIB)
                self.assertEqual(monitor.summary['minimum_host_available_bytes'], ram * resources.MIB)
                with self.assertRaisesRegex(resources.ResourceError, 'memory floor crossed'):
                    monitor.check()

    def test_monitoring_failure_is_not_silently_accepted(self):
        monitor = self.monitor()
        monitor.nvml.free = lambda: (_ for _ in ()).throw(RuntimeError('NVML unavailable'))
        monitor.run()
        self.assertTrue(monitor.process.terminated)
        with self.assertRaisesRegex(resources.ResourceError, 'NVML unavailable'):
            monitor.check()

    def test_exact_floor_is_allowed_and_normal_stop_keeps_process_running(self):
        monitor = self.monitor(1024)
        with patch.object(resources, 'host_available', return_value=2048 * resources.MIB), \
                patch.object(monitor.stop, 'wait', side_effect=lambda _: monitor.stop.set()):
            monitor.run()
        monitor.check()
        self.assertFalse(monitor.process.terminated)
        self.assertEqual(monitor.summary['samples'], 1)
        self.assertGreater(monitor.summary['peak_rss_bytes'], 0)

    def test_available_ram_respects_parent_cgroup_limit(self):
        files = {
            '/proc/meminfo': 'MemAvailable: 8192 kB\n',
            '/proc/self/cgroup': '0::/parent/child\n',
            '/sys/fs/cgroup/parent/child/memory.max': 'max',
            '/sys/fs/cgroup/parent/child/memory.current': '0',
            '/sys/fs/cgroup/parent/memory.max': '4096',
            '/sys/fs/cgroup/parent/memory.current': '3072',
        }
        with patch.object(Path, 'read_text', lambda path: files[str(path)]), \
                patch.object(Path, 'exists', lambda path: str(path) in files):
            self.assertEqual(resources.host_available(), 1024)
            files['/sys/fs/cgroup/parent/memory.current'] = '8192'
            self.assertEqual(resources.host_available(), 0)

    def test_unsupported_memory_accounting_fails_closed(self):
        files = {'/proc/meminfo': 'MemAvailable: 8192 kB\n',
                 '/proc/self/cgroup': '3:memory:/child\n'}
        with patch.object(Path, 'read_text', lambda path: files[str(path)]):
            with self.assertRaisesRegex(RuntimeError, 'cgroup-v1'):
                resources.host_available()

    def test_port_reuse_keeps_active_listener_protected(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            with self.assertRaises(OSError):
                resources.check_port(port)
            with socket.create_connection(('127.0.0.1', port)) as client:
                connection, _ = server.accept()
                with connection:
                    connection.sendall(b'x')
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'x')
        resources.check_port(port)


if __name__ == '__main__':
    unittest.main()
