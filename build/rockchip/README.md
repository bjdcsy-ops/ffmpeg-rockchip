# Rockchip FFmpeg 构建与维护指南

## 概览

本目录包含项目的 Rockchip FFmpeg 构建实现。本地构建和 GitHub Actions
使用相同的 Dockerfile 与脚本，构建逻辑在这里维护，工作流负责 CI 调度、
缓存和产物发布。

当前基于 FFmpeg 6.1，支持 `rk3588`、`rk3576` 和 `rv1126b`，构建与交付
仅面向 Linux ARM64。

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
2. 容器入口 `build.sh` 读取目标配置和源码状态，按锁文件构建或复用
   MPP、RGA。
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
| `build.sh` | 容器内依赖、缓存、FFmpeg 和交付物构建编排 | 调整构建阶段或执行顺序 |
| `Dockerfile` | Ubuntu 22.04 ARM64 构建环境和检查工具 | 增减系统依赖或升级工具 |

FFmpeg 的 configure、make 和 install 命令集中在 `build-ffmpeg.sh`，修改
FFmpeg 构建能力时通常从这里开始。

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

目录外还有两个相关入口：

| 文件 | 作用 |
| --- | --- |
| `build-rockchip.sh` | 开发者和 GitHub Actions 共用的构建入口 |
| `.github/workflows/rockchip-build.yml` | `master` 构建、缓存、归档和产物发布 |

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
只覆盖依赖构建脚本、目标编译参数以及实际 builder fingerprint。任一输入
发生变化都会生成新的缓存目录，因此日常修改不需要人工递增缓存版本。

如果修改了 `lib/build-dependencies.sh` 或目标编译参数，下一次构建重新
编译 MPP、RGA 是预期结果。未修改这些输入时，日志中应出现
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
| `master` | 当前 8.1 | 主要开发与集成 | 构建文件静态检查 |
| `8.1` | 8.1 | 当前版本交付，与 `master` 快进保持同指针 | 三平台完整构建、签名和上传 |
| `6.1` | 6.1 | 兼容版本维护 | 三平台完整构建、签名和上传 |

通用修改先提交到 `master`，验证后将 `8.1` 快进到同一提交；需要支持
6.1 时，使用带来源记录的 `git cherry-pick -x` 回移到 `6.1`，并完成该
分支的独立构建。上游更新分别从 `upstream/8.1` 和 `upstream/6.1` 合并，
不要在两个版本分支之间整体合并。

工作流在推送到 `master`、`8.1` 或 `6.1` 时自动运行，也可以通过
`workflow_dispatch` 手动启动。`master` 不生成交付物；`8.1` 和 `6.1`
分别生成对应版本的完整产物。

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
合并到 `master` 后，再检查 Actions 三个目标的归档、校验文件和构建来源
证明。

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
