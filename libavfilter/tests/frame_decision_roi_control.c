/*
 * Frame-decision ROI runtime control tests
 *
 * This file is part of FFmpeg.
 *
 * FFmpeg is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "libavutil/error.h"
#include "libavutil/intreadwrite.h"

#include "libavfilter/frame_decision_roi_control.h"

#define TEST_CHANNEL 5
#define TEST_EPOCH   UINT64_C(0x1020304050607080)

/* Shared with Stream Forge's SfRoiProtocolTest. */
static const char golden_hello_hex[] =
    "5346524f00010030000100000000005000000005000000000000000000000000"
    "0000000000000000010203040506070811121314151617180102030400000003"
    "00001000001000080000000000000000";

static const char golden_snapshot_hex[] =
    "5346524f0001003000030001000000d000000005000000001020304050607080"
    "00000000000000090000000000abc00900020001000f42400000000000000000"
    "666163655f686561640000000000000000000000000000000000000000000000"
    "ffffffff000000046d6f746f725f76656869636c650000000000000000000000"
    "0000000000000000000000010000000867617465000000000000000000000000"
    "00000000000000000000000000000000000186a000030d400003d090000493e0"
    "ffffffff000000030000000000000000";

static const char golden_ack_hex[] =
    "5346524f00010030000400000000004000000005000000001020304050607080"
    "00000000000000090000000000abc00900020001000000010000000000000000";

#define CHECK(condition, message) do {                                  \
    if (!(condition)) {                                                  \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, message);   \
        return 1;                                                        \
    }                                                                   \
} while (0)

typedef struct TestServer {
    char socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    pthread_mutex_t lock;
    pthread_cond_t cond;
    int phase;
    int gate;
    int error;
} TestServer;

static int hex_nibble(char value)
{
    if (value >= '0' && value <= '9')
        return value - '0';
    if (value >= 'a' && value <= 'f')
        return value - 'a' + 10;
    return -1;
}

static int decode_hex(uint8_t *dst, size_t dst_size, const char *hex)
{
    size_t i;

    if (strlen(hex) != dst_size * 2)
        return AVERROR_INVALIDDATA;
    for (i = 0; i < dst_size; i++) {
        const int high = hex_nibble(hex[2 * i]);
        const int low  = hex_nibble(hex[2 * i + 1]);

        if (high < 0 || low < 0)
            return AVERROR_INVALIDDATA;
        dst[i] = high << 4 | low;
    }
    return 0;
}

static int64_t monotonic_ms(void)
{
    struct timespec time;

    clock_gettime(CLOCK_MONOTONIC, &time);
    return (int64_t)time.tv_sec * 1000 + time.tv_nsec / 1000000;
}

static void sleep_ms(unsigned milliseconds)
{
    struct timespec delay = {
        .tv_sec  = milliseconds / 1000,
        .tv_nsec = (milliseconds % 1000) * 1000000,
    };

    while (nanosleep(&delay, &delay) < 0 && errno == EINTR)
        ;
}

static int make_temp_directory(char *path)
{
    int fd = mkstemp(path);

    if (fd < 0)
        return AVERROR(errno);
    if (close(fd) < 0) {
        int error = AVERROR(errno);

        unlink(path);
        return error;
    }
    if (unlink(path) < 0 || mkdir(path, 0700) < 0)
        return AVERROR(errno);
    return 0;
}

static int test_write_all(int fd, const uint8_t *data, size_t size)
{
    size_t written = 0;

    while (written < size) {
        ssize_t ret = write(fd, data + written, size - written);

        if (ret > 0) {
            written += ret;
            continue;
        }
        if (ret < 0 && errno == EINTR)
            continue;
        return AVERROR(ret < 0 ? errno : EPIPE);
    }
    return 0;
}

static int test_read_all(int fd, uint8_t *data, size_t size)
{
    size_t received = 0;

    while (received < size) {
        ssize_t ret = read(fd, data + received, size - received);

        if (ret > 0) {
            received += ret;
            continue;
        }
        if (ret < 0 && errno == EINTR)
            continue;
        return AVERROR(ret < 0 ? errno : EPIPE);
    }
    return 0;
}

