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

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "libavutil/error.h"
#include "libavutil/log.h"
#include "libavutil/rational.h"

#include "libavcodec/rkmppenc_roi.h"

static int check_invalid_json(const char *json, size_t json_size)
{
    AVRegionOfInterest regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    AVRegionOfInterest original[FF_RKMPPENC_ROI_MAX_REGIONS];
    int nb_regions = 7;

    memset(regions, 0x5a, sizeof(regions));
    memcpy(original, regions, sizeof(original));

    if (ff_rkmppenc_roi_parse_json(regions, &nb_regions,
                                   json, json_size) >= 0) {
        fprintf(stderr, "invalid ROI JSON accepted (%zu bytes)\n", json_size);
        return 1;
    }
    if (nb_regions != 7 || memcmp(regions, original, sizeof(regions))) {
        fprintf(stderr, "invalid ROI JSON changed active state\n");
        return 1;
    }

    return 0;
}

#define CHECK_INVALID_LITERAL(value) \
    check_invalid_json(value, sizeof(value) - 1)

static int test_json(void)
{
    static const char valid[] =
        " { \"regions\" : ["
        "{\"qoffset\":-5e-1,\"h\":240,\"w\":320,\"y\":32,\"x\":16},"
        "{\"x\":640,\"y\":10,\"w\":64,\"h\":32,\"qoffset\":0.25}"
        "], \"version\" : 1 } ";
    static const char eight[] =
        "{\"version\":1,\"regions\":["
        "{\"x\":0,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":16,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":32,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":48,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":64,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":80,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":96,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0},"
        "{\"x\":112,\"y\":0,\"w\":16,\"h\":16,\"qoffset\":0}]}";
    static const char embedded_nul[] =
        "{\"version\":1,\"regions\":[]}\0junk";
    static const char *const invalid[] = {
        "{}",
        "[]",
        "{\"version\":1}",
        "{\"regions\":[]}",
        "{\"version\":2,\"regions\":[]}",
        "{\"version\":1.0,\"regions\":[]}",
        "{\"version\":1,\"version\":1,\"regions\":[]}",
        "{\"version\":1,\"regions\":[],\"regions\":[]}",
        "{\"version\":1,\"regions\":[],\"unknown\":0}",
        "{\"version\":1,\"regions\":[],}",
        "{\"version\":1,\"regions\":[]} trailing",
        "{\"version\":01,\"regions\":[]}",
        "{\"version\":2147483648,\"regions\":[]}",
        "{\"version\":1,\"regions\":[,]}",
        "{\"version\":1,\"regions\":[{}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0,}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0,\"z\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":0.0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":-1,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":0,\"h\":1,\"qoffset\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":2147483647,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1.01}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1.00000000000000000001}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":-1.01}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":\"0\"}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":NaN}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1e999}]}",
        "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1e}]}",
        "{\"version\":1,\"regions\":["
        "{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":1,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":2,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":3,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":4,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":5,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":6,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":7,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0},"
        "{\"x\":8,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0}]}"
    };
    AVRegionOfInterest regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    char packet[FF_RKMPPENC_ROI_MAX_PACKET_SIZE + 1];
    int nb_regions = -1;
    size_t i;
    size_t len;

    if (ff_rkmppenc_roi_parse_json(regions, &nb_regions,
                                   valid, sizeof(valid) - 1) < 0 ||
        nb_regions != 2 ||
        regions[0].left != 16 || regions[0].top != 32 ||
        regions[0].right != 336 || regions[0].bottom != 272 ||
        av_cmp_q(regions[0].qoffset, (AVRational) { -1, 2 }) ||
        regions[1].left != 640 || regions[1].top != 10 ||
        av_cmp_q(regions[1].qoffset, (AVRational) { 1, 4 })) {
        fprintf(stderr, "valid ROI JSON did not map as expected\n");
        return 1;
    }

    if (ff_rkmppenc_roi_parse_json(
            regions, &nb_regions,
            "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0.019607843137}]}",
            sizeof("{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":0.019607843137}]}") - 1) < 0 ||
        nb_regions != 1 ||
        av_cmp_q(regions[0].qoffset, (AVRational) { 1, 51 })) {
        fprintf(stderr, "long decimal ROI JSON lost its scale\n");
        return 1;
    }

    if (ff_rkmppenc_roi_parse_json(
            regions, &nb_regions,
            "{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1e-1}]}",
            sizeof("{\"version\":1,\"regions\":[{\"x\":0,\"y\":0,\"w\":1,\"h\":1,\"qoffset\":1e-1}]}") - 1) < 0 ||
        nb_regions != 1 ||
        av_cmp_q(regions[0].qoffset, (AVRational) { 1, 10 })) {
        fprintf(stderr, "exponent ROI JSON lost its scale\n");
        return 1;
    }

    if (ff_rkmppenc_roi_parse_json(regions, &nb_regions,
                                   "{\"regions\":[],\"version\":1}",
                                   sizeof("{\"regions\":[],\"version\":1}") - 1) < 0 ||
        nb_regions != 0) {
        fprintf(stderr, "empty ROI update was not accepted\n");
        return 1;
    }

    if (ff_rkmppenc_roi_parse_json(regions, &nb_regions,
                                   eight, sizeof(eight) - 1) < 0 ||
        nb_regions != FF_RKMPPENC_ROI_MAX_REGIONS) {
        fprintf(stderr, "eight-region ROI update was not accepted\n");
        return 1;
    }

    for (i = 0; i < FF_ARRAY_ELEMS(invalid); i++) {
        if (check_invalid_json(invalid[i], strlen(invalid[i])))
            return 1;
    }
    if (CHECK_INVALID_LITERAL(embedded_nul) || check_invalid_json("", 0))
        return 1;

    len = sizeof("{\"version\":1,\"regions\":[]}") - 1;
    memcpy(packet, "{\"version\":1,\"regions\":[]}", len);
    memset(packet + len, ' ', FF_RKMPPENC_ROI_MAX_PACKET_SIZE - len);
    if (ff_rkmppenc_roi_parse_json(regions, &nb_regions, packet,
                                   FF_RKMPPENC_ROI_MAX_PACKET_SIZE) < 0 ||
        nb_regions != 0) {
        fprintf(stderr, "4096-byte ROI update was not accepted\n");
        return 1;
    }
    packet[FF_RKMPPENC_ROI_MAX_PACKET_SIZE] = ' ';
    if (check_invalid_json(packet, sizeof(packet)))
        return 1;

    return 0;
}

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

