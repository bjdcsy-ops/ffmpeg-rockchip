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

static FFFrameDecisionROITargetPolicy test_policy(void)
{
    return (FFFrameDecisionROITargetPolicy) {
        .targets = {
            { "face_head",         { -3, 20 } },
            { "motor_vehicle",     { -1,  4 } },
            { "non_motor_vehicle", {  1,  5 } },
        },
        .nb_targets = 3,
    };
}

static int test_policy_parsing_and_validation(void)
{
    const char *invalid[] = {
        "face_head", "=1/2", "face_head=", "face_head=1/0",
        "face_head=2/1", "Face=-1/2", "face head=-1/2",
        "face_head=-1/2|", "|face_head=-1/2",
        "face_head=-1/2||motor_vehicle=-1/3",
        "face_head=-1/2|face_head=-1/3",
        "abcdefghijklmnopqrstuvwxyzabcdef=-1/2",
        "face_head=1/2/3", "face_head=0.5",
        "face_head= 1/2", "face_head=1/2 ",
    };
    FFFrameDecisionROITargetPolicy policy;
    FFFrameDecisionROITargetPolicy invalid_policy = test_policy();
    size_t i;

    CHECK(ff_frame_decision_roi_parse_target_qoffsets(
              &policy,
              "face_head=-1/17|motor_vehicle=-1/10|"
              "non_motor_vehicle=0") == 0,
          "valid target policy rejected");
    CHECK(policy.nb_targets == 3,
          "valid target count differs");
    CHECK(!strcmp(policy.targets[0].target_id, "face_head") &&
          av_cmp_q(policy.targets[0].qoffset,
                   (AVRational) { -1, 17 }) == 0,
          "face_head mapping differs");
    CHECK(ff_frame_decision_roi_target_is_enabled(&policy, "face_head"),
          "non-zero target was not enabled");
    CHECK(!ff_frame_decision_roi_target_is_enabled(
              &policy, "non_motor_vehicle"),
          "zero target was enabled");
    CHECK(!ff_frame_decision_roi_target_is_enabled(&policy, "unknown"),
          "unknown target was enabled");

    CHECK(ff_frame_decision_roi_parse_target_qoffsets(&policy, "") == 0 &&
          policy.nb_targets == 0,
          "empty policy did not disable detection");
    for (i = 0; i < FF_ARRAY_ELEMS(invalid); i++)
        CHECK(ff_frame_decision_roi_parse_target_qoffsets(
                  &policy, invalid[i]) < 0,
              "invalid target policy accepted");

    CHECK(ff_frame_decision_roi_parse_target_qoffsets(
              &policy,
              "a=0|b=0|c=0|d=0|e=0|f=0|g=0|h=0|i=0|j=0|k=0|"
              "l=0|m=0|n=0|o=0|p=0|q=0") == AVERROR(E2BIG),
          "more than sixteen targets accepted");

    invalid_policy.targets[0].qoffset = (AVRational) { 1, 0 };
    CHECK(ff_frame_decision_roi_validate_target_policy(&invalid_policy) < 0,
          "zero denominator accepted by validator");
    return 0;
}

