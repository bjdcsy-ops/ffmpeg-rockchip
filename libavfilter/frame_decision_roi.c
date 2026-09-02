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
#include <limits.h>
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

static int target_id_is_valid(const char *target_id)
{
    const char *terminator;
    size_t i;
    size_t length;

    if (!target_id)
        return 0;
    terminator = memchr(target_id, '\0',
                        FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
    if (!terminator)
        return 0;
    length = terminator - target_id;
    if (!length ||
        !((target_id[0] >= 'a' && target_id[0] <= 'z') ||
          (target_id[0] >= '0' && target_id[0] <= '9')))
        return 0;

    for (i = 1; i < length; i++) {
        const unsigned char c = target_id[i];

        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '.' || c == '_' || c == '-'))
            return 0;
    }
    return 1;
}

static int parse_integer(const char *text, size_t length, int allow_sign,
                         int *value)
{
    char number[32];
    char *end;
    long parsed;
    size_t i = 0;

    if (!text || !length || length >= sizeof(number) || !value)
        return AVERROR(EINVAL);
    memcpy(number, text, length);
    number[length] = '\0';
    if (number[0] == '-' || number[0] == '+') {
        if (!allow_sign || length == 1)
            return AVERROR(EINVAL);
        i++;
    }
    for (; i < length; i++) {
        if (number[i] < '0' || number[i] > '9')
            return AVERROR(EINVAL);
    }
    errno = 0;
    end = NULL;
    parsed = strtol(number, &end, 10);
    if (errno || end != number + length || parsed < INT_MIN ||
        parsed > INT_MAX)
        return AVERROR(EINVAL);
    *value = parsed;
    return 0;
}

static int parse_qoffset(const char *text, size_t length,
                         AVRational *qoffset)
{
    const char *slash;
    int numerator;
    int denominator = 1;
    int ret;

    if (!text || !length || !qoffset)
        return AVERROR(EINVAL);
    slash = memchr(text, '/', length);
    if (slash && memchr(slash + 1, '/', text + length - slash - 1))
        return AVERROR(EINVAL);

    ret = parse_integer(text, slash ? (size_t)(slash - text) : length,
                        1, &numerator);
    if (ret < 0)
        return ret;
    if (slash) {
        ret = parse_integer(slash + 1, text + length - slash - 1,
                            0, &denominator);
        if (ret < 0 || denominator <= 0)
            return AVERROR(EINVAL);
    }

    *qoffset = (AVRational) { numerator, denominator };
    if (!qoffset_is_valid(*qoffset))
        return AVERROR(EINVAL);
    if (!numerator)
        *qoffset = (AVRational) { 0, 1 };
    return 0;
}

int ff_frame_decision_roi_validate_target_policy(
    const FFFrameDecisionROITargetPolicy *policy)
{
    int i;
    int j;

    if (!policy || policy->nb_targets < 0 ||
        policy->nb_targets > FF_FRAME_DECISION_ROI_MAX_TARGETS)
        return AVERROR(EINVAL);

    for (i = 0; i < policy->nb_targets; i++) {
        if (!target_id_is_valid(policy->targets[i].target_id) ||
            !qoffset_is_valid(policy->targets[i].qoffset))
            return AVERROR(EINVAL);
        for (j = 0; j < i; j++) {
            if (!strcmp(policy->targets[i].target_id,
                        policy->targets[j].target_id))
                return AVERROR(EINVAL);
        }
    }

    return 0;
}

int ff_frame_decision_roi_parse_target_qoffsets(
    FFFrameDecisionROITargetPolicy *policy, const char *spec)
{
    const char *item;

    if (!policy)
        return AVERROR(EINVAL);
    memset(policy, 0, sizeof(*policy));
    if (!spec || !spec[0])
        return 0;

    item = spec;
    for (;;) {
        const char *separator = strchr(item, '|');
        const char *end = separator ? separator : item + strlen(item);
        const char *equals = memchr(item, '=', end - item);
        FFFrameDecisionROITarget *target;
        size_t target_length;
        int ret;

        if (end == item || !equals || equals == item || equals + 1 == end ||
            memchr(equals + 1, '=', end - equals - 1))
            return AVERROR(EINVAL);
        if (policy->nb_targets >= FF_FRAME_DECISION_ROI_MAX_TARGETS)
            return AVERROR(E2BIG);

        target = &policy->targets[policy->nb_targets];
        target_length = equals - item;
        if (target_length >= sizeof(target->target_id))
            return AVERROR(EINVAL);
        memcpy(target->target_id, item, target_length);
        target->target_id[target_length] = '\0';
        if (!target_id_is_valid(target->target_id))
            return AVERROR(EINVAL);
        ret = parse_qoffset(equals + 1, end - equals - 1,
                            &target->qoffset);
        if (ret < 0)
            return ret;
        policy->nb_targets++;

        if (!separator)
            break;
        item = separator + 1;
    }

    return ff_frame_decision_roi_validate_target_policy(policy);
}

static const AVRational *target_qoffset(
    const FFFrameDecisionROITargetPolicy *policy, const char *target_id)
{
    int i;

    if (!policy || !target_id_is_valid(target_id))
        return NULL;
    for (i = 0; i < policy->nb_targets; i++) {
        if (!strcmp(policy->targets[i].target_id, target_id))
            return &policy->targets[i].qoffset;
    }
    return NULL;
}

int ff_frame_decision_roi_target_is_enabled(
    const FFFrameDecisionROITargetPolicy *policy, const char *target_id)
{
    const AVRational *qoffset = target_qoffset(policy, target_id);

    return qoffset && qoffset->num;
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
    const FFFrameDecisionROITargetPolicy *policy,
    int frame_width, int frame_height)
{
    NormalizedCandidate candidates[FF_FRAME_DECISION_ROI_MAX_DETECTIONS];
    FFFrameDecisionROINormalizeStats local_stats = { 0 };
    int nb_candidates = 0;
    int nb_input;
    int i;
    int j;

    if (!regions || !policy || nb_detections < 0 ||
        (nb_detections && !detections) ||
        frame_width <= 0 || frame_height <= 0)
        return AVERROR(EINVAL);
    if (ff_frame_decision_roi_validate_target_policy(policy) < 0)
        return AVERROR(EINVAL);

    nb_input = nb_detections;
    if (nb_input > FF_FRAME_DECISION_ROI_MAX_DETECTIONS) {
        local_stats.truncated += nb_input -
                                 FF_FRAME_DECISION_ROI_MAX_DETECTIONS;
        nb_input = FF_FRAME_DECISION_ROI_MAX_DETECTIONS;
    }

    for (i = 0; i < nb_input; i++) {
        const FFFrameDecisionROIDetection *src = &detections[i];
        const AVRational *qoffset = target_qoffset(policy, src->target_id);
        int64_t left;
        int64_t top;
        int64_t right;
        int64_t bottom;

        if (!target_id_is_valid(src->target_id) || !qoffset ||
            !isfinite(src->score) || src->score < 0.0 || src->score > 1.0 ||
            src->right <= src->left || src->bottom <= src->top) {
            local_stats.rejected++;
            continue;
        }
        if (!qoffset->num) {
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
                .qoffset   = *qoffset,
            },
            .priority = src->score * fabs(av_q2d(*qoffset)),
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
