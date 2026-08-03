# secagent-analyzer-rust — optional heavy Rust analyzer (rust-analyzer -> SCIP -> contract).
#
# Part of the heavy-analysis pipeline (docs/design/heavy-analysis-pipeline.md). Built on
# demand via `make analyzer-rust`; NOT in the default secagent image. rust-analyzer needs
# the Rust toolchain to load a crate's metadata, so the toolchain ships in the image; the
# target project is NOT built (SCIP indexing is analysis, not a full build).
#
# Runtime contract: source is mounted read-only at /src; the entrypoint copies it to a
# writable workdir, runs `rust-analyzer scip`, converts the index to a secagent-analysis/v1
# report on stdout. Run offline (`--network none`) — dependencies must be pre-fetched
# (`cargo fetch`/vendored); a std-only crate needs nothing.

ARG RUST_IMAGE=rust:1-bookworm
FROM ${RUST_IMAGE} AS build
WORKDIR /build
COPY tools/secagent-rust-analyzer/ ./
RUN cargo build --release

FROM ${RUST_IMAGE}
# rust-analyzer as a rustup component (proxied onto PATH via ~/.cargo/bin).
RUN rustup component add rust-analyzer
COPY --from=build /build/target/release/secagent-rust-analyzer /usr/local/bin/secagent-rust-analyzer
COPY docker/analyzer-rust-entrypoint.sh /usr/local/bin/secagent-analyze
RUN chmod +x /usr/local/bin/secagent-analyze && mkdir -p /work
ENV CARGO_NET_OFFLINE=true
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/secagent-analyze"]