static int test_normalize_and_merge(void)
{
    const FFFrameDecisionROITargetPolicy policy = test_policy();
    const FFFrameDecisionROIDetection detections[] = {
        { "face_head",                                      0.90,
          17, 17, 31, 31 },
        { "motor_vehicle",                                  0.80,
          24, 16, 47, 31 },
        { "non_motor_vehicle",                              0.95,
          100, 100, 117, 117 },
        { "bird",                                           0.90,
          0, 0, 16, 16 },
        { "face_head",                                       NAN,
          0, 0, 16, 16 },
        { "face_head",                                      0.50,
          300, 300, 320, 320 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &policy, 192, 128);
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
    FFFrameDecisionROITargetPolicy policy = test_policy();
    const FFFrameDecisionROIDetection detections[] = {
        { "face_head",                                  0.8,
          -20, -4, 17, 18 },
        { "motor_vehicle",                              0.9,
          95, 95, 120, 130 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    policy.targets[0].qoffset = (AVRational) { 0, 1 };
    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &policy, 100, 100);
    CHECK(count == 1, "disabled category was not removed");
    CHECK(stats.disabled == 1, "disabled category not counted");
    CHECK(regions[0].left == 80 && regions[0].top == 80 &&
          regions[0].right == 100 && regions[0].bottom == 100,
          "frame-edge clipping or alignment differs");
    return 0;
}

static int test_exact_target_lookup(void)
{
    FFFrameDecisionROITargetPolicy policy;
    const FFFrameDecisionROIDetection detection = {
        "face_head", 0.75, 16, 16, 32, 32,
    };
    AVRegionOfInterest region;
    int count;

    CHECK(ff_frame_decision_roi_parse_target_qoffsets(
              &policy, "face=-1/20|face_head=-1/2") == 0,
          "prefix target policy rejected");
    count = ff_frame_decision_roi_normalize(
        &region, NULL, &detection, 1, &policy, 64, 64);
    CHECK(count == 1, "exact target was not normalized");
    CHECK(av_cmp_q(region.qoffset, (AVRational) { -1, 2 }) == 0,
          "target lookup matched a prefix instead of the exact ID");
    return 0;
}

static int test_deterministic_cap(void)
{
    const FFFrameDecisionROITargetPolicy policy = test_policy();
    FFFrameDecisionROIDetection detections[10];
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;
    size_t i;

    for (i = 0; i < FF_ARRAY_ELEMS(detections); i++) {
        detections[i] = (FFFrameDecisionROIDetection) {
            .score    = (i + 1) / 10.0,
            .left     = i * 32,
            .top      = 0,
            .right    = i * 32 + 16,
            .bottom   = 16,
        };
        snprintf(detections[i].target_id,
                 sizeof(detections[i].target_id), "face_head");
    }

    count = ff_frame_decision_roi_normalize(
        regions, &stats, detections, FF_ARRAY_ELEMS(detections),
        &policy, 352, 64);
    CHECK(count == FF_FRAME_DECISION_ROI_MAX_REGIONS,
          "more than eight regions returned");
    CHECK(stats.truncated == 2, "truncation count differs");
    CHECK(regions[0].left == 288 && regions[7].left == 64,
          "importance ordering is not deterministic");
    return 0;
}

static int test_manual_coordinate_conversion(void)
{
    const FFFrameDecisionROIManualRegion manual_regions[] = {
        {
            .roi_id  = "small",
            .x       = 10000,
            .y       = 20000,
            .width   = 10000,
            .height  = 10000,
            .qoffset = { -1, 8 },
        },
        {
            .roi_id  = "edge",
            .x       = FF_FRAME_DECISION_ROI_COORDINATE_SCALE - 1,
            .y       = FF_FRAME_DECISION_ROI_COORDINATE_SCALE - 1,
            .width   = 1,
            .height  = 1,
            .qoffset = { -1, 4 },
        },
    };
    const FFFrameDecisionROIManualRegion full_frame = {
        .roi_id  = "full",
        .x       = 0,
        .y       = 0,
        .width   = FF_FRAME_DECISION_ROI_COORDINATE_SCALE,
        .height  = FF_FRAME_DECISION_ROI_COORDINATE_SCALE,
        .qoffset = { -1, 16 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_compose_normalized(
        regions, &stats, NULL, 0, manual_regions,
        FF_ARRAY_ELEMS(manual_regions), 1920, 1080);
    CHECK(count == 2, "manual coordinate conversion count differs");
    CHECK(regions[0].left == 1904 && regions[0].top == 1072 &&
          regions[0].right == 1920 && regions[0].bottom == 1080,
          "right-edge ppm conversion or clipping differs");
    CHECK(regions[1].left == 16 && regions[1].top == 16 &&
          regions[1].right == 48 && regions[1].bottom == 48,
          "ppm floor/ceil or outward alignment differs");
    CHECK(av_cmp_q(regions[0].qoffset, (AVRational) { -1, 4 }) == 0 &&
          av_cmp_q(regions[1].qoffset, (AVRational) { -1, 8 }) == 0,
          "manual absolute-qoffset ordering differs");
    CHECK(!stats.rejected && !stats.disabled && !stats.merged &&
          !stats.truncated,
          "valid manual coordinates changed statistics");

    count = ff_frame_decision_roi_compose_normalized(
        regions, NULL, NULL, 0, &full_frame, 1, 1919, 1079);
    CHECK(count == 1 && regions[0].left == 0 && regions[0].top == 0 &&
          regions[0].right == 1919 && regions[0].bottom == 1079,
          "full-frame ppm boundary was not preserved");
    return 0;
}

static int test_manual_validation(void)
{
    const FFFrameDecisionROIManualRegion manual_regions[] = {
        {
            .x       = FF_FRAME_DECISION_ROI_COORDINATE_SCALE,
            .width   = 1,
            .height  = 1,
            .qoffset = { -1, 8 },
        },
        {
            .y       = FF_FRAME_DECISION_ROI_COORDINATE_SCALE,
            .width   = 1,
            .height  = 1,
            .qoffset = { -1, 8 },
        },
        {
            .width   = 0,
            .height  = 1,
            .qoffset = { -1, 8 },
        },
        {
            .x       = 900000,
            .width   = 100001,
            .height  = 1,
            .qoffset = { -1, 8 },
        },
        {
            .width   = 1,
            .height  = 1,
            .qoffset = { -1, 0 },
        },
        {
            .width   = 1,
            .height  = 1,
            .qoffset = { -2, 1 },
        },
        {
            .width   = 1,
            .height  = 1,
            .qoffset = { 0, 1 },
        },
    };
    FFFrameDecisionROIManualRegion too_many[
        FF_FRAME_DECISION_ROI_MAX_REGIONS + 1] = { 0 };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_compose_normalized(
        regions, &stats, NULL, 0, manual_regions,
        FF_ARRAY_ELEMS(manual_regions), 64, 64);
    CHECK(count == 0, "invalid or disabled manual ROI was emitted");
    CHECK(stats.rejected == 6 && stats.disabled == 1,
          "manual rejection statistics differ");
    CHECK(ff_frame_decision_roi_compose_normalized(
              regions, NULL, NULL, 0, NULL, -1, 64, 64) ==
              AVERROR(EINVAL),
          "negative manual count accepted");
    CHECK(ff_frame_decision_roi_compose_normalized(
              regions, NULL, NULL, 0, too_many,
              FF_ARRAY_ELEMS(too_many), 64, 64) == AVERROR(E2BIG),
          "more than eight manual regions accepted");
    CHECK(ff_frame_decision_roi_compose_normalized(
              regions, NULL, NULL, 0, manual_regions, 1, 0, 64) ==
              AVERROR(EINVAL),
          "zero frame width accepted by composer");
    return 0;
}

static int test_compose_repeated_union_and_qoffset(void)
{
    const AVRegionOfInterest detector_regions[] = {
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 64, .left = 0, .right = 16,
            .qoffset = { -1, 8 },
        },
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 32, .bottom = 48, .left = 32, .right = 48,
            .qoffset = { 1, 4 },
        },
    };
    const FFFrameDecisionROIManualRegion manual_region = {
        .roi_id  = "bridge",
        .x       = 0,
        .y       = 0,
        .width   = FF_FRAME_DECISION_ROI_COORDINATE_SCALE,
        .height  = FF_FRAME_DECISION_ROI_COORDINATE_SCALE / 4,
        .qoffset = { -1, 2 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_compose_normalized(
        regions, &stats, detector_regions,
        FF_ARRAY_ELEMS(detector_regions), &manual_region, 1, 64, 64);
    CHECK(count == 1, "transitive overlap was not fully merged");
    CHECK(stats.merged == 2, "transitive merge count differs");
    CHECK(regions[0].left == 0 && regions[0].top == 0 &&
          regions[0].right == 64 && regions[0].bottom == 64,
          "transitive rectangular union bounds differ");
    CHECK(av_cmp_q(regions[0].qoffset, (AVRational) { -1, 2 }) == 0,
          "merged ROI did not retain the lowest qoffset");
    return 0;
}

static int test_manual_priority_at_cap(void)
{
    AVRegionOfInterest detector_regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROIManualRegion manual_regions[
        FF_FRAME_DECISION_ROI_MAX_REGIONS];
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;
    int i;

    for (i = 0; i < FF_FRAME_DECISION_ROI_MAX_REGIONS; i++) {
        detector_regions[i] = (AVRegionOfInterest) {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 16,
            .left = i * 32, .right = i * 32 + 16,
            .qoffset = { -1, 1 },
        };
        manual_regions[i] = (FFFrameDecisionROIManualRegion) {
            .x       = i * 62500,
            .y       = 500000,
            .width   = 31250,
            .height  = 250000,
            .qoffset = { -1, 100 },
        };
    }

    count = ff_frame_decision_roi_compose_normalized(
        regions, &stats, detector_regions,
        FF_ARRAY_ELEMS(detector_regions), manual_regions,
        FF_ARRAY_ELEMS(manual_regions), 512, 64);
    CHECK(count == FF_FRAME_DECISION_ROI_MAX_REGIONS,
          "composed ROI cap differs");
    CHECK(stats.truncated == FF_FRAME_DECISION_ROI_MAX_REGIONS,
          "composed truncation count differs");
    for (i = 0; i < count; i++) {
        CHECK(regions[i].top == 32 && regions[i].bottom == 48 &&
              regions[i].left == i * 32 &&
              regions[i].right == i * 32 + 16,
              "detector ROI displaced a manual ROI at the cap");
    }
    return 0;
}

static int test_raw_detection_and_manual_compose(void)
{
    const FFFrameDecisionROITargetPolicy policy = test_policy();
    const FFFrameDecisionROIDetection detection = {
        "face_head", 1.0, 0, 0, 16, 16,
    };
    const FFFrameDecisionROIManualRegion manual_region = {
        .roi_id  = "manual",
        .x       = 750000,
        .y       = 750000,
        .width   = 250000,
        .height  = 250000,
        .qoffset = { -1, 16 },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    int count;

    count = ff_frame_decision_roi_compose(
        regions, NULL, &detection, 1, &policy, &manual_region, 1,
        64, 64);
    CHECK(count == 2, "raw detector/manual composition count differs");
    CHECK(regions[0].left == 48 && regions[0].top == 48 &&
          regions[0].right == 64 && regions[0].bottom == 64,
          "manual ROI did not sort ahead of detector ROI");
    CHECK(regions[1].left == 0 && regions[1].top == 0 &&
          regions[1].right == 16 && regions[1].bottom == 16,
          "raw detector ROI was not normalized by composer");
    return 0;
}

static int test_normalized_detector_validation(void)
{
    const AVRegionOfInterest detector_regions[] = {
        {
            .self_size = 0,
            .top = 0, .bottom = 16, .left = 0, .right = 16,
            .qoffset = { -1, 8 },
        },
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 16, .left = 0, .right = 16,
            .qoffset = { -1, 0 },
        },
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 16, .left = 0, .right = 16,
            .qoffset = { 0, 1 },
        },
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 80, .bottom = 96, .left = 80, .right = 96,
            .qoffset = { -1, 8 },
        },
    };
    AVRegionOfInterest too_many[FF_FRAME_DECISION_ROI_MAX_REGIONS + 1];
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    int count;

    count = ff_frame_decision_roi_compose_normalized(
        regions, &stats, detector_regions,
        FF_ARRAY_ELEMS(detector_regions), NULL, 0, 64, 64);
    CHECK(count == 0, "invalid or disabled detector ROI was emitted");
    CHECK(stats.rejected == 3 && stats.disabled == 1,
          "normalized detector rejection statistics differ");
    CHECK(ff_frame_decision_roi_compose_normalized(
              regions, NULL, too_many, FF_ARRAY_ELEMS(too_many),
              NULL, 0, 64, 64) == AVERROR(E2BIG),
          "more than eight normalized detector regions accepted");
    return 0;
}

static int test_normalized_detector_order_is_retained(void)
{
    const AVRegionOfInterest detector_regions[] = {
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 16, .left = 0, .right = 16,
            .qoffset = { -1, 8 },
        },
        {
            .self_size = sizeof(AVRegionOfInterest),
            .top = 0, .bottom = 16, .left = 32, .right = 48,
            .qoffset = { -1, 2 },
        },
    };
    AVRegionOfInterest regions[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    int count;

    count = ff_frame_decision_roi_compose_normalized(
        regions, NULL, detector_regions,
        FF_ARRAY_ELEMS(detector_regions), NULL, 0, 64, 64);
    CHECK(count == 2 && regions[0].left == 0 && regions[1].left == 32,
          "pre-sorted detector snapshot order was changed");
    return 0;
}

static int test_result_deadline(void)
{
    CHECK(ff_frame_decision_roi_result_is_fresh(1000000, 1499999, 500),
          "fresh result rejected");
    CHECK(ff_frame_decision_roi_result_is_fresh(1000000, 1500000, 500),
          "result deadline boundary rejected");
    CHECK(!ff_frame_decision_roi_result_is_fresh(1000000, 1500001, 500),
          "expired result accepted");
    CHECK(!ff_frame_decision_roi_result_is_fresh(1000000, 999999, 500),
          "backwards monotonic time accepted");
    CHECK(!ff_frame_decision_roi_result_is_fresh(-1, 1000000, 500),
          "unset capture time accepted");
    return 0;
}

static int test_result_deadline_and_reuse_are_independent(void)
{
    const int64_t capture_time_us = 1000000;
    const int64_t accept_time_us = 1490000;
    const int64_t output_time_us = 1900000;

    CHECK(ff_frame_decision_roi_result_is_fresh(
              capture_time_us, accept_time_us, 500),
          "result inside source deadline was rejected");
    CHECK(!ff_frame_decision_roi_result_is_fresh(
               capture_time_us, output_time_us, 500),
          "output time unexpectedly met the source result deadline");
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              accept_time_us, -1, output_time_us, 300, 500),
          "accepted ROI was not reused independently of source age");
    return 0;
}

