/*
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

#include <stdio.h>

#include "libavutil/avassert.h"
#include "libavfilter/rkrga_common.c"

static void test_overlay_alpha(void)
{
    static const int alphas[] = { 0, 1, 51, 128, 254 };

    for (int premultiply = 0; premultiply <= 1; premultiply++) {
        for (int i = 0; i < FF_ARRAY_ELEMS(alphas); i++) {
            unsigned int mode = get_overlay_blend_mode(alphas[i], premultiply);

            av_assert0((mode & 0xfff) == 4);
            av_assert0(((mode >> 12) & 1) == premultiply);
            av_assert0(((mode >> 16) & 0xff) == 255);
            av_assert0(((mode >> 24) & 0xff) == alphas[i]);
        }
        av_assert0(get_overlay_blend_mode(255, premultiply) ==
                   (premultiply ? 0x504 : 0x501));
    }
}

static void test_colorspace(void)
{
    static const struct {
        enum AVColorRange range;
        enum AVColorSpace space;
        int mode;
    } cases[] = {
        { AVCOL_RANGE_UNSPECIFIED, AVCOL_SPC_UNSPECIFIED, 1 },
        { AVCOL_RANGE_UNSPECIFIED, AVCOL_SPC_BT470BG,     1 },
        { AVCOL_RANGE_UNSPECIFIED, AVCOL_SPC_BT709,       3 },
        { AVCOL_RANGE_UNSPECIFIED, AVCOL_SPC_BT2020_NCL,  1 },
        { AVCOL_RANGE_MPEG,        AVCOL_SPC_UNSPECIFIED, 1 },
        { AVCOL_RANGE_MPEG,        AVCOL_SPC_BT470BG,     1 },
        { AVCOL_RANGE_MPEG,        AVCOL_SPC_BT709,       3 },
        { AVCOL_RANGE_MPEG,        AVCOL_SPC_BT2020_NCL,  1 },
        { AVCOL_RANGE_JPEG,        AVCOL_SPC_UNSPECIFIED, 2 },
        { AVCOL_RANGE_JPEG,        AVCOL_SPC_BT709,       2 },
    };
    RGAFrameInfo nv12 = {
        .pix_fmt  = AV_PIX_FMT_NV12,
        .pix_desc = av_pix_fmt_desc_get(AV_PIX_FMT_NV12),
    };
    RGAFrameInfo rgba = {
        .pix_fmt  = AV_PIX_FMT_RGBA,
        .pix_desc = av_pix_fmt_desc_get(AV_PIX_FMT_RGBA),
    };
    enum AVColorSpace rgb_space = AVCOL_SPC_UNSPECIFIED;
    enum AVColorRange rgb_range = AVCOL_RANGE_UNSPECIFIED;
    enum AVColorSpace yuv_space = AVCOL_SPC_BT470BG;
    enum AVColorRange yuv_range = AVCOL_RANGE_MPEG;
    int mode = 0;

    for (int i = 0; i < FF_ARRAY_ELEMS(cases); i++) {
        enum AVColorSpace out_space = AVCOL_SPC_UNSPECIFIED;
        enum AVColorRange out_range = AVCOL_RANGE_UNSPECIFIED;
        int out_mode = 0;

        set_colorspace_info(&nv12, cases[i].space, cases[i].range,
                            &rgba, &out_space, &out_range, &out_mode, 0);
        av_assert0(out_mode == cases[i].mode);
        av_assert0(out_space == AVCOL_SPC_RGB);
        av_assert0(out_range == AVCOL_RANGE_JPEG);
    }

    set_colorspace_info(&nv12, AVCOL_SPC_UNSPECIFIED, AVCOL_RANGE_UNSPECIFIED,
                        &rgba, &rgb_space, &rgb_range, &mode, 0);
    set_colorspace_info(&rgba, rgb_space, rgb_range,
                        &nv12, &yuv_space, &yuv_range, &mode, 0);
    av_assert0(mode == 9);
    av_assert0(yuv_space == AVCOL_SPC_BT470BG);
    av_assert0(yuv_range == AVCOL_RANGE_MPEG);
}

int main(void)
{
    test_overlay_alpha();
    test_colorspace();
    printf("RKRGA alpha and colorspace checks passed.\n");
    return 0;
}
