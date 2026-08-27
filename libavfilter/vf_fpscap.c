/*
 * Copyright (c) 2026
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

/**
 * @file
 * Limit a video stream to a maximum frame rate without changing timestamps.
 */

#include "libavutil/mathematics.h"
#include "libavutil/opt.h"

#include "avfilter.h"
#include "filters.h"
#include "video.h"

typedef struct FPSCapContext {
    const AVClass *class;

    AVRational fps;

    int initialized;
    int64_t first_pts;
    int64_t previous_pts;
    int64_t last_slot;

    int64_t frames_in;
    int64_t frames_out;
    int64_t frames_dropped;
} FPSCapContext;

#define OFFSET(x) offsetof(FPSCapContext, x)
#define FLAGS (AV_OPT_FLAG_FILTERING_PARAM | AV_OPT_FLAG_VIDEO_PARAM)

static const AVOption fpscap_options[] = {
    { "fps", "set the maximum output frame rate", OFFSET(fps),
      AV_OPT_TYPE_VIDEO_RATE, { .str = "25" }, 0, INT_MAX, FLAGS },
    { NULL }
};

AVFILTER_DEFINE_CLASS(fpscap);

static int config_output_props(AVFilterLink *outlink)
{
    AVFilterContext *ctx = outlink->src;
    FPSCapContext *s = ctx->priv;
    AVFilterLink *inlink = ctx->inputs[0];
    FilterLink *il = ff_filter_link(inlink);
    FilterLink *ol = ff_filter_link(outlink);

    if (il->frame_rate.num <= 0 || il->frame_rate.den <= 0) {
        av_log(ctx, AV_LOG_ERROR,
               "Input frame rate is unknown or invalid; fpscap requires a "
               "valid nominal input frame rate.\n");
        return AVERROR(EINVAL);
    }

    if (s->fps.num <= 0 || s->fps.den <= 0) {
        av_log(ctx, AV_LOG_ERROR,
               "Maximum frame rate must be greater than zero.\n");
        return AVERROR(EINVAL);
    }

    if (inlink->time_base.num <= 0 || inlink->time_base.den <= 0) {
        av_log(ctx, AV_LOG_ERROR,
               "Input time base is invalid; fpscap cannot assign PTS time "
               "slots.\n");
        return AVERROR(EINVAL);
    }

    outlink->time_base = inlink->time_base;
    ol->frame_rate = av_cmp_q(il->frame_rate, s->fps) > 0 ?
                     s->fps : il->frame_rate;

    av_log(ctx, AV_LOG_VERBOSE,
           "input frame rate %d/%d, maximum %d/%d, output frame rate %d/%d\n",
           il->frame_rate.num, il->frame_rate.den,
           s->fps.num, s->fps.den,
           ol->frame_rate.num, ol->frame_rate.den);

    return 0;
}

static int drop_frame(FPSCapContext *s, AVFrame **frame)
{
    s->frames_dropped++;
    av_frame_free(frame);
    return 0;
}

static int output_frame(AVFilterLink *inlink, FPSCapContext *s, AVFrame *frame)
{
    s->frames_out++;
    return ff_filter_frame(inlink->dst->outputs[0], frame);
}

static int filter_frame(AVFilterLink *inlink, AVFrame *frame)
{
    AVFilterContext *ctx = inlink->dst;
    FPSCapContext *s = ctx->priv;
    int64_t delta, slot;

    s->frames_in++;

    if (frame->pts == AV_NOPTS_VALUE) {
        av_log(ctx, AV_LOG_ERROR,
               "Missing input PTS; fpscap requires valid timestamps.\n");
        av_frame_free(&frame);
        return AVERROR_INVALIDDATA;
    }

    if (!s->initialized) {
        s->initialized = 1;
        s->first_pts = frame->pts;
        s->previous_pts = frame->pts;
        s->last_slot = 0;
        return output_frame(inlink, s, frame);
    }

    if (frame->pts < s->previous_pts) {
        av_log(ctx, AV_LOG_ERROR,
               "Input PTS moved backwards from %"PRId64" to %"PRId64
               "; fpscap requires monotonically non-decreasing timestamps.\n",
               s->previous_pts, frame->pts);
        av_frame_free(&frame);
        return AVERROR_INVALIDDATA;
    }

    if (frame->pts == s->previous_pts)
        return drop_frame(s, &frame);

    s->previous_pts = frame->pts;

    if (s->first_pts < 0 && frame->pts > INT64_MAX + s->first_pts) {
        /* The forward gap cannot be represented as a signed timestamp delta.
         * Its slot cannot be compared reliably, so fail closed by dropping
         * this frame and re-anchor future slot calculations at its PTS. */
        s->first_pts = frame->pts;
        s->last_slot = 0;
        return drop_frame(s, &frame);
    }

    delta = frame->pts - s->first_pts;
    slot = av_rescale_q_rnd(delta, inlink->time_base, av_inv_q(s->fps),
                            AV_ROUND_DOWN);

    if (slot == INT64_MIN) {
        /* The forward gap crossed more slots than fit in an int64_t. */
        s->first_pts = frame->pts;
        s->last_slot = 0;
        return output_frame(inlink, s, frame);
    }

    if (slot <= s->last_slot)
        return drop_frame(s, &frame);

    s->last_slot = slot;
    return output_frame(inlink, s, frame);
}

static av_cold void uninit(AVFilterContext *ctx)
{
    FPSCapContext *s = ctx->priv;

    av_log(ctx, AV_LOG_VERBOSE,
           "%"PRId64" frames in, %"PRId64" frames out; %"PRId64
           " frames dropped.\n",
           s->frames_in, s->frames_out, s->frames_dropped);
}

static const AVFilterPad fpscap_inputs[] = {
    {
        .name = "default",
        .type = AVMEDIA_TYPE_VIDEO,
        .filter_frame = filter_frame,
    },
};

static const AVFilterPad fpscap_outputs[] = {
    {
        .name = "default",
        .type = AVMEDIA_TYPE_VIDEO,
        .config_props = config_output_props,
    },
};

const FFFilter ff_vf_fpscap = {
    .p.name        = "fpscap",
    .p.description = NULL_IF_CONFIG_SMALL("Limit a video stream to a maximum frame rate."),
    .p.priv_class  = &fpscap_class,
    .p.flags       = AVFILTER_FLAG_METADATA_ONLY,
    .priv_size     = sizeof(FPSCapContext),
    .uninit        = uninit,
    FILTER_INPUTS(fpscap_inputs),
    FILTER_OUTPUTS(fpscap_outputs),
};
