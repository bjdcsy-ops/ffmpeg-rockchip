/*
 * AAC RTP packetization regression tests
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

#include "libavutil/intreadwrite.h"
#include "libavutil/mem.h"
#include "libavformat/avformat.h"
#include "libavformat/rtpenc.h"

typedef struct Packet {
    uint8_t data[600];
    int size;
} Packet;

typedef struct TestContext {
    AVFormatContext *format;
    Packet packets[16];
    int count;
    int header_written;
} TestContext;

#define CHECK(condition) do {                                          \
    if (!(condition)) {                                               \
        fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #condition);  \
        ret = 1;                                                      \
        goto end;                                                     \
    }                                                                 \
} while (0)

static int capture_packet(void *opaque, const uint8_t *data, int size)
{
    TestContext *test = opaque;
    Packet *packet;

    if (test->count >= FF_ARRAY_ELEMS(test->packets) || size > 600)
        return AVERROR(ENOSPC);
    packet = &test->packets[test->count++];
    memcpy(packet->data, data, size);
    packet->size = size;
    return size;
}

static int init_test(TestContext *test, int delay, int sender_reports)
{
    AVFormatContext *format;
    AVStream *stream;
    AVDictionary *options = NULL;
    uint8_t *buffer;
    int ret;

    ret = avformat_alloc_output_context2(&test->format, NULL, "rtp", NULL);
    if (ret < 0)
        return ret;
    format = test->format;
    stream = avformat_new_stream(format, NULL);
    if (!stream)
        return AVERROR(ENOMEM);
    stream->codecpar->codec_type = AVMEDIA_TYPE_AUDIO;
    stream->codecpar->codec_id = AV_CODEC_ID_AAC;
    stream->codecpar->sample_rate = 16000;
    stream->codecpar->ch_layout = (AVChannelLayout)AV_CHANNEL_LAYOUT_MONO;
    stream->codecpar->extradata = av_mallocz(2 + AV_INPUT_BUFFER_PADDING_SIZE);
    if (!stream->codecpar->extradata)
        return AVERROR(ENOMEM);
    stream->codecpar->extradata_size = 2;
    stream->codecpar->extradata[0] = 0x14;
    stream->codecpar->extradata[1] = 0x08;
    stream->time_base = (AVRational){ 1, 16000 };
    format->max_delay = delay;
    format->packet_size = 600;

    buffer = av_malloc(4096);
    if (!buffer)
        return AVERROR(ENOMEM);
    format->pb = avio_alloc_context(buffer, 4096, 1, test,
                                    NULL, capture_packet, NULL);
    if (!format->pb) {
        av_free(buffer);
        return AVERROR(ENOMEM);
    }
    format->pb->max_packet_size = 600;
    av_dict_set(&options, "rtpflags",
                sender_reports ? "rtcp_from_packet" : "skip_rtcp", 0);
    ret = avformat_write_header(format, &options);
    av_dict_free(&options);
    test->header_written = ret >= 0;
    return ret;
}

static void uninit_test(TestContext *test)
{
    if (!test->format)
        return;
    if (test->header_written)
        av_write_trailer(test->format);
    if (test->format->pb) {
        av_freep(&test->format->pb->buffer);
        avio_context_free(&test->format->pb);
    }
    avformat_free_context(test->format);
}

static int write_au(TestContext *test, int64_t pts, const uint8_t *data, int size)
{
    AVPacket *packet = av_packet_alloc();
    int ret;

    if (!packet)
        return AVERROR(ENOMEM);
    ret = av_new_packet(packet, size);
    if (ret >= 0) {
        memcpy(packet->data, data, size);
        packet->pts = packet->dts = pts;
        packet->duration = 1024;
        ret = av_write_frame(test->format, packet);
    }
    av_packet_free(&packet);
    return ret;
}

static int test_zero_delay(int sender_reports)
{
    TestContext test = { 0 };
    uint8_t data[100];
    uint32_t first_timestamp = 0;
    int i, ret = 0;

    memset(data, 0x5a, sizeof(data));
    CHECK(init_test(&test, 0, sender_reports) >= 0);
    for (i = 0; i < 3; i++) {
        Packet *packet;
        uint32_t timestamp;
        int start = test.count;

        /* Exercise an SR followed by media on every call without sleeping. */
        if (sender_reports)
            ((RTPMuxContext *)test.format->priv_data)->first_packet = 1;
        CHECK(write_au(&test, i * 1024, data, sizeof(data)) >= 0);
        CHECK(test.count == start + 1 + sender_reports);
        packet = &test.packets[start + sender_reports];
        CHECK(packet->size == 12 + 4 + sizeof(data));
        CHECK(packet->data[1] & 0x80);
        CHECK(AV_RB16(packet->data + 12) == 16);
        CHECK(AV_RB16(packet->data + 14) == sizeof(data) * 8);
        CHECK(!memcmp(packet->data + 16, data, sizeof(data)));
        timestamp = AV_RB32(packet->data + 4);
        if (!i)
            first_timestamp = timestamp;
        CHECK(timestamp - first_timestamp == i * 1024);
        if (sender_reports) {
            CHECK(test.packets[start].data[1] == 200);
            CHECK(AV_RB32(test.packets[start].data + 16) == timestamp);
        }
        CHECK(!((RTPMuxContext *)test.format->priv_data)->num_frames);
    }
    /* The final AU must already be on the wire, not awaiting another AU. */
    i = test.count;
    CHECK(av_write_trailer(test.format) >= 0);
    test.header_written = 0;
    CHECK(test.count == i);
