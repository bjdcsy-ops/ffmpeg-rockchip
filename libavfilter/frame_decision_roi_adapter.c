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

#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>

#include <frame_decision_async.h>

#include "libavutil/error.h"
#include "libavutil/log.h"
#include "libavutil/mem.h"
#include "libavutil/time.h"

#include "frame_decision_roi_adapter.h"

#define FD_ROI_RECONNECT_DELAY_US 100000

typedef struct FFFrameDecisionROIAPI {
    void (*client_config_init)(fd_v3_client_config_t *config);
    int (*client_create)(const fd_v3_client_config_t *config,
                         fd_v3_client_t **client,
                         char *error, size_t error_size);
    int (*client_connect)(fd_v3_client_t *client, uint64_t *server_epoch,
                          int *epoch_changed);
    int (*client_submit_dmabuf)(fd_v3_client_t *client,
                                const fd_v3_dmabuf_frame_t *frame);
    int (*client_receive)(fd_v3_client_t *client, fd_v3_event_t *event,
                          uint32_t timeout_ms);
    int (*client_query_input)(fd_v3_client_t *client,
                              const fd_v3_input_key_t *key);
    void (*client_interrupt)(fd_v3_client_t *client);
    int (*client_has_uncertain_input)(const fd_v3_client_t *client,
                                      fd_v3_input_key_t *key);
    uint64_t (*client_instance_id)(const fd_v3_client_t *client);
    void (*event_release)(fd_v3_event_t *event);
    void (*client_destroy)(fd_v3_client_t *client);
} FFFrameDecisionROIAPI;

struct FFFrameDecisionROIAdapter {
    void *log_ctx;
    void *library;
    FFFrameDecisionROIAPI api;
    fd_v3_client_t *client;
    pthread_mutex_t interrupt_lock;
    int interrupt_lock_initialized;
    int interrupted;
    int connected;

    uint32_t channel_id;
    uint64_t generation;
    fd_v3_input_key_t current_key;
    int has_current_key;

    fd_v3_event_t buffered_result;
    int has_buffered_result;
    int result_error;
};

static int load_symbol(FFFrameDecisionROIAdapter *adapter,
                       void *destination, size_t destination_size,
                       const char *name)
{
    const char *error;
    void *symbol;

    dlerror();
    symbol = dlsym(adapter->library, name);
    error = dlerror();
    if (error || !symbol) {
        av_log(adapter->log_ctx, AV_LOG_ERROR,
               "frame-decision SDK is missing V3 symbol %s: %s\n",
               name, error ? error : "symbol not found");
        return AVERROR(ENOSYS);
    }
    if (destination_size != sizeof(symbol)) {
        av_log(adapter->log_ctx, AV_LOG_ERROR,
               "frame-decision SDK function-pointer ABI is incompatible\n");
        return AVERROR(ENOSYS);
    }
    memcpy(destination, &symbol, sizeof(symbol));
    return 0;
}

static int load_api(FFFrameDecisionROIAdapter *adapter)
{
    int ret;

#define LOAD_API(field, symbol) do {                                      \
    ret = load_symbol(adapter, &adapter->api.field,                       \
                      sizeof(adapter->api.field), #symbol);               \
    if (ret < 0)                                                          \
        return ret;                                                       \
} while (0)

    LOAD_API(client_config_init, fd_v3_client_config_init);
    LOAD_API(client_create, fd_v3_client_create);
    LOAD_API(client_connect, fd_v3_client_connect);
    LOAD_API(client_submit_dmabuf, fd_v3_client_submit_dmabuf);
    LOAD_API(client_receive, fd_v3_client_receive);
    LOAD_API(client_query_input, fd_v3_client_query_input);
    LOAD_API(client_interrupt, fd_v3_client_interrupt);
    LOAD_API(client_has_uncertain_input,
             fd_v3_client_has_uncertain_input);
    LOAD_API(client_instance_id, fd_v3_client_instance_id);
    LOAD_API(event_release, fd_v3_event_release);
    LOAD_API(client_destroy, fd_v3_client_destroy);

#undef LOAD_API
    return 0;
}

