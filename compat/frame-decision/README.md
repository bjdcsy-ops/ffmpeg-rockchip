# Frame-decision V3 public ABI headers

This directory vendors only the public C ABI headers needed to compile the
`frame_decision_roi` filter. The filter loads the SDK shared library at
runtime; no frame-decision implementation or wire-protocol implementation is
copied into FFmpeg.

The snapshot was exported on 2026-08-26 from frame-decision-service source
commit `fd6db29ccf31b70fda977e218c9949f3d756357f`. It corresponds to
`FD_V3_CLIENT_ABI_VERSION=1` and the RK3588 ARM64
`libframe_decision_client.so.1` artifact with SHA-256
`1358dbc3485541b191ba7efab7064147ae673dac3c2fdd62f5eab5bbe25dfde6`.

| Header | SHA-256 |
| --- | --- |
| `frame_decision_async.h` | `faa37d8ad4c94aaf200521b7a85067bbf8eb5ab8ce96841e0f1f818fd32febb8` |
| `frame_decision_protocol_v3.h` | `c2936e95f0ec589c098f31fefba8557a4094ba530bd1bd7b28295de48da1d28c` |
| `frame_decision_sdk.h` | `738f71c273a3b780727be532cc6fab13aeefc05f5f5908b538b3d41324bb43aa` |

Update all three headers as one ABI snapshot. After replacement, update this
table and run the Rockchip build-state test. RK3588 build stamps include the
combined header-content hash, so a header change invalidates the FFmpeg build
directory automatically.
