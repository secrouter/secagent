# secagent-analyzer-dotnet — optional heavy C# analyzer (Roslyn / MSBuildWorkspace).
#
# Part of the heavy-analysis pipeline (docs/design/heavy-analysis-pipeline.md). Built on
# demand via `make analyzer-dotnet`; NOT included in the default secagent image. The .NET
# SDK (not just the runtime) is required because MSBuildWorkspace performs design-time
# builds. The base image is configurable (see DOTNET_SDK below) — a UBI9 .NET SDK for a
# FIPS posture, or the default public Microsoft SDK.
#
# Runtime contract: source is mounted read-only at /src; the entrypoint copies it to a
# writable workdir (so MSBuild can write obj/ without touching the host) and writes a
# secagent-analysis/v1 report to stdout. Run offline (`--network none`).

# Base .NET 8 SDK image. Default is Microsoft's official multi-arch SDK (public, amd64 +
# arm64) so a bare `make analyzer-dotnet` builds out of the box. For a FIPS posture on a
# RHEL host, override with a UBI9 .NET SDK you can pull:
#   --build-arg DOTNET_SDK=registry.redhat.io/ubi9/dotnet-80   (needs registry.redhat.io auth)
# (registry.access.redhat.com/ubi9/dotnet-80 publishes no usable public tag, so it can't
# be the default.)
ARG DOTNET_SDK=mcr.microsoft.com/dotnet/sdk:8.0

FROM ${DOTNET_SDK} AS build
WORKDIR /build
COPY tools/secagent-roslyn/ ./
RUN dotnet publish -c Release -o /app

FROM ${DOTNET_SDK}
USER 0
COPY --from=build /app /app
COPY docker/analyzer-dotnet-entrypoint.sh /usr/local/bin/secagent-analyze
RUN chmod +x /usr/local/bin/secagent-analyze && mkdir -p /work && chown 1001:0 /work
USER 1001
ENV HOME=/work DOTNET_CLI_HOME=/work \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/secagent-analyze"]
