# secagent UC3 — optional C/C++ static-analysis image (IKOS + secagent).
#
# This is an OPT-IN build (not part of `make docker`). IKOS is a heavyweight,
# LLVM-based native toolchain that pins to a specific LLVM major (14 for IKOS v3.4),
# which is cleanly packaged on Ubuntu 22.04 but not on the UBI9 FIPS base — so the
# analyzer lives in its own image rather than bloating the runtime. It is a build/
# analysis *tool*; for a FIPS posture, set BASE to an Ubuntu Pro FIPS image.
#
# Build:
#   docker build -f docker/analysis.Dockerfile -t secagent-analysis:latest .
# Use:
#   docker run --rm -v "$PWD:/repo" secagent-analysis analyze run /repo /repo/src/foo.c -o /repo/out
#   docker run --rm -v "$PWD:/repo" secagent-analysis analyze ingest /repo /repo/ikos.json -o /repo/out

ARG BASE=ubuntu:22.04
ARG IKOS_VERSION=3.4
ARG LLVM_VERSION=14

# ---- Stage 1: build IKOS from a pinned release --------------------------------
FROM ${BASE} AS ikos-build
ARG IKOS_VERSION
ARG LLVM_VERSION
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates cmake gcc g++ make \
        libgmp-dev libboost-dev libboost-filesystem-dev libboost-thread-dev \
        libboost-test-dev libsqlite3-dev libtbb-dev libz-dev libedit-dev \
        python3 python3-dev python3-venv python3-pip \
        "llvm-${LLVM_VERSION}" "llvm-${LLVM_VERSION}-dev" \
        "clang-${LLVM_VERSION}" "libclang-${LLVM_VERSION}-dev" \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive --depth 1 -b "v${IKOS_VERSION}" \
        https://github.com/NASA-SW-VnV/ikos /src/ikos \
    && cmake -S /src/ikos -B /src/ikos/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/ikos \
        -DLLVM_CONFIG_EXECUTABLE="/usr/bin/llvm-config-${LLVM_VERSION}" \
    && cmake --build /src/ikos/build --target install -j "$(nproc)"

# ---- Stage 2: runtime (IKOS + secagent) -----------------------------------------
FROM ${BASE} AS runtime
ARG LLVM_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/ikos/bin:$PATH
# Runtime libraries IKOS links against, plus clang (ikos-scan intercepts CC/CXX) and
# Python for secagent. Also cmake/make/gcc so ikos-scan can drive a project's own build
# (e.g. a CMake/Make C project) to emit bitcode for analysis. These are analysis/build
# utilities, not cryptographic components.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgmp10 libgmpxx4ldbl libboost-filesystem1.74.0 libboost-thread1.74.0 \
        libsqlite3-0 libtbb12 libedit2 zlib1g \
        "llvm-${LLVM_VERSION}" "clang-${LLVM_VERSION}" \
        python3 build-essential cmake git \
        software-properties-common gpg-agent \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-dev python3.11-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ikos-build /opt/ikos /opt/ikos

# Install secagent with Python 3.11 (secagent requires >=3.11; the base's python3 is 3.10 and
# stays for ikos-scan's own scripts). Core is all `analyze` needs; the optional `tokenizer`
# extra (HuggingFace `tokenizers`, a Rust build with no arm64 wheel here) is omitted —
# analyze falls back to the heuristic token counter, which is fine for reporting.
WORKDIR /app
COPY . /app
RUN python3.11 -m ensurepip --upgrade \
    && python3.11 -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 secagent
USER secagent

# IKOS sanity + secagent entrypoint.
RUN ikos --version && secagent version
ENV SECAGENT_REPO=/repo
ENTRYPOINT ["secagent"]
CMD ["analyze", "--help"]
