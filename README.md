ffmpeg-rockchip
=============
This project aims to provide full hardware transcoding pipeline in FFmpeg CLI for Rockchip platforms that support MPP ([Media Process Platform](https://github.com/rockchip-linux/mpp)) and RGA ([2D Raster Graphic Acceleration](https://github.com/airockchip/librga)). This includes hardware decoders, encoders and filters. A typical target platform is RK3588/3588s based devices.

## Hightlights
* MPP decoders support up to 8K 10-bit H.264, HEVC, VP9 and AV1 decoding
* MPP decoders support producing AFBC (ARM Frame Buffer Compression) image
* MPP decoders support de-interlace using IEP (Image Enhancement Processor)
* MPP decoders support allocator half-internal and pure-external modes
* MPP encoders support up to 8K H.264 and HEVC encoding
* MPP encoders support async encoding, AKA frame-parallel
* MPP encoders support consuming AFBC image
* RGA filters support image scaling and pixel format conversion
* RGA filters support image cropping
* RGA filters support image transposing
* RGA filters support blending two images
* RGA filters support async operation
* RGA filters support producing and consuming AFBC image
* Zero-copy DMA in above stages

## How to use
The documentation is available on the [Wiki](https://github.com/nyanmisaka/ffmpeg-rockchip/wiki) page of this project.

## Linux ARM64 builds

The release build supports `rk3588`, `rk3576`, and `rv1126b`, and produces only
Linux ARM64 deliverables. It runs in an Ubuntu 22.04 ARM64 container shared by
local builds and GitHub Actions. The Docker client may run on macOS or Linux,
but the Docker server must be Linux ARM64; native host builds are not supported.

Build one target from the repository root:

```bash
./build-rockchip.sh rk3588
```

The runtime and its manifest are written to:

```text
artifact/ffmpeg-rockchip-<target>-ubuntu22-arm64/
└── MANIFEST.mtree
```

Local builds leave this directory unpacked; GitHub Actions additionally creates
and directly uploads a branch-qualified `.tar.gz` archive.

See the [Rockchip FFmpeg build and maintenance guide](build/rockchip/README.md)
for the file map, cache behavior, dependency updates, verification, and
troubleshooting.

## Region-of-interest encoding

On RK3588 and RK3576, the FFmpeg 8.1 build supports up to eight regions of
interest per frame with the `h264_rkmpp` and `hevc_rkmpp` encoders. A negative
`qoffset` gives a region more quality and a positive value gives it less
quality. The accepted range is `-1` through `1`.

For a fixed ROI, use FFmpeg's `addroi` filter. Multiple filters add multiple
regions:

```bash
./ffmpeg -i input.mp4 \
  -vf "addroi=x=0:y=0:w=iw/2:h=ih:qoffset=-1/4,\
addroi=x=iw/2:y=0:w=iw/2:h=ih:qoffset=1/4" \
  -c:v h264_rkmpp -b:v 4M -an output.mp4
```

For live detections on RK3588, place the `frame_decision_roi` filter after the
final size-changing filter and immediately before the RKMPP encoder. In a
Stream Forge managed pipeline, the command only needs the product channel;
target weights and manual rectangles are updated through the control socket:

```bash
./ffmpeg -rtsp_transport tcp -c:v h264_rkmpp -afbc 0 -i input \
  -vf "scale_rkrga=w=1920:h=1080:format=nv12:afbc=0,\
frame_decision_roi=channel=5" \
  -c:v hevc_rkmpp \
  -b:v 4M -f rtsp rtsp://127.0.0.1/live/camera
```

For standalone FFmpeg, use the raw detector channel and a static target
policy. Add `:dev=1` only when the final composed rectangles need to be drawn
on the encoded picture:

```bash
./ffmpeg -rtsp_transport tcp -c:v h264_rkmpp -afbc 0 -i input \
  -vf "scale_rkrga=w=1920:h=1080:format=nv12:afbc=0,\
frame_decision_roi=channel_id=0:\
targets=face=-3/20|head=-3/20|motor_vehicle=-1/10" \
  -c:v hevc_rkmpp \
  -b:v 4M -f rtsp rtsp://127.0.0.1/live/camera
```

The frame-decision client library defaults to
`/opt/frame-decision-service/lib/libframe_decision_client.so.2`, and its V3
service socket defaults to `/run/frame-decision/frame-decision-v3.sock`. The
filter maps generic model targets to independent weights through
`target_qoffsets`, caps the normalized set at eight non-overlapping regions,
and rejects detector results that miss the source-frame deadline configured by
`ttl_ms` (1500 ms by default). Only non-zero targets are enabled on the detector
connection, allowing frame-decision to skip unrelated models. A fresh accepted
non-empty result may be reused for `reuse_ttl_ms` (1000 ms by default), measured
from result receipt, so multi-channel inference cadence does not create gaps in
the encoder ROI state. A successful empty detection clears that state after
`miss_hold_ms` (600 ms by default), without refreshing the reuse window; set
`miss_hold_ms=0` to clear immediately. Failed, expired, or missing results also
do not refresh it. Set `debug_overlay=1` only during development to draw the
same selected rectangles into a separate RGA output frame. See the
`frame_decision_roi` filter documentation for all options and input layout
requirements.

ROI metadata follows the asynchronous RKMPP frame queue, so a new detection can
become visible after frames already queued for encoding. Managed target,
weight, and manual-rectangle changes are applied from atomic control snapshots
without rebuilding the filter graph or encoder. In standalone mode, the static
`targets` policy remains fixed for the lifetime of the filter graph.
Internally RK3588 uses MPP's ROI2 block-map path, while RK3576 uses the legacy
eight-region path. Both consume the same AVFrame ROI side-data contract, but
block alignment can make the exact affected pixel boundary differ.

## Codecs and filters
### Decoders/Hwaccel
```
 V..... av1_rkmpp            Rockchip MPP (Media Process Platform) AV1 decoder (codec av1)
 V..... h263_rkmpp           Rockchip MPP (Media Process Platform) H263 decoder (codec h263)
 V..... h264_rkmpp           Rockchip MPP (Media Process Platform) H264 decoder (codec h264)
 V..... hevc_rkmpp           Rockchip MPP (Media Process Platform) HEVC decoder (codec hevc)
 V..... mjpeg_rkmpp          Rockchip MPP (Media Process Platform) MJPEG decoder (codec mjpeg)
 V..... mpeg1_rkmpp          Rockchip MPP (Media Process Platform) MPEG1VIDEO decoder (codec mpeg1video)
 V..... mpeg2_rkmpp          Rockchip MPP (Media Process Platform) MPEG2VIDEO decoder (codec mpeg2video)
 V..... mpeg4_rkmpp          Rockchip MPP (Media Process Platform) MPEG4 decoder (codec mpeg4)
 V..... vp8_rkmpp            Rockchip MPP (Media Process Platform) VP8 decoder (codec vp8)
 V..... vp9_rkmpp            Rockchip MPP (Media Process Platform) VP9 decoder (codec vp9)
```

### Encoders
```
 V..... h264_rkmpp           Rockchip MPP (Media Process Platform) H264 encoder (codec h264)
 V..... hevc_rkmpp           Rockchip MPP (Media Process Platform) HEVC encoder (codec hevc)
 V..... mjpeg_rkmpp          Rockchip MPP (Media Process Platform) MJPEG encoder (codec mjpeg)
```

### Filters
```
 ... addroi           V->V       Add region of interest to frame
 ... overlay_rkrga     VV->V      Rockchip RGA (2D Raster Graphic Acceleration) video compositor
 ... scale_rkrga       V->V       Rockchip RGA (2D Raster Graphic Acceleration) video resizer and format converter
 ... vpp_rkrga         V->V       Rockchip RGA (2D Raster Graphic Acceleration) video post-process (scale/crop/transpose)
```

## Important
* Rockchip BSP/vendor kernel is necessary, 5.10 and 6.1 are two tested versions.
* For the supported maximum resolution and FPS you can refer to the datasheet or TRM.
* User MUST be granted permission to access these device files.
```
# DRM allocator
/dev/dri

# DMA_HEAP allocator
/dev/dma_heap

# RGA filters
/dev/rga

# MPP codecs
/dev/mpp_service

# Optional, for compatibility with older kernels and socs
/dev/iep
/dev/mpp-service
/dev/vpu_service
/dev/vpu-service
/dev/hevc_service
/dev/hevc-service
/dev/rkvdec
/dev/rkvenc
/dev/vepu
/dev/h265e
```

## Todo
* Support MPP VP8 video encoder
* ...

## Acknowledgments

@[hbiyik](https://github.com/hbiyik) @[HermanChen](https://github.com/HermanChen) @[rigaya](https://github.com/rigaya)

## Disclaimer

This project is an UNOFFICIAL fork of FFmpeg and is NOT the property of Rockchip Electronics Co., Ltd.
The MPP and RGA components used are compatible with FFmpeg by enabling the LGPLv3 license: "./configure --enable-version3".

---

FFmpeg README
=============

FFmpeg is a collection of libraries and tools to process multimedia content
such as audio, video, subtitles and related metadata.

## Libraries

* `libavcodec` provides implementation of a wider range of codecs.
* `libavformat` implements streaming protocols, container formats and basic I/O access.
* `libavutil` includes hashers, decompressors and miscellaneous utility functions.
* `libavfilter` provides means to alter decoded audio and video through a directed graph of connected filters.
* `libavdevice` provides an abstraction to access capture and playback devices.
* `libswresample` implements audio mixing and resampling routines.
* `libswscale` implements color conversion and scaling routines.

## Tools

* [ffmpeg](https://ffmpeg.org/ffmpeg.html) is a command line toolbox to
  manipulate, convert and stream multimedia content.
* [ffplay](https://ffmpeg.org/ffplay.html) is a minimalistic multimedia player.
* [ffprobe](https://ffmpeg.org/ffprobe.html) is a simple analysis tool to inspect
  multimedia content.
* Additional small tools such as `aviocat`, `ismindex` and `qt-faststart`.

## Documentation

The offline documentation is available in the **doc/** directory.

The online documentation is available in the main [website](https://ffmpeg.org)
and in the [wiki](https://trac.ffmpeg.org).

### Examples

Coding examples are available in the **doc/examples** directory.

## License

FFmpeg codebase is mainly LGPL-licensed with optional components licensed under
GPL. Please refer to the LICENSE file for detailed information.

## Contributing

Patches should be submitted to the ffmpeg-devel mailing list using
`git format-patch` or `git send-email`. Github pull requests should be
avoided because they are not part of our review process and will be ignored.
