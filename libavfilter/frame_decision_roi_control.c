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

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "libavutil/error.h"
#include "libavutil/intreadwrite.h"
#include "libavutil/log.h"

#include "frame_decision_roi_control.h"

#define SFRO_HELLO_SIZE              80
#define SFRO_SERVER_HELLO_SIZE       64
#define SFRO_SNAPSHOT_PREFIX_SIZE    64
#define SFRO_TARGET_SIZE             40
#define SFRO_MANUAL_SIZE             64
#define SFRO_ACK_SIZE                64
#define SFRO_MAX_SNAPSHOT_SIZE       \
    (SFRO_SNAPSHOT_PREFIX_SIZE +     \
     FF_FRAME_DECISION_ROI_MAX_TARGETS * SFRO_TARGET_SIZE + \
     FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL * SFRO_MANUAL_SIZE)
#define SFRO_RECONNECT_DEFAULT_MS    100
#define SFRO_RECONNECT_MAX_MS        2000

struct FFFrameDecisionROIControlClient {
    void *log_ctx;
    char *socket_path;
    uint32_t channel;
    uint64_t client_instance_id;
    unsigned reconnect_delay_ms;

    pthread_mutex_t lock;
    pthread_t worker;
    int wake_fds[2];
    int active_fd;
    int started;
    int stop;

    FFFrameDecisionROIControlSnapshot latest;
    uint64_t publish_serial;
    uint64_t read_serial;
    int has_snapshot;
    uint64_t next_correlation_id;
};

static int channel_is_valid(uint32_t channel)
{
    return channel >= 1 && channel <= 16;
}

static int status_is_valid(int32_t status)
{
    return status >= FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK &&
           status <= FF_FRAME_DECISION_ROI_CONTROL_STATUS_INTERNAL;
}

