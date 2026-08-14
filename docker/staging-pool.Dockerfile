# Staging pool-pod image: SecChat's runnerd base + this repo's secagent baked in.
#
# Built in GitHub's cloud by .github/workflows/deploy-staging.yml for any requested ref and
# published as ghcr.io/secrouter/secchat-runnerd-pool:pool (+ :pool-<sha12> for history).
# The staging box pulls it with a fixed operator-owned poller — no GitHub-triggered code ever
# executes on that host, and pool pods have no egress, so everything agents need is baked here.
#
# Provenance rides in two places: /etc/secagent-deployed (agents can cat it) and the
# org.secchat.secagent.* OCI labels (the poller reads them for the deploy notice).

# --- stage 1: build the wheel from the checked-out ref ---
FROM python:3.11-slim-bookworm AS wheel
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /wheels

# --- stage 2: layer secagent onto the runnerd base ---
ARG BASE=ghcr.io/secrouter/secchat-runnerd:base
FROM ${BASE}

USER root
# Toolchain — the pod IS the coding agent's machine (python for agent work, git for git-SSH).
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# secagent into its own venv (bookworm system python is PEP-668-managed), CLI on PATH for
# agents and for pi/extensions/secagent.ts, which shells out to it.
ARG SECAGENT_EXTRAS="review,docs,tokenizer"
COPY --from=wheel /wheels /opt/secagent-wheels
RUN python3 -m venv /opt/secagent \
 && w=$(ls /opt/secagent-wheels/secagent-*.whl) \
 && /opt/secagent/bin/pip install --no-cache-dir "${w}[${SECAGENT_EXTRAS}]" \
 && ln -s /opt/secagent/bin/secagent /usr/local/bin/secagent

ARG SECAGENT_REF=unknown SECAGENT_SHA=unknown SECAGENT_VERSION=unknown \
    BUILT_AT=unknown DEPLOYED_BY=unknown
RUN printf 'ref=%s\nsha=%s\nversion=%s\nbuilt_at=%s\nby=%s\n' \
      "$SECAGENT_REF" "$SECAGENT_SHA" "$SECAGENT_VERSION" "$BUILT_AT" "$DEPLOYED_BY" \
      > /etc/secagent-deployed
LABEL org.secchat.secagent.ref=$SECAGENT_REF \
      org.secchat.secagent.sha=$SECAGENT_SHA \
      org.secchat.secagent.version=$SECAGENT_VERSION \
      org.secchat.secagent.built_at=$BUILT_AT \
      org.secchat.secagent.by=$DEPLOYED_BY

USER node

# Staging model wiring (dev-only workaround): pool-runner.ts forwards no PI_* env into the pod
# manifest, so pi's gateway endpoint is baked as image ENV. 10.42.0.1 is the k3s cni0 gateway
# where the staging compose publishes secrouter (47002).
ARG PI_BASE_URL=http://10.42.0.1:47002/v1
ENV PI_BASE_URL=$PI_BASE_URL PI_MODEL=auto SECCHAT_PI_ALLOW_EGRESS=1
