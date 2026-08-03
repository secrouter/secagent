# Sandbox for running GENERATED tests under `secagent verify-tests` (Python).
#
# A separate, deliberately minimal image rather than adding pytest to the agent image.
# The sandbox runs untrusted model-authored code with `--network none`, so everything a
# test needs must already be present — there is no install step at run time, by design.
# Anything added here is something untrusted code can reach, so keep it to the runner.
FROM python:3.11-slim

RUN pip install --no-cache-dir pytest==8.3.4 coverage==7.6.10

# Nothing runs as root. The harness also passes --user, but an image that only works
# because the caller remembered is an image that will eventually run as root.
RUN useradd -u 65534 -o -m runner || true
USER 65534:65534
WORKDIR /w
ENTRYPOINT []