static int id_is_valid(const char id[FF_FRAME_DECISION_ROI_TARGET_ID_SIZE])
{
    const char *terminator;
    size_t length;
    size_t i;

    terminator = memchr(id, '\0', FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
    if (!terminator)
        return 0;
    length = terminator - id;
    if (!length ||
        !((id[0] >= 'a' && id[0] <= 'z') ||
          (id[0] >= '0' && id[0] <= '9')))
        return 0;
    for (i = 1; i < length; i++) {
        const unsigned char c = id[i];

        if (!((c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') ||
              c == '.' || c == '_' || c == '-'))
            return 0;
    }
    for (i = length + 1; i < FF_FRAME_DECISION_ROI_TARGET_ID_SIZE; i++) {
        if (id[i])
            return 0;
    }
    return 1;
}

static int qoffset_is_valid(AVRational qoffset, int allow_zero)
{
    int64_t numerator;

    if (qoffset.den <= 0 || (!allow_zero && !qoffset.num))
        return 0;
    numerator = qoffset.num;
    if (numerator < 0)
        numerator = -numerator;
    return numerator <= qoffset.den;
}

static int target_policy_is_valid(
    const FFFrameDecisionROITargetPolicy *policy)
{
    int i;
    int j;

    if (!policy || policy->nb_targets < 0 ||
        policy->nb_targets > FF_FRAME_DECISION_ROI_MAX_TARGETS)
        return 0;
    for (i = 0; i < policy->nb_targets; i++) {
        const FFFrameDecisionROITarget *target = &policy->targets[i];

        if (!id_is_valid(target->target_id) ||
            !qoffset_is_valid(target->qoffset, 0))
            return 0;
        for (j = 0; j < i; j++) {
            if (!strcmp(target->target_id, policy->targets[j].target_id))
                return 0;
        }
    }
    return 1;
}

static int manual_region_is_valid(
    const FFFrameDecisionROIManualRegion *region)
{
    const uint64_t scale = FF_FRAME_DECISION_ROI_CONTROL_COORDINATE_SCALE;

    if (!id_is_valid(region->roi_id) ||
        !qoffset_is_valid(region->qoffset, 0) ||
        !region->width || !region->height ||
        region->x > scale || region->y > scale ||
        region->width > scale || region->height > scale ||
        (uint64_t)region->x + region->width > scale ||
        (uint64_t)region->y + region->height > scale)
        return 0;
    return 1;
}

static int manual_regions_are_valid(
    const FFFrameDecisionROIManualRegion *regions, int nb_regions)
{
    int i;
    int j;

    if (nb_regions < 0 ||
        nb_regions > FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL ||
        (nb_regions && !regions))
        return 0;
    for (i = 0; i < nb_regions; i++) {
        if (!manual_region_is_valid(&regions[i]))
            return 0;
        for (j = 0; j < i; j++) {
            if (!strcmp(regions[i].roi_id, regions[j].roi_id))
                return 0;
        }
    }
    return 1;
}

static void encode_header(uint8_t *dst, uint16_t type, uint16_t flags,
                          uint32_t message_size, uint32_t channel,
                          int32_t status, uint64_t server_epoch,
                          uint64_t revision, uint64_t correlation_id)
{
    AV_WB32(dst +  0, FF_FRAME_DECISION_ROI_CONTROL_MAGIC);
    AV_WB16(dst +  4, FF_FRAME_DECISION_ROI_CONTROL_VERSION);
    AV_WB16(dst +  6, FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    AV_WB16(dst +  8, type);
    AV_WB16(dst + 10, flags);
    AV_WB32(dst + 12, message_size);
    AV_WB32(dst + 16, channel);
    AV_WB32(dst + 20, (uint32_t)status);
    AV_WB64(dst + 24, server_epoch);
    AV_WB64(dst + 32, revision);
    AV_WB64(dst + 40, correlation_id);
}

int ff_frame_decision_roi_control_decode_header(
    FFFrameDecisionROIControlWireHeader *header,
    const uint8_t *src, size_t src_size)
{
    FFFrameDecisionROIControlWireHeader parsed;

    if (!header || !src ||
        src_size < FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE)
        return AVERROR(EINVAL);
    if (AV_RB32(src + 0) != FF_FRAME_DECISION_ROI_CONTROL_MAGIC ||
        AV_RB16(src + 6) != FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE)
        return AVERROR_INVALIDDATA;

    parsed.version        = AV_RB16(src + 4);
    parsed.type           = AV_RB16(src + 8);
    parsed.flags          = AV_RB16(src + 10);
    parsed.message_size   = AV_RB32(src + 12);
    parsed.channel        = AV_RB32(src + 16);
    parsed.status         = (int32_t)AV_RB32(src + 20);
    parsed.server_epoch   = AV_RB64(src + 24);
    parsed.revision       = AV_RB64(src + 32);
    parsed.correlation_id = AV_RB64(src + 40);
    if (parsed.message_size < FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE ||
        parsed.message_size > FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE)
        return AVERROR_INVALIDDATA;

    *header = parsed;
    return 0;
}

static int decode_message_header(FFFrameDecisionROIControlWireHeader *header,
                                 const uint8_t *src, size_t src_size,
                                 uint16_t type, uint32_t message_size,
                                 uint16_t flags, int require_epoch)
{
    int ret = ff_frame_decision_roi_control_decode_header(header, src,
                                                           src_size);

    if (ret < 0)
        return ret;
    if (src_size != message_size || header->message_size != message_size ||
        header->version != FF_FRAME_DECISION_ROI_CONTROL_VERSION ||
        header->type != type || header->flags != flags ||
        !channel_is_valid(header->channel) ||
        !status_is_valid(header->status) ||
        !header->correlation_id ||
        (require_epoch && !header->server_epoch))
        return AVERROR_INVALIDDATA;
    return 0;
}

static int hello_limits_are_valid(uint32_t capabilities,
                                  uint32_t max_message_size,
                                  uint16_t max_targets,
                                  uint16_t max_manual_regions)
{
    return capabilities == FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES &&
           max_message_size >= SFRO_MAX_SNAPSHOT_SIZE &&
           max_message_size <= FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE &&
           max_targets == FF_FRAME_DECISION_ROI_MAX_TARGETS &&
           max_manual_regions == FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL;
}

int ff_frame_decision_roi_control_encode_hello(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlHelloMessage *message)
{
    if (!dst || !message || dst_size < SFRO_HELLO_SIZE ||
        !channel_is_valid(message->channel) ||
        !message->correlation_id || !message->client_instance_id ||
        !message->process_id ||
        !hello_limits_are_valid(message->capabilities,
                                message->max_message_size,
                                message->max_targets,
                                message->max_manual_regions))
        return AVERROR(EINVAL);

    memset(dst, 0, SFRO_HELLO_SIZE);
    encode_header(dst, FF_FRAME_DECISION_ROI_CONTROL_HELLO, 0,
                  SFRO_HELLO_SIZE, message->channel,
                  FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK, 0, 0,
                  message->correlation_id);
    AV_WB64(dst + 48, message->client_instance_id);
    AV_WB32(dst + 56, message->process_id);
    AV_WB32(dst + 60, message->capabilities);
    AV_WB32(dst + 64, message->max_message_size);
    AV_WB16(dst + 68, message->max_targets);
    AV_WB16(dst + 70, message->max_manual_regions);
    return SFRO_HELLO_SIZE;
}

int ff_frame_decision_roi_control_decode_hello(
    FFFrameDecisionROIControlHelloMessage *message,
    const uint8_t *src, size_t src_size)
{
    FFFrameDecisionROIControlWireHeader header;
    FFFrameDecisionROIControlHelloMessage parsed;
    int ret;

    if (!message)
        return AVERROR(EINVAL);
    ret = decode_message_header(&header, src, src_size,
                                FF_FRAME_DECISION_ROI_CONTROL_HELLO,
                                SFRO_HELLO_SIZE, 0, 0);
    if (ret < 0 || header.status || header.server_epoch || header.revision ||
        AV_RB64(src + 72))
        return AVERROR_INVALIDDATA;

    parsed = (FFFrameDecisionROIControlHelloMessage) {
        .channel             = header.channel,
        .correlation_id      = header.correlation_id,
        .client_instance_id  = AV_RB64(src + 48),
        .process_id          = AV_RB32(src + 56),
        .capabilities        = AV_RB32(src + 60),
        .max_message_size    = AV_RB32(src + 64),
        .max_targets         = AV_RB16(src + 68),
        .max_manual_regions  = AV_RB16(src + 70),
    };
    if (!parsed.client_instance_id || !parsed.process_id ||
        !hello_limits_are_valid(parsed.capabilities,
                                parsed.max_message_size,
                                parsed.max_targets,
                                parsed.max_manual_regions))
        return AVERROR_INVALIDDATA;
    *message = parsed;
    return 0;
}

int ff_frame_decision_roi_control_encode_server_hello(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlServerHelloMessage *message)
{
    if (!dst || !message || dst_size < SFRO_SERVER_HELLO_SIZE ||
        !channel_is_valid(message->channel) || !message->server_epoch ||
        !message->correlation_id ||
        message->max_message_size !=
            FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE ||
        !hello_limits_are_valid(message->capabilities,
                                message->max_message_size,
                                message->max_targets,
                                message->max_manual_regions))
        return AVERROR(EINVAL);

    memset(dst, 0, SFRO_SERVER_HELLO_SIZE);
    encode_header(dst, FF_FRAME_DECISION_ROI_CONTROL_SERVER_HELLO, 0,
                  SFRO_SERVER_HELLO_SIZE, message->channel,
                  FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
                  message->server_epoch, 0, message->correlation_id);
    AV_WB32(dst + 48, message->capabilities);
    AV_WB32(dst + 52, message->max_message_size);
    AV_WB16(dst + 56, message->max_targets);
    AV_WB16(dst + 58, message->max_manual_regions);
    return SFRO_SERVER_HELLO_SIZE;
}

int ff_frame_decision_roi_control_decode_server_hello(
    FFFrameDecisionROIControlServerHelloMessage *message,
    const uint8_t *src, size_t src_size)
{
    FFFrameDecisionROIControlWireHeader header;
    FFFrameDecisionROIControlServerHelloMessage parsed;
    int ret;

    if (!message)
        return AVERROR(EINVAL);
    ret = decode_message_header(&header, src, src_size,
                                FF_FRAME_DECISION_ROI_CONTROL_SERVER_HELLO,
                                SFRO_SERVER_HELLO_SIZE, 0, 1);
    if (ret < 0 || header.status || header.revision || AV_RB32(src + 60))
        return AVERROR_INVALIDDATA;

    parsed = (FFFrameDecisionROIControlServerHelloMessage) {
        .channel             = header.channel,
        .server_epoch        = header.server_epoch,
        .correlation_id      = header.correlation_id,
        .capabilities        = AV_RB32(src + 48),
        .max_message_size    = AV_RB32(src + 52),
        .max_targets         = AV_RB16(src + 56),
        .max_manual_regions  = AV_RB16(src + 58),
    };
    if (!hello_limits_are_valid(parsed.capabilities,
                                parsed.max_message_size,
                                parsed.max_targets,
                                parsed.max_manual_regions) ||
        parsed.max_message_size !=
            FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE)
        return AVERROR_INVALIDDATA;
    *message = parsed;
    return 0;
}

static size_t snapshot_message_size(int nb_targets, int nb_manual_regions)
{
    return SFRO_SNAPSHOT_PREFIX_SIZE +
           (size_t)nb_targets * SFRO_TARGET_SIZE +
           (size_t)nb_manual_regions * SFRO_MANUAL_SIZE;
}

int ff_frame_decision_roi_control_encode_snapshot(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlSnapshotMessage *message)
{
    const FFFrameDecisionROIControlSnapshot *snapshot;
    size_t message_size;
    size_t offset;
    int i;

    if (!dst || !message)
        return AVERROR(EINVAL);
    snapshot = &message->snapshot;
    if (!channel_is_valid(message->channel) ||
        !message->correlation_id || !snapshot->server_epoch ||
        !target_policy_is_valid(&snapshot->target_policy) ||
        !manual_regions_are_valid(snapshot->manual_regions,
                                  snapshot->nb_manual_regions))
        return AVERROR(EINVAL);
    message_size = snapshot_message_size(snapshot->target_policy.nb_targets,
                                         snapshot->nb_manual_regions);
    if (message_size > FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE ||
        dst_size < message_size)
        return AVERROR(EINVAL);

    memset(dst, 0, message_size);
    encode_header(dst, FF_FRAME_DECISION_ROI_CONTROL_SNAPSHOT,
                  FF_FRAME_DECISION_ROI_CONTROL_FLAG_FULL_REPLACE,
                  message_size, message->channel,
                  FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
                  snapshot->server_epoch, snapshot->revision,
                  message->correlation_id);
    AV_WB16(dst + 48, snapshot->target_policy.nb_targets);
    AV_WB16(dst + 50, snapshot->nb_manual_regions);
    AV_WB32(dst + 52, FF_FRAME_DECISION_ROI_CONTROL_COORDINATE_SCALE);

    offset = SFRO_SNAPSHOT_PREFIX_SIZE;
    for (i = 0; i < snapshot->target_policy.nb_targets; i++) {
        const FFFrameDecisionROITarget *target =
            &snapshot->target_policy.targets[i];

        memcpy(dst + offset, target->target_id,
               FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
        AV_WB32(dst + offset + 32, (uint32_t)target->qoffset.num);
        AV_WB32(dst + offset + 36, (uint32_t)target->qoffset.den);
        offset += SFRO_TARGET_SIZE;
    }
    for (i = 0; i < snapshot->nb_manual_regions; i++) {
        const FFFrameDecisionROIManualRegion *region =
            &snapshot->manual_regions[i];

        memcpy(dst + offset, region->roi_id,
               FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
        AV_WB32(dst + offset + 32, region->x);
        AV_WB32(dst + offset + 36, region->y);
        AV_WB32(dst + offset + 40, region->width);
        AV_WB32(dst + offset + 44, region->height);
        AV_WB32(dst + offset + 48, (uint32_t)region->qoffset.num);
        AV_WB32(dst + offset + 52, (uint32_t)region->qoffset.den);
        offset += SFRO_MANUAL_SIZE;
    }
    return message_size;
}

int ff_frame_decision_roi_control_decode_snapshot(
    FFFrameDecisionROIControlSnapshotMessage *message,
    int *protocol_status, const uint8_t *src, size_t src_size)
{
    FFFrameDecisionROIControlSnapshotMessage parsed = { 0 };
    FFFrameDecisionROIControlWireHeader header;
    uint16_t nb_targets;
    uint16_t nb_manual_regions;
    size_t expected_size;
    size_t offset;
    int i;
    int ret;

    if (!message || !protocol_status)
        return AVERROR(EINVAL);
    *protocol_status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_MALFORMED;
    ret = ff_frame_decision_roi_control_decode_header(&header, src, src_size);
    if (ret < 0 || header.version != FF_FRAME_DECISION_ROI_CONTROL_VERSION ||
        header.type != FF_FRAME_DECISION_ROI_CONTROL_SNAPSHOT ||
        header.flags != FF_FRAME_DECISION_ROI_CONTROL_FLAG_FULL_REPLACE ||
        header.message_size != src_size || src_size < SFRO_SNAPSHOT_PREFIX_SIZE ||
        !channel_is_valid(header.channel) || header.status ||
        !header.server_epoch || !header.correlation_id ||
        AV_RB32(src + 52) !=
            FF_FRAME_DECISION_ROI_CONTROL_COORDINATE_SCALE ||
        AV_RB32(src + 56) || AV_RB32(src + 60))
        return AVERROR_INVALIDDATA;

    nb_targets = AV_RB16(src + 48);
    nb_manual_regions = AV_RB16(src + 50);
    if (nb_targets > FF_FRAME_DECISION_ROI_MAX_TARGETS ||
        nb_manual_regions > FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL) {
        *protocol_status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_LIMIT_EXCEEDED;
        return AVERROR(E2BIG);
    }
    expected_size = snapshot_message_size(nb_targets, nb_manual_regions);
    if (src_size != expected_size)
        return AVERROR_INVALIDDATA;

    parsed.channel = header.channel;
    parsed.correlation_id = header.correlation_id;
    parsed.snapshot.server_epoch = header.server_epoch;
    parsed.snapshot.revision = header.revision;
    parsed.snapshot.target_policy.nb_targets = nb_targets;
    parsed.snapshot.nb_manual_regions = nb_manual_regions;

    offset = SFRO_SNAPSHOT_PREFIX_SIZE;
    for (i = 0; i < nb_targets; i++) {
        FFFrameDecisionROITarget *target =
            &parsed.snapshot.target_policy.targets[i];

        memcpy(target->target_id, src + offset,
               FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
        target->qoffset.num = (int32_t)AV_RB32(src + offset + 32);
        target->qoffset.den = AV_RB32(src + offset + 36);
        offset += SFRO_TARGET_SIZE;
    }
    if (!target_policy_is_valid(&parsed.snapshot.target_policy)) {
        *protocol_status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_TARGET;
        return AVERROR(EINVAL);
    }

    for (i = 0; i < nb_manual_regions; i++) {
        FFFrameDecisionROIManualRegion *region =
            &parsed.snapshot.manual_regions[i];

        memcpy(region->roi_id, src + offset,
               FF_FRAME_DECISION_ROI_TARGET_ID_SIZE);
        region->x           = AV_RB32(src + offset + 32);
        region->y           = AV_RB32(src + offset + 36);
        region->width       = AV_RB32(src + offset + 40);
        region->height      = AV_RB32(src + offset + 44);
        region->qoffset.num = (int32_t)AV_RB32(src + offset + 48);
        region->qoffset.den = AV_RB32(src + offset + 52);
        if (AV_RB32(src + offset + 56) ||
            AV_RB32(src + offset + 60))
            return AVERROR_INVALIDDATA;
        offset += SFRO_MANUAL_SIZE;
    }
    if (!manual_regions_are_valid(parsed.snapshot.manual_regions,
                                  parsed.snapshot.nb_manual_regions)) {
        *protocol_status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_ROI;
        return AVERROR(EINVAL);
    }

    *message = parsed;
    *protocol_status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK;
    return 0;
}

int ff_frame_decision_roi_control_encode_ack(
    uint8_t *dst, size_t dst_size,
    const FFFrameDecisionROIControlAckMessage *message)
{
    if (!dst || !message || dst_size < SFRO_ACK_SIZE ||
        !channel_is_valid(message->channel) ||
        !status_is_valid(message->status) || !message->server_epoch ||
        !message->correlation_id ||
        message->accepted_targets > FF_FRAME_DECISION_ROI_MAX_TARGETS ||
        message->accepted_manual_regions >
            FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL ||
        (message->ack_flags &
         ~FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE) ||
        (message->status &&
         (message->accepted_targets || message->accepted_manual_regions ||
          message->ack_flags)))
        return AVERROR(EINVAL);

    memset(dst, 0, SFRO_ACK_SIZE);
    encode_header(dst, FF_FRAME_DECISION_ROI_CONTROL_ACK, 0,
                  SFRO_ACK_SIZE, message->channel, message->status,
                  message->server_epoch, message->revision,
                  message->correlation_id);
    AV_WB16(dst + 48, message->accepted_targets);
    AV_WB16(dst + 50, message->accepted_manual_regions);
    AV_WB32(dst + 52, message->ack_flags);
    return SFRO_ACK_SIZE;
}

int ff_frame_decision_roi_control_decode_ack(
    FFFrameDecisionROIControlAckMessage *message,
    const uint8_t *src, size_t src_size)
{
    FFFrameDecisionROIControlWireHeader header;
    FFFrameDecisionROIControlAckMessage parsed;
    int ret;

    if (!message)
        return AVERROR(EINVAL);
    ret = decode_message_header(&header, src, src_size,
                                FF_FRAME_DECISION_ROI_CONTROL_ACK,
                                SFRO_ACK_SIZE, 0, 1);
    if (ret < 0 || AV_RB64(src + 56))
        return AVERROR_INVALIDDATA;
    parsed = (FFFrameDecisionROIControlAckMessage) {
        .channel                 = header.channel,
        .status                  = header.status,
        .server_epoch            = header.server_epoch,
        .revision                = header.revision,
        .correlation_id          = header.correlation_id,
        .accepted_targets        = AV_RB16(src + 48),
        .accepted_manual_regions = AV_RB16(src + 50),
        .ack_flags               = AV_RB32(src + 52),
    };
    if (parsed.accepted_targets > FF_FRAME_DECISION_ROI_MAX_TARGETS ||
        parsed.accepted_manual_regions >
            FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL ||
        (parsed.ack_flags &
         ~FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE) ||
        (parsed.status &&
         (parsed.accepted_targets || parsed.accepted_manual_regions ||
          parsed.ack_flags)))
        return AVERROR_INVALIDDATA;
    *message = parsed;
    return 0;
}

static int set_nonblocking_cloexec(int fd)
{
    int flags;

    flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
        return AVERROR(errno);
    flags = fcntl(fd, F_GETFD);
    if (flags < 0 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0)
        return AVERROR(errno);
    return 0;
}

static int client_is_stopped(FFFrameDecisionROIControlClient *client)
{
    int stopped;

    pthread_mutex_lock(&client->lock);
    stopped = client->stop;
    pthread_mutex_unlock(&client->lock);
    return stopped;
}

static int install_active_fd(FFFrameDecisionROIControlClient *client, int fd)
{
    int ret = 0;

    pthread_mutex_lock(&client->lock);
    if (client->stop)
        ret = AVERROR(EINTR);
    else
        client->active_fd = fd;
    pthread_mutex_unlock(&client->lock);
    return ret;
}

static void close_active_fd(FFFrameDecisionROIControlClient *client, int fd)
{
    pthread_mutex_lock(&client->lock);
    if (client->active_fd == fd)
        client->active_fd = -1;
    pthread_mutex_unlock(&client->lock);
    close(fd);
}

static int wait_for_io(FFFrameDecisionROIControlClient *client, int fd,
                       short events, int timeout_ms)
{
    struct pollfd poll_fds[2] = {
        { .fd = client->wake_fds[0], .events = POLLIN },
        { .fd = fd, .events = events },
    };

    for (;;) {
        int ret = poll(poll_fds, 2, timeout_ms);

        if (ret < 0) {
            if (errno == EINTR)
                continue;
            return AVERROR(errno);
        }
        if (!ret)
            return AVERROR(ETIMEDOUT);
        if (poll_fds[0].revents || client_is_stopped(client))
            return AVERROR(EINTR);
        if (poll_fds[1].revents & events)
            return 0;
        if (poll_fds[1].revents & (POLLERR | POLLHUP | POLLNVAL))
            return AVERROR(EPIPE);
    }
}

static int wait_for_reconnect(FFFrameDecisionROIControlClient *client,
                              unsigned delay_ms)
{
    struct pollfd poll_fd = {
        .fd = client->wake_fds[0],
        .events = POLLIN,
    };

    for (;;) {
        int ret = poll(&poll_fd, 1, delay_ms);

        if (ret < 0) {
            if (errno == EINTR)
                continue;
            return AVERROR(errno);
        }
        if (ret || client_is_stopped(client))
            return AVERROR(EINTR);
        return 0;
    }
}

static int connect_socket(FFFrameDecisionROIControlClient *client, int *fd_out)
{
    struct sockaddr_un address = { 0 };
    socklen_t address_size;
    size_t path_length = strlen(client->socket_path);
    int fd;
    int error;
    socklen_t error_size = sizeof(error);
    int ret;

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return AVERROR(errno);
#ifdef SO_NOSIGPIPE
    error = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE,
                   &error, sizeof(error)) < 0) {
        ret = AVERROR(errno);
        close(fd);
        return ret;
    }
#endif
    ret = set_nonblocking_cloexec(fd);
    if (ret < 0 || (ret = install_active_fd(client, fd)) < 0) {
        close(fd);
        return ret;
    }

    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, client->socket_path, path_length + 1);
    address_size = offsetof(struct sockaddr_un, sun_path) + path_length + 1;
    if (connect(fd, (const struct sockaddr *)&address, address_size) < 0) {
        if (errno != EINPROGRESS) {
            ret = AVERROR(errno);
            close_active_fd(client, fd);
            return ret;
        }
        ret = wait_for_io(client, fd, POLLOUT, -1);
        if (ret < 0) {
            close_active_fd(client, fd);
            return ret;
        }
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR,
                       &error, &error_size) < 0) {
            ret = AVERROR(errno);
            close_active_fd(client, fd);
            return ret;
        }
        if (error) {
            ret = AVERROR(error);
            close_active_fd(client, fd);
            return ret;
        }
    }
    *fd_out = fd;
    return 0;
}

static int send_all(FFFrameDecisionROIControlClient *client, int fd,
                    const uint8_t *data, size_t size)
{
    size_t sent = 0;

    while (sent < size) {
        ssize_t ret;

#ifdef MSG_NOSIGNAL
        ret = send(fd, data + sent, size - sent, MSG_NOSIGNAL);
#else
        ret = send(fd, data + sent, size - sent, 0);
#endif
        if (ret > 0) {
            sent += ret;
            continue;
        }
        if (!ret)
            return AVERROR(EPIPE);
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            int wait_ret = wait_for_io(client, fd, POLLOUT, -1);

            if (wait_ret < 0)
                return wait_ret;
            continue;
        }
        return AVERROR(errno);
    }
    return 0;
}

