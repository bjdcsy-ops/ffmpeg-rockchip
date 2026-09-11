#!/usr/bin/env python3
"""Analyze the RK3588 all-negative 0/1/2/8 production ROI matrix."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import statistics
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

# Keep this standalone tool tied to the already validated fixed-effect metric
# implementation without creating bytecode beside the evidence scripts.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as common  # noqa: E402


EXPECTED_WIDTH = 1920
EXPECTED_HEIGHT = 1080
EXPECTED_FPS = 30.0
EXPECTED_GOP = 60
EXPECTED_BITRATE_BPS = 4_000_000
EXPECTED_FRAMES = 1020
EXPECTED_REGION_COUNTS = [0, 1, 0, 2, 0, 8, 0]
EXPECTED_ACTIVE_COUNTS = [1, 2, 8]
EXPECTED_CLEAR_ROLES = ["initial", "intermediate", "intermediate", "final"]
EXPECTED_ROI_AREA = 147_456
EXPECTED_TARGET_PROBE_AREA = 102_400
MIN_REGION_SEPARATION = 32
MIN_TOTAL_SEND_COVERAGE = Fraction(9, 10)
MIN_INITIAL_SEND_COVERAGE = Fraction(3, 4)
MIN_PHASE_SEND_COVERAGE = Fraction(9, 10)
TERMINAL_REPEAT_GUARD_SLOTS = 3
SAFE_NAME = common.SAFE_NAME_PATTERN
SAFE_PATH = common.SAFE_FILTER_PATH_PATTERN
PROGRESS_INTEGER_PATTERN = re.compile(r"-?[0-9]+\Z")
PROGRESS_INT64_MIN = -(1 << 63)
PROGRESS_INT64_MAX = (1 << 63) - 1
MAX_PROGRESS_LINE_BYTES = 64 * 1024
APPLIED_ROI_PATTERN = re.compile(
    r"Applied ROI socket update #(\d+) with (\d+) region\(s\)\."
)
FORBIDDEN_ROI_LOG_PATTERNS = (
    re.compile(r"Ignoring .*ROI socket update", re.IGNORECASE),
    re.compile(r"ROI socket update .*overlap", re.IGNORECASE),
    re.compile(r"(?:Invalid|Overlapping) ROI", re.IGNORECASE),
    re.compile(r"ROI socket update budget exhausted", re.IGNORECASE),
    re.compile(r"Failed (?:to )?.*ROI", re.IGNORECASE),
    re.compile(r"ROI setup (?:failed|error)", re.IGNORECASE),
)

THRESHOLD_FIELDS = {
    "identity",
    "expected_qoffset",
    "min_target_gain_psnr_y_db",
    "min_target_background_selectivity_psnr_y_db",
    "max_background_loss_psnr_y_db",
    "min_q25_over_q15_psnr_y_db",
    "max_initial_clear_absolute_psnr_y_db",
    "max_initial_clear_spatial_psnr_y_db",
    "max_clear_spatial_psnr_y_db",
    "max_cbr_size_delta_ratio",
    "max_cbr_target_delta_ratio",
}
EXPECTED_PROFILE_THRESHOLDS = {
    "production-q15": {
        "min_target_gain_psnr_y_db": 0.3,
        "min_target_background_selectivity_psnr_y_db": 0.5,
    },
    "production-q25": {
        "min_target_gain_psnr_y_db": 0.6,
        "min_target_background_selectivity_psnr_y_db": 0.9,
    },
}
EXPECTED_COMMON_THRESHOLDS = {
    "max_background_loss_psnr_y_db": 2.0,
    "min_q25_over_q15_psnr_y_db": 0.2,
    "max_initial_clear_absolute_psnr_y_db": 0.5,
    "max_initial_clear_spatial_psnr_y_db": 0.3,
    "max_clear_spatial_psnr_y_db": 0.5,
    "max_cbr_size_delta_ratio": 0.1,
    "max_cbr_target_delta_ratio": 0.1,
}


class ProductionAnalysisError(RuntimeError):
    """The production-matrix evidence or schedule is inconsistent."""


def require_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProductionAnalysisError(f"{name} must be an integer >= {minimum}")
    return value


def require_number(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductionAnalysisError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ProductionAnalysisError(f"{name} must be finite and >= {minimum}")
    return result


def require_exact_fields(value: dict[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ",".join(missing))
        if unknown:
            details.append("unknown " + ",".join(unknown))
        raise ProductionAnalysisError(f"{name} fields are invalid: {'; '.join(details)}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionAnalysisError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProductionAnalysisError(f"expected a JSON object in {path}")
    return value


def validate_rect(
    value: Any, name: str, width: int, height: int, *, with_qoffset: bool
) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise ProductionAnalysisError(f"{name} must be an object")
    fields = {"x", "y", "w", "h", "qoffset"} if with_qoffset else {
        "x",
        "y",
        "w",
        "h",
    }
    require_exact_fields(value, fields, name)
    result: dict[str, int | float] = {
        "x": require_int(value["x"], f"{name}.x"),
        "y": require_int(value["y"], f"{name}.y"),
        "w": require_int(value["w"], f"{name}.w", 1),
        "h": require_int(value["h"], f"{name}.h", 1),
    }
    if int(result["x"]) + int(result["w"]) > width or int(result["y"]) + int(
        result["h"]
    ) > height:
        raise ProductionAnalysisError(f"{name} is outside the encoded frame")
    if with_qoffset:
        qoffset = require_number(value["qoffset"], f"{name}.qoffset", -1.0)
        if qoffset > 1.0:
            raise ProductionAnalysisError(f"{name}.qoffset must be <= 1")
        result["qoffset"] = qoffset
    return result


def contains(outer: dict[str, int | float], inner: dict[str, int | float]) -> bool:
    return (
        int(inner["x"]) >= int(outer["x"])
        and int(inner["y"]) >= int(outer["y"])
        and int(inner["x"]) + int(inner["w"])
        <= int(outer["x"]) + int(outer["w"])
        and int(inner["y"]) + int(inner["h"])
        <= int(outer["y"]) + int(outer["h"])
    )


def overlaps(first: dict[str, int | float], second: dict[str, int | float]) -> bool:
    return (
        int(first["x"]) < int(second["x"]) + int(second["w"])
        and int(first["x"]) + int(first["w"]) > int(second["x"])
        and int(first["y"]) < int(second["y"]) + int(second["h"])
        and int(first["y"]) + int(first["h"]) > int(second["y"])
    )


def rect_area(rect: dict[str, int | float]) -> int:
    return int(rect["w"]) * int(rect["h"])


def separation(first: dict[str, int | float], second: dict[str, int | float]) -> int:
    horizontal = max(
        int(second["x"]) - (int(first["x"]) + int(first["w"])),
        int(first["x"]) - (int(second["x"]) + int(second["w"])),
        0,
    )
    vertical = max(
        int(second["y"]) - (int(first["y"]) + int(first["h"])),
        int(first["y"]) - (int(second["y"]) + int(second["h"])),
        0,
    )
    return max(horizontal, vertical)


def validate_thresholds(
    value: Any, identity: str, expected_qoffset: float
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionAnalysisError("thresholds must be an object")
    require_exact_fields(value, THRESHOLD_FIELDS, "thresholds")
    if value["identity"] != identity:
        raise ProductionAnalysisError(
            f"thresholds.identity must be {identity!r}, got {value['identity']!r}"
        )
    actual_qoffset = require_number(
        value["expected_qoffset"], "thresholds.expected_qoffset", -1.0
    )
    if not math.isclose(actual_qoffset, expected_qoffset, abs_tol=1e-12):
        raise ProductionAnalysisError(
            f"thresholds.expected_qoffset must be {expected_qoffset}"
        )
    normalized = {
        "identity": identity,
        "expected_qoffset": actual_qoffset,
    }
    for field in sorted(THRESHOLD_FIELDS - {"identity", "expected_qoffset"}):
        normalized[field] = require_number(value[field], f"thresholds.{field}")
    expected_values = {
        **EXPECTED_COMMON_THRESHOLDS,
        **EXPECTED_PROFILE_THRESHOLDS[identity],
    }
    for field, expected in expected_values.items():
        if not math.isclose(normalized[field], expected, abs_tol=1e-12):
            raise ProductionAnalysisError(
                f"thresholds.{field} must be preregistered as {expected} for {identity}"
            )
    return normalized


def validate_schedule(
    schedule: dict[str, Any], identity: str, expected_qoffset: float
) -> dict[str, Any]:
    if schedule.get("schema_version") != 1:
        raise ProductionAnalysisError("schedule schema_version must be 1")
    expected_name = f"rk3588-{identity}"
    if schedule.get("name") != expected_name:
        raise ProductionAnalysisError(f"schedule.name must be {expected_name!r}")

    frame = schedule.get("frame")
    if not isinstance(frame, dict):
        raise ProductionAnalysisError("frame must be an object")
    require_exact_fields(frame, {"width", "height", "fps"}, "frame")
    width = require_int(frame["width"], "frame.width", 1)
    height = require_int(frame["height"], "frame.height", 1)
    fps = require_number(frame["fps"], "frame.fps", 0.001)
    if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise ProductionAnalysisError("production matrix must use 1920x1080")
    if not math.isclose(fps, EXPECTED_FPS, abs_tol=1e-12):
        raise ProductionAnalysisError("production matrix must use 30 fps")
    if require_int(schedule.get("gop_frames"), "gop_frames", 1) != EXPECTED_GOP:
        raise ProductionAnalysisError("production matrix must use GOP 60")
    if not math.isclose(
        require_number(schedule.get("repeat_hz"), "repeat_hz", 0.001),
        10.0,
        abs_tol=1e-12,
    ):
        raise ProductionAnalysisError("production matrix must send at 10 Hz")
    bitrate = require_int(
        schedule.get("target_bitrate_bps"), "target_bitrate_bps", 1
    )
    if bitrate != EXPECTED_BITRATE_BPS:
        raise ProductionAnalysisError("production matrix must use CBR 4 Mbps")

    probes_value = schedule.get("probes")
    if not isinstance(probes_value, dict) or not probes_value:
        raise ProductionAnalysisError("probes must be a non-empty object")
    probes: dict[str, dict[str, int | float]] = {}
    for name, value in probes_value.items():
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ProductionAnalysisError(f"invalid probe name: {name!r}")
        probes[name] = validate_rect(
            value, f"probes.{name}", width, height, with_qoffset=False
        )

    thresholds = validate_thresholds(
        schedule.get("thresholds"), identity, expected_qoffset
    )
    phases_value = schedule.get("phases")
    if not isinstance(phases_value, list) or not phases_value:
        raise ProductionAnalysisError("phases must be a non-empty array")

    expected_start = 0
    normalized_phases: list[dict[str, Any]] = []
    region_counts: list[int] = []
    active_counts: list[int] = []
    clear_roles: list[str] = []
    phase_names: set[str] = set()
    for phase_index, phase in enumerate(phases_value):
        if not isinstance(phase, dict):
            raise ProductionAnalysisError(f"phase {phase_index} must be an object")
        require_exact_fields(
            phase,
            {"name", "start_frame", "end_frame", "regions", "measure"},
            f"phase {phase_index}",
        )
        name = phase["name"]
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ProductionAnalysisError(f"phase {phase_index} has an invalid name")
        if name in phase_names:
            raise ProductionAnalysisError(f"duplicate phase name: {name}")
        phase_names.add(name)
        start = require_int(phase["start_frame"], f"phases.{name}.start_frame")
        end = require_int(phase["end_frame"], f"phases.{name}.end_frame", 1)
        if start != expected_start or end <= start:
            raise ProductionAnalysisError(
                f"phase {name} must be contiguous and use [start_frame,end_frame)"
            )
        expected_start = end

        regions_value = phase["regions"]
        if not isinstance(regions_value, list) or len(regions_value) > 8:
            raise ProductionAnalysisError(f"phase {name}.regions must contain 0..8 items")
        regions = [
            validate_rect(
                region,
                f"phases.{name}.regions.{index}",
                width,
                height,
                with_qoffset=True,
            )
            for index, region in enumerate(regions_value)
        ]
        for index, region in enumerate(regions):
            if any(int(region[field]) % 16 for field in ("x", "y", "w", "h")):
                raise ProductionAnalysisError(
                    f"phase {name} region {index} geometry must be 16-pixel aligned"
                )
            if not math.isclose(
                float(region["qoffset"]), expected_qoffset, abs_tol=1e-12
            ):
                raise ProductionAnalysisError(
                    f"phase {name} region {index} must use qoffset {expected_qoffset}"
                )
            if float(region["qoffset"]) >= 0:
                raise ProductionAnalysisError("production ROI qoffset must be negative")
            for previous in regions[:index]:
                if overlaps(previous, region):
                    raise ProductionAnalysisError(
                        f"phase {name} contains overlapping regions"
                    )
                if len(regions) > 1 and separation(previous, region) < MIN_REGION_SEPARATION:
                    raise ProductionAnalysisError(
                        f"phase {name} regions must be separated by at least "
                        f"{MIN_REGION_SEPARATION} pixels"
                    )
        region_counts.append(len(regions))

        measure = phase["measure"]
        if not isinstance(measure, dict):
            raise ProductionAnalysisError(f"phase {name}.measure must be an object")
        kind = measure.get("kind")
        fields = {
            "kind",
            "start_frame",
            "end_frame",
            "target_probes",
            "background_probe",
        }
        if kind == "clear":
            fields.add("role")
        elif kind != "active":
            raise ProductionAnalysisError(f"phase {name} has invalid measure.kind")
        require_exact_fields(measure, fields, f"phases.{name}.measure")
        measure_start = require_int(
            measure["start_frame"], f"phases.{name}.measure.start_frame"
        )
        measure_end = require_int(
            measure["end_frame"], f"phases.{name}.measure.end_frame", 1
        )
        if measure_start != start + EXPECTED_GOP or measure_end != end:
            raise ProductionAnalysisError(
                f"phase {name} must skip exactly one GOP before measuring to phase end"
            )
        target_names = measure["target_probes"]
        if (
            not isinstance(target_names, list)
            or not target_names
            or any(not isinstance(item, str) for item in target_names)
            or len(set(target_names)) != len(target_names)
        ):
            raise ProductionAnalysisError(
                f"phase {name}.target_probes must contain distinct probe names"
            )
        background_name = measure["background_probe"]
        if background_name not in probes or any(item not in probes for item in target_names):
            raise ProductionAnalysisError(f"phase {name} references an unknown probe")
        target_area = sum(rect_area(probes[item]) for item in target_names)
        if target_area != EXPECTED_TARGET_PROBE_AREA:
            raise ProductionAnalysisError(
                f"phase {name} target probe area is {target_area}; "
                f"expected {EXPECTED_TARGET_PROBE_AREA}"
            )

        if kind == "active":
            if len(regions) not in EXPECTED_ACTIVE_COUNTS:
                raise ProductionAnalysisError(f"phase {name} must contain 1, 2, or 8 ROIs")
            if len(target_names) != len(regions):
                raise ProductionAnalysisError(
                    f"phase {name} must use one target probe per ROI"
                )
            roi_area = sum(rect_area(region) for region in regions)
            if roi_area != EXPECTED_ROI_AREA:
                raise ProductionAnalysisError(
                    f"phase {name} ROI area is {roi_area}; expected {EXPECTED_ROI_AREA}"
                )
            for region, target_name in zip(regions, target_names):
                if not contains(region, probes[target_name]):
                    raise ProductionAnalysisError(
                        f"phase {name} target probe {target_name} is outside its ROI"
                    )
            if any(overlaps(region, probes[background_name]) for region in regions):
                raise ProductionAnalysisError(
                    f"phase {name} background probe overlaps an ROI"
                )
            active_counts.append(len(regions))
        else:
            if regions:
                raise ProductionAnalysisError(f"clear phase {name} must use regions: []")
            role = measure["role"]
            if role not in {"initial", "intermediate", "final"}:
                raise ProductionAnalysisError(f"phase {name} has invalid clear role")
            clear_roles.append(role)

        normalized_phases.append(
            {
                "name": name,
                "kind": kind,
                "role": measure.get("role"),
                "start_frame": measure_start,
                "end_frame": measure_end,
                "target_probes": target_names,
                "background_probe": background_name,
                "region_count": len(regions),
            }
        )

    if expected_start != EXPECTED_FRAMES:
        raise ProductionAnalysisError(
            f"production schedule must contain {EXPECTED_FRAMES} frames"
        )
    if region_counts != EXPECTED_REGION_COUNTS:
        raise ProductionAnalysisError(
            f"region-count timeline must be {EXPECTED_REGION_COUNTS}; got {region_counts}"
        )
    if active_counts != EXPECTED_ACTIVE_COUNTS:
        raise ProductionAnalysisError(
            f"active region counts must be {EXPECTED_ACTIVE_COUNTS}; got {active_counts}"
        )
    if clear_roles != EXPECTED_CLEAR_ROLES:
        raise ProductionAnalysisError(
            f"clear roles must be {EXPECTED_CLEAR_ROLES}; got {clear_roles}"
        )
    return {
        "identity": identity,
        "qoffset": expected_qoffset,
        "width": width,
        "height": height,
        "fps": fps,
        "gop_frames": EXPECTED_GOP,
        "target_bitrate_bps": bitrate,
        "expected_frames": expected_start,
        "probes": probes,
        "thresholds": thresholds,
        "phases": normalized_phases,
    }


def schedule_shape(schedule: dict[str, Any]) -> dict[str, Any]:
    """Compare timing/geometry while allowing preregistered profile thresholds."""

    result = copy.deepcopy(schedule)
    result["name"] = "rk3588-production-qXX"
    result["thresholds"] = {
        field: "<profile-threshold>" for field in sorted(THRESHOLD_FIELDS)
    }
    for phase in result["phases"]:
        for region in phase["regions"]:
            region["qoffset"] = -0.0
    return result


def validate_schedule_pair(
    q15_raw: dict[str, Any], q25_raw: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    q15 = validate_schedule(q15_raw, "production-q15", -0.15)
    q25 = validate_schedule(q25_raw, "production-q25", -0.25)
    if schedule_shape(q15_raw) != schedule_shape(q25_raw):
        raise ProductionAnalysisError(
            "q15/q25 schedules differ outside profile identity and qoffset"
        )
    return q15, q25


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ProductionAnalysisError("cannot calculate a percentile of no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        raise ProductionAnalysisError("cannot summarize an empty metric window")
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p25": percentile(values, 0.25),
        "minimum": min(values),
        "maximum": max(values),
    }


def aggregate_probe_deltas(
    metric_values: dict[str, dict[str, dict[str, list[float]]]],
    mode: str,
    probes: list[str],
    metric: str,
    start: int,
    end: int,
) -> list[float]:
    result: list[float] = []
    for frame in range(start, end):
        result.append(
            statistics.fmean(
                metric_values[mode][probe][metric][frame]
                - metric_values["baseline"][probe][metric][frame]
                for probe in probes
            )
        )
    return result


def active_probe_report(
    target_psnr: list[float],
    target_ssim: list[float],
    background_psnr: list[float],
    background_ssim: list[float],
) -> dict[str, Any]:
    selectivity_psnr = [
        target - background
        for target, background in zip(target_psnr, background_psnr)
    ]
    selectivity_ssim = [
        target - background
        for target, background in zip(target_ssim, background_ssim)
    ]
    return {
        "target_psnr_y_db": summarize(target_psnr),
        "target_ssim_y": summarize(target_ssim),
        "target_background_selectivity_psnr_y_db": summarize(selectivity_psnr),
        "target_background_selectivity_ssim_y": summarize(selectivity_ssim),
    }


def active_probe_passes(report: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, bool]:
    return {
        "target_gain": report["target_psnr_y_db"]["median"]
        >= thresholds["min_target_gain_psnr_y_db"],
        "selectivity": report["target_background_selectivity_psnr_y_db"]["median"]
        >= thresholds["min_target_background_selectivity_psnr_y_db"],
    }


def collapsed_counts(counts: list[int]) -> list[int]:
    result: list[int] = []
    for count in counts:
        if not result or result[-1] != count:
            result.append(count)
    return result


def read_progress_records(path: Path) -> list[dict[str, int | str | None]]:
    try:
        text = path.read_bytes().decode("ascii", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ProductionAnalysisError(f"cannot read FFmpeg progress {path}: {exc}") from exc

    records: list[dict[str, int | str | None]] = []
    out_time_us: int | None = None
    out_time_seen = False
    found_end = False
    for line_number, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if len(line) > MAX_PROGRESS_LINE_BYTES:
            raise ProductionAnalysisError(
                f"FFmpeg progress line exceeds {MAX_PROGRESS_LINE_BYTES} bytes "
                f"at {path}:{line_number}"
            )
        if not line:
            continue
        if "=" not in line:
            raise ProductionAnalysisError(
                f"invalid FFmpeg progress line at {path}:{line_number}: {line!r}"
            )
        key, value = line.split("=", 1)
        if not key:
            raise ProductionAnalysisError(
                f"empty FFmpeg progress key at {path}:{line_number}"
            )
        if key == "out_time_us":
            if out_time_seen:
                raise ProductionAnalysisError(
                    f"duplicate out_time_us in one progress record at {path}:{line_number}"
                )
            out_time_seen = True
            if value == "N/A":
                out_time_us = None
            elif PROGRESS_INTEGER_PATTERN.fullmatch(value):
                out_time_us = int(value, 10)
                if not PROGRESS_INT64_MIN <= out_time_us <= PROGRESS_INT64_MAX:
                    raise ProductionAnalysisError(
                        f"out_time_us is outside int64 at {path}:{line_number}"
                    )
            else:
                raise ProductionAnalysisError(
                    f"invalid out_time_us at {path}:{line_number}: {value!r}"
                )
        elif key == "progress":
            if value not in {"continue", "end"}:
                raise ProductionAnalysisError(
                    f"invalid progress status at {path}:{line_number}: {value!r}"
                )
            records.append({"status": value, "out_time_us": out_time_us})
            out_time_us = None
            out_time_seen = False
            if value == "end":
                found_end = True
                break
    if not found_end:
        raise ProductionAnalysisError(f"FFmpeg progress does not end with progress=end: {path}")
    return records


def frame_for_progress(out_time_us: int, fps: Fraction) -> int:
    if out_time_us <= 0:
        return 0
    numerator = out_time_us * fps.numerator
    denominator = 1_000_000 * fps.denominator
    return (2 * numerator + denominator) // (2 * denominator)


def phase_index_for_progress(schedule: dict[str, Any], frame: int) -> int | None:
    for index, phase in enumerate(schedule["phases"]):
        if phase["start_frame"] <= frame < phase["end_frame"]:
            return index
    return None


def terminal_repeat_is_guarded(
    schedule: dict[str, Any], frame: int, reason: str
) -> bool:
    fps = Fraction(str(schedule["frame"]["fps"]))
    repeat_hz = Fraction(str(schedule["repeat_hz"]))
    guard_frames = math.ceil(TERMINAL_REPEAT_GUARD_SLOTS * fps / repeat_hz)
    total_frames = schedule["phases"][-1]["end_frame"]
    return reason == "repeat" and frame >= total_frames - guard_frames


def replay_progress(
    path: Path,
    schedule: dict[str, Any],
    force_empty: bool,
    *,
    enforce_coverage: bool = True,
) -> dict[str, Any]:
    fps = Fraction(str(schedule["frame"]["fps"]))
    repeat_hz = Fraction(str(schedule["repeat_hz"]))
    phases = schedule["phases"]
    events = [
        {
            "out_time_us": 0,
            "frame": 0,
            "phase": phases[0]["name"],
            "phase_index": 0,
            "reason": "initial",
            "region_count": 0 if force_empty else len(phases[0]["regions"]),
        }
    ]
    current_phase_index = 0
    last_repeat_slot = 0
    previous_out_time_us: int | None = None
    records = read_progress_records(path)
    for record in records:
        if record["status"] == "end":
            break
        value = record["out_time_us"]
        if value is None:
            continue
        out_time_us = int(value)
        if previous_out_time_us is not None and out_time_us < previous_out_time_us:
            raise ProductionAnalysisError(
                f"FFmpeg progress time moved backwards in {path}: "
                f"{previous_out_time_us} -> {out_time_us}"
            )
        previous_out_time_us = out_time_us
        frame = frame_for_progress(out_time_us, fps)
        phase_index = phase_index_for_progress(schedule, frame)
        if phase_index is None:
            raise ProductionAnalysisError(
                f"FFmpeg progress {path} reached frame {frame} outside the schedule"
            )
        repeat_slot = (
            max(0, out_time_us) * repeat_hz.numerator
        ) // (1_000_000 * repeat_hz.denominator)
        if phase_index != current_phase_index:
            current_phase_index = phase_index
            reason = "phase_change"
            last_repeat_slot = repeat_slot
        elif repeat_slot > last_repeat_slot:
            reason = "repeat"
            last_repeat_slot = repeat_slot
            if terminal_repeat_is_guarded(schedule, frame, reason):
                continue
        else:
            continue
        phase = phases[phase_index]
        events.append(
            {
                "out_time_us": out_time_us,
                "frame": frame,
                "phase": phase["name"],
                "phase_index": phase_index,
                "reason": reason,
                "region_count": 0 if force_empty else len(phase["regions"]),
            }
        )

    nominal_per_phase: dict[str, int] = {}
    observed_per_phase = {phase["name"]: 0 for phase in phases}
    for phase in phases:
        nominal = Fraction(
            phase["end_frame"] - phase["start_frame"], 1
        ) * repeat_hz / fps
        if nominal.denominator != 1:
            raise ProductionAnalysisError(
                f"phase {phase['name']} has a non-integral nominal send count"
            )
        nominal_per_phase[phase["name"]] = nominal.numerator
    for event in events:
        observed_per_phase[str(event["phase"])] += 1

    coverage_per_phase: dict[str, dict[str, int | float]] = {}
    for index, phase in enumerate(phases):
        name = phase["name"]
        nominal = nominal_per_phase[name]
        minimum_fraction = (
            MIN_INITIAL_SEND_COVERAGE if index == 0 else MIN_PHASE_SEND_COVERAGE
        )
        minimum = math.ceil(nominal * minimum_fraction)
        observed = observed_per_phase[name]
        if enforce_coverage and observed < minimum:
            raise ProductionAnalysisError(
                f"progress-observable sender coverage for phase {name} is "
                f"{observed}/{nominal}; minimum is {minimum}/{nominal}"
            )
        coverage_per_phase[name] = {
            "observed": observed,
            "nominal": nominal,
            "minimum": minimum,
            "ratio": observed / nominal,
        }
    nominal_total = sum(nominal_per_phase.values())
    minimum_total = math.ceil(nominal_total * MIN_TOTAL_SEND_COVERAGE)
    observed_total = len(events)
    if enforce_coverage and observed_total < minimum_total:
        raise ProductionAnalysisError(
            f"progress-observable total sender coverage is {observed_total}/"
            f"{nominal_total}; minimum is {minimum_total}/{nominal_total}"
        )
    return {
        "path": str(path),
        "sha256": common.sha256(path),
        "records": len(records),
        "events": events,
        "coverage": {
            "observed": observed_total,
            "nominal": nominal_total,
            "minimum": minimum_total,
            "ratio": observed_total / nominal_total,
            "per_phase": coverage_per_phase,
        },
    }


def strict_event_summary(
    path: Path,
    progress_path: Path,
    schedule: dict[str, Any],
    force_empty: bool,
    expected_counts: set[int],
    expected_transition: list[int],
) -> dict[str, Any]:
    summary = common.validate_event_log(path, schedule, force_empty)
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionAnalysisError(f"cannot re-read event evidence {path}: {exc}") from exc
    frames = [event["frame"] for event in events]
    phase_indices = [event["phase_index"] for event in events]
    out_times = [event["out_time_us"] for event in events]
    if frames != sorted(frames) or phase_indices != sorted(phase_indices):
        raise ProductionAnalysisError(f"event phase/frame order moved backwards: {path}")
    if out_times != sorted(out_times):
        raise ProductionAnalysisError(f"event media time moved backwards: {path}")
    observed_counts = {event["region_count"] for event in events}
    if observed_counts != expected_counts:
        raise ProductionAnalysisError(
            f"event region counts in {path} are {sorted(observed_counts)}; "
            f"expected {sorted(expected_counts)}"
        )
    ordered_counts = [event["region_count"] for event in events]
    transition = collapsed_counts(ordered_counts)
    if transition != expected_transition:
        raise ProductionAnalysisError(
            f"event region-count transition in {path} is {transition}; "
            f"expected {expected_transition}"
        )

    progress = replay_progress(progress_path, schedule, force_empty)
    signature_fields = (
        "out_time_us",
        "frame",
        "phase",
        "phase_index",
        "reason",
        "region_count",
    )
    actual_signatures = [
        {field: event[field] for field in signature_fields} for event in events
    ]
    expected_signatures = progress.pop("events")
    if actual_signatures != expected_signatures:
        mismatch = next(
            (
                index
                for index, (actual, expected) in enumerate(
                    zip(actual_signatures, expected_signatures), 1
                )
                if actual != expected
            ),
            min(len(actual_signatures), len(expected_signatures)) + 1,
        )
        raise ProductionAnalysisError(
            f"sender events in {path} do not match progress replay at send {mismatch}: "
            f"events={len(actual_signatures)}, replay={len(expected_signatures)}"
        )
    return {
        **summary,
        "observed_region_counts": sorted(observed_counts),
        "region_count_transition": transition,
        "ordered_region_counts": ordered_counts,
        "progress_replay": progress,
    }


def validate_encode_log(path: Path, expected_counts: list[int]) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProductionAnalysisError(f"cannot read encoder log {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        for pattern in FORBIDDEN_ROI_LOG_PATTERNS:
            if pattern.search(line):
                raise ProductionAnalysisError(
                    f"encoder log {path}:{line_number} contains a forbidden ROI error: {line}"
                )
    applied = []
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
        raise ProductionAnalysisError(f"encoder log contains no Applied ROI updates: {path}")
    generations = [item["generation"] for item in applied]
    expected_generations = list(range(1, len(applied) + 1))
    if generations != expected_generations:
        raise ProductionAnalysisError(
            f"Applied ROI generations in {path} are not exactly 1..N: {generations}"
        )
    applied_counts = [item["region_count"] for item in applied]
    if applied_counts != expected_counts:
        mismatch = next(
            (
                index
                for index, (actual, expected) in enumerate(
                    zip(applied_counts, expected_counts), 1
                )
                if actual != expected
            ),
            min(len(applied_counts), len(expected_counts)) + 1,
        )
        raise ProductionAnalysisError(
            f"Applied ROI count sequence in {path} differs from sender event "
            f"sequence at generation {mismatch}: applied={len(applied_counts)}, "
            f"sent={len(expected_counts)}"
        )
    return {
        "path": str(path),
        "sha256": common.sha256(path),
        "applied_updates": len(applied),
        "first_generation": generations[0],
        "last_generation": generations[-1],
        "region_count_transition": collapsed_counts(applied_counts),
    }


def calculate_cbr(
    paths: dict[str, Path], target_bitrate_bps: int, frames: int, fps: float
) -> dict[str, Any]:
    duration = frames / fps
    theoretical = target_bitrate_bps * duration / 8.0
    sizes = {name: path.stat().st_size for name, path in paths.items()}
    baseline_size = sizes["baseline"]
    profiles: dict[str, Any] = {}
    for name, size in sizes.items():
        profiles[name] = {
            "bytes": size,
            "bitrate_bps": size * 8.0 / duration,
            "target_delta_ratio": abs(size - theoretical) / theoretical,
            "baseline_size_delta_ratio": abs(size - baseline_size) / baseline_size,
        }
    return {
        "duration_seconds": duration,
        "theoretical_bytes": theoretical,
        "profiles": profiles,
    }


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, **data: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), **data})


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    q15_raw = load_json(args.q15_schedule)
    q25_raw = load_json(args.q25_schedule)
    q15, q25 = validate_schedule_pair(q15_raw, q25_raw)
    for path in (
        args.ffmpeg,
        args.source,
        args.baseline,
        args.q15,
        args.q25,
        args.baseline_events,
        args.q15_events,
        args.q25_events,
        args.baseline_progress,
        args.q15_progress,
        args.q25_progress,
        args.baseline_encode_log,
        args.q15_encode_log,
        args.q25_encode_log,
    ):
        if not path.is_file():
            raise ProductionAnalysisError(f"required file does not exist: {path}")
    if not SAFE_PATH.fullmatch(str(args.output_dir)):
        raise ProductionAnalysisError(
            "output directory may contain only letters, digits, _, -, ., and /"
        )
    if args.output_dir.exists():
        raise ProductionAnalysisError(f"output directory already exists: {args.output_dir}")

    sender_events = {
        "baseline": strict_event_summary(
            args.baseline_events,
            args.baseline_progress,
            q15_raw,
            True,
            {0},
            [0],
        ),
        "q15": strict_event_summary(
            args.q15_events,
            args.q15_progress,
            q15_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        ),
        "q25": strict_event_summary(
            args.q25_events,
            args.q25_progress,
            q25_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        ),
    }
    encode_logs = {
        "baseline": validate_encode_log(
            args.baseline_encode_log,
            sender_events["baseline"]["ordered_region_counts"],
        ),
        "q15": validate_encode_log(
            args.q15_encode_log, sender_events["q15"]["ordered_region_counts"]
        ),
        "q25": validate_encode_log(
            args.q25_encode_log, sender_events["q25"]["ordered_region_counts"]
        ),
    }

    args.output_dir.mkdir(parents=True)
    metrics_dir = args.output_dir / "metrics"
    metrics_dir.mkdir()
    metric_schedule = {
        "probes": q15["probes"],
        "expected_frames": q15["expected_frames"],
        "fps": q15["fps"],
    }
    encoded_paths = {
        "baseline": args.baseline,
        "q15": args.q15,
        "q25": args.q25,
    }
    for mode, encoded in encoded_paths.items():
        common.run_metrics(
            args.ffmpeg,
            args.source,
            encoded,
            args.format,
            metric_schedule,
            metrics_dir,
            mode,
        )

    metric_values: dict[str, dict[str, dict[str, list[float]]]] = {}
    for mode in encoded_paths:
        metric_values[mode] = {}
        for probe in q15["probes"]:
            metric_values[mode][probe] = {
                "psnr_y": common.parse_metric(
                    metrics_dir / f"{mode}-{probe}.psnr",
                    common.PSNR_PATTERN,
                    q15["expected_frames"],
                    common.PSNR_INFINITY_CAP_DB,
                ),
                "ssim_y": common.parse_metric(
                    metrics_dir / f"{mode}-{probe}.ssim",
                    common.SSIM_PATTERN,
                    q15["expected_frames"],
                ),
            }

    checks: list[dict[str, Any]] = []
    active_reports: list[dict[str, Any]] = []
    clear_reports: list[dict[str, Any]] = []
    profile_thresholds = {
        "q15": q15["thresholds"],
        "q25": q25["thresholds"],
    }
    for phase in q15["phases"]:
        start = phase["start_frame"]
        end = phase["end_frame"]
        targets = phase["target_probes"]
        background = phase["background_probe"]
        if phase["kind"] == "active":
            profile_reports: dict[str, Any] = {}
            target_series: dict[str, list[float]] = {}
            probe_target_series: dict[str, dict[str, list[float]]] = {
                "q15": {},
                "q25": {},
            }
            for mode in ("q15", "q25"):
                thresholds = profile_thresholds[mode]
                target_psnr = aggregate_probe_deltas(
                    metric_values, mode, targets, "psnr_y", start, end
                )
                target_ssim = aggregate_probe_deltas(
                    metric_values, mode, targets, "ssim_y", start, end
                )
                background_psnr = aggregate_probe_deltas(
                    metric_values, mode, [background], "psnr_y", start, end
                )
                background_ssim = aggregate_probe_deltas(
                    metric_values, mode, [background], "ssim_y", start, end
                )
                selectivity_psnr = [
                    target - background_value
                    for target, background_value in zip(target_psnr, background_psnr)
                ]
                selectivity_ssim = [
                    target - background_value
                    for target, background_value in zip(target_ssim, background_ssim)
                ]
                target_summary = summarize(target_psnr)
                background_summary = summarize(background_psnr)
                selectivity_summary = summarize(selectivity_psnr)
                probe_reports: dict[str, Any] = {}
                for probe in targets:
                    probe_psnr = aggregate_probe_deltas(
                        metric_values, mode, [probe], "psnr_y", start, end
                    )
                    probe_ssim = aggregate_probe_deltas(
                        metric_values, mode, [probe], "ssim_y", start, end
                    )
                    probe_report = active_probe_report(
                        probe_psnr,
                        probe_ssim,
                        background_psnr,
                        background_ssim,
                    )
                    probe_passes = active_probe_passes(probe_report, thresholds)
                    probe_reports[probe] = probe_report
                    probe_target_series[mode][probe] = probe_psnr
                    add_check(
                        checks,
                        f"{phase['name']}:{mode}:{probe}:target-gain",
                        probe_passes["target_gain"],
                        actual_db=probe_report["target_psnr_y_db"]["median"],
                        minimum_db=thresholds["min_target_gain_psnr_y_db"],
                    )
                    add_check(
                        checks,
                        f"{phase['name']}:{mode}:{probe}:target-background-selectivity",
                        probe_passes["selectivity"],
                        actual_db=probe_report[
                            "target_background_selectivity_psnr_y_db"
                        ]["median"],
                        minimum_db=thresholds[
                            "min_target_background_selectivity_psnr_y_db"
                        ],
                    )
                profile_reports[mode] = {
                    "target_psnr_y_db": target_summary,
                    "target_ssim_y": summarize(target_ssim),
                    "background_psnr_y_db": background_summary,
                    "background_ssim_y": summarize(background_ssim),
                    "target_background_selectivity_psnr_y_db": selectivity_summary,
                    "target_background_selectivity_ssim_y": summarize(selectivity_ssim),
                    "target_probes": probe_reports,
                }
                target_series[mode] = target_psnr
                add_check(
                    checks,
                    f"{phase['name']}:{mode}:target-gain",
                    target_summary["median"]
                    >= thresholds["min_target_gain_psnr_y_db"],
                    actual_db=target_summary["median"],
                    minimum_db=thresholds["min_target_gain_psnr_y_db"],
                )
                add_check(
                    checks,
                    f"{phase['name']}:{mode}:target-background-selectivity",
                    selectivity_summary["median"]
                    >= thresholds[
                        "min_target_background_selectivity_psnr_y_db"
                    ],
                    actual_db=selectivity_summary["median"],
                    minimum_db=thresholds[
                        "min_target_background_selectivity_psnr_y_db"
                    ],
                )
                add_check(
                    checks,
                    f"{phase['name']}:{mode}:background-loss",
                    background_summary["median"]
                    >= -thresholds["max_background_loss_psnr_y_db"],
                    actual_db=background_summary["median"],
                    minimum_db=-thresholds["max_background_loss_psnr_y_db"],
                )
            q25_over_q15 = summarize(
                [
                    q25_value - q15_value
                    for q25_value, q15_value in zip(
                        target_series["q25"], target_series["q15"]
                    )
                ]
            )
            add_check(
                checks,
                f"{phase['name']}:q25-over-q15",
                q25_over_q15["median"]
                >= max(
                    profile_thresholds["q15"]["min_q25_over_q15_psnr_y_db"],
                    profile_thresholds["q25"]["min_q25_over_q15_psnr_y_db"],
                ),
                actual_db=q25_over_q15["median"],
                minimum_db=max(
                    profile_thresholds["q15"]["min_q25_over_q15_psnr_y_db"],
                    profile_thresholds["q25"]["min_q25_over_q15_psnr_y_db"],
                ),
            )
            probe_q25_over_q15: dict[str, Any] = {}
            q25_over_q15_minimum = max(
                profile_thresholds["q15"]["min_q25_over_q15_psnr_y_db"],
                profile_thresholds["q25"]["min_q25_over_q15_psnr_y_db"],
            )
            for probe in targets:
                probe_cross = summarize(
                    [
                        q25_value - q15_value
                        for q25_value, q15_value in zip(
                            probe_target_series["q25"][probe],
                            probe_target_series["q15"][probe],
                        )
                    ]
                )
                probe_q25_over_q15[probe] = probe_cross
                add_check(
                    checks,
                    f"{phase['name']}:{probe}:q25-over-q15",
                    probe_cross["median"] >= q25_over_q15_minimum,
                    actual_db=probe_cross["median"],
                    minimum_db=q25_over_q15_minimum,
                )
            active_reports.append(
                {
                    **phase,
                    "profiles": profile_reports,
                    "q25_over_q15_target_psnr_y_db": q25_over_q15,
                    "probe_q25_over_q15_target_psnr_y_db": probe_q25_over_q15,
                }
            )
            continue

        profile_reports = {}
        for mode in ("q15", "q25"):
            thresholds = profile_thresholds[mode]
            target_psnr = aggregate_probe_deltas(
                metric_values, mode, targets, "psnr_y", start, end
            )
            background_psnr = aggregate_probe_deltas(
                metric_values, mode, [background], "psnr_y", start, end
            )
            target_summary = summarize(target_psnr)
            background_summary = summarize(background_psnr)
            spatial = statistics.fmean(target_psnr) - statistics.fmean(background_psnr)
            absolute = max(abs(target_summary["mean"]), abs(background_summary["mean"]))
            profile_reports[mode] = {
                "target_psnr_y_db": target_summary,
                "background_psnr_y_db": background_summary,
                "spatial_residual_psnr_y_db": abs(spatial),
                "max_absolute_psnr_y_db": absolute,
            }
            if phase["role"] == "initial":
                add_check(
                    checks,
                    f"{phase['name']}:{mode}:absolute-clear",
                    absolute
                    <= thresholds["max_initial_clear_absolute_psnr_y_db"],
                    actual_db=absolute,
                    maximum_db=thresholds[
                        "max_initial_clear_absolute_psnr_y_db"
                    ],
                )
                spatial_limit = thresholds["max_initial_clear_spatial_psnr_y_db"]
            else:
                spatial_limit = thresholds["max_clear_spatial_psnr_y_db"]
            add_check(
                checks,
                f"{phase['name']}:{mode}:spatial-clear",
                abs(spatial) <= spatial_limit,
                actual_db=abs(spatial),
                maximum_db=spatial_limit,
            )
        clear_reports.append({**phase, "profiles": profile_reports})

    cbr = calculate_cbr(
        encoded_paths,
        q15["target_bitrate_bps"],
        q15["expected_frames"],
        q15["fps"],
    )
    for mode, profile in cbr["profiles"].items():
        thresholds = profile_thresholds["q15" if mode == "baseline" else mode]
        add_check(
            checks,
            f"cbr:{mode}:target-delta",
            profile["target_delta_ratio"]
            <= thresholds["max_cbr_target_delta_ratio"],
            actual_ratio=profile["target_delta_ratio"],
            maximum_ratio=thresholds["max_cbr_target_delta_ratio"],
        )
        if mode != "baseline":
            add_check(
                checks,
                f"cbr:{mode}:baseline-size-delta",
                profile["baseline_size_delta_ratio"]
                <= thresholds["max_cbr_size_delta_ratio"],
                actual_ratio=profile["baseline_size_delta_ratio"],
                maximum_ratio=thresholds["max_cbr_size_delta_ratio"],
            )

    return {
        "schema_version": 1,
        "passed": all(check["passed"] for check in checks),
        "format": args.format,
        "expected_frames": q15["expected_frames"],
        "fps": q15["fps"],
        "psnr_infinity_cap_db": common.PSNR_INFINITY_CAP_DB,
        "schedules": {
            "q15": {
                "path": str(args.q15_schedule),
                "sha256": common.sha256(args.q15_schedule),
            },
            "q25": {
                "path": str(args.q25_schedule),
                "sha256": common.sha256(args.q25_schedule),
            },
        },
        "artifacts": {
            "source": {"path": str(args.source), "sha256": common.sha256(args.source)},
            **{
                name: {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": common.sha256(path),
                }
                for name, path in encoded_paths.items()
            },
        },
        "sender_events": sender_events,
        "encode_logs": encode_logs,
        "thresholds": profile_thresholds,
        "checks": checks,
        "active_phases": active_reports,
        "clear_phases": clear_reports,
        "cbr": cbr,
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        f"result={'PASS' if report['passed'] else 'FAIL'}",
        f"format={report['format']}",
        f"frames={report['expected_frames']}",
    ]
    for phase in report["active_phases"]:
        q15 = phase["profiles"]["q15"]
        q25 = phase["profiles"]["q25"]
        lines.append(
            f"{phase['name']} count={phase['region_count']}: "
            f"q15-target={q15['target_psnr_y_db']['median']:+.3f} dB, "
            f"q15-bg={q15['background_psnr_y_db']['median']:+.3f} dB, "
            f"q15-select={q15['target_background_selectivity_psnr_y_db']['median']:+.3f} dB, "
            f"q25-target={q25['target_psnr_y_db']['median']:+.3f} dB, "
            f"q25-bg={q25['background_psnr_y_db']['median']:+.3f} dB, "
            f"q25-select={q25['target_background_selectivity_psnr_y_db']['median']:+.3f} dB, "
            f"q25-q15={phase['q25_over_q15_target_psnr_y_db']['median']:+.3f} dB"
        )
    for phase in report["clear_phases"]:
        lines.append(
            f"{phase['name']} ({phase['role']}): "
            f"q15-residual={phase['profiles']['q15']['spatial_residual_psnr_y_db']:.3f} dB, "
            f"q25-residual={phase['profiles']['q25']['spatial_residual_psnr_y_db']:.3f} dB"
        )
    cbr = report["cbr"]
    lines.append(
        "cbr: "
        + ", ".join(
            f"{name}={profile['bytes']} bytes/target-delta="
            f"{profile['target_delta_ratio']:.3%}/baseline-delta="
            f"{profile['baseline_size_delta_ratio']:.3%}"
            for name, profile in cbr["profiles"].items()
        )
    )
    failed = [check["name"] for check in report["checks"] if not check["passed"]]
    if failed:
        lines.append("failed-checks=" + ",".join(failed))
    return "\n".join(lines) + "\n"


def write_test_progress(path: Path, out_times_us: list[int], end_time_us: int) -> None:
    lines = []
    for out_time_us in out_times_us:
        lines.extend((f"out_time_us={out_time_us}\n", "progress=continue\n"))
    lines.extend((f"out_time_us={end_time_us}\n", "progress=end\n"))
    path.write_text("".join(lines), encoding="ascii")


def write_test_events(
    path: Path,
    schedule: dict[str, Any],
    force_empty: bool,
    replayed_events: list[dict[str, Any]],
) -> None:
    events = []
    for sequence, replayed in enumerate(replayed_events):
        index = replayed["phase_index"]
        phase = schedule["phases"][index]
        payload = common.canonical_phase_payload(phase, force_empty)
        events.append(
            {
                "event": "roi_send",
                "sequence": sequence,
                "phase": phase["name"],
                "phase_index": index,
                "phase_start_frame": phase["start_frame"],
                "phase_end_frame": phase["end_frame"],
                "out_time_us": replayed["out_time_us"],
                "frame": replayed["frame"],
                "reason": replayed["reason"],
                "region_count": replayed["region_count"],
                "payload_bytes": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "status": "sent",
                "force_empty": force_empty,
            }
        )
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def write_test_encode_log(path: Path, counts: list[int]) -> None:
    path.write_text(
        "".join(
            f"Applied ROI socket update #{generation} with {count} region(s).\n"
            for generation, count in enumerate(counts, 1)
        ),
        encoding="utf-8",
    )


def self_test() -> None:
    root = Path(__file__).resolve().parent
    q15_raw = load_json(root / "production-q15-schedule.json")
    q25_raw = load_json(root / "production-q25-schedule.json")
    q15, q25 = validate_schedule_pair(q15_raw, q25_raw)
    assert q15["expected_frames"] == EXPECTED_FRAMES
    assert q25["qoffset"] == -0.25
    assert q15["thresholds"]["min_target_gain_psnr_y_db"] == 0.3
    assert q25["thresholds"]["min_target_gain_psnr_y_db"] == 0.6
    assert [phase["region_count"] for phase in q15["phases"]] == EXPECTED_REGION_COUNTS
    phases_by_name = {phase["name"]: phase for phase in q15["phases"]}
    assert phases_by_name["clear-2-to-8"]["target_probes"] == ["T2L", "T2R"]
    assert phases_by_name["final-clear"]["target_probes"] == [
        f"T8_{index}" for index in range(8)
    ]
    assert summarize([1.0, 2.0, 3.0, 4.0])["median"] == 2.5
    assert math.isclose(percentile([1.0, 2.0, 3.0, 4.0], 0.25), 1.75)
    good_probe = active_probe_report([2.0] * 4, [0.01] * 4, [0.0] * 4, [0.0] * 4)
    missed_probe = active_probe_report(
        [-0.1] * 4, [-0.001] * 4, [0.0] * 4, [0.0] * 4
    )
    aggregate_gain = statistics.fmean(
        [
            good_probe["target_psnr_y_db"]["median"],
            missed_probe["target_psnr_y_db"]["median"],
        ]
    )
    aggregate_selectivity = statistics.fmean(
        [
            good_probe["target_background_selectivity_psnr_y_db"]["median"],
            missed_probe["target_background_selectivity_psnr_y_db"]["median"],
        ]
    )
    assert aggregate_gain >= q15["thresholds"]["min_target_gain_psnr_y_db"]
    assert aggregate_selectivity >= q15["thresholds"][
        "min_target_background_selectivity_psnr_y_db"
    ]
    missed_passes = active_probe_passes(missed_probe, q15["thresholds"])
    assert not missed_passes["target_gain"]
    assert not missed_passes["selectivity"]
    aggregate_cross = summarize([0.5, 0.1])["median"]
    missed_cross = summarize([0.1])["median"]
    assert aggregate_cross >= q15["thresholds"]["min_q25_over_q15_psnr_y_db"]
    assert missed_cross < q15["thresholds"]["min_q25_over_q15_psnr_y_db"]

    invalid = copy.deepcopy(q25_raw)
    invalid["phases"][1]["regions"][0]["qoffset"] = -0.15
    try:
        validate_schedule(invalid, "production-q25", -0.25)
    except ProductionAnalysisError as exc:
        assert "qoffset -0.25" in str(exc)
    else:
        raise AssertionError("q25 schedule accepted a q15 region")

    with tempfile.TemporaryDirectory(prefix="roi-production-self-test-") as directory:
        temp = Path(directory)
        baseline_events = temp / "baseline.jsonl"
        q15_events = temp / "q15.jsonl"
        q25_events = temp / "q25.jsonl"
        baseline_progress = temp / "baseline-progress.txt"
        q15_progress = temp / "q15-progress.txt"
        q25_progress = temp / "q25-progress.txt"
        full_progress_times = [slot * 100_000 for slot in range(1, 340)]
        gap_seven_times = [
            316_667 if slot == 3 else slot * 100_000
            for slot in range(1, 340)
            if slot != 2
        ]
        write_test_progress(baseline_progress, full_progress_times, 34_000_000)
        write_test_progress(q15_progress, gap_seven_times, 34_000_000)
        write_test_progress(q25_progress, full_progress_times, 34_000_000)
        baseline_replay = replay_progress(baseline_progress, q15_raw, True)
        q15_replay = replay_progress(q15_progress, q15_raw, False)
        q25_replay = replay_progress(q25_progress, q25_raw, False)
        assert not terminal_repeat_is_guarded(q15_raw, 1008, "repeat")
        assert terminal_repeat_is_guarded(q15_raw, 1011, "repeat")
        assert not terminal_repeat_is_guarded(q15_raw, 1011, "phase_change")
        assert q25_replay["events"][-1]["out_time_us"] == 33_600_000
        write_test_events(
            baseline_events, q15_raw, True, baseline_replay["events"]
        )
        write_test_events(q15_events, q15_raw, False, q15_replay["events"])
        write_test_events(q25_events, q25_raw, False, q25_replay["events"])
        q15_same_phase_gaps = [
            current["frame"] - previous["frame"]
            for previous, current in zip(
                q15_replay["events"], q15_replay["events"][1:]
            )
            if previous["phase_index"] == current["phase_index"]
        ]
        assert max(q15_same_phase_gaps) == 7
        baseline_summary = strict_event_summary(
            baseline_events, baseline_progress, q15_raw, True, {0}, [0]
        )
        q15_summary = strict_event_summary(
            q15_events,
            q15_progress,
            q15_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        )
        q25_summary = strict_event_summary(
            q25_events,
            q25_progress,
            q25_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        )
        assert baseline_summary["observed_region_counts"] == [0]
        assert q15_summary["observed_region_counts"] == [0, 1, 2, 8]
        assert q25_summary["observed_region_counts"] == [0, 1, 2, 8]
        assert baseline_summary["region_count_transition"] == [0]
        assert q15_summary["region_count_transition"] == EXPECTED_REGION_COUNTS
        assert q25_summary["region_count_transition"] == EXPECTED_REGION_COUNTS
        assert baseline_summary["progress_replay"]["coverage"]["observed"] == 337
        assert q15_summary["progress_replay"]["coverage"]["observed"] == 336

        def coverage_case(
            name: str, phase_counts: list[int]
        ) -> tuple[Path, Path]:
            progress_path = temp / f"{name}-progress.txt"
            events_path = temp / f"{name}-events.jsonl"
            selected_slots: list[int] = []
            for index, (phase, count) in enumerate(
                zip(q15_raw["phases"], phase_counts)
            ):
                start_slot = phase["start_frame"] * 10 // 30
                first_slot = 1 if index == 0 else start_slot
                repeat_count = count - 1 if index == 0 else count
                selected_slots.extend(
                    range(first_slot, first_slot + repeat_count)
                )
            write_test_progress(
                progress_path,
                [slot * 100_000 for slot in selected_slots],
                34_000_000,
            )
            replay = replay_progress(
                progress_path, q15_raw, False, enforce_coverage=False
            )
            observed = [
                sum(event["phase_index"] == index for event in replay["events"])
                for index in range(len(q15_raw["phases"]))
            ]
            assert observed == phase_counts
            write_test_events(events_path, q15_raw, False, replay["events"])
            return progress_path, events_path

        final_36_progress, final_36_events = coverage_case(
            "final-36", [40, 60, 40, 60, 40, 60, 36]
        )
        final_36_summary = strict_event_summary(
            final_36_events,
            final_36_progress,
            q15_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        )
        final_36_coverage = final_36_summary["progress_replay"]["coverage"]
        assert final_36_coverage["per_phase"]["final-clear"]["observed"] == 36
        assert final_36_coverage["per_phase"]["final-clear"]["minimum"] == 36

        final_35_progress, final_35_events = coverage_case(
            "final-35", [40, 60, 40, 60, 40, 60, 35]
        )
        try:
            strict_event_summary(
                final_35_events,
                final_35_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "final-clear" in str(exc) and "35/40" in str(exc)
        else:
            raise AssertionError("35/40 final-clear coverage was accepted")

        active_54_progress, active_54_events = coverage_case(
            "active-54", [40, 54, 40, 60, 40, 60, 36]
        )
        active_54_summary = strict_event_summary(
            active_54_events,
            active_54_progress,
            q15_raw,
            False,
            {0, 1, 2, 8},
            EXPECTED_REGION_COUNTS,
        )
        active_54_coverage = active_54_summary["progress_replay"]["coverage"]
        assert active_54_coverage["per_phase"]["active-1"]["observed"] == 54
        assert active_54_coverage["per_phase"]["active-1"]["minimum"] == 54

        active_53_progress, active_53_events = coverage_case(
            "active-53", [40, 53, 40, 60, 40, 60, 36]
        )
        try:
            strict_event_summary(
                active_53_events,
                active_53_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "active-1" in str(exc) and "53/60" in str(exc)
        else:
            raise AssertionError("53/60 active coverage was accepted")

        total_305_progress, total_305_events = coverage_case(
            "total-305", [30, 56, 36, 56, 36, 55, 36]
        )
        try:
            strict_event_summary(
                total_305_events,
                total_305_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "total sender coverage" in str(exc) and "305/340" in str(exc)
        else:
            raise AssertionError("305/340 total coverage was accepted")

        five_hz_progress = temp / "five-hz-progress.txt"
        five_hz_events = temp / "five-hz-events.jsonl"
        five_hz_times = [slot * 100_000 for slot in range(2, 340, 2)]
        write_test_progress(five_hz_progress, five_hz_times, 34_000_000)
        five_hz_replay = replay_progress(
            five_hz_progress, q15_raw, False, enforce_coverage=False
        )
        write_test_events(
            five_hz_events, q15_raw, False, five_hz_replay["events"]
        )
        try:
            strict_event_summary(
                five_hz_events,
                five_hz_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "coverage" in str(exc)
        else:
            raise AssertionError("complete 5 Hz progress coverage was accepted")

        initial_five_hz_progress = temp / "initial-five-hz-progress.txt"
        initial_five_hz_events = temp / "initial-five-hz-events.jsonl"
        initial_five_hz_times = [
            slot * 100_000
            for slot in range(1, 340)
            if slot >= 40 or slot % 2 == 0
        ]
        write_test_progress(
            initial_five_hz_progress, initial_five_hz_times, 34_000_000
        )
        initial_five_hz_replay = replay_progress(
            initial_five_hz_progress, q15_raw, False, enforce_coverage=False
        )
        assert initial_five_hz_replay["coverage"]["observed"] == 317
        write_test_events(
            initial_five_hz_events,
            q15_raw,
            False,
            initial_five_hz_replay["events"],
        )
        try:
            strict_event_summary(
                initial_five_hz_events,
                initial_five_hz_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "initial-clear" in str(exc) and "coverage" in str(exc)
        else:
            raise AssertionError("initial-clear-only 5 Hz coverage was accepted")

        mismatched_events = temp / "progress-mismatch.jsonl"
        mismatched_lines = q25_events.read_text(encoding="utf-8").splitlines()
        mismatched = json.loads(mismatched_lines[1])
        mismatched["frame"] += 1
        mismatched_lines[1] = json.dumps(mismatched, sort_keys=True)
        mismatched_events.write_text(
            "\n".join(mismatched_lines) + "\n", encoding="utf-8"
        )
        try:
            strict_event_summary(
                mismatched_events,
                q25_progress,
                q25_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except ProductionAnalysisError as exc:
            assert "do not match progress replay" in str(exc)
        else:
            raise AssertionError("sender/progress mismatch was accepted")

        out_of_range_progress = temp / "out-of-range-progress.txt"
        out_of_range_progress.write_text(
            "out_time_us=-9223372036854775809\nprogress=continue\n"
            "out_time_us=34000000\nprogress=end\n",
            encoding="ascii",
        )
        try:
            replay_progress(out_of_range_progress, q15_raw, False)
        except ProductionAnalysisError as exc:
            assert "outside int64" in str(exc)
        else:
            raise AssertionError("out-of-int64 progress timestamp was accepted")

        oversized_progress = temp / "oversized-progress.txt"
        oversized_progress.write_text(
            f"{'x' * (MAX_PROGRESS_LINE_BYTES + 1)}=ignored\n"
            "out_time_us=34000000\nprogress=end\n",
            encoding="ascii",
        )
        try:
            replay_progress(oversized_progress, q15_raw, False)
        except ProductionAnalysisError as exc:
            assert "progress line exceeds" in str(exc)
        else:
            raise AssertionError("oversized progress line was accepted")

        crlf_progress = temp / "crlf-progress.txt"
        crlf_progress.write_bytes(
            b"out_time_us=0\r\nprogress=continue\r\n"
            b"out_time_us=34000000\r\nprogress=end\r\n"
        )
        assert read_progress_records(crlf_progress) == [
            {"status": "continue", "out_time_us": 0},
            {"status": "end", "out_time_us": 34_000_000},
        ]

        for name, summary in (
            ("baseline", baseline_summary),
            ("q15", q15_summary),
            ("q25", q25_summary),
        ):
            encode_log = temp / f"{name}.log"
            write_test_encode_log(encode_log, summary["ordered_region_counts"])
            assert validate_encode_log(
                encode_log, summary["ordered_region_counts"]
            )["applied_updates"] == len(summary["ordered_region_counts"])
        wrong_order_log = temp / "wrong-order.log"
        write_test_encode_log(wrong_order_log, q15_summary["ordered_region_counts"])
        wrong_lines = wrong_order_log.read_text(encoding="utf-8").splitlines()
        wrong_lines[1] = wrong_lines[1].replace("#2 ", "#4 ")
        wrong_order_log.write_text("\n".join(wrong_lines) + "\n", encoding="utf-8")
        try:
            validate_encode_log(wrong_order_log, q15_summary["ordered_region_counts"])
        except ProductionAnalysisError as exc:
            assert "not exactly 1..N" in str(exc)
        else:
            raise AssertionError("out-of-order Applied generations were accepted")
        wrong_count_log = temp / "wrong-count.log"
        write_test_encode_log(wrong_count_log, q15_summary["ordered_region_counts"])
        wrong_count_lines = wrong_count_log.read_text(encoding="utf-8").splitlines()
        wrong_count_lines[-1] = wrong_count_lines[-1].replace(
            "with 0 region(s)", "with 8 region(s)"
        )
        wrong_count_log.write_text(
            "\n".join(wrong_count_lines) + "\n", encoding="utf-8"
        )
        try:
            validate_encode_log(
                wrong_count_log, q15_summary["ordered_region_counts"]
            )
        except ProductionAnalysisError as exc:
            assert "count sequence" in str(exc)
        else:
            raise AssertionError("wrong Applied region-count sequence was accepted")
        forbidden_log = temp / "forbidden.log"
        write_test_encode_log(forbidden_log, q15_summary["ordered_region_counts"])
        with forbidden_log.open("a", encoding="utf-8") as stream:
            stream.write("Ignoring invalid ROI socket update (Invalid argument).\n")
        try:
            validate_encode_log(forbidden_log, q15_summary["ordered_region_counts"])
        except ProductionAnalysisError as exc:
            assert "forbidden ROI error" in str(exc)
        else:
            raise AssertionError("invalid ROI encoder log was accepted")
        tampered_events = temp / "tampered.jsonl"
        tampered = q15_events.read_text(encoding="utf-8").splitlines()
        first = json.loads(tampered[0])
        first["payload_sha256"] = "0" * 64
        tampered[0] = json.dumps(first, sort_keys=True)
        tampered_events.write_text("\n".join(tampered) + "\n", encoding="utf-8")
        try:
            strict_event_summary(
                tampered_events,
                q15_progress,
                q15_raw,
                False,
                {0, 1, 2, 8},
                EXPECTED_REGION_COUNTS,
            )
        except common.AnalysisError as exc:
            assert "wrong payload hash" in str(exc)
        else:
            raise AssertionError("tampered payload SHA was accepted")
    print("analyze-production.py self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--q15", type=Path)
    parser.add_argument("--q25", type=Path)
    parser.add_argument("--baseline-events", type=Path)
    parser.add_argument("--q15-events", type=Path)
    parser.add_argument("--q25-events", type=Path)
    parser.add_argument("--baseline-progress", type=Path)
    parser.add_argument("--q15-progress", type=Path)
    parser.add_argument("--q25-progress", type=Path)
    parser.add_argument("--baseline-encode-log", type=Path)
    parser.add_argument("--q15-encode-log", type=Path)
    parser.add_argument("--q25-encode-log", type=Path)
    parser.add_argument("--format", choices=("h264", "hevc"))
    parser.add_argument("--q15-schedule", type=Path)
    parser.add_argument("--q25-schedule", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--validate-schedules",
        nargs=2,
        type=Path,
        metavar=("Q15", "Q25"),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        if sys.argv[1:] != ["--self-test"]:
            parser.error("--self-test cannot be combined with other arguments")
        self_test()
        return 0
    if args.validate_schedules is not None:
        if len(sys.argv[1:]) != 3 or sys.argv[1] != "--validate-schedules":
            parser.error("--validate-schedules cannot be combined with other arguments")
        try:
            validate_schedule_pair(
                load_json(args.validate_schedules[0]),
                load_json(args.validate_schedules[1]),
            )
        except ProductionAnalysisError as exc:
            print(f"production schedule validation failed: {exc}", file=sys.stderr)
            return 2
        print("production schedules valid")
        return 0

    required = (
        "ffmpeg",
        "source",
        "baseline",
        "q15",
        "q25",
        "baseline_events",
        "q15_events",
        "q25_events",
        "baseline_progress",
        "q15_progress",
        "q25_progress",
        "baseline_encode_log",
        "q15_encode_log",
        "q25_encode_log",
        "format",
        "q15_schedule",
        "q25_schedule",
        "output_dir",
    )
    missing = [f"--{name.replace('_', '-')}" for name in required if getattr(args, name) is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    try:
        report = analyze(args)
        summary = render_summary(report)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.output_dir / "summary.txt").write_text(summary, encoding="utf-8")
        sys.stdout.write(summary)
        return 0 if report["passed"] else 1
    except (ProductionAnalysisError, common.AnalysisError) as exc:
        print(f"production analysis failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
