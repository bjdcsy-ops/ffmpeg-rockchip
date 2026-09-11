#!/usr/bin/env python3
"""Forward canonical ROI datagrams and clear stale state with a watchdog."""

import argparse
import errno
import hashlib
import json
import math
import os
import select
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


MAX_REGIONS = 8
MAX_PAYLOAD_BYTES = 4096
REGION_FIELDS = {"x", "y", "w", "h", "qoffset"}
EMPTY_PAYLOAD = b'{"regions":[],"version":1}'


class ConfigurationError(Exception):
    """The command line or a received payload is invalid."""


class BridgeError(Exception):
    """The bridge could not complete a runtime operation."""


class StopState:
    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self, _signal_number: int = 0) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result = {}  # type: Dict[str, object]
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate JSON field {!r}".format(key))
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ConfigurationError("non-finite JSON number {!r}".format(value))


def _canonical_payload(regions: Sequence[Dict[str, object]]) -> bytes:
    normalized = []  # type: List[Dict[str, object]]
    for region in regions:
        normalized.append(
            {
                "x": region["x"],
                "y": region["y"],
                "w": region["w"],
                "h": region["h"],
                "qoffset": float(region["qoffset"]),
            }
        )
    return json.dumps(
        {"version": 1, "regions": normalized},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _overlap(first: Dict[str, object], second: Dict[str, object]) -> bool:
    ax = int(first["x"])
    ay = int(first["y"])
    aw = int(first["w"])
    ah = int(first["h"])
    bx = int(second["x"])
    by = int(second["y"])
    bw = int(second["w"])
    bh = int(second["h"])
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def parse_canonical_payload(payload: bytes, width: int, height: int) -> List[Dict[str, object]]:
    """Validate the exact canonical wire representation and return its regions."""

    if not payload:
        raise ConfigurationError("empty datagram")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ConfigurationError("datagram exceeds {} bytes".format(MAX_PAYLOAD_BYTES))
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("payload is not ASCII at byte {}".format(exc.start))
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ConfigurationError) as exc:
        raise ConfigurationError("invalid JSON: {}".format(exc))
    if type(value) is not dict or set(value) != {"version", "regions"}:
        raise ConfigurationError("top level must contain only version and regions")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ConfigurationError("version must be integer 1")
    raw_regions = value["regions"]
    if type(raw_regions) is not list:
        raise ConfigurationError("regions must be an array")
    if len(raw_regions) > MAX_REGIONS:
        raise ConfigurationError("regions contains more than {} entries".format(MAX_REGIONS))

    regions = []  # type: List[Dict[str, object]]
    for index, raw in enumerate(raw_regions):
        if type(raw) is not dict or set(raw) != REGION_FIELDS:
            raise ConfigurationError(
                "regions[{}] must contain only x, y, w, h and qoffset".format(index)
            )
        for field in ("x", "y", "w", "h"):
            if type(raw[field]) is not int:
                raise ConfigurationError("regions[{}].{} must be an integer".format(index, field))
        x = int(raw["x"])
        y = int(raw["y"])
        w = int(raw["w"])
        h = int(raw["h"])
        qvalue = raw["qoffset"]
        if type(qvalue) not in (int, float) or not math.isfinite(float(qvalue)):
            raise ConfigurationError("regions[{}].qoffset must be finite".format(index))
        qoffset = float(qvalue)
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ConfigurationError("regions[{}] is outside the frame".format(index))
        if qoffset < -1.0 or qoffset > 1.0:
            raise ConfigurationError("regions[{}].qoffset is outside [-1,1]".format(index))
        region = {"x": x, "y": y, "w": w, "h": h, "qoffset": qoffset}
        for previous in regions:
            if _overlap(previous, region):
                raise ConfigurationError("regions overlap")
        regions.append(region)

    canonical = _canonical_payload(regions)
    if payload != canonical:
        raise ConfigurationError("payload is valid JSON but not canonical")
    return regions


