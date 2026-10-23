# 实时 RTSP 静态 PNG 水印指南

## 概览

本指南使用预先生成的透明 PNG，为实时 RTSP 视频添加固定 Logo、文字或
矩阵水印。PNG 输入产生一帧，`hwupload` 将其上传为 RKMPP 硬件帧，
`overlay_rkrga` 在 PNG 输入结束后持续复用末帧。视频仍需逐帧合成并重新编码，
不能与 `-c:v copy` 同时使用。

下列命令用于建立目标板验收基线，不代表已经完成三平台的性能或透明效果验收。
PNG 排版生成和运行中热更新不在本次范围内。

| 项目 | 要求或行为 |
| --- | --- |
| 构建组件 | 新增 `image2` demuxer、`png` decoder 和 `zlib`；复用已启用的 RKMPP、RGA、`hwupload`、`setpts`、`format` |
| 运行库 | 交付目录的 `lib/` 包含 `libz.so.1`，与 MPP/RGA 运行库一起部署 |
| 主视频 | 示例输入 H.264，最终合成为 `1920×1080`、NV12 |
| PNG | 单张、8 bit RGBA、`1920×1080`、透明背景；文字或 Logo 可带抗锯齿 Alpha |
| PNG Alpha | 标准 PNG 使用非预乘 Alpha，合成设置 `alpha_format=straight` |
| 整体不透明度 | 示例 `alpha=51`，约为 20%；与 PNG 自身 Alpha 共同生效 |
| 缺失色彩元数据 | 未标记的 YUV 按 librga 默认 BT.601 limited 转换；已标记的 BT.601/BT.709 和范围继续参与 CSC 配置 |
| 水印布局 | 角度、间距、错行、矩阵偏移及边缘覆盖提前写入 PNG；全屏画布以 `x=0:y=0` 合成 |
| 音频 | 示例使用 `-an`，仅验证视频链路 |

## 准备与构建检查

1. 在仓库根目录构建目标，例如：

   ```bash
   ./build-rockchip.sh rk3576
   ```

   构建配置应包含 `--enable-demuxer=image2`、`--enable-decoder=png` 和
   `--enable-zlib`。构建镜像安装 `zlib1g-dev`；PNG 编码器、`drawtext`、
   `rotate` 和其他图片格式不因本功能加入交付清单。

2. 将完整交付目录复制到目标板。以下命令均在目标板运行，替换本地路径和 URL：

   ```bash
   PACKAGE_DIR=/opt/ffmpeg-rockchip
   FFMPEG="$PACKAGE_DIR/ffmpeg"
   FFPROBE="$PACKAGE_DIR/ffprobe"
   WATERMARK=/opt/watermarks/watermark-1920x1080.png
   SOURCE_URL='rtsp://camera.example:554/live/main'
   OUTPUT_URL='rtsp://media.example:554/live/watermarked'
   ```

   `SOURCE_URL` 必须指向实际摄像头码流；`OUTPUT_URL` 必须指向已运行、允许
   RTSP 发布的媒体服务。FFmpeg 的 RTSP 输出命令不会启动 RTSP 服务端。

3. 确认使用本次构建产物及随包的 zlib：

   ```bash
   "$FFMPEG" -hide_banner -version
   "$FFMPEG" -hide_banner -buildconf
   "$FFMPEG" -hide_banner -h demuxer=image2
   "$FFMPEG" -hide_banner -h decoder=png
   "$FFMPEG" -hide_banner -h filter=overlay_rkrga
   env -u LD_LIBRARY_PATH ldd "$FFMPEG"
   env -u LD_LIBRARY_PATH ldd "$FFPROBE"
   ```

   两次 `ldd` 均应把 `libz.so.1` 解析到此交付目录的 `lib/`，不能出现
   `not found`。若只复制可执行文件或只看到宿主机的 zlib，交付验证不通过。