static int test_empty_miss_hold(void)
{
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              1000000, -1, 1100000, 300, 500),
          "snapshot without an empty result was not applied");
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              1000000, 1100000, 1100000, 300, 500),
          "first empty result was not held");
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              1000000, 1100000, 1399999, 300, 500),
          "empty result inside miss window was not held");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               1000000, 1100000, 1400000, 300, 500),
          "empty result at miss-window boundary was held");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               1000000, 1100000, 1100000, 0, 500),
          "disabled miss hold retained an empty result");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               1000000, 1200000, 1199999, 300, 500),
          "backwards result time was held");
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              1000000, 1490000, 1500000, 300, 500),
          "reuse TTL boundary did not retain ROI after empty result");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               1000000, 1490000, 1500001, 300, 500),
          "miss hold exceeded the reuse TTL hard limit");
    CHECK(ff_frame_decision_roi_snapshot_should_apply(
              1000000, -1, 1500000, 300, 500),
          "reuse TTL boundary did not retain ROI");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               1000000, -1, 1500001, 300, 500),
          "service silence exceeded the reuse TTL");
    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               -1, -1, 1000000, 300, 500),
          "unset accepted-result time retained ROI");
    return 0;
}

static int test_single_empty_expires_without_followup(void)
{
    const int64_t snapshot_accept_time_us = 1000000;
    const int64_t first_empty_result_time_us = 1100000;
    const int64_t later_output_time_us = 1400000;

    CHECK(!ff_frame_decision_roi_snapshot_should_apply(
               snapshot_accept_time_us, first_empty_result_time_us,
               later_output_time_us, 300, 500),
          "single empty result without follow-up survived miss hold");
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
    const FFFrameDecisionROITargetPolicy policy = test_policy();
    FFFrameDecisionROIAdapterConfig config = {
        .sdk_library       = "/definitely/missing/libframe_decision.so.2",
        .socket_path       = "/tmp/frame-decision-test.sock",
        .channel_id        = 0,
        .target_policy     = &policy,
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

    ret |= test_policy_parsing_and_validation();
    ret |= test_normalize_and_merge();
    ret |= test_disabled_and_edge_clip();
    ret |= test_exact_target_lookup();
    ret |= test_deterministic_cap();
    ret |= test_manual_coordinate_conversion();
    ret |= test_manual_validation();
    ret |= test_compose_repeated_union_and_qoffset();
    ret |= test_manual_priority_at_cap();
    ret |= test_raw_detection_and_manual_compose();
    ret |= test_normalized_detector_validation();
    ret |= test_normalized_detector_order_is_retained();
    ret |= test_result_deadline();
    ret |= test_result_deadline_and_reuse_are_independent();
    ret |= test_empty_miss_hold();
    ret |= test_single_empty_expires_without_followup();
    ret |= test_side_data();
    ret |= test_missing_sdk_is_lazy_failure();
    return ret;
}
