# RK3588 动态 ROI 效果验收

## 概览

本目录用于验证 FFmpeg 8.1 的 RK3588 RKMPP 编码器在接收假 YOLO
Unix Datagram 更新后，是否把质量变化施加到指定区域。它是需要真机的
效果验收，不替代 `fate-rkmppenc-roi` 的协议和生命周期单元测试。

首轮测试采用空间交叉对照：同一输入、同一 CBR 参数顺序编码 control 和
dynamic 两路。两路都启用 `-roi_socket`，control 每 100 ms 发送空集合，
dynamic 按编码进度发送 A/B 反转集合。这样可以扣除画面区域难度、socket
更新开销和两次编码的全局码控波动。

## 快速开始

要求：

| 项目 | 要求 |
| --- | --- |
| 设备 | RK3588 Linux ARM64 |
| 被测程序 | 本功能分支构建的 `ffmpeg`，必须含 `h264_rkmpp`、`hevc_rkmpp` 和 `roi_socket` |
| 分析工具 | 一套完整 FFmpeg/FFprobe，默认 `/usr/bin/ffmpeg` 和 `/usr/bin/ffprobe` |
| Python | Python 3，仅使用标准库 |
| 时间和空间 | 两个编码器约 2 分钟实时编码；建议至少 1 GiB 可用空间 |

在 RK3588 上执行：

```bash
./build/rockchip/tests/roi/run-effect-test.sh \
  --ffmpeg /path/to/ffmpeg-rockchip/ffmpeg \
  --output-dir /path/to/new-evidence-directory
```

只测一个编码器：

```bash
./build/rockchip/tests/roi/run-effect-test.sh \
  --ffmpeg /path/to/ffmpeg-rockchip/ffmpeg \
  --codec h264 \
  --output-dir /path/to/new-h264-evidence-directory
```

输出目录必须尚不存在，脚本不会覆盖已有证据。测试异常中止时，已生成的
日志和码流会保留；运行时 progress 文件和 socket 位于单独的 `mktemp` 目录
并清理。输出路径只允许字母、数字、`_`、`-`、`.` 和 `/`，避免 FFmpeg
filtergraph 把合法文件名中的分隔符误解析为滤镜语法。

## 假 YOLO 场景

默认输入为一次生成并冻结的 `1920x1080`、30 fps、720 帧 H.264 素材。
画面包含运动测试图、时变噪声和两个高对比矩形，避免平坦画面掩盖 ROI 效果。
编码参数固定为 CBR 4 Mbps、8 Mbit buffer、GOP 60。

三个内部采样区均避开 ROI 边界：

| 采样区 | 坐标 `x,y,w,h` | 用途 |
| --- | --- | --- |
| A | `128,320,384,384` | 左侧实验区 |
| N | `768,320,384,384` | 不设置 ROI 的中性区 |
| B | `1408,320,384,384` | 右侧实验区 |

`effect-schedule.json` 使用 `[start_frame,end_frame)`：

| 帧 | 完整 ROI 集合 | 计分窗口 |
| --- | --- | --- |
| 0–120 | `regions:[]` | 60–120，验证两路初始空集合对照 |
| 120–360 | A `-0.5`，B `+0.5` | 180–360 |
| 360–600 | A `+0.5`，B `-0.5` | 420–600 |
| 600–720 | `regions:[]` | 660–720，验证 A/B 强空间偏置已经消退 |

每次切换后跳过一个完整 GOP 再计分。正 `qoffset` 只用于制造方向明确的
校准对照；实际 YOLO 对人、车等目标通常只发送负偏移。

发送器读取 FFmpeg `-progress` 的 `out_time_us`，不依赖启动后的 wall-clock
sleep。它仍不是逐帧 PTS 控制：RKMPP 异步队列会带来数帧延迟，所以计分窗口
必须保留上述过渡区。

## 自动判定

每个采样区逐帧计算：

```text
delta(region) = PSNR-Y(dynamic, source) - PSNR-Y(control, source)
```

空间差分会再扣除中性区：

```text
boost effect  = delta(负 ROI 区) - delta(N)
reduce effect = delta(N) - delta(正 ROI 区)
```

默认门槛：

