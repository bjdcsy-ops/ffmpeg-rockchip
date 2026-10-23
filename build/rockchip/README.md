# Rockchip FFmpeg 构建与维护指南

## 概览

本目录包含项目的 Rockchip FFmpeg 构建实现。本地构建和 GitHub Actions
使用相同的 Dockerfile 与脚本，构建逻辑在这里维护，工作流负责 CI 调度、
缓存和产物发布。

仓库维护 FFmpeg 8.1 和 6.1 两个版本分支，支持 `rk3588`、`rk3576` 和
`rv1126b`，构建与交付仅面向 Linux ARM64。

## RTSP IPC 输入兼容范围

构建使用显式组件清单，不使用 `--enable-decoder=all`。这样可以控制交付物
大小和不可信码流的解析面，同时由构建检查保证清单中的组件确实进入产物。

| IPC 输入类型 | FFmpeg 组件 | RTP/RTSP 说明 |
| --- | --- | --- |
| H.264、H.265 | `h264_rkmpp`、`hevc_rkmpp` | MPP 硬件解码 |
| MJPEG、MPEG-4 Part 2 | `mjpeg_rkmpp`、`mpeg4_rkmpp` | MPP 硬件解码；用于 MJPEG 子码流及旧款 IPC |
| AAC | `aac`、`aac_latm` | RTSP 支持 `MPEG4-GENERIC` 及 RTP handler 可处理的 `MP4A-LATM` 子集（常见 `cpresent=0`）；其它容器取决于已启用 demuxer |
| G.711 | `pcm_alaw`、`pcm_mulaw` | 支持 RTP `PCMA`、`PCMU` |
| G.722、G.726 | `g722`、`g726`、`g726le` | configure 名称带 `adpcm_` 前缀；同时支持 G.726 的 RFC 与 AAL2 位序 |
| Opus | `opus` | 支持动态 RTP `opus` |
| MPEG 音频 | `mp2`、`mp3` | 支持标准 RTP `MPA`；厂商私有名称需要单独适配 |
| PCM | `pcm_s16be/le`、`pcm_s24be/le` | 标准 RTP `L16`、`L24` 使用大端；小端用于厂商或容器输入 |
| G.723.1 | `g723_1` | 支持标准 RTP 静态负载类型 4 |

下列音频不能仅靠增加 decoder 就宣称 RTSP 可用：

- G.722.1 对应 FFmpeg 的 `siren` decoder，但当前缺少 G.722.1 RTP 映射。
- G.729 虽有 decoder，但当前 RTP 静态负载类型 18 未绑定可用 codec。
- `MP2L2`、`PCM` 等厂商私有 SDP 名称可能不等同于标准 `MPA`、`L16`
  或 `L24`。

这些格式需要取得真实 IPC 的 SDP 和 RTP 样本，补充并验证 payload 映射、
时钟频率、声道数及分帧方式后再加入兼容清单。交付验证只能证明组件已注册；
新增输入格式仍需在目标板上完成 RTSP 到 AAC 的端到端测试。

固定水印使用 `image2` demuxer、`png` decoder 和 `zlib` 读取单张透明 PNG，
再由已有的 `hwupload`、`overlay_rkrga` 完成硬件合成；运行时随包交付
`libz.so.1`。输入约束、RTSP 命令及目标板验收步骤见
[实时 RTSP 静态 PNG 水印指南](STATIC-WATERMARK.md)。

## Mac 一键水印演示

`watermark-demo.py` 在 Mac 生成文字 PNG，通过 SSH 上传完整 runtime，
在目标板启动独立的 loopback 媒体服务、测试源和水印流水线。支持普通文字、
水平矩阵和斜向矩阵；默认复用板端测试文件，不接受摄像头 URL。

在仓库根目录准备 Python 3.8 或更新版本，并一次安装本机排版依赖：

```bash
python3 -m venv /private/tmp/ffmpeg-watermark-venv
source /private/tmp/ffmpeg-watermark-venv/bin/activate
python -m pip install -r build/rockchip/watermark-demo-requirements.txt
```

