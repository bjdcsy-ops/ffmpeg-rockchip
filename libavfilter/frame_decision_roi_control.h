/*
 * Runtime control client for frame-decision ROI
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#ifndef AVFILTER_FRAME_DECISION_ROI_CONTROL_H
#define AVFILTER_FRAME_DECISION_ROI_CONTROL_H

#include <stddef.h>
#include <stdint.h>

#include "frame_decision_roi.h"

#define FF_FRAME_DECISION_ROI_CONTROL_MAGIC            UINT32_C(0x5346524f)
#define FF_FRAME_DECISION_ROI_CONTROL_VERSION          1
#define FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE      48
#define FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE 4096
#define FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL       8
#define FF_FRAME_DECISION_ROI_CONTROL_COORDINATE_SCALE \
    FF_FRAME_DECISION_ROI_COORDINATE_SCALE

#define FF_FRAME_DECISION_ROI_CONTROL_FLAG_FULL_REPLACE UINT16_C(0x0001)
#define FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE      UINT32_C(0x00000001)
#define FF_FRAME_DECISION_ROI_CONTROL_CAP_TARGETS        UINT32_C(0x00000001)
#define FF_FRAME_DECISION_ROI_CONTROL_CAP_MANUAL         UINT32_C(0x00000002)
#define FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES       \
    (FF_FRAME_DECISION_ROI_CONTROL_CAP_TARGETS |          \
     FF_FRAME_DECISION_ROI_CONTROL_CAP_MANUAL)

typedef enum FFFrameDecisionROIControlMessageType {
    FF_FRAME_DECISION_ROI_CONTROL_HELLO        = 1,
    FF_FRAME_DECISION_ROI_CONTROL_SERVER_HELLO = 2,
    FF_FRAME_DECISION_ROI_CONTROL_SNAPSHOT     = 3,
    FF_FRAME_DECISION_ROI_CONTROL_ACK          = 4,
} FFFrameDecisionROIControlMessageType;

typedef enum FFFrameDecisionROIControlStatus {
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK                  = 0,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_UNSUPPORTED_VERSION = 1,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_MALFORMED           = 2,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_WRONG_CHANNEL       = 3,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_LIMIT_EXCEEDED      = 4,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_TARGET      = 5,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_ROI         = 6,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_STALE_REVISION      = 7,
    FF_FRAME_DECISION_ROI_CONTROL_STATUS_INTERNAL            = 8,
} FFFrameDecisionROIControlStatus;

typedef struct FFFrameDecisionROIControlWireHeader {
    uint16_t version;
    uint16_t type;
    uint16_t flags;
    uint32_t message_size;
    uint32_t channel;
    int32_t status;
    uint64_t server_epoch;
    uint64_t revision;
    uint64_t correlation_id;
} FFFrameDecisionROIControlWireHeader;

typedef struct FFFrameDecisionROIControlHelloMessage {
    uint32_t channel;
    uint64_t correlation_id;
    uint64_t client_instance_id;
    uint32_t process_id;
    uint32_t capabilities;
    uint32_t max_message_size;
    uint16_t max_targets;
    uint16_t max_manual_regions;
} FFFrameDecisionROIControlHelloMessage;

typedef struct FFFrameDecisionROIControlServerHelloMessage {
    uint32_t channel;
    uint64_t server_epoch;
    uint64_t correlation_id;
    uint32_t capabilities;
    uint32_t max_message_size;
    uint16_t max_targets;
    uint16_t max_manual_regions;
} FFFrameDecisionROIControlServerHelloMessage;

typedef struct FFFrameDecisionROIControlSnapshot {
    FFFrameDecisionROITargetPolicy target_policy;
    FFFrameDecisionROIManualRegion
        manual_regions[FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL];
    int nb_manual_regions;
    uint64_t server_epoch;
    uint64_t revision;
} FFFrameDecisionROIControlSnapshot;

typedef struct FFFrameDecisionROIControlSnapshotMessage {
    FFFrameDecisionROIControlSnapshot snapshot;
    uint32_t channel;
    uint64_t correlation_id;
} FFFrameDecisionROIControlSnapshotMessage;

typedef struct FFFrameDecisionROIControlAckMessage {
    uint32_t channel;
    int32_t status;
    uint64_t server_epoch;
    uint64_t revision;
    uint64_t correlation_id;
    uint16_t accepted_targets;
    uint16_t accepted_manual_regions;
    uint32_t ack_flags;
} FFFrameDecisionROIControlAckMessage;

typedef struct FFFrameDecisionROIControlConfig {
    void *log_ctx;
    const char *socket_path;
    int channel;
    uint64_t client_instance_id;
    unsigned reconnect_delay_ms;
} FFFrameDecisionROIControlConfig;

typedef struct FFFrameDecisionROIControlClient
    FFFrameDecisionROIControlClient;

/* Wire helpers return the encoded byte count or zero on decode success. */
int ff_frame_decision_roi_control_encode_hello(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlHelloMessage *message);
int ff_frame_decision_roi_control_decode_hello(
    FFFrameDecisionROIControlHelloMessage *message,
    const uint8_t *src, size_t src_size);

int ff_frame_decision_roi_control_encode_server_hello(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlServerHelloMessage *message);
int ff_frame_decision_roi_control_decode_server_hello(
    FFFrameDecisionROIControlServerHelloMessage *message,
    const uint8_t *src, size_t src_size);

int ff_frame_decision_roi_control_encode_snapshot(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlSnapshotMessage *message);

/**
 * Decode and validate a full-replacement snapshot.
 *
 * protocol_status is set to MALFORMED for framing failures, or to the
 * semantic NACK status for target, ROI, or count failures.  A caller cannot
 * safely correlate every malformed frame and must close the connection
 * instead of attempting to acknowledge STATUS_MALFORMED.
 */
int ff_frame_decision_roi_control_decode_snapshot(
    FFFrameDecisionROIControlSnapshotMessage *message,
    int *protocol_status, const uint8_t *src, size_t src_size);

int ff_frame_decision_roi_control_encode_ack(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlAckMessage *message);
int ff_frame_decision_roi_control_decode_ack(
    FFFrameDecisionROIControlAckMessage *message,
    const uint8_t *src, size_t src_size);

int ff_frame_decision_roi_control_decode_header(
    FFFrameDecisionROIControlWireHeader *header,
    const uint8_t *src, size_t src_size);

/** Allocate and validate a client without performing socket I/O. */
int ff_frame_decision_roi_control_create(
    FFFrameDecisionROIControlClient **client,
    const FFFrameDecisionROIControlConfig *config);

/** Start the reconnecting background receiver. */
int ff_frame_decision_roi_control_start(
    FFFrameDecisionROIControlClient *client);

/**
 * Atomically copy the most recently published full snapshot.
 *
 * Returns 1 once for each new published state observed by this consumer,
 * zero when no newer state is available, or a negative AVERROR code.
 */
int ff_frame_decision_roi_control_get_latest(
    FFFrameDecisionROIControlClient *client,
    FFFrameDecisionROIControlSnapshot *snapshot);

/** Stop the worker, interrupt any socket wait, and release the client. */
void ff_frame_decision_roi_control_destroy(
    FFFrameDecisionROIControlClient **client);

#endif /* AVFILTER_FRAME_DECISION_ROI_CONTROL_H */
