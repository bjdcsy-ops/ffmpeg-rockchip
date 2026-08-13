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
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stddef.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <unistd.h>

#include "libavutil/common.h"
#include "libavutil/error.h"
#include "libavutil/log.h"
#include "libavutil/mathematics.h"
#include "libavutil/rational.h"

#include "rkmppenc_roi.h"

typedef struct RKMPPJSONParser {
    const char *pos;
    const char *end;
} RKMPPJSONParser;

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

static void json_skip_space(RKMPPJSONParser *p)
{
    while (p->pos < p->end &&
           (*p->pos == ' ' || *p->pos == '\t' ||
            *p->pos == '\r' || *p->pos == '\n'))
        p->pos++;
}

static int json_take(RKMPPJSONParser *p, char token)
{
    json_skip_space(p);
    if (p->pos >= p->end || *p->pos != token)
        return AVERROR(EINVAL);

    p->pos++;
    return 0;
}

static int json_parse_key(RKMPPJSONParser *p, char *key, size_t key_size)
{
    size_t len = 0;

    if (json_take(p, '"') < 0)
        return AVERROR(EINVAL);

    while (p->pos < p->end && *p->pos != '"') {
        unsigned char ch = *p->pos++;

        /* Protocol field names are deliberately ASCII and unescaped. */
        if (ch < 0x20 || ch >= 0x7f || ch == '\\' || len + 1 >= key_size)
            return AVERROR(EINVAL);
        key[len++] = ch;
    }

    if (p->pos >= p->end)
        return AVERROR(EINVAL);

    p->pos++;
    key[len] = '\0';
    return 0;
}

static int json_parse_int(RKMPPJSONParser *p, int *result)
{
    int64_t value = 0;
    int negative = 0;
    int64_t limit;

    json_skip_space(p);
    if (p->pos < p->end && *p->pos == '-') {
        negative = 1;
        p->pos++;
    }

    if (p->pos >= p->end || *p->pos < '0' || *p->pos > '9')
        return AVERROR(EINVAL);
    if (*p->pos == '0' && p->pos + 1 < p->end &&
        p->pos[1] >= '0' && p->pos[1] <= '9')
        return AVERROR(EINVAL);

    limit = negative ? -(int64_t)INT_MIN : INT_MAX;
    do {
        int digit = *p->pos++ - '0';

        if (value > (limit - digit) / 10)
            return AVERROR(ERANGE);
        value = value * 10 + digit;
    } while (p->pos < p->end && *p->pos >= '0' && *p->pos <= '9');

    *result = negative ? -value : value;
    return 0;
}