static int send_packet(const char *path, const void *data, size_t size)
{
    struct sockaddr_un addr = { 0 };
    size_t path_len = strlen(path);
    socklen_t addr_len;
    int fd;
    ssize_t sent;

    if (path_len >= sizeof(addr.sun_path))
        return AVERROR(ENAMETOOLONG);

    fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0)
        return AVERROR(errno);

    addr.sun_family = AF_UNIX;
    memcpy(addr.sun_path, path, path_len + 1);
    addr_len = offsetof(struct sockaddr_un, sun_path) + path_len + 1;
    sent = sendto(fd, data, size, 0,
                  (const struct sockaddr *)&addr, addr_len);
    close(fd);

    if (sent < 0)
        return AVERROR(errno);
    return sent == size ? 0 : AVERROR(EIO);
}

static int test_socket(void)
{
    static const char first[] =
        "{\"version\":1,\"regions\":["
        "{\"x\":10,\"y\":20,\"w\":100,\"h\":80,\"qoffset\":-0.5}]}";
    static const char second[] =
        "{\"version\":1,\"regions\":["
        "{\"x\":30,\"y\":40,\"w\":120,\"h\":90,\"qoffset\":0.25}]}";
    static const char outside[] =
        "{\"version\":1,\"regions\":["
        "{\"x\":1900,\"y\":0,\"w\":30,\"h\":16,\"qoffset\":0}]}";
    static const char overlap[] =
        "{\"version\":1,\"regions\":["
        "{\"x\":0,\"y\":0,\"w\":64,\"h\":64,\"qoffset\":-0.5},"
        "{\"x\":32,\"y\":32,\"w\":64,\"h\":64,\"qoffset\":0.5}]}";
    static const char empty[] = "{\"version\":1,\"regions\":[]}";
    char directory[] = "/tmp/ffmpeg-rkmpp-roi-XXXXXX";
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    char moved[sizeof(path)];
    char occupied[sizeof(path)];
    char normal[sizeof(path)];
    char exact[FF_RKMPPENC_ROI_MAX_PACKET_SIZE];
    char oversized[5000];
    FFRKMPPEncROIContext roi = { 0 };
    FFRKMPPEncROIContext other = { 0 };
    FFRKMPPEncROIContext unopened = { 0 };
    struct stat st;
    int pipefd[2] = { -1, -1 };
    int replacement_fd = -1;
    int ret = 1;
    int flags;
    size_t len;

    {
        int directory_fd = mkstemp(directory);

        if (directory_fd < 0) {
            fprintf(stderr, "mkstemp failed: %s\n",
                    av_err2str(AVERROR(errno)));
            return 1;
        }
        close(directory_fd);
        if (unlink(directory) < 0 || mkdir(directory, 0700) < 0) {
            fprintf(stderr, "temporary directory creation failed: %s\n",
                    av_err2str(AVERROR(errno)));
            return 1;
        }
    }
    snprintf(path, sizeof(path), "%s/roi.sock", directory);
    snprintf(moved, sizeof(moved), "%s/moved.sock", directory);
    snprintf(occupied, sizeof(occupied), "%s/occupied", directory);
    snprintf(normal, sizeof(normal), "%s/normal.sock", directory);

    if (pipe(pipefd) < 0)
        goto end;
    unopened.socket_fd = pipefd[0];
    ff_rkmppenc_roi_uninit(NULL, &unopened);
    if (fcntl(pipefd[0], F_GETFD) < 0) {
        fprintf(stderr, "uninitialized ROI context closed an unrelated fd\n");
        goto end;
    }

    replacement_fd = open(occupied, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (replacement_fd < 0)
        goto end;
    close(replacement_fd);
    replacement_fd = -1;
    other.socket_path = occupied;
    if (ff_rkmppenc_roi_init(NULL, &other) >= 0) {
        fprintf(stderr, "existing ROI path did not make bind fail\n");
        goto end;
    }
    ff_rkmppenc_roi_uninit(NULL, &other);
    if (lstat(occupied, &st) < 0 || !S_ISREG(st.st_mode)) {
        fprintf(stderr, "failed bind removed an existing path\n");
        goto end;
    }
    unlink(occupied);

    roi.socket_path = path;
    if (ff_rkmppenc_roi_init(NULL, &roi) < 0)
        goto end;
    flags = fcntl(roi.socket_fd, F_GETFL);
    if (flags < 0 || !(flags & O_NONBLOCK) ||
        (fcntl(roi.socket_fd, F_GETFD) & FD_CLOEXEC) == 0 ||
        lstat(path, &st) < 0 || !S_ISSOCK(st.st_mode)) {
        fprintf(stderr, "ROI socket flags or path type are wrong\n");
        goto end;
    }

    other = (FFRKMPPEncROIContext) { .socket_path = path };
    if (ff_rkmppenc_roi_init(NULL, &other) >= 0) {
        fprintf(stderr, "duplicate ROI socket bind succeeded\n");
        goto end;
    }
    ff_rkmppenc_roi_uninit(NULL, &other);
    if (fcntl(roi.socket_fd, F_GETFD) < 0 || lstat(path, &st) < 0) {
        fprintf(stderr, "duplicate bind damaged the original socket\n");
        goto end;
    }

    if (send_packet(path, first, sizeof(first) - 1) < 0 ||
        send_packet(path, "{", 1) < 0 ||
        send_packet(path, second, sizeof(second) - 1) < 0 ||
        ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0)
        goto end;
    if (!roi.socket_active || roi.socket_generation != 2 ||
        roi.socket_nb_regions != 1 ||
        roi.socket_regions[0].left != 30 ||
        av_cmp_q(roi.socket_regions[0].qoffset,
                 (AVRational) { 1, 4 })) {
        fprintf(stderr, "latest valid ROI packet did not win\n");
        goto end;
    }

    if (send_packet(path, outside, sizeof(outside) - 1) < 0 ||
        send_packet(path, overlap, sizeof(overlap) - 1) < 0 ||
        send_packet(path, "bad", 3) < 0 ||
        ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0)
        goto end;
    if (roi.socket_generation != 2 || roi.socket_regions[0].left != 30) {
        fprintf(stderr, "invalid ROI packet changed active state\n");
        goto end;
    }

    if (send_packet(path, empty, sizeof(empty) - 1) < 0 ||
        ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0 ||
        !roi.socket_active || roi.socket_generation != 3 ||
        roi.socket_nb_regions != 0) {
        fprintf(stderr, "empty ROI packet did not clear active state\n");
        goto end;
    }

    len = sizeof(empty) - 1;
    memcpy(exact, empty, len);
    memset(exact + len, ' ', sizeof(exact) - len);
    memcpy(oversized, empty, len);
    memset(oversized + len, ' ', sizeof(oversized) - len);
    if (send_packet(path, "", 0) < 0 ||
        send_packet(path, oversized, FF_RKMPPENC_ROI_MAX_PACKET_SIZE + 1) < 0 ||
        send_packet(path, oversized, sizeof(oversized)) < 0 ||
        ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0 ||
        roi.socket_generation != 3) {
        fprintf(stderr, "empty or oversized packet changed active state\n");
        goto end;
    }
    if (send_packet(path, exact, sizeof(exact)) < 0 ||
        ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0 ||
        roi.socket_generation != 4 || roi.socket_nb_regions != 0) {
        fprintf(stderr, "4096-byte socket packet was not accepted\n");
        goto end;
    }

    roi.max_packets_per_frame = 4;
    for (int i = 0; i < roi.max_packets_per_frame + 1; i++) {
        if (send_packet(path, first, sizeof(first) - 1) < 0)
            goto end;
    }
    if (ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0 ||
        roi.socket_generation != 4 + roi.max_packets_per_frame) {
        fprintf(stderr, "ROI socket drain budget was not enforced\n");
        goto end;
    }
    if (ff_rkmppenc_roi_drain(NULL, &roi, 1920, 1080) < 0 ||
        roi.socket_generation != 5 + roi.max_packets_per_frame) {
        fprintf(stderr, "deferred ROI socket packet was not drained\n");
        goto end;
    }

    if (rename(path, moved) < 0)
        goto end;
    replacement_fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (replacement_fd < 0)
        goto end;
    close(replacement_fd);
    replacement_fd = -1;
    ff_rkmppenc_roi_uninit(NULL, &roi);
    if (lstat(path, &st) < 0 || !S_ISREG(st.st_mode) ||
        lstat(moved, &st) < 0 || !S_ISSOCK(st.st_mode)) {
        fprintf(stderr, "ROI shutdown removed a replacement path\n");
        goto end;
    }
    unlink(path);
    unlink(moved);

    other = (FFRKMPPEncROIContext) { .socket_path = normal };
    if (ff_rkmppenc_roi_init(NULL, &other) < 0)
        goto end;
    ff_rkmppenc_roi_uninit(NULL, &other);
    if (lstat(normal, &st) == 0 || errno != ENOENT) {
        fprintf(stderr, "normal ROI shutdown did not remove its socket\n");
        goto end;
    }

    ret = 0;

end:
    if (replacement_fd >= 0)
        close(replacement_fd);
    ff_rkmppenc_roi_uninit(NULL, &roi);
    ff_rkmppenc_roi_uninit(NULL, &other);
    if (pipefd[0] >= 0)
        close(pipefd[0]);
    if (pipefd[1] >= 0)
        close(pipefd[1]);
    unlink(path);
    unlink(moved);
    unlink(occupied);
    unlink(normal);
    rmdir(directory);
    return ret;
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
            .left      = 32 + i * 320,
            .right     = 320 + i * 320,
            .qoffset   = { qp_num + i, qp_den },
        };
    }
    return frame;
}