4. 检查图片格式、尺寸及单帧输入：

   ```bash
   "$FFPROBE" -v error \
     -f image2 -pattern_type none -c:v png \
     -select_streams v:0 -count_frames \
     -show_entries stream=codec_name,width,height,pix_fmt,nb_read_frames \
     -of default=noprint_wrappers=1 "$WATERMARK"
   ```

   期望 `codec_name=png`、`width=1920`、`height=1080`、`pix_fmt=rgba`、
   `nb_read_frames=1`。不符合时先重新生成图片，不依赖裁剪构建自动转换
   palette、灰度、16 bit 或无 Alpha 输入。
   不要仅凭 `ffprobe` 退出码判定成功：无效 PNG 可能返回 0，但会报告解码
   错误，且 `nb_read_frames=N/A`；应同时核对上述字段和错误输出。

## RTSP 转码与水印合成

先定义滤镜图，再启动推流：

```bash
WATERMARK_FILTER='[0:v]setpts=PTS-STARTPTS,scale_rkrga=w=1920:h=1080:format=nv12:async_depth=0[main];[1:v]setpts=PTS-STARTPTS,format=rgba,hwupload[mark];[main][mark]overlay_rkrga=x=0:y=0:alpha=51:alpha_format=straight:format=nv12:repeatlast=1:eof_action=repeat:shortest=0:async_depth=0[v]'

"$FFMPEG" -hide_banner -loglevel verbose \
  -init_hw_device rkmpp=wm -filter_hw_device wm \
  -rtsp_transport tcp \
  -hwaccel rkmpp -hwaccel_device wm -hwaccel_output_format drm_prime \
  -c:v h264_rkmpp -afbc 0 -i "$SOURCE_URL" \
  -f image2 -pattern_type none -c:v png -i "$WATERMARK" \
  -filter_complex "$WATERMARK_FILTER" \
  -map '[v]' -an -c:v h264_rkmpp -b:v 4M -fps_mode passthrough \
  -f rtsp -rtsp_transport tcp "$OUTPUT_URL" \
  2>watermark-rtsp.log
```

输入为 HEVC 时，只将第一个输入前的 `-c:v h264_rkmpp` 改成
`-c:v hevc_rkmpp`；输出仍可使用 `h264_rkmpp`。

| 参数或处理步骤 | 用途与限制 |
| --- | --- |
| `-init_hw_device rkmpp=wm -filter_hw_device wm` | 为解码和 PNG 的 `hwupload` 提供明确的 RKMPP 设备 |
| `-hwaccel_output_format drm_prime` | 保持主视频为硬件帧，满足 RGA 输入要求 |
| `-afbc 0` | 本验收使用线性主视频输入；不混入 AFBC 优化变量 |
| 主视频 `scale_rkrga` | 在合成前完成最终尺寸与 NV12 转换；已是该尺寸和格式时可另行验证省去此步 |
| 两路 `setpts=PTS-STARTPTS` | 将主视频和 PNG 的首帧都对齐到 0，保留主视频后续相对时间间隔 |
| `-f image2 -pattern_type none -c:v png` | 明确按一个静态 PNG 文件读取，不使用 `-loop 1` 持续解码 |
| `format=rgba,hwupload` | 保留 PNG Alpha，再上传为硬件帧；图片格式已由前置检查确认 |
| `repeatlast=1:eof_action=repeat:shortest=0` | PNG EOF 后保持最后一帧，不因单帧图片结束而终止推流 |
| `alpha=51:alpha_format=straight` | 以非预乘 Alpha 混合；PNG 透明区域仍应完整显示主视频 |
| 两处 `async_depth=0` | 建立同步合成验收基线，后续再独立评估并行深度 |
| `-fps_mode passthrough` | 保留滤镜输出时间戳，不主动设定新帧率 |

`alpha=0` 表示水印完全透明，`alpha=255` 保留 PNG 自身 Alpha；中间值只
降低水印图层的不透明度，PNG 透明区域应保持主视频亮度。需要完全省去合成
开销时，在重新启动命令时移除 PNG 输入及合成步骤。`alpha`、`x/y` 等参数
在滤镜初始化时生效；覆盖 PNG 文件不会让已缓存的水印自动变化。

全屏 PNG 应按最终视频尺寸生成并固定 `x=0:y=0`。单个 Logo 可使用更小的
RGBA PNG 和非负 `x/y`，但不属于本例的全屏矩阵验收；不要通过移动整张
全屏画布调整矩阵偏移。

