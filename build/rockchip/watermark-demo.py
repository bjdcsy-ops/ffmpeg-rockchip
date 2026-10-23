#!/usr/bin/env python3
"""Generate a text PNG locally and run an isolated RTSP demo over SSH."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from watermark_image import render_watermark


HERE = Path(__file__).resolve().parent
DEFAULT_PACKAGE = HERE.parent.parent / 'artifact/ffmpeg-rockchip-rk3576-ubuntu20-arm64'
BOOTSTRAP = (
    'import os,sys,tarfile; '
    'os.makedirs(sys.argv[1],exist_ok=False); '
    'tarfile.open(fileobj=sys.stdin.buffer,mode="r|").extractall(sys.argv[1])'
)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('single', 'matrix', 'diagonal'), default='single')
    parser.add_argument('--text', default='RTSP WATERMARK', help='Single-line watermark text')
    parser.add_argument('--font', type=Path, help='Local TrueType/OpenType font; select a CJK font for Chinese')
    parser.add_argument('--font-size', type=int, default=44, help='Font size in output pixels (8..256)')
    parser.add_argument('--alpha', type=int, default=51, help='Opacity, 0..255; 51 is 20%%')
    parser.add_argument('--angle', type=float, default=30, help='Counterclockwise angle for diagonal mode (-90..90)')
    parser.add_argument('--gap-x', type=int, default=56, help='Horizontal gap between text bounds in pixels')
    parser.add_argument('--gap-y', type=int, default=148, help='Vertical gap between text bounds in pixels')
    parser.add_argument('--x', type=int, default=32, help='Single text left edge / matrix phase offset')
    parser.add_argument('--y', type=int, default=32, help='Single text top edge / matrix phase offset')
    parser.add_argument('--host', default='RK3576', help='SSH host alias or user@host (non-interactive authentication)')
    parser.add_argument('--package-dir', type=Path, default=DEFAULT_PACKAGE, help='Local complete Rockchip runtime package')
    parser.add_argument('--remote-root', default='/run/media/mmcblk0p8/ffmpeg-watermark-demo', help='Remote evidence parent directory')
    parser.add_argument('--source-file', help='Existing remote H.264 MP4; default: REMOTE_ROOT/source.mp4')
    parser.add_argument('--media-server', default='/opt/zlmediakit/MediaServer', help='Remote ZLMediaKit executable')
    parser.add_argument('--local-port', type=int, default=0, help='Local forwarded RTSP port; 0 selects a free port')
    parser.add_argument('--duration', type=int, default=3600, help='Wall-clock run limit in seconds (10..3600)')
    parser.add_argument('--output-dir', type=Path, help='New local evidence directory; default: a temporary directory')
    parser.add_argument('--open', action='store_true', help='Open the stream in a new VLC instance on macOS')
    args = parser.parse_args(argv)
    if args.host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', args.host):
        parser.error('--host must be an SSH host alias or user@host, without shell characters')
    if not 0 <= args.alpha <= 255:
        parser.error('--alpha must be in 0..255')
    if not 10 <= args.duration <= 3600:
        parser.error('--duration must be in 10..3600 seconds')
    if not 0 <= args.local_port <= 65535:
        parser.error('--local-port must be in 0..65535')
    if args.source_file is None:
        args.source_file = str(PurePosixPath(args.remote_root) / 'source.mp4')
    for value in (args.remote_root, args.source_file, args.media_server):
        if not value.startswith('/') or '\0' in value or '\n' in value or '\r' in value:
            parser.error('remote paths must be absolute, without NUL or newlines')
    if args.open and (sys.platform != 'darwin' or not shutil.which('open')):
        parser.error('--open requires macOS and VLC')
    return args


def ssh_command(host, remote_argv=None, forward=None):
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3']
    if forward is not None:
        local_port, remote_port = forward
        command += ['-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                    '127.0.0.1:{}:127.0.0.1:{}'.format(local_port, remote_port)]
    command += ['--', host]
    if remote_argv is not None:
        command.append(shlex.join(str(value) for value in remote_argv))
    return command


def available_port(requested):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', requested))
        return listener.getsockname()[1]


def package_archive(package, directory, config):
    package = package.resolve()
    for name in ('ffmpeg', 'ffprobe', 'BUILDINFO.txt', 'lib/libz.so.1'):
        if not (package / name).is_file():
            raise ValueError('Incomplete runtime package: {}'.format(package / name))
    for path in package.rglob('*'):
        if path.is_symlink():
            try:
                path.resolve().relative_to(package)
            except ValueError:
                raise ValueError('Runtime symlink escapes package: {}'.format(path))
    config_file = directory / 'config.json'
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = directory / 'upload.tar'
    with tarfile.open(archive, 'w') as bundle:
        bundle.add(package, arcname='runtime')
        bundle.add(HERE / 'watermark_remote.py', arcname='worker.py')
        bundle.add(config_file, arcname='config.json')
        bundle.add(directory / 'watermark.png', arcname='watermark.png')
    return archive


def next_event(worker, deadline):
    while time.monotonic() < deadline:
        readable, _, _ = select.select([worker.stdout], [], [], min(1, max(0, deadline - time.monotonic())))
        if readable:
            line = worker.stdout.readline()
            if not line:
                raise RuntimeError('Remote worker disconnected; see ssh.log')
            event = json.loads(line)
            if event.get('event') == 'error':
                raise RuntimeError('Remote worker: {}'.format(event.get('message', event)))
            return event
        if worker.poll() is not None:
            raise RuntimeError('Remote worker exited; see ssh.log')
    return None


def close_session(worker, tunnel):
    if worker is not None:
        if worker.stdin is not None:
            try:
                worker.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            worker.wait(timeout=12)
        except subprocess.TimeoutExpired:
            worker.terminate()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
        if worker.stdout is not None:
            worker.stdout.close()
    if tunnel is not None:
        if tunnel.poll() is None:
            tunnel.terminate()
        try:
            tunnel.wait(timeout=3)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()


def run_demo(args):
    if not shutil.which('ssh'):
        raise ValueError('OpenSSH client is required')
    local_port = available_port(args.local_port)
    if args.font is None:
        fonts = ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        args.font = next((Path(path) for path in fonts if Path(path).is_file()), None)
        if args.font is None:
            raise ValueError('No default font found; pass --font /path/to/font.ttf')
    if args.output_dir is None:
        directory = Path(tempfile.mkdtemp(prefix='rockchip-watermark-{}-'.format(args.mode)))
    else:
        directory = args.output_dir.resolve()
        directory.mkdir(parents=True, exist_ok=False)
    print('Local evidence: {}'.format(directory), flush=True)
    image = render_watermark(directory / 'watermark.png', mode=args.mode, text=args.text,
                             font_path=str(args.font), font_size=args.font_size, angle=args.angle,
                             gap_x=args.gap_x, gap_y=args.gap_y, x=args.x, y=args.y)
    run_dir = str(PurePosixPath(args.remote_root) / ('run-' + uuid.uuid4().hex))
    config = {'run_dir': run_dir, 'source_file': args.source_file, 'media_server': args.media_server,
              'alpha': args.alpha, 'duration': args.duration, 'mode': args.mode}
    archive = package_archive(args.package_dir, directory, config)
    print('Remote evidence: {}:{}'.format(args.host, run_dir), flush=True)
    with archive.open('rb') as payload:
        upload = subprocess.run(ssh_command(args.host, ['python3', '-c', BOOTSTRAP, run_dir]),
                                stdin=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=120, check=False)
    archive.unlink()
    if upload.returncode:
        raise RuntimeError('Upload failed: {}'.format(upload.stderr.decode('utf-8', errors='replace').strip()))
    worker = tunnel = None
    with (directory / 'ssh.log').open('w') as worker_log, (directory / 'tunnel.log').open('w') as tunnel_log:
        try:
            worker = subprocess.Popen(ssh_command(args.host, ['python3', str(PurePosixPath(run_dir) / 'worker.py'),
                                                             str(PurePosixPath(run_dir) / 'config.json')]),
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=worker_log,
                                      text=True, encoding='utf-8')
            deadline = time.monotonic() + args.duration + 60
            ready = next_event(worker, time.monotonic() + 60)
            if ready is None or ready.get('event') != 'ready':
                raise RuntimeError('Expected ready event, received {}'.format(ready))
            tunnel = subprocess.Popen(ssh_command(args.host, forward=(local_port, int(ready['port']))),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=tunnel_log)
            for _ in range(50):
                if tunnel.poll() is not None:
                    raise RuntimeError('SSH forwarding failed; see tunnel.log')
                try:
                    with socket.create_connection(('127.0.0.1', local_port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise TimeoutError('SSH forwarding did not become ready')
            url = 'rtsp://127.0.0.1:{}/live/{}'.format(local_port, ready['stream'])
            evidence = {'url': url, 'host': args.host, 'remote_dir': run_dir, 'mode': args.mode,
                        'alpha': args.alpha, 'duration': args.duration, 'image': image, 'ready': ready}
            (directory / 'demo.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print('RTSP: {}\nRuntime source: {}\n'
                  'Press Ctrl+C to stop; automatic stop after {} seconds.'.format(
                      url, ready.get('source_sha', 'unknown'), args.duration), flush=True)
            if args.open:
                subprocess.run(['open', '-n', '-a', 'VLC', '--args', '--rtsp-tcp', url], check=True)
            while True:
                if tunnel.poll() is not None:
                    raise RuntimeError('SSH forwarding exited; see tunnel.log')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Remote worker exceeded its run limit')
                event = next_event(worker, min(deadline, time.monotonic() + 2))
                if event is None:
                    continue
                if event.get('event') == 'stopped':
                    (directory / 'stopped.json').write_text(json.dumps(event, indent=2) + '\n')
                    print('Stopped: {}'.format(event.get('reason', 'completed')), flush=True)
                    break
        finally:
            close_session(worker, tunnel)


def interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    args = arguments(argv)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        run_demo(args)
    except KeyboardInterrupt:
        print('Stopped by user; owned remote processes and SSH forwarding closed.', flush=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
        print('Watermark demo failed: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
