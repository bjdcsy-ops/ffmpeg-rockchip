#!/usr/bin/env python3
"""Aggregate three comparable RK3588 production ROI reports."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


class RepeatAnalysisError(Exception):
    """Raised when repeat evidence is missing, invalid, or incomparable."""


SHA256_HEX_LENGTH = 64
PROFILES = ("q15", "q25")
ENCODE_PROFILES = ("baseline", "q15", "q25")
EXPECTED_FRAMES = 1020
EXPECTED_FPS = 30.0
EXPECTED_PSNR_INFINITY_CAP_DB = 100.0
EXPECTED_SOURCE_SHA256 = "0382d967113e8d6cce96282b5667d2de37f8278f07a0a1092476e6eab7436e7f"
EXPECTED_SCHEDULE_SHA256 = {
    "q15": "34be92cd68d8afff1ced8df0b63735c59eb804d9824eb107c59b4071cc8644c5",
    "q25": "6029a719646ae719e327afb2bdc2edc038cca344bc2417654b52d36f5b435c44",
}
EXPECTED_THRESHOLDS = {
    "q15": {
        "identity": "production-q15",
        "expected_qoffset": -0.15,
        "min_target_gain_psnr_y_db": 0.3,
        "min_target_background_selectivity_psnr_y_db": 0.5,
        "max_background_loss_psnr_y_db": 2.0,
        "min_q25_over_q15_psnr_y_db": 0.2,
        "max_initial_clear_absolute_psnr_y_db": 0.5,
        "max_initial_clear_spatial_psnr_y_db": 0.3,
        "max_clear_spatial_psnr_y_db": 0.5,
        "max_cbr_size_delta_ratio": 0.1,
        "max_cbr_target_delta_ratio": 0.1,
    },
    "q25": {
        "identity": "production-q25",
        "expected_qoffset": -0.25,
        "min_target_gain_psnr_y_db": 0.6,
        "min_target_background_selectivity_psnr_y_db": 0.9,
        "max_background_loss_psnr_y_db": 2.0,
        "min_q25_over_q15_psnr_y_db": 0.2,
        "max_initial_clear_absolute_psnr_y_db": 0.5,
        "max_initial_clear_spatial_psnr_y_db": 0.3,
        "max_clear_spatial_psnr_y_db": 0.5,
        "max_cbr_size_delta_ratio": 0.1,
        "max_cbr_target_delta_ratio": 0.1,
    },
}
ROI_TARGETS = {
    1: ("T1",),
    2: ("T2L", "T2R"),
    8: tuple(f"T8_{index}" for index in range(8)),
}
POST_CLEAR_PHASES = {
    1: "clear-1-to-2",
    2: "clear-2-to-8",
    8: "final-clear",
}
PHASE_ORDER = (
    "initial-clear",
    "active-1",
    "clear-1-to-2",
    "active-2",
    "clear-2-to-8",
    "active-8",
    "final-clear",
)
PHASE_START_FRAMES = {
    "initial-clear": 0,
    "active-1": 120,
    "clear-1-to-2": 300,
    "active-2": 420,
    "clear-2-to-8": 600,
    "active-8": 720,
    "final-clear": 900,
}
PHASE_REGION_COUNTS = (0, 1, 0, 2, 0, 8, 0)
PHASE_NOMINAL_SENDS = {
    "initial-clear": 40,
    "active-1": 60,
    "clear-1-to-2": 40,
    "active-2": 60,
    "clear-2-to-8": 40,
    "active-8": 60,
    "final-clear": 40,
}
PHASE_MINIMUM_SENDS = {
    "initial-clear": 30,
    "active-1": 54,
    "clear-1-to-2": 36,
    "active-2": 54,
    "clear-2-to-8": 36,
    "active-8": 54,
    "final-clear": 36,
}
TOTAL_NOMINAL_SENDS = 340
TOTAL_MINIMUM_SENDS = 306
ACTIVE_PHASE_WINDOWS = {
    1: ("active-1", 180, 300),
    2: ("active-2", 480, 600),
    8: ("active-8", 780, 900),
}
CLEAR_PHASE_WINDOWS = {
    "initial-clear": ("initial", 60, 120, ROI_TARGETS[1]),
    "clear-1-to-2": ("intermediate", 360, 420, ROI_TARGETS[1]),
    "clear-2-to-8": ("intermediate", 660, 720, ROI_TARGETS[2]),
    "final-clear": ("final", 960, 1020, ROI_TARGETS[8]),
}
REPORT_IDENTITY_FIELDS = (
    "schema_version",
    "format",
    "expected_frames",
    "fps",
    "psnr_infinity_cap_db",
)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    if not is_number(value) or not math.isfinite(value):
        raise RepeatAnalysisError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise RepeatAnalysisError(f"{name} must be greater than zero")
    return float(value)


def require_sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RepeatAnalysisError(f"{name} must be a lowercase SHA-256 digest")
    return value


def expected_check_specs() -> dict[str, tuple[str, str, float]]:
    specs: dict[str, tuple[str, str, float]] = {}

    def add(
        name: str, actual_field: str, threshold_field: str, threshold: float
    ) -> None:
        if name in specs:
            raise AssertionError(f"duplicate expected check: {name}")
        specs[name] = (actual_field, threshold_field, threshold)

    for profile in PROFILES:
        thresholds = EXPECTED_THRESHOLDS[profile]
        add(
            f"initial-clear:{profile}:absolute-clear",
            "actual_db",
            "maximum_db",
            thresholds["max_initial_clear_absolute_psnr_y_db"],
        )
        add(
            f"initial-clear:{profile}:spatial-clear",
            "actual_db",
            "maximum_db",
            thresholds["max_initial_clear_spatial_psnr_y_db"],
        )
    for roi_count, targets in ROI_TARGETS.items():
        phase = f"active-{roi_count}"
        for profile in PROFILES:
            thresholds = EXPECTED_THRESHOLDS[profile]
            add(
                f"{phase}:{profile}:target-gain",
                "actual_db",
                "minimum_db",
                thresholds["min_target_gain_psnr_y_db"],
            )
            add(
                f"{phase}:{profile}:target-background-selectivity",
                "actual_db",
                "minimum_db",
                thresholds["min_target_background_selectivity_psnr_y_db"],
            )
            add(
                f"{phase}:{profile}:background-loss",
                "actual_db",
                "minimum_db",
                -thresholds["max_background_loss_psnr_y_db"],
            )
            for target in targets:
                add(
                    f"{phase}:{profile}:{target}:target-gain",
                    "actual_db",
                    "minimum_db",
                    thresholds["min_target_gain_psnr_y_db"],
                )
                add(
                    f"{phase}:{profile}:{target}:target-background-selectivity",
                    "actual_db",
                    "minimum_db",
                    thresholds["min_target_background_selectivity_psnr_y_db"],
                )
            add(
                f"{POST_CLEAR_PHASES[roi_count]}:{profile}:spatial-clear",
                "actual_db",
                "maximum_db",
                thresholds["max_clear_spatial_psnr_y_db"],
            )
        cross_threshold = max(
            EXPECTED_THRESHOLDS[profile]["min_q25_over_q15_psnr_y_db"]
            for profile in PROFILES
        )
        add(
            f"{phase}:q25-over-q15",
            "actual_db",
            "minimum_db",
            cross_threshold,
        )
        for target in targets:
            add(
                f"{phase}:{target}:q25-over-q15",
                "actual_db",
                "minimum_db",
                cross_threshold,
            )
    add(
        "cbr:baseline:target-delta",
        "actual_ratio",
        "maximum_ratio",
        EXPECTED_THRESHOLDS["q15"]["max_cbr_target_delta_ratio"],
    )
    for profile in PROFILES:
        thresholds = EXPECTED_THRESHOLDS[profile]
        add(
            f"cbr:{profile}:target-delta",
            "actual_ratio",
            "maximum_ratio",
            thresholds["max_cbr_target_delta_ratio"],
        )
        add(
            f"cbr:{profile}:baseline-size-delta",
            "actual_ratio",
            "maximum_ratio",
            thresholds["max_cbr_size_delta_ratio"],
        )
    if len(specs) != 91:
        raise AssertionError(f"expected 91 production checks, got {len(specs)}")
    return specs


EXPECTED_CHECK_SPECS = expected_check_specs()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepeatAnalysisError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_non_finite_json(value: str) -> Any:
    raise RepeatAnalysisError(f"non-finite JSON number is not allowed: {value}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RepeatAnalysisError(f"cannot hash report {path}: {exc}") from exc
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RepeatAnalysisError(f"report does not exist: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite_json,
        )
    except RepeatAnalysisError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepeatAnalysisError(f"cannot read JSON report {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RepeatAnalysisError(f"report must be a JSON object: {path}")
    return value


def validate_check(raw: Any, report_label: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RepeatAnalysisError(f"{report_label}.checks[{index}] must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise RepeatAnalysisError(
            f"{report_label}.checks[{index}].name must be a non-empty string"
        )
    passed = raw.get("passed")
    if not isinstance(passed, bool):
        raise RepeatAnalysisError(f"{report_label} check {name!r}.passed must be boolean")

    actual_fields = [key for key in raw if key.startswith("actual_")]
    threshold_fields = [
        key
        for key in raw
        if key.startswith("minimum_") or key.startswith("maximum_")
    ]
    if len(actual_fields) != 1:
        raise RepeatAnalysisError(
            f"{report_label} check {name!r} must have exactly one actual_* field"
        )
    if len(threshold_fields) != 1:
        raise RepeatAnalysisError(
            f"{report_label} check {name!r} must have exactly one minimum_* or "
            "maximum_* field"
        )
    actual_field = actual_fields[0]
    threshold_field = threshold_fields[0]
    allowed_fields = {"name", "passed", actual_field, threshold_field}
    if set(raw) != allowed_fields:
        extras = sorted(set(raw) - allowed_fields)
        missing = sorted(allowed_fields - set(raw))
        details = []
        if extras:
            details.append(f"unexpected fields {extras}")
        if missing:
            details.append(f"missing fields {missing}")
        raise RepeatAnalysisError(
            f"{report_label} check {name!r} has invalid fields: {'; '.join(details)}"
        )

    unit = actual_field.removeprefix("actual_")
    direction, threshold_unit = threshold_field.split("_", 1)
    if not unit or threshold_unit != unit:
        raise RepeatAnalysisError(
            f"{report_label} check {name!r} actual and threshold units differ"
        )
    actual = require_finite_number(raw[actual_field], f"{report_label} check {name!r} actual")
    threshold = require_finite_number(
        raw[threshold_field], f"{report_label} check {name!r} threshold"
    )
    expected_passed = actual >= threshold if direction == "minimum" else actual <= threshold
    if passed != expected_passed:
        raise RepeatAnalysisError(
            f"{report_label} check {name!r}.passed is inconsistent with "
            f"{actual_field}={actual!r} and {threshold_field}={threshold!r}"
        )
    return {
        "name": name,
        "passed": passed,
        "actual_field": actual_field,
        "actual": actual,
        "threshold_field": threshold_field,
        "threshold": threshold,
        "direction": direction,
        "unit": unit,
    }


def require_close(actual: Any, expected: float, name: str) -> float:
    value = require_finite_number(actual, name)
    if not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
        raise RepeatAnalysisError(f"{name} is {value!r}; expected {expected!r}")
    return value


def region_transition(values: list[int]) -> list[int]:
    transition = []
    for value in values:
        if not transition or transition[-1] != value:
            transition.append(value)
    return transition


def validate_sender_encoder_sections(raw: dict[str, Any], label: str) -> None:
    senders = raw["sender_events"]
    encoders = raw["encode_logs"]
    if set(senders) != set(ENCODE_PROFILES):
        raise RepeatAnalysisError(
            f"{label}.sender_events must contain exactly baseline, q15, and q25"
        )
    if set(encoders) != set(ENCODE_PROFILES):
        raise RepeatAnalysisError(
            f"{label}.encode_logs must contain exactly baseline, q15, and q25"
        )

    for profile in ENCODE_PROFILES:
        sender = senders[profile]
        encoder = encoders[profile]
        if not isinstance(sender, dict) or not isinstance(encoder, dict):
            raise RepeatAnalysisError(
                f"{label} sender/encoder evidence for {profile} must be objects"
            )
        total_sent = sender.get("total_sent")
        if not isinstance(total_sent, int) or isinstance(total_sent, bool) or total_sent <= 0:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.total_sent must be positive"
            )
        ordered = sender.get("ordered_region_counts")
        if (
            not isinstance(ordered, list)
            or len(ordered) != total_sent
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value not in (0, 1, 2, 8)
                for value in ordered
            )
        ):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.ordered_region_counts is invalid"
            )
        expected_transition = [0] if profile == "baseline" else list(PHASE_REGION_COUNTS)
        if region_transition(ordered) != expected_transition:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} has an invalid region transition"
            )
        if sender.get("region_count_transition") != expected_transition:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.region_count_transition is invalid"
            )
        expected_observed = [0] if profile == "baseline" else [0, 1, 2, 8]
        if sender.get("observed_region_counts") != expected_observed:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.observed_region_counts is invalid"
            )
        expected_force_empty = profile == "baseline"
        if sender.get("force_empty") is not expected_force_empty:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.force_empty is invalid"
            )

        per_phase = sender.get("per_phase")
        first_frames = sender.get("first_frame_per_phase")
        if not isinstance(per_phase, dict) or set(per_phase) != set(PHASE_ORDER):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.per_phase has invalid phases"
            )
        if not isinstance(first_frames, dict) or set(first_frames) != set(PHASE_ORDER):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.first_frame_per_phase is invalid"
            )
        for phase in PHASE_ORDER:
            sends = per_phase[phase]
            first_frame = first_frames[phase]
            if (
                not isinstance(sends, int)
                or isinstance(sends, bool)
                or sends < PHASE_MINIMUM_SENDS[phase]
            ):
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile}.per_phase.{phase} is below minimum"
                )
            if (
                not isinstance(first_frame, int)
                or isinstance(first_frame, bool)
                or first_frame < PHASE_START_FRAMES[phase]
                or first_frame > PHASE_START_FRAMES[phase] + 6
            ):
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile}.first_frame_per_phase.{phase} "
                    "is invalid"
                )
        if sum(per_phase.values()) != total_sent:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} phase sends do not equal total_sent"
            )
        expected_order = []
        for phase, region_count in zip(PHASE_ORDER, PHASE_REGION_COUNTS):
            value = 0 if profile == "baseline" else region_count
            expected_order.extend([value] * per_phase[phase])
        if ordered != expected_order:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} order does not match phase counts"
            )

        replay = sender.get("progress_replay")
        if not isinstance(replay, dict):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.progress_replay must be an object"
            )
        if not isinstance(replay.get("path"), str) or not replay["path"]:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.progress_replay.path is invalid"
            )
        require_sha256(
            replay.get("sha256"),
            f"{label}.sender_events.{profile}.progress_replay.sha256",
        )
        records = replay.get("records")
        if not isinstance(records, int) or isinstance(records, bool) or records <= 0:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.progress_replay.records is invalid"
            )
        coverage = replay.get("coverage")
        if not isinstance(coverage, dict):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile}.progress_replay.coverage is invalid"
            )
        if coverage.get("observed") != total_sent:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} coverage does not equal total_sent"
            )
        if coverage.get("nominal") != TOTAL_NOMINAL_SENDS:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} nominal coverage is invalid"
            )
        if coverage.get("minimum") != TOTAL_MINIMUM_SENDS:
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} minimum coverage is invalid"
            )
        require_close(
            coverage.get("ratio"),
            total_sent / TOTAL_NOMINAL_SENDS,
            f"{label}.sender_events.{profile}.coverage.ratio",
        )
        coverage_phases = coverage.get("per_phase")
        if not isinstance(coverage_phases, dict) or set(coverage_phases) != set(
            PHASE_ORDER
        ):
            raise RepeatAnalysisError(
                f"{label}.sender_events.{profile} coverage phases are invalid"
            )
        for phase in PHASE_ORDER:
            phase_coverage = coverage_phases[phase]
            if not isinstance(phase_coverage, dict):
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile} coverage {phase} is invalid"
                )
            if phase_coverage.get("observed") != per_phase[phase]:
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile} observed {phase} is invalid"
                )
            if phase_coverage.get("nominal") != PHASE_NOMINAL_SENDS[phase]:
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile} nominal {phase} is invalid"
                )
            if phase_coverage.get("minimum") != PHASE_MINIMUM_SENDS[phase]:
                raise RepeatAnalysisError(
                    f"{label}.sender_events.{profile} minimum {phase} is invalid"
                )
            require_close(
                phase_coverage.get("ratio"),
                per_phase[phase] / PHASE_NOMINAL_SENDS[phase],
                f"{label}.sender_events.{profile}.coverage.{phase}.ratio",
            )

        applied = encoder.get("applied_updates")
        if applied != total_sent:
            raise RepeatAnalysisError(
                f"{label} {profile} sender total_sent does not equal applied_updates"
            )
        if encoder.get("first_generation") != 1 or encoder.get("last_generation") != applied:
            raise RepeatAnalysisError(
                f"{label}.encode_logs.{profile} generation range is invalid"
            )
        if encoder.get("region_count_transition") != expected_transition:
            raise RepeatAnalysisError(
                f"{label}.encode_logs.{profile}.region_count_transition is invalid"
            )
        if not isinstance(encoder.get("path"), str) or not encoder["path"]:
            raise RepeatAnalysisError(f"{label}.encode_logs.{profile}.path is invalid")
        require_sha256(
            encoder.get("sha256"), f"{label}.encode_logs.{profile}.sha256"
        )


def check_actual(checks: dict[str, dict[str, Any]], name: str) -> float:
    return checks[name]["actual"]


def require_metric_median(value: Any, name: str) -> float:
    if not isinstance(value, dict):
        raise RepeatAnalysisError(f"{name} must be an object")
    return require_finite_number(value.get("median"), f"{name}.median")


def validate_phase_sections(
    raw: dict[str, Any], checks: dict[str, dict[str, Any]], label: str
) -> None:
    active_by_name = {
        phase.get("name"): phase
        for phase in raw["active_phases"]
        if isinstance(phase, dict) and isinstance(phase.get("name"), str)
    }
    if len(active_by_name) != len(raw["active_phases"]) or set(active_by_name) != {
        data[0] for data in ACTIVE_PHASE_WINDOWS.values()
    }:
        raise RepeatAnalysisError(f"{label}.active_phases identity set is invalid")
    for roi_count, (phase_name, start, end) in ACTIVE_PHASE_WINDOWS.items():
        phase = active_by_name[phase_name]
        targets = ROI_TARGETS[roi_count]
        if (
            phase.get("kind") != "active"
            or phase.get("role") is not None
            or phase.get("region_count") != roi_count
            or phase.get("start_frame") != start
            or phase.get("end_frame") != end
            or phase.get("background_probe") != "BG"
            or phase.get("target_probes") != list(targets)
        ):
            raise RepeatAnalysisError(f"{label}.active_phases.{phase_name} is invalid")
        profiles = phase.get("profiles")
        if not isinstance(profiles, dict) or set(profiles) != set(PROFILES):
            raise RepeatAnalysisError(
                f"{label}.active_phases.{phase_name}.profiles is invalid"
            )
        for profile in PROFILES:
            metrics = profiles[profile]
            if not isinstance(metrics, dict):
                raise RepeatAnalysisError(
                    f"{label}.active_phases.{phase_name}.{profile} is invalid"
                )
            expected_values = {
                f"{phase_name}:{profile}:target-gain": require_metric_median(
                    metrics.get("target_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.target_psnr_y_db",
                ),
                f"{phase_name}:{profile}:target-background-selectivity": require_metric_median(
                    metrics.get("target_background_selectivity_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.selectivity",
                ),
                f"{phase_name}:{profile}:background-loss": require_metric_median(
                    metrics.get("background_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.background_psnr_y_db",
                ),
            }
            target_metrics = metrics.get("target_probes")
            if not isinstance(target_metrics, dict) or set(target_metrics) != set(targets):
                raise RepeatAnalysisError(
                    f"{label}.{phase_name}.{profile}.target_probes is invalid"
                )
            for target in targets:
                target = str(target)
                target_report = target_metrics[target]
                if not isinstance(target_report, dict):
                    raise RepeatAnalysisError(
                        f"{label}.{phase_name}.{profile}.{target} is invalid"
                    )
                expected_values[
                    f"{phase_name}:{profile}:{target}:target-gain"
                ] = require_metric_median(
                    target_report.get("target_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.{target}.target_psnr_y_db",
                )
                expected_values[
                    f"{phase_name}:{profile}:{target}:target-background-selectivity"
                ] = require_metric_median(
                    target_report.get("target_background_selectivity_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.{target}.selectivity",
                )
            for check_name, expected in expected_values.items():
                require_close(
                    check_actual(checks, check_name),
                    expected,
                    f"{label}.checks.{check_name}.actual",
                )

        aggregate_cross = require_metric_median(
            phase.get("q25_over_q15_target_psnr_y_db"),
            f"{label}.{phase_name}.q25_over_q15_target_psnr_y_db",
        )
        require_close(
            check_actual(checks, f"{phase_name}:q25-over-q15"),
            aggregate_cross,
            f"{label}.checks.{phase_name}:q25-over-q15.actual",
        )
        probe_cross = phase.get("probe_q25_over_q15_target_psnr_y_db")
        if not isinstance(probe_cross, dict) or set(probe_cross) != set(targets):
            raise RepeatAnalysisError(f"{label}.{phase_name}.probe cross metrics are invalid")
        for target in targets:
            value = require_metric_median(
                probe_cross[target], f"{label}.{phase_name}.{target}.q25_over_q15"
            )
            require_close(
                check_actual(checks, f"{phase_name}:{target}:q25-over-q15"),
                value,
                f"{label}.checks.{phase_name}:{target}:q25-over-q15.actual",
            )

    clear_by_name = {
        phase.get("name"): phase
        for phase in raw["clear_phases"]
        if isinstance(phase, dict) and isinstance(phase.get("name"), str)
    }
    if len(clear_by_name) != len(raw["clear_phases"]) or set(clear_by_name) != set(
        CLEAR_PHASE_WINDOWS
    ):
        raise RepeatAnalysisError(f"{label}.clear_phases identity set is invalid")
    for phase_name, (role, start, end, targets) in CLEAR_PHASE_WINDOWS.items():
        phase = clear_by_name[phase_name]
        if (
            phase.get("kind") != "clear"
            or phase.get("role") != role
            or phase.get("region_count") != 0
            or phase.get("start_frame") != start
            or phase.get("end_frame") != end
            or phase.get("background_probe") != "BG"
            or phase.get("target_probes") != list(targets)
        ):
            raise RepeatAnalysisError(f"{label}.clear_phases.{phase_name} is invalid")
        profiles = phase.get("profiles")
        if not isinstance(profiles, dict) or set(profiles) != set(PROFILES):
            raise RepeatAnalysisError(
                f"{label}.clear_phases.{phase_name}.profiles is invalid"
            )
        for profile in PROFILES:
            metrics = profiles[profile]
            if not isinstance(metrics, dict):
                raise RepeatAnalysisError(
                    f"{label}.clear_phases.{phase_name}.{profile} is invalid"
                )
            spatial = require_finite_number(
                metrics.get("spatial_residual_psnr_y_db"),
                f"{label}.{phase_name}.{profile}.spatial_residual_psnr_y_db",
            )
            if spatial < 0:
                raise RepeatAnalysisError(
                    f"{label}.{phase_name}.{profile}.spatial residual must be non-negative"
                )
            require_close(
                check_actual(checks, f"{phase_name}:{profile}:spatial-clear"),
                spatial,
                f"{label}.checks.{phase_name}:{profile}:spatial-clear.actual",
            )
            if phase_name == "initial-clear":
                absolute = require_finite_number(
                    metrics.get("max_absolute_psnr_y_db"),
                    f"{label}.{phase_name}.{profile}.max_absolute_psnr_y_db",
                )
                if absolute < 0:
                    raise RepeatAnalysisError(
                        f"{label}.{phase_name}.{profile}.absolute residual must be non-negative"
                    )
                require_close(
                    check_actual(checks, f"{phase_name}:{profile}:absolute-clear"),
                    absolute,
                    f"{label}.checks.{phase_name}:{profile}:absolute-clear.actual",
                )


def validate_cbr_section(
    raw: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Any],
    label: str,
) -> None:
    cbr = raw["cbr"]
    duration = EXPECTED_FRAMES / EXPECTED_FPS
    theoretical = 4_000_000 * duration / 8
    require_close(cbr.get("duration_seconds"), duration, f"{label}.cbr.duration_seconds")
    require_close(cbr.get("theoretical_bytes"), theoretical, f"{label}.cbr.theoretical_bytes")
    profiles = cbr.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(ENCODE_PROFILES):
        raise RepeatAnalysisError(
            f"{label}.cbr.profiles must contain baseline, q15, and q25"
        )
    baseline_bytes = artifacts["baseline"]["bytes"]
    for profile in ENCODE_PROFILES:
        values = profiles[profile]
        if not isinstance(values, dict):
            raise RepeatAnalysisError(f"{label}.cbr.profiles.{profile} is invalid")
        encoded_bytes = values.get("bytes")
        if encoded_bytes != artifacts[profile]["bytes"]:
            raise RepeatAnalysisError(
                f"{label}.cbr.profiles.{profile}.bytes does not match artifacts"
            )
        expected_target_delta = abs(encoded_bytes - theoretical) / theoretical
        expected_baseline_delta = abs(encoded_bytes - baseline_bytes) / baseline_bytes
        target_delta = require_close(
            values.get("target_delta_ratio"),
            expected_target_delta,
            f"{label}.cbr.profiles.{profile}.target_delta_ratio",
        )
        baseline_delta = require_close(
            values.get("baseline_size_delta_ratio"),
            expected_baseline_delta,
            f"{label}.cbr.profiles.{profile}.baseline_size_delta_ratio",
        )
        require_close(
            values.get("bitrate_bps"),
            encoded_bytes * 8 / duration,
            f"{label}.cbr.profiles.{profile}.bitrate_bps",
        )
        require_close(
            check_actual(checks, f"cbr:{profile}:target-delta"),
            target_delta,
            f"{label}.checks.cbr:{profile}:target-delta.actual",
        )
        if profile != "baseline":
            require_close(
                check_actual(checks, f"cbr:{profile}:baseline-size-delta"),
                baseline_delta,
                f"{label}.checks.cbr:{profile}:baseline-size-delta.actual",
            )


def validate_report(path: Path, report_number: int) -> dict[str, Any]:
    raw = load_json(path)
    label = f"report[{report_number}]"

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise RepeatAnalysisError(f"{label}.schema_version must be an integer")
    if schema_version != 1:
        raise RepeatAnalysisError(f"{label}.schema_version must be 1")
    report_format = raw.get("format")
    if report_format not in ("h264", "hevc"):
        raise RepeatAnalysisError(f"{label}.format must be h264 or hevc")
    expected_frames = raw.get("expected_frames")
    if (
        not isinstance(expected_frames, int)
        or isinstance(expected_frames, bool)
        or expected_frames <= 0
    ):
        raise RepeatAnalysisError(f"{label}.expected_frames must be a positive integer")
    if expected_frames != EXPECTED_FRAMES:
        raise RepeatAnalysisError(
            f"{label}.expected_frames must be {EXPECTED_FRAMES} for the production matrix"
        )
    fps = require_finite_number(raw.get("fps"), f"{label}.fps", positive=True)
    require_close(fps, EXPECTED_FPS, f"{label}.fps")
    psnr_cap = raw.get("psnr_infinity_cap_db")
    psnr_cap = require_finite_number(
        psnr_cap, f"{label}.psnr_infinity_cap_db", positive=True
    )
    require_close(
        psnr_cap, EXPECTED_PSNR_INFINITY_CAP_DB, f"{label}.psnr_infinity_cap_db"
    )
    report_passed = raw.get("passed")
    if not isinstance(report_passed, bool):
        raise RepeatAnalysisError(f"{label}.passed must be boolean")

    schedules = raw.get("schedules")
    if not isinstance(schedules, dict) or set(schedules) != set(PROFILES):
        raise RepeatAnalysisError(f"{label}.schedules must contain exactly q15 and q25")
    schedule_sha256: dict[str, str] = {}
    for profile in PROFILES:
        schedule = schedules[profile]
        if not isinstance(schedule, dict):
            raise RepeatAnalysisError(f"{label}.schedules.{profile} must be an object")
        if not isinstance(schedule.get("path"), str) or not schedule["path"]:
            raise RepeatAnalysisError(
                f"{label}.schedules.{profile}.path must be a non-empty string"
            )
        schedule_sha256[profile] = require_sha256(
            schedule.get("sha256"), f"{label}.schedules.{profile}.sha256"
        )
    if schedule_sha256 != EXPECTED_SCHEDULE_SHA256:
        raise RepeatAnalysisError(
            f"{label} schedule SHA-256 values do not match the frozen production matrix"
        )

    thresholds = raw.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != set(PROFILES):
        raise RepeatAnalysisError(f"{label}.thresholds must contain exactly q15 and q25")
    for profile in PROFILES:
        if not isinstance(thresholds[profile], dict) or not thresholds[profile]:
            raise RepeatAnalysisError(f"{label}.thresholds.{profile} must be non-empty")
    try:
        canonical_json(thresholds)
    except (TypeError, ValueError) as exc:
        raise RepeatAnalysisError(f"{label}.thresholds is not strict JSON: {exc}") from exc
    if canonical_json(thresholds) != canonical_json(EXPECTED_THRESHOLDS):
        raise RepeatAnalysisError(
            f"{label}.thresholds does not match the frozen production thresholds"
        )

    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RepeatAnalysisError(f"{label}.artifacts must be an object")
    for artifact_name in ("source", "baseline", "q15", "q25"):
        artifact = artifacts.get(artifact_name)
        if not isinstance(artifact, dict):
            raise RepeatAnalysisError(
                f"{label}.artifacts.{artifact_name} must be an object"
            )
        if not isinstance(artifact.get("path"), str) or not artifact["path"]:
            raise RepeatAnalysisError(
                f"{label}.artifacts.{artifact_name}.path must be a non-empty string"
            )
        require_sha256(
            artifact.get("sha256"), f"{label}.artifacts.{artifact_name}.sha256"
        )
        if artifact_name != "source":
            size = artifact.get("bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise RepeatAnalysisError(
                    f"{label}.artifacts.{artifact_name}.bytes must be a positive integer"
                )
    source_sha256 = require_sha256(
        artifacts["source"].get("sha256"), f"{label}.artifacts.source.sha256"
    )
    if source_sha256 != EXPECTED_SOURCE_SHA256:
        raise RepeatAnalysisError(
            f"{label}.artifacts.source.sha256 does not match the frozen production source"
        )

    for field, expected_type in (
        ("sender_events", dict),
        ("encode_logs", dict),
        ("active_phases", list),
        ("clear_phases", list),
        ("cbr", dict),
    ):
        if not isinstance(raw.get(field), expected_type):
            raise RepeatAnalysisError(
                f"{label}.{field} must be a {expected_type.__name__}"
            )

    raw_checks = raw.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise RepeatAnalysisError(f"{label}.checks must be a non-empty array")
    checks: dict[str, dict[str, Any]] = {}
    for index, raw_check in enumerate(raw_checks):
        check = validate_check(raw_check, label, index)
        if check["name"] in checks:
            raise RepeatAnalysisError(f"{label} has duplicate check {check['name']!r}")
        checks[check["name"]] = check
    if set(checks) != set(EXPECTED_CHECK_SPECS):
        missing = sorted(set(EXPECTED_CHECK_SPECS) - set(checks))
        extra = sorted(set(checks) - set(EXPECTED_CHECK_SPECS))
        raise RepeatAnalysisError(
            f"{label} check name set does not match the production gate set: "
            f"missing={missing}, extra={extra}"
        )
    for name, expected_spec in EXPECTED_CHECK_SPECS.items():
        check = checks[name]
        actual_spec = (
            check["actual_field"],
            check["threshold_field"],
            check["threshold"],
        )
        if canonical_json(actual_spec) != canonical_json(expected_spec):
            raise RepeatAnalysisError(
                f"{label} check {name!r} threshold does not match the production gate"
            )
    expected_report_passed = all(check["passed"] for check in checks.values())
    if report_passed != expected_report_passed:
        raise RepeatAnalysisError(
            f"{label}.passed is inconsistent with the report check results"
        )
    validate_sender_encoder_sections(raw, label)
    validate_phase_sections(raw, checks, label)
    validate_cbr_section(raw, checks, artifacts, label)

    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "schema_version": schema_version,
        "format": report_format,
        "expected_frames": expected_frames,
        "fps": fps,
        "psnr_infinity_cap_db": psnr_cap,
        "schedule_sha256": schedule_sha256,
        "source_sha256": source_sha256,
        "thresholds": copy.deepcopy(thresholds),
        "passed": report_passed,
        "checks": checks,
    }


def require_equal_reports(reports: list[dict[str, Any]]) -> None:
    report_hashes = [report["sha256"] for report in reports]
    if len(set(report_hashes)) != 3:
        raise RepeatAnalysisError("the three report files must have distinct SHA-256 digests")
    reference = reports[0]
    for report_index, report in enumerate(reports[1:], start=2):
        for field in REPORT_IDENTITY_FIELDS:
            if canonical_json(report[field]) != canonical_json(reference[field]):
                raise RepeatAnalysisError(
                    f"report[{report_index}].{field} does not match report[1]"
                )
        if report["schedule_sha256"] != reference["schedule_sha256"]:
            raise RepeatAnalysisError(
                f"report[{report_index}] schedule SHA-256 values do not match report[1]"
            )
        if report["source_sha256"] != reference["source_sha256"]:
            raise RepeatAnalysisError(
                f"report[{report_index}] source SHA-256 does not match report[1]"
            )
        if canonical_json(report["thresholds"]) != canonical_json(reference["thresholds"]):
            raise RepeatAnalysisError(
                f"report[{report_index}].thresholds does not match report[1]"
            )
        reference_names = set(reference["checks"])
        report_names = set(report["checks"])
        if report_names != reference_names:
            missing = sorted(reference_names - report_names)
            extra = sorted(report_names - reference_names)
            raise RepeatAnalysisError(
                f"report[{report_index}] check name set differs: "
                f"missing={missing}, extra={extra}"
            )
        for name in sorted(reference_names):
            expected = reference["checks"][name]
            actual = report["checks"][name]
            for field in ("actual_field", "threshold_field", "threshold", "direction", "unit"):
                if canonical_json(actual[field]) != canonical_json(expected[field]):
                    raise RepeatAnalysisError(
                        f"report[{report_index}] check {name!r} {field} does not "
                        "match report[1]"
                    )


def check_status(pass_count: int) -> str:
    if pass_count == 3:
        return "STABLE_PASS"
    if pass_count == 2:
        return "UNSTABLE_NO_RECOMMEND"
    return "REJECT"


def cell_status(pass_count: int) -> str:
    if pass_count == 3:
        return "RECOMMEND"
    if pass_count == 2:
        return "UNSTABLE_NO_RECOMMEND"
    return "REJECT"


def aggregate_checks(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregates = []
    for name in sorted(reports[0]["checks"]):
        source_checks = [report["checks"][name] for report in reports]
        values = [check["actual"] for check in source_checks]
        passes = [check["passed"] for check in source_checks]
        pass_count = sum(passes)
        direction = source_checks[0]["direction"]
        worst = min(values) if direction == "minimum" else max(values)
        threshold = source_checks[0]["threshold"]
        aggregates.append(
            {
                "name": name,
                "actual_field": source_checks[0]["actual_field"],
                "threshold_field": source_checks[0]["threshold_field"],
                "threshold": threshold,
                "direction": direction,
                "values": values,
                "min": min(values),
                "median": statistics.median(values),
                "max": max(values),
                "range": max(values) - min(values),
                "worst": worst,
                "worst_margin": (
                    worst - threshold if direction == "minimum" else threshold - worst
                ),
                "passes": passes,
                "pass_count": pass_count,
                "status": check_status(pass_count),
                "failed_run_ids": [
                    f"run-{index}"
                    for index, passed in enumerate(passes, start=1)
                    if not passed
                ],
                "stable_pass": pass_count == 3,
            }
        )
    return aggregates


def required_cell_checks(roi_count: int, profile: str) -> list[str]:
    phase = f"active-{roi_count}"
    checks = [
        f"initial-clear:{profile}:absolute-clear",
        f"initial-clear:{profile}:spatial-clear",
        f"{phase}:{profile}:target-gain",
        f"{phase}:{profile}:target-background-selectivity",
        f"{phase}:{profile}:background-loss",
    ]
    for target in ROI_TARGETS[roi_count]:
        checks.extend(
            (
                f"{phase}:{profile}:{target}:target-gain",
                f"{phase}:{profile}:{target}:target-background-selectivity",
            )
        )
    checks.extend(
        (
            f"{POST_CLEAR_PHASES[roi_count]}:{profile}:spatial-clear",
            "cbr:baseline:target-delta",
            f"cbr:{profile}:target-delta",
            f"cbr:{profile}:baseline-size-delta",
        )
    )
    if profile == "q25":
        checks.append(f"{phase}:q25-over-q15")
        checks.extend(
            f"{phase}:{target}:q25-over-q15" for target in ROI_TARGETS[roi_count]
        )
    return checks


def build_cells(
    reports: list[dict[str, Any]], check_aggregates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    available = set(reports[0]["checks"])
    aggregates_by_name = {check["name"]: check for check in check_aggregates}
    cells = []
    for roi_count in ROI_TARGETS:
        for profile in PROFILES:
            required = required_cell_checks(roi_count, profile)
            missing = [name for name in required if name not in available]
            if missing:
                raise RepeatAnalysisError(
                    f"cannot build ROI {roi_count} {profile} cell; missing checks: {missing}"
                )
            run_passes = [
                all(report["checks"][name]["passed"] for name in required)
                for report in reports
            ]
            pass_count = sum(run_passes)
            qoffset = reports[0]["thresholds"][profile].get("expected_qoffset")
            qoffset = require_finite_number(
                qoffset, f"thresholds.{profile}.expected_qoffset"
            )
            cells.append(
                {
                    "codec": reports[0]["format"],
                    "roi_count": roi_count,
                    "profile": profile,
                    "qoffset": qoffset,
                    "target_ids": list(ROI_TARGETS[roi_count]),
                    "required_checks": required,
                    "gate_results": [
                        {
                            "name": name,
                            "pass_count": aggregates_by_name[name]["pass_count"],
                            "status": aggregates_by_name[name]["status"],
                            "worst": aggregates_by_name[name]["worst"],
                            "threshold": aggregates_by_name[name]["threshold"],
                        }
                        for name in required
                    ],
                    "run_cell_passed": run_passes,
                    "cell_pass_count": pass_count,
                    "status": cell_status(pass_count),
                    "stable_pass": pass_count == 3,
                    "qualified": pass_count == 3,
                    "preferred": False,
                    "clear_scope": "phase_aggregate",
                    "failed_checks_by_run": [
                        [
                            name
                            for name in required
                            if not reports[run_index]["checks"][name]["passed"]
                        ]
                        for run_index in range(3)
                    ],
                }
            )
    return cells


def build_recommendations(
    cells: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_key = {(cell["roi_count"], cell["profile"]): cell for cell in cells}
    per_roi = []
    for roi_count in ROI_TARGETS:
        qualified_profiles = [
            profile
            for profile in PROFILES
            if by_key[(roi_count, profile)]["qualified"]
        ]
        recommendation = qualified_profiles[0] if qualified_profiles else None
        for profile in PROFILES:
            by_key[(roi_count, profile)]["preferred"] = profile == recommendation
        per_roi.append(
            {
                "roi_count": roi_count,
                "profiles": {
                    profile: {
                        "status": by_key[(roi_count, profile)]["status"],
                        "qualified": by_key[(roi_count, profile)]["qualified"],
                        "pass_count": by_key[(roi_count, profile)]["cell_pass_count"],
                        "failed_checks_by_run": copy.deepcopy(
                            by_key[(roi_count, profile)]["failed_checks_by_run"]
                        ),
                    }
                    for profile in PROFILES
                },
                "qualified_profiles": qualified_profiles,
                "preferred_profile": recommendation,
            }
        )

    uniform_qualified = [
        profile
        for profile in PROFILES
        if all(
            by_key[(roi_count, profile)]["status"] == "RECOMMEND"
            for roi_count in ROI_TARGETS
        )
    ]
    uniform_profile = {
        "qualified_profiles": uniform_qualified,
        "preferred_profile": uniform_qualified[0] if uniform_qualified else None,
        "status": "RECOMMEND" if uniform_qualified else "NO_UNIFORM_PROFILE",
    }
    return per_roi, uniform_profile


def build_aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    require_equal_reports(reports)
    checks = aggregate_checks(reports)
    cells = build_cells(reports, checks)
    per_roi, uniform_profile = build_recommendations(cells)
    reference = reports[0]
    return {
        "schema_version": 1,
        "passed": all(check["pass_count"] == 3 for check in checks),
        "format": reference["format"],
        "expected_frames": reference["expected_frames"],
        "fps": reference["fps"],
        "psnr_infinity_cap_db": reference["psnr_infinity_cap_db"],
        "source_sha256": reference["source_sha256"],
        "schedule_sha256": copy.deepcopy(reference["schedule_sha256"]),
        "thresholds": copy.deepcopy(reference["thresholds"]),
        "inputs": [
            {
                "index": index,
                "path": report["path"],
                "sha256": report["sha256"],
                "passed": report["passed"],
            }
            for index, report in enumerate(reports, start=1)
        ],
        "clear_scope": "phase_aggregate",
        "per_target_clear_verified": False,
        "checks": checks,
        "cells": cells,
        "per_roi_recommendations": per_roi,
        "uniform_profile": uniform_profile,
    }


def render_summary(report: dict[str, Any]) -> str:
    status_counts = {
        status: sum(check["status"] == status for check in report["checks"])
        for status in ("STABLE_PASS", "UNSTABLE_NO_RECOMMEND", "REJECT")
    }
    lines = [
        f"result={'PASS' if report['passed'] else 'FAIL'}",
        f"format={report['format']}",
        "reports=3",
        f"source-sha256={report['source_sha256']}",
        f"stable-checks={status_counts['STABLE_PASS']}/{len(report['checks'])}",
        f"unstable-checks={status_counts['UNSTABLE_NO_RECOMMEND']}",
        f"rejected-checks={status_counts['REJECT']}",
        f"clear-scope={report['clear_scope']}",
        "per-target-clear-verified=false",
    ]
    for check in report["checks"]:
        if check["status"] != "STABLE_PASS":
            values = ",".join(f"{value:.9g}" for value in check["values"])
            lines.append(
                f"check={check['name']} values={values} pass={check['pass_count']}/3 "
                f"status={check['status']}"
            )
    for cell in report["cells"]:
        lines.append(
            f"cell=roi-{cell['roi_count']}:{cell['profile']} "
            f"pass={cell['cell_pass_count']}/3 status={cell['status']}"
        )
    for recommendation in report["per_roi_recommendations"]:
        profile = recommendation["preferred_profile"]
        lines.append(
            f"roi-{recommendation['roi_count']}-recommendation={profile or 'NONE'}"
        )
    uniform = report["uniform_profile"]["preferred_profile"]
    lines.append(f"uniform-profile={uniform or 'NONE'}")
    return "\n".join(lines) + "\n"


def analyze(report_paths: list[Path], output_dir: Path) -> dict[str, Any]:
    if len(report_paths) != 3:
        raise RepeatAnalysisError("exactly three --report PATH arguments are required")
    if output_dir.exists():
        raise RepeatAnalysisError(f"output directory already exists: {output_dir}")
    try:
        resolved_paths = [path.resolve(strict=True) for path in report_paths]
    except OSError as exc:
        raise RepeatAnalysisError(f"cannot resolve report path: {exc}") from exc
    if len(set(resolved_paths)) != 3:
        raise RepeatAnalysisError("the three --report paths must resolve to distinct files")
    reports = [
        validate_report(path, report_number)
        for report_number, path in enumerate(report_paths, start=1)
    ]
    aggregate = build_aggregate(reports)
    try:
        output_dir.mkdir(parents=True)
        (output_dir / "report.json").write_text(
            json.dumps(aggregate, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (output_dir / "summary.txt").write_text(
            render_summary(aggregate), encoding="utf-8"
        )
    except OSError as exc:
        raise RepeatAnalysisError(f"cannot write output directory {output_dir}: {exc}") from exc
    return aggregate


def make_test_checks() -> list[dict[str, Any]]:
    checks = []
    for name, (actual_field, threshold_field, threshold) in EXPECTED_CHECK_SPECS.items():
        if threshold_field.startswith("minimum_"):
            actual = threshold + 1.0
        elif name.startswith("cbr:"):
            actual = 0.0
        else:
            actual = threshold / 2
        checks.append(
            {
                "name": name,
                "passed": True,
                actual_field: actual,
                threshold_field: threshold,
            }
        )
    return checks


def make_test_sender_encoder() -> tuple[dict[str, Any], dict[str, Any]]:
    senders = {}
    encoders = {}
    per_phase = dict(PHASE_NOMINAL_SENDS)
    first_frames = dict(PHASE_START_FRAMES)
    for profile in ENCODE_PROFILES:
        ordered = []
        for phase, region_count in zip(PHASE_ORDER, PHASE_REGION_COUNTS):
            value = 0 if profile == "baseline" else region_count
            ordered.extend([value] * per_phase[phase])
        transition = [0] if profile == "baseline" else list(PHASE_REGION_COUNTS)
        coverage_phases = {
            phase: {
                "observed": per_phase[phase],
                "nominal": PHASE_NOMINAL_SENDS[phase],
                "minimum": PHASE_MINIMUM_SENDS[phase],
                "ratio": per_phase[phase] / PHASE_NOMINAL_SENDS[phase],
            }
            for phase in PHASE_ORDER
        }
        senders[profile] = {
            "force_empty": profile == "baseline",
            "total_sent": len(ordered),
            "ordered_region_counts": ordered,
            "observed_region_counts": [0] if profile == "baseline" else [0, 1, 2, 8],
            "region_count_transition": transition,
            "per_phase": dict(per_phase),
            "first_frame_per_phase": dict(first_frames),
            "progress_replay": {
                "path": f"/{profile}/progress.txt",
                "sha256": hashlib.sha256(profile.encode()).hexdigest(),
                "records": 681,
                "coverage": {
                    "observed": len(ordered),
                    "nominal": TOTAL_NOMINAL_SENDS,
                    "minimum": TOTAL_MINIMUM_SENDS,
                    "ratio": len(ordered) / TOTAL_NOMINAL_SENDS,
                    "per_phase": coverage_phases,
                },
            },
        }
        encoders[profile] = {
            "applied_updates": len(ordered),
            "first_generation": 1,
            "last_generation": len(ordered),
            "path": f"/{profile}/encode.log",
            "sha256": hashlib.sha256(f"encode-{profile}".encode()).hexdigest(),
            "region_count_transition": transition,
        }
    return senders, encoders


def make_test_phases(checks: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    by_name = {check["name"]: check for check in checks}

    def actual(name: str) -> float:
        check = by_name[name]
        field = next(key for key in check if key.startswith("actual_"))
        return check[field]

    active_phases = []
    for roi_count, (phase_name, start, end) in ACTIVE_PHASE_WINDOWS.items():
        targets = ROI_TARGETS[roi_count]
        profiles = {}
        for profile in PROFILES:
            profiles[profile] = {
                "target_psnr_y_db": {
                    "median": actual(f"{phase_name}:{profile}:target-gain")
                },
                "target_background_selectivity_psnr_y_db": {
                    "median": actual(
                        f"{phase_name}:{profile}:target-background-selectivity"
                    )
                },
                "background_psnr_y_db": {
                    "median": actual(f"{phase_name}:{profile}:background-loss")
                },
                "target_probes": {
                    target: {
                        "target_psnr_y_db": {
                            "median": actual(
                                f"{phase_name}:{profile}:{target}:target-gain"
                            )
                        },
                        "target_background_selectivity_psnr_y_db": {
                            "median": actual(
                                f"{phase_name}:{profile}:{target}:"
                                "target-background-selectivity"
                            )
                        },
                    }
                    for target in targets
                },
            }
        active_phases.append(
            {
                "name": phase_name,
                "kind": "active",
                "role": None,
                "region_count": roi_count,
                "start_frame": start,
                "end_frame": end,
                "background_probe": "BG",
                "target_probes": list(targets),
                "profiles": profiles,
                "q25_over_q15_target_psnr_y_db": {
                    "median": actual(f"{phase_name}:q25-over-q15")
                },
                "probe_q25_over_q15_target_psnr_y_db": {
                    target: {
                        "median": actual(f"{phase_name}:{target}:q25-over-q15")
                    }
                    for target in targets
                },
            }
        )

    clear_phases = []
    for phase_name, (role, start, end, targets) in CLEAR_PHASE_WINDOWS.items():
        profiles = {}
        for profile in PROFILES:
            profiles[profile] = {
                "spatial_residual_psnr_y_db": actual(
                    f"{phase_name}:{profile}:spatial-clear"
                )
            }
            if phase_name == "initial-clear":
                profiles[profile]["max_absolute_psnr_y_db"] = actual(
                    f"{phase_name}:{profile}:absolute-clear"
                )
        clear_phases.append(
            {
                "name": phase_name,
                "kind": "clear",
                "role": role,
                "region_count": 0,
                "start_frame": start,
                "end_frame": end,
                "background_probe": "BG",
                "target_probes": list(targets),
                "profiles": profiles,
            }
        )
    return active_phases, clear_phases


def make_test_report(run_number: int) -> dict[str, Any]:
    checks = make_test_checks()
    sender_events, encode_logs = make_test_sender_encoder()
    active_phases, clear_phases = make_test_phases(checks)
    encoded_bytes = 17_000_000
    return {
        "schema_version": 1,
        "passed": True,
        "format": "h264",
        "expected_frames": 1020,
        "fps": 30.0,
        "psnr_infinity_cap_db": 100.0,
        "schedules": {
            profile: {
                "path": f"/run/{run_number}/{profile}.json",
                "sha256": EXPECTED_SCHEDULE_SHA256[profile],
            }
            for profile in PROFILES
        },
        "artifacts": {
            "source": {
                "path": f"/run/{run_number}/source.mp4",
                "sha256": EXPECTED_SOURCE_SHA256,
            },
            "baseline": {
                "path": f"/run/{run_number}/baseline.bin",
                "bytes": encoded_bytes,
                "sha256": "4" * 64,
            },
            "q15": {
                "path": f"/run/{run_number}/q15.bin",
                "bytes": encoded_bytes,
                "sha256": "5" * 64,
            },
            "q25": {
                "path": f"/run/{run_number}/q25.bin",
                "bytes": encoded_bytes,
                "sha256": "6" * 64,
            },
        },
        "sender_events": sender_events,
        "encode_logs": encode_logs,
        "thresholds": copy.deepcopy(EXPECTED_THRESHOLDS),
        "checks": checks,
        "active_phases": active_phases,
        "clear_phases": clear_phases,
        "cbr": {
            "duration_seconds": 34.0,
            "theoretical_bytes": float(encoded_bytes),
            "profiles": {
                profile: {
                    "bytes": encoded_bytes,
                    "bitrate_bps": 4_000_000.0,
                    "target_delta_ratio": 0.0,
                    "baseline_size_delta_ratio": 0.0,
                }
                for profile in ENCODE_PROFILES
            },
        },
    }


def set_test_check_failure(report: dict[str, Any], name: str) -> None:
    check = next(check for check in report["checks"] if check["name"] == name)
    actual_field = next(field for field in check if field.startswith("actual_"))
    threshold_field = next(
        field
        for field in check
        if field.startswith("minimum_") or field.startswith("maximum_")
    )
    if threshold_field.startswith("minimum_"):
        check[actual_field] = check[threshold_field] - 1.0
    else:
        check[actual_field] = check[threshold_field] + 1.0
    check["passed"] = False
    report["passed"] = False
    parts = name.split(":")
    if (
        len(parts) == 4
        and parts[0].startswith("active-")
        and parts[1] in PROFILES
        and parts[3] == "target-gain"
    ):
        phase_name, profile, target = parts[0], parts[1], parts[2]
        phase = next(
            phase for phase in report["active_phases"] if phase["name"] == phase_name
        )
        phase["profiles"][profile]["target_probes"][target]["target_psnr_y_db"][
            "median"
        ] = check[actual_field]


def self_test() -> None:
    def write_reports(root: Path, reports: list[dict[str, Any]], prefix: str) -> list[Path]:
        paths = []
        for index, report in enumerate(reports, start=1):
            path = root / f"{prefix}-{index}.json"
            path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
            paths.append(path)
        return paths

    def expect_error(paths: list[Path], output: Path, text: str) -> None:
        try:
            analyze(paths, output)
        except RepeatAnalysisError as exc:
            assert text in str(exc), (text, str(exc))
        else:
            raise AssertionError(f"expected RepeatAnalysisError containing {text!r}")
        assert not output.exists()

    with tempfile.TemporaryDirectory(prefix="roi-production-repeats-") as temporary:
        root = Path(temporary)
        all_pass = [make_test_report(index) for index in range(1, 4)]
        paths = write_reports(root, all_pass, "all-pass")
        stable = analyze(paths, root / "stable")
        assert stable["passed"] is True
        assert all(check["status"] == "STABLE_PASS" for check in stable["checks"])
        assert all(cell["status"] == "RECOMMEND" for cell in stable["cells"])
        assert [
            recommendation["preferred_profile"]
            for recommendation in stable["per_roi_recommendations"]
        ] == ["q15", "q15", "q15"]
        assert stable["uniform_profile"]["qualified_profiles"] == ["q15", "q25"]
        assert stable["uniform_profile"]["preferred_profile"] == "q15"

        two_of_three = copy.deepcopy(all_pass)
        set_test_check_failure(two_of_three[2], "active-1:q15:T1:target-gain")
        paths = write_reports(root, two_of_three, "two-of-three")
        unstable = analyze(paths, root / "unstable")
        check = next(
            item for item in unstable["checks"] if item["name"] == "active-1:q15:T1:target-gain"
        )
        assert check["pass_count"] == 2
        assert check["status"] == "UNSTABLE_NO_RECOMMEND"
        cell = next(
            item
            for item in unstable["cells"]
            if item["roi_count"] == 1 and item["profile"] == "q15"
        )
        assert cell["status"] == "UNSTABLE_NO_RECOMMEND"
        assert unstable["per_roi_recommendations"][0]["preferred_profile"] == "q25"

        one_of_three = copy.deepcopy(all_pass)
        set_test_check_failure(one_of_three[1], "active-1:q15:T1:target-gain")
        set_test_check_failure(one_of_three[2], "active-1:q15:T1:target-gain")
        paths = write_reports(root, one_of_three, "one-of-three")
        rejected = analyze(paths, root / "rejected")
        check = next(
            item for item in rejected["checks"] if item["name"] == "active-1:q15:T1:target-gain"
        )
        assert check["pass_count"] == 1
        assert check["status"] == "REJECT"

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["format"] = "hevc"
        mismatched_paths = write_reports(root, mismatched, "format-mismatch")
        expect_error(mismatched_paths, root / "bad-format", ".format does not match")

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["schedules"]["q25"]["sha256"] = "7" * 64
        mismatched_paths = write_reports(root, mismatched, "schedule-sha-mismatch")
        expect_error(mismatched_paths, root / "bad-schedule-sha", "schedule SHA-256")

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["artifacts"]["source"]["sha256"] = "7" * 64
        mismatched_paths = write_reports(root, mismatched, "source-sha-mismatch")
        expect_error(mismatched_paths, root / "bad-source-sha", "production source")

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["thresholds"]["q15"]["min_target_gain_psnr_y_db"] = 2.0
        mismatched_paths = write_reports(root, mismatched, "thresholds-mismatch")
        expect_error(
            mismatched_paths, root / "bad-thresholds", "frozen production thresholds"
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["checks"][0]["maximum_db"] = 2.0
        mismatched_paths = write_reports(root, mismatched, "check-threshold-mismatch")
        expect_error(
            mismatched_paths, root / "bad-check-threshold", "threshold does not match"
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["checks"].pop()
        mismatched_paths = write_reports(root, mismatched, "check-set-mismatch")
        expect_error(mismatched_paths, root / "bad-check-set", "production gate set")

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["checks"][0]["passed"] = False
        mismatched_paths = write_reports(root, mismatched, "check-passed-tamper")
        expect_error(mismatched_paths, root / "bad-check-passed", ".passed is inconsistent")

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["passed"] = False
        mismatched_paths = write_reports(root, mismatched, "report-passed-tamper")
        expect_error(
            mismatched_paths,
            root / "bad-report-passed",
            ".passed is inconsistent with the report check results",
        )

        expect_error(
            [paths[0], paths[0], paths[0]],
            root / "duplicate-paths",
            "distinct files",
        )

        identical = [make_test_report(1) for _ in range(3)]
        identical_paths = write_reports(root, identical, "duplicate-sha")
        expect_error(
            identical_paths,
            root / "duplicate-report-sha",
            "distinct SHA-256",
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["encode_logs"]["q15"]["applied_updates"] -= 1
        mismatched_paths = write_reports(root, mismatched, "sender-encoder-mismatch")
        expect_error(
            mismatched_paths,
            root / "bad-sender-encoder",
            "does not equal applied_updates",
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["sender_events"]["q15"]["first_frame_per_phase"][
            "active-1"
        ] = 0
        mismatched_paths = write_reports(root, mismatched, "phase-first-frame-tamper")
        expect_error(
            mismatched_paths,
            root / "bad-phase-first-frame",
            "first_frame_per_phase.active-1 is invalid",
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["active_phases"].pop()
        mismatched_paths = write_reports(root, mismatched, "active-phase-truncated")
        expect_error(
            mismatched_paths,
            root / "bad-active-phases",
            "active_phases identity set",
        )

        mismatched = copy.deepcopy(all_pass)
        mismatched[2]["cbr"]["profiles"].pop("q25")
        mismatched_paths = write_reports(root, mismatched, "cbr-truncated")
        expect_error(
            mismatched_paths,
            root / "bad-cbr",
            "cbr.profiles must contain",
        )

        mismatched = copy.deepcopy(all_pass)
        absolute_check = next(
            check
            for check in mismatched[2]["checks"]
            if check["name"] == "initial-clear:q15:absolute-clear"
        )
        absolute_check["actual_db"] = -0.1
        initial_clear = next(
            phase
            for phase in mismatched[2]["clear_phases"]
            if phase["name"] == "initial-clear"
        )
        initial_clear["profiles"]["q15"]["max_absolute_psnr_y_db"] = -0.1
        mismatched_paths = write_reports(root, mismatched, "negative-absolute-clear")
        expect_error(
            mismatched_paths,
            root / "bad-negative-absolute",
            "absolute residual must be non-negative",
        )

        mismatched = copy.deepcopy(all_pass)
        spatial_check = next(
            check
            for check in mismatched[2]["checks"]
            if check["name"] == "clear-1-to-2:q15:spatial-clear"
        )
        spatial_check["actual_db"] = -0.1
        intermediate_clear = next(
            phase
            for phase in mismatched[2]["clear_phases"]
            if phase["name"] == "clear-1-to-2"
        )
        intermediate_clear["profiles"]["q15"][
            "spatial_residual_psnr_y_db"
        ] = -0.1
        mismatched_paths = write_reports(root, mismatched, "negative-spatial-clear")
        expect_error(
            mismatched_paths,
            root / "bad-negative-spatial",
            "spatial residual must be non-negative",
        )

        existing = root / "already-exists"
        existing.mkdir()
        try:
            analyze(paths, existing)
        except RepeatAnalysisError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("existing output directory was accepted")

    assert check_status(3) == "STABLE_PASS"
    assert check_status(2) == "UNSTABLE_NO_RECOMMEND"
    assert check_status(1) == "REJECT"
    assert check_status(0) == "REJECT"
    print("analyze-production-repeats.py self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="append",
        type=Path,
        help="production report.json; provide exactly three times",
    )
    parser.add_argument("--output-dir", type=Path, help="new aggregate evidence directory")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        if sys.argv[1:] != ["--self-test"]:
            parser.error("--self-test cannot be combined with other arguments")
        self_test()
        return 0
    if args.report is None or len(args.report) != 3:
        parser.error("exactly three --report PATH arguments are required")
    if args.output_dir is None:
        parser.error("--output-dir is required")
    try:
        report = analyze(args.report, args.output_dir)
    except RepeatAnalysisError as exc:
        print(f"production repeat analysis failed: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render_summary(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