static int status_to_averror(fd_status_t status)
{
    switch (status) {
    case FD_OK:
        return 0;
    case FD_ERR_ARGUMENT:
    case FD_ERR_CONFIG:
        return AVERROR(EINVAL);
    case FD_ERR_BUSY:
    case FD_ERR_SUPERSEDED:
    case FD_ERR_AGAIN:
        return AVERROR(EAGAIN);
    case FD_ERR_EXPIRED:
        return AVERROR(ETIMEDOUT);
    case FD_ERR_UNSUPPORTED:
        return AVERROR(ENOSYS);
    case FD_ERR_PROTOCOL:
        return AVERROR(EPROTO);
    case FD_ERR_IO:
    case FD_ERR_UNCERTAIN:
        return AVERROR(EIO);
    case FD_ERR_MODEL:
    case FD_ERR_DECODE:
    case FD_ERR_PREPROCESS:
    case FD_ERR_INFERENCE:
    case FD_ERR_POSTPROCESS:
    case FD_ERR_TOO_LARGE:
        return AVERROR_EXTERNAL;
    default:
        return AVERROR_UNKNOWN;
    }
}

static int input_key_equal(const fd_v3_input_key_t *left,
                           const fd_v3_input_key_t *right)
{
    return left->request_id == right->request_id &&
           left->client_instance_id == right->client_instance_id &&
           left->generation == right->generation &&
           left->sequence == right->sequence &&
           left->channel_id == right->channel_id;
}

static int adapter_interrupted(FFFrameDecisionROIAdapter *adapter)
{
    int interrupted;

    pthread_mutex_lock(&adapter->interrupt_lock);
    interrupted = adapter->interrupted;
    pthread_mutex_unlock(&adapter->interrupt_lock);
    return interrupted;
}

static int connect_client(FFFrameDecisionROIAdapter *adapter,
                          int *epoch_changed)
{
    uint64_t server_epoch;
    int changed = 0;
    int status;

    if (adapter_interrupted(adapter))
        return AVERROR(EINTR);

    status = adapter->api.client_connect(adapter->client, &server_epoch,
                                         &changed);
    if (status != FD_OK) {
        adapter->connected = 0;
        return status_to_averror(status);
    }
    if (adapter_interrupted(adapter)) {
        adapter->api.client_interrupt(adapter->client);
        adapter->connected = 0;
        return AVERROR(EINTR);
    }
    if (!server_epoch)
        return AVERROR(EPROTO);

    adapter->connected = 1;
    if (epoch_changed)
        *epoch_changed = changed;
    return 0;
}

static void clear_buffered_result(FFFrameDecisionROIAdapter *adapter)
{
    if (adapter->has_buffered_result) {
        adapter->api.event_release(&adapter->buffered_result);
        adapter->has_buffered_result = 0;
    }
}

static int buffer_matching_result(FFFrameDecisionROIAdapter *adapter,
                                  fd_v3_event_t *event)
{
    if (!adapter->has_current_key ||
        !input_key_equal(&event->key, &adapter->current_key))
        return 0;

    clear_buffered_result(adapter);
    adapter->buffered_result = *event;
    memset(event, 0, sizeof(*event));
    adapter->has_buffered_result = 1;
    return 1;
}

static int receive_event(FFFrameDecisionROIAdapter *adapter,
                         fd_v3_event_t *event)
{
    const int status = adapter->api.client_receive(adapter->client, event,
                                                    UINT32_MAX);

    if (status == FD_ERR_IO || status == FD_ERR_PROTOCOL)
        adapter->connected = 0;
    return status_to_averror(status);
}