默认使用 `RK3576` SSH 别名、本机
`artifact/ffmpeg-rockchip-rk3576-ubuntu20-arm64/` 完整交付目录，以及板端
`/run/media/mmcblk0p8/ffmpeg-watermark-demo/source.mp4`。准备好板端
Python、ZLMediaKit 和 `1920×1080`、25 fps 的 H.264 测试文件后运行：

```bash
python build/rockchip/watermark-demo.py --mode single --text 'RTSP WATERMARK' --open
```

`--open` 需要 Mac 安装 VLC；省略时只打印经 SSH 转发的本机 RTSP URL。
脚本前台运行，Ctrl+C 或到达 `--duration` 时限后停止本次创建的子进程与
转发，保留两端证据目录。默认时限 3600 秒，范围 `10..3600` 秒。
脚本不自动安装依赖、构建 FFmpeg 或替换生产程序。准备步骤、三模式并行
示例、全部参数、离线测试与故障定位见
[一键演示指南](STATIC-WATERMARK.md#mac-一键演示)。

## 快速开始

Docker 客户端可以运行在 macOS 或 Linux，但 Docker 服务端必须是 Linux
ARM64。宿主机只负责启动容器，不会直接编译 FFmpeg。以下命令均在项目
根目录执行。

构建单个目标：

```bash
./build-rockchip.sh rk3576
```

依次构建全部目标：

```bash
./build-rockchip.sh all
```

清理某个目标的构建目录和缓存：

```bash
./build-rockchip.sh clean rk3576
```

本地交付物位于 `artifact/ffmpeg-rockchip-<target>-ubuntu22-arm64/`，内部
包含 `MANIFEST.mtree`。`.tar.gz` 只由 GitHub Actions 在上传前生成。

## 构建如何工作

1. 根目录的 `build-rockchip.sh` 检查 Docker 服务端架构，构建或复用
   Ubuntu 22.04 ARM64 镜像，并把源码目录挂载到容器。
2. 容器入口 `build.sh` 读取目标配置和源码状态，按锁文件及仓库维护的
   MPP ROI 补丁构建或复用 MPP、RGA。
3. `build-ffmpeg.sh` 执行 FFmpeg configure，并编译、安装 `ffmpeg` 和
   `ffprobe`，同时确认要求的协议、编解码器和滤镜已启用。
4. `lib/package-runtime.sh` 生成运行时目录、构建信息和 manifest，并完成
   动态库、组件及产物结构检查。GitHub Actions 在此基础上完成归档和发布。

## 文件说明

### 常用构建文件

| 文件 | 作用 | 修改它的典型场景 |
| --- | --- | --- |
| `build-ffmpeg.sh` | FFmpeg configure 参数、编译安装命令和组件检查 | 增减协议、编解码器、滤镜或编译选项 |
| `config.sh` | 目标列表、CPU 参数、依赖仓库和缓存键计算 | 新增目标或调整目标参数 |
| `dependencies.lock` | MPP、RGA 的锁定提交 | 查看当前锁定版本；更新时使用 `tools/update-dependencies.sh` |
| `patches/mpp-roi-utils-public.patch` | 公开锁定 MPP 的 ROI 接口；保留 RK3588 ROI2，并支持 RK3576/RV1126B legacy rectangle 后端与 CQP ROI | 更新 MPP 或 ROI 后端时重新移植并验证补丁 |
| `build.sh` | 容器内依赖、缓存、FFmpeg 和交付物构建编排 | 调整构建阶段或执行顺序 |
| `Dockerfile` | Ubuntu 22.04 ARM64 构建环境和检查工具 | 增减系统依赖或升级工具 |

FFmpeg 的 configure、make、ROI 无硬件测试和 install 命令集中在
`build-ffmpeg.sh`，修改 FFmpeg 构建能力时通常从这里开始。

### 水印演示文件

| 文件 | 作用 |
| --- | --- |
| `watermark-demo.py` | Mac 主机入口，上传 runtime、管理 SSH 转发与预览生命周期 |
| `watermark_image.py` | 使用本机 Pillow 生成普通文字、水平矩阵和斜向矩阵 RGBA PNG |
| `watermark_remote.py` | 仅使用标准库的板端 worker，管理独立媒体服务、FFmpeg 和证据 |
| `watermark-demo-requirements.txt` | 本机图片依赖 `Pillow>=9.1,<13`，不安装到板端 |
| `tests/test_watermark_control.py`、`tests/test_watermark_remote.py` | 标准库离线测试，接入 `tools/check.sh` |
| `tests/test_watermark_image.py` | 需要 Pillow 的图像布局测试，单独执行 |
| `STATIC-WATERMARK.md` | 一键演示、手工摄像头 RTSP 合成与板端验收指南 |

### `lib/`

`lib/` 中的脚本由构建入口加载，不作为日常命令直接执行。

| 文件 | 作用 |
| --- | --- |
| `lib/build-dependencies.sh` | 编译并安装锁定版本的 MPP、RGA |
| `lib/compiler.sh` | 接入 ccache，并为编译设置稳定的随机种子 |
| `lib/git-helpers.sh` | Git 网络重试、精确提交下载和提交校验 |
| `lib/package-runtime.sh` | 生成运行时布局、构建信息、manifest 并执行交付验证 |

### `tools/`

| 文件 | 作用 |
| --- | --- |
| `tools/check.sh` | 检查 Shell、工作流、Dockerfile 和 FFmpeg 组件配置 |
| `tools/compare-artifacts.sh` | 验证 `.mtree` manifest，或比较两个交付目录 |
| `tools/resolve-dependencies.sh` | 为 Actions 输出缓存键、缓存路径和构建器指纹 |
| `tools/target-info.sh` | 为 Actions 输出目标对应的产物名称 |
| `tools/test-build-state.sh` | 无网络验证依赖哈希边界、RGA 环境隔离和 FFmpeg stamp 状态迁移 |
| `tools/update-dependencies.sh` | 查询维护分支并更新依赖锁文件 |

[`tests/roi/`](tests/roi/README.md) 保留的是旧 `-roi_socket` 协议的历史验收
记录和分析脚本。该 encoder 参数已从当前实现删除，旧 `run-*-test.sh`
不能用于验证当前动态 ROI；`tools/check.sh` 对其中 Python 模块的自检也不代表
当前真机链路验收通过。

当前动态 ROI 使用 `frame_decision_roi` → frame-decision V3 DMA-BUF 检测 →
AVFrame ROI metadata → RKMPP encoder。Stream Forge 管理模式通过 SFRO control
socket 更新 target 权重和手工区域；独立 FFmpeg 使用 `control_socket=`、
`channel_id` 和当前 bundle 支持的静态 target。参数示例见
[项目 README 的 ROI 章节](../../README.md#region-of-interest-encoding)。

### 三平台 ROI 后端与验收边界

| 目标 | MPP 后端 | metadata | QP 边界 |
| --- | --- | --- | --- |
| `rk3588` | VEPU580 ROI2 block map | `KEY_ROI_DATA2` / `MppEncROICfg2` | 保留现有 ROI2 分区 QP 行为 |
| `rk3576` | VEPU510 legacy rectangles | `KEY_ROI_DATA` / `MppEncROICfg` | 仅非空 CQP ROI 放开为 0–51；背景 picture QP 固定 |
| `rv1126b` | VEPU511 legacy rectangles | `KEY_ROI_DATA` / `MppEncROICfg` | 仅非空 CQP ROI 放开为 0–51；背景 picture QP 固定 |

仅明确支持表中的三种 SoC，RV1126B 不等同于 RV1126。三平台编码器的硬件区域
上限均为 8；直接 AVFrame 输入超限时警告并使用前 8 区。filter 会裁剪、按 16 像素
网格向外对齐、合并重叠区域并限制为 8 区，最终硬件边界可能受 block 对齐影响。
直接输入重叠 AVFrame ROI 会被拒绝。

`qoffset` 接受 `[-1,1]`，转换为四舍五入的 `[-51,51]` QP delta。
RK3576/RV1126B 的 CQP ROI 扩展只在当前帧存在非空 ROI 时生效；无 ROI 帧沿用
原路径。CBR/AVBR 保留 frame QP 上下限和原 rate-control 行为，ROI delta 可能被
`qp_min`/`qp_max` 或 I-frame 上下限裁剪，不能将 CQP 的强度结果直接推断为 CBR/AVBR。

当前真机验收应使用新链路，分别检查 H264/HEVC 的固定 ROI、空结果清除、目标筛选、
实时帧数和 encoder metadata；画质比较使用相同输入和参数，并把 CQP 与 CBR/AVBR
分开记录。完整参考源生成和区域指标分析可使用系统 FFmpeg，不扩大交付物组件集合。

目录外还有两个相关入口：

| 文件 | 作用 |
| --- | --- |
| `build-rockchip.sh` | 开发者和 GitHub Actions 共用的构建入口 |
| `.github/workflows/rockchip-build.yml` | `6.1`、`8.1` 构建、缓存、归档和产物发布 |

升级 FFmpeg、Ubuntu 或目标 CPU 基线会同时影响兼容性、缓存和交付物，适合
作为独立改动处理，并重新验证所有目标。

## 依赖与缓存

### 更新 MPP、RGA

普通构建只读取 `dependencies.lock` 中的完整提交，不会在构建时跟随远程
分支。需要升级 MPP、RGA 时，先更新锁文件：

```bash
./build/rockchip/tools/update-dependencies.sh
```

然后检查实际变化：

```bash
git diff -- build/rockchip/dependencies.lock
```

确认提交与上游维护分支符合预期后，再把锁文件纳入提交。

### 依赖缓存

依赖缓存键由锁定的 MPP/RGA 提交 SHA 和依赖输入哈希组成。依赖输入哈希
覆盖依赖构建脚本、MPP ROI 补丁、目标编译参数以及实际 builder
fingerprint。任一输入发生变化都会生成新的缓存目录，因此日常修改不需要
人工递增缓存版本。

如果修改了 `lib/build-dependencies.sh`、
`patches/mpp-roi-utils-public.patch` 或目标编译参数，下一次构建重新编译
MPP、RGA 是预期结果。未修改这些输入时，日志中应出现
`Rockchip dependencies: cache hit`。

RGA 的 `CC`、`CXX`、`CFLAGS`、`CXXFLAGS` 和 `LDFLAGS` 只在其 Meson
构建子进程中生效，不会泄漏到后续 FFmpeg configure。因此依赖缓存是冷缓存
还是热缓存，不应改变 FFmpeg 的编译参数。

### ccache

ccache 按目标保存到 `.rockchip-cache/ccache/<target>/`。GitHub Actions 的
恢复和保存步骤都使用 `tools/resolve-dependencies.sh` 输出的同一个路径，
避免两处配置发生偏差。Actions 优先恢复本周最近一次成功构建的缓存，并用
源码提交标识保存新的缓存快照，使后续提交可以继续获得新增的编译结果。

`./build-rockchip.sh clean <target>` 会同时删除依赖缓存和 ccache。验证增量
构建或编译参数是否生效时，应保留缓存并直接重新构建。

### FFmpeg 构建配置 stamp

`.build/rockchip/<target>/ffmpeg/.rockchip-config-stamp` 记录 FFmpeg
configure 参数、受控编译环境、编译器包装器、依赖缓存键、工具链指纹以及
`SOURCE_DATE_EPOCH` 等构建输入。

- stamp 不变时保留 `FFMPEG_BUILD_DIR`，继续使用 Make 增量构建。
- stamp 变化时只清理该目标的 FFmpeg 对象目录。
- ccache、MPP/RGA 安装缓存都不会因为 stamp 变化被删除。

每次 make 前仍会刷新 FFmpeg 的 revision 文件，使未提交源码状态的版本标识
能够更新，而不会因为普通源码修改清空整个对象目录。

## 产物与版本

构建过程中会使用以下目录：

| 路径 | 内容 |
| --- | --- |
| `.build/rockchip/` | FFmpeg、MPP 和 RGA 的中间构建目录 |
| `.rockchip-cache/` | 已安装的 Rockchip 依赖和 ccache |
| `dist/` | FFmpeg 的临时安装目录；可执行文件从自身 `bin/lib/` 加载运行库 |
| `artifact/` | 最终运行时目录；目录内包含 `MANIFEST.mtree` |

最终交付物包含 `ffmpeg`、`ffprobe`、MPP/RGA 运行库以及版本和构建信息。
头文件、静态库、pkg-config 文件等开发内容不会进入交付目录。

`dist/<target>/bin/ffmpeg`、`dist/<target>/bin/ffprobe` 以及最终交付目录
根部的两个二进制，其内嵌 RUNPATH 都精确为 `$ORIGIN/lib`。因此 staged
install 的运行库位于 `dist/<target>/bin/lib/`，最终交付物的运行库位于自身
根目录下的 `lib/`。

`version.txt` 使用源码提交时间和提交标识生成版本。本地工作树存在已跟踪
或未跟踪修改时，版本会带有 `dirty.<state-hash>`；这是区分本地修改构建和
干净提交构建的正常行为。

`BUILDINFO.txt` 使用 `buildinfo_format=2` 的带分组注释 `key=value` 格式。
空行和以 `#` 开头的行可以忽略，数据行按第一个 `=` 分隔。文件记录产物
版本、源码状态、依赖提交、构建器指纹、FFmpeg 构建配置 stamp、目标参数和
glibc 版本；`package_script_sha256` 标识生成当前运行时布局的打包脚本。

`MANIFEST.mtree` 使用标准 mtree 格式，记录交付目录中除 manifest 自身之外的
完整文件集合、类型、权限、普通文件 SHA-256 和符号链接目标。manifest 无法
对自身进行哈希；Actions 显示的 Artifact Digest 和构建来源证明覆盖完整的
`.tar.gz`，包括其中的 `MANIFEST.mtree`。

验证一个交付目录：

```bash
./build/rockchip/tools/compare-artifacts.sh --verify artifact/ffmpeg-rockchip-rk3576-ubuntu22-arm64
```

比较本地与 Actions 解压后的交付目录：

```bash
./build/rockchip/tools/compare-artifacts.sh artifact/ffmpeg-rockchip-rk3576-ubuntu22-arm64 /path/to/actions/ffmpeg-rockchip-rk3576-ubuntu22-arm64
```

## GitHub Actions

仓库维护三个长期分支：

| 分支 | FFmpeg 基线 | 用途 | 推送后的 Actions |
| --- | --- | --- | --- |
| `master` | 当前 8.1 | 主要开发与集成 | 不自动运行，与 `8.1` 保持同指针 |
| `8.1` | 8.1 | 当前版本交付，与 `master` 快进保持同指针 | 三平台完整构建、签名和上传 |
| `6.1` | 6.1 | 兼容版本维护 | 三平台完整构建、签名和上传 |

通用修改先提交到 `master`，验证后将 `8.1` 快进到同一提交；需要支持
6.1 时，使用带来源记录的 `git cherry-pick -x` 回移到 `6.1`，并完成该
分支的独立构建。上游更新分别从 `upstream/8.1` 和 `upstream/6.1` 合并，
不要在两个版本分支之间整体合并。

工作流只在推送到 `8.1` 或 `6.1` 时自动运行，也可以通过
`workflow_dispatch` 手动启动。推送 `master` 只更新分支，不创建工作流
运行；`8.1` 和 `6.1` 分别生成对应版本的完整产物。

Actions 与本地构建都调用根目录的 `build-rockchip.sh`。构建参数、依赖
编译和运行时打包继续放在脚本中，工作流只处理矩阵调度、缓存传输、静态
检查和发布步骤，从而避免本地与 CI 维护两套构建逻辑。

本地构建不会生成最终交付物归档。`8.1` 和 `6.1` 的 Actions 会把包含
`MANIFEST.mtree` 的单一运行时目录写入具有固定条目顺序、时间和所有者的
`.tar.gz`，生成构建来源证明，并使用 Actions 的直接文件上传能力发布，不再
生成额外的 `.sha256` 文件或 ZIP 包装层。产物文件名包含版本分支和芯片目标；
Actions 页面显示的 Artifact Digest 可用于核对下载文件。工作流引用的
GitHub Actions 固定到完整提交 SHA，版本号保留在行尾注释中。

构建镜像的 Ubuntu 基础镜像使用 digest，缓存键还会记录实际工具链指纹。
Ubuntu apt 仓库目前没有固定到历史快照，因此跨日期重建仍应通过 SHA-256
判断是否字节一致。TODO：后续将 apt 固定到 Ubuntu snapshot，或发布统一的
builder image 并让本地和 Actions 按同一 digest 使用。

## 验证

先准备统一的构建镜像：

```bash
./build-rockchip.sh image
```

在镜像中运行静态检查：

```bash
docker run --rm \
  --platform linux/arm64 \
  --workdir /workspace \
  --volume "$PWD:/workspace" \
  --entrypoint /bin/bash \
  ffmpeg-rockchip-build:ubuntu22-arm64 \
  /workspace/build/rockchip/tools/check.sh
```

`tools/check.sh` 包含水印控制器和板端 worker 的标准库离线测试；图片布局
测试需要在安装 Pillow 的虚拟环境中单独运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover \
  -s build/rockchip/tests -p 'test_watermark_image.py'
```

离线测试不覆盖板端 MPP/RGA 驱动、RTSP 发布或播放效果，目标板步骤见
[水印验收指南](STATIC-WATERMARK.md#目标板验收)。

至少构建一个受影响的目标：

```bash
./build-rockchip.sh rk3576
```

查看产物版本：

```bash
cat artifact/ffmpeg-rockchip-rk3576-ubuntu22-arm64/version.txt
```

查看完整构建信息：

```bash
cat artifact/ffmpeg-rockchip-rk3576-ubuntu22-arm64/BUILDINFO.txt
```

验证运行时目录与 manifest：

```bash
./build/rockchip/tools/compare-artifacts.sh --verify artifact/ffmpeg-rockchip-rk3576-ubuntu22-arm64
```

该验证工具需要宿主机提供 `mtree`。macOS 可直接使用系统自带版本；Ubuntu
22.04 可安装 `mtree-netbsd`：

```bash
sudo apt-get install mtree-netbsd
```

提交前还应确认 `git diff --check` 通过，`git status` 中没有构建输出或缓存。
将 `8.1` 快进到 `master` 并推送后，再检查 Actions 三个目标的归档、校验
文件和构建来源证明。

## 故障排查

交付验证日志默认只输出规范化摘要。需要查看完整 ccache、ldd、版本信息和
Shell 执行过程时，可以临时启用 trace：

```bash
ROCKCHIP_BUILD_TRACE=1 ./build-rockchip.sh rk3576
```

| 症状 | 常见原因 | 处理 |
| --- | --- | --- |
| `Unsupported Docker platform` | Docker 服务端不是 Linux ARM64 | 切换到 Linux ARM64 Docker 虚拟机或主机 |
| MPP/RGA 每次都重建 | 锁文件、构建脚本、目标参数或工具链发生变化 | 比较日志中的 `input_hash` |
| Actions ccache 一直不命中 | 恢复和保存路径或构建器指纹不一致 | 检查两步是否都引用 `ccache_path` 输出 |
| FFmpeg 对象目录被重建 | 构建配置 stamp 发生变化或缺失 | 比较 `.rockchip-config-stamp` 中的输入哈希 |
| configure 成功但组件缺失 | `--enable-*` 与组件检查没有同步 | 检查 `build-ffmpeg.sh` 的 `required_configs` |
| 本地版本带 `dirty` | 工作树存在本地修改 | 查看 `git status`；该后缀是本地修改构建的预期标识 |
| 二进制找不到 MPP/RGA | `$ORIGIN/lib` 或 `lib/` 符号链接异常 | 检查 `patchelf --print-rpath`、`ldd` 和交付目录 |
