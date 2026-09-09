/*
 * Rockchip MPP encoder region-of-interest support
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

#include <errno.h>
#include <stddef.h>
#include <stdint.h>

#include "libavutil/common.h"
#include "libavutil/error.h"
#include "libavutil/log.h"
#include "libavutil/mathematics.h"
#include "libavutil/rational.h"

#include "rkmppenc_roi.h"

const FFRKMPPEncROIOps ff_rkmppenc_roi_mpp_ops = {
    .init       = mpp_enc_roi_init,
    .deinit     = mpp_enc_roi_deinit,
    .add_region = mpp_enc_roi_add_region,
    .setup_meta = mpp_enc_roi_setup_meta,
};

static const FFRKMPPEncROIOps *roi_mpp_ops(const FFRKMPPEncROIContext *roi)
{
    return roi->mpp_ops ? roi->mpp_ops : &ff_rkmppenc_roi_mpp_ops;
}

int ff_rkmppenc_roi_map_region(RoiRegionCfg *mpp_roi,
                               const AVRegionOfInterest *roi,
                               int frame_width, int frame_height)
{
    int delta_qp;

    if (!mpp_roi || !roi ||
        frame_width <= 0 || frame_width > UINT16_MAX ||
        frame_height <= 0 || frame_height > UINT16_MAX ||
        roi->qoffset.den <= 0 ||
        av_cmp_q(roi->qoffset, (AVRational) { -1, 1 }) < 0 ||
        av_cmp_q(roi->qoffset, (AVRational) {  1, 1 }) > 0 ||
        roi->left < 0 || roi->top < 0 ||
        roi->right <= roi->left || roi->bottom <= roi->top ||
        roi->right > frame_width || roi->bottom > frame_height ||
        roi->right > UINT16_MAX || roi->bottom > UINT16_MAX)
        return AVERROR(EINVAL);

    delta_qp = av_clip((int)av_rescale_rnd(roi->qoffset.num, 51,
                                           roi->qoffset.den,
                                           AV_ROUND_NEAR_INF),
                       -51, 51);

    *mpp_roi = (RoiRegionCfg) {
        .x           = roi->left,
        .y           = roi->top,
        .w           = roi->right  - roi->left,
        .h           = roi->bottom - roi->top,
        .force_intra = 0,
        .qp_mode     = 0,
        .qp_val      = delta_qp,
    };

    return 0;
}

static int roi_regions_overlap(const RoiRegionCfg *a,
                               const RoiRegionCfg *b)
{
    return a->x < b->x + b->w && a->x + a->w > b->x &&
           a->y < b->y + b->h && a->y + a->h > b->y;
}

int ff_rkmppenc_roi_init(AVCodecContext *avctx,
                         FFRKMPPEncROIContext *roi)
{
    if (!roi)
        return AVERROR(EINVAL);

    if (roi->initialized)
        ff_rkmppenc_roi_uninit(avctx, roi);

    roi->initialized = 1;
    roi->metadata_used = 0;
    roi->regions_warned = 0;
    return 0;
}

void ff_rkmppenc_roi_uninit(AVCodecContext *avctx,
                            FFRKMPPEncROIContext *roi)
{
    (void)avctx;

    if (!roi || !roi->initialized)
        return;

    roi->metadata_used = 0;
    roi->regions_warned = 0;
    roi->initialized = 0;
}

static int collect_frame_regions(AVCodecContext *avctx,
                                 FFRKMPPEncROIContext *roi,
                                 const AVFrame *frame,
                                 RoiRegionCfg *regions, int *nb_regions)
{
    const AVFrameSideData *sd;
    const uint8_t *roi_data;
    uint32_t roi_size;
    int count;
    int i;
    int j;

    *nb_regions = 0;
    sd = av_frame_get_side_data(frame, AV_FRAME_DATA_REGIONS_OF_INTEREST);
    if (!sd)
        return 0;
    if (sd->size < sizeof(uint32_t)) {
        av_log(avctx, AV_LOG_ERROR,
               "Invalid regions of interest side data.\n");
        return AVERROR(EINVAL);
    }

    roi_size = ((const AVRegionOfInterest *)sd->data)->self_size;
    if (roi_size < sizeof(AVRegionOfInterest) || sd->size % roi_size) {
        av_log(avctx, AV_LOG_ERROR,
               "Invalid AVRegionOfInterest.self_size.\n");
        return AVERROR(EINVAL);
    }

    roi_data = sd->data;
    count = sd->size / roi_size;
    if (count > FF_RKMPPENC_ROI_MAX_REGIONS) {
        if (!roi->regions_warned) {
            av_log(avctx, AV_LOG_WARNING,
                   "More ROIs set than supported by RKMPP (%d > %d); "
                   "using the first %d regions.\n",
                   count, FF_RKMPPENC_ROI_MAX_REGIONS,
                   FF_RKMPPENC_ROI_MAX_REGIONS);
            roi->regions_warned = 1;
        }
        count = FF_RKMPPENC_ROI_MAX_REGIONS;
    }

    for (i = 0; i < count; i++) {
        const AVRegionOfInterest *src =
            (const AVRegionOfInterest *)(roi_data + (size_t)i * roi_size);
        int ret = ff_rkmppenc_roi_map_region(&regions[i], src,
                                             frame->width, frame->height);

        if (ret < 0) {
            av_log(avctx, AV_LOG_ERROR,
                   "Invalid ROI[%d]: region (%d,%d)-(%d,%d), "
                   "qoffset %d/%d, frame %dx%d.\n",
                   i, src->left, src->top, src->right, src->bottom,
                   src->qoffset.num, src->qoffset.den,
                   frame->width, frame->height);
            return ret;
        }

        /*
         * ROI2 first builds a 16x16 map and then folds it into codec CTUs.
         * Reject overlaps so the result cannot vary with CTU granularity.
         */
        for (j = 0; j < i; j++) {
            if (roi_regions_overlap(&regions[j], &regions[i])) {
                av_log(avctx, AV_LOG_ERROR,
                       "Overlapping ROI regions %d and %d are not "
                       "supported.\n", j, i);
                return AVERROR(EINVAL);
            }
        }

    }

    *nb_regions = count;
    return 0;
}

