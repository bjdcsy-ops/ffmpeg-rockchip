FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        file \
        git \
        libdrm-dev \
        meson \
        nasm \
        ninja-build \
        patchelf \
        pkg-config \
        yasm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

ENTRYPOINT ["/bin/bash", "/workspace/build/rockchip/build.sh"]
