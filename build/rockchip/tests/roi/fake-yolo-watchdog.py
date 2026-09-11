#!/usr/bin/env python3
"""Emit deterministic moving ROI detections with a scheduled silent interval."""

import argparse
import bisect
import errno
import hashlib
import json
import math
import os
import re
import select
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MAX_REGIONS = 8
MAX_PAYLOAD_BYTES = 4096
MAX_PROGRESS_LINE_BYTES = 64 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_INT64 = (1 << 63) - 1
REGION_FIELDS = {"x", "y", "w", "h", "qoffset"}


class ConfigurationError(Exception):
    """The schedule or runtime paths are invalid."""


class DriverError(Exception):
    """The producer could not complete a runtime operation."""


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


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    qoffset: float

    def protocol_object(self) -> Dict[str, object]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "qoffset": self.qoffset}


@dataclass(frozen=True)
class Update:
    frame: int
    regions: Tuple[Region, ...]


@dataclass(frozen=True)
class Phase:
    name: str
    start_frame: int
    end_frame: int
    silent: bool
    updates: Tuple[Update, ...]
    update_frames: Tuple[int, ...]


@dataclass(frozen=True)
class Schedule:
    width: int
    height: int
    fps: Fraction
    repeat_hz: Fraction
    phases: Tuple[Phase, ...]
    phase_starts: Tuple[int, ...]

    @property
    def total_frames(self) -> int:
        return self.phases[-1].end_frame


@dataclass(frozen=True)
class ProgressRecord:
    status: str
    out_time_us: Optional[int]


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result = {}  # type: Dict[str, object]
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate JSON field {!r}".format(key))
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ConfigurationError("non-finite JSON number {!r}".format(value))


def _object(value: object, location: str) -> Dict[str, object]:
    if type(value) is not dict:
        raise ConfigurationError("{} must be an object".format(location))
    return value  # type: ignore[return-value]


def _fields(value: Dict[str, object], location: str, allowed: set, required: set) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ConfigurationError("{} has unknown fields: {}".format(location, ", ".join(unknown)))
    if missing:
        raise ConfigurationError("{} is missing fields: {}".format(location, ", ".join(missing)))


