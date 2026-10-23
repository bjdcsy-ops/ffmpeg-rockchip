# 实时 RTSP 静态 PNG 水印指南

## 概览

本指南使用预先生成的透明 PNG，为实时 RTSP 视频添加固定 Logo、文字或
矩阵水印。PNG 输入产生一帧，`hwupload` 将其上传为 RKMPP 硬件帧，
`overlay_rkrga` 在 PNG 输入结束后持续复用末帧。视频仍需逐帧合成并重新编码，
不能与 `-c:v copy` 同时使用。

Mac 一键演示会在本机生成文字 PNG，通过 SSH 启动独立的板端测试链路；
摄像头 RTSP 输入使用后文的手工命令。下列步骤用于建立目标板验收基线，
不代表已经完成三平台的性能或透明效果验收。Java 服务集成和运行中热更新
不在本次范围内。

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

## Mac 一键演示

`watermark-demo.py` 支持普通文字、水平矩阵和斜向矩阵三种布局。演示固定
使用板端现有的 `1920×1080`、25 fps、H.264 MP4 测试画面；输出为 H.264、
4 Mbit/s、GOP 25、无音频。当前入口不接受摄像头 URL。

### 准备

1. 在仓库根目录准备 Python 3.8 或更新版本、OpenSSH 客户端，以及可用的
   `RK3576` SSH 配置。脚本使用非交互认证，先确认下列命令可以直接完成：

   ```bash
   ssh -o BatchMode=yes RK3576 'python3 --version'
   ```

   板端需要 Python 3.8 或更新版本、可运行的 MPP/RGA 环境，以及已有的
   `/opt/zlmediakit/MediaServer`。板端 worker 仅使用 Python 标准库。

2. 在 Mac 创建虚拟环境，一次安装 PNG 排版依赖：

   ```bash
   python3 -m venv /private/tmp/ffmpeg-watermark-venv
   source /private/tmp/ffmpeg-watermark-venv/bin/activate
   python -m pip install -r build/rockchip/watermark-demo-requirements.txt
   ```

   requirements 要求 `Pillow>=9.1,<13`。后续终端只需激活同一虚拟环境；
   演示脚本不会自动安装依赖或构建 FFmpeg。

3. 准备完整的本地交付目录，默认是
   `artifact/ffmpeg-rockchip-rk3576-ubuntu20-arm64/`，包含 `ffmpeg`、
   `ffprobe`、`BUILDINFO.txt` 和完整 `lib/`。脚本每次上传此目录，
   不使用板端已有的生产 FFmpeg。

4. 确认板端测试文件和媒体服务存在：

   ```bash
   ssh RK3576 'test -f /run/media/mmcblk0p8/ffmpeg-watermark-demo/source.mp4 && test -x /opt/zlmediakit/MediaServer'
   ```

   `source.mp4` 是预先放到板端稳定路径的测试画面，不随脚本上传。使用其他
   测试文件时通过 `--source-file` 指定板端绝对路径，文件仍须为 H.264、
   `1920×1080`、25 fps。使用 `--open` 前，在 Mac 安装 VLC。

### 三种布局

在三个终端分别激活上述虚拟环境，执行以下命令可并行观察三种布局。
每次运行使用独立的远端目录、媒体服务和 TCP 端口，并各自打开新的 VLC
实例。默认运行上限为 3600 秒；在对应终端按 Ctrl+C 提前停止。

普通文字：

```bash
python build/rockchip/watermark-demo.py \
  --mode single --text '普通水印' --font-size 44 --alpha 51 --open
```

水平矩阵：

```bash
python build/rockchip/watermark-demo.py \
  --mode matrix --text '水平矩阵水印' --gap-x 56 --gap-y 148 --alpha 51 --open
```

斜向矩阵：

```bash
python build/rockchip/watermark-demo.py \
  --mode diagonal --text '斜向矩阵水印' --angle 30 --alpha 51 --open
```

省略 `--open` 时只打印 `rtsp://127.0.0.1:<端口>/live/watermark`，可手工在
VLC 中打开并使用 RTSP over TCP。预览 URL 仅在脚本前台运行期间有效，
无需保持旧的 18554 会话。

### 参数

| 参数 | 默认值 | 行为与限制 |
| --- | --- | --- |
| `--mode` | `single` | `single` 普通文字、`matrix` 水平矩阵、`diagonal` 斜向矩阵 |
| `--text` | `RTSP WATERMARK` | 非空单行文字，最多 256 个字符；中文字形取决于字体 |
| `--font` | Mac 的 `Arial Unicode.ttf` | 本机 TrueType/OpenType 字体路径；默认 `/System/Library/Fonts/Supplemental/Arial Unicode.ttf`，支持中文，缺失时显式指定 |
| `--font-size` | `44` | 输出像素字号，整数 `8..256` |
| `--alpha` | `51` | 整体不透明度，整数 `0..255`；51 约为 20%，0 完全透明，255 保留文字抗锯齿 Alpha |
| `--angle` | `30` | `-90..90` 度，正值逆时针；仅 `diagonal` 生效，围绕画面中心旋转矩阵 |
| `--gap-x` | `56` | 文字实际可见包围盒之间的水平空隙，整数 `0..1920` 像素 |
| `--gap-y` | `148` | 文字实际可见包围盒之间的垂直空隙，整数 `0..1080` 像素 |
| `--x` | `32` | `single` 可见文字包围盒左边缘；矩阵布局为网格相位，整数 `0..1919` 像素 |
| `--y` | `32` | `single` 可见文字包围盒上边缘；矩阵布局为网格相位，整数 `0..1079` 像素 |
| `--host` | `RK3576` | SSH 主机别名或 `user@host`，使用非交互认证 |
| `--package-dir` | `artifact/ffmpeg-rockchip-rk3576-ubuntu20-arm64` | 本机完整交付目录；默认路径相对仓库根目录定位 |
| `--remote-root` | `/run/media/mmcblk0p8/ffmpeg-watermark-demo` | 板端运行目录的父目录，必须为绝对路径 |
| `--source-file` | `<remote-root>/source.mp4` | 板端已有测试 MP4 的绝对路径 |
| `--media-server` | `/opt/zlmediakit/MediaServer` | 板端已有 ZLMediaKit 可执行文件的绝对路径 |
| `--local-port` | `0` | 本机 loopback 转发端口；0 自动选择空闲端口，也可指定 `1..65535` |
| `--duration` | `3600` | 准备完成后的自动停止时限，整数 `10..3600` 秒 |
| `--output-dir` | 自动创建本机临时目录 | 指定时必须是尚不存在的新目录，保存 PNG、配置和本机证据 |
| `--open` | 关闭 | 在 Mac 启动新的 VLC 实例；默认只打印 URL |

