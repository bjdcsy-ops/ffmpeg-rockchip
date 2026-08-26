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

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "libavutil/buffer.h"
#include "libavutil/error.h"

#include "frame_decision_roi.h"

typedef struct NormalizedCandidate {
    AVRegionOfInterest roi;
    double priority;
    int order;
} NormalizedCandidate;

static int qoffset_is_valid(AVRational qoffset)
{
    return qoffset.den > 0 &&
           av_cmp_q(qoffset, (AVRational) { -1, 1 }) >= 0 &&
           av_cmp_q(qoffset, (AVRational) {  1, 1 }) <= 0;
}

int ff_frame_decision_roi_validate_weights(
    const FFFrameDecisionROIWeights *weights)
{
    int i;

    if (!weights)
        return AVERROR(EINVAL);

    for (i = FF_FRAME_DECISION_ROI_CATEGORY_PERSON;
         i < FF_FRAME_DECISION_ROI_CATEGORY_NB; i++) {
        if (!qoffset_is_valid(weights->qoffset[i]))
            return AVERROR(EINVAL);
    }

    return 0;
}

static int regions_overlap(const AVRegionOfInterest *a,
                           const AVRegionOfInterest *b)
{
    return a->left < b->right && a->right > b->left &&
           a->top < b->bottom && a->bottom > b->top;
}

static int compare_candidates(const void *opaque_a, const void *opaque_b)
{
    const NormalizedCandidate *a = opaque_a;
    const NormalizedCandidate *b = opaque_b;

    if (a->priority > b->priority)
        return -1;
    if (a->priority < b->priority)
        return 1;
    if (a->order < b->order)
        return -1;
    if (a->order > b->order)
        return 1;
    return 0;
}

static int64_t clip_coordinate(int64_t value, int limit)
{
    if (value < 0)
        return 0;
    if (value > limit)
        return limit;
    return value;
}

static int align_down(int64_t value)
{
    return (int)(value / FF_FRAME_DECISION_ROI_ALIGNMENT *
                 FF_FRAME_DECISION_ROI_ALIGNMENT);
}

static int align_up_clipped(int64_t value, int limit)
{
    int64_t aligned = ((value + FF_FRAME_DECISION_ROI_ALIGNMENT - 1) /
                       FF_FRAME_DECISION_ROI_ALIGNMENT) *
                      FF_FRAME_DECISION_ROI_ALIGNMENT;

    return (int)(aligned < limit ? aligned : limit);
}

