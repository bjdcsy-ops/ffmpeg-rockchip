#!/usr/bin/env python3
"""Analyze paired RKMPP baseline/dynamic ROI encodes by spatial probe."""

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
from pathlib import Path
from typing import Any


PSNR_PATTERN = re.compile(
    r"(?:^|\s)n:(\d+).*?(?:^|\s)psnr_y:([^\s]+)"
)
SSIM_PATTERN = re.compile(r"(?:^|\s)n:(\d+).*?(?:^|\s)Y:([^\s]+)")
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_FILTER_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
# For an 8-bit 384x384 probe, the smallest non-zero per-pixel MSE is close to
# 1 / (384 * 384), which corresponds to about 100 dB.  FFmpeg reports `inf`
# when a crop is pixel-identical; capping it here keeps paired deltas defined.
PSNR_INFINITY_CAP_DB = 100.0


class AnalysisError(RuntimeError):
    """Raised when evidence is incomplete or internally inconsistent."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AnalysisError(f"expected a JSON object in {path}")
    return value


def require_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AnalysisError(f"{name} must be an integer >= {minimum}")
    return value


def require_number(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise AnalysisError(f"{name} must be finite and >= {minimum}")
    return result


def validate_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    if schedule.get("schema_version") != 1:
        raise AnalysisError("schedule schema_version must be 1")

    target_bitrate_bps = require_int(
        schedule.get("target_bitrate_bps"), "target_bitrate_bps", 1
    )

    frame = schedule.get("frame")
    if not isinstance(frame, dict):
        raise AnalysisError("schedule.frame must be an object")
    width = require_int(frame.get("width"), "frame.width", 1)
    height = require_int(frame.get("height"), "frame.height", 1)
    fps = require_number(frame.get("fps"), "frame.fps", 0.001)

    probes = schedule.get("probes")
    if not isinstance(probes, dict) or not probes:
        raise AnalysisError("schedule.probes must be a non-empty object")
    normalized_probes: dict[str, dict[str, int]] = {}
    for name, probe in probes.items():
        if not isinstance(name, str) or not SAFE_NAME_PATTERN.fullmatch(name):
            raise AnalysisError(f"invalid probe name: {name!r}")
        if not isinstance(probe, dict) or set(probe) != {"x", "y", "w", "h"}:
            raise AnalysisError(f"probe {name} must contain exactly x/y/w/h")
        item = {
            "x": require_int(probe["x"], f"probes.{name}.x"),
            "y": require_int(probe["y"], f"probes.{name}.y"),
            "w": require_int(probe["w"], f"probes.{name}.w", 1),
            "h": require_int(probe["h"], f"probes.{name}.h", 1),
        }
        if item["x"] + item["w"] > width or item["y"] + item["h"] > height:
            raise AnalysisError(f"probe {name} is outside the encoded frame")
        normalized_probes[name] = item

    phases = schedule.get("phases")
    if not isinstance(phases, list) or not phases:
        raise AnalysisError("schedule.phases must be a non-empty array")
    expected_start = 0
    measured_phases: list[dict[str, Any]] = []
    clear_phases: list[dict[str, Any]] = []
    final_clear_phases: list[dict[str, Any]] = []
    phase_names: list[str] = []
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            raise AnalysisError(f"phase {index} must be an object")
        name = phase.get("name")
        if not isinstance(name, str) or not SAFE_NAME_PATTERN.fullmatch(name):
            raise AnalysisError(f"phase {index} has an invalid name")
        if name in phase_names:
            raise AnalysisError(f"duplicate phase name: {name}")
        phase_names.append(name)
        start = require_int(phase.get("start_frame"), f"phases.{name}.start_frame")
        end = require_int(phase.get("end_frame"), f"phases.{name}.end_frame", 1)
        if start != expected_start or end <= start:
            raise AnalysisError(
                f"phase {name} must be contiguous and use [start_frame,end_frame)"
            )
        expected_start = end

        measure = phase.get("measure")
        if measure is None:
            continue
        if not isinstance(measure, dict):
            raise AnalysisError(f"phase {name}.measure must be an object")
        measure_start = require_int(
            measure.get("start_frame"), f"phases.{name}.measure.start_frame"
        )
        measure_end = require_int(
            measure.get("end_frame"), f"phases.{name}.measure.end_frame", 1
        )
        if not (start <= measure_start < measure_end <= end):
            raise AnalysisError(f"phase {name} measurement is outside its interval")
        roi_roles = ("boost_probe", "neutral_probe", "reduce_probe")
        present_roi_roles = [role for role in roi_roles if role in measure]
        if "final_clear_probes" in measure:
            if "clear_probes" in measure or present_roi_roles:
                raise AnalysisError(
                    f"phase {name}.final_clear_probes cannot be mixed with "
                    "clear_probes or ROI probe roles"
                )
            if set(measure) != {
                "start_frame",
                "end_frame",
                "final_clear_probes",
            }:
                raise AnalysisError(
                    f"phase {name}.measure has unexpected final-clear fields"
                )
            if phase.get("regions") != []:
                raise AnalysisError(
                    f"phase {name}.final_clear_probes requires an empty ROI set"
                )
            names = measure["final_clear_probes"]
            if not isinstance(names, list) or len(names) != 2:
                raise AnalysisError(
                    f"phase {name}.final_clear_probes must contain exactly two probes"
                )
            if any(probe not in normalized_probes for probe in names):
                raise AnalysisError(
                    f"phase {name} references an unknown final clear probe"
                )
            if len(set(names)) != 2:
                raise AnalysisError(
                    f"phase {name}.final_clear_probes must use two distinct probes"
                )
            final_clear_phases.append(
                {
                    "name": name,
                    "start_frame": measure_start,
                    "end_frame": measure_end,
                    "probes": names,
                }
            )
            continue
        if "clear_probes" in measure:
            if present_roi_roles:
                raise AnalysisError(
                    f"phase {name}.clear_probes cannot be mixed with ROI probe roles"
                )
            if set(measure) != {"start_frame", "end_frame", "clear_probes"}:
                raise AnalysisError(
                    f"phase {name}.measure has unexpected clear fields"
                )
            if phase.get("regions") != []:
                raise AnalysisError(
                    f"phase {name}.clear_probes requires an empty ROI set"
                )
            names = measure["clear_probes"]
            if not isinstance(names, list) or len(names) < 2:
                raise AnalysisError(f"phase {name}.clear_probes needs at least two probes")
            if any(probe not in normalized_probes for probe in names):
                raise AnalysisError(f"phase {name} references an unknown clear probe")
            if len(set(names)) != len(names):
                raise AnalysisError(
                    f"phase {name}.clear_probes must use distinct probes"
                )
            clear_phases.append(
                {
                    "name": name,
                    "start_frame": measure_start,
                    "end_frame": measure_end,
                    "probes": names,
                }
            )
            continue

        required = roi_roles
        if any(key not in measure for key in required):
            raise AnalysisError(
                f"phase {name}.measure must define boost/neutral/reduce probes"
            )
        if set(measure) != {"start_frame", "end_frame", *required}:
            raise AnalysisError(f"phase {name}.measure has unexpected ROI fields")
        selected = {key: measure[key] for key in required}
        if any(probe not in normalized_probes for probe in selected.values()):
            raise AnalysisError(f"phase {name} references an unknown probe")
        if len(set(selected.values())) != 3:
            raise AnalysisError(f"phase {name} must use three distinct probes")
        measured_phases.append(
            {
                "name": name,
                "start_frame": measure_start,
                "end_frame": measure_end,
                **selected,
            }
        )

    thresholds = schedule.get("thresholds")
    if not isinstance(thresholds, dict):
        raise AnalysisError("schedule.thresholds must be an object")
    normalized_thresholds = {
        "min_boost_absolute_psnr_y_db": require_number(
            thresholds.get("min_boost_absolute_psnr_y_db"),
            "thresholds.min_boost_absolute_psnr_y_db",
        ),
        "min_reduce_absolute_psnr_y_db": require_number(
            thresholds.get("min_reduce_absolute_psnr_y_db"),
            "thresholds.min_reduce_absolute_psnr_y_db",
        ),
        "boost_vs_neutral_psnr_y_db": require_number(
            thresholds.get("boost_vs_neutral_psnr_y_db"),
            "thresholds.boost_vs_neutral_psnr_y_db",
        ),
        "neutral_vs_reduce_psnr_y_db": require_number(
            thresholds.get("neutral_vs_reduce_psnr_y_db"),
            "thresholds.neutral_vs_reduce_psnr_y_db",
        ),
        "max_cbr_size_delta_ratio": require_number(
            thresholds.get("max_cbr_size_delta_ratio"),
            "thresholds.max_cbr_size_delta_ratio",
        ),
        "max_cbr_target_delta_ratio": require_number(
            thresholds.get("max_cbr_target_delta_ratio"),
            "thresholds.max_cbr_target_delta_ratio",
        ),
        "max_clear_absolute_psnr_y_db": require_number(
            thresholds.get("max_clear_absolute_psnr_y_db"),
            "thresholds.max_clear_absolute_psnr_y_db",
        ),
        "max_clear_spatial_psnr_y_db": require_number(
            thresholds.get("max_clear_spatial_psnr_y_db"),
            "thresholds.max_clear_spatial_psnr_y_db",
        ),
        "max_final_clear_residual_psnr_y_db": require_number(
            thresholds.get("max_final_clear_residual_psnr_y_db"),
            "thresholds.max_final_clear_residual_psnr_y_db",
        ),
    }

    if not measured_phases:
        raise AnalysisError("schedule must contain at least one measured ROI phase")
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "target_bitrate_bps": target_bitrate_bps,
        "expected_frames": expected_start,
        "probes": normalized_probes,
        "phases": measured_phases,
        "clear_phases": clear_phases,
        "final_clear_phases": final_clear_phases,
        "phase_names": phase_names,
        "thresholds": normalized_thresholds,
    }


def build_filter_graph(
    probes: dict[str, dict[str, int]],
    expected_frames: int,
    fps: float,
    metrics_dir: Path,
    mode: str,
) -> tuple[str, list[str]]:
    branch_count = len(probes) * 2
    ref_labels = "".join(f"[r{i}]" for i in range(branch_count))
    dut_labels = "".join(f"[d{i}]" for i in range(branch_count))
    parts = [
        f"[0:v]trim=end_frame={expected_frames},setpts=N/({fps}*TB),"
        f"format=yuv420p,split={branch_count}{ref_labels}",
        f"[1:v]trim=end_frame={expected_frames},setpts=N/({fps}*TB),"
        f"format=yuv420p,split={branch_count}{dut_labels}",
    ]
    outputs: list[str] = []
    for probe_index, (name, probe) in enumerate(probes.items()):
        psnr_index = probe_index * 2
        ssim_index = psnr_index + 1
        crop = f"{probe['w']}:{probe['h']}:{probe['x']}:{probe['y']}"
        psnr_path = metrics_dir / f"{mode}-{name}.psnr"
        ssim_path = metrics_dir / f"{mode}-{name}.ssim"
        parts.extend(
            [
                f"[d{psnr_index}]crop={crop}[dpc{probe_index}]",
                f"[r{psnr_index}]crop={crop}[rpc{probe_index}]",
                f"[dpc{probe_index}][rpc{probe_index}]"
                f"psnr=stats_file={psnr_path}:stats_version=2[op{probe_index}]",
                f"[d{ssim_index}]crop={crop}[dsc{probe_index}]",
                f"[r{ssim_index}]crop={crop}[rsc{probe_index}]",
                f"[dsc{probe_index}][rsc{probe_index}]"
                f"ssim=stats_file={ssim_path}[os{probe_index}]",
            ]
        )
        outputs.extend([f"op{probe_index}", f"os{probe_index}"])
    return ";".join(parts), outputs


def run_metrics(
    ffmpeg: Path,
    source: Path,
    encoded: Path,
    bitstream_format: str,
    schedule: dict[str, Any],
    metrics_dir: Path,
    mode: str,
) -> None:
    graph, outputs = build_filter_graph(
        schedule["probes"],
        schedule["expected_frames"],
        schedule["fps"],
        metrics_dir,
        mode,
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
    (metrics_dir / f"{mode}.stdout").write_bytes(completed.stdout)
    (metrics_dir / f"{mode}.log").write_bytes(completed.stderr)
    if completed.returncode != 0:
        raise AnalysisError(
            f"{mode} metric command failed with exit code {completed.returncode}; "
            f"see {metrics_dir / f'{mode}.log'}"
        )


def parse_metric(
    path: Path,
    pattern: re.Pattern[str],
    expected: int,
    positive_infinity_cap: float | None = None,
) -> list[float]:
    values: list[float] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AnalysisError(f"cannot read metric file {path}: {exc}") from exc
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        try:
            value = float(match.group(2))
        except ValueError as exc:
            raise AnalysisError(f"invalid metric in {path}: {match.group(2)}") from exc
        if math.isinf(value) and value > 0 and positive_infinity_cap is not None:
            value = positive_infinity_cap
        elif not math.isfinite(value):
            raise AnalysisError(f"non-finite metric in {path}: {value}")
        values.append(value)
    if len(values) != expected:
        raise AnalysisError(
            f"{path} contains {len(values)} frames; expected exactly {expected}"
        )
    return values


def mean(values: list[float]) -> float:
    if not values:
        raise AnalysisError("cannot average an empty metric window")
    return statistics.fmean(values)


def paired_delta(
    dynamic: list[float], baseline: list[float], start: int, end: int
) -> list[float]:
    return [
        dynamic[index] - baseline[index]
        for index in range(start, end)
    ]


def final_clear_residual(probe_deltas: dict[str, float]) -> float:
    if len(probe_deltas) != 2:
        raise AnalysisError("final clear residual requires exactly two probe deltas")
    first, second = probe_deltas.values()
    return abs(first - second)


def calculate_cbr_size_metrics(
    baseline_size: int,
    dynamic_size: int,
    target_bitrate_bps: int,
    expected_frames: int,
    fps: float,
) -> dict[str, float]:
    if baseline_size <= 0 or dynamic_size <= 0:
        raise AnalysisError("encoded outputs must not be empty")
    duration_seconds = expected_frames / fps
    theoretical_bytes = target_bitrate_bps * duration_seconds / 8.0
    return {
        "duration_seconds": duration_seconds,
        "theoretical_bytes": theoretical_bytes,
        "baseline_target_delta_ratio": abs(baseline_size - theoretical_bytes)
        / theoretical_bytes,
        "dynamic_target_delta_ratio": abs(dynamic_size - theoretical_bytes)
        / theoretical_bytes,
        "output_size_delta_ratio": abs(dynamic_size - baseline_size) / baseline_size,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_phase_payload(phase: dict[str, Any], force_empty: bool) -> bytes:
    regions = phase.get("regions")
    if not isinstance(regions, list) or len(regions) > 8:
        raise AnalysisError(f"phase {phase.get('name')} has an invalid regions array")
    normalized: list[dict[str, int | float]] = []
    for index, region in enumerate(regions):
        if not isinstance(region, dict) or set(region) != {"x", "y", "w", "h", "qoffset"}:
            raise AnalysisError(
                f"phase {phase.get('name')} region {index} must contain x/y/w/h/qoffset"
            )
        x = require_int(region["x"], f"regions.{index}.x")
        y = require_int(region["y"], f"regions.{index}.y")
        w = require_int(region["w"], f"regions.{index}.w", 1)
        h = require_int(region["h"], f"regions.{index}.h", 1)
        qoffset = require_number(region["qoffset"], f"regions.{index}.qoffset", -1.0)
        if qoffset > 1.0:
            raise AnalysisError(f"regions.{index}.qoffset must be <= 1")
        normalized.append(
            {"x": x, "y": y, "w": w, "h": h, "qoffset": float(qoffset)}
        )
    selected = [] if force_empty else normalized
    return json.dumps(
        {"version": 1, "regions": selected},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def validate_event_log(
    path: Path, raw_schedule: dict[str, Any], force_empty: bool
) -> dict[str, Any]:
    phases = raw_schedule.get("phases")
    if not isinstance(phases, list) or not phases:
        raise AnalysisError("schedule phases are unavailable for event validation")
    counts = {phase["name"]: 0 for phase in phases}
    first_frames: dict[str, int] = {}
    expected_payloads = [
        canonical_phase_payload(phase, force_empty) for phase in phases
    ]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AnalysisError(f"cannot read sender events {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisError(
                f"invalid event JSON at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise AnalysisError(f"event at {path}:{line_number} is not an object")
        sequence = event.get("sequence")
        if sequence != line_number - 1:
            raise AnalysisError(
                f"event sequence at {path}:{line_number} is {sequence}; "
                f"expected {line_number - 1}"
            )
        if event.get("event") != "roi_send" or event.get("status") != "sent":
            raise AnalysisError(f"event at {path}:{line_number} is not a successful ROI send")
        phase_index = event.get("phase_index")
        if isinstance(phase_index, bool) or not isinstance(phase_index, int):
            raise AnalysisError(f"event at {path}:{line_number} has invalid phase_index")
        if not 0 <= phase_index < len(phases):
            raise AnalysisError(f"event at {path}:{line_number} has out-of-range phase_index")
        phase = phases[phase_index]
        phase_name = phase["name"]
        start = phase["start_frame"]
        end = phase["end_frame"]
        frame = event.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or not start <= frame < end:
            raise AnalysisError(f"event at {path}:{line_number} is outside its phase")
        if (
            event.get("phase") != phase_name
            or event.get("phase_start_frame") != start
            or event.get("phase_end_frame") != end
        ):
            raise AnalysisError(f"event at {path}:{line_number} has inconsistent phase metadata")
        if event.get("force_empty") is not force_empty:
            raise AnalysisError(f"event at {path}:{line_number} has the wrong force_empty mode")
        payload = expected_payloads[phase_index]
        expected_regions = 0 if force_empty else len(phase["regions"])
        if event.get("region_count") != expected_regions:
            raise AnalysisError(f"event at {path}:{line_number} has the wrong region count")
        if event.get("payload_bytes") != len(payload):
            raise AnalysisError(f"event at {path}:{line_number} has the wrong payload size")
        if event.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
            raise AnalysisError(f"event at {path}:{line_number} has the wrong payload hash")
        out_time_us = event.get("out_time_us")
        if isinstance(out_time_us, bool) or not isinstance(out_time_us, int) or out_time_us < 0:
            raise AnalysisError(f"event at {path}:{line_number} has invalid out_time_us")
        if event.get("reason") not in {"initial", "phase_change", "repeat"}:
            raise AnalysisError(f"event at {path}:{line_number} has an invalid reason")
        counts[phase_name] += 1
        first_frames.setdefault(phase_name, frame)
    missing = [name for name, count in counts.items() if count == 0]
    if missing:
        raise AnalysisError(f"sender did not successfully send phases: {missing}")
    if not lines:
        raise AnalysisError(f"sender event log is empty: {path}")
    first_event = json.loads(lines[0])
    if (
        first_event.get("phase_index") != 0
        or first_event.get("frame") != 0
        or first_event.get("reason") != "initial"
    ):
        raise AnalysisError(f"sender event log does not start with phase zero: {path}")
    for phase in phases:
        measure = phase.get("measure")
        deadline = measure.get("start_frame") if isinstance(measure, dict) else phase["end_frame"]
        if first_frames[phase["name"]] >= deadline:
            raise AnalysisError(
                f"phase {phase['name']} was first sent after its measurement window began"
            )
    return {
        "force_empty": force_empty,
        "total_sent": len(lines),
        "per_phase": counts,
        "first_frame_per_phase": first_frames,
    }


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    raw_schedule = load_json(args.schedule)
    schedule = validate_schedule(raw_schedule)
    for path in (
        args.ffmpeg,
        args.source,
        args.baseline,
        args.dynamic,
        args.baseline_events,
        args.dynamic_events,
    ):
        if not path.is_file():
            raise AnalysisError(f"required file does not exist: {path}")

    if not SAFE_FILTER_PATH_PATTERN.fullmatch(str(args.output_dir)):
        raise AnalysisError(
            "output directory may contain only letters, digits, _, -, ., and /"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir = args.output_dir / "metrics"
    metrics_dir.mkdir(exist_ok=True)
    run_metrics(
        args.ffmpeg,
        args.source,
        args.baseline,
        args.format,
        schedule,
        metrics_dir,
        "baseline",
    )
    run_metrics(
        args.ffmpeg,
        args.source,
        args.dynamic,
        args.format,
        schedule,
        metrics_dir,
        "dynamic",
    )

    metric_values: dict[str, dict[str, dict[str, list[float]]]] = {
        "baseline": {},
        "dynamic": {},
    }
    for mode in metric_values:
        for probe in schedule["probes"]:
            metric_values[mode][probe] = {
                "psnr_y": parse_metric(
                    metrics_dir / f"{mode}-{probe}.psnr",
                    PSNR_PATTERN,
                    schedule["expected_frames"],
                    PSNR_INFINITY_CAP_DB,
                ),
                "ssim_y": parse_metric(
                    metrics_dir / f"{mode}-{probe}.ssim",
                    SSIM_PATTERN,
                    schedule["expected_frames"],
                ),
            }

    checks: list[dict[str, Any]] = []
    phase_reports: list[dict[str, Any]] = []
    thresholds = schedule["thresholds"]
    for phase in schedule["phases"]:
        start = phase["start_frame"]
        end = phase["end_frame"]
        deltas: dict[str, dict[str, Any]] = {}
        for role in ("boost", "neutral", "reduce"):
            probe = phase[f"{role}_probe"]
            psnr = paired_delta(
                metric_values["dynamic"][probe]["psnr_y"],
                metric_values["baseline"][probe]["psnr_y"],
                start,
                end,
            )
            ssim = paired_delta(
                metric_values["dynamic"][probe]["ssim_y"],
                metric_values["baseline"][probe]["ssim_y"],
                start,
                end,
            )
            deltas[role] = {
                "probe": probe,
                "psnr_y_db_mean": mean(psnr),
                "psnr_y_db_median": statistics.median(psnr),
                "ssim_y_mean": mean(ssim),
            }

        boost_spatial = (
            deltas["boost"]["psnr_y_db_mean"]
            - deltas["neutral"]["psnr_y_db_mean"]
        )
        reduce_spatial = (
            deltas["neutral"]["psnr_y_db_mean"]
            - deltas["reduce"]["psnr_y_db_mean"]
        )
        boost_check = boost_spatial >= thresholds["boost_vs_neutral_psnr_y_db"]
        reduce_check = reduce_spatial >= thresholds["neutral_vs_reduce_psnr_y_db"]
        absolute_boost_check = (
            deltas["boost"]["psnr_y_db_median"]
            >= thresholds["min_boost_absolute_psnr_y_db"]
        )
        absolute_reduce_check = (
            -deltas["reduce"]["psnr_y_db_median"]
            >= thresholds["min_reduce_absolute_psnr_y_db"]
        )
        checks.extend(
            [
                {
                    "name": f"{phase['name']}:absolute-boost",
                    "passed": absolute_boost_check,
                    "actual_db": deltas["boost"]["psnr_y_db_median"],
                    "minimum_db": thresholds["min_boost_absolute_psnr_y_db"],
                },
                {
                    "name": f"{phase['name']}:absolute-reduce",
                    "passed": absolute_reduce_check,
                    "actual_db": deltas["reduce"]["psnr_y_db_median"],
                    "maximum_db": -thresholds["min_reduce_absolute_psnr_y_db"],
                },
                {
                    "name": f"{phase['name']}:boost-vs-neutral",
                    "passed": boost_check,
                    "actual_db": boost_spatial,
                    "minimum_db": thresholds["boost_vs_neutral_psnr_y_db"],
                },
                {
                    "name": f"{phase['name']}:neutral-vs-reduce",
                    "passed": reduce_check,
                    "actual_db": reduce_spatial,
                    "minimum_db": thresholds["neutral_vs_reduce_psnr_y_db"],
                },
            ]
        )
        phase_reports.append(
            {
                **phase,
                "deltas": deltas,
                "boost_vs_neutral_psnr_y_db": boost_spatial,
                "neutral_vs_reduce_psnr_y_db": reduce_spatial,
            }
        )

    clear_reports: list[dict[str, Any]] = []
    for phase in schedule["clear_phases"]:
        probe_deltas: dict[str, float] = {}
        for probe in phase["probes"]:
            probe_deltas[probe] = mean(
                paired_delta(
                    metric_values["dynamic"][probe]["psnr_y"],
                    metric_values["baseline"][probe]["psnr_y"],
                    phase["start_frame"],
                    phase["end_frame"],
                )
            )
        spread = max(probe_deltas.values()) - min(probe_deltas.values())
        max_absolute = max(abs(value) for value in probe_deltas.values())
        spread_passed = spread <= thresholds["max_clear_spatial_psnr_y_db"]
        absolute_passed = max_absolute <= thresholds["max_clear_absolute_psnr_y_db"]
        checks.extend(
            [
                {
                    "name": f"{phase['name']}:clear-absolute-delta",
                    "passed": absolute_passed,
                    "actual_db": max_absolute,
                    "maximum_db": thresholds["max_clear_absolute_psnr_y_db"],
                },
                {
                    "name": f"{phase['name']}:clear-spatial-spread",
                    "passed": spread_passed,
                    "actual_db": spread,
                    "maximum_db": thresholds["max_clear_spatial_psnr_y_db"],
                },
            ]
        )
        clear_reports.append(
            {
                **phase,
                "psnr_y_db_deltas": probe_deltas,
                "max_absolute_delta_db": max_absolute,
                "spatial_spread_db": spread,
            }
        )

    final_clear_reports: list[dict[str, Any]] = []
    for phase in schedule["final_clear_phases"]:
        probe_deltas: dict[str, float] = {}
        for probe in phase["probes"]:
            probe_deltas[probe] = mean(
                paired_delta(
                    metric_values["dynamic"][probe]["psnr_y"],
                    metric_values["baseline"][probe]["psnr_y"],
                    phase["start_frame"],
                    phase["end_frame"],
                )
            )
        residual = final_clear_residual(probe_deltas)
        residual_passed = (
            residual <= thresholds["max_final_clear_residual_psnr_y_db"]
        )
        checks.append(
            {
                "name": f"{phase['name']}:final-clear-residual",
                "passed": residual_passed,
                "actual_db": residual,
                "maximum_db": thresholds[
                    "max_final_clear_residual_psnr_y_db"
                ],
            }
        )
        final_clear_reports.append(
            {
                **phase,
                "psnr_y_db_deltas": probe_deltas,
                "residual_psnr_y_db": residual,
            }
        )

    baseline_size = args.baseline.stat().st_size
    dynamic_size = args.dynamic.stat().st_size
    cbr = calculate_cbr_size_metrics(
        baseline_size,
        dynamic_size,
        schedule["target_bitrate_bps"],
        schedule["expected_frames"],
        schedule["fps"],
    )
    cbr["target_bitrate_bps"] = schedule["target_bitrate_bps"]
    checks.extend(
        [
            {
                "name": "cbr-baseline-target-delta",
                "passed": cbr["baseline_target_delta_ratio"]
                <= thresholds["max_cbr_target_delta_ratio"],
                "actual_ratio": cbr["baseline_target_delta_ratio"],
                "maximum_ratio": thresholds["max_cbr_target_delta_ratio"],
            },
            {
                "name": "cbr-dynamic-target-delta",
                "passed": cbr["dynamic_target_delta_ratio"]
                <= thresholds["max_cbr_target_delta_ratio"],
                "actual_ratio": cbr["dynamic_target_delta_ratio"],
                "maximum_ratio": thresholds["max_cbr_target_delta_ratio"],
            },
            {
                "name": "cbr-output-size-delta",
                "passed": cbr["output_size_delta_ratio"]
                <= thresholds["max_cbr_size_delta_ratio"],
                "actual_ratio": cbr["output_size_delta_ratio"],
                "maximum_ratio": thresholds["max_cbr_size_delta_ratio"],
            },
        ]
    )
    sender_events = {
        "baseline": validate_event_log(args.baseline_events, raw_schedule, True),
        "dynamic": validate_event_log(args.dynamic_events, raw_schedule, False),
    }

    report = {
        "schema_version": 1,
        "passed": all(check["passed"] for check in checks),
        "format": args.format,
        "expected_frames": schedule["expected_frames"],
        "fps": schedule["fps"],
        "psnr_infinity_cap_db": PSNR_INFINITY_CAP_DB,
        "artifacts": {
            "source": {"path": str(args.source), "sha256": sha256(args.source)},
            "baseline": {
                "path": str(args.baseline),
                "bytes": baseline_size,
                "sha256": sha256(args.baseline),
            },
            "dynamic": {
                "path": str(args.dynamic),
                "bytes": dynamic_size,
                "sha256": sha256(args.dynamic),
            },
        },
        "sender_events": sender_events,
        "thresholds": thresholds,
        "checks": checks,
        "roi_phases": phase_reports,
        "clear_phases": clear_reports,
        "final_clear_phases": final_clear_reports,
        "cbr": cbr,
    }
    return report


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        f"result={'PASS' if report['passed'] else 'FAIL'}",
        f"format={report['format']}",
        f"frames={report['expected_frames']}",
    ]
    for phase in report["roi_phases"]:
        lines.append(
            f"{phase['name']}: "
            f"boost={phase['deltas']['boost']['psnr_y_db_median']:+.3f} dB, "
            f"reduce={phase['deltas']['reduce']['psnr_y_db_median']:+.3f} dB, "
            f"boost-neutral={phase['boost_vs_neutral_psnr_y_db']:+.3f} dB, "
            f"neutral-reduce={phase['neutral_vs_reduce_psnr_y_db']:+.3f} dB"
        )
    for phase in report["clear_phases"]:
        lines.append(
            f"{phase['name']}: clear-spatial-spread="
            f"{phase['spatial_spread_db']:.3f} dB, "
            f"clear-max-absolute={phase['max_absolute_delta_db']:.3f} dB"
        )
    for phase in report["final_clear_phases"]:
        probe_details = ", ".join(
            f"{probe}={delta:+.3f} dB"
            for probe, delta in phase["psnr_y_db_deltas"].items()
        )
        lines.append(
            f"{phase['name']}: final-clear {probe_details}, "
            f"residual={phase['residual_psnr_y_db']:.3f} dB"
        )
    baseline = report["artifacts"]["baseline"]["bytes"]
    dynamic = report["artifacts"]["dynamic"]["bytes"]
    cbr = report["cbr"]
    lines.append(
        f"cbr-bytes: baseline={baseline}, dynamic={dynamic}, "
        f"target={cbr['theoretical_bytes']:.1f}, "
        f"baseline-target-delta={cbr['baseline_target_delta_ratio']:.3%}, "
        f"dynamic-target-delta={cbr['dynamic_target_delta_ratio']:.3%}, "
        f"output-delta={cbr['output_size_delta_ratio']:.3%}"
    )
    failed = [check["name"] for check in report["checks"] if not check["passed"]]
    if failed:
        lines.append("failed-checks=" + ",".join(failed))
    return "\n".join(lines) + "\n"


def self_test() -> None:
    sample = {
        "schema_version": 1,
        "target_bitrate_bps": 8_000,
        "frame": {"width": 64, "height": 64, "fps": 30},
        "probes": {
            "A": {"x": 0, "y": 0, "w": 16, "h": 16},
            "N": {"x": 16, "y": 0, "w": 16, "h": 16},
            "B": {"x": 32, "y": 0, "w": 16, "h": 16},
        },
        "thresholds": {
            "boost_vs_neutral_psnr_y_db": 1,
            "neutral_vs_reduce_psnr_y_db": 1,
            "min_boost_absolute_psnr_y_db": 0.5,
            "min_reduce_absolute_psnr_y_db": 0.5,
            "max_cbr_size_delta_ratio": 0.05,
            "max_cbr_target_delta_ratio": 0.1,
            "max_clear_absolute_psnr_y_db": 1,
            "max_clear_spatial_psnr_y_db": 0.5,
            "max_final_clear_residual_psnr_y_db": 0.25,
        },
        "phases": [
            {
                "name": "warmup",
                "start_frame": 0,
                "end_frame": 30,
                "regions": [],
                "measure": {
                    "start_frame": 15,
                    "end_frame": 30,
                    "clear_probes": ["A", "N", "B"],
                },
            },
            {
                "name": "active",
                "start_frame": 30,
                "end_frame": 90,
                "regions": [
                    {"x": 0, "y": 0, "w": 16, "h": 16, "qoffset": -0.5},
                    {"x": 32, "y": 0, "w": 16, "h": 16, "qoffset": 0.5},
                ],
                "measure": {
                    "start_frame": 60,
                    "end_frame": 90,
                    "boost_probe": "A",
                    "neutral_probe": "N",
                    "reduce_probe": "B",
                },
            },
            {
                "name": "final_clear",
                "start_frame": 90,
                "end_frame": 120,
                "regions": [],
                "measure": {
                    "start_frame": 105,
                    "end_frame": 120,
                    "final_clear_probes": ["A", "B"],
                },
            },
        ],
    }
    normalized = validate_schedule(sample)
    assert normalized["expected_frames"] == 120
    assert normalized["target_bitrate_bps"] == 8_000
    assert normalized["phases"][0]["start_frame"] == 60
    assert normalized["clear_phases"][0]["probes"] == ["A", "N", "B"]
    assert normalized["final_clear_phases"][0]["probes"] == ["A", "B"]
    assert paired_delta([2.0, 4.0], [1.0, 1.5], 0, 2) == [1.0, 2.5]
    assert final_clear_residual({"A": 0.125, "B": -0.125}) == 0.25
    cbr = calculate_cbr_size_metrics(900, 1100, 8_000, 30, 30)
    assert cbr["theoretical_bytes"] == 1000.0
    assert math.isclose(cbr["baseline_target_delta_ratio"], 0.1)
    assert math.isclose(cbr["dynamic_target_delta_ratio"], 0.1)

    def assert_invalid_schedule(value: dict[str, Any], expected: str) -> None:
        try:
            validate_schedule(value)
        except AnalysisError as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError(f"schedule unexpectedly accepted; wanted {expected!r}")

    missing_target = json.loads(json.dumps(sample))
    del missing_target["target_bitrate_bps"]
    assert_invalid_schedule(missing_target, "target_bitrate_bps")
    missing_target_threshold = json.loads(json.dumps(sample))
    del missing_target_threshold["thresholds"]["max_cbr_target_delta_ratio"]
    assert_invalid_schedule(
        missing_target_threshold, "thresholds.max_cbr_target_delta_ratio"
    )
    duplicate_final_probe = json.loads(json.dumps(sample))
    duplicate_final_probe["phases"][2]["measure"]["final_clear_probes"] = [
        "A",
        "A",
    ]
    assert_invalid_schedule(duplicate_final_probe, "two distinct probes")
    mixed_final_measure = json.loads(json.dumps(sample))
    mixed_final_measure["phases"][2]["measure"]["clear_probes"] = ["A", "B"]
    assert_invalid_schedule(mixed_final_measure, "cannot be mixed")
    nonempty_final_clear = json.loads(json.dumps(sample))
    nonempty_final_clear["phases"][2]["regions"] = [
        {"x": 0, "y": 0, "w": 16, "h": 16, "qoffset": -0.5}
    ]
    assert_invalid_schedule(nonempty_final_clear, "requires an empty ROI set")
    unknown_final_field = json.loads(json.dumps(sample))
    unknown_final_field["phases"][2]["measure"]["unexpected"] = True
    assert_invalid_schedule(unknown_final_field, "unexpected final-clear fields")
    with tempfile.TemporaryDirectory(prefix="roi-analyze-self-test-") as root:
        paths = {
            "baseline": Path(root) / "baseline.jsonl",
            "dynamic": Path(root) / "dynamic.jsonl",
        }
        for mode, force_empty in (("baseline", True), ("dynamic", False)):
            events = []
            for index, phase in enumerate(sample["phases"]):
                payload = canonical_phase_payload(phase, force_empty)
                events.append(
                    {
                        "event": "roi_send",
                        "sequence": index,
                        "phase": phase["name"],
                        "phase_index": index,
                        "phase_start_frame": phase["start_frame"],
                        "phase_end_frame": phase["end_frame"],
                        "out_time_us": phase["start_frame"] * 1_000_000 // 30,
                        "frame": phase["start_frame"],
                        "reason": "initial" if index == 0 else "phase_change",
                        "region_count": 0 if force_empty else len(phase["regions"]),
                        "payload_bytes": len(payload),
                        "payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "status": "sent",
                        "force_empty": force_empty,
                    }
                )
            paths[mode].write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
        assert validate_event_log(paths["baseline"], sample, True)["total_sent"] == 3
        assert validate_event_log(paths["dynamic"], sample, False)["total_sent"] == 3
    print("analyze.py self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--dynamic", type=Path, required=True)
    parser.add_argument("--baseline-events", type=Path, required=True)
    parser.add_argument("--dynamic-events", type=Path, required=True)
    parser.add_argument("--format", choices=("h264", "hevc"), required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    if "--self-test" in sys.argv:
        self_test()
        return 0
    args = parse_args()
    try:
        report = analyze(args)
        summary = render_summary(report)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.output_dir / "summary.txt").write_text(summary, encoding="utf-8")
        sys.stdout.write(summary)
        return 0 if report["passed"] else 1
    except AnalysisError as exc:
        print(f"analysis failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