| 检查 | 通过条件 |
| --- | --- |
| 负 ROI 绝对增益 | 相对空集合 control 的 PSNR-Y 中位增益至少 `+0.5 dB` |
| 正 ROI 绝对损失 | 相对空集合 control 的 PSNR-Y 中位变化不高于 `-0.5 dB` |
| 负 ROI 方向 | `boost effect >= 1.0 dB` |
| 正 ROI 方向 | `reduce effect >= 1.0 dB` |
| 反转 | A/B 在第二阶段按新完整集合交换方向 |
| 初始空集合对照 | ROI 启用前各区绝对变化不超过 `1.0 dB`，空间 spread 不超过 `0.5 dB` |
| 最终清空 | A/B 的 dynamic-control PSNR-Y 残余差不超过 `1.0 dB`；事件与日志同时确认 0 区域集合 |
| CBR 约束 | 强 `±0.5` 校准下，两路总字节差不超过 10%，且各自相对理论 4 Mbps 总量偏差不超过 10% |
| 连续性 | control/dynamic 都能严格解码且恰好 720 帧 |
| 控制面 | 每阶段 payload SHA、区域数、帧窗口和顺序都与冻结 schedule 一致；日志确认应用 0/2 区域更新 |

PSNR-Y 是首轮硬门禁，SSIM-Y 同时记录用于确认方向。整帧 PSNR 不作为 ROI
效果判据，因为 CBR 的目标就是在近似固定总码率内重新分配空间质量。
最终清空不会重置编码器已经分化的 VBV/QP 历史，因此不能要求它立即与另一
个独立 control 编码器绝对质量相等。初始空集合负责验证两路同历史时的质量
一致性；最终阶段则验证之前 A/B 间的强方向偏置已收敛，同时由冻结 payload、
发送事件和 RKMPP 应用日志确认清空控制面。

## 证据目录

成功或失败都会尽量保留下列证据：

| 路径 | 内容 |
| --- | --- |
| `commands.txt` | 实际执行的完整命令 |
| `source.mp4`、`source.sha256` | 冻结参考源及哈希 |
| `dut-version.txt` | 被测 FFmpeg 版本 |
| `dut-ffmpeg.sha256`、`dut-ffmpeg-ldd.txt` | 被测程序身份及动态库解析 |
| `host-*.txt`、`mpp-clients.txt` | 测试开始时的负载、内存、进程名和 MPP 客户端快照 |
| `schedule.json`、`schedule.sha256` | 本次编码和分析共同读取的冻结时间表 |
| `<codec>/<mode>/events.jsonl` | 每次假 YOLO 发送的帧进度、阶段、payload 和结果 |
| `<codec>/<mode>/progress.txt` | FFmpeg 原始 `-progress` 时间线 |
| `<codec>/<mode>/encode.log` | RKMPP 编码器及 ROI socket 日志 |
| `<codec>/<mode>/decode.log` | 严格完整解码检查 |
| `<codec>/analysis/metrics/` | A/N/B 的逐帧 PSNR/SSIM |
| `<codec>/analysis/report.json` | 机器可读指标、哈希、门槛和逐项结果 |
| `summary.txt` | 人工快速查看的总结果 |

失败时先看对应 `sender.log`、`encode.log`、`decode.log`，再看
`analysis/report.json` 的 failed checks。若分析 FFmpeg 缺少 `libx264`、
`testsrc2`、`noise`、`drawbox`、`crop`、`psnr` 或 `ssim`，预检会直接停止。

## 生产参数矩阵

首轮强对照中的 `+0.5` 只用于校准方向。生产参数测试只使用负偏移，分别运行
`-0.15` 和 `-0.25`，并用同一个 force-empty baseline 做对照：

```bash
./build/rockchip/tests/roi/run-production-test.sh \
  --ffmpeg /path/to/ffmpeg-rockchip/ffmpeg \
  --output-dir /path/to/new-production-evidence
```

每个编码器依次运行 baseline、q15 和 q25，共 1020 帧。完整集合按
`0 -> 1 -> 0 -> 2 -> 0 -> 8 -> 0` 切换；1、2、8 个目标阶段的 ROI 总面积
都固定为 147456 像素，避免集合数量变化同时改变总覆盖面积。每个目标内部
采样区独立通过以下门槛，聚合值只用于汇总：

| 检查 | q15 | q25 |
| --- | --- | --- |
| 目标绝对 PSNR-Y 增益 | 至少 `0.30 dB` | 至少 `0.60 dB` |
| 目标相对背景选择性 | 至少 `0.50 dB` | 至少 `0.90 dB` |
| q25 相对 q15 的目标增益 | 至少 `0.20 dB` | 至少 `0.20 dB` |

