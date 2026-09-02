/*
 * Frame-decision driven ROI metadata filter
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

#include <drm_fourcc.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#include "libavutil/common.h"
#include "libavutil/error.h"
#include "libavutil/hwcontext.h"
#include "libavutil/hwcontext_drm.h"
#include "libavutil/mathematics.h"
#include "libavutil/mem.h"
#include "libavutil/opt.h"
#include "libavutil/pixdesc.h"
#include "libavutil/time.h"

#include "avfilter.h"
#include "filters.h"
#include "frame_decision_roi.h"
#include "frame_decision_roi_adapter.h"
#include "rkrga_common.h"
#include "video.h"

#define FD_ROI_DEFAULT_SOCKET \
    "/run/frame-decision/frame-decision-v3.sock"
#define FD_ROI_DEFAULT_SDK_LIBRARY \
    "/opt/frame-decision-service/lib/libframe_decision_client.so.2"
#define FD_ROI_LOG_INTERVAL_US (5 * 1000 * 1000)
#define FD_ROI_SHUTDOWN_GRACE_US (250 * 1000)

typedef struct FrameDecisionROIJob {
    AVFrame *frame;
    uint64_t request_id;
    uint64_t frame_sequence;
    int64_t pts_us;
    int64_t capture_time_us;
    int width;
    int height;
} FrameDecisionROIJob;

typedef struct FrameDecisionROIContext {
    /* rkrga_common requires RKRGAContext to be the first member. */
    RKRGAContext rga;

    char *sdk_library;
    char *socket_path;
    int channel_id;
    int sample_interval_ms;
    int ttl_ms;
    int connect_timeout_ms;
    int io_timeout_ms;
    char *target_qoffsets;
    int debug_overlay;
    int debug_thickness;
    int rga_initialized;

    FFFrameDecisionROITargetPolicy target_policy;
    int detector_enabled;

    pthread_t worker;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    int sync_initialized;
    int worker_started;
    int worker_exited;
    int stop;
    int adapter_stopped;

    FrameDecisionROIJob pending;
    AVFrame *pinned_frame;
    FFFrameDecisionROIAdapter *adapter;

    AVRegionOfInterest latest[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    int nb_latest;
    int64_t latest_capture_time_us;
    uint64_t latest_sequence;

    uint64_t next_request_id;
    uint64_t next_frame_sequence;
    int64_t next_sample_time_us;
    int64_t last_worker_log_us;
} FrameDecisionROIContext;

static int any_target_enabled(const FrameDecisionROIContext *s)
{
    int i;

    for (i = 0; i < s->target_policy.nb_targets; i++) {
        if (s->target_policy.targets[i].qoffset.num)
            return 1;
    }
    return 0;
}

static void log_worker_error(AVFilterContext *avctx, const char *operation,
                             int error)
{
    FrameDecisionROIContext *s = avctx->priv;
    const int64_t now_us = av_gettime_relative();

    if (s->last_worker_log_us &&
        now_us - s->last_worker_log_us < FD_ROI_LOG_INTERVAL_US)
        return;
    s->last_worker_log_us = now_us;
    av_log(avctx, AV_LOG_WARNING, "%s failed: %s\n",
           operation, av_err2str(error));
}

