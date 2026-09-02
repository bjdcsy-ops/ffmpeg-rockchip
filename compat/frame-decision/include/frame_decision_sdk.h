#ifndef FRAME_DECISION_SDK_H
#define FRAME_DECISION_SDK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FD_ABI_VERSION 3u
#define FD_CLIENT_ABI_VERSION 1u
#define FD_DEFAULT_SOCKET_PATH "/run/frame-decision/frame-decision.sock"
#define FD_SOCKET_PATH_SIZE 108
#define FD_MAX_CLASSES 128
#define FD_CLASS_NAME_SIZE 64
#define FD_TARGET_ID_SIZE 32
#define FD_LABEL_DISPLAY_NAME_SIZE 64
#define FD_PATH_SIZE 512
#define FD_DEFAULT_LABEL_POLICY_PATH \
    "/etc/frame-decision-service/protected-labels.conf"
#define FD_YOLOV5_OUTPUTS 3
#define FD_ANCHORS_PER_OUTPUT 3

typedef struct fd_engine fd_engine_t;
typedef struct fd_client fd_client_t;

typedef enum {
    FD_OK = 0,
    FD_ERR_ARGUMENT = -1,
    FD_ERR_CONFIG = -2,
    FD_ERR_MODEL = -3,
    FD_ERR_DECODE = -4,
    FD_ERR_PREPROCESS = -5,
    FD_ERR_INFERENCE = -6,
    FD_ERR_POSTPROCESS = -7,
    FD_ERR_TOO_LARGE = -8,
    FD_ERR_BUSY = -9,
    FD_ERR_SUPERSEDED = -10,
    FD_ERR_PROTOCOL = -11,
    FD_ERR_IO = -12,
    FD_ERR_EXPIRED = -13,
    FD_ERR_UNSUPPORTED = -14,
    FD_ERR_AGAIN = -15,
    FD_ERR_UNCERTAIN = -16
} fd_status_t;

typedef enum {
    FD_IMAGE_JPEG = 1,
    FD_IMAGE_RGB24 = 2,
    FD_IMAGE_BGR24 = 3,
    FD_IMAGE_NV12 = 4
} fd_image_format_t;

typedef enum {
    FD_NPU_AUTO = 0,
    FD_NPU_CORE_0 = 1,
    FD_NPU_CORE_1 = 2,
    FD_NPU_CORE_2 = 3,
    FD_NPU_CORES_0_1_2 = 4
} fd_npu_core_t;

typedef enum {
    FD_CATEGORY_NONE = 0,
    FD_CATEGORY_PERSON = 1,
    FD_CATEGORY_MOTOR_VEHICLE = 2,
    FD_CATEGORY_NON_MOTOR_VEHICLE = 3
} fd_category_t;

typedef struct {
    uint32_t abi_version;
    char model_path[FD_PATH_SIZE];
    char class_names[FD_MAX_CLASSES][FD_CLASS_NAME_SIZE];
    fd_category_t category_by_class[FD_MAX_CLASSES];
    char target_ids_by_class[FD_MAX_CLASSES][FD_TARGET_ID_SIZE];
    uint8_t emit_by_class[FD_MAX_CLASSES];
    uint32_t class_count;
    char decoder[32];
    float confidence_threshold;
    float nms_threshold;
    float anchors[FD_YOLOV5_OUTPUTS][FD_ANCHORS_PER_OUTPUT * 2];
    uint32_t strides[FD_YOLOV5_OUTPUTS];
    float roi_expand_ratio;
    uint32_t roi_expand_min_pixels;
    uint8_t pad_value;
    fd_npu_core_t npu_core;
    uint32_t max_channels;
    uint64_t max_queued_bytes;
    uint32_t max_width;
    uint32_t max_height;
    uint64_t max_jpeg_bytes;
    uint64_t max_raw_bytes;
} fd_engine_config_t;

typedef struct {
    uint32_t abi_version;
    char socket_path[FD_SOCKET_PATH_SIZE];
    uint32_t connect_timeout_ms;
    uint32_t io_timeout_ms;
} fd_client_config_t;

typedef struct {
    uint64_t request_id;
    uint32_t channel_id;
    uint64_t frame_sequence;
    int64_t pts_us;
    fd_image_format_t format;
    const uint8_t *data;
    size_t data_size;
    uint32_t width;
    uint32_t height;
    uint32_t stride0;
    uint32_t stride1;
} fd_image_t;

typedef struct {
    int32_t class_id;
    char class_name[FD_CLASS_NAME_SIZE];
    fd_category_t category;
    char target_id[FD_TARGET_ID_SIZE];
    float score;
    uint32_t x1;
    uint32_t y1;
    uint32_t x2;
    uint32_t y2;
} fd_roi_t;

typedef struct {
    int32_t class_id;
    char class_name[FD_CLASS_NAME_SIZE];
    fd_category_t category;
    char display_name[FD_LABEL_DISPLAY_NAME_SIZE];
} fd_label_descriptor_t;

typedef struct {
    uint32_t model_class_count;
    uint32_t label_count;
    fd_label_descriptor_t labels[FD_MAX_CLASSES];
} fd_label_policy_t;

typedef struct {
    uint64_t request_id;
    uint32_t channel_id;
    uint64_t frame_sequence;
    int64_t pts_us;
    fd_status_t status;
    uint8_t has_target;
    uint8_t reserved[3];
    uint32_t roi_count;
    fd_roi_t *rois;
    double decode_ms;
    double preprocess_ms;
    double queue_ms;
    double inference_ms;
    double postprocess_ms;
    double total_ms;
    char message[256];
} fd_result_t;

void fd_engine_config_init(fd_engine_config_t *config);
int fd_engine_config_load(const char *path, fd_engine_config_t *config,
                          char *error, size_t error_size);
int fd_label_policy_load(const char *path, fd_label_policy_t *policy,
                         char *error, size_t error_size);
int fd_engine_create(const fd_engine_config_t *config, fd_engine_t **engine,
                     char *error, size_t error_size);
int fd_engine_detect(fd_engine_t *engine, const fd_image_t *image,
                     fd_result_t *result);
int fd_engine_health(fd_engine_t *engine, char *message, size_t message_size);
void fd_result_release(fd_result_t *result);
void fd_engine_destroy(fd_engine_t *engine);

void fd_client_config_init(fd_client_config_t *config);
int fd_client_create(const fd_client_config_t *config, fd_client_t **client,
                     char *error, size_t error_size);
int fd_client_health(fd_client_t *client, char *message, size_t message_size);
int fd_client_detect(fd_client_t *client, const fd_image_t *image,
                     fd_result_t *result);
void fd_client_destroy(fd_client_t *client);

const char *fd_status_string(fd_status_t status);
const char *fd_category_string(fd_category_t category);

#ifdef __cplusplus
}
#endif
#endif