static int receive_all(FFFrameDecisionROIControlClient *client, int fd,
                       uint8_t *data, size_t size)
{
    size_t received = 0;

    while (received < size) {
        ssize_t ret = recv(fd, data + received, size - received, 0);

        if (ret > 0) {
            received += ret;
            continue;
        }
        if (!ret)
            return AVERROR(EPIPE);
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            int wait_ret = wait_for_io(client, fd, POLLIN, -1);

            if (wait_ret < 0)
                return wait_ret;
            continue;
        }
        return AVERROR(errno);
    }
    return 0;
}

static int receive_message(FFFrameDecisionROIControlClient *client, int fd,
                           uint8_t *message, size_t message_capacity,
                           FFFrameDecisionROIControlWireHeader *header)
{
    int ret;

    ret = receive_all(client, fd, message,
                      FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    if (ret < 0)
        return ret;
    ret = ff_frame_decision_roi_control_decode_header(
        header, message, FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    if (ret < 0 || header->message_size > message_capacity)
        return AVERROR_INVALIDDATA;
    ret = receive_all(client, fd,
                      message + FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE,
                      header->message_size -
                          FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    if (ret < 0)
        return ret;
    return header->message_size;
}

static uint64_t next_correlation_id(
    FFFrameDecisionROIControlClient *client)
{
    client->next_correlation_id++;
    if (!client->next_correlation_id)
        client->next_correlation_id++;
    return client->next_correlation_id;
}

static int send_ack(FFFrameDecisionROIControlClient *client, int fd,
                    const FFFrameDecisionROIControlWireHeader *header,
                    int status, uint16_t accepted_targets,
                    uint16_t accepted_manual_regions, uint32_t ack_flags)
{
    FFFrameDecisionROIControlAckMessage ack = {
        .channel                 = header->channel,
        .status                  = status,
        .server_epoch            = header->server_epoch,
        .revision                = header->revision,
        .correlation_id          = header->correlation_id,
        .accepted_targets        = accepted_targets,
        .accepted_manual_regions = accepted_manual_regions,
        .ack_flags               = ack_flags,
    };
    uint8_t message[SFRO_ACK_SIZE];
    int ret = ff_frame_decision_roi_control_encode_ack(
        message, sizeof(message), &ack);

    if (ret < 0)
        return ret;
    return send_all(client, fd, message, ret);
}

static void publish_snapshot(FFFrameDecisionROIControlClient *client,
                             const FFFrameDecisionROIControlSnapshot *snapshot)
{
    pthread_mutex_lock(&client->lock);
    client->latest = *snapshot;
    client->has_snapshot = 1;
    client->publish_serial++;
    if (!client->publish_serial)
        client->publish_serial++;
    pthread_mutex_unlock(&client->lock);
}

static int classify_revision(FFFrameDecisionROIControlClient *client,
                             uint64_t server_epoch, uint64_t revision)
{
    int result = 1;

    pthread_mutex_lock(&client->lock);
    if (client->has_snapshot &&
        client->latest.server_epoch == server_epoch) {
        if (revision == client->latest.revision)
            result = 0;
        else if (revision < client->latest.revision)
            result = -1;
    }
    pthread_mutex_unlock(&client->lock);
    return result;
}

static int run_connection(FFFrameDecisionROIControlClient *client, int fd,
                          int *handshake_complete)
{
    FFFrameDecisionROIControlHelloMessage hello = {
        .channel             = client->channel,
        .correlation_id      = next_correlation_id(client),
        .client_instance_id  = client->client_instance_id,
        .process_id          = getpid(),
        .capabilities        = FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES,
        .max_message_size    = FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE,
        .max_targets         = FF_FRAME_DECISION_ROI_MAX_TARGETS,
        .max_manual_regions  = FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL,
    };
    FFFrameDecisionROIControlServerHelloMessage server_hello;
    FFFrameDecisionROIControlWireHeader header;
    uint8_t message[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    int ret;

    *handshake_complete = 0;
    ret = ff_frame_decision_roi_control_encode_hello(
        message, sizeof(message), &hello);
    if (ret < 0)
        return ret;
    ret = send_all(client, fd, message, ret);
    if (ret < 0)
        return ret;
    ret = receive_message(client, fd, message, sizeof(message), &header);
    if (ret < 0)
        return ret;
    ret = ff_frame_decision_roi_control_decode_server_hello(
        &server_hello, message, ret);
    if (ret < 0 || server_hello.channel != client->channel ||
        server_hello.correlation_id != hello.correlation_id)
        return AVERROR_INVALIDDATA;
    *handshake_complete = 1;

    for (;;) {
        FFFrameDecisionROIControlSnapshotMessage snapshot_message;
        int protocol_status;
        int revision_state;
        int message_size;

        message_size = receive_message(client, fd, message,
                                       sizeof(message), &header);
        if (message_size < 0)
            return message_size;
        if (header.type != FF_FRAME_DECISION_ROI_CONTROL_SNAPSHOT)
            return AVERROR_INVALIDDATA;
        if (header.version != FF_FRAME_DECISION_ROI_CONTROL_VERSION) {
            send_ack(client, fd, &header,
                     FF_FRAME_DECISION_ROI_CONTROL_STATUS_UNSUPPORTED_VERSION,
                     0, 0, 0);
            return AVERROR_INVALIDDATA;
        }
        if (header.channel != client->channel) {
            send_ack(client, fd, &header,
                     FF_FRAME_DECISION_ROI_CONTROL_STATUS_WRONG_CHANNEL,
                     0, 0, 0);
            return AVERROR_INVALIDDATA;
        }
        if (header.server_epoch != server_hello.server_epoch)
            return AVERROR_INVALIDDATA;

        ret = ff_frame_decision_roi_control_decode_snapshot(
            &snapshot_message, &protocol_status, message, message_size);
        if (ret < 0) {
            if (protocol_status !=
                FF_FRAME_DECISION_ROI_CONTROL_STATUS_MALFORMED)
                send_ack(client, fd, &header, protocol_status, 0, 0, 0);
            return ret;
        }
        if (snapshot_message.snapshot.target_policy.nb_targets >
                server_hello.max_targets ||
            snapshot_message.snapshot.nb_manual_regions >
                server_hello.max_manual_regions ||
            (uint32_t)message_size > server_hello.max_message_size) {
            send_ack(client, fd, &header,
                     FF_FRAME_DECISION_ROI_CONTROL_STATUS_LIMIT_EXCEEDED,
                     0, 0, 0);
            return AVERROR(E2BIG);
        }

        revision_state = classify_revision(
            client, snapshot_message.snapshot.server_epoch,
            snapshot_message.snapshot.revision);
        if (revision_state < 0) {
            send_ack(client, fd, &header,
                     FF_FRAME_DECISION_ROI_CONTROL_STATUS_STALE_REVISION,
                     0, 0, 0);
            return AVERROR_INVALIDDATA;
        }
        if (!revision_state) {
            ret = send_ack(
                client, fd, &header,
                FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
                snapshot_message.snapshot.target_policy.nb_targets,
                snapshot_message.snapshot.nb_manual_regions,
                FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE);
            if (ret < 0)
                return ret;
            continue;
        }

        publish_snapshot(client, &snapshot_message.snapshot);
        ret = send_ack(client, fd, &header,
                       FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
                       snapshot_message.snapshot.target_policy.nb_targets,
                       snapshot_message.snapshot.nb_manual_regions, 0);
        if (ret < 0)
            return ret;
    }
}

static void *control_worker(void *opaque)
{
    FFFrameDecisionROIControlClient *client = opaque;
    unsigned delay_ms = client->reconnect_delay_ms;

    for (;;) {
        int handshake_complete = 0;
        int fd;
        int ret;

        if (client_is_stopped(client))
            break;
        ret = connect_socket(client, &fd);
        if (ret >= 0) {
            ret = run_connection(client, fd, &handshake_complete);
            close_active_fd(client, fd);
        }
        if (client_is_stopped(client))
            break;
        av_log(client->log_ctx, AV_LOG_DEBUG,
               "frame-decision ROI control disconnected: %s\n",
               av_err2str(ret));
        if (handshake_complete)
            delay_ms = client->reconnect_delay_ms;
        if (wait_for_reconnect(client, delay_ms) < 0)
            break;
        if (!handshake_complete && delay_ms < SFRO_RECONNECT_MAX_MS) {
            delay_ms *= 2;
            if (delay_ms > SFRO_RECONNECT_MAX_MS)
                delay_ms = SFRO_RECONNECT_MAX_MS;
        }
    }
    return NULL;
}

static uint64_t make_instance_id(const void *address)
{
    struct timespec realtime = { 0 };
    struct timespec monotonic = { 0 };
    uint64_t value;

    clock_gettime(CLOCK_REALTIME, &realtime);
    clock_gettime(CLOCK_MONOTONIC, &monotonic);
    value = ((uint64_t)realtime.tv_sec << 32) ^ realtime.tv_nsec ^
            ((uint64_t)monotonic.tv_sec << 17) ^ monotonic.tv_nsec ^
            ((uint64_t)(uint32_t)getpid() << 7) ^ (uintptr_t)address;
    return value ? value : 1;
}

int ff_frame_decision_roi_control_create(
    FFFrameDecisionROIControlClient **client_out,
    const FFFrameDecisionROIControlConfig *config)
{
    FFFrameDecisionROIControlClient *client;
    size_t path_length;
    int ret;

    if (!client_out)
        return AVERROR(EINVAL);
    *client_out = NULL;
    if (!config || !config->socket_path ||
        !config->socket_path[0] || !channel_is_valid(config->channel))
        return AVERROR(EINVAL);
    path_length = strlen(config->socket_path);
    if (path_length >= sizeof(((struct sockaddr_un *)0)->sun_path))
        return AVERROR(ENAMETOOLONG);

    client = calloc(1, sizeof(*client));
    if (!client)
        return AVERROR(ENOMEM);
    client->socket_path = strdup(config->socket_path);
    if (!client->socket_path) {
        free(client);
        return AVERROR(ENOMEM);
    }
    client->log_ctx = config->log_ctx;
    client->channel = config->channel;
    client->client_instance_id = config->client_instance_id;
    client->reconnect_delay_ms = config->reconnect_delay_ms ?
                                 config->reconnect_delay_ms :
                                 SFRO_RECONNECT_DEFAULT_MS;
    if (client->reconnect_delay_ms > SFRO_RECONNECT_MAX_MS)
        client->reconnect_delay_ms = SFRO_RECONNECT_MAX_MS;
    client->active_fd = -1;
    client->wake_fds[0] = client->wake_fds[1] = -1;

    ret = pthread_mutex_init(&client->lock, NULL);
    if (ret) {
        free(client->socket_path);
        free(client);
        return AVERROR(ret);
    }
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, client->wake_fds) < 0) {
        ret = AVERROR(errno);
        pthread_mutex_destroy(&client->lock);
        free(client->socket_path);
        free(client);
        return ret;
    }
    ret = set_nonblocking_cloexec(client->wake_fds[0]);
    if (ret >= 0)
        ret = set_nonblocking_cloexec(client->wake_fds[1]);
    if (ret < 0) {
        close(client->wake_fds[0]);
        close(client->wake_fds[1]);
        pthread_mutex_destroy(&client->lock);
        free(client->socket_path);
        free(client);
        return ret;
    }
    if (!client->client_instance_id)
        client->client_instance_id = make_instance_id(client);
    client->next_correlation_id = client->client_instance_id;
    *client_out = client;
    return 0;
}

int ff_frame_decision_roi_control_start(
    FFFrameDecisionROIControlClient *client)
{
    int ret;

    if (!client)
        return AVERROR(EINVAL);
    pthread_mutex_lock(&client->lock);
    if (client->started || client->stop) {
        pthread_mutex_unlock(&client->lock);
        return AVERROR(EALREADY);
    }
    ret = pthread_create(&client->worker, NULL, control_worker, client);
    if (!ret)
        client->started = 1;
    pthread_mutex_unlock(&client->lock);
    return ret ? AVERROR(ret) : 0;
}

int ff_frame_decision_roi_control_get_latest(
    FFFrameDecisionROIControlClient *client,
    FFFrameDecisionROIControlSnapshot *snapshot)
{
    int available = 0;

    if (!client || !snapshot)
        return AVERROR(EINVAL);
    pthread_mutex_lock(&client->lock);
    if (client->has_snapshot &&
        client->read_serial != client->publish_serial) {
        *snapshot = client->latest;
        client->read_serial = client->publish_serial;
        available = 1;
    }
    pthread_mutex_unlock(&client->lock);
    return available;
}

void ff_frame_decision_roi_control_destroy(
    FFFrameDecisionROIControlClient **client_ptr)
{
    FFFrameDecisionROIControlClient *client;
    int active_fd;
    int started;
    uint8_t wake = 1;

    if (!client_ptr || !*client_ptr)
        return;
    client = *client_ptr;
    *client_ptr = NULL;

    pthread_mutex_lock(&client->lock);
    client->stop = 1;
    active_fd = client->active_fd;
    started = client->started;
    if (active_fd >= 0)
        shutdown(active_fd, SHUT_RDWR);
    pthread_mutex_unlock(&client->lock);
#ifdef MSG_NOSIGNAL
    send(client->wake_fds[1], &wake, sizeof(wake), MSG_NOSIGNAL);
#else
    send(client->wake_fds[1], &wake, sizeof(wake), 0);
#endif
    if (started)
        pthread_join(client->worker, NULL);

    close(client->wake_fds[0]);
    close(client->wake_fds[1]);
    pthread_mutex_destroy(&client->lock);
    free(client->socket_path);
    free(client);
}
