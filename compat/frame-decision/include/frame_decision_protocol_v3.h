#ifndef FRAME_DECISION_PROTOCOL_V3_H
#define FRAME_DECISION_PROTOCOL_V3_H

#include "frame_decision_sdk.h"

#include <stddef.h>
#include <stdint.h>

#define FD_V3_WIRE_MAGIC 0x33445646u /* FDV3 */
#define FD_V3_WIRE_VERSION 3u
#define FD_V3_WIRE_MAX_PACKET_SIZE 32768u
#define FD_V3_WIRE_MAX_ROIS 256u
#define FD_V3_WIRE_MAX_FDS 1u
#define FD_V3_WIRE_MAX_TARGETS 16u
#define FD_V3_MESSAGE_SIZE 128u

#define FD_V3_CAP_DMABUF (1ull << 0)
#define FD_V3_CAP_TARGETED_ROI (1ull << 1)

/* DRM_FORMAT_NV12 and DRM_FORMAT_MOD_LINEAR without a libdrm header dependency. */
#define FD_V3_DRM_FORMAT_NV12 0x3231564eu
#define FD_V3_DRM_FORMAT_MOD_LINEAR 0u

typedef enum {
    FD_V3_WIRE_HELLO = 1,
    FD_V3_WIRE_HELLO_ACK = 2,
    FD_V3_WIRE_SUBMIT_DMABUF = 3,
    FD_V3_WIRE_INPUT_CONSUMED = 4,
    FD_V3_WIRE_RESULT = 5,
    FD_V3_WIRE_QUERY_INPUT = 6,
    FD_V3_WIRE_INPUT_STATE = 7,
    FD_V3_WIRE_CONFIGURE_TARGETS = 8,
    FD_V3_WIRE_CONFIGURE_TARGETS_ACK = 9,
    FD_V3_WIRE_SUBMIT_DMABUF_TARGETED = 10,
    FD_V3_WIRE_RESULT_TARGETED = 11
} fd_v3_wire_type_t;

typedef enum {
    FD_V3_INPUT_UNKNOWN = 0,
    FD_V3_INPUT_PROCESSING = 1,
    FD_V3_INPUT_CONSUMED = 2
} fd_v3_input_state_t;

/*
 * Every V3 packet begins with this fixed 64-byte header. All peers are local
 * RK3588 processes, so V3 intentionally uses native little-endian integers.
 */
typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t type;
    uint32_t packet_size;
    int32_t status;
    uint64_t server_epoch;
    uint64_t client_instance_id;
    uint64_t generation;
    uint64_t sequence;
    uint64_t request_id;
    uint32_t channel_id;
    uint32_t reserved;
} fd_v3_wire_header_t;

typedef struct {
    fd_v3_wire_header_t header;
} fd_v3_wire_hello_t;

typedef struct {
    fd_v3_wire_header_t header;
    uint32_t max_channels;
    uint32_t max_rois;
    uint64_t capabilities;
} fd_v3_wire_hello_ack_t;

typedef struct {
    fd_v3_wire_header_t header;
    uint32_t target_count;
    uint32_t reserved;
    char target_ids[][FD_TARGET_ID_SIZE];
} fd_v3_wire_configure_targets_t;

typedef struct {
    fd_v3_wire_header_t header;
    uint32_t target_count;
    uint32_t reserved;
    char message[FD_V3_MESSAGE_SIZE];
} fd_v3_wire_configure_targets_ack_t;

typedef struct {
    fd_v3_wire_header_t header;
    int64_t pts_us;
    uint64_t submit_monotonic_ns;
    uint64_t deadline_monotonic_ns;
    uint64_t object_size;
    uint64_t format_modifier;
    uint32_t drm_fourcc;
    uint32_t width;
    uint32_t height;
    uint32_t plane_count;
    uint32_t offsets[2];
    uint32_t pitches[2];
    uint32_t reserved[2];
} fd_v3_wire_submit_dmabuf_t;

typedef struct {
    fd_v3_wire_header_t header;
    uint32_t input_state;
    uint32_t reserved;
    uint64_t consumed_monotonic_ns;
    char message[FD_V3_MESSAGE_SIZE];
} fd_v3_wire_input_state_t;

typedef struct {
    uint32_t category;
    int32_t class_id;
    char class_name[FD_CLASS_NAME_SIZE];
    float score;
    uint32_t x1;
    uint32_t y1;
    uint32_t x2;
    uint32_t y2;
} fd_v3_wire_roi_t;

typedef struct {
    uint32_t category;
    int32_t class_id;
    char class_name[FD_CLASS_NAME_SIZE];
    float score;
    uint32_t x1;
    uint32_t y1;
    uint32_t x2;
    uint32_t y2;
    char target_id[FD_TARGET_ID_SIZE];
} fd_v3_wire_targeted_roi_t;

typedef struct {
    fd_v3_wire_header_t header;
    int64_t pts_us;
    uint64_t submit_monotonic_ns;
    uint64_t deadline_monotonic_ns;
    uint64_t completed_monotonic_ns;
    uint64_t decode_ns;
    uint64_t preprocess_ns;
    uint64_t queue_ns;
    uint64_t inference_ns;
    uint64_t postprocess_ns;
    uint64_t total_ns;
    uint32_t width;
    uint32_t height;
    uint32_t has_target;
    uint32_t roi_count;
    char message[FD_V3_MESSAGE_SIZE];
    fd_v3_wire_roi_t rois[];
} fd_v3_wire_result_t;

typedef struct {
    fd_v3_wire_header_t header;
    int64_t pts_us;
    uint64_t submit_monotonic_ns;
    uint64_t deadline_monotonic_ns;
    uint64_t completed_monotonic_ns;
    uint64_t decode_ns;
    uint64_t preprocess_ns;
    uint64_t queue_ns;
    uint64_t inference_ns;
    uint64_t postprocess_ns;
    uint64_t total_ns;
    uint32_t width;
    uint32_t height;
    uint32_t has_target;
    uint32_t roi_count;
    char message[FD_V3_MESSAGE_SIZE];
    fd_v3_wire_targeted_roi_t rois[];
} fd_v3_wire_targeted_result_t;

_Static_assert(sizeof(fd_v3_wire_header_t) == 64,
               "unexpected V3 header layout");
_Static_assert(sizeof(fd_v3_wire_submit_dmabuf_t) == 144,
               "unexpected V3 submit layout");
_Static_assert(sizeof(fd_v3_wire_input_state_t) == 208,
               "unexpected V3 input-state layout");
_Static_assert(sizeof(fd_v3_wire_roi_t) == 92,
               "unexpected V3 ROI layout");
_Static_assert(sizeof(fd_v3_wire_targeted_roi_t) == 124,
               "unexpected V3 targeted ROI layout");
_Static_assert(offsetof(fd_v3_wire_result_t, rois) == 288,
               "unexpected V3 result layout");
_Static_assert(offsetof(fd_v3_wire_targeted_result_t, rois) == 288,
               "unexpected V3 targeted result layout");
_Static_assert(offsetof(fd_v3_wire_configure_targets_t, target_ids) == 72,
               "unexpected V3 target configuration layout");
_Static_assert(sizeof(fd_v3_wire_configure_targets_ack_t) == 200,
               "unexpected V3 target configuration ack layout");
_Static_assert(offsetof(fd_v3_wire_targeted_result_t, rois) +
               FD_V3_WIRE_MAX_ROIS * sizeof(fd_v3_wire_targeted_roi_t) <=
               FD_V3_WIRE_MAX_PACKET_SIZE,
               "maximum V3 targeted result exceeds packet limit");

#endif