static int json_parse_qoffset(RKMPPJSONParser *p, AVRational *result)
{
    uint64_t significand = 0;
    int64_t decimal_pos;
    int64_t exponent = 0;
    int64_t scale;
    size_t digit_index = 0;
    size_t first_nonzero = (size_t)-1;
    int integer_digits = 0;
    int exponent_negative = 0;
    int negative = 0;
    int stored_digits = 0;
    int first_digit = 0;
    int later_nonzero = 0;
    int digit;
    long double value;

#define CONSUME_DIGIT() do {                                           \
        digit = *p->pos++ - '0';                                      \
        if (first_nonzero == (size_t)-1 && digit) {                    \
            first_nonzero = digit_index;                              \
            first_digit = digit;                                      \
        } else if (first_nonzero != (size_t)-1 && digit) {             \
            later_nonzero = 1;                                        \
        }                                                              \
        if (first_nonzero != (size_t)-1 && stored_digits < 18) {       \
            significand = significand * 10 + digit;                   \
            stored_digits++;                                          \
        }                                                              \
        digit_index++;                                                 \
    } while (0)

    json_skip_space(p);
    if (p->pos < p->end && *p->pos == '-') {
        negative = 1;
        p->pos++;
    }

    if (p->pos >= p->end)
        return AVERROR(EINVAL);
    if (*p->pos == '0') {
        CONSUME_DIGIT();
        integer_digits = 1;
        if (p->pos < p->end && *p->pos >= '0' && *p->pos <= '9')
            return AVERROR(EINVAL);
    } else {
        if (*p->pos < '1' || *p->pos > '9')
            return AVERROR(EINVAL);
        while (p->pos < p->end && *p->pos >= '0' && *p->pos <= '9') {
            CONSUME_DIGIT();
            integer_digits++;
        }
    }

    if (p->pos < p->end && *p->pos == '.') {
        p->pos++;
        if (p->pos >= p->end || *p->pos < '0' || *p->pos > '9')
            return AVERROR(EINVAL);
        while (p->pos < p->end && *p->pos >= '0' && *p->pos <= '9')
            CONSUME_DIGIT();
    }

    if (p->pos < p->end && (*p->pos == 'e' || *p->pos == 'E')) {
        p->pos++;
        if (p->pos < p->end && (*p->pos == '+' || *p->pos == '-')) {
            exponent_negative = *p->pos == '-';
            p->pos++;
        }
        if (p->pos >= p->end || *p->pos < '0' || *p->pos > '9')
            return AVERROR(EINVAL);
        while (p->pos < p->end && *p->pos >= '0' && *p->pos <= '9') {
            digit = *p->pos - '0';
            if (exponent < 1000000)
                exponent = exponent * 10 + digit;
            p->pos++;
        }
    }

    if (first_nonzero == (size_t)-1) {
        *result = (AVRational) { 0, 1 };
#undef CONSUME_DIGIT
        return 0;
    }

    exponent = exponent_negative ? -exponent : exponent;
    decimal_pos = (int64_t)integer_digits + exponent;
    scale = decimal_pos - (int64_t)first_nonzero;
    if (scale > 1 ||
        (scale == 1 && (first_digit != 1 || later_nonzero))) {
#undef CONSUME_DIGIT
        return AVERROR(ERANGE);
    }

    scale = decimal_pos - (int64_t)first_nonzero - stored_digits;
    value = significand;
    if (scale < -5000) {
        value = 0;
    } else if (scale > 5000) {
#undef CONSUME_DIGIT
        return AVERROR(ERANGE);
    } else {
        while (scale < 0) {
            value /= 10;
            scale++;
        }
        while (scale > 0) {
            value *= 10;
            scale--;
        }
    }
    if (negative)
        value = -value;
    *result = av_d2q((double)value, 1 << 24);

#undef CONSUME_DIGIT
    return 0;
}

