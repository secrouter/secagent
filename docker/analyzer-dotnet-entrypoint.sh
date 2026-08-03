#!/bin/sh
# Copy the read-only mounted source into a writable workdir so MSBuild design-time builds
# can write obj/ without touching the host, then emit the secagent-analysis/v1 report to
# stdout. Arg $1 is the repo-relative .sln/.csproj (else auto-discovered). Offline: the
# project must already be restored (or a NuGet cache mounted) — no restore is performed.
set -e

cp -a /src/. /work/ 2>/dev/null || true
cd /work

TARGET="$1"
if [ -z "$TARGET" ]; then
    TARGET="$(find . -maxdepth 4 -name '*.sln' 2>/dev/null | head -1)"
    [ -z "$TARGET" ] && TARGET="$(find . -maxdepth 4 -name '*.csproj' 2>/dev/null | head -1)"
fi
if [ -z "$TARGET" ]; then
    echo "secagent-analyzer-dotnet: no .sln/.csproj found under /src" >&2
    exit 3
fi

exec dotnet /app/secagent-roslyn.dll "$TARGET" /work