static int drm_request_from_frame(AVFilterContext *avctx,
                                  const FrameDecisionROIJob *job,
                                  FFFrameDecisionROIDMABufRequest *request)
{
    const AVFrame *frame = job->frame;
    const AVDRMFrameDescriptor *desc;
    const AVDRMLayerDescriptor *layer;
    const AVDRMPlaneDescriptor *plane0;
    const AVDRMPlaneDescriptor *plane1;
    uint64_t y_end;
    uint64_t uv_end;

    if (!frame || frame->format != AV_PIX_FMT_DRM_PRIME ||
        !frame->data[0] || frame->width <= 0 || frame->height <= 0 ||
        frame->width % 2 || frame->height % 2) {
        av_log(avctx, AV_LOG_ERROR,
               "frame_decision_roi requires even-sized DRM_PRIME frames\n");
        return AVERROR(EINVAL);
    }

    desc = (const AVDRMFrameDescriptor *)frame->data[0];
    if (desc->nb_objects != 1 || desc->nb_layers != 1) {
        av_log(avctx, AV_LOG_ERROR,
               "frame_decision_roi requires one DRM object and one layer\n");
        return AVERROR(EINVAL);
    }
    layer = &desc->layers[0];
    if (desc->objects[0].fd < 0 || !desc->objects[0].size ||
        desc->objects[0].format_modifier != DRM_FORMAT_MOD_LINEAR ||
        layer->format != DRM_FORMAT_NV12 || layer->nb_planes != 2) {
        av_log(avctx, AV_LOG_ERROR,
               "frame_decision_roi requires linear DRM_FORMAT_NV12\n");
        return AVERROR(ENOSYS);
    }

    plane0 = &layer->planes[0];
    plane1 = &layer->planes[1];
    if (plane0->object_index != 0 || plane1->object_index != 0 ||
        plane0->offset != 0 || plane1->offset < 0 ||
        plane0->pitch <= 0 || plane1->pitch <= 0 ||
        plane0->pitch != plane1->pitch ||
        plane0->pitch < frame->width ||
        plane0->offset > UINT32_MAX || plane1->offset > UINT32_MAX ||
        plane0->pitch > UINT32_MAX || plane1->pitch > UINT32_MAX) {
        av_log(avctx, AV_LOG_ERROR,
               "invalid linear NV12 DRM plane layout\n");
        return AVERROR(EINVAL);
    }

    y_end = (uint64_t)plane0->offset +
            (uint64_t)plane0->pitch * frame->height;
    uv_end = (uint64_t)plane1->offset +
             (uint64_t)plane1->pitch * (frame->height / 2);
    if ((uint64_t)plane1->offset < y_end ||
        y_end > desc->objects[0].size ||
        uv_end > desc->objects[0].size) {
        av_log(avctx, AV_LOG_ERROR,
               "NV12 DRM planes exceed or overlap the DMA-BUF object\n");
        return AVERROR(EINVAL);
    }

    *request = (FFFrameDecisionROIDMABufRequest) {
        .request_id       = job->request_id,
        .frame_sequence   = job->frame_sequence,
        .pts_us           = job->pts_us,
        .capture_time_us  = job->capture_time_us,
        .deadline_time_us = job->capture_time_us +
                            (int64_t)((FrameDecisionROIContext *)avctx->priv)
                                ->ttl_ms * 1000,
        .fd               = desc->objects[0].fd,
        .object_size      = desc->objects[0].size,
        .format_modifier  = desc->objects[0].format_modifier,
        .drm_fourcc       = layer->format,
        .width            = frame->width,
        .height           = frame->height,
        .plane_count      = 2,
        .offsets          = {
            (uint32_t)plane0->offset,
            (uint32_t)plane1->offset,
        },
        .pitches          = {
            (uint32_t)plane0->pitch,
            (uint32_t)plane1->pitch,
        },
    };

    return 0;
}

static int debug_fill_rectangle(rga_buffer_t output, im_rect rectangle,
                                uint32_t color)
{
    IM_STATUS status;

    if (rectangle.width <= 0 || rectangle.height <= 0)
        return 0;

    status = imfill_t(output, rectangle, (int)color, 1);
    if (status != IM_STATUS_SUCCESS && status != IM_STATUS_NOERROR)
        return AVERROR_EXTERNAL;
    return 0;
}

static int debug_draw_region(rga_buffer_t output,
                             const AVRegionOfInterest *roi,
                             int configured_thickness)
{
    const int width = roi->right - roi->left;
    const int height = roi->bottom - roi->top;
    const int thickness = FFMIN(configured_thickness,
                                FFMIN(width, height) / 2) & ~1;
    /* librga's sample/API color convention: opaque bright green. */
    const uint32_t color = 0xff00ff00;
    int ret;

    if (thickness < 2)
        return 0;

    ret = debug_fill_rectangle(output,
                               (im_rect) { roi->left, roi->top,
                                           width, thickness }, color);
    if (ret < 0)
        return ret;
    ret = debug_fill_rectangle(output,
                               (im_rect) { roi->left,
                                           roi->bottom - thickness,
                                           width, thickness }, color);
    if (ret < 0)
        return ret;
    ret = debug_fill_rectangle(output,
                               (im_rect) { roi->left,
                                           roi->top + thickness,
                                           thickness,
                                           height - 2 * thickness }, color);
    if (ret < 0)
        return ret;
    return debug_fill_rectangle(output,
                                (im_rect) { roi->right - thickness,
                                            roi->top + thickness,
                                            thickness,
                                            height - 2 * thickness }, color);
}