static int test_read_message(int fd, uint8_t *message, size_t capacity)
{
    FFFrameDecisionROIControlWireHeader header;
    int ret;

    ret = test_read_all(fd, message,
                        FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    if (ret < 0)
        return ret;
    ret = ff_frame_decision_roi_control_decode_header(
        &header, message, FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    if (ret < 0 || header.message_size > capacity)
        return AVERROR_INVALIDDATA;
    ret = test_read_all(fd,
                        message + FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE,
                        header.message_size -
                            FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE);
    return ret < 0 ? ret : (int)header.message_size;
}

static FFFrameDecisionROIControlSnapshotMessage test_snapshot(
    uint64_t revision, int variant)
{
    FFFrameDecisionROIControlSnapshotMessage message = {
        .snapshot = {
            .target_policy = {
                .targets = {
                    { "face_head", { -1, 4 } },
                    { "motor_vehicle", { 1, 8 } },
                },
                .nb_targets = 2,
            },
            .manual_regions = {
                {
                    .roi_id  = "gate",
                    .x       = variant ? 300000 : 100000,
                    .y       = 200000,
                    .width   = 250000,
                    .height  = 300000,
                    .qoffset = { -1, 3 },
                },
            },
            .nb_manual_regions = 1,
            .server_epoch      = TEST_EPOCH,
            .revision          = revision,
        },
        .channel        = TEST_CHANNEL,
        .correlation_id = UINT64_C(0xabc000) + revision,
    };

    return message;
}

static int test_hello_wire(void)
{
    const FFFrameDecisionROIControlHelloMessage expected = {
        .channel             = TEST_CHANNEL,
        .correlation_id      = UINT64_C(0x0102030405060708),
        .client_instance_id  = UINT64_C(0x1112131415161718),
        .process_id          = UINT32_C(0x01020304),
        .capabilities        = FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES,
        .max_message_size    = FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE,
        .max_targets         = FF_FRAME_DECISION_ROI_MAX_TARGETS,
        .max_manual_regions  = FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL,
    };
    FFFrameDecisionROIControlHelloMessage actual;
    uint8_t wire[80];
    uint8_t golden[80];

    CHECK(decode_hex(golden, sizeof(golden), golden_hello_hex) == 0,
          "invalid HELLO golden vector");
    CHECK(ff_frame_decision_roi_control_encode_hello(
              wire, sizeof(wire), &expected) == sizeof(wire),
          "HELLO encode failed");
    CHECK(!memcmp(wire, golden, sizeof(wire)),
          "HELLO differs from the SFRO v1 golden vector");
    CHECK(AV_RB32(wire + 0) == FF_FRAME_DECISION_ROI_CONTROL_MAGIC &&
          AV_RB16(wire + 4) == FF_FRAME_DECISION_ROI_CONTROL_VERSION &&
          AV_RB16(wire + 6) ==
              FF_FRAME_DECISION_ROI_CONTROL_HEADER_SIZE &&
          AV_RB16(wire + 8) == FF_FRAME_DECISION_ROI_CONTROL_HELLO &&
          AV_RB32(wire + 12) == sizeof(wire) &&
          AV_RB32(wire + 16) == TEST_CHANNEL,
          "HELLO header byte order or offsets differ");
    CHECK(AV_RB64(wire + 48) == expected.client_instance_id &&
          AV_RB32(wire + 56) == expected.process_id &&
          AV_RB32(wire + 60) == expected.capabilities &&
          AV_RB32(wire + 64) == expected.max_message_size &&
          AV_RB16(wire + 68) == expected.max_targets &&
          AV_RB16(wire + 70) == expected.max_manual_regions &&
          !AV_RB64(wire + 72),
          "HELLO payload byte order or offsets differ");
    CHECK(ff_frame_decision_roi_control_decode_hello(
              &actual, golden, sizeof(golden)) == 0,
          "HELLO decode failed");
    CHECK(actual.channel == expected.channel &&
          actual.correlation_id == expected.correlation_id &&
          actual.client_instance_id == expected.client_instance_id &&
          actual.process_id == expected.process_id &&
          actual.capabilities == expected.capabilities &&
          actual.max_message_size == expected.max_message_size &&
          actual.max_targets == expected.max_targets &&
          actual.max_manual_regions == expected.max_manual_regions,
          "HELLO round trip differs");

    AV_WB32(wire + 60, 0);
    CHECK(ff_frame_decision_roi_control_decode_hello(
              &actual, wire, sizeof(wire)) < 0,
          "HELLO without required capabilities was accepted");
    AV_WB32(wire + 60, expected.capabilities);
    AV_WB64(wire + 72, 1);
    CHECK(ff_frame_decision_roi_control_decode_hello(
              &actual, wire, sizeof(wire)) < 0,
          "HELLO reserved field was accepted");
    return 0;
}

static int test_server_hello_wire(void)
{
    const FFFrameDecisionROIControlServerHelloMessage expected = {
        .channel             = TEST_CHANNEL,
        .server_epoch        = TEST_EPOCH,
        .correlation_id      = UINT64_C(0x100),
        .capabilities        = FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES,
        .max_message_size    = FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE,
        .max_targets         = FF_FRAME_DECISION_ROI_MAX_TARGETS,
        .max_manual_regions  = FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL,
    };
    FFFrameDecisionROIControlServerHelloMessage actual;
    uint8_t wire[64];

    CHECK(ff_frame_decision_roi_control_encode_server_hello(
              wire, sizeof(wire), &expected) == sizeof(wire),
          "SERVER_HELLO encode failed");
    CHECK(ff_frame_decision_roi_control_decode_server_hello(
              &actual, wire, sizeof(wire)) == 0,
          "SERVER_HELLO decode failed");
    CHECK(actual.channel == expected.channel &&
          actual.server_epoch == expected.server_epoch &&
          actual.correlation_id == expected.correlation_id &&
          actual.capabilities == expected.capabilities &&
          actual.max_message_size == expected.max_message_size &&
          actual.max_targets == expected.max_targets &&
          actual.max_manual_regions == expected.max_manual_regions,
          "SERVER_HELLO round trip differs");
    AV_WB32(wire + 60, 1);
    CHECK(ff_frame_decision_roi_control_decode_server_hello(
              &actual, wire, sizeof(wire)) < 0,
          "SERVER_HELLO reserved field was accepted");
    return 0;
}

static int test_snapshot_wire(void)
{
    const FFFrameDecisionROIControlSnapshotMessage expected =
        test_snapshot(9, 0);
    FFFrameDecisionROIControlSnapshotMessage actual;
    uint8_t wire[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    uint8_t golden[208];
    uint8_t invalid[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    const size_t manual_offset = 64 + 2 * 40;
    int protocol_status;
    int size;

    CHECK(decode_hex(golden, sizeof(golden), golden_snapshot_hex) == 0,
          "invalid SNAPSHOT golden vector");
    size = ff_frame_decision_roi_control_encode_snapshot(
        wire, sizeof(wire), &expected);
    CHECK(size == sizeof(golden),
          "SNAPSHOT encoded size differs");
    CHECK(!memcmp(wire, golden, sizeof(golden)),
          "SNAPSHOT differs from the SFRO v1 golden vector");
    CHECK(AV_RB16(wire + 10) ==
              FF_FRAME_DECISION_ROI_CONTROL_FLAG_FULL_REPLACE &&
          AV_RB16(wire + 48) == 2 && AV_RB16(wire + 50) == 1 &&
          AV_RB32(wire + 52) ==
              FF_FRAME_DECISION_ROI_CONTROL_COORDINATE_SCALE,
          "SNAPSHOT prefix differs");
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, golden, sizeof(golden)) == 0 &&
          protocol_status == FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
          "SNAPSHOT decode failed");
    CHECK(actual.channel == expected.channel &&
          actual.correlation_id == expected.correlation_id &&
          actual.snapshot.server_epoch == expected.snapshot.server_epoch &&
          actual.snapshot.revision == expected.snapshot.revision &&
          actual.snapshot.target_policy.nb_targets == 2 &&
          actual.snapshot.nb_manual_regions == 1 &&
          !strcmp(actual.snapshot.target_policy.targets[0].target_id,
                  "face_head") &&
          actual.snapshot.target_policy.targets[0].qoffset.num == -1 &&
          actual.snapshot.target_policy.targets[0].qoffset.den == 4 &&
          !strcmp(actual.snapshot.target_policy.targets[1].target_id,
                  "motor_vehicle") &&
          actual.snapshot.target_policy.targets[1].qoffset.num == 1 &&
          actual.snapshot.target_policy.targets[1].qoffset.den == 8 &&
          !strcmp(actual.snapshot.manual_regions[0].roi_id, "gate") &&
          actual.snapshot.manual_regions[0].x == 100000 &&
          actual.snapshot.manual_regions[0].y == 200000 &&
          actual.snapshot.manual_regions[0].width == 250000 &&
          actual.snapshot.manual_regions[0].height == 300000 &&
          actual.snapshot.manual_regions[0].qoffset.num == -1 &&
          actual.snapshot.manual_regions[0].qoffset.den == 3,
          "SNAPSHOT round trip differs");

    memcpy(invalid, wire, size);
    invalid[64 + strlen("face_head") + 1] = 1;
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_TARGET,
          "non-zero target ID tail was accepted");

    memcpy(invalid, wire, size);
    AV_WB32(invalid + 64 + 32, 0);
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_TARGET,
          "zero target qoffset was accepted instead of being omitted");

    memcpy(invalid, wire, size);
    AV_WB32(invalid + manual_offset + 32, 900000);
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_ROI,
          "out-of-frame manual ROI was accepted");

    memcpy(invalid, wire, size);
    AV_WB32(invalid + manual_offset + 56, 1);
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_MALFORMED,
          "non-zero manual reserved field was accepted");

    memcpy(invalid, wire, size);
    AV_WB16(invalid + 10,
            FF_FRAME_DECISION_ROI_CONTROL_FLAG_FULL_REPLACE | 2);
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_MALFORMED,
          "unknown SNAPSHOT flag was accepted");

    memcpy(invalid, wire, size);
    AV_WB16(invalid + 48, FF_FRAME_DECISION_ROI_MAX_TARGETS + 1);
    CHECK(ff_frame_decision_roi_control_decode_snapshot(
              &actual, &protocol_status, invalid, size) < 0 &&
          protocol_status ==
              FF_FRAME_DECISION_ROI_CONTROL_STATUS_LIMIT_EXCEEDED,
          "excessive target count did not produce LIMIT_EXCEEDED");
    return 0;
}

