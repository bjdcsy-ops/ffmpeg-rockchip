# Frame-decision V3 public ABI headers

This directory vendors only the public C ABI headers needed to compile the
`frame_decision_roi` filter. The filter loads the SDK shared library at
runtime; no frame-decision implementation or wire-protocol implementation is
copied into FFmpeg.

The snapshot was exported on 2026-09-02 from frame-decision-service source
commit `ad66e2ab853e1e5e8cb0ca23c0b547ed0eb7a2e0`. It corresponds to
`FD_ABI_VERSION=3`, `FD_V3_CLIENT_ABI_VERSION=2`, and the
`libframe_decision_client.so.2` runtime ABI. Runtime library hashes are
recorded by the matching package rather than this header-only snapshot.

| Header | SHA-256 |
| --- | --- |
| `frame_decision_async.h` | `387620f82d56ba26cb068f4918915a04ecfb770f9676470b5148839748d01a8b` |
| `frame_decision_protocol_v3.h` | `d8623681a4a4480f0db0fec47a4d6e7e11f84ecc2f8cb0d971e3d65655fdba43` |
| `frame_decision_sdk.h` | `8f40a54b8fc8db70a46d8e52814f89934093b9191b4e48e5d739f876e587adb6` |

Update all three headers as one ABI snapshot. After replacement, update this
table and run the Rockchip build-state test. RK3588 build stamps include the
combined header-content hash, so a header change invalidates the FFmpeg build
directory automatically.