static int json_parse_roi(RKMPPJSONParser *p, AVRegionOfInterest *roi)
{
    enum {
        ROI_X       = 1 << 0,
        ROI_Y       = 1 << 1,
        ROI_W       = 1 << 2,
        ROI_H       = 1 << 3,
        ROI_QOFFSET = 1 << 4,
        ROI_ALL     = ROI_X | ROI_Y | ROI_W | ROI_H | ROI_QOFFSET,
    };
    char key[16];
    AVRational qoffset = { 0, 1 };
    int fields = 0;
    int x = 0, y = 0, w = 0, h = 0;
    int ret;

    if ((ret = json_take(p, '{')) < 0)
        return ret;

    while (1) {
        json_skip_space(p);
        if (p->pos >= p->end || *p->pos == '}')
            return AVERROR(EINVAL);

        if ((ret = json_parse_key(p, key, sizeof(key))) < 0 ||
            (ret = json_take(p, ':')) < 0)
            return ret;

#define PARSE_ROI_INT_FIELD(name, bit, dst)                              \
        if (!strcmp(key, name)) {                                       \
            if (fields & bit)                                           \
                return AVERROR(EINVAL);                                 \
            if ((ret = json_parse_int(p, &dst)) < 0)                    \
                return ret;                                             \
            fields |= bit;                                              \
        }

        PARSE_ROI_INT_FIELD("x", ROI_X, x)
        else PARSE_ROI_INT_FIELD("y", ROI_Y, y)
        else PARSE_ROI_INT_FIELD("w", ROI_W, w)
        else PARSE_ROI_INT_FIELD("h", ROI_H, h)
        else if (!strcmp(key, "qoffset")) {
            if (fields & ROI_QOFFSET)
                return AVERROR(EINVAL);
            if ((ret = json_parse_qoffset(p, &qoffset)) < 0)
                return ret;
            fields |= ROI_QOFFSET;
        } else {
            return AVERROR(EINVAL);
        }

#undef PARSE_ROI_INT_FIELD

        json_skip_space(p);
        if (p->pos < p->end && *p->pos == ',') {
            p->pos++;
            continue;
        }
        if (p->pos < p->end && *p->pos == '}') {
            p->pos++;
            break;
        }
        return AVERROR(EINVAL);
    }

    if (fields != ROI_ALL ||
        x < 0 || y < 0 || w <= 0 || h <= 0 ||
        (int64_t)x + w > INT_MAX || (int64_t)y + h > INT_MAX ||
        av_cmp_q(qoffset, (AVRational) { -1, 1 }) < 0 ||
        av_cmp_q(qoffset, (AVRational) { 1, 1 }) > 0)
        return AVERROR(EINVAL);

    *roi = (AVRegionOfInterest) {
        .self_size = sizeof(*roi),
        .top       = y,
        .bottom    = y + h,
        .left      = x,
        .right     = x + w,
        .qoffset   = qoffset,
    };

    return 0;
}