typedef struct FakeROICtx {
    RoiRegionCfg regions[FF_RKMPPENC_ROI_MAX_REGIONS];
    MppEncROIRegion mapped[FF_RKMPPENC_ROI_MAX_REGIONS];
    MppEncROICfg cfg;
    int nb_regions;
} FakeROICtx;

static MPP_RET fake_roi_init(MppEncRoiCtx *ctx, RK_U32 width, RK_U32 height,
                             MppCodingType coding_type, RK_S32 max_regions)
{
    FakeROICtx *fake;

    if (!ctx || !width || !height || max_regions != FF_RKMPPENC_ROI_MAX_REGIONS ||
        (coding_type != MPP_VIDEO_CodingAVC &&
         coding_type != MPP_VIDEO_CodingHEVC))
        return MPP_ERR_VALUE;

    fake = calloc(1, sizeof(*fake));
    if (!fake)
        return MPP_ERR_MALLOC;
    fake->cfg.regions = fake->mapped;
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

    fake->regions[fake->nb_regions++] = *region;
    return MPP_OK;
}

static MPP_RET fake_roi_setup_meta(MppEncRoiCtx ctx, MppMeta meta)
{
    FakeROICtx *fake = ctx;
    int i;

    if (!fake || !meta)
        return MPP_ERR_NULL_PTR;

    for (i = 0; i < fake->nb_regions; i++) {
        const RoiRegionCfg *src = &fake->regions[i];
        MppEncROIRegion *dst = &fake->mapped[i];

        *dst = (MppEncROIRegion) {
            .x           = src->x,
            .y           = src->y,
            .w           = src->w,
            .h           = src->h,
            .intra       = src->force_intra,
            .quality     = src->qp_val,
            .qp_area_idx = 0,
            .area_map_en = 1,
            .abs_qp_en   = src->qp_mode,
        };
    }
    fake->cfg.number = fake->nb_regions;
    fake->nb_regions = 0;
    return mpp_meta_set_ptr(meta, KEY_ROI_DATA, &fake->cfg);
}