int ff_frame_decision_roi_normalize(
    AVRegionOfInterest *regions,
    FFFrameDecisionROINormalizeStats *stats,
    const FFFrameDecisionROIDetection *detections, int nb_detections,
    const FFFrameDecisionROIWeights *weights,
    int frame_width, int frame_height)
{
    NormalizedCandidate candidates[FF_FRAME_DECISION_ROI_MAX_DETECTIONS];
    FFFrameDecisionROINormalizeStats local_stats = { 0 };
    int nb_candidates = 0;
    int nb_input;
    int i;
    int j;

    if (!regions || !weights || nb_detections < 0 ||
        (nb_detections && !detections) ||
        frame_width <= 0 || frame_height <= 0)
        return AVERROR(EINVAL);
    if (ff_frame_decision_roi_validate_weights(weights) < 0)
        return AVERROR(EINVAL);

    nb_input = nb_detections;
    if (nb_input > FF_FRAME_DECISION_ROI_MAX_DETECTIONS) {
        local_stats.truncated += nb_input -
                                 FF_FRAME_DECISION_ROI_MAX_DETECTIONS;
        nb_input = FF_FRAME_DECISION_ROI_MAX_DETECTIONS;
    }

    for (i = 0; i < nb_input; i++) {
        const FFFrameDecisionROIDetection *src = &detections[i];
        const AVRational qoffset =
            src->category > FF_FRAME_DECISION_ROI_CATEGORY_NONE &&
            src->category < FF_FRAME_DECISION_ROI_CATEGORY_NB
                ? weights->qoffset[src->category]
                : (AVRational) { 0, 0 };
        int64_t left;
        int64_t top;
        int64_t right;
        int64_t bottom;

        if (src->category <= FF_FRAME_DECISION_ROI_CATEGORY_NONE ||
            src->category >= FF_FRAME_DECISION_ROI_CATEGORY_NB ||
            !isfinite(src->score) || src->score < 0.0 || src->score > 1.0 ||
            src->right <= src->left || src->bottom <= src->top) {
            local_stats.rejected++;
            continue;
        }
        if (!qoffset.num) {
            local_stats.disabled++;
            continue;
        }

        left   = clip_coordinate(src->left,   frame_width);
        top    = clip_coordinate(src->top,    frame_height);
        right  = clip_coordinate(src->right,  frame_width);
        bottom = clip_coordinate(src->bottom, frame_height);
        if (right <= left || bottom <= top) {
            local_stats.rejected++;
            continue;
        }

        candidates[nb_candidates] = (NormalizedCandidate) {
            .roi = {
                .self_size = sizeof(AVRegionOfInterest),
                .left      = align_down(left),
                .top       = align_down(top),
                .right     = align_up_clipped(right, frame_width),
                .bottom    = align_up_clipped(bottom, frame_height),
                .qoffset   = qoffset,
            },
            .priority = src->score * fabs(av_q2d(qoffset)),
            .order    = i,
        };
        nb_candidates++;
    }

    /*
     * Restart after every merge.  A rectangular union can overlap a region
     * which neither original rectangle overlapped, so one forward pass is
     * insufficient to guarantee the encoder receives disjoint regions.
     */
    for (i = 0; i < nb_candidates; i++) {
        for (j = i + 1; j < nb_candidates; j++) {
            NormalizedCandidate *a = &candidates[i];
            const NormalizedCandidate *b = &candidates[j];

            if (!regions_overlap(&a->roi, &b->roi))
                continue;

            if (b->roi.left < a->roi.left)
                a->roi.left = b->roi.left;
            if (b->roi.top < a->roi.top)
                a->roi.top = b->roi.top;
            if (b->roi.right > a->roi.right)
                a->roi.right = b->roi.right;
            if (b->roi.bottom > a->roi.bottom)
                a->roi.bottom = b->roi.bottom;
            if (av_cmp_q(b->roi.qoffset, a->roi.qoffset) < 0)
                a->roi.qoffset = b->roi.qoffset;
            if (b->priority > a->priority)
                a->priority = b->priority;
            if (b->order < a->order)
                a->order = b->order;

            memmove(&candidates[j], &candidates[j + 1],
                    (nb_candidates - j - 1) * sizeof(candidates[0]));
            nb_candidates--;
            local_stats.merged++;
            i = -1;
            break;
        }
    }

    qsort(candidates, nb_candidates, sizeof(candidates[0]),
          compare_candidates);

    if (nb_candidates > FF_FRAME_DECISION_ROI_MAX_REGIONS) {
        local_stats.truncated += nb_candidates -
                                 FF_FRAME_DECISION_ROI_MAX_REGIONS;
        nb_candidates = FF_FRAME_DECISION_ROI_MAX_REGIONS;
    }

    for (i = 0; i < nb_candidates; i++)
        regions[i] = candidates[i].roi;
    if (stats)
        *stats = local_stats;

    return nb_candidates;
}

int ff_frame_decision_roi_snapshot_is_fresh(int64_t capture_time_us,
                                            int64_t now_us, int ttl_ms)
{
    if (capture_time_us < 0 || now_us < capture_time_us || ttl_ms <= 0)
        return 0;

    return now_us - capture_time_us <= (int64_t)ttl_ms * 1000;
}

int ff_frame_decision_roi_attach(
    AVFrame *frame,
    const AVRegionOfInterest *regions,
    int nb_regions)
{
    AVBufferRef *roi_ref;
    AVFrameSideData *side_data;

    if (!frame || nb_regions < 0 ||
        nb_regions > FF_FRAME_DECISION_ROI_MAX_REGIONS ||
        (nb_regions && !regions))
        return AVERROR(EINVAL);

    if (!nb_regions) {
        av_frame_remove_side_data(frame,
                                  AV_FRAME_DATA_REGIONS_OF_INTEREST);
        return 0;
    }

    roi_ref = av_buffer_alloc((size_t)nb_regions * sizeof(regions[0]));
    if (!roi_ref)
        return AVERROR(ENOMEM);
    memcpy(roi_ref->data, regions,
           (size_t)nb_regions * sizeof(regions[0]));

    av_frame_remove_side_data(frame, AV_FRAME_DATA_REGIONS_OF_INTEREST);
    side_data = av_frame_new_side_data_from_buf(
        frame, AV_FRAME_DATA_REGIONS_OF_INTEREST, roi_ref);
    if (!side_data) {
        av_buffer_unref(&roi_ref);
        return AVERROR(ENOMEM);
    }

    return 0;
}
