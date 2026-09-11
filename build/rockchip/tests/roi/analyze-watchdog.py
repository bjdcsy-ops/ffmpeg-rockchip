#!/usr/bin/env python3
"""Analyze the RK3588 moving-ROI watchdog acceptance experiment.

This analyzer intentionally scores fixed probe intersections.  The moving ROI
boxes always cover those intersections, including the asynchronous RKMPP queue
lag, so the result demonstrates spatial behavior without claiming frame-exact
ROI scheduling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Sequence


MAX_REGIONS = 8
MAX_PAYLOAD_BYTES = 4096
MAX_PROGRESS_LINE_BYTES = 64 * 1024
MAX_INT64 = (1 << 63) - 1
PSNR_INFINITY_CAP_DB = 100.0
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
PROGRESS_INTEGER = re.compile(r"-?[0-9]+\Z")
PSNR_PATTERN = re.compile(r"(?:^|\s)n:(\d+).*?(?:^|\s)psnr_y:([^\s]+)")
SSIM_PATTERN = re.compile(r"(?:^|\s)n:(\d+).*?(?:^|\s)Y:([^\s]+)")
APPLIED_ROI_PATTERN = re.compile(
    r"Applied ROI socket update #(\d+) with (\d+) region\(s\)\."
)
FORBIDDEN_ROI_LOG_PATTERNS = (
    re.compile(r"Ignoring .*ROI socket update", re.IGNORECASE),
    re.compile(r"ROI socket update .*overlap", re.IGNORECASE),
    re.compile(r"ROI socket update budget exhausted", re.IGNORECASE),
    re.compile(r"Failed (?:to )?.*ROI", re.IGNORECASE),
    re.compile(r"ROI setup (?:failed|error)", re.IGNORECASE),
)

EXPECTED_PHASES = (
    ("warmup-clear", 0, 120),
    ("moving-a", 120, 300),
    ("watchdog-silent", 300, 540),
    ("moving-b", 540, 780),
    ("final-clear", 780, 900),
)

PRODUCER_SEND_FIELDS = {
    "event",
    "sequence",
    "send_sequence",
    "monotonic_ns",
    "phase",
    "phase_index",
    "phase_start_frame",
    "phase_end_frame",
    "frame",
    "out_time_us",
    "state",
    "reason",
    "update_frame",
    "region_count",
    "payload_bytes",
    "payload_sha256",
    "force_empty",
    "status",
}
PRODUCER_STATE_FIELDS = {
    "event",
    "sequence",
    "monotonic_ns",
    "phase",
    "phase_index",
    "phase_start_frame",
    "phase_end_frame",
    "frame",
    "out_time_us",
    "state",
    "reason",
    "update_frame",
    "region_count",
    "payload_bytes",
    "payload_sha256",
    "force_empty",
    "status",
}
BRIDGE_RX_FIELDS = {
    "event",
    "sequence",
    "rx_sequence",
    "monotonic_ns",
    "payload_bytes",
    "payload_sha256",
    "region_count",
    "status",
}
BRIDGE_FORWARD_FIELDS = {
    "event",
    "sequence",
    "forward_sequence",
    "rx_sequence",
    "monotonic_ns",
    "payload_bytes",
    "payload_sha256",
    "region_count",
    "reason",
    "status",
}
WATCHDOG_CLEAR_FIELDS = {
    "event",
    "sequence",
    "forward_sequence",
    "monotonic_ns",
    "payload_bytes",
    "payload_sha256",
    "region_count",
    "reason",
    "last_valid_rx_sequence",
    "last_valid_rx_monotonic_ns",
    "stale_age_ns",
    "timeout_ms",
    "status",
}


class AnalysisError(RuntimeError):
    """Evidence is missing, malformed, or fails an integrity invariant."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AnalysisError(f"non-finite JSON number {value!r}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, json.JSONDecodeError, AnalysisError) as exc:
        raise AnalysisError(f"cannot read JSON {path}: {exc}") from exc
    if type(value) is not dict:
        raise AnalysisError(f"expected a JSON object in {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AnalysisError(f"cannot read events {path}: {exc}") from exc
    if not lines:
        raise AnalysisError(f"event log is empty: {path}")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, AnalysisError) as exc:
            raise AnalysisError(f"invalid event at {path}:{line_number}: {exc}") from exc
        if type(value) is not dict:
            raise AnalysisError(f"event at {path}:{line_number} is not an object")
        result.append(value)
    return result


