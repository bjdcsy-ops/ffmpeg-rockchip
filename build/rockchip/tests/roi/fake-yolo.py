#!/usr/bin/env python3
"""Replay deterministic YOLO-like ROI updates against an FFmpeg encoder.

The driver follows FFmpeg media time from a ``-progress`` FIFO or regular
file.  It sends each phase's complete ROI set through the encoder's Unix
datagram socket, both at phase boundaries and at the configured media-time
repeat rate.  It never derives timing from wall-clock execution speed.
"""

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
from unittest import mock
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MAX_REGIONS = 8
MAX_PAYLOAD_BYTES = 4096
MAX_FRAME_DIMENSION = 65535
MAX_FRAME_NUMBER = (1 << 63) - 1
MAX_TARGET_BITRATE_BPS = 1_000_000_000
MAX_PROGRESS_LINE_BYTES = 64 * 1024
READ_CHUNK_BYTES = 64 * 1024
TERMINAL_REPEAT_GUARD_SLOTS = 3

TOP_LEVEL_FIELDS = {
    "schema_version",
    "name",
    "frame",
    "gop_frames",
    "repeat_hz",
    "target_bitrate_bps",
    "probes",
    "thresholds",
    "phases",
}
FRAME_FIELDS = {"width", "height", "fps"}
PHASE_FIELDS = {"name", "start_frame", "end_frame", "measure", "regions"}
REGION_FIELDS = {"x", "y", "w", "h", "qoffset"}


class ConfigurationError(Exception):
    """A schedule or command-line path is not suitable for this driver."""


class DriverError(Exception):
    """The live sender could not complete its work."""


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    qoffset: float

    def protocol_object(self) -> Dict[str, object]:
        return {
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "qoffset": self.qoffset,
        }


@dataclass(frozen=True)
class Phase:
    name: str
    start_frame: int
    end_frame: int
    regions: Tuple[Region, ...]


@dataclass(frozen=True)
class Schedule:
    name: Optional[str]
    width: int
    height: int
    fps: Fraction
    repeat_hz: Fraction
    target_bitrate_bps: int
    phases: Tuple[Phase, ...]
    phase_starts: Tuple[int, ...]

    @property
    def total_frames(self) -> int:
        return self.phases[-1].end_frame


@dataclass(frozen=True)
class ProgressRecord:
    status: str
    out_time_us: Optional[int]


class StopState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.signal_number = None  # type: Optional[int]

    def request(self, signal_number: int) -> None:
        self.signal_number = signal_number
        self.event.set()

    @property
    def requested(self) -> bool:
        return self.event.is_set()

    def wait(self, seconds: float) -> bool:
        return self.event.wait(seconds)


def _reject_json_constant(value: str) -> None:
    raise ConfigurationError("non-finite JSON number {!r} is not allowed".format(value))


def _unique_json_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result = {}  # type: Dict[str, object]
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate JSON field {!r}".format(key))
        result[key] = value
    return result


def _require_object(value: object, location: str) -> Dict[str, object]:
    if type(value) is not dict:
        raise ConfigurationError("{} must be a JSON object".format(location))
    return value  # type: ignore[return-value]