static int debug_draw_and_output(AVFilterLink *outlink, AVFrame *frame)
{
    AVFilterContext *avctx = outlink->src;
    FrameDecisionROIContext *s = avctx->priv;
    const AVFrameSideData *side_data = av_frame_get_side_data(
        frame, AV_FRAME_DATA_REGIONS_OF_INTEREST);
    const AVDRMFrameDescriptor *desc;
    const AVDRMLayerDescriptor *layer;
    const AVRegionOfInterest *first;
    FFFrameDecisionROIDMABufRequest validated_request;
    FrameDecisionROIJob validation_job = { .frame = frame };
    rga_buffer_t output;
    uint32_t roi_size;
    uint64_t hstride64;
    int hstride;
    int nb_regions;
    int i;
    int ret;

    if (!side_data)
        return ff_filter_frame(outlink, frame);
    if (side_data->size < sizeof(AVRegionOfInterest)) {
        av_log(avctx, AV_LOG_ERROR,
               "debug overlay received invalid ROI side data\n");
        return AVERROR(EINVAL);
    }

    first = (const AVRegionOfInterest *)side_data->data;
    roi_size = first->self_size;
    if (roi_size < sizeof(AVRegionOfInterest) ||
        side_data->size % roi_size) {
        av_log(avctx, AV_LOG_ERROR,
               "debug overlay received invalid ROI self_size\n");
        return AVERROR(EINVAL);
    }
    nb_regions = side_data->size / roi_size;
    if (nb_regions > FF_FRAME_DECISION_ROI_MAX_REGIONS) {
        av_log(avctx, AV_LOG_ERROR,
               "debug overlay received more than %d normalized ROIs\n",
               FF_FRAME_DECISION_ROI_MAX_REGIONS);
        return AVERROR(EINVAL);
    }

    ret = drm_request_from_frame(avctx, &validation_job,
                                 &validated_request);
    if (ret < 0)
        return ret;

    desc = (const AVDRMFrameDescriptor *)frame->data[0];
    layer = &desc->layers[0];
    if (layer->planes[0].offset != 0 || layer->planes[0].pitch <= 0 ||
        layer->planes[1].offset <= 0 ||
        layer->planes[1].offset % layer->planes[0].pitch) {
        av_log(avctx, AV_LOG_ERROR,
               "debug overlay cannot wrap the RGA output plane layout\n");
        return AVERROR(EINVAL);
    }
    hstride64 = (uint64_t)layer->planes[1].offset /
                (uint64_t)layer->planes[0].pitch;
    if (layer->planes[0].pitch > INT_MAX || !hstride64 ||
        hstride64 > INT_MAX) {
        av_log(avctx, AV_LOG_ERROR,
               "debug overlay received invalid RGA output strides\n");
        return AVERROR(EINVAL);
    }
    hstride = (int)hstride64;

    output = wrapbuffer_fd_t(desc->objects[0].fd,
                             frame->width, frame->height,
                             (int)layer->planes[0].pitch, hstride,
                             RK_FORMAT_YCbCr_420_SP);
    for (i = 0; i < nb_regions; i++) {
        const AVRegionOfInterest *roi =
            (const AVRegionOfInterest *)(side_data->data +
                                         (size_t)i * roi_size);

        if (roi->self_size != roi_size ||
            roi->left < 0 || roi->top < 0 ||
            roi->right > frame->width || roi->bottom > frame->height ||
            roi->right <= roi->left || roi->bottom <= roi->top) {
            av_log(avctx, AV_LOG_ERROR,
                   "debug overlay received invalid ROI bounds at region %d\n",
                   i);
            return AVERROR(EINVAL);
        }
        ret = debug_draw_region(output, roi, s->debug_thickness);
        if (ret < 0) {
            av_log(avctx, AV_LOG_ERROR,
                   "RGA debug ROI drawing failed for region %d\n", i);
            return ret;
        }
    }

    return ff_filter_frame(outlink, frame);
}