`single` 要求整段文字完整放入画面；字号、文字长度或坐标导致越界时直接
报错。矩阵以文字实际包围盒加间隙形成周期，网格向四周延伸后裁剪到画面，
因此边缘可能显示部分文字。斜向矩阵先补齐旋转所需范围，再旋转和裁剪。
PNG 保存文字形状与抗锯齿 Alpha，整体 `--alpha` 只在 RGA 合成时应用一次。

### 生命周期与证据

每次运行上传完整 runtime、生成的 PNG、配置与 worker 到唯一的
`<remote-root>/run-<随机标识>/`。worker 自建只监听板端 loopback 的 ZLMediaKit，
循环发布测试文件，再以 RKMPP/RGA 解码、合成和重编码；Mac 通过 SSH TCP
转发读取水印流。演示不替换生产程序或修改生产媒体服务配置。

Ctrl+C、运行时限到达或控制连接结束后，worker 停止本次创建的媒体服务、
源推流和水印进程，主机关闭本次 SSH 连接和转发。VLC 窗口可手工关闭。
日志和证据目录保留，不在清理时删除；多次演示后可按保留策略手工归档或删除。

| 位置 | 主要内容 |
| --- | --- |
| 启动时打印的 `Local evidence` | `watermark.png`、`config.json`、`demo.json`、`ssh.log`、`tunnel.log`；收到远端停止事件后写入 `stopped.json`，启动失败时可能只有部分文件 |
| 打印的 `Remote evidence` | `runtime/`、`evidence.json`、`commands.json`、`pids.json`、`summary.json`、`watermark-progress.txt`、各子进程 stdout/stderr 与媒体服务日志；启动失败时可能只有部分文件 |

`evidence.json` 记录构建来源、运行库与 PNG 的 SHA-256、图片和测试文件格式；
`commands.json` 记录实际子进程命令。排查时保留两端目录，不仅保留播放截图。

### 离线测试与故障定位

不连接目标板的控制器与 worker 测试只使用 Python 标准库，已接入
`tools/check.sh`。在仓库根目录可单独运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s build/rockchip/tests -p 'test_watermark_control.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s build/rockchip/tests -p 'test_watermark_remote.py'
```

图像布局测试需要先激活已安装 Pillow 的虚拟环境，单独执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover \
  -s build/rockchip/tests -p 'test_watermark_image.py'
```

这些测试覆盖参数、命令、生命周期和 PNG 布局，不证明目标板驱动、RTSP
发布或实际透明效果通过。首次使用仍应观察三种布局，并按后文进行板端验收。

| 症状 | 检查与处理 |
| --- | --- |
| 提示缺少 Pillow 或版本过低 | 激活虚拟环境，用该环境的 `python -m pip install -r build/rockchip/watermark-demo-requirements.txt` 安装依赖 |
| SSH 认证或上传失败 | 先单独执行准备步骤中的非交互 SSH 命令，检查主机别名、密钥、远端 Python 和目录写权限 |
| `Incomplete runtime package` 或版本检查失败 | 指向完整交付目录；检查 `BUILDINFO.txt`、`lib/` 和板端运行库兼容性 |
| `source_file` 不存在或格式不符 | 检查板端路径，使用 `1920×1080`、25 fps 的 H.264 MP4；入口不接受摄像头 URL |
| 字体加载失败、文字缺字或单行越界 | 用 `--font` 指定含对应字形的本机字体，减少文字、字号或调整 `--x/--y` |
| 媒体服务或水印进程提前退出、启动超时 | 查看远端 `commands.json` 和对应 stderr，确认 MediaServer 路径、MPP/RGA 设备与 runtime 可用 |
| 本地转发失败 | 查看 `tunnel.log`；保留默认 `--local-port 0` 或换空闲端口后重试 |
| VLC 没有打开或无法播放 | 确认 Mac 已安装 VLC，先用打印的 URL 手工打开并选择 RTSP over TCP；同时检查脚本仍在运行 |

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
   可额外加 `-xerror` 做短时检查，使非单调 DTS 直接返回失败；需要定位时，
   加 `-debug_ts -loglevel debug`，并在 RTSP 输入前加 `-fdebug ts`，对照修正前的输入、解码、
   编码时间戳。最终 MP4 的单调时间戳不能证明输入时间戳正确。
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