class EventWriter:
    def __init__(self, path: str) -> None:
        try:
            self._stream = open(path, "x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise BridgeError("cannot create events file {!r}: {}".format(path, exc))
        self._sequence = 0

    def write(self, event: Dict[str, object]) -> None:
        event = dict(event)
        event["sequence"] = self._sequence
        self._sequence += 1
        try:
            self._stream.write(
                json.dumps(event, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                + "\n"
            )
            self._stream.flush()
        except OSError as exc:
            raise BridgeError("cannot write events file: {}".format(exc))

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError as exc:
            raise BridgeError("cannot close events file: {}".format(exc))


def _payload_evidence(payload: bytes, region_count: Optional[int]) -> Dict[str, object]:
    return {
        "payload_bytes": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "region_count": region_count,
    }


def _wait_and_connect(path: str, stop: StopState, timeout: float, poll: float) -> Optional[socket.socket]:
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    last_error = None  # type: Optional[BaseException]
    try:
        while not stop.requested:
            try:
                path_stat = os.stat(path)
                if not stat.S_ISSOCK(path_stat.st_mode):
                    raise BridgeError("forward path is not a socket: {!r}".format(path))
                client.connect(path)
                client.setblocking(False)
                return client
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_error = exc
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    last_error = exc
                else:
                    raise BridgeError("cannot connect forward socket {!r}: {}".format(path, exc))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = ": {}".format(last_error) if last_error else ""
                raise BridgeError(
                    "timed out after {:.3f}s waiting for forward socket {!r}{}".format(timeout, path, detail)
                )
            stop.wait(min(poll, remaining))
        client.close()
        return None
    except Exception:
        client.close()
        raise


def _safe_unlink_bound_socket(path: str, identity: Optional[Tuple[int, int]]) -> None:
    if identity is None:
        return
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        os.unlink(path)


def run_bridge(args: argparse.Namespace, stop: StopState) -> int:
    listen_path = os.path.abspath(args.listen)
    forward_path = os.path.abspath(args.forward)
    events_path = os.path.abspath(args.events)
    if len({listen_path, forward_path, events_path}) != 3:
        raise ConfigurationError("--listen, --forward and --events must be different paths")
    if not os.path.isdir(os.path.dirname(listen_path)):
        raise ConfigurationError("listen directory does not exist")
    if not os.path.isdir(os.path.dirname(events_path)):
        raise ConfigurationError("events directory does not exist")
    if os.path.lexists(listen_path):
        raise ConfigurationError("listen path already exists: {!r}".format(listen_path))

    writer = EventWriter(events_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    forward = None  # type: Optional[socket.socket]
    bound_identity = None  # type: Optional[Tuple[int, int]]
    rx_sequence = 0
    forward_sequence = 0
    last_valid_ns = None  # type: Optional[int]
    last_valid_rx_sequence = None  # type: Optional[int]
    armed = False
    timeout_ns = args.timeout_ms * 1_000_000
    try:
        try:
            listener.bind(listen_path)
        except OSError as exc:
            raise BridgeError("cannot bind listen socket {!r}: {}".format(listen_path, exc))
        bound_stat = os.lstat(listen_path)
        bound_identity = (bound_stat.st_dev, bound_stat.st_ino)
        os.chmod(listen_path, 0o600)
        listener.setblocking(False)
        forward = _wait_and_connect(forward_path, stop, args.wait_timeout, args.poll_ms / 1000.0)
        if forward is None:
            return 0

        while not stop.requested:
            now_ns = time.monotonic_ns()
            wait_seconds = args.poll_ms / 1000.0
            if armed and last_valid_ns is not None:
                remaining_ns = last_valid_ns + timeout_ns - now_ns
                wait_seconds = min(wait_seconds, max(0.0, remaining_ns / 1_000_000_000.0))
            try:
                readable, _, _ = select.select([listener], [], [], wait_seconds)
            except InterruptedError:
                continue

            if readable:
                try:
                    payload, _ancillary, flags, _address = listener.recvmsg(MAX_PAYLOAD_BYTES + 1)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise BridgeError("cannot receive producer datagram: {}".format(exc))
                rx_ns = time.monotonic_ns()
                current_rx = rx_sequence
                rx_sequence += 1
                regions = None  # type: Optional[List[Dict[str, object]]]
                error = None  # type: Optional[str]
                if flags & socket.MSG_TRUNC or len(payload) > MAX_PAYLOAD_BYTES:
                    error = "datagram exceeds {} bytes".format(MAX_PAYLOAD_BYTES)
                else:
                    try:
                        regions = parse_canonical_payload(payload, args.width, args.height)
                    except ConfigurationError as exc:
                        error = str(exc)
                rx_event = {
                    "event": "roi_rx",
                    "rx_sequence": current_rx,
                    "monotonic_ns": rx_ns,
                    "status": "valid" if error is None else "rejected",
                    **_payload_evidence(payload, len(regions) if regions is not None else None),
                }  # type: Dict[str, object]
                if error is not None:
                    rx_event["error"] = error
                writer.write(rx_event)
                if error is not None:
                    continue

                sent_ns = time.monotonic_ns()
                forward_event = {
                    "event": "roi_forward",
                    "forward_sequence": forward_sequence,
                    "rx_sequence": current_rx,
                    "monotonic_ns": sent_ns,
                    "reason": "producer_update",
                    "status": "sent",
                    **_payload_evidence(payload, len(regions)),
                }  # type: Dict[str, object]
                try:
                    sent = forward.send(payload)
                    if sent != len(payload):
                        raise OSError(errno.EIO, "short datagram send: {} of {}".format(sent, len(payload)))
                except OSError as exc:
                    forward_event["status"] = "send_error"
                    forward_event["error"] = str(exc)
                    writer.write(forward_event)
                    raise BridgeError("cannot forward ROI datagram: {}".format(exc))
                writer.write(forward_event)
                forward_sequence += 1
                last_valid_ns = rx_ns
                last_valid_rx_sequence = current_rx
                armed = True

            now_ns = time.monotonic_ns()
            if armed and last_valid_ns is not None and now_ns >= last_valid_ns + timeout_ns:
                clear_event = {
                    "event": "watchdog_clear",
                    "forward_sequence": forward_sequence,
                    "monotonic_ns": now_ns,
                    "reason": "watchdog_clear",
                    "last_valid_rx_sequence": last_valid_rx_sequence,
                    "last_valid_rx_monotonic_ns": last_valid_ns,
                    "stale_age_ns": now_ns - last_valid_ns,
                    "timeout_ms": args.timeout_ms,
                    "status": "sent",
                    **_payload_evidence(EMPTY_PAYLOAD, 0),
                }  # type: Dict[str, object]
                try:
                    sent = forward.send(EMPTY_PAYLOAD)
                    if sent != len(EMPTY_PAYLOAD):
                        raise OSError(errno.EIO, "short watchdog send: {} of {}".format(sent, len(EMPTY_PAYLOAD)))
                except OSError as exc:
                    clear_event["status"] = "send_error"
                    clear_event["error"] = str(exc)
                    writer.write(clear_event)
                    raise BridgeError("cannot send watchdog clear: {}".format(exc))
                writer.write(clear_event)
                forward_sequence += 1
                armed = False
        return 0
    finally:
        if forward is not None:
            forward.close()
        listener.close()
        _safe_unlink_bound_socket(listen_path, bound_identity)
        writer.close()


class BridgeSelfTest(unittest.TestCase):
    def test_canonical_validation(self) -> None:
        active = b'{"regions":[{"h":40,"qoffset":-0.25,"w":30,"x":10,"y":20}],"version":1}'
        self.assertEqual(len(parse_canonical_payload(active, 1920, 1080)), 1)
        self.assertEqual(parse_canonical_payload(EMPTY_PAYLOAD, 1920, 1080), [])
        with self.assertRaisesRegex(ConfigurationError, "not canonical"):
            parse_canonical_payload(b'{"version":1,"regions":[]}', 1920, 1080)
        with self.assertRaisesRegex(ConfigurationError, "overlap"):
            parse_canonical_payload(
                b'{"regions":[{"h":20,"qoffset":-0.25,"w":20,"x":0,'
                b'"y":0},{"h":20,"qoffset":-0.25,"w":20,"x":10,'
                b'"y":10}],"version":1}',
                100,
                100,
            )

    def test_forward_watchdog_and_rearm(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            listen_path = str(base / "bridge.sock")
            forward_path = str(base / "encoder.sock")
            events_path = str(base / "events.jsonl")
            sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sink.bind(forward_path)
            sink.settimeout(1.0)
            args = argparse.Namespace(
                listen=listen_path,
                forward=forward_path,
                events=events_path,
                width=1920,
                height=1080,
                timeout_ms=40,
                poll_ms=5,
                wait_timeout=1.0,
            )
            stop = StopState()
            result = []  # type: List[int]
            worker = threading.Thread(
                target=lambda: result.append(run_bridge(args, stop))
            )
            worker.start()
            deadline = time.monotonic() + 1.0
            while not os.path.exists(listen_path) and time.monotonic() < deadline:
                time.sleep(0.005)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client.connect(listen_path)
            active = b'{"regions":[{"h":40,"qoffset":-0.25,"w":30,"x":10,"y":20}],"version":1}'
            client.send(active)
            self.assertEqual(sink.recv(MAX_PAYLOAD_BYTES), active)
            client.send(b"bad")
            self.assertEqual(sink.recv(MAX_PAYLOAD_BYTES), EMPTY_PAYLOAD)
            client.send(EMPTY_PAYLOAD)
            self.assertEqual(sink.recv(MAX_PAYLOAD_BYTES), EMPTY_PAYLOAD)
            self.assertEqual(sink.recv(MAX_PAYLOAD_BYTES), EMPTY_PAYLOAD)
            stop.request()
            worker.join(1.0)
            client.close()
            sink.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [0])
            self.assertFalse(os.path.exists(listen_path))
            events = [
                json.loads(line)
                for line in Path(events_path).read_text().splitlines()
            ]
            self.assertEqual(
                [event["sequence"] for event in events], list(range(len(events)))
            )
            self.assertEqual(
                sum(event["event"] == "watchdog_clear" for event in events), 2
            )
            clears = [event for event in events if event["event"] == "watchdog_clear"]
            self.assertTrue(
                all(event["stale_age_ns"] >= 40_000_000 for event in clears)
            )
            rejected = [
                event
                for event in events
                if event["event"] == "roi_rx" and event["status"] == "rejected"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(clears[0]["last_valid_rx_sequence"], 0)


def positive_int(value: str) -> int:
    try:
        result = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen")
    parser.add_argument("--forward")
    parser.add_argument("--events")
    parser.add_argument("--width", type=positive_int)
    parser.add_argument("--height", type=positive_int)
    parser.add_argument("--timeout-ms", type=positive_int, default=400)
    parser.add_argument("--poll-ms", type=positive_int, default=10)
    parser.add_argument("--wait-timeout", type=positive_float, default=30.0)
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.self_test:
        if arguments != ["--self-test"]:
            parser.error("--self-test cannot be combined with other arguments")
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(BridgeSelfTest)
        )
        return 0 if result.wasSuccessful() else 1
    missing = [
        name
        for name in ("listen", "forward", "events", "width", "height")
        if getattr(args, name) is None
    ]
    if missing:
        options = ", ".join("--" + value.replace("_", "-") for value in missing)
        parser.error("the following arguments are required: {}".format(options))
    if args.poll_ms > args.timeout_ms:
        parser.error("--poll-ms must not exceed --timeout-ms")

    stop = StopState()
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, lambda number, _frame: stop.request(number))
    signal.signal(signal.SIGINT, lambda number, _frame: stop.request(number))
    try:
        return run_bridge(args, stop)
    except ConfigurationError as exc:
        print("roi-watchdog-bridge: configuration error: {}".format(exc), file=sys.stderr)
        return 2
    except BridgeError as exc:
        print("roi-watchdog-bridge: error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(main())