def _check_fields(
    value: Dict[str, object],
    location: str,
    allowed: set,
    required: set,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ConfigurationError(
            "{} has unknown field(s): {}".format(location, ", ".join(unknown))
        )
    if missing:
        raise ConfigurationError(
            "{} is missing required field(s): {}".format(location, ", ".join(missing))
        )


def _require_int(
    value: object,
    location: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if type(value) is not int:
        raise ConfigurationError("{} must be a JSON integer".format(location))
    result = value
    if minimum is not None and result < minimum:
        raise ConfigurationError("{} must be >= {}".format(location, minimum))
    if maximum is not None and result > maximum:
        raise ConfigurationError("{} must be <= {}".format(location, maximum))
    return result


def _require_fraction(
    value: object,
    location: str,
    minimum: Optional[Fraction] = None,
    maximum: Optional[Fraction] = None,
) -> Fraction:
    if type(value) not in (int, float):
        raise ConfigurationError("{} must be a JSON number".format(location))
    if type(value) is float and not math.isfinite(value):
        raise ConfigurationError("{} must be finite".format(location))
    try:
        result = Fraction(value if type(value) is int else str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ConfigurationError("{} is not a finite number: {}".format(location, exc))
    if minimum is not None and result < minimum:
        raise ConfigurationError("{} must be >= {}".format(location, float(minimum)))
    if maximum is not None and result > maximum:
        raise ConfigurationError("{} must be <= {}".format(location, float(maximum)))
    return result


def _require_nonempty_string(value: object, location: str) -> str:
    if type(value) is not str or not value.strip():
        raise ConfigurationError("{} must be a non-empty JSON string".format(location))
    return value


def _regions_overlap(first: Region, second: Region) -> bool:
    return (
        first.x < second.x + second.w
        and first.x + first.w > second.x
        and first.y < second.y + second.h
        and first.y + first.h > second.y
    )


def canonical_payload(regions: Sequence[Region], force_empty: bool = False) -> bytes:
    """Return the exact, stable byte representation sent to RKMPP."""

    selected = [] if force_empty else [region.protocol_object() for region in regions]
    payload = json.dumps(
        {"version": 1, "regions": selected},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ConfigurationError(
            "canonical ROI payload is {} bytes; maximum is {}".format(
                len(payload), MAX_PAYLOAD_BYTES
            )
        )
    return payload


def parse_schedule_object(root_value: object) -> Schedule:
    root = _require_object(root_value, "schedule")
    _check_fields(
        root,
        "schedule",
        TOP_LEVEL_FIELDS,
        {
            "schema_version",
            "frame",
            "repeat_hz",
            "target_bitrate_bps",
            "phases",
        },
    )

    schema_version = _require_int(root["schema_version"], "schema_version")
    if schema_version != 1:
        raise ConfigurationError(
            "schema_version must be 1, got {}".format(schema_version)
        )

    schedule_name = None
    if "name" in root:
        schedule_name = _require_nonempty_string(root["name"], "name")
    if "gop_frames" in root:
        _require_int(root["gop_frames"], "gop_frames", 1, MAX_FRAME_NUMBER)

    frame = _require_object(root["frame"], "frame")
    _check_fields(frame, "frame", FRAME_FIELDS, FRAME_FIELDS)
    width = _require_int(frame["width"], "frame.width", 1, MAX_FRAME_DIMENSION)
    height = _require_int(
        frame["height"], "frame.height", 1, MAX_FRAME_DIMENSION
    )
    fps = _require_fraction(frame["fps"], "frame.fps", Fraction(1, 1000000))
    repeat_hz = _require_fraction(
        root["repeat_hz"], "repeat_hz", Fraction(1, 1000000)
    )
    target_bitrate_bps = _require_int(
        root["target_bitrate_bps"],
        "target_bitrate_bps",
        1,
        MAX_TARGET_BITRATE_BPS,
    )

    phases_value = root["phases"]
    if type(phases_value) is not list or not phases_value:
        raise ConfigurationError("phases must be a non-empty JSON array")

    phases = []  # type: List[Phase]
    phase_names = set()
    expected_start = 0
    for phase_index, phase_value in enumerate(phases_value):
        location = "phases[{}]".format(phase_index)
        phase_object = _require_object(phase_value, location)
        _check_fields(
            phase_object,
            location,
            PHASE_FIELDS,
            {"name", "start_frame", "end_frame", "regions"},
        )

        phase_name = _require_nonempty_string(
            phase_object["name"], "{}.name".format(location)
        )
        if phase_name in phase_names:
            raise ConfigurationError("duplicate phase name {!r}".format(phase_name))
        phase_names.add(phase_name)

        start_frame = _require_int(
            phase_object["start_frame"],
            "{}.start_frame".format(location),
            0,
            MAX_FRAME_NUMBER,
        )
        end_frame = _require_int(
            phase_object["end_frame"],
            "{}.end_frame".format(location),
            1,
            MAX_FRAME_NUMBER,
        )
        if start_frame != expected_start:
            raise ConfigurationError(
                "{}.start_frame must be {} so phases are contiguous; got {}".format(
                    location, expected_start, start_frame
                )
            )
        if end_frame <= start_frame:
            raise ConfigurationError(
                "{}.end_frame must be greater than start_frame for [start, end)"
                .format(location)
            )

        if "measure" in phase_object:
            _require_object(
                phase_object["measure"], "{}.measure".format(location)
            )

        regions_value = phase_object["regions"]
        if type(regions_value) is not list:
            raise ConfigurationError("{}.regions must be a JSON array".format(location))
        if len(regions_value) > MAX_REGIONS:
            raise ConfigurationError(
                "{}.regions has {} entries; maximum is {}".format(
                    location, len(regions_value), MAX_REGIONS
                )
            )

        regions = []  # type: List[Region]
        for region_index, region_value in enumerate(regions_value):
            region_location = "{}.regions[{}]".format(location, region_index)
            region_object = _require_object(region_value, region_location)
            _check_fields(
                region_object,
                region_location,
                REGION_FIELDS,
                REGION_FIELDS,
            )
            x = _require_int(
                region_object["x"], "{}.x".format(region_location), 0
            )
            y = _require_int(
                region_object["y"], "{}.y".format(region_location), 0
            )
            w = _require_int(
                region_object["w"], "{}.w".format(region_location), 1
            )
            h = _require_int(
                region_object["h"], "{}.h".format(region_location), 1
            )
            qoffset_fraction = _require_fraction(
                region_object["qoffset"],
                "{}.qoffset".format(region_location),
                Fraction(-1, 1),
                Fraction(1, 1),
            )
            if x + w > width or y + h > height:
                raise ConfigurationError(
                    "{} rectangle ({},{},{},{}) exceeds {}x{} frame".format(
                        region_location, x, y, w, h, width, height
                    )
                )
            qoffset = float(qoffset_fraction)
            if qoffset == 0.0:
                qoffset = 0.0  # Canonicalize negative zero.
            region = Region(x=x, y=y, w=w, h=h, qoffset=qoffset)
            for previous_index, previous in enumerate(regions):
                if _regions_overlap(previous, region):
                    raise ConfigurationError(
                        "{} overlaps {}.regions[{}]; encoder updates must be atomic "
                        "and non-overlapping".format(
                            region_location, location, previous_index
                        )
                    )
            regions.append(region)

        canonical_payload(regions)
        phases.append(
            Phase(
                name=phase_name,
                start_frame=start_frame,
                end_frame=end_frame,
                regions=tuple(regions),
            )
        )
        expected_start = end_frame

    return Schedule(
        name=schedule_name,
        width=width,
        height=height,
        fps=fps,
        repeat_hz=repeat_hz,
        target_bitrate_bps=target_bitrate_bps,
        phases=tuple(phases),
        phase_starts=tuple(phase.start_frame for phase in phases),
    )


def load_schedule(path: str) -> Schedule:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read schedule {!r}: {}".format(path, exc))
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except ConfigurationError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "invalid schedule JSON at line {}, column {}: {}".format(
                exc.lineno, exc.colno, exc.msg
            )
        )
    return parse_schedule_object(value)


def phase_index_for_frame(schedule: Schedule, frame: int) -> Optional[int]:
    """Find the phase containing ``frame`` under [start_frame, end_frame)."""

    if frame < 0 or frame >= schedule.total_frames:
        return None
    phase_index = bisect.bisect_right(schedule.phase_starts, frame) - 1
    if phase_index < 0:
        return None
    phase = schedule.phases[phase_index]
    if frame >= phase.end_frame:
        return None
    return phase_index


def frame_for_out_time_us(out_time_us: int, fps: Fraction) -> int:
    """Map a microsecond timestamp to the nearest zero-based source frame."""

    if out_time_us <= 0:
        return 0
    numerator = out_time_us * fps.numerator
    denominator = 1000000 * fps.denominator
    return (2 * numerator + denominator) // (2 * denominator)


def repeat_slot_for_out_time_us(out_time_us: int, repeat_hz: Fraction) -> int:
    media_time_us = max(0, out_time_us)
    return (media_time_us * repeat_hz.numerator) // (
        1000000 * repeat_hz.denominator
    )


def terminal_repeat_is_guarded(
    schedule: Schedule, frame: int, reason: str
) -> bool:
    guard_frames = math.ceil(
        TERMINAL_REPEAT_GUARD_SLOTS * schedule.fps / schedule.repeat_hz
    )
    return reason == "repeat" and frame >= schedule.total_frames - guard_frames


def _same_path(first: str, second: str) -> bool:
    return os.path.abspath(first) == os.path.abspath(second)


def validate_runtime_paths(args: argparse.Namespace) -> None:
    paths = {
        "schedule": args.schedule,
        "socket": args.socket_path,
        "progress": args.progress,
        "events": args.events,
    }
    for name, value in paths.items():
        if not value:
            raise ConfigurationError("--{} must not be empty".format(name))
        if "\x00" in value:
            raise ConfigurationError("--{} contains a NUL byte".format(name))
    names = list(paths)
    for first_index, first_name in enumerate(names):
        for second_name in names[first_index + 1 :]:
            if _same_path(paths[first_name], paths[second_name]):
                raise ConfigurationError(
                    "--{} and --{} must use different paths".format(
                        first_name, second_name
                    )
                )
    events_parent = os.path.dirname(os.path.abspath(args.events))
    if not os.path.isdir(events_parent):
        raise ConfigurationError(
            "events directory does not exist: {!r}".format(events_parent)
        )


def connect_roi_socket(
    path: str, stop: StopState, timeout: float, poll_interval: float
) -> Optional[socket.socket]:
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    last_error = None  # type: Optional[BaseException]
    try:
        while not stop.requested:
            try:
                path_stat = os.stat(path)
                if not stat.S_ISSOCK(path_stat.st_mode):
                    raise DriverError(
                        "ROI socket path exists but is not a socket: {!r}".format(path)
                    )
                client.connect(path)
                return client
            except FileNotFoundError as exc:
                last_error = exc
            except ConnectionRefusedError as exc:
                # The pathname can briefly outlive a receiver during startup.
                last_error = exc
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    last_error = exc
                else:
                    raise DriverError(
                        "cannot connect to ROI socket {!r}: {}".format(path, exc)
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = ": {}".format(last_error) if last_error else ""
                raise DriverError(
                    "timed out after {:.3f}s waiting for ROI socket {!r}{}".format(
                        timeout, path, detail
                    )
                )
            stop.wait(min(poll_interval, remaining))
        client.close()
        return None
    except Exception:
        client.close()
        raise


def open_progress_stream(
    path: str, stop: StopState, timeout: float, poll_interval: float
) -> Optional[int]:
    deadline = time.monotonic() + timeout
    while not stop.requested:
        try:
            path_stat = os.stat(path)
            if not (stat.S_ISFIFO(path_stat.st_mode) or stat.S_ISREG(path_stat.st_mode)):
                raise DriverError(
                    "progress path must be a FIFO or regular file: {!r}".format(path)
                )
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            opened_stat = os.fstat(fd)
            if not (
                stat.S_ISFIFO(opened_stat.st_mode) or stat.S_ISREG(opened_stat.st_mode)
            ):
                os.close(fd)
                raise DriverError(
                    "opened progress path is not a FIFO or regular file: {!r}".format(
                        path
                    )
                )
            return fd
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise DriverError(
                    "cannot open progress stream {!r}: {}".format(path, exc)
                )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError(
                "timed out after {:.3f}s waiting for progress stream {!r}".format(
                    timeout, path
                )
            )
        stop.wait(min(poll_interval, remaining))
    return None


def progress_lines(
    fd: int, stop: StopState, poll_interval: float
) -> Iterable[str]:
    pending = bytearray()
    is_fifo = stat.S_ISFIFO(os.fstat(fd).st_mode)
    fifo_poller = None
    if is_fifo:
        fifo_poller = select.poll()
        fifo_poller.register(
            fd, select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL
        )
    while not stop.requested:
        fifo_flags = 0
        if fifo_poller is not None:
            events = fifo_poller.poll(max(1, math.ceil(poll_interval * 1000)))
            if not events:
                continue
            fifo_flags = events[0][1]
            if fifo_flags & (select.POLLERR | select.POLLNVAL):
                raise DriverError(
                    "FFmpeg progress FIFO reported poll error 0x{:x}".format(
                        fifo_flags
                    )
                )
        try:
            chunk = os.read(fd, READ_CHUNK_BYTES)
        except BlockingIOError:
            if fifo_flags & select.POLLHUP:
                raise DriverError(
                    "FFmpeg closed the progress FIFO before progress=end"
                )
            stop.wait(poll_interval)
            continue
        except InterruptedError:
            continue
        except OSError as exc:
            raise DriverError("cannot read FFmpeg progress stream: {}".format(exc))

        if not chunk:
            if is_fifo:
                if pending:
                    raise DriverError(
                        "FFmpeg closed the progress FIFO with an incomplete line"
                    )
                raise DriverError(
                    "FFmpeg closed the progress FIFO before progress=end"
                )
            stop.wait(poll_interval)
            continue
        pending.extend(chunk)
        if len(pending) > MAX_PROGRESS_LINE_BYTES and b"\n" not in pending:
            raise DriverError(
                "FFmpeg progress line exceeds {} bytes".format(
                    MAX_PROGRESS_LINE_BYTES
                )
            )

        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(pending[:newline])
            del pending[: newline + 1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if len(raw_line) > MAX_PROGRESS_LINE_BYTES:
                raise DriverError(
                    "FFmpeg progress line exceeds {} bytes".format(
                        MAX_PROGRESS_LINE_BYTES
                    )
                )
            try:
                yield raw_line.decode("ascii")
            except UnicodeDecodeError as exc:
                raise DriverError(
                    "FFmpeg progress is not ASCII at byte {}".format(exc.start)
                )


_PROGRESS_INTEGER = re.compile(r"-?[0-9]+\Z")


def progress_records(lines: Iterable[str]) -> Iterable[ProgressRecord]:
    out_time_us = None  # type: Optional[int]
    out_time_seen = False
    for line in lines:
        if not line:
            continue
        if "=" not in line:
            raise DriverError("invalid FFmpeg progress line {!r}".format(line))
        key, value = line.split("=", 1)
        if not key:
            raise DriverError("FFmpeg progress key must not be empty")

        if key == "out_time_us":
            if out_time_seen:
                raise DriverError("duplicate out_time_us in one FFmpeg progress record")
            out_time_seen = True
            if value == "N/A":
                out_time_us = None
            else:
                if not _PROGRESS_INTEGER.fullmatch(value):
                    raise DriverError(
                        "invalid FFmpeg out_time_us value {!r}".format(value)
                    )
                out_time_us = int(value, 10)
                if not (-MAX_FRAME_NUMBER <= out_time_us <= MAX_FRAME_NUMBER):
                    raise DriverError("FFmpeg out_time_us is outside int64 range")
        elif key == "progress":
            if value not in ("continue", "end"):
                raise DriverError(
                    "invalid FFmpeg progress status {!r}".format(value)
                )
            yield ProgressRecord(status=value, out_time_us=out_time_us)
            out_time_us = None
            out_time_seen = False


class EventWriter:
    def __init__(self, path: str) -> None:
        try:
            self._stream = open(path, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise DriverError("cannot open events file {!r}: {}".format(path, exc))
        self._sequence = 0

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError as exc:
            raise DriverError("cannot close events file: {}".format(exc))

    def record_send(
        self,
        phase: Phase,
        phase_index: int,
        out_time_us: int,
        frame: int,
        reason: str,
        payload: bytes,
        force_empty: bool,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        digest = hashlib.sha256(payload).hexdigest()
        event = {
            "event": "roi_send",
            "sequence": self._sequence,
            "phase": phase.name,
            "phase_index": phase_index,
            "phase_start_frame": phase.start_frame,
            "phase_end_frame": phase.end_frame,
            "out_time_us": out_time_us,
            "frame": frame,
            "reason": reason,
            "region_count": 0 if force_empty else len(phase.regions),
            "payload_bytes": len(payload),
            "payload_sha256": digest,
            "status": status,
            "force_empty": force_empty,
        }
        if error is not None:
            event["error"] = error
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


def send_phase(
    client: socket.socket,
    writer: EventWriter,
    phase: Phase,
    phase_index: int,
    out_time_us: int,
    frame: int,
    reason: str,
    payload: bytes,
    force_empty: bool,
) -> None:
    try:
        sent = client.send(payload)
        if sent != len(payload):
            raise OSError(
                errno.EIO,
                "short Unix datagram send: {} of {} bytes".format(
                    sent, len(payload)
                ),
            )
    except OSError as exc:
        writer.record_send(
            phase,
            phase_index,
            out_time_us,
            frame,
            reason,
            payload,
            force_empty,
            "send_error",
            str(exc),
        )
        raise DriverError("cannot send ROI update: {}".format(exc))
    writer.record_send(
        phase,
        phase_index,
        out_time_us,
        frame,
        reason,
        payload,
        force_empty,
        "sent",
    )


def run_driver(args: argparse.Namespace, stop: StopState) -> int:
    validate_runtime_paths(args)
    schedule = load_schedule(args.schedule)
    payloads = tuple(
        canonical_payload(phase.regions, args.force_empty)
        for phase in schedule.phases
    )

    writer = EventWriter(args.events)
    client = None  # type: Optional[socket.socket]
    progress_fd = None  # type: Optional[int]
    try:
        # Open the nonblocking read end first.  FFmpeg opens a -progress FIFO
        # for writing before encoder initialization; waiting for roi_socket
        # first would therefore deadlock both processes during startup.
        progress_fd = open_progress_stream(
            args.progress, stop, args.wait_timeout, args.poll_interval
        )
        if progress_fd is None:
            return 0

        client = connect_roi_socket(
            args.socket_path, stop, args.wait_timeout, args.poll_interval
        )
        if client is None:
            return 0

        current_phase_index = 0
        current_phase = schedule.phases[current_phase_index]
        send_phase(
            client,
            writer,
            current_phase,
            current_phase_index,
            0,
            0,
            "initial",
            payloads[current_phase_index],
            args.force_empty,
        )
        last_repeat_slot = 0
        previous_out_time_us = None  # type: Optional[int]

        records = progress_records(
            progress_lines(progress_fd, stop, args.poll_interval)
        )
        for record in records:
            if stop.requested:
                break
            # FFmpeg writes the final progress record after the encoder has
            # finished and may already have closed/unlinked its ROI socket.
            # Treat progress=end as a pure completion marker: its out_time_us
            # is evidence only and must not trigger one last repeat send.
            if record.status == "end":
                return 0
            out_time_us = record.out_time_us
            if out_time_us is not None:
                if (
                    previous_out_time_us is not None
                    and out_time_us < previous_out_time_us
                ):
                    raise DriverError(
                        "FFmpeg out_time_us moved backwards from {} to {}".format(
                            previous_out_time_us, out_time_us
                        )
                    )
                previous_out_time_us = out_time_us
                frame = frame_for_out_time_us(out_time_us, schedule.fps)
                phase_index = phase_index_for_frame(schedule, frame)
                if phase_index is None:
                    raise DriverError(
                        "schedule ends at frame {}, but FFmpeg progress reached "
                        "frame {} (out_time_us={}) before progress=end".format(
                            schedule.total_frames, frame, out_time_us
                        )
                    )
                else:
                    repeat_slot = repeat_slot_for_out_time_us(
                        out_time_us, schedule.repeat_hz
                    )
                    if phase_index != current_phase_index:
                        current_phase_index = phase_index
                        current_phase = schedule.phases[phase_index]
                        send_phase(
                            client,
                            writer,
                            current_phase,
                            current_phase_index,
                            out_time_us,
                            frame,
                            "phase_change",
                            payloads[current_phase_index],
                            args.force_empty,
                        )
                        last_repeat_slot = repeat_slot
                    elif repeat_slot > last_repeat_slot:
                        last_repeat_slot = repeat_slot
                        if terminal_repeat_is_guarded(schedule, frame, "repeat"):
                            continue
                        send_phase(
                            client,
                            writer,
                            current_phase,
                            current_phase_index,
                            out_time_us,
                            frame,
                            "repeat",
                            payloads[current_phase_index],
                            args.force_empty,
                        )
        return 0
    finally:
        if progress_fd is not None:
            os.close(progress_fd)
        if client is not None:
            client.close()
        writer.close()


def _valid_test_schedule() -> Dict[str, object]:
    return {
        "schema_version": 1,
        "name": "self-test",
        "frame": {"width": 1920, "height": 1080, "fps": 30},
        "gop_frames": 60,
        "repeat_hz": 10,
        "target_bitrate_bps": 4_000_000,
        "probes": [],
        "thresholds": {},
        "phases": [
            {
                "name": "clear",
                "start_frame": 0,
                "end_frame": 30,
                "regions": [],
            },
            {
                "name": "target",
                "start_frame": 30,
                "end_frame": 90,
                "measure": {"start_frame": 60, "end_frame": 90},
                "regions": [
                    {"x": 100, "y": 100, "w": 320, "h": 240, "qoffset": -0.25}
                ],
            },
        ],
    }


class FakeYoloSelfTest(unittest.TestCase):
    def _run_driver_with_client(
        self,
        schedule_value: Dict[str, object],
        progress_text: str,
        client: object,
    ) -> Tuple[int, List[Dict[str, object]]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedule_path = root / "schedule.json"
            progress_path = root / "progress.txt"
            events_path = root / "events.jsonl"
            socket_path = root / "encoder.roi"
            schedule_path.write_text(json.dumps(schedule_value), encoding="utf-8")
            progress_path.write_text(progress_text, encoding="ascii")
            with mock.patch.object(
                sys.modules[__name__], "connect_roi_socket", return_value=client
            ):
                args = argparse.Namespace(
                    schedule=str(schedule_path),
                    socket_path=str(socket_path),
                    progress=str(progress_path),
                    events=str(events_path),
                    force_empty=False,
                    wait_timeout=0.5,
                    poll_interval=0.001,
                )
                result = run_driver(args, StopState())
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            return result, events

    def test_schedule_validation_and_lookup(self) -> None:
        schedule = parse_schedule_object(_valid_test_schedule())
        self.assertEqual(schedule.fps, Fraction(30, 1))
        self.assertEqual(schedule.target_bitrate_bps, 4_000_000)
        self.assertEqual(phase_index_for_frame(schedule, 0), 0)
        self.assertEqual(phase_index_for_frame(schedule, 29), 0)
        self.assertEqual(phase_index_for_frame(schedule, 30), 1)
        self.assertEqual(phase_index_for_frame(schedule, 89), 1)
        self.assertIsNone(phase_index_for_frame(schedule, 90))
        self.assertIsNone(phase_index_for_frame(schedule, -1))

    def test_schedule_rejects_gap(self) -> None:
        value = _valid_test_schedule()
        value["phases"][1]["start_frame"] = 31  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "contiguous"):
            parse_schedule_object(value)

    def test_schedule_rejects_overlap_and_out_of_frame(self) -> None:
        value = _valid_test_schedule()
        regions = value["phases"][1]["regions"]  # type: ignore[index]
        regions.append({"x": 200, "y": 200, "w": 100, "h": 100, "qoffset": -0.1})
        with self.assertRaisesRegex(ConfigurationError, "overlaps"):
            parse_schedule_object(value)

        value = _valid_test_schedule()
        value["phases"][1]["regions"][0]["x"] = 1800  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "exceeds"):
            parse_schedule_object(value)

    def test_schedule_rejects_unknown_and_bad_qoffset(self) -> None:
        value = _valid_test_schedule()
        value["unexpected"] = True
        with self.assertRaisesRegex(ConfigurationError, "unknown field"):
            parse_schedule_object(value)

        value = _valid_test_schedule()
        value["phases"][1]["regions"][0]["qoffset"] = -1.1  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "must be >="):
            parse_schedule_object(value)

    def test_schedule_rejects_invalid_target_bitrate(self) -> None:
        value = _valid_test_schedule()
        del value["target_bitrate_bps"]
        with self.assertRaisesRegex(ConfigurationError, "missing required field"):
            parse_schedule_object(value)

        value = _valid_test_schedule()
        value["target_bitrate_bps"] = MAX_TARGET_BITRATE_BPS + 1
        with self.assertRaisesRegex(ConfigurationError, "must be <="):
            parse_schedule_object(value)

    def test_payload_is_canonical_and_force_empty_is_controlled(self) -> None:
        first = Region(x=10, y=20, w=30, h=40, qoffset=-0.25)
        second = Region(x=100, y=200, w=50, h=60, qoffset=0.0)
        payload = canonical_payload((first, second))
        self.assertEqual(
            payload,
            b'{"regions":[{"h":40,"qoffset":-0.25,"w":30,"x":10,"y":20},'
            b'{"h":60,"qoffset":0.0,"w":50,"x":100,"y":200}],"version":1}',
        )
        self.assertEqual(
            canonical_payload((first, second), force_empty=True),
            b'{"regions":[],"version":1}',
        )

    def test_media_time_mapping_and_repeat_slots(self) -> None:
        self.assertEqual(frame_for_out_time_us(33333, Fraction(30, 1)), 1)
        self.assertEqual(frame_for_out_time_us(1000000, Fraction(30, 1)), 30)
        self.assertEqual(repeat_slot_for_out_time_us(99999, Fraction(10, 1)), 0)
        self.assertEqual(repeat_slot_for_out_time_us(100000, Fraction(10, 1)), 1)

    def test_terminal_repeat_guard_sends_336_and_suppresses_337_to_339(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.payloads = []  # type: List[bytes]

            def send(self, payload: bytes) -> int:
                self.payloads.append(payload)
                return len(payload)

            def close(self) -> None:
                pass

        value = _valid_test_schedule()
        value["phases"][0]["end_frame"] = 900  # type: ignore[index]
        value["phases"][1]["start_frame"] = 900  # type: ignore[index]
        value["phases"][1]["end_frame"] = 1020  # type: ignore[index]
        value["phases"][1]["measure"] = {  # type: ignore[index]
            "start_frame": 960,
            "end_frame": 1020,
        }
        client = RecordingClient()
        result, events = self._run_driver_with_client(
            value,
            "out_time_us=30000000\nprogress=continue\n"
            "out_time_us=33600000\nprogress=continue\n"
            "out_time_us=33700000\nprogress=continue\n"
            "out_time_us=33800000\nprogress=continue\n"
            "out_time_us=33900000\nprogress=continue\n"
            "out_time_us=33966667\nprogress=end\n",
            client,
        )
        self.assertEqual(result, 0)
        self.assertEqual([event["frame"] for event in events], [0, 900, 1008])
        self.assertEqual(
            [event["reason"] for event in events],
            ["initial", "phase_change", "repeat"],
        )
        schedule = parse_schedule_object(value)
        self.assertFalse(terminal_repeat_is_guarded(schedule, 1008, "repeat"))
        self.assertTrue(terminal_repeat_is_guarded(schedule, 1011, "repeat"))
        self.assertFalse(
            terminal_repeat_is_guarded(schedule, 1011, "phase_change")
        )

    def test_phase_change_inside_terminal_guard_is_still_sent(self) -> None:
        class RecordingClient:
            def send(self, payload: bytes) -> int:
                return len(payload)

            def close(self) -> None:
                pass

        value = _valid_test_schedule()
        value["phases"][0]["end_frame"] = 1011  # type: ignore[index]
        value["phases"][1]["start_frame"] = 1011  # type: ignore[index]
        value["phases"][1]["end_frame"] = 1020  # type: ignore[index]
        value["phases"][1]["measure"] = {  # type: ignore[index]
            "start_frame": 1011,
            "end_frame": 1020,
        }
        result, events = self._run_driver_with_client(
            value,
            "out_time_us=33700000\nprogress=continue\n"
            "out_time_us=33800000\nprogress=continue\n"
            "out_time_us=33966667\nprogress=end\n",
            RecordingClient(),
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            [(event["frame"], event["reason"]) for event in events],
            [(0, "initial"), (1011, "phase_change")],
        )

    def test_send_error_before_terminal_guard_remains_fatal(self) -> None:
        class FailingClient:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, payload: bytes) -> int:
                self.calls += 1
                if self.calls == 3:
                    raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")
                return len(payload)

            def close(self) -> None:
                pass

        value = _valid_test_schedule()
        value["phases"][0]["end_frame"] = 900  # type: ignore[index]
        value["phases"][1]["start_frame"] = 900  # type: ignore[index]
        value["phases"][1]["end_frame"] = 1020  # type: ignore[index]
        value["phases"][1]["measure"] = {  # type: ignore[index]
            "start_frame": 960,
            "end_frame": 1020,
        }
        with self.assertRaisesRegex(DriverError, "cannot send ROI update"):
            self._run_driver_with_client(
                value,
                "out_time_us=30000000\nprogress=continue\n"
                "out_time_us=33600000\nprogress=continue\n"
                "out_time_us=33966667\nprogress=end\n",
                FailingClient(),
            )

    def test_progress_end_record_from_nonblocking_fifo(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(read_fd, False)
            os.write(write_fd, b"out_time_us=100000\nprogress=end\n")
            os.close(write_fd)
            write_fd = -1
            records = progress_records(
                progress_lines(read_fd, StopState(), 0.001)
            )
            self.assertEqual(
                next(records), ProgressRecord(status="end", out_time_us=100000)
            )
            records.close()  # type: ignore[attr-defined]
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)

    def test_fifo_close_without_progress_end_is_an_error(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(read_fd, False)
            os.write(write_fd, b"out_time_us=0\nprogress=continue\n")
            os.close(write_fd)
            write_fd = -1
            with self.assertRaisesRegex(DriverError, "before progress=end"):
                list(
                    progress_records(
                        progress_lines(read_fd, StopState(), 0.001)
                    )
                )
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)

    def test_progress_end_does_not_trigger_a_final_send(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.payloads = []  # type: List[bytes]

            def send(self, payload: bytes) -> int:
                self.payloads.append(payload)
                return len(payload)

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedule_path = root / "schedule.json"
            progress_path = root / "progress.txt"
            events_path = root / "events.jsonl"
            socket_path = root / "encoder.roi"
            schedule_path.write_text(
                json.dumps(_valid_test_schedule()), encoding="utf-8"
            )
            progress_path.write_text(
                "out_time_us=966667\nprogress=end\n", encoding="ascii"
            )

            client = RecordingClient()
            with mock.patch.object(
                sys.modules[__name__], "connect_roi_socket", return_value=client
            ):
                args = argparse.Namespace(
                    schedule=str(schedule_path),
                    socket_path=str(socket_path),
                    progress=str(progress_path),
                    events=str(events_path),
                    force_empty=False,
                    wait_timeout=0.5,
                    poll_interval=0.001,
                )
                self.assertEqual(run_driver(args, StopState()), 0)

            first_phase = parse_schedule_object(_valid_test_schedule()).phases[0]
            self.assertEqual(
                client.payloads,
                [canonical_payload(first_phase.regions)],
            )

            events = events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0])["reason"], "initial")


def run_self_test() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FakeYoloSelfTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Send scheduled, complete ROI sets to an RKMPP encoder using "
            "FFmpeg -progress media time."
        )
    )
    parser.add_argument("--schedule", help="schedule JSON path")
    parser.add_argument(
        "--socket",
        "--roi-socket",
        dest="socket_path",
        help="filesystem AF_UNIX/SOCK_DGRAM ROI socket path",
    )
    parser.add_argument(
        "--progress", help="FFmpeg -progress FIFO or regular file path"
    )
    parser.add_argument("--events", help="JSONL send-event output path")
    parser.add_argument(
        "--wait-timeout",
        type=positive_float,
        default=30.0,
        help="startup wait timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive_float,
        default=0.02,
        help="path and stream poll interval in seconds (default: 0.02)",
    )
    parser.add_argument(
        "--force-empty",
        action="store_true",
        help="preserve timing but send an empty ROI set in every datagram",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run schedule, lookup, and payload unit tests, then exit",
    )
    parser.add_argument(
        "--validate-schedule",
        metavar="PATH",
        help="validate one schedule with the runtime parser, then exit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.self_test:
        if arguments != ["--self-test"]:
            parser.error("--self-test cannot be combined with runtime arguments")
        return run_self_test()
    if args.validate_schedule is not None:
        if arguments != ["--validate-schedule", args.validate_schedule]:
            parser.error("--validate-schedule cannot be combined with other arguments")
        try:
            schedule = load_schedule(args.validate_schedule)
        except ConfigurationError as exc:
            print("fake-yolo: configuration error: {}".format(exc), file=sys.stderr)
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
        option
        for option, value in (
            ("--schedule", args.schedule),
            ("--socket", args.socket_path),
            ("--progress", args.progress),
            ("--events", args.events),
        )
        if value is None
    ]
    if missing:
        parser.error("the following arguments are required: {}".format(", ".join(missing)))
    if args.poll_interval > args.wait_timeout:
        parser.error("--poll-interval must not exceed --wait-timeout")

    stop = StopState()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda number, _frame: stop.request(number))
    try:
        return run_driver(args, stop)
    except ConfigurationError as exc:
        print("fake-yolo: configuration error: {}".format(exc), file=sys.stderr)
        return 2
    except DriverError as exc:
        print("fake-yolo: error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
