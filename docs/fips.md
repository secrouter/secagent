# FIPS compatibility

secagent is designed to run inside a FIPS 140-2/140-3 environment. This document lists
what the toolset does to stay compatible and how to verify it.

## Design choices

| Area | Choice |
|------|--------|
| Hashing | **SHA-256 only** for content addressing and cache keys (`secagent.security`). MD5/SHA-1 are never used; BLAKE2 is avoided (fast but not FIPS-approved). |
| TLS | All HTTP uses `httpx`, which uses the system OpenSSL — the FIPS-validated module on a FIPS host. TLS verification is on by default (`verify=True`); never disabled. |
| Bundled crypto | None. No pure-Python or non-validated crypto libraries are pulled into the security path. |
| Secrets | GitLab tokens, API keys, and webhook secrets come from env vars / secret mounts, are redacted by `Settings.safe_dict()`, and are never logged. |
| Outbound traffic | Only to the configured LLM endpoint and GitLab instance — air-gap friendly. No telemetry. |
| Base image | UBI9 (RHEL FIPS-validated OpenSSL). Chainguard FIPS or Ubuntu Pro FIPS are drop-in alternatives. |
| Diagram rendering | The default backend (`diagrams.renderer = "svg"`) renders in pure Python — **no X server, browser, or extra OS packages**. Faithful backends are opt-in: `DIAGRAM_BACKEND=drawio` adds drawio-desktop + `Xvfb` (from Rocky 9, since UBI omits the X server); `chromium` renders via a headless Chromium you provide (UBI cannot install one cleanly, so it suits a Chromium-bearing base) plus the bundled draw.io viewer JS. All are display/rendering components with no cryptographic role, so the validated crypto surface (OpenSSL/Node from UBI) is unchanged regardless of backend. |
| Agent runtime (pi/Node) | pi runs on Node.js, which links OpenSSL. Run it with `--enable-fips` (the image sets `NODE_OPTIONS=--enable-fips`) on a FIPS host so pi uses the validated module. pi/secagent communicate over local process boundaries; no secagent crypto runs in Node. See [Running off a FIPS host](#running-off-a-fips-host) for the non-FIPS caveat. |
| UC3 analysis image (optional) | The IKOS static-analysis image (`docker/analysis.Dockerfile`) is **Ubuntu-based**, because IKOS pins to LLVM 14 which is packaged there but not on UBI9. It is an opt-in build/analysis *tool*, separate from the FIPS runtime images. For a FIPS posture set its `BASE` arg to an Ubuntu Pro FIPS image; IKOS/LLVM are analysis tooling with no cryptographic role. |
| C# analyzer image (optional) | ⚠️ The Roslyn/MSBuild C# analyzer image (`docker/analyzer-dotnet.Dockerfile`, used by `secagent analyze deep`) **defaults to Microsoft's public .NET SDK (`mcr.microsoft.com/dotnet/sdk:8.0`), which is _not_ FIPS-validated** — the Red Hat UBI9 .NET image it originally targeted publishes no usable public tag, so it can't be the default. For a FIPS posture, rebuild it on a UBI9 (or Ubuntu Pro FIPS) .NET SDK: `make analyzer-dotnet DOCKER_BUILD_ARGS="--build-arg DOTNET_SDK=registry.redhat.io/ubi9/dotnet-80"` (needs `registry.redhat.io` access). Like IKOS it is an opt-in analysis *tool* with no cryptographic role, and `secagent analyze deep` runs it `--network none` (offline). |

## Enabling FIPS at runtime

FIPS enforcement is a host/kernel property that the container inherits:

1. Boot the host with `fips=1` (RHEL: `fips-mode-setup --enable`, then reboot).
2. Optionally `update-crypto-policies --set FIPS`.
3. Run secagent with `fips.require_fips: true` (or `SECAGENT_FIPS__REQUIRE_FIPS=true`) so
   startup/`doctor` hard-fails if enforcement is not actually active.
4. Run pi (Node) with `--enable-fips` (set via `NODE_OPTIONS` in the image) so the
   agent runtime also uses the validated OpenSSL module.

## Running off a FIPS host

`NODE_OPTIONS=--enable-fips` makes Node refuse to start unless the host OpenSSL is in
FIPS mode. On a non-FIPS host this has two consequences:

- The `pi` (Node) agent cannot start, and its image install is skipped at build time
  (the base Dockerfile tolerates the failure with a warning rather than aborting).
- The Python `secagent` roles — docs build (UC1), GitLab review (UC100), and the
  affordance/MCP tools — do not use Node and run normally, including headless Draw.io
  rendering.

To exercise the `pi` role off a FIPS host, drop `--enable-fips` from `NODE_OPTIONS`
(e.g. `docker run -e NODE_OPTIONS= ...`); on a FIPS host, keep it as above so pi uses
the validated module.

### The secagent (Python) FIPS policy

By default (`require_fips: false`) secagent runs anywhere; a non-FIPS host is just a
`doctor` warning. With `require_fips: true`, the agent entry points abort on a
non-FIPS host. To develop or test a *FIPS-configured* deployment on a non-FIPS host
without removing `require_fips`, set the escape hatch `fips.allow_non_fips: true` (or
`SECAGENT_FIPS__ALLOW_NON_FIPS=true`): the agents then run and `doctor` downgrades the
`fips_mode` check to a warning. Do **not** set this in production FIPS deployments.

## Verifying

```bash
secagent doctor          # checks OpenSSL FIPS mode + scans for forbidden hash usage
secagent doctor --probe  # also probes the configured LLM endpoint
```

`doctor` runs these checks:

- **openssl** — reports the linked OpenSSL version.
- **fips_mode** — detects whether OpenSSL refuses non-approved algorithms (probes MD5).
  With `require_fips: true`, a non-FIPS host fails the check.
- **weak_hash_scan** — statically scans the installed `secagent` package for
  `hashlib.md5/sha1/blake2*` usage that is not explicitly marked `usedforsecurity=False`.
- **audit** — reports whether structured audit logging is enabled and, when it is,
  verifies the audit log's SHA-256 hash chain (warns if disabled under `require_fips`).
- **network** — when the egress policy is enforced, validates `llm`/`gitlab` endpoints
  against `require_tls` and `allowed_hosts`.
- **drawio / docs_extra / llm_endpoint** — operational readiness.

## Notes for reviewers

- The single audited hashing surface is `src/secagent/security.py`. All content
  addressing routes through `content_hash` / `file_hash` (SHA-256).
- The MCP server (`src/secagent/mcp/server.py`) is a dependency-free JSON-RPC stdio
  implementation, deliberately adding no extra runtime/crypto surface.
- The only place a non-approved digest could appear is an explicit
  `usedforsecurity=False` call for a non-security purpose; none are currently present.
- `Xvfb` and a small set of Draw.io rendering dependencies come from Rocky 9 AppStream
  rather than UBI (UBI ships no X server). These are non-cryptographic display
  utilities; confirm the added repo is acceptable for your supply-chain policy, or
  swap in a mirror. The repo is removed in the same layer it is added.