def _integer(value: object, location: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigurationError("{} must be an integer >= {}".format(location, minimum))
    return value


def _number(value: object, location: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ConfigurationError("{} must be a finite number".format(location))
    return float(value)


def _fraction(value: object, location: str) -> Fraction:
    number = _number(value, location)
    if number <= 0:
        raise ConfigurationError("{} must be greater than zero".format(location))
    return Fraction(str(value))


def _string(value: object, location: str) -> str:
    if type(value) is not str or not value.strip():
        raise ConfigurationError("{} must be a non-empty string".format(location))
    return value


def _overlap(first: Region, second: Region) -> bool:
    return (
        first.x < second.x + second.w
        and first.x + first.w > second.x
        and first.y < second.y + second.h
        and first.y + first.h > second.y
    )


def canonical_payload(regions: Sequence[Region], force_empty: bool = False) -> bytes:
    selected = [] if force_empty else [region.protocol_object() for region in regions]
    payload = json.dumps(
        {"version": 1, "regions": selected},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ConfigurationError("canonical payload exceeds {} bytes".format(MAX_PAYLOAD_BYTES))
    return payload


def _parse_regions(value: object, location: str, width: int, height: int) -> Tuple[Region, ...]:
    if type(value) is not list:
        raise ConfigurationError("{} must be an array".format(location))
    if len(value) > MAX_REGIONS:
        raise ConfigurationError("{} has more than {} regions".format(location, MAX_REGIONS))
    regions = []  # type: List[Region]
    for index, item in enumerate(value):
        raw = _object(item, "{}[{}]".format(location, index))
        _fields(raw, "{}[{}]".format(location, index), REGION_FIELDS, REGION_FIELDS)
        x = _integer(raw["x"], "{}.x".format(location))
        y = _integer(raw["y"], "{}.y".format(location))
        w = _integer(raw["w"], "{}.w".format(location), 1)
        h = _integer(raw["h"], "{}.h".format(location), 1)
        qoffset = _number(raw["qoffset"], "{}.qoffset".format(location))
        if x + w > width or y + h > height:
            raise ConfigurationError("{}[{}] is outside the frame".format(location, index))
        if qoffset < -1.0 or qoffset > 1.0:
            raise ConfigurationError("{}[{}].qoffset is outside [-1,1]".format(location, index))
        region = Region(x, y, w, h, qoffset)
        if any(_overlap(previous, region) for previous in regions):
            raise ConfigurationError("{} contains overlapping regions".format(location))
        regions.append(region)
    canonical_payload(regions)
    return tuple(regions)


def parse_schedule(value: object) -> Schedule:
    root = _object(value, "schedule")
    top_fields = {
        "schema_version", "name", "frame", "gop_frames", "repeat_hz",
        "target_bitrate_bps", "watchdog", "probes", "thresholds", "phases",
    }
    _fields(root, "schedule", top_fields, top_fields)
    if _integer(root["schema_version"], "schema_version", 1) != 1:
        raise ConfigurationError("schema_version must be 1")
    _string(root["name"], "name")
    frame = _object(root["frame"], "frame")
    _fields(frame, "frame", {"width", "height", "fps"}, {"width", "height", "fps"})
    width = _integer(frame["width"], "frame.width", 1)
    height = _integer(frame["height"], "frame.height", 1)
    fps = _fraction(frame["fps"], "frame.fps")
    repeat_hz = _fraction(root["repeat_hz"], "repeat_hz")
    _integer(root["gop_frames"], "gop_frames", 1)
    _integer(root["target_bitrate_bps"], "target_bitrate_bps", 1)

    watchdog = _object(root["watchdog"], "watchdog")
    watchdog_fields = {"timeout_ms", "min_clear_delay_ms", "max_clear_delay_ms", "max_resume_forward_ms"}
    _fields(watchdog, "watchdog", watchdog_fields, watchdog_fields)
    minimum = _integer(watchdog["min_clear_delay_ms"], "watchdog.min_clear_delay_ms", 1)
    timeout = _integer(watchdog["timeout_ms"], "watchdog.timeout_ms", 1)
    maximum = _integer(watchdog["max_clear_delay_ms"], "watchdog.max_clear_delay_ms", 1)
    _integer(watchdog["max_resume_forward_ms"], "watchdog.max_resume_forward_ms", 1)
    if not minimum <= timeout <= maximum:
        raise ConfigurationError("watchdog clear bounds must contain timeout_ms")

    probes = _object(root["probes"], "probes")
    if not probes:
        raise ConfigurationError("probes must not be empty")
    for name, probe_value in probes.items():
        _string(name, "probe name")
        probe = _object(probe_value, "probes.{}".format(name))
        _fields(probe, "probes.{}".format(name), {"x", "y", "w", "h"}, {"x", "y", "w", "h"})
        x = _integer(probe["x"], "probe.x")
        y = _integer(probe["y"], "probe.y")
        w = _integer(probe["w"], "probe.w", 1)
        h = _integer(probe["h"], "probe.h", 1)
        if x + w > width or y + h > height:
            raise ConfigurationError("probe {} is outside the frame".format(name))
    thresholds = _object(root["thresholds"], "thresholds")
    if not thresholds:
        raise ConfigurationError("thresholds must not be empty")
    for name, threshold in thresholds.items():
        _string(name, "threshold name")
        _number(threshold, "thresholds.{}".format(name))

    raw_phases = root["phases"]
    if type(raw_phases) is not list or not raw_phases:
        raise ConfigurationError("phases must be a non-empty array")
    phases = []  # type: List[Phase]
    names = set()
    expected_start = 0
    for phase_index, phase_value in enumerate(raw_phases):
        raw_phase = _object(phase_value, "phases[{}]".format(phase_index))
        allowed = {"name", "start_frame", "end_frame", "silent", "updates", "measure"}
        required = {"name", "start_frame", "end_frame", "measure"}
        _fields(raw_phase, "phases[{}]".format(phase_index), allowed, required)
        name = _string(raw_phase["name"], "phase.name")
        if name in names:
            raise ConfigurationError("duplicate phase name {!r}".format(name))
        names.add(name)
        start = _integer(raw_phase["start_frame"], "phase.start_frame")
        end = _integer(raw_phase["end_frame"], "phase.end_frame", 1)
        if start != expected_start or end <= start:
            raise ConfigurationError("phases must be contiguous [start,end) intervals")
        expected_start = end
        _object(raw_phase["measure"], "phase.measure")
        silent_value = raw_phase.get("silent", False)
        if type(silent_value) is not bool:
            raise ConfigurationError("phase.silent must be a boolean")
        silent = bool(silent_value)
        has_updates = "updates" in raw_phase
        if silent == has_updates:
            raise ConfigurationError("phase must contain either silent=true or updates, but not both")
        updates = []  # type: List[Update]
        if has_updates:
            raw_updates = raw_phase["updates"]
            if type(raw_updates) is not list or not raw_updates:
                raise ConfigurationError("phase.updates must be a non-empty array")
            previous_frame = None  # type: Optional[int]
            for update_index, update_value in enumerate(raw_updates):
                raw_update = _object(update_value, "phase.updates[{}]".format(update_index))
                _fields(raw_update, "phase.update", {"frame", "regions"}, {"frame", "regions"})
                update_frame = _integer(raw_update["frame"], "update.frame")
                if not start <= update_frame < end:
                    raise ConfigurationError("update frame is outside its phase")
                if previous_frame is not None and update_frame <= previous_frame:
                    raise ConfigurationError("update frames must be strictly increasing")
                previous_frame = update_frame
                regions = _parse_regions(raw_update["regions"], "update.regions", width, height)
                updates.append(Update(update_frame, regions))
            if updates[0].frame != start:
                raise ConfigurationError("first update must equal phase start_frame")
        phases.append(Phase(name, start, end, silent, tuple(updates), tuple(item.frame for item in updates)))
    return Schedule(width, height, fps, repeat_hz, tuple(phases), tuple(item.start_frame for item in phases))


def load_schedule(path: str) -> Schedule:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except OSError as exc:
        raise ConfigurationError("cannot read schedule {!r}: {}".format(path, exc))
    except json.JSONDecodeError as exc:
        raise ConfigurationError("invalid schedule JSON: {}".format(exc))
    return parse_schedule(value)


def phase_index_for_frame(schedule: Schedule, frame: int) -> Optional[int]:
    index = bisect.bisect_right(schedule.phase_starts, frame) - 1
    if index < 0 or frame >= schedule.phases[index].end_frame:
        return None
    return index


def update_index_for_frame(phase: Phase, frame: int) -> Optional[int]:
    if phase.silent:
        return None
    index = bisect.bisect_right(phase.update_frames, frame) - 1
    return index if index >= 0 else None


def frame_for_out_time_us(out_time_us: int, fps: Fraction) -> int:
    if out_time_us <= 0:
        return 0
    return (out_time_us * fps.numerator + 1_000_000 * fps.denominator - 1) // (1_000_000 * fps.denominator)


def repeat_slot(out_time_us: int, frequency: Fraction) -> int:
    return max(0, out_time_us) * frequency.numerator // (1_000_000 * frequency.denominator)


def frame_for_repeat_slot(slot: int, fps: Fraction, frequency: Fraction) -> int:
    numerator = slot * fps.numerator * frequency.denominator
    denominator = fps.denominator * frequency.numerator
    return numerator // denominator


def _wait_for_socket(path: str, stop: StopState, timeout: float = 30.0, poll: float = 0.01) -> Optional[socket.socket]:
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        while not stop.requested:
            try:
                path_stat = os.stat(path)
                if not stat.S_ISSOCK(path_stat.st_mode):
                    raise DriverError("producer socket path is not a socket")
                client.connect(path)
                return client
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise DriverError("cannot connect producer socket: {}".format(exc))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverError("timed out waiting for producer socket {!r}".format(path))
            stop.wait(min(poll, remaining))
        client.close()
        return None
    except Exception:
        client.close()
        raise


def _open_progress(path: str, stop: StopState, timeout: float = 30.0, poll: float = 0.01) -> Optional[int]:
    deadline = time.monotonic() + timeout
    while not stop.requested:
        try:
            path_stat = os.stat(path)
            if not (stat.S_ISREG(path_stat.st_mode) or stat.S_ISFIFO(path_stat.st_mode)):
                raise DriverError("progress path must be a regular file or FIFO")
            return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise DriverError("cannot open progress stream: {}".format(exc))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("timed out waiting for progress path {!r}".format(path))
        stop.wait(min(poll, remaining))
    return None


def progress_lines(fd: int, stop: StopState, poll: float = 0.01) -> Iterable[str]:
    pending = bytearray()
    is_fifo = stat.S_ISFIFO(os.fstat(fd).st_mode)
    poller = None
    if is_fifo:
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL)
    while not stop.requested:
        flags = 0
        if poller is not None:
            events = poller.poll(max(1, math.ceil(poll * 1000)))
            if not events:
                continue
            flags = events[0][1]
            if flags & (select.POLLERR | select.POLLNVAL):
                raise DriverError("progress FIFO poll error")
        try:
            chunk = os.read(fd, READ_CHUNK_BYTES)
        except BlockingIOError:
            if flags & select.POLLHUP:
                raise DriverError("progress FIFO closed before progress=end")
            stop.wait(poll)
            continue
        except InterruptedError:
            continue
        if not chunk:
            if is_fifo:
                raise DriverError("progress FIFO closed before progress=end")
            stop.wait(poll)
            continue
        pending.extend(chunk)
        if len(pending) > MAX_PROGRESS_LINE_BYTES and b"\n" not in pending:
            raise DriverError("progress line is too long")
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            raw = bytes(pending[:newline])
            del pending[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if len(raw) > MAX_PROGRESS_LINE_BYTES:
                raise DriverError("progress line is too long")
            try:
                yield raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise DriverError("progress is not ASCII at byte {}".format(exc.start))


_INTEGER = re.compile(r"-?[0-9]+\Z")


def progress_records(lines: Iterable[str]) -> Iterable[ProgressRecord]:
    out_time_us = None  # type: Optional[int]
    seen = False
    for line in lines:
        if not line:
            continue
        if "=" not in line:
            raise DriverError("invalid progress line {!r}".format(line))
        key, value = line.split("=", 1)
        if key == "out_time_us":
            if seen:
                raise DriverError("duplicate out_time_us in one progress record")
            seen = True
            if value == "N/A":
                out_time_us = None
            elif _INTEGER.fullmatch(value):
                out_time_us = int(value, 10)
                if not -MAX_INT64 <= out_time_us <= MAX_INT64:
                    raise DriverError("out_time_us is outside int64 range")
            else:
                raise DriverError("invalid out_time_us {!r}".format(value))
        elif key == "progress":
            if value not in ("continue", "end"):
                raise DriverError("invalid progress status {!r}".format(value))
            yield ProgressRecord(value, out_time_us)
            out_time_us = None
            seen = False


class EventWriter:
    def __init__(self, path: str) -> None:
        try:
            self._stream = open(path, "x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise DriverError("cannot create events file {!r}: {}".format(path, exc))
        self._sequence = 0

    def write(self, event: Dict[str, object]) -> None:
        event = dict(event)
        event["sequence"] = self._sequence
        self._sequence += 1
        try:
            self._stream.write(
                json.dumps(
                    event,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            self._stream.flush()
        except OSError as exc:
            raise DriverError("cannot write events file: {}".format(exc))

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError as exc:
            raise DriverError("cannot close events file: {}".format(exc))


def _phase_fields(
    phase: Phase, phase_index: int, frame: int, out_time_us: int
) -> Dict[str, object]:
    return {
        "phase": phase.name,
        "phase_index": phase_index,
        "phase_start_frame": phase.start_frame,
        "phase_end_frame": phase.end_frame,
        "frame": frame,
        "out_time_us": out_time_us,
    }


def _send_update(
    client: socket.socket,
    writer: EventWriter,
    phase: Phase,
    phase_index: int,
    update: Update,
    frame: int,
    out_time_us: int,
    reason: str,
    force_empty: bool,
    send_sequence: int,
) -> None:
    payload = canonical_payload(update.regions, force_empty)
    region_count = 0 if force_empty else len(update.regions)
    event = {
        "event": "producer_send",
        "send_sequence": send_sequence,
        "monotonic_ns": time.monotonic_ns(),
        "state": "empty" if region_count == 0 else "active",
        "reason": reason,
        "update_frame": update.frame,
        "region_count": region_count,
        "payload_bytes": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "force_empty": force_empty,
        "status": "sent",
        **_phase_fields(phase, phase_index, frame, out_time_us),
    }  # type: Dict[str, object]
    try:
        sent = client.send(payload)
        if sent != len(payload):
            raise OSError(
                errno.EIO,
                "short datagram send: {} of {}".format(sent, len(payload)),
            )
    except OSError as exc:
        event["status"] = "send_error"
        event["error"] = str(exc)
        writer.write(event)
        raise DriverError("cannot send producer update: {}".format(exc))
    writer.write(event)


def run_driver(args: argparse.Namespace, stop: StopState) -> int:
    schedule = load_schedule(args.schedule)
    paths = [
        os.path.abspath(value)
        for value in (args.socket_path, args.progress, args.schedule, args.events)
    ]
    if len(set(paths)) != len(paths):
        raise ConfigurationError("socket, progress, schedule and events paths must differ")
    if not os.path.isdir(os.path.dirname(paths[3])):
        raise ConfigurationError("events directory does not exist")
    writer = EventWriter(args.events)
    progress_fd = None  # type: Optional[int]
    client = None  # type: Optional[socket.socket]
    try:
        progress_fd = _open_progress(args.progress, stop)
        if progress_fd is None:
            return 0
        client = _wait_for_socket(args.socket_path, stop)
        if client is None:
            return 0

        phase_index = 0
        phase = schedule.phases[0]
        if phase.silent:
            raise ConfigurationError("first phase must not be silent")
        update_index = 0
        update = phase.updates[0]
        send_sequence = 0
        _send_update(
            client,
            writer,
            phase,
            phase_index,
            update,
            0,
            0,
            "initial",
            args.force_empty,
            send_sequence,
        )
        send_sequence += 1
        last_repeat_slot = 0
        previous_out_time = None  # type: Optional[int]

        for record in progress_records(progress_lines(progress_fd, stop)):
            if stop.requested:
                return 0
            # Completion is evidence only. Never turn its final timestamp into a send.
            if record.status == "end":
                return 0
            if record.out_time_us is None:
                continue
            out_time_us = record.out_time_us
            if previous_out_time is not None and out_time_us < previous_out_time:
                raise DriverError("out_time_us moved backwards")
            previous_out_time = out_time_us
            slot = repeat_slot(out_time_us, schedule.repeat_hz)
            for heartbeat_slot in range(last_repeat_slot + 1, slot + 1):
                frame = frame_for_repeat_slot(
                    heartbeat_slot, schedule.fps, schedule.repeat_hz
                )
                new_phase_index = phase_index_for_frame(schedule, frame)
                if new_phase_index is None:
                    raise DriverError(
                        "progress reached heartbeat frame {} outside schedule "
                        "before progress=end".format(frame)
                    )
                if new_phase_index != phase_index:
                    phase_index = new_phase_index
                    phase = schedule.phases[phase_index]
                    if phase.silent:
                        update_index = -1
                        writer.write(
                            {
                                "event": "producer_state",
                                "monotonic_ns": time.monotonic_ns(),
                                "state": "silent",
                                "reason": "phase_change",
                                "update_frame": None,
                                "region_count": None,
                                "payload_bytes": 0,
                                "payload_sha256": None,
                                "force_empty": args.force_empty,
                                "status": "entered",
                                **_phase_fields(
                                    phase, phase_index, frame, out_time_us
                                ),
                            }
                        )
                        continue
                    update_index = update_index_for_frame(phase, frame)
                    if update_index is None:
                        raise DriverError(
                            "non-silent phase has no update for frame {}".format(
                                frame
                            )
                        )
                    update = phase.updates[update_index]
                    _send_update(
                        client,
                        writer,
                        phase,
                        phase_index,
                        update,
                        frame,
                        out_time_us,
                        "phase_change",
                        args.force_empty,
                        send_sequence,
                    )
                    send_sequence += 1
                    continue
                if phase.silent:
                    continue

                new_update_index = update_index_for_frame(phase, frame)
                if new_update_index is None:
                    raise DriverError(
                        "phase has no update for frame {}".format(frame)
                    )
                changed = new_update_index != update_index
                update_index = new_update_index
                update = phase.updates[update_index]
                reason = "update" if changed else "heartbeat"
                _send_update(
                    client,
                    writer,
                    phase,
                    phase_index,
                    update,
                    frame,
                    out_time_us,
                    reason,
                    args.force_empty,
                    send_sequence,
                )
                send_sequence += 1
            last_repeat_slot = slot
        return 0
    finally:
        if progress_fd is not None:
            os.close(progress_fd)
        if client is not None:
            client.close()
        writer.close()


def _sample_schedule() -> Dict[str, object]:
    return {
        "schema_version": 1,
        "name": "test",
        "frame": {"width": 100, "height": 100, "fps": 30},
        "gop_frames": 30,
        "repeat_hz": 10,
        "target_bitrate_bps": 1000000,
        "watchdog": {
            "timeout_ms": 40,
            "min_clear_delay_ms": 40,
            "max_clear_delay_ms": 80,
            "max_resume_forward_ms": 20,
        },
        "probes": {"A": {"x": 0, "y": 0, "w": 10, "h": 10}},
        "thresholds": {"minimum": 0.1},
        "phases": [
            {
                "name": "active",
                "start_frame": 0,
                "end_frame": 30,
                "updates": [
                    {
                        "frame": 0,
                        "regions": [
                            {
                                "x": 10,
                                "y": 10,
                                "w": 20,
                                "h": 20,
                                "qoffset": -0.25,
                            }
                        ],
                    }
                ],
                "measure": {},
            },
            {
                "name": "silent",
                "start_frame": 30,
                "end_frame": 60,
                "silent": True,
                "measure": {},
            },
            {
                "name": "empty",
                "start_frame": 60,
                "end_frame": 90,
                "updates": [{"frame": 60, "regions": []}],
                "measure": {},
            },
        ],
    }


class ProducerSelfTest(unittest.TestCase):
    def test_schedule_lookup_and_payload(self) -> None:
        schedule = parse_schedule(_sample_schedule())
        self.assertEqual(phase_index_for_frame(schedule, 29), 0)
        self.assertEqual(phase_index_for_frame(schedule, 30), 1)
        self.assertEqual(phase_index_for_frame(schedule, 60), 2)
        self.assertIsNone(phase_index_for_frame(schedule, 90))
        self.assertTrue(schedule.phases[1].silent)
        self.assertEqual(
            canonical_payload(schedule.phases[0].updates[0].regions),
            b'{"regions":[{"h":20,"qoffset":-0.25,"w":20,"x":10,'
            b'"y":10}],"version":1}',
        )

    def test_schedule_rejects_updates_in_silent_phase(self) -> None:
        value = _sample_schedule()
        value["phases"][1]["updates"] = []  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "either silent=true or updates"):
            parse_schedule(value)

    def test_progress_end_does_not_send(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.payloads = []  # type: List[bytes]

            def send(self, payload: bytes) -> int:
                self.payloads.append(payload)
                return len(payload)

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            schedule_path = base / "schedule.json"
            progress_path = base / "progress.txt"
            events_path = base / "events.jsonl"
            schedule_path.write_text(
                json.dumps(_sample_schedule()), encoding="utf-8"
            )
            progress_path.write_text(
                "out_time_us=966667\nprogress=end\n", encoding="ascii"
            )
            fake = FakeSocket()
            args = argparse.Namespace(
                socket_path=str(base / "socket"),
                progress=str(progress_path),
                schedule=str(schedule_path),
                events=str(events_path),
                force_empty=False,
            )
            original = _wait_for_socket
            try:
                globals()["_wait_for_socket"] = lambda *_args, **_kwargs: fake
                self.assertEqual(run_driver(args, StopState()), 0)
            finally:
                globals()["_wait_for_socket"] = original
            self.assertEqual(len(fake.payloads), 1)
            event = json.loads(events_path.read_text().splitlines()[0])
            self.assertEqual(event["reason"], "initial")

    def test_silent_phase_has_no_send_and_recovery_is_immediate(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.payloads = []  # type: List[bytes]

            def send(self, payload: bytes) -> int:
                self.payloads.append(payload)
                return len(payload)

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            schedule_path = base / "schedule.json"
            progress_path = base / "progress.txt"
            events_path = base / "events.jsonl"
            schedule_path.write_text(
                json.dumps(_sample_schedule()), encoding="utf-8"
            )
            records = []
            for slot in range(1, 30):
                records.append(
                    "out_time_us={}\nprogress=continue\n".format(slot * 100000)
                )
            records.append("out_time_us=3000000\nprogress=end\n")
            progress_path.write_text("".join(records), encoding="ascii")
            fake = FakeSocket()
            args = argparse.Namespace(
                socket_path=str(base / "socket"),
                progress=str(progress_path),
                schedule=str(schedule_path),
                events=str(events_path),
                force_empty=False,
            )
            original = _wait_for_socket
            try:
                globals()["_wait_for_socket"] = lambda *_args, **_kwargs: fake
                self.assertEqual(run_driver(args, StopState()), 0)
            finally:
                globals()["_wait_for_socket"] = original
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            sends = [event for event in events if event["event"] == "producer_send"]
            states = [event for event in events if event["event"] == "producer_state"]
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0]["phase"], "silent")
            self.assertEqual(states[0]["state"], "silent")
            self.assertFalse(any(event["phase"] == "silent" for event in sends))
            recovery = [event for event in sends if event["phase"] == "empty"][0]
            self.assertEqual(recovery["reason"], "phase_change")
            self.assertEqual(recovery["region_count"], 0)
            self.assertEqual(recovery["frame"], 60)
            self.assertEqual(
                [event["send_sequence"] for event in sends], list(range(len(sends)))
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", dest="socket_path")
    parser.add_argument("--progress")
    parser.add_argument("--schedule")
    parser.add_argument("--events")
    parser.add_argument("--force-empty", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--validate-schedule")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.self_test:
        if arguments != ["--self-test"]:
            parser.error("--self-test cannot be combined with other arguments")
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ProducerSelfTest)
        )
        return 0 if result.wasSuccessful() else 1
    if args.validate_schedule is not None:
        if arguments != ["--validate-schedule", args.validate_schedule]:
            parser.error("--validate-schedule cannot be combined with other arguments")
        try:
            schedule = load_schedule(args.validate_schedule)
        except ConfigurationError as exc:
            print(
                "fake-yolo-watchdog: schedule validation failed: {}".format(exc),
                file=sys.stderr,
            )
            return 2
        print(
            "schedule valid: {}x{} fps={} phases={} frames={}".format(
                schedule.width,
                schedule.height,
                float(schedule.fps),
                len(schedule.phases),
                schedule.total_frames,
            )
        )
        return 0
    missing = [
        name
        for name in ("socket_path", "progress", "schedule", "events")
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("missing required arguments: {}".format(", ".join(missing)))
    stop = StopState()
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda number, _frame: stop.request(number))
    try:
        return run_driver(args, stop)
    except ConfigurationError as exc:
        print("fake-yolo-watchdog: configuration error: {}".format(exc), file=sys.stderr)
        return 2
    except DriverError as exc:
        print("fake-yolo-watchdog: error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(main())
