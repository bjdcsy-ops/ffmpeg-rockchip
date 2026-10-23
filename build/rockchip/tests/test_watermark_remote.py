#!/usr/bin/env python3
"""Offline worker checks. All media processes, sockets, and signals are mocked."""

import configparser
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "watermark_remote", Path(__file__).resolve().parents[1] / "watermark_remote.py")
REMOTE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REMOTE)


def config(run_dir="/tmp/watermark-test"):
    return {"run_dir": run_dir, "source_file": "/tmp/source.mp4", "alpha": 51,
            "duration": 30, "mode": "single"}


def process(pid, status=None):
    child = mock.Mock(pid=pid, returncode=status)
    child.poll.return_value = status
    return child


class CommandTests(unittest.TestCase):
    def test_config_rejects_invalid_values(self):
        for key, value in (("alpha", -1), ("alpha", 256), ("alpha", True),
                           ("duration", 9), ("duration", 3601), ("duration", "30"),
                           ("mode", "unknown"), ("run_dir", "relative"),
                           ("source_file", "relative"), ("media_server", "relative")):
            with self.subTest(key=key, value=value):
                candidate = config()
                candidate[key] = value
                with self.assertRaises(ValueError):
                    REMOTE.validate_config(candidate)
        self.assertEqual(REMOTE.validate_config(config())["media_server"],
                         "/opt/zlmediakit/MediaServer")

    def test_server_exposes_only_loopback_preview_rtsp(self):
        ini = configparser.ConfigParser()
        ini.read_string(REMOTE.make_server_config(32123))
        self.assertEqual(ini["general"]["listen_ip"], "127.0.0.1")
        self.assertEqual(ini["general"]["enableVhost"], "0")
        self.assertEqual(ini["hook"]["enable"], "0")
        self.assertEqual(ini["rtsp"]["port"], "32123")
        self.assertEqual(ini["rtsp"]["sslport"], "0")
        for section in ("http", "rtmp", "rtc", "srt", "rtp_proxy", "shell", "onvif"):
            self.assertEqual(ini[section]["port"], "0")
        for key in ("tcpPort", "signalingPort", "signalingSslPort", "icePort", "iceTcpPort"):
            self.assertEqual(ini["rtc"][key], "0")
        self.assertEqual(ini["onvif"]["enable"], "0")
        for protocol in ("rtmp", "hls", "hls_fmp4", "mp4", "ts", "fmp4"):
            self.assertEqual(ini["protocol"]["enable_" + protocol], "0")
        self.assertEqual(ini["protocol"]["enable_rtsp"], "1")
        for port in (0, 554, 18554, 65536, True):
            with self.assertRaises(ValueError):
                REMOTE.make_server_config(port)

    def test_server_generates_a_fresh_temporary_api_secret(self):
        with mock.patch.object(REMOTE.secrets, "token_hex", side_effect=["a" * 32, "b" * 32]) as random:
            first, second = configparser.ConfigParser(), configparser.ConfigParser()
            first.read_string(REMOTE.make_server_config(32123))
            second.read_string(REMOTE.make_server_config(32123))
        self.assertEqual(random.call_args_list, [mock.call(16), mock.call(16)])
        self.assertEqual(first["api"]["secret"], "a" * 32)
        self.assertEqual(second["api"]["secret"], "b" * 32)

    def test_ephemeral_port_selection_binds_loopback(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("127.0.0.1", 32123)
        with mock.patch.object(REMOTE.socket, "socket", return_value=sock):
            self.assertEqual(REMOTE.choose_port(), 32123)
        sock.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_same_source_and_alpha_endpoints_use_literal_argv(self):
        for alpha in (0, 255):
            candidate = config("/tmp/test ' ; $(ignored)")
            candidate.update(alpha=alpha, source_file="/tmp/video ; $(ignored).mp4",
                             mode="diagonal")
            commands = REMOTE.make_commands(candidate, 32123)
            self.assertEqual(commands["source"][commands["source"].index("-i") + 1],
                             candidate["source_file"])
            self.assertEqual(commands["source"][-1], "rtsp://127.0.0.1:32123/live/source")
            self.assertEqual(commands["source"][commands["source"].index("-c:v") + 1], "copy")
            self.assertEqual(commands["source"][commands["source"].index("-stream_loop") + 1], "-1")
            argv = commands["watermark"]
            graph = argv[argv.index("-filter_complex") + 1]
            self.assertIn("alpha={}:alpha_format=straight".format(alpha), graph)
            self.assertIn("repeatlast=1:eof_action=repeat:shortest=0", graph)
            self.assertEqual(argv.count("rtsp://127.0.0.1:32123/live/source"), 1)
            self.assertEqual(argv[-1], "rtsp://127.0.0.1:32123/live/watermark")
            self.assertNotIn("-loop", argv)
            self.assertEqual(argv[argv.index("-b:v") + 1], "4M")
            self.assertEqual(argv[argv.index("-g") + 1], "25")
            self.assertEqual(commands["server"][2], candidate["run_dir"] + "/media-server.ini")


class LifecycleTests(unittest.TestCase):
    def test_startup_deadline_and_duration_are_bounded(self):
        worker = REMOTE.Worker(config(), io.StringIO(), send=mock.Mock())
        worker.started = 100
        with mock.patch.object(REMOTE.time, "monotonic", return_value=145):
            with self.assertRaisesRegex(RuntimeError, "startup exceeded"):
                worker.tick(0)
        worker.ready_at = 120
        with mock.patch.object(REMOTE.time, "monotonic", return_value=150):
            with self.assertRaisesRegex(REMOTE.StopRequested, "duration"):
                worker.tick(0)

    def test_stop_signals_only_owned_running_groups_and_kills_on_timeout(self):
        exited, running, stubborn = process(101, 0), process(102), process(103)
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        with mock.patch.object(REMOTE.os, "killpg") as killpg:
            REMOTE.stop_processes([exited, running, stubborn])
        self.assertEqual(killpg.call_args_list,
                         [mock.call(102, signal.SIGTERM), mock.call(103, signal.SIGTERM),
                          mock.call(103, signal.SIGKILL)])
        exited.wait.assert_not_called()
        running.wait.assert_called_once()
        self.assertEqual(stubborn.wait.call_count, 2)

    def test_failed_kill_does_not_skip_other_owned_children(self):
        first, second = process(101), process(102)
        first.wait.side_effect = subprocess.TimeoutExpired("fixture", 5)
        with mock.patch.object(REMOTE.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "101"):
                REMOTE.stop_processes([first, second])
        second.wait.assert_called_once()
        killpg.assert_any_call(102, signal.SIGTERM)

    def test_control_pipe_eof_requests_cleanup(self):
        stream = mock.Mock()
        stream.fileno.return_value = 10
        with mock.patch.object(REMOTE.select, "select", return_value=([stream], [], [])), \
                mock.patch.object(REMOTE.os, "read", return_value=b""):
            with self.assertRaisesRegex(REMOTE.StopRequested, "stdin_eof"):
                REMOTE.read_control(stream, 0.1)

    def test_spawn_uses_owned_session_without_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = REMOTE.Worker(config(temp), io.StringIO(), send=mock.Mock())
            worker.commands_file = io.StringIO()
            argv = ["/literal path/ffmpeg", "-i", "file;$(literal)"]
            child = process(101)
            try:
                with mock.patch.object(REMOTE.subprocess, "Popen", return_value=child) as popen:
                    self.assertIs(worker.spawn("source", argv, service=True), child)
                self.assertEqual(popen.call_args.args, (argv,))
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertNotIn("shell", popen.call_args.kwargs)
                self.assertEqual(worker.processes, [child])
                self.assertEqual(worker.services, {"source": child})
                self.assertEqual(json.loads(worker.commands_file.getvalue())[0]["argv"], argv)
            finally:
                for handle in worker.handles:
                    handle.close()

    def test_early_service_exit_is_failure(self):
        worker = REMOTE.Worker(config(), io.StringIO(), send=mock.Mock())
        worker.services["source"] = process(101, 7)
        with self.assertRaisesRegex(RuntimeError, "source exited early with status 7"):
            worker.tick(0)

    def run_mock_worker(self, start_error=None, tick_error=None):
        with tempfile.TemporaryDirectory() as temp:
            events = []
            worker = REMOTE.Worker(config(temp), io.StringIO(), send=events.append)
            worker.owns_run = True
            child = process(101)
            worker.processes.append(child)
            worker.services["source"] = child
            worker.frames, worker.port, worker.source_sha = 1, 32123, "fixture-sha"
            with mock.patch.object(worker, "start", side_effect=start_error), \
                    mock.patch.object(worker, "tick", side_effect=tick_error), \
                    mock.patch.object(REMOTE, "stop_processes") as stop:
                code = worker.run()
            stop.assert_called_once_with([child])
            summary = json.loads((Path(temp) / "summary.json").read_text())
            self.assertEqual(summary, events[-1])
            return code, events

    def test_startup_eof_cleans_up_without_ready(self):
        code, events = self.run_mock_worker(start_error=REMOTE.StopRequested("stdin_eof"))
        self.assertEqual(code, 0)
        self.assertEqual([event["event"] for event in events], ["stopped"])
        self.assertEqual(events[-1]["reason"], "stdin_eof")

    def test_ready_eof_cleans_up_successfully(self):
        code, events = self.run_mock_worker(tick_error=REMOTE.StopRequested("stdin_eof"))
        self.assertEqual(code, 0)
        self.assertEqual([event["event"] for event in events], ["ready", "stopped"])
        self.assertEqual(events[0]["port"], 32123)
        self.assertEqual(events[0]["stream"], "watermark")

    def test_startup_and_monitor_errors_cleanup_and_exit_failure(self):
        for phase in ("start", "monitor"):
            with self.subTest(phase=phase):
                error = RuntimeError("fixture failure")
                code, events = self.run_mock_worker(
                    start_error=error if phase == "start" else None,
                    tick_error=error if phase == "monitor" else None)
                self.assertEqual(code, 1)
                self.assertEqual(events[-2], {"event": "error", "message": "fixture failure"})
                self.assertEqual(events[-1]["reason"], "error")

    def test_stalled_output_is_failure_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = REMOTE.Worker(config(temp), io.StringIO(), send=mock.Mock())
            worker.frames = 1
            with mock.patch.object(worker, "start"), mock.patch.object(worker, "tick"), \
                    mock.patch.object(REMOTE, "read_frames", return_value=1), \
                    mock.patch.object(REMOTE.time, "monotonic", side_effect=[1, 17, 18]), \
                    mock.patch.object(REMOTE, "stop_processes") as stop:
                self.assertEqual(worker.run(), 1)
            stop.assert_called_once_with([])
            worker.send.assert_any_call({"event": "error", "message": "watermark output stalled for 15 seconds"})

    def test_progress_uses_latest_frame_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "progress"
            path.write_text("frame=1\nprogress=continue\nframe=42\nprogress=continue\n")
            self.assertEqual(REMOTE.read_frames(path), 42)
            self.assertEqual(REMOTE.read_frames(Path(temp) / "missing"), 0)


class InputTests(unittest.TestCase):
    def prepare_fixture(self, directory):
        root = Path(directory)
        (root / "runtime" / "lib").mkdir(parents=True)
        for name in ("runtime/ffmpeg", "runtime/ffprobe", "media-server", "source.mp4", "watermark.png"):
            path = root / name
            path.write_bytes(b"fixture")
            path.chmod(0o700)
        (root / "runtime" / "BUILDINFO.txt").write_text("source_sha=current-source-sha\n")
        (root / "runtime" / "lib" / "libz.so.1").write_bytes(b"bundled-zlib")
        candidate = config(directory)
        candidate.update(source_file=str(root / "source.mp4"), media_server=str(root / "media-server"))
        return REMOTE.Worker(candidate, io.StringIO(), send=mock.Mock())

    def test_png_must_decode_to_exactly_one_full_resolution_rgba_frame(self):
        expected = {"codec_name": "png", "width": 1920, "height": 1080,
                    "pix_fmt": "rgba", "nb_read_frames": "1"}
        for key, value in (("nb_read_frames", "2"), ("nb_read_frames", "N/A"),
                           ("pix_fmt", "rgb24"), ("width", 1280)):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temp:
                worker = self.prepare_fixture(temp)
                stream = dict(expected)
                stream[key] = value
                try:
                    with mock.patch.object(worker, "tick"), mock.patch.object(
                            worker, "probe", return_value=(0, json.dumps({"streams": [stream]}), "")):
                        with self.assertRaisesRegex(ValueError, "one 1920x1080 RGBA frame"):
                            worker.prepare()
                finally:
                    for handle in worker.handles:
                        handle.close()

    def test_evidence_uses_uploaded_runtime_and_source_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.prepare_fixture(temp)
            png = {"codec_name": "png", "width": 1920, "height": 1080,
                   "pix_fmt": "rgba", "nb_read_frames": "1"}
            source = {"codec_name": "h264", "width": 1920, "height": 1080,
                      "avg_frame_rate": "25/1"}
            replies = [(0, json.dumps({"streams": [png]}), ""),
                       (0, json.dumps({"streams": [source]}), ""),
                       (0, "ffmpeg version fixture\nconfiguration: fixture\n", "")]
            try:
                with mock.patch.object(worker, "tick"), mock.patch.object(worker, "probe", side_effect=replies):
                    worker.prepare()
                evidence = json.loads((Path(temp) / "evidence.json").read_text())
                self.assertEqual(evidence["source_sha"], "current-source-sha")
                self.assertEqual(evidence["png"], png)
                self.assertEqual(evidence["source"], source)
                self.assertEqual(evidence["sha256"]["runtime/lib/libz.so.1"],
                                 REMOTE.file_sha256(Path(temp) / "runtime/lib/libz.so.1"))
            finally:
                for handle in worker.handles:
                    handle.close()

    def test_existing_run_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.prepare_fixture(temp)
            commands = Path(temp) / "commands.json"
            commands.write_text("owned by another worker\n")
            with mock.patch.object(worker, "tick"), mock.patch.object(REMOTE, "stop_processes"):
                self.assertEqual(worker.run(), 1)
            self.assertEqual(commands.read_text(), "owned by another worker\n")
            self.assertFalse((Path(temp) / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
