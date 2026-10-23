#!/usr/bin/env python3
"""Run one bounded, isolated RTSP watermark preview on a Rockchip board."""

import hashlib
import json
import os
from pathlib import Path
import select
import secrets
import signal
import socket
import subprocess
import sys
import time
from fractions import Fraction


class StopRequested(Exception):
    pass


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def validate_config(config):
    config = dict(config)
    for key in ("run_dir", "source_file"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError("{} must be an absolute path".format(key))
    config.setdefault("media_server", "/opt/zlmediakit/MediaServer")
    if not isinstance(config["media_server"], str) or not Path(config["media_server"]).is_absolute():
        raise ValueError("media_server must be an absolute path")
    for key, low, high in (("alpha", 0, 255), ("duration", 10, 3600)):
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError("{} must be an integer in {}..{}".format(key, low, high))
    if config.get("mode") not in ("single", "matrix", "diagonal"):
        raise ValueError("mode must be single, matrix, or diagonal")
    return config


def check_port(port):
    if type(port) is not int or not 1 <= port <= 65535 or port in (554, 18554):
        raise ValueError("invalid or reserved preview port")


def choose_port():
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in (554, 18554):
            return port


def make_server_config(port):
    check_port(port)
    return """[general]
listen_ip=127.0.0.1
enableVhost=0
streamNoneReaderDelayMS=20000
[rtsp]
port={port}
sslport=0
[http]
port=0
sslport=0
[rtmp]
port=0
sslport=0
[rtc]
port=0
tcpPort=0
signalingPort=0
signalingSslPort=0
icePort=0
iceTcpPort=0
[onvif]
port=0
enable=0
[srt]
port=0
[rtp_proxy]
port=0
[shell]
port=0
[hook]
enable=0
[api]
secret={secret}
[protocol]
enable_rtsp=1
enable_rtmp=0
enable_hls=0
enable_hls_fmp4=0
enable_mp4=0
enable_ts=0
enable_fmp4=0
enable_audio=0
auto_close=0
""".format(port=port, secret=secrets.token_hex(16))


def make_commands(config, port):
    config = validate_config(config)
    check_port(port)
    run_dir = Path(config["run_dir"])
    ffmpeg = str(run_dir / "runtime" / "ffmpeg")
    source_url = "rtsp://127.0.0.1:{}/live/source".format(port)
    output_url = "rtsp://127.0.0.1:{}/live/watermark".format(port)
    graph = (
        "[0:v]setpts=PTS-STARTPTS,scale_rkrga=w=1920:h=1080:format=nv12:async_depth=0[main];"
        "[1:v]setpts=PTS-STARTPTS,format=rgba,hwupload[mark];"
        "[main][mark]overlay_rkrga=x=0:y=0:alpha={}:alpha_format=straight:format=nv12:"
        "repeatlast=1:eof_action=repeat:shortest=0:async_depth=0[v]"
    ).format(config["alpha"])
    return {
        "server": [config["media_server"], "-c", str(run_dir / "media-server.ini"),
                   "--log-dir", str(run_dir / "server-logs"), "-t", "2", "--affinity", "0"],
        "source": [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-xerror",
                   "-stream_loop", "-1", "-re", "-i", config["source_file"],
                   "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "rtsp",
                   "-rtsp_transport", "tcp", source_url],
        "watermark": [ffmpeg, "-hide_banner", "-loglevel", "verbose", "-nostdin", "-xerror",
                      "-progress", str(run_dir / "watermark-progress.txt"),
                      "-init_hw_device", "rkmpp=wm", "-filter_hw_device", "wm",
                      "-rtsp_transport", "tcp", "-hwaccel", "rkmpp", "-hwaccel_device", "wm",
                      "-hwaccel_output_format", "drm_prime", "-c:v", "h264_rkmpp", "-afbc", "0",
                      "-i", source_url, "-f", "image2", "-pattern_type", "none", "-c:v", "png",
                      "-i", str(run_dir / "watermark.png"), "-filter_complex", graph,
                      "-map", "[v]", "-an", "-c:v", "h264_rkmpp", "-b:v", "4M", "-g", "25",
                      "-fps_mode", "passthrough", "-f", "rtsp", "-rtsp_transport", "tcp", output_url],
    }


def stop_processes(processes):
    pending = [process for process in processes if process.poll() is None]
    for process in pending:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    errors = []
    for process in pending:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                errors.append(str(process.pid))
    if errors:
        raise RuntimeError("processes did not exit after SIGKILL: " + ", ".join(errors))


def read_control(stream, timeout):
    readable, _, _ = select.select([stream], [], [], timeout)
    if readable and not os.read(stream.fileno(), 4096):
        raise StopRequested("stdin_eof")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_frames(path):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if line.startswith("frame="):
                return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 0


class Worker:
    def __init__(self, config, control, send=emit):
        self.config = validate_config(config)
        self.run_dir = Path(self.config["run_dir"])
        self.control = control
        self.send = send
        self.processes = []
        self.services = {}
        self.handles = []
        self.commands = []
        self.commands_file = None
        self.owns_run = False
        self.frames = 0
        self.port = None
        self.source_sha = None
        self.started = time.monotonic()
        self.ready_at = None
        self.last_advance = self.started

    def write_json(self, name, data):
        with (self.run_dir / name).open("x", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    def record_command(self, name, argv):
        self.commands.append({"name": name, "argv": argv})
        self.commands_file.seek(0)
        self.commands_file.truncate()
        json.dump(self.commands, self.commands_file, indent=2)
        self.commands_file.flush()

    def spawn(self, name, argv, service=False):
        self.record_command(name, argv)
        stdout = (self.run_dir / (name + ".stdout.log")).open("xb")
        self.handles.append(stdout)
        stderr = (self.run_dir / (name + ".stderr.log")).open("xb")
        self.handles.append(stderr)
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   cwd=str(self.run_dir), start_new_session=True)
        self.processes.append(process)
        if service:
            self.services[name] = process
        return process

    def tick(self, timeout=0.1):
        for name, process in self.services.items():
            code = process.poll()
            if code is not None:
                raise RuntimeError("{} exited early with status {}".format(name, code))
        now = time.monotonic()
        if self.ready_at is None and now - self.started >= 45:
            raise RuntimeError("startup exceeded 45 seconds")
        if self.ready_at is not None and now - self.ready_at >= self.config["duration"]:
            raise StopRequested("duration")
        read_control(self.control, timeout)

    def probe(self, name, argv):
        process = self.spawn(name, argv)
        while process.poll() is None:
            self.tick()
        process.wait()
        self.processes.remove(process)
        output = (self.run_dir / (name + ".stdout.log")).read_text(encoding="utf-8")
        errors = (self.run_dir / (name + ".stderr.log")).read_text(encoding="utf-8")
        return process.returncode, output, errors

    def prepare(self):
        if not self.run_dir.is_dir() or self.run_dir == Path("/"):
            raise ValueError("run_dir must be an existing preview directory")
        self.tick(0)
        self.commands_file = (self.run_dir / "commands.json").open("x+", encoding="utf-8")
        self.owns_run = True
        self.handles.append(self.commands_file)
        runtime = self.run_dir / "runtime"
        for path in (runtime / "ffmpeg", runtime / "ffprobe", Path(self.config["media_server"])):
            if not path.is_file() or not os.access(str(path), os.X_OK):
                raise ValueError("missing executable: {}".format(path))
        if not Path(self.config["source_file"]).is_file():
            raise ValueError("source_file does not exist")
        buildinfo = {}
        for line in (runtime / "BUILDINFO.txt").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                buildinfo[key] = value
        self.source_sha = buildinfo.get("source_sha")
        if not self.source_sha:
            raise ValueError("BUILDINFO.txt has no source_sha")
        ffprobe = str(runtime / "ffprobe")
        code, output, errors = self.probe("png-probe", [
            ffprobe, "-v", "error", "-f", "image2", "-pattern_type", "none", "-c:v", "png",
            "-select_streams", "v:0", "-count_frames", "-show_entries",
            "stream=codec_name,width,height,pix_fmt,nb_read_frames", "-of", "json",
            str(self.run_dir / "watermark.png")])
        png = json.loads(output).get("streams", []) if code == 0 and not errors else []
        expected = {"codec_name": "png", "width": 1920, "height": 1080,
                    "pix_fmt": "rgba", "nb_read_frames": "1"}
        if len(png) != 1 or any(png[0].get(key) != value for key, value in expected.items()):
            raise ValueError("watermark.png must decode to one 1920x1080 RGBA frame")
        code, output, errors = self.probe("source-file-probe", [
            ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate", "-of", "json", self.config["source_file"]])
        streams = json.loads(output).get("streams", []) if code == 0 and not errors else []
        if (len(streams) != 1 or streams[0].get("codec_name") != "h264" or
                streams[0].get("width") != 1920 or streams[0].get("height") != 1080 or
                Fraction(streams[0].get("avg_frame_rate", "0")) != 25):
            raise ValueError("source_file must contain 1920x1080 H264 at 25 fps")
        code, version, errors = self.probe("runtime-version", [str(runtime / "ffmpeg"), "-hide_banner", "-version"])
        if code != 0 or not version.splitlines():
            raise ValueError("runtime ffmpeg version check failed")
        paths = [runtime / "ffmpeg", runtime / "ffprobe", runtime / "BUILDINFO.txt",
                 self.run_dir / "watermark.png"]
        paths.extend(path for path in sorted((runtime / "lib").iterdir()) if path.is_file())
        self.write_json("evidence.json", {"buildinfo": buildinfo, "source_sha": self.source_sha,
                        "config": self.config,
                        "ffmpeg_version": version.splitlines()[0], "png": png[0], "source": streams[0],
                        "sha256": {str(path.relative_to(self.run_dir)): file_sha256(path) for path in paths}})

    def start(self):
        self.prepare()
        self.port = choose_port()
        with (self.run_dir / "media-server.ini").open("x", encoding="utf-8") as handle:
            handle.write(make_server_config(self.port))
        (self.run_dir / "server-logs").mkdir()
        commands = make_commands(self.config, self.port)
        self.spawn("server", commands["server"], service=True)
        while True:
            self.tick()
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                continue
        self.spawn("source", commands["source"], service=True)
        attempt = 0
        while True:
            self.tick()
            code, output, errors = self.probe("source-rtsp-probe-{}".format(attempt), [
                str(self.run_dir / "runtime" / "ffprobe"), "-v", "error", "-rtsp_transport", "tcp",
                "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                "-of", "json", "rtsp://127.0.0.1:{}/live/source".format(self.port)])
            if code == 0 and not errors and json.loads(output).get("streams"):
                break
            attempt += 1
        self.spawn("watermark", commands["watermark"], service=True)
        self.write_json("pids.json", {name: process.pid for name, process in self.services.items()})
        while self.frames == 0:
            self.tick()
            self.frames = read_frames(self.run_dir / "watermark-progress.txt")

    def run(self):
        reason, code = "error", 1
        previous_handlers = {}
        try:
            def interrupted(signum, frame):
                raise StopRequested("signal_{}".format(signum))
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, interrupted)
            self.start()
            self.ready_at = self.last_advance = time.monotonic()
            self.send({"event": "ready", "port": self.port, "stream": "watermark",
                       "source_sha": self.source_sha, "frames": self.frames,
                       "worker_pid": os.getpid(),
                       "pids": {name: process.pid for name, process in self.services.items()}})
            while True:
                self.tick(0.25)
                frames = read_frames(self.run_dir / "watermark-progress.txt")
                if frames > self.frames:
                    self.frames = frames
                    self.last_advance = time.monotonic()
                if time.monotonic() - self.last_advance > 15:
                    raise RuntimeError("watermark output stalled for 15 seconds")
        except StopRequested as stopped:
            reason, code = str(stopped), 0
        except Exception as error:
            self.send({"event": "error", "message": str(error)})
        finally:
            for signum in previous_handlers:
                signal.signal(signum, signal.SIG_IGN)
            try:
                stop_processes(self.processes)
            except Exception as error:
                reason, code = "error", 1
                self.send({"event": "error", "message": "cleanup failed: {}".format(error)})
            for handle in self.handles:
                handle.close()
            self.frames = max(self.frames, read_frames(self.run_dir / "watermark-progress.txt"))
            event = {"event": "stopped", "reason": reason, "frames": self.frames,
                     "elapsed": round(time.monotonic() - self.started, 3)}
            if self.owns_run:
                try:
                    self.write_json("summary.json", event)
                except OSError as error:
                    code = 1
                    self.send({"event": "error", "message": "cannot save summary: {}".format(error)})
            self.send(event)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return code


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: watermark_remote.py CONFIG_JSON")
        with open(sys.argv[1], encoding="utf-8") as handle:
            config = json.load(handle)
        return Worker(config, sys.stdin).run()
    except Exception as error:
        emit({"event": "error", "message": str(error)})
        emit({"event": "stopped", "reason": "error", "frames": 0})
        return 1


if __name__ == "__main__":
    sys.exit(main())
