# check=error=true

FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982

ARG DEBIAN_FRONTEND=noninteractive
ARG ACTIONLINT_VERSION=1.7.12
ARG ACTIONLINT_SHA256=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
ARG HADOLINT_VERSION=2.14.0
ARG HADOLINT_SHA256=331f1d3511b84a4f1e3d18d52fec284723e4019552f4f47b19322a53ce9a40ed

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# TODO: Pin apt to an Ubuntu snapshot or consume one published builder image
# by digest so local and Actions builds cannot resolve different package sets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        curl \
        git \
        libdrm-dev \
        meson \
        mtree-netbsd \
        ninja-build \
        patchelf \
        pkg-config \
        shellcheck \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /tmp/actionlint.tar.gz \
        "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_arm64.tar.gz" \
    && printf '%s  %s\n' "$ACTIONLINT_SHA256" /tmp/actionlint.tar.gz \
        | sha256sum --check - \
    && tar -xzf /tmp/actionlint.tar.gz -C /tmp actionlint \
    && install -m 0755 /tmp/actionlint /usr/local/bin/actionlint \
    && curl -fsSLo /tmp/hadolint \
        "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-linux-arm64" \
    && printf '%s  %s\n' "$HADOLINT_SHA256" /tmp/hadolint \
        | sha256sum --check - \
    && install -m 0755 /tmp/hadolint /usr/local/bin/hadolint \
    && rm -f /tmp/actionlint.tar.gz /tmp/actionlint /tmp/hadolint

WORKDIR /workspace

ENTRYPOINT ["/bin/bash", "/workspace/build/rockchip/build.sh"]