## 目标板验收

1. 持续推流至少 60 秒并播放 `OUTPUT_URL`。确认透明背景没有变成黑色矩形，
   抗锯齿边缘没有明显暗边，20% 不透明度、四角和边缘布局符合预期。
2. 检查 `watermark-rtsp.log` 的两路输入统计：PNG 为一帧，主视频输出帧数
   随时间继续增加。PNG EOF 后不应立即退出或停止出帧。这里的“一帧”指
   输入解码结果；格式探测可能读取文件，不能据此要求文件只被打开一次。
3. 检查日志不存在硬件上下文缺失、图片格式协商失败、RGA 提交失败或时间戳
   倒退。发生失败时保留完整命令、输入格式、板卡和驱动版本及日志。
4. 用相同输入、最终分辨率、帧率、码率、编码参数和运行时长，对比“仅
   `setpts` 与 `scale_rkrga`”的基线和水印版本。记录 CPU、内存、输出帧数、
   丢帧及端到端延迟；多路部署按实际路数重复。PNG 排版一次不等于每帧
   合成免费，全屏透明像素仍占硬件图层尺寸和内存带宽。
5. 用同一静态画面测试 `alpha=0/51/128/255`，与无水印基线比较。PNG 的
   Alpha 0 区域应保持基线颜色；不透明色块的覆盖率应约为 0%、20%、50%、
   100%。PNG 自身 Alpha 128 的区域再乘约 50%，避免只凭水印可见就判定通过。
   同时覆盖色彩矩阵缺失和明确 BT.601/BT.709 的输入，检查透明区域没有色偏。

需要检查结束后的帧数和时长时，可使用同一滤镜图录制 60 秒本地 MP4。
下列命令使用 `-n`，已有文件时直接退出：

```bash
"$FFMPEG" -hide_banner -loglevel verbose -benchmark -n \
  -init_hw_device rkmpp=wm -filter_hw_device wm \
  -rtsp_transport tcp \
  -hwaccel rkmpp -hwaccel_device wm -hwaccel_output_format drm_prime \
  -c:v h264_rkmpp -afbc 0 -i "$SOURCE_URL" \
  -f image2 -pattern_type none -c:v png -i "$WATERMARK" \
  -filter_complex "$WATERMARK_FILTER" \
  -map '[v]' -an -c:v h264_rkmpp -b:v 4M -fps_mode passthrough \
  -t 60 -f mp4 watermark-check.mp4 \
  2>watermark-check.log

"$FFPROBE" -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,duration,avg_frame_rate,nb_frames \
  -of default=noprint_wrappers=1 watermark-check.mp4
```

期望 H.264、`1920×1080`、时长接近 60 秒。恒定 25 fps 输入应约为 1500
帧；实际帧数应结合源流帧率与日志判断。录制检查只覆盖本地编码文件，
RTSP 发布和播放端延迟仍需按前述步骤单独验收。

| 症状 | 先检查的原因 | 处理 |
| --- | --- | --- |
| `Unknown input format: image2` 或缺少 PNG decoder | 仍在运行旧产物或配置不完整 | 检查 `-version`、`-buildconf`，部署整个新交付目录 |
| `libz.so.1` 找不到或解析到宿主机 | 漏复制运行库或加载路径异常 | 检查 `lib/` 与 `ldd` 的实际路径 |
| `A hardware device reference is required` | 没有为 `hwupload` 指定设备 | 保留 `-init_hw_device` 与 `-filter_hw_device` |
| `No hw context provided` | 主视频或水印仍是软件帧 | 确认 RKMPP 解码输出及 PNG 的 `hwupload` |
| 水印黑底、颜色或暗边异常 | PNG 无有效 Alpha、格式不符或 Alpha 模式不匹配 | 先检查 `pix_fmt=rgba` 和原图，再检查 `alpha_format=straight` |
| 开始后立即退出 | 把 PNG EOF 当作全部结束或 RTSP 服务拒绝发布 | 检查 `repeatlast`、`eof_action`、`shortest` 和服务端日志 |
| 修改文件或参数后画面未更新 | 现有进程已持有初始化参数和末帧 | 重新启动流水线加载新 PNG 与参数 |