static int test_ack_wire(void)
{
    const FFFrameDecisionROIControlAckMessage expected = {
        .channel                 = TEST_CHANNEL,
        .status                  = FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
        .server_epoch            = TEST_EPOCH,
        .revision                = 9,
        .correlation_id          = UINT64_C(0xabc009),
        .accepted_targets        = 2,
        .accepted_manual_regions = 1,
        .ack_flags               =
            FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE,
    };
    FFFrameDecisionROIControlAckMessage actual;
    FFFrameDecisionROIControlAckMessage invalid = expected;
    uint8_t wire[64];
    uint8_t golden[64];

    CHECK(decode_hex(golden, sizeof(golden), golden_ack_hex) == 0,
          "invalid ACK golden vector");
    CHECK(ff_frame_decision_roi_control_encode_ack(
              wire, sizeof(wire), &expected) == sizeof(wire),
          "ACK encode failed");
    CHECK(!memcmp(wire, golden, sizeof(wire)),
          "ACK differs from the SFRO v1 golden vector");
    CHECK(ff_frame_decision_roi_control_decode_ack(
              &actual, golden, sizeof(golden)) == 0,
          "ACK decode failed");
    CHECK(actual.channel == expected.channel &&
          actual.status == expected.status &&
          actual.server_epoch == expected.server_epoch &&
          actual.revision == expected.revision &&
          actual.correlation_id == expected.correlation_id &&
          actual.accepted_targets == expected.accepted_targets &&
          actual.accepted_manual_regions ==
              expected.accepted_manual_regions &&
          actual.ack_flags == expected.ack_flags,
          "ACK round trip differs");
    invalid.status = FF_FRAME_DECISION_ROI_CONTROL_STATUS_INVALID_ROI;
    CHECK(ff_frame_decision_roi_control_encode_ack(
              wire, sizeof(wire), &invalid) < 0,
          "NACK with accepted counts was encoded");
    return 0;
}