int ff_rkmppenc_roi_parse_json(AVRegionOfInterest *regions, int *nb_regions,
                               const char *json, size_t json_size)
{
    AVRegionOfInterest parsed[FF_RKMPPENC_ROI_MAX_REGIONS];
    RKMPPJSONParser p;
    char key[16];
    int parsed_count = 0;
    int version_seen = 0;
    int regions_seen = 0;
    int ret;

    if (!regions || !nb_regions || !json || !json_size)
        return AVERROR(EINVAL);
    if (json_size > FF_RKMPPENC_ROI_MAX_PACKET_SIZE)
        return AVERROR(EMSGSIZE);

    p = (RKMPPJSONParser) { json, json + json_size };
    if ((ret = json_take(&p, '{')) < 0)
        return ret;

    while (1) {
        json_skip_space(&p);
        if (p.pos >= p.end || *p.pos == '}')
            return AVERROR(EINVAL);

        if ((ret = json_parse_key(&p, key, sizeof(key))) < 0 ||
            (ret = json_take(&p, ':')) < 0)
            return ret;

        if (!strcmp(key, "version")) {
            int version;

            if (version_seen)
                return AVERROR(EINVAL);
            if ((ret = json_parse_int(&p, &version)) < 0)
                return ret;
            if (version != 1)
                return AVERROR(EPROTONOSUPPORT);
            version_seen = 1;
        } else if (!strcmp(key, "regions")) {
            if (regions_seen)
                return AVERROR(EINVAL);
            if ((ret = json_take(&p, '[')) < 0)
                return ret;

            json_skip_space(&p);
            if (p.pos < p.end && *p.pos == ']') {
                p.pos++;
            } else {
                while (1) {
                    if (parsed_count >= FF_RKMPPENC_ROI_MAX_REGIONS)
                        return AVERROR(ENOSPC);
                    if ((ret = json_parse_roi(&p, &parsed[parsed_count])) < 0)
                        return ret;
                    parsed_count++;

                    json_skip_space(&p);
                    if (p.pos < p.end && *p.pos == ',') {
                        p.pos++;
                        continue;
                    }
                    if (p.pos < p.end && *p.pos == ']') {
                        p.pos++;
                        break;
                    }
                    return AVERROR(EINVAL);
                }
            }
            regions_seen = 1;
        } else {
            return AVERROR(EINVAL);
        }

        json_skip_space(&p);
        if (p.pos < p.end && *p.pos == ',') {
            p.pos++;
            continue;
        }
        if (p.pos < p.end && *p.pos == '}') {
            p.pos++;
            break;
        }
        return AVERROR(EINVAL);
    }

    json_skip_space(&p);
    if (p.pos != p.end || !version_seen || !regions_seen)
        return AVERROR(EINVAL);

    memcpy(regions, parsed, parsed_count * sizeof(*regions));
    *nb_regions = parsed_count;
    return 0;
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

static void roi_socket_close(AVCodecContext *avctx,
                             FFRKMPPEncROIContext *roi)
{
    struct stat st;

    if (roi->socket_fd >= 0) {
        close(roi->socket_fd);
        roi->socket_fd = -1;
    }

    if (roi->socket_bound && roi->socket_path &&
        lstat(roi->socket_path, &st) == 0 && S_ISSOCK(st.st_mode) &&
        (uint64_t)st.st_dev == roi->socket_dev &&
        (uint64_t)st.st_ino == roi->socket_ino) {
        if (unlink(roi->socket_path) < 0)
            av_log(avctx, AV_LOG_WARNING,
                   "Failed to remove ROI socket '%s': %s\n",
                   roi->socket_path, av_err2str(AVERROR(errno)));
    }

    roi->socket_bound = 0;
    roi->socket_dev = 0;
    roi->socket_ino = 0;
}

static int roi_socket_open(AVCodecContext *avctx,
                           FFRKMPPEncROIContext *roi)
{
    struct sockaddr_un addr = { 0 };
    struct stat st;
    socklen_t addr_len;
    size_t path_len;
    int fd = -1;
    int fd_flags;
    int status_flags;
    int ret;

    if (!roi->socket_path || !*roi->socket_path)
        return 0;

    path_len = strlen(roi->socket_path);
    if (path_len >= sizeof(addr.sun_path)) {
        av_log(avctx, AV_LOG_ERROR,
               "ROI socket path is too long (%zu >= %zu): %s\n",
               path_len, sizeof(addr.sun_path), roi->socket_path);
        return AVERROR(ENAMETOOLONG);
    }

    fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0) {
        ret = AVERROR(errno);
        av_log(avctx, AV_LOG_ERROR,
               "Failed to create ROI socket: %s\n", av_err2str(ret));
        return ret;
    }

    status_flags = fcntl(fd, F_GETFL);
    fd_flags = fcntl(fd, F_GETFD);
    if (status_flags < 0 || fd_flags < 0 ||
        fcntl(fd, F_SETFL, status_flags | O_NONBLOCK) < 0 ||
        fcntl(fd, F_SETFD, fd_flags | FD_CLOEXEC) < 0) {
        ret = AVERROR(errno);
        av_log(avctx, AV_LOG_ERROR,
               "Failed to configure ROI socket: %s\n", av_err2str(ret));
        close(fd);
        return ret;
    }

    addr.sun_family = AF_UNIX;
    memcpy(addr.sun_path, roi->socket_path, path_len + 1);
    addr_len = offsetof(struct sockaddr_un, sun_path) + path_len + 1;

    /* Never unlink here: an existing path is an intentional hard failure. */
    if (bind(fd, (const struct sockaddr *)&addr, addr_len) < 0) {
        ret = AVERROR(errno);
        av_log(avctx, AV_LOG_ERROR,
               "Failed to bind ROI socket '%s': %s. "
               "Remove a stale socket path before retrying.\n",
               roi->socket_path, av_err2str(ret));
        close(fd);
        return ret;
    }

    if (lstat(roi->socket_path, &st) < 0) {
        ret = AVERROR(errno);
        av_log(avctx, AV_LOG_ERROR,
               "Failed to identify bound ROI socket '%s': %s\n",
               roi->socket_path, av_err2str(ret));
        close(fd);
        /* Do not blindly remove a path whose identity was not established. */
        return ret;
    }
    if (!S_ISSOCK(st.st_mode)) {
        ret = AVERROR(EIO);
        av_log(avctx, AV_LOG_ERROR,
               "Bound ROI path '%s' is not a socket.\n", roi->socket_path);
        close(fd);
        /* Do not blindly remove a path whose identity was not established. */
        return ret;
    }

    roi->socket_fd = fd;
    roi->socket_dev = st.st_dev;
    roi->socket_ino = st.st_ino;
    roi->socket_bound = 1;

    av_log(avctx, AV_LOG_INFO,
           "Listening for atomic ROI updates on Unix datagram socket '%s'.\n",
           roi->socket_path);
    return 0;
}