void ff_rkmppenc_roi_frame_uninit(FFRKMPPEncROIFrame *roi_frame)
{
    const FFRKMPPEncROIOps *ops;

    if (!roi_frame)
        return;

    ops = roi_frame->mpp_ops ? roi_frame->mpp_ops
                             : &ff_rkmppenc_roi_mpp_ops;
    if (roi_frame->mpp_roi)
        ops->deinit(roi_frame->mpp_roi);
    roi_frame->mpp_roi = NULL;
    roi_frame->mpp_ops = NULL;
    roi_frame->width = 0;
    roi_frame->height = 0;
    roi_frame->coding_type = MPP_VIDEO_CodingUnused;
    roi_frame->prepared = 0;
}

static int roi_frame_ensure(AVCodecContext *avctx,
                            FFRKMPPEncROIFrame *roi_frame,
                            int width, int height,
                            MppCodingType coding_type,
                            const FFRKMPPEncROIOps *ops)
{
    MPP_RET mpp_ret;

    if (roi_frame->mpp_roi &&
        (roi_frame->width != width || roi_frame->height != height ||
         roi_frame->coding_type != coding_type ||
         roi_frame->mpp_ops != ops))
        ff_rkmppenc_roi_frame_uninit(roi_frame);

    if (roi_frame->mpp_roi)
        return 0;

    roi_frame->mpp_ops = ops;
    mpp_ret = ops->init(&roi_frame->mpp_roi, width, height, coding_type,
                        FF_RKMPPENC_ROI_MAX_REGIONS);
    if (mpp_ret != MPP_OK || !roi_frame->mpp_roi) {
        av_log(avctx, AV_LOG_ERROR,
               "Failed to initialize MPP ROI context: %d.\n", mpp_ret);
        ff_rkmppenc_roi_frame_uninit(roi_frame);
        return AVERROR_EXTERNAL;
    }

    roi_frame->width = width;
    roi_frame->height = height;
    roi_frame->coding_type = coding_type;
    return 0;
}

int ff_rkmppenc_roi_prepare(AVCodecContext *avctx,
                            FFRKMPPEncROIContext *roi,
                            FFRKMPPEncROIFrame *roi_frame,
                            const AVFrame *frame, MppFrame mpp_frame,
                            MppCodingType coding_type)
{
    RoiRegionCfg regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    const FFRKMPPEncROIOps *ops;
    MppMeta meta;
    int nb_regions;
    int ret;
    int i;

    if (!roi || !roi->initialized || !roi_frame || !frame || !mpp_frame ||
        frame->width <= 0 || frame->height <= 0 ||
        (coding_type != MPP_VIDEO_CodingAVC &&
         coding_type != MPP_VIDEO_CodingHEVC))
        return AVERROR(EINVAL);

    ops = roi_mpp_ops(roi);
    if (!ops->init || !ops->deinit || !ops->add_region || !ops->setup_meta)
        return AVERROR(EINVAL);

    /* encode_put_frame() may return EAGAIN; keep the same metadata snapshot. */
    if (roi_frame->prepared)
        return 0;

    if ((ret = collect_frame_regions(avctx, roi, frame,
                                     regions, &nb_regions)) < 0)
        return ret;

    if (nb_regions > 0)
        roi->metadata_used = 1;
    if (!roi->metadata_used) {
        roi_frame->prepared = 1;
        return 0;
    }

    if ((ret = roi_frame_ensure(avctx, roi_frame,
                                frame->width, frame->height,
                                coding_type, ops)) < 0)
        return ret;

    /* Preserve side-data order when generating the RK3588 ROI2 map. */
    for (i = 0; i < nb_regions; i++) {
        MPP_RET mpp_ret = ops->add_region(roi_frame->mpp_roi, &regions[i]);
        if (mpp_ret != MPP_OK) {
            av_log(avctx, AV_LOG_ERROR,
                   "Failed to add MPP ROI region %d: %d.\n", i, mpp_ret);
            ff_rkmppenc_roi_frame_uninit(roi_frame);
            return AVERROR_EXTERNAL;
        }
    }

    meta = mpp_frame_get_meta(mpp_frame);
    if (!meta) {
        av_log(avctx, AV_LOG_ERROR,
               "Failed to get MPP frame metadata for ROI.\n");
        ff_rkmppenc_roi_frame_uninit(roi_frame);
        return AVERROR_EXTERNAL;
    }

    if (ops->setup_meta(roi_frame->mpp_roi, meta) != MPP_OK) {
        av_log(avctx, AV_LOG_ERROR,
               "Failed to attach MPP ROI metadata to frame.\n");
        ff_rkmppenc_roi_frame_uninit(roi_frame);
        return AVERROR_EXTERNAL;
    }

    av_log(avctx, AV_LOG_DEBUG,
           "RKMPP attached %d AVFrame ROI region(s) to frame metadata%s.\n",
           nb_regions, nb_regions ? "" : " (clear)");
    roi_frame->prepared = 1;

    return 0;
}
