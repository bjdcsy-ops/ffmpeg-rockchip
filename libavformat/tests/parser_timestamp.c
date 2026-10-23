/*
 * Parser timestamp and raw byte-position regression tests
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

#include <stdio.h>
#include <string.h>

#include "libavutil/common.h"
#include "libavutil/mem.h"
#include "libavcodec/tests/parser_data.h"
#include "libavformat/avformat.h"
#include "libavformat/demux.h"
#include "libavformat/internal.h"

#define FRAME_SIZE 11
#define KNOWN_POS 128
#define FIRST_PTS 10000

typedef struct TestInput {
    AVFormatContext *format;
    AVIOContext *io;
    enum AVStreamParseType parsing;
    int known_pos;
    int position;
    int size;
} TestInput;

#define CHECK(condition) do {                                         \
    if (!(condition)) {                                               \
        fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #condition); \
        ret = 1;                                                      \
        goto end;                                                     \
    }                                                                 \
} while (0)

static int read_memory(void *opaque, uint8_t *buffer, int size)
{
    TestInput *test = opaque;

    if (test->position == test->size)
        return AVERROR_EOF;
    size = FFMIN(size, test->size - test->position);
    memcpy(buffer, parser_aac_data + test->position, size);
    test->position += size;
    return size;
}

static int64_t seek_memory(void *opaque, int64_t offset, int whence)
{
    TestInput *test = opaque;

    if (whence == AVSEEK_SIZE)
        return test->size;
    if (whence == SEEK_CUR)
        offset += test->position;
    else if (whence == SEEK_END)
        offset += test->size;
    else if (whence != SEEK_SET)
        return AVERROR(EINVAL);
    if (offset < 0 || offset > test->size)
        return AVERROR(EINVAL);
    test->position = offset;
    return offset;
}

static int read_header(AVFormatContext *format)
{
    TestInput *test = format->opaque;
    AVStream *stream = avformat_new_stream(format, NULL);

    if (!stream)
        return AVERROR(ENOMEM);
    stream->codecpar->codec_type  = AVMEDIA_TYPE_AUDIO;
    stream->codecpar->codec_id    = AV_CODEC_ID_AAC;
    stream->codecpar->sample_rate = 48000;
    stream->codecpar->frame_size  = 1024;
    stream->codecpar->ch_layout  = (AVChannelLayout)AV_CHANNEL_LAYOUT_MONO;
    stream->time_base = (AVRational){ 1, 48000 };
    ffstream(stream)->need_parsing = test->parsing;
    return 0;
}

static int read_packet(AVFormatContext *format, AVPacket *packet)
{
    TestInput *test = format->opaque;
    int64_t position = avio_tell(format->pb);
    int ret = av_get_packet(format->pb, packet, FRAME_SIZE);

    if (ret < 0)
        return ret;
    packet->stream_index = 0;
    packet->pos = test->known_pos ? KNOWN_POS + position : -1;
    packet->pts = packet->dts = FIRST_PTS + position / FRAME_SIZE * 1024;
    return 0;
}

/* Only the packet source is synthetic; parsing and byte seeks use libavformat. */
static const FFInputFormat test_demuxer = {
    .p.name      = "parser_timestamp_test",
    .read_header = read_header,
    .read_packet = read_packet,
};

static int init_test(TestInput *test, enum AVStreamParseType parsing,
                     int known_pos, int size)
{
    uint8_t *buffer;

    test->parsing   = parsing;
    test->known_pos = known_pos;
    test->size      = size;
    test->format = avformat_alloc_context();
    if (!test->format)
        return AVERROR(ENOMEM);
    buffer = av_malloc(64);
    if (!buffer)
        return AVERROR(ENOMEM);
    test->io = avio_alloc_context(buffer, 64, 0, test, read_memory, NULL, seek_memory);
    if (!test->io) {
        av_free(buffer);
        return AVERROR(ENOMEM);
    }
    test->format->pb = test->io;
    test->format->opaque = test;
    test->format->flags |= AVFMT_FLAG_CUSTOM_IO;
    return avformat_open_input(&test->format, NULL, &test_demuxer.p, NULL);
}

static void uninit_test(TestInput *test)
{
    avformat_close_input(&test->format);
    if (test->io)
        av_freep(&test->io->buffer);
    avio_context_free(&test->io);
}

static int check_packet(TestInput *test, int index, int parser_start)
{
    AVPacket packet = { 0 };
    int64_t position = test->known_pos ? KNOWN_POS + index * FRAME_SIZE :
                       test->parsing == AVSTREAM_PARSE_FULL_RAW ?
                           (index - parser_start) * FRAME_SIZE : -1;
    int ret = 0;

    CHECK(av_read_frame(test->format, &packet) >= 0);
    CHECK(packet.size == FRAME_SIZE);
    CHECK(!memcmp(packet.data, parser_aac_data + index * FRAME_SIZE, FRAME_SIZE));
    CHECK(packet.pos == position);
    CHECK(packet.pts == FIRST_PTS + index * 1024);
    CHECK(packet.dts == packet.pts);
    CHECK(ffstream(test->format->streams[0])->parser);
end:
    av_packet_unref(&packet);
    return ret;
}

static int check_eof(TestInput *test)
{
    AVPacket packet = { 0 };
    int ret = 0;

    for (int i = 0; i < 3; i++) {
        CHECK(av_read_frame(test->format, &packet) == AVERROR_EOF);
        CHECK(!packet.data && !packet.size);
        CHECK(!ffstream(test->format->streams[0])->parser);
    }
end:
    av_packet_unref(&packet);
    return ret;
}

static int test_seek(enum AVStreamParseType parsing, int known_pos)
{
    TestInput test = { 0 };
    int ret = 0;

    CHECK(init_test(&test, parsing, known_pos, sizeof(parser_aac_data)) >= 0);
    CHECK(!check_packet(&test, 0, 0));
    ff_read_frame_flush(test.format);
    CHECK(!ffstream(test.format->streams[0])->parser);
    CHECK(!check_packet(&test, 1, 1));

    CHECK(av_seek_frame(test.format, -1, 0, AVSEEK_FLAG_BYTE) >= 0);
    CHECK(!ffstream(test.format->streams[0])->parser);
    for (int i = 0; i < FF_ARRAY_ELEMS(parser_aac_sizes); i++)
        CHECK(!check_packet(&test, i, 0));
    CHECK(!check_eof(&test));

    CHECK(avformat_seek_file(test.format, -1, FRAME_SIZE, FRAME_SIZE,
                             FRAME_SIZE, AVSEEK_FLAG_BYTE) >= 0);
    CHECK(!ffstream(test.format->streams[0])->parser);
    for (int i = 1; i < FF_ARRAY_ELEMS(parser_aac_sizes); i++)
        CHECK(!check_packet(&test, i, 1));
    CHECK(!check_eof(&test));
end:
    uninit_test(&test);
    return ret;
}

static int test_empty(enum AVStreamParseType parsing, int known_pos)
{
    TestInput test = { 0 };
    int ret = 0;

    CHECK(init_test(&test, parsing, known_pos, 0) >= 0);
    CHECK(!check_eof(&test));
end:
    uninit_test(&test);
    return ret;
}

int main(void)
{
    for (int raw = 0; raw < 2; raw++) {
        enum AVStreamParseType parsing = raw ? AVSTREAM_PARSE_FULL_RAW : AVSTREAM_PARSE_FULL;

        for (int known_pos = 0; known_pos < 2; known_pos++)
            if (test_seek(parsing, known_pos) || test_empty(parsing, known_pos))
                return 1;
    }
    return 0;
}
