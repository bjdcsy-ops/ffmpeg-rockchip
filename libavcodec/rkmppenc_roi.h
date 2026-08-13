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

#ifndef AVCODEC_RKMPPENC_ROI_H
#define AVCODEC_RKMPPENC_ROI_H

#include <stddef.h>
#include <stdint.h>

#include <rockchip/rk_mpi.h>
#include <rockchip/mpp_enc_roi_utils.h>

#include "libavutil/frame.h"

#include "avcodec.h"

#define FF_RKMPPENC_ROI_MAX_REGIONS 8
#define FF_RKMPPENC_ROI_MAX_PACKET_SIZE 4096
#define FF_RKMPPENC_ROI_MAX_PACKETS_PER_FRAME 256

/**
 * MPP calls used by the ROI bridge.
 *
 * Production RKMPP encoder contexts use ff_rkmppenc_roi_mpp_ops.  The
 * hardware-independent test supplies a memory-only implementation so its
 * result cannot depend on the build host's SoC or allocator device nodes.
 */
typedef struct FFRKMPPEncROIOps {
    MPP_RET (*init)(MppEncRoiCtx *ctx, RK_U32 w, RK_U32 h,
                    MppCodingType type, RK_S32 count);
    MPP_RET (*deinit)(MppEncRoiCtx ctx);
    MPP_RET (*add_region)(MppEncRoiCtx ctx, RoiRegionCfg *region);
    MPP_RET (*setup_meta)(MppEncRoiCtx ctx, MppMeta meta);
} FFRKMPPEncROIOps;

extern const FFRKMPPEncROIOps ff_rkmppenc_roi_mpp_ops;

/**
 * Encoder-lifetime ROI state.
 *
 * Embed this structure in RKMPPEncContext.  socket_path is an AVOption-owned
 * string: ff_rkmppenc_roi_uninit() deliberately does not free it.
 */
typedef struct FFRKMPPEncROIContext {
    char               *socket_path;
    const FFRKMPPEncROIOps *mpp_ops;
    int                 initialized;
    int                 socket_fd;
    uint64_t            socket_dev;
    uint64_t            socket_ino;
    int                 socket_bound;

    AVRegionOfInterest  socket_regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    int                 socket_nb_regions;
    int                 socket_active;
    uint64_t            socket_generation;

    int                 metadata_used;
    int                 regions_warned;
    int                 invalid_packet_warned;
    int                 budget_warned;
    int                 max_packets_per_frame;
} FFRKMPPEncROIContext;

/**
 * Per-asynchronous-frame-slot ROI state.
 *
 * Embed this structure in MPPEncFrame.  MPP ROI2 metadata references buffers
 * owned by mpp_roi, so every in-flight slot must have a separate instance.
 */
typedef struct FFRKMPPEncROIFrame {
    MppEncRoiCtx mpp_roi;
    const FFRKMPPEncROIOps *mpp_ops;
    int          width;
    int          height;
    MppCodingType coding_type;
    int          prepared;
} FFRKMPPEncROIFrame;

int ff_rkmppenc_roi_init(AVCodecContext *avctx,
                         FFRKMPPEncROIContext *roi);
void ff_rkmppenc_roi_uninit(AVCodecContext *avctx,
                            FFRKMPPEncROIContext *roi);

void ff_rkmppenc_roi_frame_uninit(FFRKMPPEncROIFrame *roi_frame);

/**
 * Drain pending socket updates and attach the selected ROI set to mpp_frame.
 *
 * Before the first valid socket update, AV_FRAME_DATA_REGIONS_OF_INTEREST on
 * frame is used.  After that update, the last valid complete socket state is
 * used for the remainder of the encoder lifetime, including an empty state.
 */
int ff_rkmppenc_roi_prepare(AVCodecContext *avctx,
                            FFRKMPPEncROIContext *roi,
                            FFRKMPPEncROIFrame *roi_frame,
                            const AVFrame *frame, MppFrame mpp_frame,
                            MppCodingType coding_type);

/* Internal helpers are exposed for the hardware-independent unit test. */
int ff_rkmppenc_roi_parse_json(AVRegionOfInterest *regions, int *nb_regions,
                               const char *json, size_t json_size);
int ff_rkmppenc_roi_map_region(RoiRegionCfg *mpp_roi,
                               const AVRegionOfInterest *roi,
                               int frame_width, int frame_height);
int ff_rkmppenc_roi_drain(AVCodecContext *avctx,
                          FFRKMPPEncROIContext *roi,
                          int frame_width, int frame_height);

#endif /* AVCODEC_RKMPPENC_ROI_H */