static int reconnect_and_query(FFFrameDecisionROIAdapter *adapter,
                               int *input_released)
{
    fd_v3_input_key_t uncertain_key;
    int epoch_changed;
    int ret;

    *input_released = 0;
    for (;;) {
        if (adapter_interrupted(adapter))
            return AVERROR(EINTR);

        ret = connect_client(adapter, &epoch_changed);
        if (ret >= 0)
            break;

        /*
         * The media thread remains independent while this worker retains at
         * most one submitted frame.  Retry until the daemon returns or
         * shutdown interrupts us; releasing an unresolved DMA-BUF is unsafe.
         */
        av_usleep(FD_ROI_RECONNECT_DELAY_US);
    }

    if (epoch_changed) {
        adapter->result_error = AVERROR(EPIPE);
        *input_released = 1;
        return 0;
    }

    if (!adapter->api.client_has_uncertain_input(adapter->client,
                                                  &uncertain_key) ||
        !adapter->has_current_key ||
        !input_key_equal(&uncertain_key, &adapter->current_key))
        return AVERROR(EPROTO);

    ret = status_to_averror(adapter->api.client_query_input(
        adapter->client, &uncertain_key));
    if (ret < 0)
        adapter->connected = 0;
    return ret;
}

static int copy_result(FFFrameDecisionROIAdapter *adapter,
                       FFFrameDecisionROIAdapterResult *result,
                       const fd_v3_event_t *event)
{
    const fd_result_t *source = &event->result;
    uint32_t i;

    if (!adapter->has_current_key ||
        !input_key_equal(&event->key, &adapter->current_key) ||
        source->roi_count > FF_FRAME_DECISION_ROI_MAX_DETECTIONS ||
        (source->roi_count && !source->rois))
        return AVERROR(EPROTO);

    memset(result, 0, sizeof(*result));
    result->request_id = source->request_id;
    result->frame_sequence = source->frame_sequence;
    result->pts_us = source->pts_us;
    result->status = source->status;
    result->nb_detections = source->roi_count;
    for (i = 0; i < source->roi_count; i++) {
        const fd_roi_t *roi = &source->rois[i];

        result->detections[i] = (FFFrameDecisionROIDetection) {
            .category = roi->category,
            .score    = roi->score,
            .left     = roi->x1,
            .top      = roi->y1,
            .right    = roi->x2,
            .bottom   = roi->y2,
        };
    }
    return 0;
}

int ff_frame_decision_roi_adapter_create(
    FFFrameDecisionROIAdapter **adapter,
    const FFFrameDecisionROIAdapterConfig *config)
{
    FFFrameDecisionROIAdapter *created;
    fd_v3_client_config_t client_config;
    char error[128];
    int ret;
    int status;

    if (!adapter || !config || !config->sdk_library ||
        !config->sdk_library[0] ||
        !config->socket_path ||
        config->channel_id < 0 ||
        strlen(config->socket_path) >= FD_SOCKET_PATH_SIZE ||
        !config->connect_timeout_ms || !config->io_timeout_ms)
        return AVERROR(EINVAL);
    *adapter = NULL;

    created = av_mallocz(sizeof(*created));
    if (!created)
        return AVERROR(ENOMEM);
    created->log_ctx = config->log_ctx;
    created->library = dlopen(config->sdk_library, RTLD_NOW | RTLD_LOCAL);
    if (!created->library) {
        av_log(created->log_ctx, AV_LOG_ERROR,
               "cannot load frame-decision SDK %s: %s\n",
               config->sdk_library, dlerror());
        av_free(created);
        return AVERROR(ENOENT);
    }
    ret = load_api(created);
    if (ret < 0)
        goto fail;

    status = pthread_mutex_init(&created->interrupt_lock, NULL);
    if (status) {
        ret = AVERROR(status);
        goto fail;
    }
    created->interrupt_lock_initialized = 1;
    created->channel_id = config->channel_id;
    created->generation = 1;

    created->api.client_config_init(&client_config);
    memcpy(client_config.socket_path, config->socket_path,
           strlen(config->socket_path) + 1);
    client_config.connect_timeout_ms = config->connect_timeout_ms;
    client_config.io_timeout_ms = config->io_timeout_ms;
    error[0] = '\0';
    status = created->api.client_create(&client_config, &created->client,
                                        error, sizeof(error));
    if (status != FD_OK) {
        av_log(created->log_ctx, AV_LOG_ERROR,
               "frame-decision V3 client rejected ABI %u: %s "
               "(status %d)\n", FD_V3_CLIENT_ABI_VERSION,
               error[0] ? error : "unknown error", status);
        ret = status_to_averror(status);
        goto fail;
    }

    *adapter = created;
    return 0;

fail:
    if (created->client)
        created->api.client_destroy(created->client);
    if (created->interrupt_lock_initialized)
        pthread_mutex_destroy(&created->interrupt_lock);
    if (created->library)
        dlclose(created->library);
    av_free(created);
    return ret;
}