static void stop_adapter_with_pinned_input(AVFilterContext *avctx,
                                           FrameDecisionROIJob *job,
                                           int error)
{
    FrameDecisionROIContext *s = avctx->priv;
    FrameDecisionROIJob pending = { 0 };

    pthread_mutex_lock(&s->lock);
    s->adapter_stopped = 1;
    s->pinned_frame = job->frame;
    job->frame = NULL;
    pending = s->pending;
    memset(&s->pending, 0, sizeof(s->pending));
    pthread_mutex_unlock(&s->lock);

    av_frame_free(&pending.frame);
    av_log(avctx, AV_LOG_ERROR,
           "frame-decision input ownership became uncertain (%s); "
           "pinning one DRM frame and stopping detector submissions\n",
           av_err2str(error < 0 ? error : AVERROR(EIO)));
}

static int worker_should_stop(FrameDecisionROIContext *s)
{
    int stop;

    pthread_mutex_lock(&s->lock);
    stop = s->stop;
    pthread_mutex_unlock(&s->lock);
    return stop;
}

static void worker_mark_exited(FrameDecisionROIContext *s)
{
    pthread_mutex_lock(&s->lock);
    s->worker_exited = 1;
    pthread_cond_broadcast(&s->cond);
    pthread_mutex_unlock(&s->lock);
}

static int wait_for_worker_exit(FrameDecisionROIContext *s,
                                int64_t grace_us)
{
    struct timespec deadline;
    int wait_status = 0;
    int exited;

    if (clock_gettime(CLOCK_MONOTONIC, &deadline))
        return 0;
    deadline.tv_sec += grace_us / AV_TIME_BASE;
    deadline.tv_nsec += grace_us % AV_TIME_BASE * 1000;
    if (deadline.tv_nsec >= 1000000000L) {
        deadline.tv_sec++;
        deadline.tv_nsec -= 1000000000L;
    }

    pthread_mutex_lock(&s->lock);
    while (!s->worker_exited && !wait_status)
        wait_status = pthread_cond_timedwait(&s->cond, &s->lock,
                                             &deadline);
    exited = s->worker_exited;
    pthread_mutex_unlock(&s->lock);
    return exited;
}

