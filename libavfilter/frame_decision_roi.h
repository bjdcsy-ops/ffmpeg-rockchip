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

typedef enum FFFrameDecisionROICategory {
    FF_FRAME_DECISION_ROI_CATEGORY_NONE = 0,
    FF_FRAME_DECISION_ROI_CATEGORY_PERSON,
    FF_FRAME_DECISION_ROI_CATEGORY_MOTOR_VEHICLE,
    FF_FRAME_DECISION_ROI_CATEGORY_NON_MOTOR_VEHICLE,
    FF_FRAME_DECISION_ROI_CATEGORY_NB,
} FFFrameDecisionROICategory;

/**
 * Protocol-independent detector output consumed by the normalizer.
 *
 * The frame-decision adapter is responsible for mapping its public SDK result
 * into this structure.  Coordinates use half-open pixel bounds.
 */
typedef struct FFFrameDecisionROIDetection {
    int category;
    double score;
    int64_t left;
    int64_t top;
    int64_t right;
    int64_t bottom;
} FFFrameDecisionROIDetection;

typedef struct FFFrameDecisionROIWeights {
    AVRational qoffset[FF_FRAME_DECISION_ROI_CATEGORY_NB];
} FFFrameDecisionROIWeights;

typedef struct FFFrameDecisionROINormalizeStats {
    int rejected;
    int disabled;
    int merged;
    int truncated;
} FFFrameDecisionROINormalizeStats;

int ff_frame_decision_roi_validate_weights(
    const FFFrameDecisionROIWeights *weights);

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
    const FFFrameDecisionROIWeights *weights,
    int frame_width, int frame_height);

int ff_frame_decision_roi_snapshot_is_fresh(int64_t capture_time_us,
                                            int64_t now_us, int ttl_ms);

/**
 * Replace AV_FRAME_DATA_REGIONS_OF_INTEREST with the supplied normalized set.
 * An empty set removes any existing ROI side data.
 */
int ff_frame_decision_roi_attach(
    AVFrame *frame,
    const AVRegionOfInterest *regions,
    int nb_regions);

#endif /* AVFILTER_FRAME_DECISION_ROI_H */