static int make_listener(const char *path)
{
    struct sockaddr_un address = { 0 };
    size_t path_length = strlen(path);
    socklen_t address_size;
    int fd;

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return AVERROR(errno);
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, path, path_length + 1);
    address_size = offsetof(struct sockaddr_un, sun_path) + path_length + 1;
    unlink(path);
    if (bind(fd, (const struct sockaddr *)&address, address_size) < 0 ||
        listen(fd, 4) < 0) {
        int ret = AVERROR(errno);

        close(fd);
        return ret;
    }
    return fd;
}

static void server_set_phase(TestServer *server, int phase)
{
    pthread_mutex_lock(&server->lock);
    server->phase = phase;
    pthread_cond_broadcast(&server->cond);
    pthread_mutex_unlock(&server->lock);
}

static void server_set_error(TestServer *server, int error)
{
    char error_text[AV_ERROR_MAX_STRING_SIZE];

    pthread_mutex_lock(&server->lock);
    if (!server->error)
        server->error = error;
    pthread_cond_broadcast(&server->cond);
    pthread_mutex_unlock(&server->lock);
    fprintf(stderr, "test server error: %s (%d)\n",
            av_make_error_string(error_text, sizeof(error_text), error),
            error);
}

static int server_wait_gate(TestServer *server, int gate)
{
    int error;

    pthread_mutex_lock(&server->lock);
    while (server->gate < gate && !server->error)
        pthread_cond_wait(&server->cond, &server->lock);
    error = server->error;
    pthread_mutex_unlock(&server->lock);
    return error ? AVERROR_EXTERNAL : 0;
}

