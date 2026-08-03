#!/bin/sh
# Copy the read-only mounted source into a writable workdir (so rust-analyzer/cargo can
# write metadata + target/ without touching the host), run rust-analyzer's SCIP indexer,
# then convert the index to a secagent-analysis/v1 report on stdout. Arg $1 is the
# repo-relative crate/workspace dir (default "."). Offline: dependencies must already be
# fetched (`cargo fetch`/vendored) — no network fetch is performed.
set -e

cp -a /src/. /work/ 2>/dev/null || true
cd /work

TARGET="${1:-.}"
if [ ! -f "$TARGET/Cargo.toml" ]; then
    echo "secagent-analyzer-rust: no Cargo.toml under /src/$TARGET" >&2
    exit 3
fi
cd "$TARGET"

# rust-analyzer writes SCIP to the output file and progress/logs to stderr; keep stdout
# clean for the contract JSON the converter emits.
rust-analyzer scip . --output /tmp/index.scip 1>&2

exec secagent-rust-analyzer /tmp/index.scip