static void publish_result(AVFilterContext *avctx,
                           const FrameDecisionROIJob *job,
                           const FFFrameDecisionROIAdapterResult *result)
{
    FrameDecisionROIContext *s = avctx->priv;
    AVRegionOfInterest normalized[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROINormalizeStats stats;
    const int64_t now_us = av_gettime_relative();
    int nb_normalized;

    if (result->status || result->request_id != job->request_id ||
        result->frame_sequence != job->frame_sequence ||
        result->pts_us != job->pts_us || result->nb_detections < 0 ||
        result->nb_detections > FF_FRAME_DECISION_ROI_MAX_DETECTIONS)
        return;

    if (!ff_frame_decision_roi_snapshot_is_fresh(job->capture_time_us,
                                                  now_us, s->ttl_ms))
        return;

    nb_normalized = ff_frame_decision_roi_normalize(
        normalized, &stats, result->detections, result->nb_detections,
        &s->target_policy, job->width, job->height);
    if (nb_normalized < 0) {
        log_worker_error(avctx, "normalizing frame-decision result",
                         nb_normalized);
        return;
    }

    pthread_mutex_lock(&s->lock);
    if (!s->stop && job->frame_sequence >= s->latest_sequence) {
        memcpy(s->latest, normalized,
               (size_t)nb_normalized * sizeof(normalized[0]));
        s->nb_latest = nb_normalized;
        s->latest_capture_time_us = job->capture_time_us;
        s->latest_sequence = job->frame_sequence;
    }
    pthread_mutex_unlock(&s->lock);

    if (stats.truncated)
        av_log(avctx, AV_LOG_VERBOSE,
               "frame-decision ROI set truncated by %d region(s)\n",
               stats.truncated);
}

static void *frame_decision_roi_worker(void *opaque)
{
    AVFilterContext *avctx = opaque;
    FrameDecisionROIContext *s = avctx->priv;

    for (;;) {
        FFFrameDecisionROIDMABufRequest request;
        FFFrameDecisionROIAdapterResult result;
        FFFrameDecisionROIInputDisposition disposition =
            FF_FRAME_DECISION_ROI_INPUT_RELEASE;
        FrameDecisionROIJob job = { 0 };
        int ret;

        pthread_mutex_lock(&s->lock);
        while (!s->stop && !s->pending.frame)
            pthread_cond_wait(&s->cond, &s->lock);
        if (s->stop) {
            pthread_mutex_unlock(&s->lock);
            break;
        }
        job = s->pending;
        memset(&s->pending, 0, sizeof(s->pending));
        pthread_mutex_unlock(&s->lock);

        ret = drm_request_from_frame(avctx, &job, &request);
        if (ret < 0) {
            av_frame_free(&job.frame);
            continue;
        }
        if (worker_should_stop(s)) {
            av_frame_free(&job.frame);
            break;
        }

        ret = ff_frame_decision_roi_adapter_submit(s->adapter, &request);
        if (ret < 0) {
            av_frame_free(&job.frame);
            if (ret == AVERROR(ENOSYS)) {
                pthread_mutex_lock(&s->lock);
                s->adapter_stopped = 1;
                pthread_mutex_unlock(&s->lock);
            }
            log_worker_error(avctx, "submitting frame-decision DMA-BUF", ret);
            continue;
        }

        ret = ff_frame_decision_roi_adapter_await_input(s->adapter,
                                                        &disposition);
        if (ret < 0 ||
            disposition == FF_FRAME_DECISION_ROI_INPUT_PIN_AND_STOP) {
            stop_adapter_with_pinned_input(avctx, &job, ret);
            break;
        }

        /* INPUT_CONSUMED is the first point where decoder reuse is safe. */
        av_frame_free(&job.frame);
        if (worker_should_stop(s))
            break;

        memset(&result, 0, sizeof(result));
        ret = ff_frame_decision_roi_adapter_receive_result(s->adapter,
                                                           &result);
        if (ret < 0) {
            log_worker_error(avctx, "receiving frame-decision result", ret);
            continue;
        }

        publish_result(avctx, &job, &result);
    }

    worker_mark_exited(s);
    return NULL;
}

static int schedule_detection(AVFilterContext *avctx, AVFilterLink *inlink,
                              const AVFrame *frame, int64_t now_us)
{
    FrameDecisionROIContext *s = avctx->priv;
    FrameDecisionROIJob job = { 0 };
    FrameDecisionROIJob replaced = { 0 };

    pthread_mutex_lock(&s->lock);
    if (s->stop || s->adapter_stopped ||
        now_us < s->next_sample_time_us) {
        pthread_mutex_unlock(&s->lock);
        return 0;
    }
    s->next_sample_time_us = now_us + (int64_t)s->sample_interval_ms * 1000;
    job.request_id = s->next_request_id++;
    job.frame_sequence = s->next_frame_sequence++;
    pthread_mutex_unlock(&s->lock);

    job.frame = av_frame_clone(frame);
    if (!job.frame)
        return AVERROR(ENOMEM);
    job.capture_time_us = now_us;
    job.width = frame->width;
    job.height = frame->height;
    job.pts_us = frame->pts == AV_NOPTS_VALUE
               ? AV_NOPTS_VALUE
               : av_rescale_q(frame->pts, inlink->time_base,
                              AV_TIME_BASE_Q);

    pthread_mutex_lock(&s->lock);
    if (s->stop || s->adapter_stopped) {
        pthread_mutex_unlock(&s->lock);
        av_frame_free(&job.frame);
        return 0;
    }
    replaced = s->pending;
    s->pending = job;
    pthread_cond_signal(&s->cond);
    pthread_mutex_unlock(&s->lock);

    av_frame_free(&replaced.frame);
    return 0;
}

static int frame_decision_roi_filter_frame(AVFilterLink *inlink,
                                           AVFrame *frame)
{
    AVFilterContext *avctx = inlink->dst;
    AVFilterLink *outlink = avctx->outputs[0];
    FrameDecisionROIContext *s = avctx->priv;
    AVRegionOfInterest snapshot[FF_FRAME_DECISION_ROI_MAX_REGIONS];
    FFFrameDecisionROIDMABufRequest validated_request;
    FrameDecisionROIJob validation_job = { .frame = frame };
    const int64_t now_us = av_gettime_relative();
    int nb_snapshot = 0;
    int ret;

    ret = drm_request_from_frame(avctx, &validation_job,
                                 &validated_request);
    if (ret < 0)
        goto fail;

    if (s->detector_enabled) {
        pthread_mutex_lock(&s->lock);
        if (ff_frame_decision_roi_snapshot_is_fresh(
                s->latest_capture_time_us, now_us, s->ttl_ms)) {
            nb_snapshot = s->nb_latest;
            memcpy(snapshot, s->latest,
                   (size_t)nb_snapshot * sizeof(snapshot[0]));
        }
        pthread_mutex_unlock(&s->lock);

        /*
         * Once enabled, this filter owns the ROI side-data contract and an
         * expired/empty snapshot intentionally clears earlier ROI metadata.
         * With every target disabled it is a true no-op and must preserve
         * any ROI side data supplied by an upstream filter.
         */
        ret = ff_frame_decision_roi_attach(frame, snapshot, nb_snapshot);
        if (ret < 0)
            goto fail;

        ret = schedule_detection(avctx, inlink, frame, now_us);
        if (ret < 0)
            goto fail;
    }

    if (s->debug_overlay) {
        /*
         * rkrga_common allocates and blits into a separate DRM frame before
         * debug_draw_and_output() reads this frame's copied ROI side data.
         * Never draw into the decoder-owned input: the worker may still have
         * a clone of that DMA-BUF submitted to frame-decision.
         */
        ret = ff_rkrga_filter_frame(&s->rga, inlink, frame, NULL, NULL);
        av_frame_free(&frame);
        return ret;
    }

    /* Production mode is a true pass-through with no pixel processing. */
    return ff_filter_frame(outlink, frame);

fail:
    av_frame_free(&frame);
    return ret;
}

static av_cold int frame_decision_roi_init(AVFilterContext *avctx)
{
    FrameDecisionROIContext *s = avctx->priv;
    pthread_condattr_t cond_attr;
    int ret;

    ret = ff_frame_decision_roi_parse_target_qoffsets(&s->target_policy,
                                                       s->target_qoffsets);
    if (ret < 0) {
        av_log(avctx, AV_LOG_ERROR,
               "target_qoffsets must contain unique "
               "target_id=qoffset entries; IDs use [a-z0-9._-] and "
               "qoffset rationals must be in [-1, 1]\n");
        return ret;
    }

    s->detector_enabled = any_target_enabled(s);
    if (s->debug_overlay && s->debug_thickness % 2) {
        av_log(avctx, AV_LOG_ERROR,
               "debug_thickness must be even for NV12 RGA drawing\n");
        return AVERROR(EINVAL);
    }
    if (!s->detector_enabled)
        return 0;
    if (s->channel_id < 0) {
        av_log(avctx, AV_LOG_ERROR,
               "channel_id is required when any target qoffset is non-zero\n");
        return AVERROR(EINVAL);
    }

    ret = pthread_mutex_init(&s->lock, NULL);
    if (ret)
        return AVERROR(ret);
    ret = pthread_condattr_init(&cond_attr);
    if (ret) {
        pthread_mutex_destroy(&s->lock);
        return AVERROR(ret);
    }
    ret = pthread_condattr_setclock(&cond_attr, CLOCK_MONOTONIC);
    if (!ret)
        ret = pthread_cond_init(&s->cond, &cond_attr);
    pthread_condattr_destroy(&cond_attr);
    if (ret) {
        pthread_mutex_destroy(&s->lock);
        return AVERROR(ret);
    }
    s->sync_initialized = 1;
    s->latest_capture_time_us = -1;
    s->next_request_id = 1;
    s->next_frame_sequence = 1;

    return 0;
}

static int frame_decision_roi_config_output(AVFilterLink *outlink)
{
    AVFilterContext *avctx = outlink->src;
    AVFilterLink *inlink = avctx->inputs[0];
    FilterLink *inl = ff_filter_link(inlink);
    FilterLink *outl = ff_filter_link(outlink);
    FrameDecisionROIContext *s = avctx->priv;
    AVHWFramesContext *frames_ctx;
    FFFrameDecisionROIAdapterConfig adapter_config;
    RKRGAParam rga_param = { 0 };
    int ret;

    /* This filter never changes geometry, including in debug-overlay mode. */
    outlink->w = inlink->w;
    outlink->h = inlink->h;
    outlink->sample_aspect_ratio = inlink->sample_aspect_ratio;

    if (!inl->hw_frames_ctx) {
        av_log(avctx, AV_LOG_ERROR,
               "frame_decision_roi requires an input hardware frames context\n");
        return AVERROR(EINVAL);
    }
    frames_ctx = (AVHWFramesContext *)inl->hw_frames_ctx->data;
    if (frames_ctx->format != AV_PIX_FMT_DRM_PRIME ||
        frames_ctx->sw_format != AV_PIX_FMT_NV12) {
        av_log(avctx, AV_LOG_ERROR,
               "frame_decision_roi requires DRM_PRIME frames with NV12 "
               "software format\n");
        return AVERROR(ENOSYS);
    }

    if (s->debug_overlay) {
        s->rga.async_depth = 0;
        s->rga.afbc_out = 0;
        rga_param.filter_frame = debug_draw_and_output;
        rga_param.out_sw_format = AV_PIX_FMT_NV12;
        ret = ff_rkrga_init(avctx, &rga_param);
        if (ret < 0)
            return ret;
        s->rga_initialized = 1;
    } else {
        outl->hw_frames_ctx = av_buffer_ref(inl->hw_frames_ctx);
        if (!outl->hw_frames_ctx)
            return AVERROR(ENOMEM);
    }

    if (!s->detector_enabled || s->worker_started)
        return 0;

    adapter_config = (FFFrameDecisionROIAdapterConfig) {
        .log_ctx           = avctx,
        .sdk_library       = s->sdk_library,
        .socket_path        = s->socket_path,
        .channel_id         = s->channel_id,
        .target_policy      = &s->target_policy,
        .connect_timeout_ms = s->connect_timeout_ms,
        .io_timeout_ms      = s->io_timeout_ms,
    };
    ret = ff_frame_decision_roi_adapter_create(&s->adapter,
                                                &adapter_config);
    if (ret < 0)
        return ret;

    ret = pthread_create(&s->worker, NULL, frame_decision_roi_worker, avctx);
    if (ret) {
        ff_frame_decision_roi_adapter_destroy(&s->adapter);
        return AVERROR(ret);
    }
    s->worker_started = 1;

    return 0;
}

static av_cold void frame_decision_roi_uninit(AVFilterContext *avctx)
{
    FrameDecisionROIContext *s = avctx->priv;
    FrameDecisionROIJob pending = { 0 };

    if (s->sync_initialized) {
        pthread_mutex_lock(&s->lock);
        s->stop = 1;
        pending = s->pending;
        memset(&s->pending, 0, sizeof(s->pending));
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);

        av_frame_free(&pending.frame);
        if (s->worker_started &&
            !wait_for_worker_exit(s, FD_ROI_SHUTDOWN_GRACE_US))
            ff_frame_decision_roi_adapter_interrupt(s->adapter);
        if (s->worker_started)
            pthread_join(s->worker, NULL);

        ff_frame_decision_roi_adapter_destroy(&s->adapter);
        if (s->pinned_frame) {
            av_log(avctx, AV_LOG_ERROR,
                   "leaving one DRM frame pinned because frame-decision "
                   "input ownership could not be proven safe\n");
            /* Safety over reuse: deliberately retain the frame until exit. */
            s->pinned_frame = NULL;
        }

        pthread_cond_destroy(&s->cond);
        pthread_mutex_destroy(&s->lock);
        s->sync_initialized = 0;
    }

    if (s->rga_initialized) {
        ff_rkrga_close(avctx);
        s->rga_initialized = 0;
    }
}