int ff_frame_decision_roi_adapter_submit(
    FFFrameDecisionROIAdapter *adapter,
    const FFFrameDecisionROIDMABufRequest *request)
{
    fd_v3_dmabuf_frame_t frame;
    int status;
    int ret;

    if (!adapter || !request || !request->request_id ||
        !request->frame_sequence || request->capture_time_us <= 0 ||
        request->deadline_time_us <= request->capture_time_us ||
        request->capture_time_us > INT64_MAX / 1000 ||
        request->deadline_time_us > INT64_MAX / 1000 ||
        request->width <= 0 || request->height <= 0)
        return AVERROR(EINVAL);

    if (!adapter->connected) {
        ret = connect_client(adapter, NULL);
        if (ret < 0)
            return ret;
    }

    clear_buffered_result(adapter);
    adapter->has_current_key = 0;
    adapter->result_error = 0;
    memset(&frame, 0, sizeof(frame));
    frame.key.request_id = request->request_id;
    frame.key.client_instance_id =
        adapter->api.client_instance_id(adapter->client);
    frame.key.generation = adapter->generation;
    frame.key.sequence = request->frame_sequence;
    frame.key.channel_id = adapter->channel_id;
    frame.pts_us = request->pts_us;
    frame.submit_monotonic_ns = (uint64_t)request->capture_time_us * 1000;
    frame.deadline_monotonic_ns =
        (uint64_t)request->deadline_time_us * 1000;
    frame.dma_fd = request->fd;
    frame.object_size = request->object_size;
    frame.format_modifier = request->format_modifier;
    frame.drm_fourcc = request->drm_fourcc;
    frame.width = request->width;
    frame.height = request->height;
    frame.plane_count = request->plane_count;
    memcpy(frame.offsets, request->offsets, sizeof(frame.offsets));
    memcpy(frame.pitches, request->pitches, sizeof(frame.pitches));

    status = adapter->api.client_submit_dmabuf(adapter->client, &frame);
    if (status != FD_OK) {
        if (status == FD_ERR_IO || status == FD_ERR_PROTOCOL)
            adapter->connected = 0;
        return status_to_averror(status);
    }

    adapter->current_key = frame.key;
    adapter->has_current_key = 1;
    return 0;
}