end:
    uninit_test(&test);
    return ret;
}

static int test_fragmentation(void)
{
    TestContext test = { 0 };
    uint8_t data[1300];
    uint32_t timestamp;
    int i, offset = 0, ret = 0;

    for (i = 0; i < sizeof(data); i++)
        data[i] = i;
    CHECK(init_test(&test, 0, 0) >= 0);
    CHECK(write_au(&test, 0, data, sizeof(data)) >= 0);
    CHECK(test.count == 3);
    timestamp = AV_RB32(test.packets[0].data + 4);
    for (i = 0; i < test.count; i++) {
        Packet *packet = &test.packets[i];
        int size = packet->size - 16;

        CHECK(packet->size <= 600);
        CHECK(AV_RB32(packet->data + 4) == timestamp);
        CHECK(AV_RB16(packet->data + 12) == 16);
        CHECK(AV_RB16(packet->data + 14) == sizeof(data) * 8);
        CHECK(!!(packet->data[1] & 0x80) == (i == test.count - 1));
        CHECK(offset + size <= sizeof(data));
        CHECK(!memcmp(packet->data + 16, data + offset, size));
        offset += size;
    }
    CHECK(offset == sizeof(data));
    CHECK(write_au(&test, 1024, data, 100) >= 0);
    CHECK(test.count == 4);
    CHECK(AV_RB32(test.packets[3].data + 4) - timestamp == 1024);
    CHECK(!((RTPMuxContext *)test.format->priv_data)->num_frames);
end:
    uninit_test(&test);
    return ret;
}

static int test_aggregation(void)
{
    TestContext test = { 0 };
    uint8_t data[100];
    int ret = 0;

    memset(data, 0x5a, sizeof(data));
    CHECK(init_test(&test, 128000, 0) >= 0);
    CHECK(write_au(&test, 0, data, sizeof(data)) >= 0);
    CHECK(test.count == 0);
    CHECK(write_au(&test, 1024, data, sizeof(data)) >= 0);
    CHECK(test.count == 0);
    CHECK(write_au(&test, 2048, data, sizeof(data)) >= 0);
    CHECK(test.count == 1);
    CHECK(test.packets[0].size == 12 + 6 + 2 * sizeof(data));
    CHECK(AV_RB16(test.packets[0].data + 12) == 32);
    CHECK(AV_RB16(test.packets[0].data + 14) == sizeof(data) * 8);
    CHECK(AV_RB16(test.packets[0].data + 16) == sizeof(data) * 8);
    CHECK(!memcmp(test.packets[0].data + 18, data, sizeof(data)));
    CHECK(!memcmp(test.packets[0].data + 118, data, sizeof(data)));
    CHECK(((RTPMuxContext *)test.format->priv_data)->num_frames == 1);
end:
    uninit_test(&test);
    return ret;
}

int main(void)
{
    int ret = 0;

    ret |= test_zero_delay(0);
    ret |= test_zero_delay(1);
    ret |= test_fragmentation();
    ret |= test_aggregation();
    return ret;
}