#define OFFSET(x) offsetof(FrameDecisionROIContext, x)
#define FLAGS (AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_FILTERING_PARAM)

static const AVOption frame_decision_roi_options[] = {
    { "sdk_library", "frame-decision SDK shared-library path",
      OFFSET(sdk_library), AV_OPT_TYPE_STRING,
      { .str = FD_ROI_DEFAULT_SDK_LIBRARY }, .flags = FLAGS },
    { "socket", "frame-decision V3 Unix socket path",
      OFFSET(socket_path), AV_OPT_TYPE_STRING,
      { .str = FD_ROI_DEFAULT_SOCKET }, .flags = FLAGS },
    { "channel_id", "frame-decision logical channel (required when enabled)",
      OFFSET(channel_id), AV_OPT_TYPE_INT, { .i64 = -1 }, -1, 15, FLAGS },
    { "sample_interval_ms", "minimum interval between detector submissions",
      OFFSET(sample_interval_ms), AV_OPT_TYPE_INT,
      { .i64 = 100 }, 1, 60000, FLAGS },
    { "ttl_ms", "ROI lifetime measured from source-frame capture",
      OFFSET(ttl_ms), AV_OPT_TYPE_INT,
      { .i64 = 500 }, 1, 600000, FLAGS },
    { "connect_timeout_ms", "frame-decision connection timeout",
      OFFSET(connect_timeout_ms), AV_OPT_TYPE_INT,
      { .i64 = 1000 }, 1, 600000, FLAGS },
    { "io_timeout_ms", "frame-decision I/O timeout",
      OFFSET(io_timeout_ms), AV_OPT_TYPE_INT,
      { .i64 = 5000 }, 1, 600000, FLAGS },
    { "target_qoffsets", "target_id=qoffset pairs separated by '|'; zero disables a target",
      OFFSET(target_qoffsets), AV_OPT_TYPE_STRING,
      { .str = NULL }, .flags = FLAGS },
    { "debug_overlay", "draw the normalized ROI snapshot (development mode)",
      OFFSET(debug_overlay), AV_OPT_TYPE_BOOL,
      { .i64 = 0 }, 0, 1, FLAGS },
    { "debug_thickness", "debug ROI border thickness in pixels",
      OFFSET(debug_thickness), AV_OPT_TYPE_INT,
      { .i64 = 4 }, 2, 16, FLAGS },
    { NULL },
};