int ff_frame_decision_roi_adapter_await_input(
    FFFrameDecisionROIAdapter *adapter,
    FFFrameDecisionROIInputDisposition *disposition)
{
    int need_recovery = 0;

    if (!adapter || !disposition || !adapter->has_current_key)
        return AVERROR(EINVAL);
    *disposition = FF_FRAME_DECISION_ROI_INPUT_RELEASE;

    for (;;) {
        fd_v3_event_t event;
        int input_released;
        int ret;

        if (adapter_interrupted(adapter)) {
            *disposition = FF_FRAME_DECISION_ROI_INPUT_PIN_AND_STOP;
            return AVERROR(EINTR);
        }

        if (need_recovery) {
            ret = reconnect_and_query(adapter, &input_released);
            if (ret < 0) {
                if (ret == AVERROR(EINTR) || ret == AVERROR(EPROTO)) {
                    *disposition =
                        FF_FRAME_DECISION_ROI_INPUT_PIN_AND_STOP;
                    return ret;
                }
                need_recovery = 1;
                continue;
            }
            if (input_released)
                return 0;
            need_recovery = 0;
        }

        memset(&event, 0, sizeof(event));
        ret = receive_event(adapter, &event);
        if (ret < 0) {
            adapter->api.event_release(&event);
            need_recovery = 1;
            continue;
        }

        if (event.type == FD_V3_EVENT_RESULT) {
            buffer_matching_result(adapter, &event);
            adapter->api.event_release(&event);
            continue;
        }
        if ((event.type != FD_V3_EVENT_INPUT_CONSUMED &&
             event.type != FD_V3_EVENT_INPUT_STATE) ||
            !input_key_equal(&event.key, &adapter->current_key)) {
            adapter->api.event_release(&event);
            continue;
        }

        if (event.input_state == FD_V3_INPUT_CONSUMED) {
            if (event.status != FD_OK)
                adapter->result_error = status_to_averror(event.status);
            adapter->api.event_release(&event);
            return 0;
        }
        if (event.input_state == FD_V3_INPUT_UNKNOWN) {
            adapter->api.event_release(&event);
            *disposition = FF_FRAME_DECISION_ROI_INPUT_PIN_AND_STOP;
            return AVERROR(EPROTO);
        }
        adapter->api.event_release(&event);
    }
}

int ff_frame_decision_roi_adapter_receive_result(
    FFFrameDecisionROIAdapter *adapter,
    FFFrameDecisionROIAdapterResult *result)
{
    int ret;

    if (!adapter || !result || !adapter->has_current_key)
        return AVERROR(EINVAL);

    if (adapter->result_error) {
        ret = adapter->result_error;
        adapter->result_error = 0;
        adapter->has_current_key = 0;
        return ret;
    }

    for (;;) {
        fd_v3_event_t event;

        if (adapter->has_buffered_result) {
            ret = copy_result(adapter, result, &adapter->buffered_result);
            clear_buffered_result(adapter);
            adapter->has_current_key = 0;
            return ret;
        }

        memset(&event, 0, sizeof(event));
        ret = receive_event(adapter, &event);
        if (ret < 0) {
            adapter->api.event_release(&event);
            adapter->has_current_key = 0;
            return ret;
        }
        if (event.type != FD_V3_EVENT_RESULT ||
            !input_key_equal(&event.key, &adapter->current_key)) {
            adapter->api.event_release(&event);
            continue;
        }

        ret = copy_result(adapter, result, &event);
        adapter->api.event_release(&event);
        adapter->has_current_key = 0;
        return ret;
    }
}

void ff_frame_decision_roi_adapter_interrupt(
    FFFrameDecisionROIAdapter *adapter)
{
    if (!adapter)
        return;

    pthread_mutex_lock(&adapter->interrupt_lock);
    adapter->interrupted = 1;
    pthread_mutex_unlock(&adapter->interrupt_lock);
    adapter->api.client_interrupt(adapter->client);
}

void ff_frame_decision_roi_adapter_destroy(
    FFFrameDecisionROIAdapter **adapter)
{
    FFFrameDecisionROIAdapter *destroyed;

    if (!adapter || !*adapter)
        return;
    destroyed = *adapter;
    *adapter = NULL;

    clear_buffered_result(destroyed);
    destroyed->api.client_destroy(destroyed->client);
    if (destroyed->interrupt_lock_initialized)
        pthread_mutex_destroy(&destroyed->interrupt_lock);
    if (destroyed->library)
        dlclose(destroyed->library);
    av_free(destroyed);
}
