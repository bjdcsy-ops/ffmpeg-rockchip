#!/usr/bin/env python3
"""Offline transport and ownership checks; no Pillow or device required."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location('watermark_demo', ROOT / 'watermark-demo.py')
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


class ControlTests(unittest.TestCase):
    def test_reject_invalid_transport_and_alpha_before_ssh(self):
        for argv in (['--host', '-bad'], ['--host', 'host with space'], ['--host', 'host;bad'],
                     ['--alpha', '-1'], ['--alpha', '256'], ['--duration', '0'],
                     ['--local-port', '65536'], ['--remote-root', 'relative'],
                     ['--source-file', '/tmp/source\nfile.mp4']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    DEMO.arguments(argv)
                self.assertEqual(error.exception.code, 2)
        for alpha in (0, 255):
            self.assertEqual(DEMO.arguments(['--alpha', str(alpha)]).alpha, alpha)

    def test_remote_shell_receives_literal_arguments(self):
        malicious = "/tmp/a b'$(touch SHOULD_NOT_EXIST);测试"
        argv = ['python3', '-c', DEMO.BOOTSTRAP, malicious]
        command = DEMO.ssh_command('example', argv)
        self.assertEqual(shlex.split(command[-1]), argv)
        self.assertEqual(command[-3:-1], ['--', 'example'])
        text = "水印 'quoted' $() ; `literal`"
        self.assertEqual(DEMO.arguments(['--text', text]).text, text)

    def test_busy_local_port_fails_without_starting_remote(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            with self.assertRaises(OSError):
                DEMO.available_port(occupied.getsockname()[1])

    def test_archive_preserves_literal_config_and_runtime_links(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            package = directory / 'package'
            (package / 'lib').mkdir(parents=True)
            for name in ('ffmpeg', 'ffprobe', 'BUILDINFO.txt', 'lib/libz.so.1.2'):
                (package / name).write_bytes(b'fixture')
            (package / 'lib/libz.so.1').symlink_to('libz.so.1.2')
            (directory / 'watermark.png').write_bytes(b'fixture')
            config = {'run_dir': "/tmp/test ' ; $()", 'mode': 'single'}
            archive = DEMO.package_archive(package, directory, config)
            with tarfile.open(archive) as bundle:
                self.assertEqual(json.load(bundle.extractfile('config.json')), config)
                self.assertTrue(bundle.getmember('runtime/lib/libz.so.1').issym())
                self.assertIn('worker.py', bundle.getnames())
            (package / 'lib/outside').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'escapes package'):
                DEMO.package_archive(package, directory, config)

    def test_stdin_close_stops_only_owned_session(self):
        code = 'import sys; sys.stdin.read()'
        worker = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE)
        tunnel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            DEMO.close_session(worker, tunnel)
            self.assertEqual(worker.returncode, 0)
            self.assertIsNotNone(tunnel.returncode)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_event_timeout_is_not_an_early_stream_stop(self):
        worker = subprocess.Popen([sys.executable, '-u', '-c',
                                   'import sys,time; time.sleep(.15); '
                                   'print("{\\"event\\":\\"ready\\"}"); sys.stdin.read()'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIsNone(DEMO.next_event(worker, time.monotonic() + .02))
            self.assertEqual(DEMO.next_event(worker, time.monotonic() + 3), {'event': 'ready'})
        finally:
            DEMO.close_session(worker, None)
            worker.stdout.close()


if __name__ == '__main__':
    unittest.main()