分析还要求背景损失不超过 `2.0 dB`、清空后的空间残余不超过 `0.5 dB`，
并将 sender 的每个成功事件与编码器 `Applied ROI socket update` 的代际和
区域数逐项核对。分析器会独立重放冻结的 `progress.txt`，事件必须与重放
序列完全一致；总发送覆盖率至少 90%，启动阶段至少 75%，其余各阶段至少
90%。runner 以 20 ms 的 stats 周期观察编码进度，为 30 fps 下的 100 ms
heartbeat 提供足够的采样分辨率。H.264 和 HEVC 的输出大小及理论 CBR 偏差
都不得超过 10%。

异步编码器可能先关闭 ROI socket，再写出滞后的尾部 progress 记录。sender
因此只抑制时间表末尾三个 slot 的冗余 `repeat`，不会抑制 phase change；分析器
按同一规则独立重放。最终阶段仍须达到原 90% 覆盖率，所有实际发送仍须与
`Applied` 日志逐项闭环，guard 之前的任何发送错误仍会使测试无效。

### 三次重复性汇总

形成生产配置建议时，同一编码器要在相同冻结输入、schedule 和门槛下完整
运行三次，再汇总三份 `report.json`：

```bash
python3 ./build/rockchip/tests/roi/analyze-production-repeats.py \
  --report /evidence/run-1/h264/analysis/report.json \
  --report /evidence/run-2/h264/analysis/report.json \
  --report /evidence/run-3/h264/analysis/report.json \
  --output-dir /evidence/h264-repeat-analysis
```

汇总器不会把三次逐帧样本混池后重新计分。每次仍按原始门槛独立判断；同一
检查只有 `3/3` 通过才是 `STABLE_PASS`，`2/3` 标记为
`UNSTABLE_NO_RECOMMEND`，`0/3` 或 `1/3` 标记为 `REJECT`。报告缺失、输入
源哈希、schedule 哈希、门槛、格式或检查集合不一致时直接判为无效，不生成
配置建议。

配置单元按编码器、ROI 数量 `1/2/8` 和 qoffset 分开判定。每个目标的增益与
选择性都必须通过；q25 还必须逐目标通过相对 q15 的增益。若 q15 和 q25 都
稳定通过，默认推荐偏移更小的 q15。当前清空门是一个阶段内多个目标探针的
聚合值，汇总报告明确记录 `clear_scope=phase_aggregate` 和
`per_target_clear_verified=false`，不能据此宣称每个目标都已独立证明清空。

## 移动目标和失联清空

移动测试在 fake YOLO 与编码器之间增加一个 Unix Datagram watchdog bridge：

```text
fake YOLO -> watchdog bridge -> ffmpeg roi_socket
```

执行命令：

```bash
./build/rockchip/tests/roi/run-watchdog-test.sh \
  --ffmpeg /path/to/ffmpeg-rockchip/ffmpeg \
  --output-dir /path/to/new-watchdog-evidence
```

900 帧场景依次为 0–120 空集合、120–300 移动 A、300–540 producer 完全静默、
540–780 移动 B、780–900 最终空集合。producer 标称 10 Hz，每 12 帧改变一次
16 像素对齐的 ROI 坐标。bridge 使用 monotonic clock；连续 400 ms 未收到有效
完整集合时只发送一次空集合，恢复后重新武装。无效报文不会刷新存活时间。

测试不宣称 ROI 与编码帧逐帧精确对齐，而是在移动框共同覆盖的固定内部区域
计分。总发送数和每个非静默阶段都必须达到标称次数的 90%；bridge 转发和
watchdog clear 必须逐项出现在编码器应用日志。分析器还会从 `progress.txt`
独立重放跨 slot 补发、静默和恢复序列，拒绝 producer 日志与输入时间线不一致。

清空质量使用 A/B 匹配差分而不是不同内容类别的中性 N 区。令
`D = delta(A) - delta(B)`，并以 warmup 空集合的 `D0` 校准：移动 A 时
`D-D0` 必须为正，移动 B 时 `D0-D` 必须为正；失联清空和最终清空的
`abs(D-D0)` 同时要求不超过 `0.5 dB`，且不超过对应活动效果的 20%。N 区
继续用于活动阶段的选择性硬门禁和码控诊断，但不作为清空因果对照。分析器
自测包含保留完整活动对比的 sticky ROI 负例。

watchdog bridge 本身崩溃时无法再发送空集合，编码器会保留最近一次 ROI。
生产部署必须监督并自动重启 bridge；若需要跨 bridge 故障保证清空，应在编码器
内部另行实现 TTL。
