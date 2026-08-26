/*
 * Frame decision ROI normalizer tests
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "libavutil/common.h"
#include "libavutil/error.h"
#include "libavutil/frame.h"

#include "libavfilter/frame_decision_roi.h"
#include "libavfilter/frame_decision_roi_adapter.h"

#define CHECK(condition, message) do {                                  \
    if (!(condition)) {                                                  \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, message);   \
        return 1;                                                        \
    }                                                                   \
} while (0)

static FFFrameDecisionROIWeights test_weights(void)
{
    FFFrameDecisionROIWeights weights = { 0 };

    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_NONE] =
        (AVRational) { 0, 1 };
    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_PERSON] =
        (AVRational) { -3, 20 };
    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_MOTOR_VEHICLE] =
        (AVRational) { -1, 4 };
    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_NON_MOTOR_VEHICLE] =
        (AVRational) { 1, 5 };
    return weights;
}

static int test_weight_validation(void)
{
    FFFrameDecisionROIWeights weights = test_weights();

    CHECK(ff_frame_decision_roi_validate_weights(&weights) == 0,
          "valid weights rejected");
    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_PERSON] =
        (AVRational) { 1, 0 };
    CHECK(ff_frame_decision_roi_validate_weights(&weights) < 0,
          "zero denominator accepted");
    weights = test_weights();
    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_PERSON] =
        (AVRational) { 21, 20 };
    CHECK(ff_frame_decision_roi_validate_weights(&weights) < 0,
          "out-of-range weight accepted");
    return 0;
}

static int test_normalize_and_merge(void)
{
    const FFFrameDecisionROIWeights weights = test_weights();
    const FFFrameDecisionROIDetection detections[] = {
        { FF_FRAME_DECISION_ROI_CATEGORY_PERSON,            0.90,
          17, 17, 31, 31 },
        { FF_FRAME_DECISION_ROI_CATEGORY_MOTOR_VEHICLE,      0.80,
          24, 16, 47, 31 },
        { FF_FRAME_DECISION_ROI_CATEGORY_NON_MOTOR_VEHICLE,  0.95,
          100, 100, 117, 117 },
        { 99,                                                 0.90,
          0, 0, 16, 16 },
        { FF_FRAME_DECISION_ROI_CATEGORY_PERSON,               NAN,
          0, 0, 16, 16 },
        { FF_FRAME_DECISION_ROI_CATEGORY_PERSON,              0.50,
          300, 300, 320, 320 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &weights, 192, 128);
    CHECK(count == 2, "unexpected normalized region count");
    CHECK(stats.merged == 1, "overlap was not merged");
    CHECK(stats.rejected == 3, "invalid detections not rejected");
    CHECK(regions[0].left == 16 && regions[0].top == 16 &&
          regions[0].right == 48 && regions[0].bottom == 32,
          "merged aligned bounds differ");
    CHECK(av_cmp_q(regions[0].qoffset, (AVRational) { -1, 4 }) == 0,
          "merged region did not keep the most protective qoffset");
    CHECK(regions[1].left == 96 && regions[1].top == 96 &&
          regions[1].right == 128 && regions[1].bottom == 128,
          "outward-aligned bounds differ");
    CHECK(regions[0].self_size == sizeof(regions[0]) &&
          regions[1].self_size == sizeof(regions[1]),
          "ROI self_size not initialized");
    return 0;
}

static int test_disabled_and_edge_clip(void)
{
    FFFrameDecisionROIWeights weights = test_weights();
    const FFFrameDecisionROIDetection detections[] = {
        { FF_FRAME_DECISION_ROI_CATEGORY_PERSON,       0.8,
          -20, -4, 17, 18 },
        { FF_FRAME_DECISION_ROI_CATEGORY_MOTOR_VEHICLE, 0.9,
          95, 95, 120, 130 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    weights.qoffset[FF_FRAME_DECISION_ROI_CATEGORY_PERSON] =
        (AVRational) { 0, 1 };
    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &weights, 100, 100);
    CHECK(count == 1, "disabled category was not removed");
    CHECK(stats.disabled == 1, "disabled category not counted");
    CHECK(regions[0].left == 80 && regions[0].top == 80 &&
          regions[0].right == 100 && regions[0].bottom == 100,
          "frame-edge clipping or alignment differs");
    return 0;
}

static int test_deterministic_cap(void)
{
    const FFFrameDecisionROIWeights weights = test_weights();
    FFFrameDecisionROIDetection detections[10];
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;
    int i;

    for (i = 0; i < FF_ARRAY_ELEMS(detections); i++) {
        detections[i] = (FFFrameDecisionROIDetection) {
            .category = FF_FRAME_DECISION_ROI_CATEGORY_PERSON,
            .score    = (i + 1) / 10.0,
            .left     = i * 32,
            .top      = 0,
            .right    = i * 32 + 16,
            .bottom   = 16,
        };
    }

    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &weights, 352, 64);
    CHECK(count == FF_FRAME_DECISION_ROI_MAX_REGIONS,
          "more than eight regions returned");
    CHECK(stats.truncated == 2, "truncation count differs");
    CHECK(regions[0].left == 288 && regions[7].left == 64,
          "importance ordering is not deterministic");
    return 0;
}

static int test_ttl(void)
{
    CHECK(ff_frame_decision_roi_snapshot_is_fresh(1000000, 1499999, 500),
          "fresh snapshot rejected");
    CHECK(ff_frame_decision_roi_snapshot_is_fresh(1000000, 1500000, 500),
          "TTL boundary rejected");
    CHECK(!ff_frame_decision_roi_snapshot_is_fresh(1000000, 1500001, 500),
          "expired snapshot accepted");
    CHECK(!ff_frame_decision_roi_snapshot_is_fresh(1000000, 999999, 500),
          "backwards monotonic time accepted");
    CHECK(!ff_frame_decision_roi_snapshot_is_fresh(-1, 1000000, 500),
          "unset snapshot accepted");
    return 0;
}

static int test_side_data(void)
{
    const AVRegionOfInterest first[] = {
        { sizeof(AVRegionOfInterest), 0, 16, 0, 16, { -1, 10 } },
    };
    const AVRegionOfInterest replacement[] = {
        { sizeof(AVRegionOfInterest), 16, 32, 16, 32, { -3, 20 } },
        { sizeof(AVRegionOfInterest), 48, 64, 48, 64, {  1,  5 } },
    };
    AVFrameSideData *side_data;
    const AVRegionOfInterest *actual;
    AVFrame *frame = av_frame_alloc();

    CHECK(frame, "frame allocation failed");
    CHECK(ff_frame_decision_roi_attach(frame, first,
                                       FF_ARRAY_ELEMS(first)) == 0,
          "initial side data attach failed");
    CHECK(ff_frame_decision_roi_attach(frame, replacement,
                                       FF_ARRAY_ELEMS(replacement)) == 0,
          "replacement side data attach failed");
    side_data = av_frame_get_side_data(
        frame, AV_FRAME_DATA_REGIONS_OF_INTEREST);
    CHECK(side_data && side_data->size == sizeof(replacement),
          "replacement side data size differs");
    actual = (const AVRegionOfInterest *)side_data->data;
    CHECK(!memcmp(actual, replacement, sizeof(replacement)),
          "replacement side data bytes differ");

    CHECK(ff_frame_decision_roi_attach(frame, NULL, 0) == 0,
          "empty side data clear failed");
    CHECK(!av_frame_get_side_data(frame,
                                  AV_FRAME_DATA_REGIONS_OF_INTEREST),
          "empty state did not clear ROI side data");
    av_frame_free(&frame);
    return 0;
}

static int test_missing_sdk_is_lazy_failure(void)
{
    FFFrameDecisionROIAdapterConfig config = {
        .sdk_library       = "/definitely/missing/libframe_decision.so.1",
        .socket_path       = "/tmp/frame-decision-test.sock",
        .channel_id        = 0,
        .connect_timeout_ms = 1,
        .io_timeout_ms      = 1,
    };
    FFFrameDecisionROIAdapter *adapter = (void *)1;

    CHECK(ff_frame_decision_roi_adapter_create(&adapter, &config) ==
              AVERROR(ENOENT),
          "missing SDK library did not fail explicitly");
    CHECK(!adapter, "failed SDK load returned an adapter");
    return 0;
}

int main(void)
{
    int ret = 0;

    ret |= test_weight_validation();
    ret |= test_normalize_and_merge();
    ret |= test_disabled_and_edge_clip();
    ret |= test_deterministic_cap();
    ret |= test_ttl();
    ret |= test_side_data();
    ret |= test_missing_sdk_is_lazy_failure();
    return ret;
}
