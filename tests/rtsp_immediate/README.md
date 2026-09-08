# RTSP immediate 输出回归测试

本测试链接已构建的真实 libavformat，向本机假 RTSP 服务发送合成 H264、AAC、G711U
包。合成包只用于验证包化、时间戳和所有权，不用于解码或音画同步验收。

先完成对应目标的 Rockchip 构建，再在**相同编译器版本的构建镜像**内运行：

```sh
# 从 FFmpeg 仓库根目录运行。IMMEDIATE_BUILD_IMAGE 使用本次构建的镜像名。
docker run --rm --platform linux/arm64 --entrypoint /bin/bash \
  --volume "$PWD:/workspace:ro" \
  "$IMMEDIATE_BUILD_IMAGE" /workspace/tests/rtsp_immediate/run.sh /workspace rk3588
```

脚本从构建 stamp 读取依赖目录，测试程序写入临时目录并在结束时删除。
需要 GCC、Python 3 和已经生成的静态库；不需要 Rockchip 硬件或摄像头。
正常结果为 `Ran 13 tests` 和 `OK`。

| 覆盖范围 | 检查 |
| --- | --- |
| 默认关闭、显式关闭、单轨 | 保持原交织/直通路径 |
| 多轨安全启动 | 原队列完全排空后才进入直通 |
| AAC 初始负 DTS | 共同平移可开启；晚到负 DTS 导致子轨独立平移则回退 |
| 缺轨、音频停供、EOF | 没有新等待、丢包或重复释放；trailer 排空残留 |
| 时间戳 | 用真实 RTP 随机基值校验绝对 RTP 时间戳、每轨包序和偏移 |
| RTCP | 首 SR 对应触发包，各子轨保留共同 header 起点 |
| 非法 DTS、PTS 重排 | 保留原有每轨 DTS 校验和错误返回 |

这个测试不能代替摄像头同源延迟对照、真实 AAC 解码及 NVR 播放验收。

## 行为与部署边界

| 设置 | 行为 |
| --- | --- |
| `-rtsp_immediate 0`（默认） | 保留原有 RTSP 多轨 DTS 交织 |
| `-rtsp_immediate 1` | 所有轨道安全初始化、旧队列排空后即时发送；用于输入已按实时节奏供包的 RTSP 输出 |
| RTP/AAC `max_delay=0`（命令行 `-muxdelay 0`） | 当前 AU 当次发送，沿用原单 AU 分片路径；正值仍保留原聚合行为 |

先分发支持 `rtsp_immediate` 的 FFmpeg，再启用 Stream Forge 等调用方的该参数。
可通过 `ffmpeg -hide_banner -h muxer=rtsp` 检查选项是否存在；旧二进制会拒绝未知参数，
调用方应在缺少该能力时省略它。该选项不修正源时钟或 RTCP 映射，也不保证所有播放器
都减少相同延迟。

AAC 零缓存、SR、分片和正延迟聚合另由 `libavformat/tests/rtpenc_aac.c` 的四项
公开路径用例覆盖。构建启用 RTP muxer 后，在对应构建目录运行 `make fate-rtpenc-aac`。
