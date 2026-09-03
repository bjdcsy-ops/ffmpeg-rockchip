/*
 * Frame decision region-of-interest helpers
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * FFmpeg is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with FFmpeg; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#ifndef AVFILTER_FRAME_DECISION_ROI_H
#define AVFILTER_FRAME_DECISION_ROI_H

#include <stdint.h>

#include "libavutil/frame.h"
#include "libavutil/rational.h"

#define FF_FRAME_DECISION_ROI_MAX_REGIONS    8
#define FF_FRAME_DECISION_ROI_MAX_DETECTIONS 256
#define FF_FRAME_DECISION_ROI_ALIGNMENT      16
#define FF_FRAME_DECISION_ROI_TARGET_ID_SIZE 32
#define FF_FRAME_DECISION_ROI_MAX_TARGETS    16
#define FF_FRAME_DECISION_ROI_COORDINATE_SCALE 1000000

/**
 * Protocol-independent detector output consumed by the normalizer.
 *
 * The frame-decision adapter is responsible for mapping its public SDK result
 * into this structure.  Coordinates use half-open pixel bounds.
 */
typedef struct FFFrameDecisionROIDetection {
    char target_id[FF_FRAME_DECISION_ROI_TARGET_ID_SIZE];
    double score;
    int64_t left;
    int64_t top;
    int64_t right;
    int64_t bottom;
} FFFrameDecisionROIDetection;

typedef struct FFFrameDecisionROITarget {
    char target_id[FF_FRAME_DECISION_ROI_TARGET_ID_SIZE];
    AVRational qoffset;
} FFFrameDecisionROITarget;

typedef struct FFFrameDecisionROITargetPolicy {
    FFFrameDecisionROITarget targets[FF_FRAME_DECISION_ROI_MAX_TARGETS];
    int nb_targets;
} FFFrameDecisionROITargetPolicy;

/**
 * A user-defined ROI rectangle using parts-per-million frame coordinates.
 * x/y identify the top-left corner; width/height identify its extent.
 * roi_id belongs to the control-plane snapshot contract and is not used when
 * composing regions.
 */
typedef struct FFFrameDecisionROIManualRegion {
    char roi_id[FF_FRAME_DECISION_ROI_TARGET_ID_SIZE];
    uint32_t x;
    uint32_t y;
    uint32_t width;
    uint32_t height;
    AVRational qoffset;
} FFFrameDecisionROIManualRegion;

typedef struct FFFrameDecisionROINormalizeStats {
    int rejected;
    int disabled;
    int merged;
    int truncated;
} FFFrameDecisionROINormalizeStats;

/**
 * Parse target_id=qoffset entries separated by '|'.  Target identifiers use
 * [a-z0-9][a-z0-9._-]{0,30}; qoffset is an integer or num/den rational in
 * [-1, 1].  An empty string produces an empty (disabled) policy.
 */
int ff_frame_decision_roi_parse_target_qoffsets(
    FFFrameDecisionROITargetPolicy *policy, const char *spec);

int ff_frame_decision_roi_validate_target_policy(
    const FFFrameDecisionROITargetPolicy *policy);

int ff_frame_decision_roi_target_is_enabled(
    const FFFrameDecisionROITargetPolicy *policy, const char *target_id);

/**
 * Convert detector rectangles to the single ROI contract used by both the
 * RKMPP encoder and the optional debug overlay.
 *
 * Regions are clipped, aligned outwards to the RKMPP 16-pixel ROI grid,
 * merged until no aligned rectangles overlap, ordered by importance, and
 * capped at FF_FRAME_DECISION_ROI_MAX_REGIONS.  When merged regions carry
 * different qoffset values, the lowest value wins so a protected object is
 * never degraded by an overlapping compression region.
 *
 * @return number of output regions, or a negative AVERROR code
 */
int ff_frame_decision_roi_normalize(
    AVRegionOfInterest *regions,
    FFFrameDecisionROINormalizeStats *stats,
    const FFFrameDecisionROIDetection *detections, int nb_detections,
    const FFFrameDecisionROITargetPolicy *policy,
    int frame_width, int frame_height);

/**
 * Normalize detector output and compose it with user-defined regions.
 *
 * Manual origins must be smaller than FF_FRAME_DECISION_ROI_COORDINATE_SCALE
 * and their positive extents must end at or before that boundary.  Invalid
 * inputs are rejected individually and a zero qoffset disables a manual
 * region.  Manual regions sort ahead of detector regions and are ordered by
 * absolute qoffset, then input order.  Detector ordering retains the existing
 * score-weighted normalizer behavior.  Overlaps are repeatedly unioned, using
 * the lowest qoffset, before the result is capped at
 * FF_FRAME_DECISION_ROI_MAX_REGIONS.
 *
 * @return number of output regions, or a negative AVERROR code
 */
int ff_frame_decision_roi_compose(
    AVRegionOfInterest *regions,
    FFFrameDecisionROINormalizeStats *stats,
    const FFFrameDecisionROIDetection *detections, int nb_detections,
    const FFFrameDecisionROITargetPolicy *policy,
    const FFFrameDecisionROIManualRegion *manual_regions,
    int nb_manual_regions,
    int frame_width, int frame_height);

/**
 * Compose an already-normalized detector snapshot with user-defined regions.
 * This is the lightweight per-frame form: both input sets are limited to
 * FF_FRAME_DECISION_ROI_MAX_REGIONS.  Detector input order is retained;
 * manual regions sort first and then by absolute qoffset and input order.
 *
 * @return number of output regions, or a negative AVERROR code
 */
int ff_frame_decision_roi_compose_normalized(
    AVRegionOfInterest *regions,
    FFFrameDecisionROINormalizeStats *stats,
    const AVRegionOfInterest *detector_regions, int nb_detector_regions,
    const FFFrameDecisionROIManualRegion *manual_regions,
    int nb_manual_regions,
    int frame_width, int frame_height);

/**
 * Return whether a detector result still meets its source-frame deadline.
 * This gate is evaluated before the result is allowed to refresh ROI state.
 */
int ff_frame_decision_roi_result_is_fresh(int64_t capture_time_us,
                                          int64_t now_us, int ttl_ms);

/**
 * Return whether an accepted non-empty ROI snapshot should be applied now.
 * snapshot_accept_time_us is the monotonic time at which FFmpeg accepted the
 * result.  A negative first_empty_result_time_us means that no successful
 * empty result has been received.  Otherwise the miss window starts at that
 * time.  reuse_ttl_ms is the hard upper bound for service silence and is not
 * refreshed by empty or failed results.
 */
int ff_frame_decision_roi_snapshot_should_apply(
    int64_t snapshot_accept_time_us,
    int64_t first_empty_result_time_us,
    int64_t now_us, int miss_hold_ms, int reuse_ttl_ms);

/**
 * Replace AV_FRAME_DATA_REGIONS_OF_INTEREST with the supplied normalized set.
 * An empty set removes any existing ROI side data.
 */
int ff_frame_decision_roi_attach(
    AVFrame *frame,
    const AVRegionOfInterest *regions,
    int nb_regions);

#endif /* AVFILTER_FRAME_DECISION_ROI_H */