int ff_rkmppenc_roi_init(AVCodecContext *avctx,
                         FFRKMPPEncROIContext *roi)
{
    if (!roi)
        return AVERROR(EINVAL);

    if (roi->initialized)
        ff_rkmppenc_roi_uninit(avctx, roi);

    roi->initialized = 1;
    roi->socket_fd = -1;
    roi->socket_dev = 0;
    roi->socket_ino = 0;
    roi->socket_bound = 0;
    roi->socket_nb_regions = 0;
    roi->socket_active = 0;
    roi->socket_generation = 0;
    roi->metadata_used = 0;
    roi->regions_warned = 0;
    roi->invalid_packet_warned = 0;
    roi->budget_warned = 0;
    roi->max_packets_per_frame = FF_RKMPPENC_ROI_MAX_PACKETS_PER_FRAME;

    return roi_socket_open(avctx, roi);
}

void ff_rkmppenc_roi_uninit(AVCodecContext *avctx,
                            FFRKMPPEncROIContext *roi)
{
    if (!roi)
        return;
    if (!roi->initialized)
        return;

    roi_socket_close(avctx, roi);
    roi->socket_nb_regions = 0;
    roi->socket_active = 0;
    roi->socket_generation = 0;
    roi->metadata_used = 0;
    roi->regions_warned = 0;
    roi->invalid_packet_warned = 0;
    roi->budget_warned = 0;
    roi->max_packets_per_frame = 0;
    roi->initialized = 0;
}

