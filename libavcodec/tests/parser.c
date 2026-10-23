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

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "libavutil/common.h"
#include "libavutil/log.h"
#include "libavcodec/avcodec.h"
#include "parser_data.h"

typedef struct ParserFixture {
    enum AVCodecID codec_id;
    const uint8_t *data;
    const int *sizes;
    int duration;
} ParserFixture;

static const ParserFixture fixtures[] = {
    { AV_CODEC_ID_H264, parser_h264_data, parser_h264_sizes, 3600 },
    { AV_CODEC_ID_HEVC, parser_hevc_data, parser_hevc_sizes, 3600 },
    { AV_CODEC_ID_AAC,  parser_aac_data,  parser_aac_sizes,  1024 },
};

static int check_output(const ParserFixture *fixture, AVCodecParserContext *parser,
                        const uint8_t *data, int size, int *count,
                        int64_t first_pts, int64_t initial_pos, int mode)
{
    int offset = 0;
    int64_t expected_pts, expected_pos, last_pos;

    if (!size)
        return 0;
    if (*count >= 4) {
        fprintf(stderr, "%s: unexpected extra frame\n",
                avcodec_get_name(fixture->codec_id));
        return 1;
    }
    for (int i = 0; i < *count; i++)
        offset += fixture->sizes[i];
    expected_pts = first_pts + *count * fixture->duration;
    expected_pos = initial_pos < 0 ? -1 : initial_pos + offset;
    last_pos = expected_pos;
    /* A fragmented frame can take its timestamp from a later input packet. */
    if (initial_pos >= 0 && mode == 2 && *count)
        last_pos += size - 1;
    if (size != fixture->sizes[*count] ||
        memcmp(data, fixture->data + offset, size) ||
        parser->pts != expected_pts || parser->dts != expected_pts ||
        parser->pos < expected_pos || parser->pos > last_pos) {
        fprintf(stderr, "%s frame %d: size=%d pts/dts=%"PRId64"/%"PRId64
                " pos=%"PRId64", expected size=%d pts/dts=%"PRId64
                " pos=%"PRId64"\n", avcodec_get_name(fixture->codec_id),
                *count, size, parser->pts, parser->dts, parser->pos,
                fixture->sizes[*count], expected_pts, expected_pos);
        return 1;
    }
    (*count)++;
    return 0;
}

static int parse_fixture(const ParserFixture *fixture, int mode,
                         int64_t first_pts, int64_t initial_pos)
{
    AVCodecContext *ctx = avcodec_alloc_context3(NULL);
    AVCodecParserContext *parser = NULL;
    int ret = 1;

    if (!ctx)
        return 1;
    ctx->codec_id = fixture->codec_id;
    ctx->codec_type = fixture->codec_id == AV_CODEC_ID_AAC ?
                      AVMEDIA_TYPE_AUDIO : AVMEDIA_TYPE_VIDEO;
    ctx->pkt_timebase = (AVRational) { 1, ctx->codec_type == AVMEDIA_TYPE_AUDIO ?
                                       48000 : 90000 };

    /* Reopen the parser with the same codec context, as after a seek. */
    for (int pass = 0; pass < 2; pass++) {
        int count = 0, total = 0;
        int64_t start_pts = first_pts + pass * 4 * fixture->duration;

        parser = av_parser_init(fixture->codec_id);
        if (!parser)
            goto end;
        if (!mode)
            parser->flags |= PARSER_FLAG_COMPLETE_FRAMES;

        for (int frame = 0; frame < 4; frame++) {
            int offset = 0;

            while (offset < fixture->sizes[frame]) {
                uint8_t input[sizeof(parser_hevc_data) + AV_INPUT_BUFFER_PADDING_SIZE];
                int chunk = mode == 2 ? (offset ? 7 : 1) : fixture->sizes[frame];
                int used = 0;
                int64_t pts = start_pts + frame * fixture->duration;
                int64_t pos = initial_pos < 0 ? -1 : initial_pos + total + offset;

                chunk = FFMIN(chunk, fixture->sizes[frame] - offset);
                memcpy(input, fixture->data + total + offset, chunk);
                memset(input + chunk, 0, AV_INPUT_BUFFER_PADDING_SIZE);
                while (used < chunk) {
                    uint8_t *output;
                    int size, consumed;

                    consumed = av_parser_parse2(parser, ctx, &output, &size,
                                                input + used, chunk - used,
                                                pts, pts, pos);
                    if (consumed < 0 || (!consumed && !size) ||
                        check_output(fixture, parser, output, size, &count,
                                     start_pts, initial_pos, mode))
                        goto end;
                    used += consumed;
                    pts = AV_NOPTS_VALUE;
                    pos = -1;
                }
                offset += chunk;
            }
            total += fixture->sizes[frame];
        }

        for (int drain = 0; drain < 3; drain++) {
            uint8_t *output;
            int size;

            if (av_parser_parse2(parser, ctx, &output, &size, NULL, 0,
                                 AV_NOPTS_VALUE, AV_NOPTS_VALUE, -1) < 0 ||
                (drain && size) ||
                check_output(fixture, parser, output, size, &count,
                             start_pts, initial_pos, mode))
                goto end;
        }
        if (count != 4) {
            fprintf(stderr, "%s: %d frames instead of 4\n",
                    avcodec_get_name(fixture->codec_id), count);
            goto end;
        }
        av_parser_close(parser);
        parser = NULL;
    }
    ret = 0;
end:
    if (ret)
        fprintf(stderr, "%s: mode=%d first_pts=%"PRId64" initial_pos=%"PRId64"\n",
                avcodec_get_name(fixture->codec_id), mode, first_pts, initial_pos);
    av_parser_close(parser);
    avcodec_free_context(&ctx);
    return ret;
}

int main(void)
{
    static const int64_t positions[] = { -1, 0, 4096 };
    static const int64_t timestamps[] = { 0, 9000 };

    /* Empty H.264 drain calls can log errors despite producing no frame. */
    av_log_set_level(AV_LOG_QUIET);
    for (int f = 0; f < FF_ARRAY_ELEMS(fixtures); f++)
        for (int mode = 0; mode < 3; mode++)
            for (int t = 0; t < FF_ARRAY_ELEMS(timestamps); t++)
                for (int p = 0; p < FF_ARRAY_ELEMS(positions); p++)
                    if (parse_fixture(&fixtures[f], mode, timestamps[t], positions[p]))
                        return 1;
    return 0;
}