static int wait_for_server_phase(TestServer *server, int phase,
                                 unsigned timeout_ms)
{
    const int64_t deadline = monotonic_ms() + timeout_ms;

    for (;;) {
        int current;
        int error;

        pthread_mutex_lock(&server->lock);
        current = server->phase;
        error = server->error;
        pthread_mutex_unlock(&server->lock);
        if (error)
            return AVERROR_EXTERNAL;
        if (current >= phase)
            return 0;
        if (monotonic_ms() >= deadline)
            return AVERROR(ETIMEDOUT);
        sleep_ms(10);
    }
}

static void release_server_gate(TestServer *server, int gate)
{
    pthread_mutex_lock(&server->lock);
    if (server->gate < gate)
        server->gate = gate;
    pthread_cond_broadcast(&server->cond);
    pthread_mutex_unlock(&server->lock);
}

static int server_handshake(int fd, uint64_t epoch)
{
    FFFrameDecisionROIControlHelloMessage hello;
    FFFrameDecisionROIControlServerHelloMessage response;
    uint8_t wire[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    int size;

    size = test_read_message(fd, wire, sizeof(wire));
    if (size < 0 || ff_frame_decision_roi_control_decode_hello(
                         &hello, wire, size) < 0 ||
        hello.channel != TEST_CHANNEL)
        return AVERROR_INVALIDDATA;
    response = (FFFrameDecisionROIControlServerHelloMessage) {
        .channel             = hello.channel,
        .server_epoch        = epoch,
        .correlation_id      = hello.correlation_id,
        .capabilities        = FF_FRAME_DECISION_ROI_CONTROL_CAPABILITIES,
        .max_message_size    = FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE,
        .max_targets         = FF_FRAME_DECISION_ROI_MAX_TARGETS,
        .max_manual_regions  = FF_FRAME_DECISION_ROI_CONTROL_MAX_MANUAL,
    };
    size = ff_frame_decision_roi_control_encode_server_hello(
        wire, sizeof(wire), &response);
    if (size < 0)
        return size;
    /* Deliberately split the stream to exercise exact-length reads. */
    if (test_write_all(fd, wire, 7) < 0 ||
        test_write_all(fd, wire + 7, size - 7) < 0)
        return AVERROR(EIO);
    return 0;
}

static int server_send_snapshot(
    int fd, const FFFrameDecisionROIControlSnapshotMessage *snapshot,
    int expected_status, uint32_t expected_ack_flags)
{
    FFFrameDecisionROIControlAckMessage ack;
    uint8_t wire[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    int size;

    size = ff_frame_decision_roi_control_encode_snapshot(
        wire, sizeof(wire), snapshot);
    if (size < 0 || test_write_all(fd, wire, size) < 0)
        return AVERROR(EIO);
    size = test_read_message(fd, wire, sizeof(wire));
    if (size < 0 || ff_frame_decision_roi_control_decode_ack(
                         &ack, wire, size) < 0)
        return AVERROR_INVALIDDATA;
    if (ack.channel != snapshot->channel ||
        ack.server_epoch != snapshot->snapshot.server_epoch ||
        ack.revision != snapshot->snapshot.revision ||
        ack.correlation_id != snapshot->correlation_id ||
        ack.status != expected_status || ack.ack_flags != expected_ack_flags)
        return AVERROR_INVALIDDATA;
    if (expected_status == FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK) {
        if (ack.accepted_targets !=
                snapshot->snapshot.target_policy.nb_targets ||
            ack.accepted_manual_regions !=
                snapshot->snapshot.nb_manual_regions)
            return AVERROR_INVALIDDATA;
    } else if (ack.accepted_targets || ack.accepted_manual_regions) {
        return AVERROR_INVALIDDATA;
    }
    return 0;
}

static int server_read_expected_ack(
    int fd, const FFFrameDecisionROIControlSnapshotMessage *snapshot,
    int expected_status, uint32_t expected_ack_flags)
{
    FFFrameDecisionROIControlAckMessage ack;
    uint8_t wire[FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    int size = test_read_message(fd, wire, sizeof(wire));

    if (size < 0 || ff_frame_decision_roi_control_decode_ack(
                         &ack, wire, size) < 0)
        return AVERROR_INVALIDDATA;
    if (ack.channel != snapshot->channel ||
        ack.server_epoch != snapshot->snapshot.server_epoch ||
        ack.revision != snapshot->snapshot.revision ||
        ack.correlation_id != snapshot->correlation_id ||
        ack.status != expected_status || ack.ack_flags != expected_ack_flags)
        return AVERROR_INVALIDDATA;
    if (expected_status == FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK) {
        if (ack.accepted_targets !=
                snapshot->snapshot.target_policy.nb_targets ||
            ack.accepted_manual_regions !=
                snapshot->snapshot.nb_manual_regions)
            return AVERROR_INVALIDDATA;
    } else if (ack.accepted_targets || ack.accepted_manual_regions) {
        return AVERROR_INVALIDDATA;
    }
    return 0;
}

static int server_send_coalesced_snapshots(
    int fd, const FFFrameDecisionROIControlSnapshotMessage *first,
    const FFFrameDecisionROIControlSnapshotMessage *second)
{
    uint8_t wire[2 * FF_FRAME_DECISION_ROI_CONTROL_MAX_MESSAGE_SIZE];
    int first_size;
    int second_size;
    int ret;

    first_size = ff_frame_decision_roi_control_encode_snapshot(
        wire, sizeof(wire), first);
    if (first_size < 0)
        return first_size;
    second_size = ff_frame_decision_roi_control_encode_snapshot(
        wire + first_size, sizeof(wire) - first_size, second);
    if (second_size < 0 ||
        test_write_all(fd, wire, first_size + second_size) < 0)
        return AVERROR(EIO);
    ret = server_read_expected_ack(
        fd, first, FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK,
        FF_FRAME_DECISION_ROI_CONTROL_ACK_DUPLICATE);
    if (ret < 0)
        return ret;
    return server_read_expected_ack(
        fd, second, FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK, 0);
}

static int server_accept(int listener)
{
    int fd;

    do {
        fd = accept(listener, NULL, NULL);
    } while (fd < 0 && errno == EINTR);
    return fd < 0 ? AVERROR(errno) : fd;
}

static void *reconnect_server(void *opaque)
{
    TestServer *server = opaque;
    FFFrameDecisionROIControlSnapshotMessage first = test_snapshot(10, 0);
    FFFrameDecisionROIControlSnapshotMessage second = test_snapshot(11, 1);
    FFFrameDecisionROIControlSnapshotMessage new_epoch = test_snapshot(1, 0);
    FFFrameDecisionROIControlSnapshotMessage stale = test_snapshot(0, 1);
    int listener = -1;
    int connection = -1;
    int ret;

    /* Ensure the client observes a missing socket before the server appears. */
    sleep_ms(150);
    listener = make_listener(server->socket_path);
    if (listener < 0) {
        server_set_error(server, listener);
        return NULL;
    }

    connection = server_accept(listener);
    if (connection < 0 ||
        (ret = server_handshake(connection, TEST_EPOCH)) < 0 ||
        (ret = server_send_snapshot(
             connection, &first,
             FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK, 0)) < 0) {
        server_set_error(server, connection < 0 ? connection : ret);
        goto end;
    }
    server_set_phase(server, 1);
    if (server_wait_gate(server, 1) < 0)
        goto end;
    close(connection);
    connection = -1;

    connection = server_accept(listener);
    if (connection < 0 ||
        (ret = server_handshake(connection, TEST_EPOCH)) < 0 ||
        (ret = server_send_coalesced_snapshots(
             connection, &first, &second)) < 0) {
        server_set_error(server, connection < 0 ? connection : ret);
        goto end;
    }
    server_set_phase(server, 2);
    if (server_wait_gate(server, 2) < 0)
        goto end;
    close(connection);
    connection = -1;

    new_epoch.snapshot.server_epoch = TEST_EPOCH + 1;
    stale.snapshot.server_epoch = TEST_EPOCH + 1;
    connection = server_accept(listener);
    if (connection < 0 ||
        (ret = server_handshake(connection, TEST_EPOCH + 1)) < 0 ||
        (ret = server_send_snapshot(
             connection, &new_epoch,
             FF_FRAME_DECISION_ROI_CONTROL_STATUS_OK, 0)) < 0) {
        server_set_error(server, connection < 0 ? connection : ret);
        goto end;
    }
    server_set_phase(server, 3);
    if (server_wait_gate(server, 3) < 0)
        goto end;
    close(connection);
    connection = -1;

    connection = server_accept(listener);
    if (connection < 0 ||
        (ret = server_handshake(connection, TEST_EPOCH + 1)) < 0 ||
        (ret = server_send_snapshot(
             connection, &stale,
             FF_FRAME_DECISION_ROI_CONTROL_STATUS_STALE_REVISION, 0)) < 0) {
        server_set_error(server, connection < 0 ? connection : ret);
        goto end;
    }
    server_set_phase(server, 4);

end:
    if (connection >= 0)
        close(connection);
    if (listener >= 0)
        close(listener);
    unlink(server->socket_path);
    return NULL;
}

static int wait_for_latest(FFFrameDecisionROIControlClient *client,
                           FFFrameDecisionROIControlSnapshot *snapshot,
                           unsigned timeout_ms)
{
    const int64_t deadline = monotonic_ms() + timeout_ms;

    for (;;) {
        int ret = ff_frame_decision_roi_control_get_latest(client, snapshot);

        if (ret)
            return ret;
        if (monotonic_ms() >= deadline)
            return AVERROR(ETIMEDOUT);
        sleep_ms(10);
    }
}

static int test_reconnect_and_atomic_snapshot(void)
{
    char directory[] = "/tmp/sfro-control-XXXXXX";
    FFFrameDecisionROIControlConfig config = {
        .channel            = TEST_CHANNEL,
        .client_instance_id = 123,
        .reconnect_delay_ms = 20,
    };
    FFFrameDecisionROIControlClient *client = NULL;
    FFFrameDecisionROIControlSnapshot snapshot;
    TestServer server = { 0 };
    pthread_t server_thread;

    CHECK(make_temp_directory(directory) == 0,
          "temporary directory creation failed");
    CHECK(snprintf(server.socket_path, sizeof(server.socket_path),
                   "%s/control.sock", directory) <
              (int)sizeof(server.socket_path),
          "temporary socket path is too long");
    CHECK(pthread_mutex_init(&server.lock, NULL) == 0 &&
          pthread_cond_init(&server.cond, NULL) == 0,
          "test server synchronization init failed");
    config.socket_path = server.socket_path;

    CHECK(ff_frame_decision_roi_control_create(&client, &config) == 0,
          "control client create failed while socket was absent");
    CHECK(ff_frame_decision_roi_control_start(client) == 0,
          "control client start failed while socket was absent");
    CHECK(pthread_create(&server_thread, NULL, reconnect_server,
                         &server) == 0,
          "test server thread creation failed");

    CHECK(wait_for_latest(client, &snapshot, 4000) == 1,
          "first snapshot was not published after reconnect");
    CHECK(snapshot.server_epoch == TEST_EPOCH && snapshot.revision == 10 &&
          snapshot.target_policy.nb_targets == 2 &&
          snapshot.nb_manual_regions == 1 &&
          snapshot.manual_regions[0].x == 100000,
          "first snapshot was not copied atomically");
    CHECK(ff_frame_decision_roi_control_get_latest(client, &snapshot) == 0,
          "same snapshot was delivered twice");
    release_server_gate(&server, 1);

    CHECK(wait_for_latest(client, &snapshot, 4000) == 1,
          "newer snapshot was not published after reconnect");
    CHECK(snapshot.server_epoch == TEST_EPOCH && snapshot.revision == 11 &&
          snapshot.manual_regions[0].x == 300000,
          "newer full snapshot differs");
    release_server_gate(&server, 2);

    CHECK(wait_for_latest(client, &snapshot, 4000) == 1,
          "first snapshot from a new server epoch was not published");
    CHECK(snapshot.server_epoch == TEST_EPOCH + 1 &&
          snapshot.revision == 1 &&
          snapshot.manual_regions[0].x == 100000,
          "new-epoch full snapshot differs");
    release_server_gate(&server, 3);
    CHECK(wait_for_server_phase(&server, 4, 4000) == 0,
          "stale revision NACK was not observed");
    CHECK(ff_frame_decision_roi_control_get_latest(client, &snapshot) == 0,
          "duplicate or stale snapshot replaced retained state");

    ff_frame_decision_roi_control_destroy(&client);
    pthread_join(server_thread, NULL);
    CHECK(!server.error, "test server observed a protocol failure");
    pthread_cond_destroy(&server.cond);
    pthread_mutex_destroy(&server.lock);
    rmdir(directory);
    return 0;
}

static void *silent_server(void *opaque)
{
    TestServer *server = opaque;
    uint8_t wire[80];
    int listener;
    int connection;

    listener = make_listener(server->socket_path);
    if (listener < 0) {
        server_set_error(server, listener);
        return NULL;
    }
    server_set_phase(server, 1);
    connection = server_accept(listener);
    if (connection < 0 || test_read_message(
                              connection, wire, sizeof(wire)) < 0) {
        server_set_error(server, connection);
    } else {
        server_set_phase(server, 2);
        /* Client destroy must interrupt its SERVER_HELLO read. */
        while (read(connection, wire, sizeof(wire)) < 0 && errno == EINTR)
            ;
    }
    if (connection >= 0)
        close(connection);
    close(listener);
    unlink(server->socket_path);
    return NULL;
}

static int test_destroy_interrupts_read(void)
{
    char directory[] = "/tmp/sfro-stop-XXXXXX";
    FFFrameDecisionROIControlConfig config = {
        .channel            = TEST_CHANNEL,
        .client_instance_id = 456,
        .reconnect_delay_ms = 20,
    };
    FFFrameDecisionROIControlClient *client = NULL;
    TestServer server = { 0 };
    pthread_t server_thread;
    int64_t start_ms;

    CHECK(make_temp_directory(directory) == 0,
          "stop-test temporary directory failed");
    CHECK(snprintf(server.socket_path, sizeof(server.socket_path),
                   "%s/control.sock", directory) <
              (int)sizeof(server.socket_path),
          "stop-test socket path is too long");
    CHECK(pthread_mutex_init(&server.lock, NULL) == 0 &&
          pthread_cond_init(&server.cond, NULL) == 0,
          "stop-test synchronization init failed");
    CHECK(pthread_create(&server_thread, NULL, silent_server,
                         &server) == 0,
          "silent server thread creation failed");
    CHECK(wait_for_server_phase(&server, 1, 1000) == 0,
          "silent server did not start");
    config.socket_path = server.socket_path;
    CHECK(ff_frame_decision_roi_control_create(&client, &config) == 0 &&
          ff_frame_decision_roi_control_start(client) == 0,
          "stop-test client start failed");
    CHECK(wait_for_server_phase(&server, 2, 1000) == 0,
          "client did not enter the blocking handshake read");

    start_ms = monotonic_ms();
    ff_frame_decision_roi_control_destroy(&client);
    CHECK(monotonic_ms() - start_ms < 500,
          "client destroy did not interrupt socket read promptly");
    pthread_join(server_thread, NULL);
    CHECK(!server.error, "silent server observed a test failure");
    pthread_cond_destroy(&server.cond);
    pthread_mutex_destroy(&server.lock);
    rmdir(directory);
    return 0;
}

static int test_config_validation(void)
{
    FFFrameDecisionROIControlConfig config = {
        .socket_path = "/tmp/control.sock",
        .channel     = 0,
    };
    FFFrameDecisionROIControlClient *client = (void *)1;

    CHECK(ff_frame_decision_roi_control_create(&client, &config) ==
              AVERROR(EINVAL) && !client,
          "product channel zero was accepted");
    config.channel = 17;
    client = (void *)1;
    CHECK(ff_frame_decision_roi_control_create(&client, &config) ==
              AVERROR(EINVAL) && !client,
          "product channel above sixteen was accepted");
    return 0;
}

int main(void)
{
    if (test_hello_wire() ||
        test_server_hello_wire() ||
        test_snapshot_wire() ||
        test_ack_wire() ||
        test_config_validation() ||
        test_reconnect_and_atomic_snapshot() ||
        test_destroy_interrupts_read())
        return 1;
    return 0;
}