int ff_rkmppenc_roi_drain(AVCodecContext *avctx,
                          FFRKMPPEncROIContext *roi,
                          int frame_width, int frame_height)
{
    char packet[FF_RKMPPENC_ROI_MAX_PACKET_SIZE + 1];
    int packets = 0;

    if (!roi || !roi->initialized ||
        frame_width <= 0 || frame_height <= 0)
        return AVERROR(EINVAL);
    if (roi->socket_fd < 0)
        return 0;

    while (packets < roi->max_packets_per_frame) {
        AVRegionOfInterest regions[FF_RKMPPENC_ROI_MAX_REGIONS];
        RoiRegionCfg mapped[FF_RKMPPENC_ROI_MAX_REGIONS];
        struct iovec iov = { packet, sizeof(packet) };
        struct msghdr msg = {
            .msg_iov = &iov,
            .msg_iovlen = 1,
        };
        ssize_t packet_size;
        int nb_regions;
        int ret;
        int i;

        /* The descriptor itself is O_NONBLOCK; avoid non-POSIX MSG_DONTWAIT. */
        packet_size = recvmsg(roi->socket_fd, &msg, 0);
        if (packet_size < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return 0;
            if (errno == EINTR)
                continue;

            ret = AVERROR(errno);
            av_log(avctx, AV_LOG_WARNING,
                   "Failed to receive ROI socket update: %s\n",
                   av_err2str(ret));
            return 0;
        }
        packets++;

        if ((msg.msg_flags & MSG_TRUNC) ||
            packet_size > FF_RKMPPENC_ROI_MAX_PACKET_SIZE) {
            av_log_once(avctx, AV_LOG_WARNING, AV_LOG_DEBUG,
                        &roi->invalid_packet_warned,
                        "Ignoring oversized ROI socket update.\n");
            continue;
        }

        ret = ff_rkmppenc_roi_parse_json(regions, &nb_regions,
                                         packet, packet_size);
        if (ret < 0) {
            av_log_once(avctx, AV_LOG_WARNING, AV_LOG_DEBUG,
                        &roi->invalid_packet_warned,
                        "Ignoring invalid ROI socket update (%s).\n",
                        av_err2str(ret));
            continue;
        }

        for (i = 0; i < nb_regions; i++) {
            int j;

            ret = ff_rkmppenc_roi_map_region(&mapped[i], &regions[i],
                                             frame_width, frame_height);
            if (ret < 0) {
                av_log_once(avctx, AV_LOG_WARNING, AV_LOG_DEBUG,
                            &roi->invalid_packet_warned,
                            "Ignoring ROI socket update with invalid region "
                            "%d: (%d,%d)-(%d,%d), qoffset %d/%d, frame "
                            "%dx%d.\n", i, regions[i].left,
                            regions[i].top, regions[i].right,
                            regions[i].bottom, regions[i].qoffset.num,
                            regions[i].qoffset.den, frame_width, frame_height);
                break;
            }

            for (j = 0; j < i; j++) {
                if (roi_regions_overlap(&mapped[j], &mapped[i])) {
                    av_log_once(avctx, AV_LOG_WARNING, AV_LOG_DEBUG,
                                &roi->invalid_packet_warned,
                                "Ignoring ROI socket update with overlapping "
                                "regions %d and %d.\n", j, i);
                    ret = AVERROR(EINVAL);
                    break;
                }
            }
            if (ret < 0)
                break;
        }
        if (ret < 0)
            continue;

        memcpy(roi->socket_regions, regions,
               nb_regions * sizeof(*regions));
        roi->socket_nb_regions = nb_regions;
        roi->socket_active = 1;
        roi->socket_generation++;

        av_log(avctx, AV_LOG_DEBUG,
               "Applied ROI socket update #%" PRIu64 " with %d region(s).\n",
               roi->socket_generation, nb_regions);
    }

    av_log_once(avctx, AV_LOG_WARNING, AV_LOG_DEBUG,
                &roi->budget_warned,
                "ROI socket update budget exhausted after %d datagrams; "
                "any remaining updates will be processed on a later "
                "frame.\n", packets);
    return 0;
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
    if (roi->socket_active) {
        roi_data = (const uint8_t *)roi->socket_regions;
        roi_size = sizeof(roi->socket_regions[0]);
        count = roi->socket_nb_regions;
    } else {
        sd = av_frame_get_side_data(frame,
                                    AV_FRAME_DATA_REGIONS_OF_INTEREST);
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
         * ROI2 maps can enforce FFmpeg's first-region-wins rule, but the
         * RK3576 legacy register backend does not document tie-breaking for
         * equal-priority overlapping rectangles.  Reject overlap so both
         * supported SoCs have one deterministic contract.
         */
        for (j = 0; j < i; j++) {
            if (roi_regions_overlap(&regions[j], &regions[i])) {
                av_log(avctx, AV_LOG_ERROR,
                       "Overlapping ROI regions %d and %d are not supported "
                       "consistently by RKMPP hardware.\n", j, i);
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

    if ((ret = ff_rkmppenc_roi_drain(avctx, roi,
                                     frame->width, frame->height)) < 0)
        return ret;
    if ((ret = collect_frame_regions(avctx, roi, frame,
                                     regions, &nb_regions)) < 0)
        return ret;

    if (nb_regions > 0 || roi->socket_active)
        roi->metadata_used = 1;
    if (!roi->metadata_used) {
        roi_frame->prepared = 1;
        return 0;
    }

    if ((ret = roi_frame_ensure(avctx, roi_frame,
                                frame->width, frame->height,
                                coding_type, ops)) < 0)
        return ret;

    /* Overlap was rejected above, so both MPP backends see the same set. */
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

    roi_frame->prepared = 1;

    return 0;
}
