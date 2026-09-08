/* Exercise the real RTSP muxer and mux.c with controlled packet timestamps. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libavformat/avformat.h"
#include "libavformat/internal.h"
#include "libavformat/avformat_internal.h"
#include "libavformat/mux.h"
#include "libavformat/rtsp.h"
#include "libavformat/rtpenc.h"
#include "libavutil/opt.h"
#include "libavutil/mem.h"

static void snapshot(AVFormatContext *s, int step, int ret, int consumed)
{
    RTSPState *rt = s->priv_data;
    int n = 0;
    for (PacketListEntry *e = ffformatcontext(s)->packet_buffer.head; e; e = e->next)
        n++;
    printf("{\"step\":%d,\"ret\":%d,\"consumed\":%d,\"queued\":%d,\"mode\":\"%s\",\"offsets\":[", step, ret, consumed, n,
           ff_fc_internal(s)->interleave_packet == ff_interleave_packet_passthrough ? "pass" :
           ff_fc_internal(s)->interleave_packet == ff_interleave_packet_per_dts ? "dts" : "guard");
    for (int i = 0; i < s->nb_streams; i++) {
        AVFormatContext *child = rt->rtsp_streams[i]->transport_priv;
        printf("%s[%lld,%lld]", i ? "," : "", (long long)ffstream(s->streams[i])->mux_ts_offset,
               (long long)ffstream(child->streams[0])->mux_ts_offset);
    }
    puts("]}");
    fflush(stdout);
}

static void clocks(AVFormatContext *s)
{
    RTSPState *rt = s->priv_data;
    printf("{\"event\":\"clocks\",\"streams\":[");
    for (unsigned i = 0; i < s->nb_streams; i++) {
        AVFormatContext *child = rt->rtsp_streams[i]->transport_priv;
        RTPMuxContext *rtp = child->priv_data;
        AVRational parent_tb = s->streams[i]->time_base;
        AVRational rtp_tb = child->streams[0]->time_base;
        printf("%s{\"base\":%u,\"parent_tb\":[%d,%d],\"rtp_tb\":[%d,%d],\"epoch_us\":%lld}",
               i ? "," : "", rtp->base_timestamp, parent_tb.num, parent_tb.den,
               rtp_tb.num, rtp_tb.den, (long long)rtp->first_rtcp_ntp_time);
    }
    puts("]}");
}

int main(int argc, char **argv)
{
    AVFormatContext *s = NULL;
    AVDictionary *opts = NULL;
    AVPacket *pkt = av_packet_alloc();
    int ret, packet_error = 0, step = 0, stream, id;
    long long dts, pts;
    if ((argc != 6 && argc != 7) || !pkt) return 2;
    if (avformat_alloc_output_context2(&s, NULL, "rtsp", argv[1]) < 0) return 3;
    av_log_set_level(AV_LOG_VERBOSE);
    /* Types: v = H264, a = AAC 16 kHz mono, u = G711U 8 kHz mono. */
    for (const char *type = argv[3]; *type; type++) {
        AVStream *st = avformat_new_stream(s, NULL);
        AVCodecParameters *p = st->codecpar;
        if (*type == 'v') {
            p->codec_type = AVMEDIA_TYPE_VIDEO; p->codec_id = AV_CODEC_ID_H264;
            p->width = 640; p->height = 480; st->time_base = (AVRational){1, 90000};
        } else {
            p->codec_type = AVMEDIA_TYPE_AUDIO;
            p->codec_id = *type == 'a' ? AV_CODEC_ID_AAC : AV_CODEC_ID_PCM_MULAW;
            p->sample_rate = *type == 'a' ? 16000 : 8000;
            av_channel_layout_default(&p->ch_layout, 1);
            st->time_base = (AVRational){1, p->sample_rate};
            if (*type == 'a') {
                p->extradata = av_mallocz(2 + AV_INPUT_BUFFER_PADDING_SIZE);
                p->extradata_size = 2; p->extradata[0] = 0x14; p->extradata[1] = 0x08;
                p->frame_size = 1024;
            }
        }
    }
    av_dict_set(&opts, "rtsp_transport", "tcp", 0);
    av_dict_set(&opts, "rtpflags", "+rtcp_from_packet", 0);
    if (strcmp(argv[2], "default")) av_dict_set(&opts, "rtsp_immediate", argv[2], 0);
    s->max_interleave_delta = 1;
    s->max_delay = 0;
    s->avoid_negative_ts = atoi(argv[4]);
    s->output_ts_offset = atoll(argv[5]);
    ret = avformat_write_header(s, &opts);
    av_dict_free(&opts);
    if (ret < 0) return 4;
    clocks(s);
    snapshot(s, step++, ret, 1);
    /* Each line: stream index, DTS ms, PTS ms, payload identity. */
    while (scanf("%d %lld %lld %d", &stream, &dts, &pts, &id) == 4) {
        if (stream == -1) {
            ret = av_interleaved_write_frame(s, NULL);
            snapshot(s, step++, ret, 1);
            continue;
        }
        AVStream *st = s->streams[stream];
        /* G711 payload contains the declared 64 ms of actual samples. */
        if (av_new_packet(pkt, st->codecpar->codec_id == AV_CODEC_ID_PCM_MULAW ? 512 : 16) < 0)
            return 6;
        memset(pkt->data, id & 255, pkt->size);
        if (st->codecpar->codec_type == AVMEDIA_TYPE_VIDEO) {
            pkt->data[0] = pkt->data[1] = pkt->data[2] = 0;
            pkt->data[3] = 1; pkt->data[4] = 0x65;
            pkt->flags = AV_PKT_FLAG_KEY;
        }
        pkt->stream_index = stream;
        pkt->dts = av_rescale_q(dts, (AVRational){1,1000}, st->time_base);
        pkt->pts = av_rescale_q(pts, (AVRational){1,1000}, st->time_base);
        pkt->duration = av_rescale_q(st->codecpar->codec_type == AVMEDIA_TYPE_VIDEO ? 40 : 64,
                                   (AVRational){1,1000}, st->time_base);
        ret = av_interleaved_write_frame(s, pkt);
        snapshot(s, step++, ret, pkt->data == NULL && pkt->size == 0);
        if (ret < 0) { packet_error = ret; break; }
    }
    if (argc < 7 || atoi(argv[6])) {
        ret = av_interleaved_write_frame(s, NULL);
        snapshot(s, step, ret, 1);
        if (ret < 0) packet_error = ret;
    }
    ret = av_write_trailer(s);
    printf("{\"event\":\"trailer\",\"ret\":%d,\"queued\":%d}\n", ret,
           ffformatcontext(s)->packet_buffer.head != NULL);
    av_packet_free(&pkt);
    avformat_free_context(s);
    return ret < 0 ? 5 : packet_error ? 6 : 0;
}
