# secagent FIPS-compatible base image.
#
# Carries both runtimes: Node.js (for the pi coding agent — the agent loop) and
# Python 3.11 (for secagent — the affordance/docs/review toolset pi drives).
#
# Diagram rendering backend is selected at build time with DIAGRAM_BACKEND, matching
# the runtime `diagrams.renderer` config:
#   svg      — default. Pure-Python renderer; NO extra packages, no X server, no
#              browser. The lightest image and FIPS-clean.
#   chromium — faithful draw.io render via headless Chromium (no X server). Bundles
#              the draw.io viewer JS; expects an operator-provided Chromium binary
#              (UBI does not package one cleanly — best on a Chromium-bearing base).
#   drawio   — faithful render via drawio-desktop + Xvfb (heaviest; adds the X server
#              from Rocky 9). Legacy.
#
# Built on Red Hat UBI9, whose cryptography uses RHEL's FIPS-validated OpenSSL. FIPS
# enforcement is inherited from the host: boot the host kernel with `fips=1` (and/or
# `update-crypto-policies --set FIPS`). Run Node with `--enable-fips` so pi uses the
# validated module; secagent's Python layer already routes all hashing through SHA-256
# and uses system-OpenSSL TLS. `secagent doctor` verifies enforcement at runtime.
#
# Alternatives with an equivalent crypto posture: Chainguard FIPS or Ubuntu Pro FIPS.

ARG BASE=registry.access.redhat.com/ubi9/ubi:9.4
FROM ${BASE}

# Diagram backend baked into the image: svg (default) | chromium | drawio.
ARG DIAGRAM_BACKEND=svg
# drawio-desktop / draw.io viewer release used by the chromium and drawio backends.
ARG DRAWIO_VERSION=24.7.17
# pi coding-agent npm package (see pi.dev). Override if your registry mirrors it.
ARG PI_PACKAGE=@earendil-works/pi-coding-agent

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_OPTIONS="--enable-fips"

# Python 3.11 + Node.js from UBI repos. The default "svg" diagram backend needs
# nothing further; the GTK/NSS libs are only pulled by the faithful backends below.
RUN dnf -y install --setopt=install_weak_deps=False \
        python3.11 python3.11-pip \
        nodejs npm \
        ca-certificates \
    && dnf clean all

# --- chromium backend: the draw.io viewer bundle (operator provides chromium) ------
# render.py drives `chromium --headless=new --dump-dom` over an HTML harness that
# renders the diagram XML with this viewer — no X server. A Chromium binary is NOT
# installed here: UBI/RHEL do not package one cleanly (EPEL's chromium-headless pulls
# an unsatisfiable pipewire/bluetooth dependency chain), so this backend is intended
# for a Chromium-bearing base (e.g. a Debian image where `apt install chromium` is one
# line). Point SECAGENT_DIAGRAMS__CHROMIUM_BINARY at it, or put it on PATH; if absent at
# runtime, rendering falls back to the svg backend.
RUN if [ "$DIAGRAM_BACKEND" = "chromium" ]; then set -eux; \
      mkdir -p /usr/share/secagent; \
      curl -fsSL -o /usr/share/secagent/drawio-viewer.min.js \
        "https://raw.githubusercontent.com/jgraph/drawio/v${DRAWIO_VERSION}/src/main/webapp/js/viewer-static.min.js"; \
    fi

# --- drawio backend: Xvfb (Rocky 9, not in UBI) + drawio-desktop + xvfb-run ---------
# Xvfb and the drawio deps are display/desktop packages with no cryptographic role, so
# the FIPS posture (validated OpenSSL/Node from UBI) is unaffected.
RUN if [ "$DIAGRAM_BACKEND" = "drawio" ]; then set -eux; \
      dnf -y install --setopt=install_weak_deps=False \
        nss atk at-spi2-atk gtk3 libdrm mesa-libgbm alsa-lib libXScrnSaver; \
      printf '%s\n' '[rocky9-appstream]' 'name=Rocky Linux 9 - AppStream' \
        'baseurl=https://dl.rockylinux.org/pub/rocky/9/AppStream/$basearch/os/' \
        'gpgcheck=1' 'gpgkey=https://dl.rockylinux.org/pub/rocky/RPM-GPG-KEY-Rocky-9' \
        'enabled=1' > /etc/yum.repos.d/rocky9-appstream.repo; \
      dnf -y --setopt=install_weak_deps=False install xorg-x11-server-Xvfb libnotify xdg-utils; \
      rm -f /etc/yum.repos.d/rocky9-appstream.repo; \
      DRAWIO_ARCH="$(uname -m)"; \
      curl -fsSL -o /tmp/drawio.rpm \
        "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_VERSION}/drawio-${DRAWIO_ARCH}-${DRAWIO_VERSION}.rpm"; \
      dnf -y install /tmp/drawio.rpm; rm -f /tmp/drawio.rpm; dnf clean all; \
    fi

# `xvfb-run` wrapper for the drawio backend (UBI/RHEL ship Xvfb but not the wrapper).
# Written with printf (not a heredoc) so it works under BuildKit and the legacy builder.
RUN if [ "$DIAGRAM_BACKEND" = "drawio" ]; then printf '%s\n' \
      '#!/bin/sh' \
      'servernum=99' \
      'xvfb_args="-screen 0 1280x1024x24 -nolisten tcp"' \
      'while [ $# -gt 0 ]; do case "$1" in -a) shift ;; -n) servernum="$2"; shift 2 ;; -s) xvfb_args="$2"; shift 2 ;; -e|-f) shift 2 ;; --) shift; break ;; *) break ;; esac; done' \
      'while [ -e "/tmp/.X${servernum}-lock" ] || [ -e "/tmp/.X11-unix/X${servernum}" ]; do servernum=$((servernum+1)); done' \
      'Xvfb ":${servernum}" ${xvfb_args} >/dev/null 2>&1 &' \
      'xvfb_pid=$!' \
      'trap "kill $xvfb_pid 2>/dev/null" EXIT INT TERM' \
      'export DISPLAY=":${servernum}"' \
      'i=0; while [ ! -e "/tmp/.X11-unix/X${servernum}" ] && [ "$i" -lt 100 ]; do i=$((i+1)); sleep 0.1; done' \
      'sleep 1' \
      '"$@"' \
      > /usr/bin/xvfb-run && chmod +x /usr/bin/xvfb-run ; fi

# Install the pi coding agent globally.
RUN npm install -g "${PI_PACKAGE}" || \
    echo "WARN: could not install ${PI_PACKAGE}; install pi manually or override PI_PACKAGE"

# Unprivileged runtime user.
RUN useradd --create-home --uid 10001 secagent
WORKDIR /app

RUN python3.11 --version && node --version