def parse_progress_lines(lines: Iterable[str], where: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    out_time_us: int | None = None
    saw_out_time = False
    saw_end = False
    for line_number, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        try:
            line_bytes = line.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AnalysisError(
                f"progress is not ASCII at {where}:{line_number}"
            ) from exc
        if len(line_bytes) > MAX_PROGRESS_LINE_BYTES:
            raise AnalysisError(f"progress line is too long at {where}:{line_number}")
        if not line:
            continue
        if saw_end:
            raise AnalysisError(f"progress data follows progress=end at {where}:{line_number}")
        if "=" not in line:
            raise AnalysisError(f"invalid progress line at {where}:{line_number}")
        key, value = line.split("=", 1)
        if key == "out_time_us":
            if saw_out_time:
                raise AnalysisError(
                    f"duplicate out_time_us in one progress record at {where}:{line_number}"
                )
            saw_out_time = True
            if value == "N/A":
                out_time_us = None
            elif PROGRESS_INTEGER.fullmatch(value):
                out_time_us = int(value, 10)
                if not -MAX_INT64 <= out_time_us <= MAX_INT64:
                    raise AnalysisError(
                        f"out_time_us is outside int64 range at {where}:{line_number}"
                    )
            else:
                raise AnalysisError(f"invalid out_time_us at {where}:{line_number}")
        elif key == "progress":
            if value not in ("continue", "end"):
                raise AnalysisError(f"invalid progress status at {where}:{line_number}")
            records.append({"status": value, "out_time_us": out_time_us})
            out_time_us = None
            saw_out_time = False
            saw_end = value == "end"
    if saw_out_time:
        raise AnalysisError(f"unterminated progress record in {where}")
    if not saw_end:
        raise AnalysisError(f"progress=end is missing from {where}")
    return records


def load_progress(path: Path) -> list[dict[str, Any]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AnalysisError(f"cannot read progress evidence {path}: {exc}") from exc
    lines: list[str] = []
    start = 0
    line_number = 1
    while True:
        newline = data.find(b"\n", start)
        if newline < 0:
            break
        raw = data[start:newline]
        start = newline + 1
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if len(raw) > MAX_PROGRESS_LINE_BYTES:
            raise AnalysisError(f"progress line is too long at {path}:{line_number}")
        try:
            lines.append(raw.decode("ascii"))
        except UnicodeDecodeError as exc:
            raise AnalysisError(
                f"progress is not ASCII at {path}:{line_number}"
            ) from exc
        line_number += 1
    pending = data[start:]
    if len(pending) > MAX_PROGRESS_LINE_BYTES:
        raise AnalysisError(f"progress line is too long at {path}:{line_number}")
    if pending:
        raise AnalysisError(f"unterminated progress line at {path}:{line_number}")
    return parse_progress_lines(lines, str(path))


def require_int(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AnalysisError(f"{name} must be an integer >= {minimum}")
    return value


def require_number(value: Any, name: str, minimum: float | None = None) -> float:
    if type(value) not in (int, float):
        raise AnalysisError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise AnalysisError(f"{name} must be finite{suffix}")
    return result


def require_fields(value: dict[str, Any], expected: set[str], where: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise AnalysisError(f"{where} fields mismatch; missing={missing}, unknown={unknown}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_payload(regions: Sequence[dict[str, Any]]) -> bytes:
    normalized: list[dict[str, int | float]] = []
    if len(regions) > MAX_REGIONS:
        raise AnalysisError("payload contains more than eight regions")
    for index, region in enumerate(regions):
        if type(region) is not dict or set(region) != {"x", "y", "w", "h", "qoffset"}:
            raise AnalysisError(f"region {index} must contain exactly x/y/w/h/qoffset")
        normalized.append(
            {
                "x": require_int(region["x"], f"regions[{index}].x"),
                "y": require_int(region["y"], f"regions[{index}].y"),
                "w": require_int(region["w"], f"regions[{index}].w", 1),
                "h": require_int(region["h"], f"regions[{index}].h", 1),
                "qoffset": require_number(region["qoffset"], f"regions[{index}].qoffset"),
            }
        )
    payload = json.dumps(
        {"version": 1, "regions": normalized},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise AnalysisError("canonical payload exceeds 4096 bytes")
    return payload


def regions_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        first["x"] < second["x"] + second["w"]
        and first["x"] + first["w"] > second["x"]
        and first["y"] < second["y"] + second["h"]
        and first["y"] + first["h"] > second["y"]
    )


def validate_regions(regions: Any, width: int, height: int, where: str) -> list[dict[str, Any]]:
    if type(regions) is not list:
        raise AnalysisError(f"{where} must be an array")
    canonical_payload(regions)
    for index, region in enumerate(regions):
        if (
            region["x"] + region["w"] > width
            or region["y"] + region["h"] > height
            or not -1.0 <= float(region["qoffset"]) <= 1.0
        ):
            raise AnalysisError(f"{where}[{index}] is outside the frame or qoffset range")
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            if regions_overlap(first, second):
                raise AnalysisError(f"{where} contains overlapping regions")
    return regions


def validate_probe(value: Any, name: str, width: int, height: int) -> dict[str, int]:
    if type(value) is not dict or set(value) != {"x", "y", "w", "h"}:
        raise AnalysisError(f"probe {name} must contain exactly x/y/w/h")
    probe = {
        key: require_int(value[key], f"probes.{name}.{key}", 1 if key in ("w", "h") else 0)
        for key in ("x", "y", "w", "h")
    }
    if probe["x"] + probe["w"] > width or probe["y"] + probe["h"] > height:
        raise AnalysisError(f"probe {name} is outside the frame")
    return probe


def validate_measure(
    phase: dict[str, Any], expected_start: int, expected_end: int, expected_role: str
) -> dict[str, Any]:
    measure = phase.get("measure")
    if type(measure) is not dict:
        raise AnalysisError(f"phase {phase.get('name')} needs a measure object")
    start = require_int(measure.get("start_frame"), "measure.start_frame")
    end = require_int(measure.get("end_frame"), "measure.end_frame", 1)
    if (start, end) != (expected_start, expected_end):
        raise AnalysisError(
            f"phase {phase.get('name')} measure must be [{expected_start},{expected_end})"
        )
    if expected_role == "clear":
        if set(measure) != {"start_frame", "end_frame", "clear_probes"}:
            raise AnalysisError(f"phase {phase.get('name')} has invalid clear measure fields")
        if measure["clear_probes"] != ["A", "N", "B"]:
            raise AnalysisError(f"phase {phase.get('name')} must score A/N/B in order")
    else:
        if set(measure) != {"start_frame", "end_frame", "boost_probe", "neutral_probe"}:
            raise AnalysisError(f"phase {phase.get('name')} has invalid active measure fields")
        if measure["boost_probe"] != expected_role or measure["neutral_probe"] != "N":
            raise AnalysisError(f"phase {phase.get('name')} has wrong active probes")
    return measure


def validate_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    expected_top = {
        "schema_version",
        "name",
        "frame",
        "gop_frames",
        "repeat_hz",
        "target_bitrate_bps",
        "watchdog",
        "probes",
        "thresholds",
        "phases",
    }
    require_fields(raw, expected_top, "schedule")
    if raw["schema_version"] != 1:
        raise AnalysisError("schedule schema_version must be 1")
    if type(raw["name"]) is not str or not SAFE_NAME.fullmatch(raw["name"]):
        raise AnalysisError("schedule name is invalid")
    frame = raw["frame"]
    if type(frame) is not dict or set(frame) != {"width", "height", "fps"}:
        raise AnalysisError("frame must contain exactly width/height/fps")
    width = require_int(frame["width"], "frame.width", 1)
    height = require_int(frame["height"], "frame.height", 1)
    fps = require_number(frame["fps"], "frame.fps", 0.001)
    if (width, height, fps) != (1920, 1080, 30.0):
        raise AnalysisError("watchdog acceptance is fixed at 1920x1080@30")
    if require_int(raw["gop_frames"], "gop_frames", 1) != 60:
        raise AnalysisError("gop_frames must be 60")
    repeat_hz = require_number(raw["repeat_hz"], "repeat_hz", 0.001)
    if repeat_hz != 10.0:
        raise AnalysisError("repeat_hz must be 10")
    target_bitrate = require_int(raw["target_bitrate_bps"], "target_bitrate_bps", 1)
    if target_bitrate != 4_000_000:
        raise AnalysisError("target_bitrate_bps must be 4000000")

    watchdog = raw["watchdog"]
    expected_watchdog = {
        "timeout_ms",
        "min_clear_delay_ms",
        "max_clear_delay_ms",
        "max_resume_forward_ms",
    }
    if type(watchdog) is not dict:
        raise AnalysisError("watchdog must be an object")
    require_fields(watchdog, expected_watchdog, "watchdog")
    timeout_ms = require_int(watchdog["timeout_ms"], "watchdog.timeout_ms", 1)
    min_delay = require_int(watchdog["min_clear_delay_ms"], "watchdog.min_clear_delay_ms", 1)
    max_delay = require_int(watchdog["max_clear_delay_ms"], "watchdog.max_clear_delay_ms", 1)
    resume_ms = require_int(watchdog["max_resume_forward_ms"], "watchdog.max_resume_forward_ms", 1)
    if timeout_ms != 400 or not 300 <= min_delay <= timeout_ms <= max_delay <= 500:
        raise AnalysisError("watchdog delay contract must stay within 300-500 ms around 400 ms")

    probes_raw = raw["probes"]
    if type(probes_raw) is not dict or set(probes_raw) != {"A", "N", "B"}:
        raise AnalysisError("probes must contain exactly A/N/B")
    probes = {
        name: validate_probe(probes_raw[name], name, width, height)
        for name in ("A", "N", "B")
    }

    threshold_fields = {
        "min_active_absolute_psnr_y_db",
        "min_active_vs_neutral_psnr_y_db",
        "min_active_ssim_y",
        "max_watchdog_spatial_residual_psnr_y_db",
        "max_watchdog_spatial_residual_fraction",
        "max_final_clear_spatial_residual_psnr_y_db",
        "max_final_clear_spatial_residual_fraction",
        "max_cbr_size_delta_ratio",
        "max_cbr_target_delta_ratio",
    }
    thresholds_raw = raw["thresholds"]
    if type(thresholds_raw) is not dict:
        raise AnalysisError("thresholds must be an object")
    require_fields(thresholds_raw, threshold_fields, "thresholds")
    thresholds = {
        key: require_number(value, f"thresholds.{key}", 0.0)
        for key, value in thresholds_raw.items()
    }
    expected_thresholds = {
        "min_active_absolute_psnr_y_db": 0.25,
        "min_active_vs_neutral_psnr_y_db": 0.5,
        "min_active_ssim_y": 0.0005,
        "max_watchdog_spatial_residual_psnr_y_db": 0.5,
        "max_watchdog_spatial_residual_fraction": 0.20,
        "max_final_clear_spatial_residual_psnr_y_db": 0.5,
        "max_final_clear_spatial_residual_fraction": 0.20,
        "max_cbr_size_delta_ratio": 0.10,
        "max_cbr_target_delta_ratio": 0.10,
    }
    if thresholds != expected_thresholds:
        raise AnalysisError("watchdog acceptance thresholds differ from the frozen contract")

    phases_raw = raw["phases"]
    if type(phases_raw) is not list or len(phases_raw) != len(EXPECTED_PHASES):
        raise AnalysisError("schedule must contain the five frozen watchdog phases")
    phases: list[dict[str, Any]] = []
    measure_contract = (
        (60, 120, "clear"),
        (180, 300, "A"),
        (390, 540, "clear"),
        (600, 780, "B"),
        (840, 900, "clear"),
    )
    for index, (phase_raw, expected, measure_expected) in enumerate(
        zip(phases_raw, EXPECTED_PHASES, measure_contract)
    ):
        if type(phase_raw) is not dict:
            raise AnalysisError(f"phase {index} is not an object")
        name, start, end = expected
        if (
            phase_raw.get("name") != name
            or phase_raw.get("start_frame") != start
            or phase_raw.get("end_frame") != end
        ):
            raise AnalysisError(f"phase {index} must be {name} [{start},{end})")
        measure = validate_measure(phase_raw, *measure_expected)
        silent = phase_raw.get("silent") is True
        if name == "watchdog-silent":
            require_fields(
                phase_raw,
                {"name", "start_frame", "end_frame", "silent", "measure"},
                f"phase {name}",
            )
            updates: list[dict[str, Any]] = []
        else:
            require_fields(
                phase_raw,
                {"name", "start_frame", "end_frame", "updates", "measure"},
                f"phase {name}",
            )
            if silent:
                raise AnalysisError(f"phase {name} cannot be silent")
            updates_value = phase_raw["updates"]
            if type(updates_value) is not list or not updates_value:
                raise AnalysisError(f"phase {name} must contain updates")
            updates = []
            previous_frame = -1
            for update_index, update in enumerate(updates_value):
                if type(update) is not dict or set(update) != {"frame", "regions"}:
                    raise AnalysisError(f"phase {name} update {update_index} is invalid")
                update_frame = require_int(update["frame"], f"phase {name} update frame")
                if not start <= update_frame < end or update_frame <= previous_frame:
                    raise AnalysisError(f"phase {name} update frames are not increasing in range")
                if update_index == 0 and update_frame != start:
                    raise AnalysisError(f"phase {name} first update must equal phase start")
                regions = validate_regions(
                    update["regions"], width, height, f"phase {name} update {update_index}"
                )
                updates.append({"frame": update_frame, "regions": regions})
                previous_frame = update_frame
            if name in ("moving-a", "moving-b"):
                expected_frames = list(range(start, end, 12))
                if [update["frame"] for update in updates] != expected_frames:
                    raise AnalysisError(f"phase {name} updates must occur every 12 frames")
                if any(len(update["regions"]) != 1 for update in updates):
                    raise AnalysisError(f"phase {name} must carry exactly one moving ROI")
                target_probe = probes["A" if name == "moving-a" else "B"]
                for update in updates:
                    region = update["regions"][0]
                    if (
                        region["w"] != 512
                        or region["h"] != 512
                        or float(region["qoffset"]) != -0.25
                    ):
                        raise AnalysisError(f"phase {name} must use one 512x512 ROI at -0.25")
                    if not (
                        region["x"] <= target_probe["x"]
                        and region["y"] <= target_probe["y"]
                        and region["x"] + region["w"]
                        >= target_probe["x"] + target_probe["w"]
                        and region["y"] + region["h"]
                        >= target_probe["y"] + target_probe["h"]
                    ):
                        raise AnalysisError(
                            f"phase {name} moving ROI does not cover its fixed intersection probe"
                        )
                positions = {
                    (
                        update["regions"][0]["x"],
                        update["regions"][0]["y"],
                    )
                    for update in updates
                }
                if len(positions) < 2:
                    raise AnalysisError(f"phase {name} needs at least two ROI coordinates")
            else:
                if len(updates) != 1 or updates[0]["regions"]:
                    raise AnalysisError(f"phase {name} must contain one empty start update")
        phases.append(
            {
                "index": index,
                "name": name,
                "start_frame": start,
                "end_frame": end,
                "silent": silent,
                "updates": updates,
                "measure": measure,
            }
        )

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "repeat_hz": repeat_hz,
        "repeat_frames": int(round(fps / repeat_hz)),
        "gop_frames": 60,
        "expected_frames": 900,
        "target_bitrate_bps": target_bitrate,
        "watchdog": {
            "timeout_ms": timeout_ms,
            "min_clear_delay_ms": min_delay,
            "max_clear_delay_ms": max_delay,
            "max_resume_forward_ms": resume_ms,
        },
        "probes": probes,
        "thresholds": thresholds,
        "phases": phases,
    }


def latest_update(phase: dict[str, Any], frame: int) -> dict[str, Any]:
    candidates = [update for update in phase["updates"] if update["frame"] <= frame]
    if not candidates:
        raise AnalysisError(f"phase {phase['name']} has no update at frame {frame}")
    return candidates[-1]


def phase_index_for_frame(schedule: dict[str, Any], frame: int) -> int | None:
    for phase in schedule["phases"]:
        if phase["start_frame"] <= frame < phase["end_frame"]:
            return phase["index"]
    return None


def update_index_for_frame(phase: dict[str, Any], frame: int) -> int | None:
    if phase["silent"]:
        return None
    result = None
    for index, update in enumerate(phase["updates"]):
        if update["frame"] > frame:
            break
        result = index
    return result


def _replayed_send(
    phase: dict[str, Any],
    frame: int,
    out_time_us: int,
    reason: str,
    update: dict[str, Any],
    force_empty: bool,
    sequence: int,
    send_sequence: int,
) -> dict[str, Any]:
    regions = [] if force_empty else update["regions"]
    payload = canonical_payload(regions)
    return {
        "event": "producer_send",
        "sequence": sequence,
        "send_sequence": send_sequence,
        "phase": phase["name"],
        "phase_index": phase["index"],
        "phase_start_frame": phase["start_frame"],
        "phase_end_frame": phase["end_frame"],
        "frame": frame,
        "out_time_us": out_time_us,
        "state": "empty" if not regions else "active",
        "reason": reason,
        "update_frame": update["frame"],
        "region_count": len(regions),
        "payload_bytes": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "force_empty": force_empty,
        "status": "sent",
    }


def replay_producer_progress(
    schedule: dict[str, Any],
    progress_records: list[dict[str, Any]],
    force_empty: bool,
) -> list[dict[str, Any]]:
    phases = schedule["phases"]
    phase_index = 0
    phase = phases[phase_index]
    if phase["silent"]:
        raise AnalysisError("first producer phase must not be silent")
    update_index = 0
    update = phase["updates"][update_index]
    events = [
        _replayed_send(phase, 0, 0, "initial", update, force_empty, 0, 0)
    ]
    sequence = 1
    send_sequence = 1
    last_repeat_slot = 0
    previous_out_time_us: int | None = None
    saw_end = False

    for record_index, record in enumerate(progress_records):
        if type(record) is not dict or set(record) != {"status", "out_time_us"}:
            raise AnalysisError(f"progress record {record_index} has invalid fields")
        status = record["status"]
        out_time_us = record["out_time_us"]
        if status not in ("continue", "end"):
            raise AnalysisError(f"progress record {record_index} has invalid status")
        if out_time_us is not None and type(out_time_us) is not int:
            raise AnalysisError(f"progress record {record_index} has invalid out_time_us")
        if out_time_us is not None and not -MAX_INT64 <= out_time_us <= MAX_INT64:
            raise AnalysisError(f"progress record {record_index} out_time_us is outside int64 range")
        if saw_end:
            raise AnalysisError("progress record follows progress=end")
        if status == "end":
            saw_end = True
            continue
        if out_time_us is None:
            continue
        if previous_out_time_us is not None and out_time_us < previous_out_time_us:
            raise AnalysisError("progress out_time_us moved backwards")
        previous_out_time_us = out_time_us
        repeat_hz = int(schedule["repeat_hz"])
        fps = int(schedule["fps"])
        repeat_slot = max(0, out_time_us) * repeat_hz // 1_000_000
        for heartbeat_slot in range(last_repeat_slot + 1, repeat_slot + 1):
            frame = heartbeat_slot * fps // repeat_hz
            new_phase_index = phase_index_for_frame(schedule, frame)
            if new_phase_index is None:
                raise AnalysisError(
                    f"progress reached heartbeat frame {frame} outside schedule before progress=end"
                )
            if new_phase_index != phase_index:
                phase_index = new_phase_index
                phase = phases[phase_index]
                if phase["silent"]:
                    update_index = -1
                    events.append(
                        {
                            "event": "producer_state",
                            "sequence": sequence,
                            "phase": phase["name"],
                            "phase_index": phase["index"],
                            "phase_start_frame": phase["start_frame"],
                            "phase_end_frame": phase["end_frame"],
                            "frame": frame,
                            "out_time_us": out_time_us,
                            "state": "silent",
                            "reason": "phase_change",
                            "update_frame": None,
                            "region_count": None,
                            "payload_bytes": 0,
                            "payload_sha256": None,
                            "force_empty": force_empty,
                            "status": "entered",
                        }
                    )
                    sequence += 1
                    continue
                new_update_index = update_index_for_frame(phase, frame)
                if new_update_index is None:
                    raise AnalysisError(
                        f"non-silent phase has no update for frame {frame}"
                    )
                update_index = new_update_index
                update = phase["updates"][update_index]
                events.append(
                    _replayed_send(
                        phase,
                        frame,
                        out_time_us,
                        "phase_change",
                        update,
                        force_empty,
                        sequence,
                        send_sequence,
                    )
                )
                sequence += 1
                send_sequence += 1
                continue
            if phase["silent"]:
                continue

            new_update_index = update_index_for_frame(phase, frame)
            if new_update_index is None:
                raise AnalysisError(f"phase has no update for frame {frame}")
            changed = new_update_index != update_index
            update_index = new_update_index
            update = phase["updates"][update_index]
            events.append(
                _replayed_send(
                    phase,
                    frame,
                    out_time_us,
                    "update" if changed else "heartbeat",
                    update,
                    force_empty,
                    sequence,
                    send_sequence,
                )
            )
            sequence += 1
            send_sequence += 1
        last_repeat_slot = repeat_slot
    if not saw_end:
        raise AnalysisError("progress=end is missing from progress records")
    return events


def validate_producer_coverage(
    sends: list[dict[str, Any]], schedule: dict[str, Any]
) -> dict[str, Any]:
    sends_per_phase = {
        phase["name"]: sum(event["phase"] == phase["name"] for event in sends)
        for phase in schedule["phases"]
        if not phase["silent"]
    }
    nominal_per_phase = {
        phase["name"]: len(
            range(phase["start_frame"], phase["end_frame"], schedule["repeat_frames"])
        )
        for phase in schedule["phases"]
        if not phase["silent"]
    }
    minimum_per_phase = {
        name: math.ceil(nominal * 0.90) for name, nominal in nominal_per_phase.items()
    }
    for phase_name, minimum in minimum_per_phase.items():
        if sends_per_phase[phase_name] < minimum:
            raise AnalysisError(
                f"producer phase {phase_name} sent {sends_per_phase[phase_name]} updates; "
                f"minimum 90% coverage is {minimum}"
            )
    nominal_total = sum(nominal_per_phase.values())
    minimum_total = math.ceil(nominal_total * 0.90)
    if len(sends) < minimum_total:
        raise AnalysisError(
            f"producer sent {len(sends)} updates; minimum 90% of nominal "
            f"{nominal_total} is {minimum_total}"
        )
    return {
        "nominal_send_count": nominal_total,
        "minimum_send_count": minimum_total,
        "sends_per_phase": sends_per_phase,
        "minimum_sends_per_phase": minimum_per_phase,
    }


def validate_producer_events(
    events: list[dict[str, Any]],
    schedule: dict[str, Any],
    force_empty: bool,
    progress_records: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_events = replay_producer_progress(schedule, progress_records, force_empty)
    if len(events) != len(expected_events):
        raise AnalysisError(
            f"producer event count {len(events)} differs from progress replay "
            f"count {len(expected_events)}"
        )
    previous_ns = -1
    for index, (event, expected) in enumerate(zip(events, expected_events)):
        event_type = expected["event"]
        fields = PRODUCER_SEND_FIELDS if event_type == "producer_send" else PRODUCER_STATE_FIELDS
        require_fields(event, fields, f"producer {event_type} event {index}")
        monotonic_ns = require_int(
            event.get("monotonic_ns"), f"producer event {index} monotonic_ns"
        )
        if monotonic_ns <= previous_ns:
            raise AnalysisError("producer monotonic timestamps are not strictly increasing")
        previous_ns = monotonic_ns
        for key, expected_value in expected.items():
            actual_value = event.get(key)
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                raise AnalysisError(
                    f"producer event {index} does not match progress replay for {key}"
                )

    sends = [event for event in events if event["event"] == "producer_send"]
    silent_states = sum(event["event"] == "producer_state" for event in events)
    first_send_per_phase: dict[str, dict[str, Any]] = {}
    previous_send_per_phase: dict[str, dict[str, Any]] = {}
    payloads_per_active_phase: dict[str, set[str]] = {
        "moving-a": set(),
        "moving-b": set(),
    }
    for event in sends:
        phase_name = event["phase"]
        previous_phase_send = previous_send_per_phase.get(phase_name)
        if previous_phase_send is not None:
            frame_gap = event["frame"] - previous_phase_send["frame"]
            if not 1 <= frame_gap <= 6:
                raise AnalysisError(
                    f"producer phase {phase_name} heartbeat gap is {frame_gap} frames; maximum is 6"
                )
        first_send_per_phase.setdefault(phase_name, event)
        previous_send_per_phase[phase_name] = event
        if not force_empty and phase_name in payloads_per_active_phase:
            payloads_per_active_phase[phase_name].add(event["payload_sha256"])

    if silent_states != 1:
        raise AnalysisError("producer must enter exactly one silent phase")
    if any(300 <= event["frame"] < 540 for event in sends):
        raise AnalysisError("producer sent an ROI datagram during the silent phase")
    if not sends or sends[0]["frame"] != 0 or sends[0]["reason"] != "initial":
        raise AnalysisError("producer does not begin with the frame-zero initial state")
    for phase in schedule["phases"]:
        if phase["silent"]:
            continue
        first = first_send_per_phase.get(phase["name"])
        if first is None:
            raise AnalysisError(f"producer did not send phase {phase['name']}")
        if first["frame"] >= phase["measure"]["start_frame"]:
            raise AnalysisError(f"producer first sent phase {phase['name']} after scoring began")
    recovery_frame = first_send_per_phase["moving-b"]["frame"]
    if recovery_frame > 543:
        raise AnalysisError(f"producer recovery first send was frame {recovery_frame}; maximum is 543")
    if not force_empty:
        for phase_name, payloads in payloads_per_active_phase.items():
            if len(payloads) < 2:
                raise AnalysisError(f"producer phase {phase_name} did not exercise two coordinates")
    coverage = validate_producer_coverage(sends, schedule)
    return {
        "force_empty": force_empty,
        "event_count": len(events),
        "send_count": len(sends),
        "silent_state_count": silent_states,
        "progress_record_count": len(progress_records),
        "progress_continue_count": sum(
            record["status"] == "continue" for record in progress_records
        ),
        "first_send_frame_per_phase": {
            name: event["frame"] for name, event in first_send_per_phase.items()
        },
        "active_payload_variants": {
            name: len(payloads) for name, payloads in payloads_per_active_phase.items()
        },
        **coverage,
        "sends": sends,
    }


def compact_states(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


def validate_bridge_events(
    events: list[dict[str, Any]],
    producer: dict[str, Any],
    schedule: dict[str, Any],
    require_dynamic_transitions: bool,
) -> dict[str, Any]:
    previous_ns = -1
    expected_forward_sequence = 0
    rx_events: list[dict[str, Any]] = []
    forward_events: list[dict[str, Any]] = []
    clear_events: list[dict[str, Any]] = []
    pending_rx: dict[int, dict[str, Any]] = {}
    states: list[int] = []
    for index, event in enumerate(events):
        if event.get("sequence") != index:
            raise AnalysisError(f"bridge sequence {index} is not contiguous")
        monotonic_ns = require_int(event.get("monotonic_ns"), f"bridge event {index} monotonic_ns")
        if monotonic_ns < previous_ns:
            raise AnalysisError("bridge monotonic timestamps went backwards")
        previous_ns = monotonic_ns
        kind = event.get("event")
        if kind == "roi_rx":
            require_fields(event, BRIDGE_RX_FIELDS, f"bridge rx event {index}")
            rx_sequence = require_int(event.get("rx_sequence"), "rx_sequence")
            if rx_sequence != len(rx_events) or event.get("status") != "valid":
                raise AnalysisError("bridge received a rejected or out-of-order test datagram")
            if event.get("region_count") not in (0, 1):
                raise AnalysisError("bridge rx region_count must be zero or one")
            pending_rx[rx_sequence] = event
            rx_events.append(event)
        elif kind == "roi_forward":
            require_fields(event, BRIDGE_FORWARD_FIELDS, f"bridge forward event {index}")
            if event.get("forward_sequence") != expected_forward_sequence:
                raise AnalysisError("bridge forward_sequence is not contiguous")
            expected_forward_sequence += 1
            rx_sequence = require_int(event.get("rx_sequence"), "forward rx_sequence")
            rx = pending_rx.pop(rx_sequence, None)
            if rx is None:
                raise AnalysisError("bridge forward has no unmatched valid receive")
            if event.get("reason") != "producer_update" or event.get("status") != "sent":
                raise AnalysisError("bridge producer forward was not successful")
            for field in ("payload_bytes", "payload_sha256", "region_count"):
                if event.get(field) != rx.get(field):
                    raise AnalysisError(f"bridge forward changed {field}")
            states.append(event["region_count"])
            forward_events.append(event)
        elif kind == "watchdog_clear":
            require_fields(event, WATCHDOG_CLEAR_FIELDS, f"watchdog clear event {index}")
            if event.get("forward_sequence") != expected_forward_sequence:
                raise AnalysisError("watchdog clear forward_sequence is not contiguous")
            expected_forward_sequence += 1
            empty = canonical_payload([])
            if (
                event.get("reason") != "watchdog_clear"
                or event.get("status") != "sent"
                or event.get("region_count") != 0
                or event.get("payload_bytes") != len(empty)
                or event.get("payload_sha256") != hashlib.sha256(empty).hexdigest()
            ):
                raise AnalysisError("watchdog clear payload or status is invalid")
            last_rx_sequence = event.get("last_valid_rx_sequence")
            if type(last_rx_sequence) is not int or not 0 <= last_rx_sequence < len(rx_events):
                raise AnalysisError("watchdog clear references an invalid receive")
            if last_rx_sequence != rx_events[-1]["rx_sequence"] or pending_rx:
                raise AnalysisError("watchdog clear does not reference the latest forwarded receive")
            last_rx = rx_events[last_rx_sequence]
            if event.get("last_valid_rx_monotonic_ns") != last_rx["monotonic_ns"]:
                raise AnalysisError("watchdog clear references the wrong receive timestamp")
            stale_age = require_int(event.get("stale_age_ns"), "stale_age_ns")
            if stale_age != event["monotonic_ns"] - last_rx["monotonic_ns"]:
                raise AnalysisError("watchdog stale_age_ns is not derived from monotonic time")
            if event.get("timeout_ms") != schedule["watchdog"]["timeout_ms"]:
                raise AnalysisError("watchdog clear reports the wrong configured timeout")
            min_ns = schedule["watchdog"]["min_clear_delay_ms"] * 1_000_000
            max_ns = schedule["watchdog"]["max_clear_delay_ms"] * 1_000_000
            if not min_ns <= stale_age <= max_ns:
                raise AnalysisError("watchdog clear is outside the frozen 300-500 ms window")
            states.append(0)
            clear_events.append(event)
        else:
            raise AnalysisError(f"bridge event {index} has unknown type {kind!r}")
    if pending_rx:
        raise AnalysisError("bridge did not forward every valid receive")
    sends = producer["sends"]
    if len(rx_events) != len(sends) or len(forward_events) != len(sends):
        raise AnalysisError("producer, bridge receive, and bridge forward counts differ")
    for index, (send, rx, forward) in enumerate(zip(sends, rx_events, forward_events)):
        for field in ("payload_bytes", "payload_sha256", "region_count"):
            if send.get(field) != rx.get(field) or send.get(field) != forward.get(field):
                raise AnalysisError(f"producer-to-bridge evidence mismatch at send {index}")
    if len(clear_events) != 1:
        raise AnalysisError(f"bridge emitted {len(clear_events)} watchdog clears; expected one")

    recovery_index = next(
        index for index, send in enumerate(sends) if send["phase"] == "moving-b"
    )
    recovery_latency_ns = forward_events[recovery_index]["monotonic_ns"] - sends[recovery_index]["monotonic_ns"]
    if not 0 <= recovery_latency_ns <= schedule["watchdog"]["max_resume_forward_ms"] * 1_000_000:
        raise AnalysisError("recovery ROI was not forwarded within 100 ms")
    transitions = compact_states(states)
    if require_dynamic_transitions and transitions != [0, 1, 0, 1, 0]:
        raise AnalysisError(f"dynamic region transitions are {transitions}; expected 0->1->0->1->0")
    if not require_dynamic_transitions and transitions != [0]:
        raise AnalysisError(f"baseline bridge forwarded non-empty state: {transitions}")
    return {
        "event_count": len(events),
        "rx_count": len(rx_events),
        "producer_forward_count": len(forward_events),
        "watchdog_clear_count": len(clear_events),
        "watchdog_stale_age_ms": clear_events[0]["stale_age_ns"] / 1_000_000.0,
        "resume_forward_latency_ms": recovery_latency_ns / 1_000_000.0,
        "region_state_transitions": transitions,
        "forwarded_region_counts": states,
    }


def validate_encode_log(path: Path, expected_region_counts: list[int]) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read encoder log {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        for pattern in FORBIDDEN_ROI_LOG_PATTERNS:
            if pattern.search(line):
                raise AnalysisError(
                    f"encoder log {path}:{line_number} contains a forbidden ROI error: {line}"
                )
    applied: list[dict[str, int]] = []
    for line_number, line in enumerate(lines, 1):
        match = APPLIED_ROI_PATTERN.search(line)
        if match:
            applied.append(
                {
                    "generation": int(match.group(1)),
                    "region_count": int(match.group(2)),
                    "line": line_number,
                }
            )
    if not applied:
        raise AnalysisError(f"encoder log contains no Applied ROI updates: {path}")
    generations = [item["generation"] for item in applied]
    if generations != list(range(1, len(applied) + 1)):
        raise AnalysisError(
            f"Applied ROI generations in {path} are not exactly 1..N: {generations}"
        )
    applied_counts = [item["region_count"] for item in applied]
    if applied_counts != expected_region_counts:
        mismatch = next(
            (
                index
                for index, (actual, expected) in enumerate(
                    zip(applied_counts, expected_region_counts), 1
                )
                if actual != expected
            ),
            min(len(applied_counts), len(expected_region_counts)) + 1,
        )
        raise AnalysisError(
            f"Applied ROI count sequence in {path} differs from bridge forwards "
            f"at generation {mismatch}: applied={len(applied_counts)}, "
            f"forwarded={len(expected_region_counts)}"
        )
    return {
        "path": str(path),
        "sha256": sha256(path),
        "applied_updates": len(applied),
        "first_generation": generations[0],
        "last_generation": generations[-1],
        "region_state_transitions": compact_states(applied_counts),
    }


def verify_sha256_file(path: Path, target: Path) -> str:
    expected = sha256(target)
    try:
        fields = path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise AnalysisError(f"cannot read SHA file {path}: {exc}") from exc
    if not fields or fields[0] != expected:
        raise AnalysisError(f"SHA file {path} does not match {target}")
    return expected


def build_filter_graph(
    probes: dict[str, dict[str, int]], expected_frames: int, fps: float, metrics: Path, mode: str
) -> tuple[str, list[str]]:
    branches = len(probes) * 2
    ref_labels = "".join(f"[r{i}]" for i in range(branches))
    dut_labels = "".join(f"[d{i}]" for i in range(branches))
    parts = [
        f"[0:v]trim=end_frame={expected_frames},setpts=N/({fps}*TB),format=yuv420p,split={branches}{ref_labels}",
        f"[1:v]trim=end_frame={expected_frames},setpts=N/({fps}*TB),format=yuv420p,split={branches}{dut_labels}",
    ]
    outputs: list[str] = []
    for index, (name, probe) in enumerate(probes.items()):
        pi = index * 2
        si = pi + 1
        crop = f"{probe['w']}:{probe['h']}:{probe['x']}:{probe['y']}"
        psnr_path = metrics / f"{mode}-{name}.psnr"
        ssim_path = metrics / f"{mode}-{name}.ssim"
        parts.extend(
            [
                f"[d{pi}]crop={crop}[dp{index}]",
                f"[r{pi}]crop={crop}[rp{index}]",
                f"[dp{index}][rp{index}]psnr=stats_file={psnr_path}:stats_version=2[op{index}]",
                f"[d{si}]crop={crop}[ds{index}]",
                f"[r{si}]crop={crop}[rs{index}]",
                f"[ds{index}][rs{index}]ssim=stats_file={ssim_path}[os{index}]",
            ]
        )
        outputs.extend([f"op{index}", f"os{index}"])
    return ";".join(parts), outputs


def run_metrics(
    ffmpeg: Path,
    source: Path,
    encoded: Path,
    bitstream_format: str,
    schedule: dict[str, Any],
    metrics: Path,
    mode: str,
) -> None:
    graph, outputs = build_filter_graph(
        schedule["probes"], schedule["expected_frames"], schedule["fps"], metrics, mode
    )
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-f",
        bitstream_format,
        "-i",
        str(encoded),
        "-filter_complex",
        graph,
    ]
    for output in outputs:
        command.extend(["-map", f"[{output}]"])
    command.extend(["-f", "null", "-"])
    completed = subprocess.run(command, capture_output=True, check=False)
    (metrics / f"{mode}.stdout").write_bytes(completed.stdout)
    (metrics / f"{mode}.log").write_bytes(completed.stderr)
    if completed.returncode != 0:
        raise AnalysisError(f"{mode} metric command failed with {completed.returncode}")


def parse_metric(
    path: Path, pattern: re.Pattern[str], expected_frames: int, infinity_cap: float | None = None
) -> list[float]:
    values: list[float] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AnalysisError(f"cannot read metric {path}: {exc}") from exc
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        try:
            value = float(match.group(2))
        except ValueError as exc:
            raise AnalysisError(f"invalid metric in {path}: {match.group(2)}") from exc
        if math.isinf(value) and value > 0 and infinity_cap is not None:
            value = infinity_cap
        elif not math.isfinite(value):
            raise AnalysisError(f"non-finite metric in {path}")
        values.append(value)
    if len(values) != expected_frames:
        raise AnalysisError(f"{path} has {len(values)} frames; expected {expected_frames}")
    return values


def strict_decode_and_count(
    ffmpeg: Path,
    ffprobe: Path,
    encoded: Path,
    bitstream_format: str,
    expected_frames: int,
    evidence_dir: Path,
    mode: str,
) -> int:
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-xerror",
        "-err_detect",
        "explode",
        "-f",
        bitstream_format,
        "-i",
        str(encoded),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    ]
    decoded = subprocess.run(command, capture_output=True, check=False)
    (evidence_dir / f"{mode}-decode.stdout").write_bytes(decoded.stdout)
    (evidence_dir / f"{mode}-decode.log").write_bytes(decoded.stderr)
    if decoded.returncode != 0:
        raise AnalysisError(f"{mode} strict decode failed with {decoded.returncode}")
    probe_command = [
        str(ffprobe),
        "-v",
        "error",
        "-f",
        bitstream_format,
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "default=nw=1:nk=1",
        str(encoded),
    ]
    counted = subprocess.run(probe_command, capture_output=True, text=True, check=False)
    (evidence_dir / f"{mode}-ffprobe.log").write_text(counted.stderr, encoding="utf-8")
    if counted.returncode != 0:
        raise AnalysisError(f"{mode} ffprobe failed with {counted.returncode}")
    try:
        frames = int(counted.stdout.strip())
    except ValueError as exc:
        raise AnalysisError(f"{mode} ffprobe returned {counted.stdout!r}") from exc
    if frames != expected_frames:
        raise AnalysisError(f"{mode} decoded {frames} frames; expected {expected_frames}")
    return frames


def mean(values: Sequence[float]) -> float:
    if not values:
        raise AnalysisError("cannot average an empty metric window")
    return statistics.fmean(values)


def window_delta(dynamic: Sequence[float], baseline: Sequence[float], start: int, end: int) -> list[float]:
    return [dynamic[index] - baseline[index] for index in range(start, end)]


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, **evidence: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), **evidence})


def calculate_cbr(
    baseline_bytes: int,
    dynamic_bytes: int,
    bitrate: int,
    frames: int,
    fps: float,
) -> dict[str, float]:
    if baseline_bytes <= 0 or dynamic_bytes <= 0:
        raise AnalysisError("encoded outputs must not be empty")
    theoretical = bitrate * (frames / fps) / 8.0
    return {
        "theoretical_bytes": theoretical,
        "baseline_target_delta_ratio": abs(baseline_bytes - theoretical) / theoretical,
        "dynamic_target_delta_ratio": abs(dynamic_bytes - theoretical) / theoretical,
        "output_size_delta_ratio": abs(dynamic_bytes - baseline_bytes) / baseline_bytes,
    }


def score_quality(
    values: dict[str, dict[str, dict[str, list[float]]]], schedule: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    thresholds = schedule["thresholds"]
    for phase in schedule["phases"]:
        measure = phase["measure"]
        start = measure["start_frame"]
        end = measure["end_frame"]
        deltas: dict[str, dict[str, float]] = {}
        for probe in ("A", "N", "B"):
            psnr = window_delta(
                values["dynamic"][probe]["psnr_y"], values["baseline"][probe]["psnr_y"], start, end
            )
            ssim = window_delta(
                values["dynamic"][probe]["ssim_y"], values["baseline"][probe]["ssim_y"], start, end
            )
            deltas[probe] = {
                "psnr_y_db_mean": mean(psnr),
                "psnr_y_db_median": statistics.median(psnr),
                "ssim_y_mean": mean(ssim),
            }
        name = phase["name"]
        matched_difference = (
            deltas["A"]["psnr_y_db_mean"] - deltas["B"]["psnr_y_db_mean"]
        )
        if name in ("moving-a", "moving-b"):
            boost = "A" if name == "moving-a" else "B"
            other = "B" if boost == "A" else "A"
            absolute = deltas[boost]["psnr_y_db_median"]
            spatial = deltas[boost]["psnr_y_db_mean"] - deltas["N"]["psnr_y_db_mean"]
            ssim_spatial = deltas[boost]["ssim_y_mean"] - deltas["N"]["ssim_y_mean"]
            inactive_neutral = abs(
                deltas[other]["psnr_y_db_mean"] - deltas["N"]["psnr_y_db_mean"]
            )
            phase_report = {
                "name": name,
                "window": [start, end],
                "deltas": deltas,
                "boost_probe": boost,
                "active_absolute_psnr_y_db": absolute,
                "active_vs_neutral_psnr_y_db": spatial,
                "active_ssim_vs_neutral": ssim_spatial,
                "matched_ab_difference_psnr_y_db": matched_difference,
                "inactive_vs_neutral_diagnostic_psnr_y_db": inactive_neutral,
            }
        else:
            probe_values = [deltas[name]["psnr_y_db_mean"] for name in ("A", "N", "B")]
            spread = max(probe_values) - min(probe_values)
            phase_report = {
                "name": name,
                "window": [start, end],
                "deltas": deltas,
                "matched_ab_difference_psnr_y_db": matched_difference,
                "spatial_residual_psnr_y_db": spread,
            }
        phases.append(phase_report)

    by_name = {phase["name"]: phase for phase in phases}
    matched_baseline = by_name["warmup-clear"]["matched_ab_difference_psnr_y_db"]
    by_name["warmup-clear"][
        "matched_baseline_difference_psnr_y_db"
    ] = matched_baseline
    for name in ("moving-a", "moving-b"):
        phase = by_name[name]
        matched_active_gain = (
            phase["matched_ab_difference_psnr_y_db"] - matched_baseline
            if name == "moving-a"
            else matched_baseline - phase["matched_ab_difference_psnr_y_db"]
        )
        phase["matched_baseline_difference_psnr_y_db"] = matched_baseline
        phase["matched_active_gain_psnr_y_db"] = matched_active_gain
        add_check(
            checks,
            f"{name}:active-absolute-gain",
            phase["active_absolute_psnr_y_db"]
            >= thresholds["min_active_absolute_psnr_y_db"],
            actual_db=phase["active_absolute_psnr_y_db"],
            minimum_db=thresholds["min_active_absolute_psnr_y_db"],
        )
        add_check(
            checks,
            f"{name}:active-vs-neutral",
            phase["active_vs_neutral_psnr_y_db"]
            >= thresholds["min_active_vs_neutral_psnr_y_db"],
            actual_db=phase["active_vs_neutral_psnr_y_db"],
            minimum_db=thresholds["min_active_vs_neutral_psnr_y_db"],
        )
        add_check(
            checks,
            f"{name}:active-ssim-vs-neutral",
            phase["active_ssim_vs_neutral"] >= thresholds["min_active_ssim_y"],
            actual=phase["active_ssim_vs_neutral"],
            minimum=thresholds["min_active_ssim_y"],
        )
        add_check(
            checks,
            f"{name}:matched-active-gain",
            matched_active_gain >= thresholds["min_active_vs_neutral_psnr_y_db"],
            actual_db=matched_active_gain,
            minimum_db=thresholds["min_active_vs_neutral_psnr_y_db"],
            baseline_difference_db=matched_baseline,
            phase_difference_db=phase["matched_ab_difference_psnr_y_db"],
        )

    residual_contracts = (
        (
            "watchdog-silent",
            "moving-a",
            "max_watchdog_spatial_residual_psnr_y_db",
            "max_watchdog_spatial_residual_fraction",
        ),
        (
            "final-clear",
            "moving-b",
            "max_final_clear_spatial_residual_psnr_y_db",
            "max_final_clear_spatial_residual_fraction",
        ),
    )
    for clear_name, active_name, absolute_key, fraction_key in residual_contracts:
        clear_phase = by_name[clear_name]
        residual = abs(
            clear_phase["matched_ab_difference_psnr_y_db"] - matched_baseline
        )
        reference_gain = by_name[active_name]["matched_active_gain_psnr_y_db"]
        fraction = thresholds[fraction_key]
        relative_maximum = reference_gain * fraction
        clear_phase["matched_baseline_difference_psnr_y_db"] = matched_baseline
        clear_phase["matched_clear_residual_psnr_y_db"] = residual
        clear_phase["reference_matched_active_gain_psnr_y_db"] = reference_gain
        clear_phase["relative_residual_fraction_limit"] = fraction
        clear_phase["relative_residual_maximum_psnr_y_db"] = relative_maximum
        add_check(
            checks,
            f"{clear_name}:matched-absolute-residual",
            residual <= thresholds[absolute_key],
            actual_db=residual,
            maximum_db=thresholds[absolute_key],
            baseline_difference_db=matched_baseline,
            clear_difference_db=clear_phase["matched_ab_difference_psnr_y_db"],
        )
        add_check(
            checks,
            f"{clear_name}:matched-relative-residual",
            residual <= relative_maximum,
            actual_db=residual,
            reference_gain_db=reference_gain,
            maximum_fraction=fraction,
            maximum_db=relative_maximum,
        )
    return checks, phases


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    required = (
        args.ffmpeg,
        args.ffprobe,
        args.source,
        args.baseline,
        args.dynamic,
        args.schedule,
        args.schedule_sha256,
        args.baseline_progress,
        args.dynamic_progress,
        args.baseline_producer_events,
        args.dynamic_producer_events,
        args.baseline_bridge_events,
        args.dynamic_bridge_events,
        args.baseline_encode_log,
        args.dynamic_encode_log,
    )
    for path in required:
        if not path.is_file():
            raise AnalysisError(f"required file does not exist: {path}")
    if not SAFE_PATH.fullmatch(str(args.output_dir)):
        raise AnalysisError("output directory may contain only letters, digits, _, -, ., and /")
    if args.output_dir.exists():
        raise AnalysisError(f"output directory already exists: {args.output_dir}")

    raw_schedule = load_json(args.schedule)
    schedule = validate_schedule(raw_schedule)
    schedule_hash = verify_sha256_file(args.schedule_sha256, args.schedule)
    args.output_dir.mkdir(parents=True)
    metrics_dir = args.output_dir / "metrics"
    metrics_dir.mkdir()

    frame_counts = {
        "baseline": strict_decode_and_count(
            args.ffmpeg, args.ffprobe, args.baseline, args.format, schedule["expected_frames"], args.output_dir, "baseline"
        ),
        "dynamic": strict_decode_and_count(
            args.ffmpeg, args.ffprobe, args.dynamic, args.format, schedule["expected_frames"], args.output_dir, "dynamic"
        ),
    }
    run_metrics(args.ffmpeg, args.source, args.baseline, args.format, schedule, metrics_dir, "baseline")
    run_metrics(args.ffmpeg, args.source, args.dynamic, args.format, schedule, metrics_dir, "dynamic")
    values: dict[str, dict[str, dict[str, list[float]]]] = {"baseline": {}, "dynamic": {}}
    for mode in values:
        for probe in ("A", "N", "B"):
            values[mode][probe] = {
                "psnr_y": parse_metric(
                    metrics_dir / f"{mode}-{probe}.psnr", PSNR_PATTERN, schedule["expected_frames"], PSNR_INFINITY_CAP_DB
                ),
                "ssim_y": parse_metric(
                    metrics_dir / f"{mode}-{probe}.ssim", SSIM_PATTERN, schedule["expected_frames"]
                ),
            }

    progress_records = {
        "baseline": load_progress(args.baseline_progress),
        "dynamic": load_progress(args.dynamic_progress),
    }
    producer_reports = {
        "baseline": validate_producer_events(
            load_jsonl(args.baseline_producer_events),
            schedule,
            True,
            progress_records["baseline"],
        ),
        "dynamic": validate_producer_events(
            load_jsonl(args.dynamic_producer_events),
            schedule,
            False,
            progress_records["dynamic"],
        ),
    }
    bridge_reports = {
        "baseline": validate_bridge_events(
            load_jsonl(args.baseline_bridge_events), producer_reports["baseline"], schedule, False
        ),
        "dynamic": validate_bridge_events(
            load_jsonl(args.dynamic_bridge_events), producer_reports["dynamic"], schedule, True
        ),
    }
    encode_log_reports = {
        "baseline": validate_encode_log(
            args.baseline_encode_log,
            bridge_reports["baseline"]["forwarded_region_counts"],
        ),
        "dynamic": validate_encode_log(
            args.dynamic_encode_log,
            bridge_reports["dynamic"]["forwarded_region_counts"],
        ),
    }
    checks, phase_reports = score_quality(values, schedule)
    baseline_bytes = args.baseline.stat().st_size
    dynamic_bytes = args.dynamic.stat().st_size
    cbr = calculate_cbr(
        baseline_bytes,
        dynamic_bytes,
        schedule["target_bitrate_bps"],
        schedule["expected_frames"],
        schedule["fps"],
    )
    max_size = schedule["thresholds"]["max_cbr_size_delta_ratio"]
    max_target = schedule["thresholds"]["max_cbr_target_delta_ratio"]
    add_check(
        checks,
        "cbr-output-size-delta",
        cbr["output_size_delta_ratio"] <= max_size,
        actual_ratio=cbr["output_size_delta_ratio"],
        maximum_ratio=max_size,
    )
    for mode in ("baseline", "dynamic"):
        key = f"{mode}_target_delta_ratio"
        add_check(
            checks,
            f"cbr-{mode}-target-delta",
            cbr[key] <= max_target,
            actual_ratio=cbr[key],
            maximum_ratio=max_target,
        )

    report = {
        "schema_version": 1,
        "passed": all(check["passed"] for check in checks),
        "format": args.format,
        "expected_frames": schedule["expected_frames"],
        "frame_counts": frame_counts,
        "measurement_semantics": (
            "fixed probe intersections; clear gates use matched A/B "
            "difference-in-differences; no frame-exact ROI claim"
        ),
        "schedule": {"path": str(args.schedule), "sha256": schedule_hash},
        "artifacts": {
            "source": {"path": str(args.source), "sha256": sha256(args.source)},
            "baseline": {"path": str(args.baseline), "bytes": baseline_bytes, "sha256": sha256(args.baseline)},
            "dynamic": {"path": str(args.dynamic), "bytes": dynamic_bytes, "sha256": sha256(args.dynamic)},
            "progress": {
                "baseline": {
                    "path": str(args.baseline_progress),
                    "sha256": sha256(args.baseline_progress),
                },
                "dynamic": {
                    "path": str(args.dynamic_progress),
                    "sha256": sha256(args.dynamic_progress),
                },
            },
            "event_logs": {
                "baseline_producer": {
                    "path": str(args.baseline_producer_events),
                    "sha256": sha256(args.baseline_producer_events),
                },
                "dynamic_producer": {
                    "path": str(args.dynamic_producer_events),
                    "sha256": sha256(args.dynamic_producer_events),
                },
                "baseline_bridge": {
                    "path": str(args.baseline_bridge_events),
                    "sha256": sha256(args.baseline_bridge_events),
                },
                "dynamic_bridge": {
                    "path": str(args.dynamic_bridge_events),
                    "sha256": sha256(args.dynamic_bridge_events),
                },
            },
        },
        "producer_events": {
            mode: {key: value for key, value in result.items() if key != "sends"}
            for mode, result in producer_reports.items()
        },
        "bridge_events": bridge_reports,
        "encoder_logs": encode_log_reports,
        "thresholds": schedule["thresholds"],
        "phases": phase_reports,
        "cbr": cbr,
        "checks": checks,
    }
    return report


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        f"result={'PASS' if report['passed'] else 'FAIL'}",
        f"format={report['format']}",
        f"frames={report['expected_frames']}",
        "alignment=fixed-intersection-probes; frame-exact ROI is not claimed",
    ]
    for phase in report["phases"]:
        if "active_absolute_psnr_y_db" in phase:
            lines.append(
                f"{phase['name']}: target={phase['active_absolute_psnr_y_db']:+.3f} dB, "
                f"target-neutral={phase['active_vs_neutral_psnr_y_db']:+.3f} dB, "
                f"matched-calibrated={phase['matched_active_gain_psnr_y_db']:+.3f} dB, "
                "inactive-neutral-diagnostic="
                f"{phase['inactive_vs_neutral_diagnostic_psnr_y_db']:.3f} dB"
            )
        else:
            if "matched_clear_residual_psnr_y_db" in phase:
                lines.append(
                    f"{phase['name']}: matched-A-B-residual="
                    f"{phase['matched_clear_residual_psnr_y_db']:.3f} dB, "
                    f"relative-limit={phase['relative_residual_maximum_psnr_y_db']:.3f} dB, "
                    "neutral-spread-diagnostic="
                    f"{phase['spatial_residual_psnr_y_db']:.3f} dB"
                )
            else:
                lines.append(
                    f"{phase['name']}: matched-A-B="
                    f"{phase['matched_ab_difference_psnr_y_db']:+.3f} dB, "
                    "neutral-spread-diagnostic="
                    f"{phase['spatial_residual_psnr_y_db']:.3f} dB"
                )
    for mode in ("baseline", "dynamic"):
        bridge = report["bridge_events"][mode]
        lines.append(
            f"{mode}-watchdog: stale={bridge['watchdog_stale_age_ms']:.3f} ms, "
            f"resume-forward={bridge['resume_forward_latency_ms']:.3f} ms, "
            f"states={bridge['region_state_transitions']}"
        )
        lines.append(
            f"{mode}-encoder-log: applied="
            f"{report['encoder_logs'][mode]['applied_updates']}, "
            f"states={report['encoder_logs'][mode]['region_state_transitions']}"
        )
    lines.append(
        f"cbr: baseline-target={report['cbr']['baseline_target_delta_ratio']:.3%}, "
        f"dynamic-target={report['cbr']['dynamic_target_delta_ratio']:.3%}, "
        f"output-delta={report['cbr']['output_size_delta_ratio']:.3%}"
    )
    failed = [check["name"] for check in report["checks"] if not check["passed"]]
    if failed:
        lines.append("failed-checks=" + ",".join(failed))
    return "\n".join(lines) + "\n"


def sample_schedule() -> dict[str, Any]:
    def updates(start: int, end: int, base_x: int) -> list[dict[str, Any]]:
        offsets = ((0, 0), (16, 0), (16, 16), (0, 16))
        return [
            {
                "frame": frame,
                "regions": [
                    {
                        "x": base_x + offsets[index % 4][0],
                        "y": 256 + offsets[index % 4][1],
                        "w": 512,
                        "h": 512,
                        "qoffset": -0.25,
                    }
                ],
            }
            for index, frame in enumerate(range(start, end, 12))
        ]

    return {
        "schema_version": 1,
        "name": "rk3588-watchdog-moving-roi",
        "frame": {"width": 1920, "height": 1080, "fps": 30},
        "gop_frames": 60,
        "repeat_hz": 10,
        "target_bitrate_bps": 4_000_000,
        "watchdog": {
            "timeout_ms": 400,
            "min_clear_delay_ms": 400,
            "max_clear_delay_ms": 500,
            "max_resume_forward_ms": 100,
        },
        "probes": {
            "A": {"x": 128, "y": 320, "w": 384, "h": 384},
            "N": {"x": 768, "y": 320, "w": 384, "h": 384},
            "B": {"x": 1408, "y": 320, "w": 384, "h": 384},
        },
        "thresholds": {
            "min_active_absolute_psnr_y_db": 0.25,
            "min_active_vs_neutral_psnr_y_db": 0.5,
            "min_active_ssim_y": 0.0005,
            "max_watchdog_spatial_residual_psnr_y_db": 0.5,
            "max_watchdog_spatial_residual_fraction": 0.20,
            "max_final_clear_spatial_residual_psnr_y_db": 0.5,
            "max_final_clear_spatial_residual_fraction": 0.20,
            "max_cbr_size_delta_ratio": 0.10,
            "max_cbr_target_delta_ratio": 0.10,
        },
        "phases": [
            {
                "name": "warmup-clear",
                "start_frame": 0,
                "end_frame": 120,
                "updates": [{"frame": 0, "regions": []}],
                "measure": {"start_frame": 60, "end_frame": 120, "clear_probes": ["A", "N", "B"]},
            },
            {
                "name": "moving-a",
                "start_frame": 120,
                "end_frame": 300,
                "updates": updates(120, 300, 64),
                "measure": {"start_frame": 180, "end_frame": 300, "boost_probe": "A", "neutral_probe": "N"},
            },
            {
                "name": "watchdog-silent",
                "start_frame": 300,
                "end_frame": 540,
                "silent": True,
                "measure": {"start_frame": 390, "end_frame": 540, "clear_probes": ["A", "N", "B"]},
            },
            {
                "name": "moving-b",
                "start_frame": 540,
                "end_frame": 780,
                "updates": updates(540, 780, 1344),
                "measure": {"start_frame": 600, "end_frame": 780, "boost_probe": "B", "neutral_probe": "N"},
            },
            {
                "name": "final-clear",
                "start_frame": 780,
                "end_frame": 900,
                "updates": [{"frame": 780, "regions": []}],
                "measure": {"start_frame": 840, "end_frame": 900, "clear_probes": ["A", "N", "B"]},
            },
        ],
    }


def nominal_progress_records(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    repeat_hz = int(schedule["repeat_hz"])
    fps = int(schedule["fps"])
    final_slot = (
        schedule["expected_frames"] * repeat_hz // fps
    )
    records = [
        {
            "status": "continue",
            "out_time_us": slot * 1_000_000 // repeat_hz,
        }
        for slot in range(1, final_slot)
    ]
    records.append(
        {
            "status": "end",
            "out_time_us": schedule["expected_frames"]
            * 1_000_000
            // fps,
        }
    )
    return records


def make_producer_events(
    schedule: dict[str, Any],
    force_empty: bool,
    progress_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    normalized = validate_schedule(schedule)
    progress = (
        nominal_progress_records(normalized)
        if progress_records is None
        else progress_records
    )
    replayed = replay_producer_progress(normalized, progress, force_empty)
    return [
        {
            **event,
            "monotonic_ns": 1_000_000_000
            + max(0, event["out_time_us"]) * 1_000
            + sequence,
        }
        for sequence, event in enumerate(replayed)
    ]


def make_bridge_events(producer: dict[str, Any], schedule: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    sequence = 0
    forward_sequence = 0
    clear_inserted = False
    sends = producer["sends"]
    for index, send in enumerate(sends):
        if not clear_inserted and send["phase"] == "moving-b":
            last = sends[index - 1]
            last_rx_ns = last["monotonic_ns"] + 1_000_000
            clear_ns = last_rx_ns + 410_000_000
            empty = canonical_payload([])
            events.append(
                {
                    "event": "watchdog_clear",
                    "sequence": sequence,
                    "forward_sequence": forward_sequence,
                    "monotonic_ns": clear_ns,
                    "payload_bytes": len(empty),
                    "payload_sha256": hashlib.sha256(empty).hexdigest(),
                    "region_count": 0,
                    "reason": "watchdog_clear",
                    "last_valid_rx_sequence": index - 1,
                    "last_valid_rx_monotonic_ns": last_rx_ns,
                    "stale_age_ns": 410_000_000,
                    "timeout_ms": 400,
                    "status": "sent",
                }
            )
            sequence += 1
            forward_sequence += 1
            clear_inserted = True
        rx_ns = send["monotonic_ns"] + 1_000_000
        fields = {
            "payload_bytes": send["payload_bytes"],
            "payload_sha256": send["payload_sha256"],
            "region_count": send["region_count"],
        }
        events.append(
            {
                "event": "roi_rx",
                "sequence": sequence,
                "rx_sequence": index,
                "monotonic_ns": rx_ns,
                "status": "valid",
                **fields,
            }
        )
        sequence += 1
        events.append(
            {
                "event": "roi_forward",
                "sequence": sequence,
                "forward_sequence": forward_sequence,
                "rx_sequence": index,
                "monotonic_ns": rx_ns + 1_000_000,
                "reason": "producer_update",
                "status": "sent",
                **fields,
            }
        )
        sequence += 1
        forward_sequence += 1
    return events


class AnalyzerSelfTest(unittest.TestCase):
    def test_schedule_and_event_chain(self) -> None:
        schedule = validate_schedule(sample_schedule())
        progress = nominal_progress_records(schedule)
        dynamic_events = make_producer_events(sample_schedule(), False, progress)
        producer = validate_producer_events(
            dynamic_events, schedule, False, progress
        )
        self.assertEqual(producer["silent_state_count"], 1)
        self.assertFalse(any(300 <= event["frame"] < 540 for event in producer["sends"]))
        bridge_events = make_bridge_events(producer, schedule)
        bridge = validate_bridge_events(bridge_events, producer, schedule, True)
        self.assertEqual(bridge["region_state_transitions"], [0, 1, 0, 1, 0])
        self.assertEqual(bridge["watchdog_clear_count"], 1)
        self.assertLessEqual(bridge["resume_forward_latency_ms"], 100)
        with tempfile.TemporaryDirectory(prefix="roi-watchdog-log-test-") as root:
            encode_log = Path(root) / "encode.log"
            encode_log.write_text(
                "".join(
                    f"Applied ROI socket update #{generation} with {count} region(s).\n"
                    for generation, count in enumerate(
                        bridge["forwarded_region_counts"], 1
                    )
                ),
                encoding="utf-8",
            )
            summary = validate_encode_log(
                encode_log, bridge["forwarded_region_counts"]
            )
            self.assertEqual(summary["applied_updates"], len(bridge["forwarded_region_counts"]))
            valid_lines = encode_log.read_text(encoding="utf-8").splitlines()
            invalid_generation = list(valid_lines)
            invalid_generation[1] = invalid_generation[1].replace("#2 ", "#4 ")
            encode_log.write_text("\n".join(invalid_generation) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AnalysisError, "not exactly 1..N"):
                validate_encode_log(encode_log, bridge["forwarded_region_counts"])
            invalid_count = list(valid_lines)
            invalid_count[1] = re.sub(
                r"with [01] region", "with 8 region", invalid_count[1]
            )
            encode_log.write_text("\n".join(invalid_count) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AnalysisError, "differs from bridge forwards"):
                validate_encode_log(encode_log, bridge["forwarded_region_counts"])
            encode_log.write_text("\n".join(valid_lines) + "\n", encoding="utf-8")
            with encode_log.open("a", encoding="utf-8") as stream:
                stream.write("Ignoring invalid ROI socket update (Invalid argument).\n")
            with self.assertRaisesRegex(AnalysisError, "forbidden ROI error"):
                validate_encode_log(encode_log, bridge["forwarded_region_counts"])

    def test_force_empty_baseline(self) -> None:
        schedule = validate_schedule(sample_schedule())
        progress = nominal_progress_records(schedule)
        events = make_producer_events(sample_schedule(), True, progress)
        producer = validate_producer_events(events, schedule, True, progress)
        bridge = validate_bridge_events(make_bridge_events(producer, schedule), producer, schedule, False)
        self.assertEqual(bridge["region_state_transitions"], [0])

    def test_progress_catch_up_replay_and_tamper_rejection(self) -> None:
        schedule = validate_schedule(sample_schedule())
        progress_lines = [
            "frame=7",
            "out_time_us=233333",
            "progress=continue",
        ]
        repeat_hz = int(schedule["repeat_hz"])
        fps = int(schedule["fps"])
        final_slot = (
            schedule["expected_frames"] * repeat_hz // fps
        )
        for slot in range(3, final_slot):
            progress_lines.extend(
                (
                    f"out_time_us={slot * 1_000_000 // repeat_hz}",
                    "progress=continue",
                )
            )
        progress_lines.extend(
            (
                f"out_time_us={schedule['expected_frames'] * 1_000_000 // fps}",
                "progress=end",
            )
        )
        progress = parse_progress_lines(progress_lines, "catch-up-test")
        events = make_producer_events(sample_schedule(), False, progress)
        self.assertEqual(
            [(event["frame"], event["out_time_us"]) for event in events[:3]],
            [(0, 0), (3, 233333), (6, 233333)],
        )
        producer = validate_producer_events(events, schedule, False, progress)
        self.assertEqual(producer["send_count"], 220)

        event_tamper = [dict(event) for event in events]
        event_tamper[1]["out_time_us"] = 200000
        with self.assertRaisesRegex(AnalysisError, "progress replay for out_time_us"):
            validate_producer_events(event_tamper, schedule, False, progress)

        progress_tamper = [dict(record) for record in progress]
        progress_tamper[0]["out_time_us"] = 133333
        with self.assertRaisesRegex(AnalysisError, "progress replay for out_time_us"):
            validate_producer_events(events, schedule, False, progress_tamper)

    def test_progress_file_line_limit_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="roi-watchdog-progress-test-") as root:
            progress_path = Path(root) / "progress.txt"
            progress_path.write_bytes(
                b"out_time_us=233333\r\nprogress=continue\r\n"
                b"out_time_us=30000000\r\nprogress=end\r\n"
            )
            self.assertEqual(
                load_progress(progress_path),
                [
                    {"status": "continue", "out_time_us": 233333},
                    {"status": "end", "out_time_us": 30000000},
                ],
            )
            progress_path.write_bytes(
                b"x" * (MAX_PROGRESS_LINE_BYTES + 1) + b"\nprogress=end\n"
            )
            with self.assertRaisesRegex(AnalysisError, "progress line is too long"):
                load_progress(progress_path)

    def test_five_hz_event_coverage_is_rejected(self) -> None:
        schedule = validate_schedule(sample_schedule())
        progress = nominal_progress_records(schedule)
        events = make_producer_events(sample_schedule(), False, progress)
        sends = [
            event
            for event in events
            if event["event"] == "producer_send" and event["frame"] % 6 == 0
        ]
        self.assertEqual(len(sends), 110)
        with self.assertRaisesRegex(AnalysisError, "minimum 90% coverage"):
            validate_producer_coverage(sends, schedule)

    def test_sticky_matched_contrast_is_rejected(self) -> None:
        schedule = validate_schedule(sample_schedule())
        values: dict[str, dict[str, dict[str, list[float]]]] = {
            mode: {
                probe: {
                    "psnr_y": [30.0] * 900,
                    "ssim_y": [0.9] * 900,
                }
                for probe in ("A", "N", "B")
            }
            for mode in ("baseline", "dynamic")
        }
        for frame in range(180, 300):
            values["dynamic"]["A"]["psnr_y"][frame] = 32.0
            values["dynamic"]["A"]["ssim_y"][frame] = 0.902
        for frame in range(390, 540):
            values["dynamic"]["A"]["psnr_y"][frame] = 32.0
        for frame in range(600, 780):
            values["dynamic"]["B"]["psnr_y"][frame] = 32.0
            values["dynamic"]["B"]["ssim_y"][frame] = 0.902
        for frame in range(840, 900):
            values["dynamic"]["B"]["psnr_y"][frame] = 32.0
        checks, _phases = score_quality(values, schedule)
        by_name = {check["name"]: check for check in checks}
        self.assertFalse(
            by_name["watchdog-silent:matched-absolute-residual"]["passed"]
        )
        self.assertFalse(
            by_name["watchdog-silent:matched-relative-residual"]["passed"]
        )
        self.assertFalse(
            by_name["final-clear:matched-absolute-residual"]["passed"]
        )
        self.assertFalse(
            by_name["final-clear:matched-relative-residual"]["passed"]
        )

    def test_neutral_rc_debt_does_not_fail_matched_clear_gates(self) -> None:
        schedule = validate_schedule(sample_schedule())
        values: dict[str, dict[str, dict[str, list[float]]]] = {
            mode: {
                probe: {
                    "psnr_y": [30.0] * 900,
                    "ssim_y": [0.9] * 900,
                }
                for probe in ("A", "N", "B")
            }
            for mode in ("baseline", "dynamic")
        }
        for frame in range(180, 300):
            values["dynamic"]["A"]["psnr_y"][frame] = 32.0
            values["dynamic"]["A"]["ssim_y"][frame] = 0.902
        for frame in range(390, 540):
            values["dynamic"]["N"]["psnr_y"][frame] = 29.3
        for frame in range(600, 780):
            values["dynamic"]["B"]["psnr_y"][frame] = 32.0
            values["dynamic"]["B"]["ssim_y"][frame] = 0.902
        for frame in range(840, 900):
            values["dynamic"]["N"]["psnr_y"][frame] = 29.3
        checks, phases = score_quality(values, schedule)
        self.assertTrue(all(check["passed"] for check in checks))
        by_phase = {phase["name"]: phase for phase in phases}
        for name in ("watchdog-silent", "final-clear"):
            phase = by_phase[name]
            self.assertGreater(phase["spatial_residual_psnr_y_db"], 0.6)
            self.assertAlmostEqual(phase["matched_clear_residual_psnr_y_db"], 0.0)

    def test_silent_send_and_hash_tamper_are_rejected(self) -> None:
        schedule = validate_schedule(sample_schedule())
        progress = nominal_progress_records(schedule)
        events = make_producer_events(sample_schedule(), False, progress)
        events[1]["payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(AnalysisError, "payload_sha256"):
            validate_producer_events(events, schedule, False, progress)
        events = make_producer_events(sample_schedule(), False, progress)
        events.insert(101, dict(events[100]))
        with self.assertRaises(AnalysisError):
            validate_producer_events(events, schedule, False, progress)

    def test_cbr_math(self) -> None:
        result = calculate_cbr(15_000_000, 15_300_000, 4_000_000, 900, 30)
        self.assertAlmostEqual(result["theoretical_bytes"], 15_000_000)
        self.assertAlmostEqual(result["output_size_delta_ratio"], 0.02)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--dynamic", type=Path)
    parser.add_argument("--baseline-progress", type=Path)
    parser.add_argument("--dynamic-progress", type=Path)
    parser.add_argument("--baseline-producer-events", type=Path)
    parser.add_argument("--dynamic-producer-events", type=Path)
    parser.add_argument("--baseline-bridge-events", type=Path)
    parser.add_argument("--dynamic-bridge-events", type=Path)
    parser.add_argument("--baseline-encode-log", type=Path)
    parser.add_argument("--dynamic-encode-log", type=Path)
    parser.add_argument("--format", choices=("h264", "hevc"))
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--schedule-sha256", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arguments)
    if args.self_test:
        if arguments != ["--self-test"]:
            parser.error("--self-test cannot be combined with other arguments")
        return args
    missing = [
        name
        for name in (
            "ffmpeg",
            "ffprobe",
            "source",
            "baseline",
            "dynamic",
            "baseline_progress",
            "dynamic_progress",
            "baseline_producer_events",
            "dynamic_producer_events",
            "baseline_bridge_events",
            "dynamic_bridge_events",
            "baseline_encode_log",
            "dynamic_encode_log",
            "format",
            "schedule",
            "schedule_sha256",
            "output_dir",
        )
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("missing required arguments: " + ", ".join("--" + name.replace("_", "-") for name in missing))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(AnalyzerSelfTest)
        )
        return 0 if result.wasSuccessful() else 1
    try:
        report = analyze(args)
        summary = render_summary(report)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.output_dir / "summary.txt").write_text(summary, encoding="utf-8")
        print(summary, end="")
        return 0 if report["passed"] else 1
    except AnalysisError as exc:
        print(f"analyze-watchdog.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