static const FFRKMPPEncROIOps fake_roi_ops = {
    .init       = fake_roi_init,
    .deinit     = fake_roi_deinit,
    .add_region = fake_roi_add_region,
    .setup_meta = fake_roi_setup_meta,
};

static int get_legacy_cfg(MppFrame frame, MppEncROICfg **cfg)
{
    void *ptr = NULL;
    MppMeta meta = mpp_frame_get_meta(frame);

    if (!meta || mpp_meta_get_ptr(meta, KEY_ROI_DATA, &ptr) != MPP_OK || !ptr)
        return AVERROR_EXTERNAL;
    *cfg = ptr;
    return 0;
}

static int test_metadata(void)
{
    FFRKMPPEncROIContext roi = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIContext unused = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIContext override = { .mpp_ops = &fake_roi_ops };
    FFRKMPPEncROIFrame slot1 = { 0 };
    FFRKMPPEncROIFrame slot2 = { 0 };
    FFRKMPPEncROIFrame unused_slot = { 0 };
    FFRKMPPEncROIFrame override_slot = { 0 };
    AVFrame *frame1 = NULL;
    AVFrame *frame2 = NULL;
    AVFrame *clear = NULL;
    AVFrame *plain = NULL;
    AVFrame *static_frame = NULL;
    MppFrame mpp1 = NULL;
    MppFrame mpp2 = NULL;
    MppFrame mpp_clear = NULL;
    MppFrame mpp_plain = NULL;
    MppFrame mpp_override = NULL;
    MppEncROICfg *cfg1 = NULL;
    MppEncROICfg *cfg2 = NULL;
    MppEncROICfg *cfg_clear = NULL;
    MppEncROICfg *cfg_override = NULL;
    int ret = 1;

    frame1 = make_frame(1920, 1080, 2, -1, 2);
    frame2 = make_frame(1920, 1080, 1, 1, 2);
    clear = make_frame(1920, 1080, 0, 0, 1);
    plain = make_frame(640, 480, 0, 0, 1);
    static_frame = make_frame(640, 480, 1, -1, 2);
    if (!frame1 || !frame2 || !clear || !plain || !static_frame ||
        mpp_frame_init(&mpp1) != MPP_OK ||
        mpp_frame_init(&mpp2) != MPP_OK ||
        mpp_frame_init(&mpp_clear) != MPP_OK ||
        mpp_frame_init(&mpp_plain) != MPP_OK ||
        mpp_frame_init(&mpp_override) != MPP_OK)
        goto end;

    if (ff_rkmppenc_roi_init(NULL, &unused) < 0 ||
        ff_rkmppenc_roi_prepare(NULL, &unused, &unused_slot,
                                plain, mpp_plain,
                                MPP_VIDEO_CodingAVC) < 0 ||
        unused.metadata_used || unused_slot.mpp_roi ||
        !unused_slot.prepared ||
        mpp_frame_has_meta(mpp_plain)) {
        fprintf(stderr, "ROI metadata was created before ROI was used\n");
        goto end;
    }

    if (ff_rkmppenc_roi_init(NULL, &roi) < 0 ||
        ff_rkmppenc_roi_prepare(NULL, &roi, &slot1, frame1, mpp1,
                                MPP_VIDEO_CodingAVC) < 0 ||
        get_legacy_cfg(mpp1, &cfg1) < 0 || cfg1->number != 2 ||
        cfg1->regions[0].x != 32 || cfg1->regions[0].quality != -26 ||
        cfg1->regions[1].x != 352 || cfg1->regions[1].quality != 0) {
        fprintf(stderr, "static ROI metadata mapping or order failed\n");
        goto end;
    }

    if (ff_rkmppenc_roi_prepare(NULL, &roi, &slot2, frame2, mpp2,
                                MPP_VIDEO_CodingAVC) < 0 ||
        get_legacy_cfg(mpp2, &cfg2) < 0 || cfg2->number != 1 ||
        cfg2->regions[0].quality != 26 || cfg1 == cfg2 ||
        cfg1->number != 2 || cfg1->regions[0].quality != -26) {
        fprintf(stderr, "per-slot ROI metadata storage is not independent\n");
        goto end;
    }

    slot1.prepared = 0;
    if (ff_rkmppenc_roi_prepare(NULL, &roi, &slot1, clear, mpp_clear,
                                MPP_VIDEO_CodingAVC) < 0 ||
        get_legacy_cfg(mpp_clear, &cfg_clear) < 0 ||
        cfg_clear != cfg1 || cfg_clear->number != 0 ||
        cfg2->number != 1) {
        fprintf(stderr, "missing side data did not explicitly clear ROI\n");
        goto end;
    }

    if (ff_rkmppenc_roi_init(NULL, &override) < 0)
        goto end;
    override.socket_active = 1;
    override.socket_nb_regions = 0;
    if (ff_rkmppenc_roi_prepare(NULL, &override, &override_slot,
                                static_frame, mpp_override,
                                MPP_VIDEO_CodingHEVC) < 0 ||
        get_legacy_cfg(mpp_override, &cfg_override) < 0 ||
        cfg_override->number != 0 || !override.metadata_used) {
        fprintf(stderr, "empty socket state did not override static ROI\n");
        goto end;
    }

    ret = 0;

end:
    ff_rkmppenc_roi_frame_uninit(&slot1);
    ff_rkmppenc_roi_frame_uninit(&slot2);
    ff_rkmppenc_roi_frame_uninit(&unused_slot);
    ff_rkmppenc_roi_frame_uninit(&override_slot);
    ff_rkmppenc_roi_uninit(NULL, &roi);
    ff_rkmppenc_roi_uninit(NULL, &unused);
    ff_rkmppenc_roi_uninit(NULL, &override);
    if (mpp1)
        mpp_frame_deinit(&mpp1);
    if (mpp2)
        mpp_frame_deinit(&mpp2);
    if (mpp_clear)
        mpp_frame_deinit(&mpp_clear);
    if (mpp_plain)
        mpp_frame_deinit(&mpp_plain);
    if (mpp_override)
        mpp_frame_deinit(&mpp_override);
    av_frame_free(&frame1);
    av_frame_free(&frame2);
    av_frame_free(&clear);
    av_frame_free(&plain);
    av_frame_free(&static_frame);
    return ret;
}

int main(void)
{
    int ret = 0;

    av_log_set_level(AV_LOG_QUIET);
    ret |= test_json();
    ret |= test_mapping();
    ret |= test_socket();
    ret |= test_metadata();
    return ret;
}
