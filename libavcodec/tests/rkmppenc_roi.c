/*
 * Rockchip MPP encoder region-of-interest tests
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

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "libavutil/error.h"
#include "libavutil/log.h"
#include "libavutil/rational.h"

#include "libavcodec/rkmppenc_roi.h"

static int test_mapping(void)
{
    AVRegionOfInterest roi = {
        .self_size = sizeof(roi),
        .top       = 16,
        .bottom    = 240,
        .left      = 32,
        .right     = 320,
        .qoffset   = { -1, 2 },
    };
    RoiRegionCfg mapped;

    if (ff_rkmppenc_roi_map_region(&mapped, &roi, 1920, 1080) < 0 ||
        mapped.x != 32 || mapped.y != 16 ||
        mapped.w != 288 || mapped.h != 224 ||
        mapped.force_intra || mapped.qp_mode || mapped.qp_val != -26) {
        fprintf(stderr, "ROI mapping failed\n");
        return 1;
    }

    roi.qoffset = (AVRational) { 1, 2 };
    if (ff_rkmppenc_roi_map_region(&mapped, &roi, 1920, 1080) < 0 ||
        mapped.qp_val != 26) {
        fprintf(stderr, "positive ROI rounding failed\n");
        return 1;
    }
    roi.qoffset = (AVRational) { -1, 102 };
    if (ff_rkmppenc_roi_map_region(&mapped, &roi, 1920, 1080) < 0 ||
        mapped.qp_val != -1) {
        fprintf(stderr, "half-away-from-zero ROI rounding failed\n");
        return 1;
    }

#define EXPECT_INVALID_MAP() do {                                      \
        if (ff_rkmppenc_roi_map_region(&mapped, &roi, 1920, 1080) >= 0) { \
            fprintf(stderr, "invalid ROI mapping was accepted\n");     \
            return 1;                                                   \
        }                                                               \
    } while (0)

    roi.qoffset = (AVRational) { 2, 1 };
    EXPECT_INVALID_MAP();
    roi.qoffset = (AVRational) { 1, 0 };
    EXPECT_INVALID_MAP();
    roi.qoffset = (AVRational) { 1, -2 };
    EXPECT_INVALID_MAP();
    roi.qoffset = (AVRational) { 0, 1 };
    roi.left = -1;
    EXPECT_INVALID_MAP();
    roi.left = 32;
    roi.right = roi.left;
    EXPECT_INVALID_MAP();
    roi.right = 1921;
    EXPECT_INVALID_MAP();

#undef EXPECT_INVALID_MAP

    roi.right = 320;
    if (ff_rkmppenc_roi_map_region(&mapped, &roi, UINT16_MAX + 1, 1080) >= 0 ||
        ff_rkmppenc_roi_map_region(&mapped, &roi, 1920, UINT16_MAX + 1) >= 0) {
        fprintf(stderr, "unsupported frame dimension was accepted\n");
        return 1;
    }

    return 0;
}

static AVFrame *make_frame(int width, int height, int nb_regions,
                           int qp_num, int qp_den)
{
    AVFrame *frame = av_frame_alloc();
    AVFrameSideData *sd;
    AVRegionOfInterest *regions;
    int i;

    if (!frame)
        return NULL;
    frame->width = width;
    frame->height = height;
    if (!nb_regions)
        return frame;

    sd = av_frame_new_side_data(frame, AV_FRAME_DATA_REGIONS_OF_INTEREST,
                                nb_regions * sizeof(*regions));
    if (!sd) {
        av_frame_free(&frame);
        return NULL;
    }
    regions = (AVRegionOfInterest *)sd->data;
    for (i = 0; i < nb_regions; i++) {
        regions[i] = (AVRegionOfInterest) {
            .self_size = sizeof(*regions),
            .top       = 16,
            .bottom    = 128,
            .left      = 32 + i * 128,
            .right     = 96 + i * 128,
            .qoffset   = { qp_num + i, qp_den },
        };
    }
    return frame;
}

static AVRegionOfInterest *frame_regions(AVFrame *frame)
{
    AVFrameSideData *sd = av_frame_get_side_data(
        frame, AV_FRAME_DATA_REGIONS_OF_INTEREST);

    return sd ? (AVRegionOfInterest *)sd->data : NULL;
}

typedef struct FakeROIMeta {
    RoiRegionCfg regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    int number;
} FakeROIMeta;

typedef struct FakeROICtx {
    FakeROIMeta meta;
    MppEncROICfg legacy_cfg;
    MppEncROIRegion legacy_regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    int nb_regions;
    int add_calls;
    int setup_calls;
} FakeROICtx;

static MPP_RET fake_roi_init(MppEncRoiCtx *ctx, RK_U32 width, RK_U32 height,
                             MppCodingType coding_type, RK_S32 max_regions)
{
    FakeROICtx *fake;

    if (!ctx || !width || !height ||
        max_regions != FF_RKMPPENC_ROI_MAX_REGIONS ||
        (coding_type != MPP_VIDEO_CodingAVC &&
         coding_type != MPP_VIDEO_CodingHEVC))
        return MPP_ERR_VALUE;

    fake = calloc(1, sizeof(*fake));
    if (!fake)
        return MPP_ERR_MALLOC;
    *ctx = fake;
    return MPP_OK;
}

static MPP_RET fake_roi_deinit(MppEncRoiCtx ctx)
{
    free(ctx);
    return MPP_OK;
}

static MPP_RET fake_roi_add_region(MppEncRoiCtx ctx, RoiRegionCfg *region)
{
    FakeROICtx *fake = ctx;

    if (!fake || !region)
        return MPP_ERR_NULL_PTR;
    if (fake->nb_regions >= FF_RKMPPENC_ROI_MAX_REGIONS)
        return MPP_ERR_VALUE;

    fake->meta.regions[fake->nb_regions++] = *region;
    fake->add_calls++;
    return MPP_OK;
}

static MPP_RET fake_roi_setup_meta(MppEncRoiCtx ctx, MppMeta meta)
{
    FakeROICtx *fake = ctx;

    if (!fake || !meta)
        return MPP_ERR_NULL_PTR;

    fake->meta.number = fake->nb_regions;
    fake->nb_regions = 0;
    fake->setup_calls++;
    return mpp_meta_set_ptr(meta, KEY_ROI_DATA2, &fake->meta);
}

static const FFRKMPPEncROIOps fake_roi_ops = {
    .init       = fake_roi_init,
    .deinit     = fake_roi_deinit,
    .add_region = fake_roi_add_region,
    .setup_meta = fake_roi_setup_meta,
};

static MPP_RET fake_legacy_setup_meta(MppEncRoiCtx ctx, MppMeta meta)
{
    FakeROICtx *fake = ctx;
    int i;

    if (!fake || !meta)
        return MPP_ERR_NULL_PTR;
    for (i = 0; i < fake->nb_regions; i++) {
        const RoiRegionCfg *src = &fake->meta.regions[i];

        fake->legacy_regions[i] = (MppEncROIRegion) {
            .x = src->x, .y = src->y, .w = src->w, .h = src->h,
            .intra = src->force_intra, .quality = src->qp_val,
            .qp_area_idx = 0, .area_map_en = 1, .abs_qp_en = src->qp_mode,
        };
    }
    fake->legacy_cfg.number = fake->nb_regions;
    fake->legacy_cfg.regions = fake->legacy_regions;
    fake->nb_regions = 0;
    fake->setup_calls++;
    return mpp_meta_set_ptr(meta, KEY_ROI_DATA, &fake->legacy_cfg);
}

static int get_fake_cfg(MppFrame frame, FakeROIMeta **cfg)
{
    void *ptr = NULL;
    MppMeta meta = mpp_frame_get_meta(frame);

    if (!meta || mpp_meta_get_ptr(meta, KEY_ROI_DATA2, &ptr) != MPP_OK || !ptr)
        return AVERROR_EXTERNAL;
    *cfg = ptr;
    return 0;
}

static int test_side_data_validation(void)
{
    FFRKMPPEncROIContext roi = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIFrame bad_slot = { 0 };
    FFRKMPPEncROIFrame overlap_slot = { 0 };
    FFRKMPPEncROIFrame cap_slot = { 0 };
    AVFrame *bad = NULL;
    AVFrame *overlap = NULL;
    AVFrame *cap = NULL;
    AVRegionOfInterest *regions;
    MppFrame mpp_bad = NULL;
    MppFrame mpp_overlap = NULL;
    MppFrame mpp_cap = NULL;
    FakeROIMeta *cap_cfg = NULL;
    int ret = 1;

    bad = make_frame(1920, 1080, 1, -1, 2);
    overlap = make_frame(1920, 1080, 2, -1, 2);
    cap = make_frame(1920, 1080,
                     FF_RKMPPENC_ROI_MAX_REGIONS + 2, 0, 10);
    if (!bad || !overlap || !cap ||
        mpp_frame_init(&mpp_bad) != MPP_OK ||
        mpp_frame_init(&mpp_overlap) != MPP_OK ||
        mpp_frame_init(&mpp_cap) != MPP_OK ||
        ff_rkmppenc_roi_init(NULL, &roi) < 0)
        goto end;

    regions = frame_regions(bad);
    if (!regions)
        goto end;
    regions[0].self_size = sizeof(regions[0]) - 1;
    if (ff_rkmppenc_roi_prepare(NULL, &roi, &bad_slot, bad, mpp_bad,
                                MPP_VIDEO_CodingAVC) >= 0 ||
        bad_slot.prepared || bad_slot.mpp_roi || roi.metadata_used) {
        fprintf(stderr, "malformed ROI self_size was accepted\n");
        goto end;
    }

    regions = frame_regions(overlap);
    if (!regions)
        goto end;
    regions[1].left = 64;
    regions[1].right = 128;
    if (ff_rkmppenc_roi_prepare(NULL, &roi, &overlap_slot,
                                overlap, mpp_overlap,
                                MPP_VIDEO_CodingAVC) >= 0 ||
        overlap_slot.prepared || overlap_slot.mpp_roi || roi.metadata_used) {
        fprintf(stderr, "overlapping ROI side data was accepted\n");
        goto end;
    }

    if (ff_rkmppenc_roi_prepare(NULL, &roi, &cap_slot, cap, mpp_cap,
                                MPP_VIDEO_CodingHEVC) < 0 ||
        get_fake_cfg(mpp_cap, &cap_cfg) < 0 ||
        cap_cfg->number != FF_RKMPPENC_ROI_MAX_REGIONS ||
        cap_cfg->regions[FF_RKMPPENC_ROI_MAX_REGIONS - 1].x !=
            32 + (FF_RKMPPENC_ROI_MAX_REGIONS - 1) * 128 ||
        !roi.regions_warned) {
        fprintf(stderr, "ROI side-data cap did not keep the first eight regions\n");
        goto end;
    }

    ret = 0;

end:
    ff_rkmppenc_roi_frame_uninit(&bad_slot);
    ff_rkmppenc_roi_frame_uninit(&overlap_slot);
    ff_rkmppenc_roi_frame_uninit(&cap_slot);
    ff_rkmppenc_roi_uninit(NULL, &roi);
    if (mpp_bad)
        mpp_frame_deinit(&mpp_bad);
    if (mpp_overlap)
        mpp_frame_deinit(&mpp_overlap);
    if (mpp_cap)
        mpp_frame_deinit(&mpp_cap);
    av_frame_free(&bad);
    av_frame_free(&overlap);
    av_frame_free(&cap);
    return ret;
}

static int test_metadata(void)
{
    FFRKMPPEncROIContext roi = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIContext unused = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIFrame slot1 = { 0 };
    FFRKMPPEncROIFrame slot2 = { 0 };
    FFRKMPPEncROIFrame unused_slot = { 0 };
    AVFrame *frame1 = NULL;
    AVFrame *frame2 = NULL;
    AVFrame *clear = NULL;
    AVFrame *plain = NULL;
    MppFrame mpp1 = NULL;
    MppFrame mpp2 = NULL;
    MppFrame mpp_clear = NULL;
    MppFrame mpp_plain = NULL;
    FakeROIMeta *cfg1 = NULL;
    FakeROIMeta *cfg2 = NULL;
    FakeROIMeta *cfg_clear = NULL;
    FakeROICtx *fake1;
    int ret = 1;

    frame1 = make_frame(1920, 1080, 2, -1, 2);
    frame2 = make_frame(1920, 1080, 1, 1, 2);
    clear = make_frame(1920, 1080, 0, 0, 1);
    plain = make_frame(640, 480, 0, 0, 1);
    if (!frame1 || !frame2 || !clear || !plain ||
        mpp_frame_init(&mpp1) != MPP_OK ||
        mpp_frame_init(&mpp2) != MPP_OK ||
        mpp_frame_init(&mpp_clear) != MPP_OK ||
        mpp_frame_init(&mpp_plain) != MPP_OK)
        goto end;

    if (ff_rkmppenc_roi_init(NULL, &unused) < 0 ||
        ff_rkmppenc_roi_prepare(NULL, &unused, &unused_slot,
                                plain, mpp_plain,
                                MPP_VIDEO_CodingAVC) < 0 ||
        unused.metadata_used || unused_slot.mpp_roi ||
        !unused_slot.prepared || mpp_frame_has_meta(mpp_plain)) {
        fprintf(stderr, "ROI metadata was created before ROI was used\n");
        goto end;
    }

    if (ff_rkmppenc_roi_init(NULL, &roi) < 0 ||
        ff_rkmppenc_roi_prepare(NULL, &roi, &slot1, frame1, mpp1,
                                MPP_VIDEO_CodingAVC) < 0 ||
        !roi.metadata_used ||
        get_fake_cfg(mpp1, &cfg1) < 0 || cfg1->number != 2 ||
        cfg1->regions[0].x != 32 || cfg1->regions[0].qp_val != -26 ||
        cfg1->regions[1].x != 160 || cfg1->regions[1].qp_val != 0) {
        fprintf(stderr, "static ROI metadata mapping or order failed\n");
        goto end;
    }

    fake1 = slot1.mpp_roi;
    if (!fake1 || fake1->add_calls != 2 || fake1->setup_calls != 1 ||
        ff_rkmppenc_roi_prepare(NULL, &roi, &slot1, frame2, mpp1,
                                MPP_VIDEO_CodingAVC) < 0 ||
        fake1->add_calls != 2 || fake1->setup_calls != 1 ||
        cfg1->number != 2) {
        fprintf(stderr, "prepared ROI slot was rebuilt during retry\n");
        goto end;
    }

    if (ff_rkmppenc_roi_prepare(NULL, &roi, &slot2, frame2, mpp2,
                                MPP_VIDEO_CodingAVC) < 0 ||
        get_fake_cfg(mpp2, &cfg2) < 0 || cfg2->number != 1 ||
        cfg2->regions[0].qp_val != 26 || cfg1 == cfg2 ||
        cfg1->number != 2 || cfg1->regions[0].qp_val != -26) {
        fprintf(stderr, "per-slot ROI metadata storage is not independent\n");
        goto end;
    }

    slot1.prepared = 0;
    if (ff_rkmppenc_roi_prepare(NULL, &roi, &slot1, clear, mpp_clear,
                                MPP_VIDEO_CodingAVC) < 0 ||
        get_fake_cfg(mpp_clear, &cfg_clear) < 0 ||
        cfg_clear != cfg1 || cfg_clear->number != 0 ||
        fake1->add_calls != 2 || fake1->setup_calls != 2 ||
        cfg2->number != 1) {
        fprintf(stderr, "missing side data did not explicitly clear ROI\n");
        goto end;
    }

    ret = 0;

end:
    ff_rkmppenc_roi_frame_uninit(&slot1);
    ff_rkmppenc_roi_frame_uninit(&slot2);
    ff_rkmppenc_roi_frame_uninit(&unused_slot);
    ff_rkmppenc_roi_uninit(NULL, &roi);
    ff_rkmppenc_roi_uninit(NULL, &unused);
    if (mpp1)
        mpp_frame_deinit(&mpp1);
    if (mpp2)
        mpp_frame_deinit(&mpp2);
    if (mpp_clear)
        mpp_frame_deinit(&mpp_clear);
    if (mpp_plain)
        mpp_frame_deinit(&mpp_plain);
    av_frame_free(&frame1);
    av_frame_free(&frame2);
    av_frame_free(&clear);
    av_frame_free(&plain);
    return ret;
}

static int test_legacy_metadata(void)
{
    FFRKMPPEncROIOps ops = fake_roi_ops;
    FFRKMPPEncROIContext roi = { .mpp_ops = &ops };
    FFRKMPPEncROIFrame slots[2] = { { 0 } };
    AVFrame *frames[2] = { make_frame(1920, 1080, 2, -1, 2),
                           make_frame(1920, 1080, 1, 1, 2) };
    AVFrame *clear = make_frame(1920, 1080, 0, 0, 1);
    MppFrame mpp_frames[2] = { NULL, NULL };
    MppFrame mpp_clear = NULL;
    MppEncROICfg *configs[2] = { NULL, NULL };
    void *ptr = NULL;
    int ret = 1, i;

    ops.setup_meta = fake_legacy_setup_meta;
    if (!frames[0] || !frames[1] || !clear ||
        ff_rkmppenc_roi_init(NULL, &roi) < 0 ||
        mpp_frame_init(&mpp_clear) != MPP_OK)
        goto end;
    for (i = 0; i < 2; i++) {
        if (mpp_frame_init(&mpp_frames[i]) != MPP_OK ||
            ff_rkmppenc_roi_prepare(NULL, &roi, &slots[i], frames[i],
                                    mpp_frames[i], MPP_VIDEO_CodingHEVC) < 0 ||
            mpp_meta_get_ptr(mpp_frame_get_meta(mpp_frames[i]), KEY_ROI_DATA,
                              &ptr) != MPP_OK || !ptr)
            goto end;
        configs[i] = ptr;
    }
    if (configs[0] == configs[1] || configs[0]->regions == configs[1]->regions ||
        configs[0]->number != 2 || configs[0]->regions[0].x != 32 ||
        configs[0]->regions[0].quality != -26 ||
        configs[0]->regions[0].abs_qp_en || !configs[0]->regions[0].area_map_en ||
        configs[1]->number != 1 || configs[1]->regions[0].quality != 26) {
        fprintf(stderr, "legacy ROI metadata mapping or slot ownership failed\n");
        goto end;
    }
    slots[0].prepared = 0;
    if (ff_rkmppenc_roi_prepare(NULL, &roi, &slots[0], clear, mpp_clear,
                                MPP_VIDEO_CodingHEVC) < 0 ||
        mpp_meta_get_ptr(mpp_frame_get_meta(mpp_clear), KEY_ROI_DATA,
                          &ptr) != MPP_OK || ptr != configs[0] ||
        configs[0]->number || configs[1]->number != 1 ||
        configs[1]->regions[0].quality != 26) {
        fprintf(stderr, "legacy ROI clear leaked across input slots\n");
        goto end;
    }
    ret = 0;

end:
    for (i = 0; i < 2; i++) {
        ff_rkmppenc_roi_frame_uninit(&slots[i]);
        if (mpp_frames[i])
            mpp_frame_deinit(&mpp_frames[i]);
        av_frame_free(&frames[i]);
    }
    if (mpp_clear)
        mpp_frame_deinit(&mpp_clear);
    av_frame_free(&clear);
    ff_rkmppenc_roi_uninit(NULL, &roi);
    return ret;
}

int main(void)
{
    int ret = 0;

    av_log_set_level(AV_LOG_QUIET);
    ret |= test_mapping();
    ret |= test_side_data_validation();
    ret |= test_metadata();
    ret |= test_legacy_metadata();
    return ret;
}