AVFILTER_DEFINE_CLASS(frame_decision_roi);

static const AVFilterPad frame_decision_roi_inputs[] = {
    {
        .name         = "default",
        .type         = AVMEDIA_TYPE_VIDEO,
        .filter_frame = frame_decision_roi_filter_frame,
    },
};

static const AVFilterPad frame_decision_roi_outputs[] = {
    {
        .name         = "default",
        .type         = AVMEDIA_TYPE_VIDEO,
        .config_props = frame_decision_roi_config_output,
    },
};

const FFFilter ff_vf_frame_decision_roi = {
    .p.name         = "frame_decision_roi",
    .p.description  = NULL_IF_CONFIG_SMALL(
        "Attach frame-decision ROI metadata to linear NV12 DRM frames."),
    .p.priv_class   = &frame_decision_roi_class,
    .p.flags        = AVFILTER_FLAG_HWDEVICE,
    .priv_size      = sizeof(FrameDecisionROIContext),
    .init           = frame_decision_roi_init,
    .uninit         = frame_decision_roi_uninit,
    .flags_internal = FF_FILTER_FLAG_HWFRAME_AWARE,
    FILTER_INPUTS(frame_decision_roi_inputs),
    FILTER_SINGLE_PIXFMT(AV_PIX_FMT_DRM_PRIME),
    FILTER_OUTPUTS(frame_decision_roi_outputs),
};
