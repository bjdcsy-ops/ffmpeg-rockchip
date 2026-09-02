/*
 * Narrow frame-decision client adapter for the ROI filter
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#ifndef AVFILTER_FRAME_DECISION_ROI_ADAPTER_H
#define AVFILTER_FRAME_DECISION_ROI_ADAPTER_H

#include <stddef.h>
#include <stdint.h>

#include "frame_decision_roi.h"

typedef struct FFFrameDecisionROIAdapter FFFrameDecisionROIAdapter;

typedef struct FFFrameDecisionROIAdapterConfig {
    void *log_ctx;
    const char *sdk_library;
    const char *socket_path;
    int channel_id;
    const FFFrameDecisionROITargetPolicy *target_policy;
    unsigned connect_timeout_ms;
    unsigned io_timeout_ms;
} FFFrameDecisionROIAdapterConfig;

typedef struct FFFrameDecisionROIDMABufRequest {
    uint64_t request_id;
    uint64_t frame_sequence;
    int64_t pts_us;
    int64_t capture_time_us;
    int64_t deadline_time_us;

    int fd;
    size_t object_size;
    uint64_t format_modifier;
    uint32_t drm_fourcc;
    int width;
    int height;
    int plane_count;
    uint32_t offsets[2];
    uint32_t pitches[2];
} FFFrameDecisionROIDMABufRequest;

typedef struct FFFrameDecisionROIAdapterResult {
    uint64_t request_id;
    uint64_t frame_sequence;
    int64_t pts_us;
    int status;
    int nb_detections;
    FFFrameDecisionROIDetection
        detections[FF_FRAME_DECISION_ROI_MAX_DETECTIONS];
} FFFrameDecisionROIAdapterResult;

typedef enum FFFrameDecisionROIInputDisposition {
    /* The filter may release the AVFrame which owns the DMA-BUF. */
    FF_FRAME_DECISION_ROI_INPUT_RELEASE = 0,

    /*
     * The server may still access the DMA-BUF and its state cannot be
     * resolved.  The filter must retain the AVFrame and stop submitting.
     */
    FF_FRAME_DECISION_ROI_INPUT_PIN_AND_STOP,
} FFFrameDecisionROIInputDisposition;

int ff_frame_decision_roi_adapter_create(
    FFFrameDecisionROIAdapter **adapter,
    const FFFrameDecisionROIAdapterConfig *config);

/**
 * Submit one DMA-BUF.
 *
 * A negative return guarantees the input was not transferred to the daemon
 * and is safe to release.  After success, await_input() is mandatory.
 */
int ff_frame_decision_roi_adapter_submit(
    FFFrameDecisionROIAdapter *adapter,
    const FFFrameDecisionROIDMABufRequest *request);

/**
 * Resolve source-buffer ownership after a successful submit.
 *
 * The V3 implementation owns reconnect, server-epoch, and QUERY_INPUT state.
 * It reports ambiguous same-epoch UNKNOWN state as PIN_AND_STOP rather than
 * exposing protocol details to the filter.
 */
int ff_frame_decision_roi_adapter_await_input(
    FFFrameDecisionROIAdapter *adapter,
    FFFrameDecisionROIInputDisposition *disposition);

int ff_frame_decision_roi_adapter_receive_result(
    FFFrameDecisionROIAdapter *adapter,
    FFFrameDecisionROIAdapterResult *result);

/* Safe to call from the filter shutdown thread. */
void ff_frame_decision_roi_adapter_interrupt(
    FFFrameDecisionROIAdapter *adapter);
void ff_frame_decision_roi_adapter_destroy(
    FFFrameDecisionROIAdapter **adapter);

#endif /* AVFILTER_FRAME_DECISION_ROI_ADAPTER_H */
