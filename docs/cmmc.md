# CMMC Level 2 compatibility

This page describes how secagent can be deployed as part of a **CMMC Level 2**
environment and maps its design to the relevant controls. CMMC Level 2 is, in
practice, the **110 security requirements of NIST SP 800-171 Rev. 2** organized into
14 control families.

```{warning}
This is **engineering guidance, not a certification, assessment, or attestation**.
secagent is a software *component*, not an accredited system or enclave. Achieving CMMC
L2 is a property of the **whole environment** (the System Security Plan, the
boundary, the host OS, the identity provider, GitLab, the SIEM, and your
organization's policies), assessed by a C3PAO. Whether secagent handles **CUI** at all
is your CUI determination to make. Use this page to (a) understand what secagent gives
you toward specific controls and (b) plan the engineering work flagged as gaps.
```

## Scope and shared responsibility

secagent runs inside a customer boundary and **inherits the bulk of CMMC L2 controls
from the environment**. Responsibilities split three ways:

Organization / environment (inherited)
: Physical protection (PE), personnel security (PS), awareness & training (AT),
  incident response (IR), maintenance (MA), most of risk & assessment (RA/CA), the
  identity provider, host hardening, disk encryption, network segmentation, and the
  SIEM that aggregates logs. secagent does **not** implement these and should not be
  credited for them.

secagent (this tool)
: Cryptographic hygiene (FIPS hashing/TLS), secret handling, least-functionality
  packaging, configuration surface, and the behavior of its two agents. These are the
  rows marked ✅ / 🟡 / ⚠️ below.

Deployer (you, configuring secagent)
: Pointing secagent at TLS endpoints, supplying scoped service-account tokens via secret
  mounts, enabling `fips.require_fips`, running on a FIPS host, and forwarding logs.

### Does CUI flow through secagent?

Potentially yes — and you should assume so when scoping. secagent reads **source code**
(which may be CUI or contain CUI), sends **excerpts and summaries to the LLM
endpoint**, writes them to the **affordance store** (`.secagent/`), and posts text to
**GitLab**. Two consequences:

- Keep the LLM endpoint **inside the boundary** (local llama.cpp/vLLM — which is
  exactly secagent's design). Never point secagent at an external/SaaS model with CUI.
- Treat `.secagent/` and generated docs as potential CUI at rest (see SC/MP gaps).

## Legend

| Mark | Meaning |
|------|---------|
| ✅ Met | Addressed by secagent's design out of the box. |
| 🟡 Partial | Supported, but depends on correct deployment/configuration. |
| 🏛 Inherited | Satisfied by the environment/organization, not the tool. |
| ⚠️ Gap | Requires development in secagent — see {ref}`cmmc-deficiencies`. |

---

## 3.1 Access Control (AC)

secagent has no interactive user model of its own; access to it (the host, the CLI, the
container) is mediated by the environment. Its own external access is via scoped
service-account tokens.

| Practice | Requirement (paraphrased) | Status | Notes |
|----------|---------------------------|--------|-------|
| AC.L2-3.1.1 | Limit system access to authorized users/processes | 🏛 / 🟡 | Host/IdP enforce who runs secagent. The GitLab token scopes what the agent can do. |
| AC.L2-3.1.2 | Limit access to permitted transactions/functions | 🟡 | Scope the GitLab token to the minimum (api/read+note); the LLM endpoint should be reachable only from secagent. |
| AC.L2-3.1.3 | Control flow of CUI | ✅ 🟡 | Egress is limited to the configured LLM + GitLab endpoints (no telemetry). `network.allowed_hosts` adds an in-tool egress allow-list and `network.require_tls` refuses plaintext (CMMC-3, implemented); defense-in-depth at the network layer still recommended. |
| AC.L2-3.1.5 | Least privilege | ✅ / 🟡 | Container runs as a non-root UID; extras are opt-in (`docs`/`review`). Use a least-privilege GitLab token. |
| AC.L2-3.1.12 | Monitor/control remote access (review webhook) | ✅ 🟡 | Constant-time `X-Gitlab-Token` check, optional `webhook_allowed_ips`, and TLS/**mTLS** via `review serve --tls-cert/--tls-key/--tls-ca` (CMMC-4). |
| AC.L2-3.1.20 | Control connections to external systems | 🟡 | All external endpoints are explicit config; keep them in-boundary. |
| AC.L2-3.1.22 | Control publicly-posted content | 🟡 | The reviewer posts to GitLab MRs; comments are signed and persona-limited. Restrict the bot to private projects. |
| AC.L2-3.1.4/3.1.6-3.1.11, 3.1.13-3.1.19, 3.1.21 | Separation of duties, session lock, MFA-gated remote, mobile/wireless, etc. | 🏛 | Environment/IdP responsibilities; not applicable to the tool. |

## 3.2 Awareness and Training (AT)

| Practice | Status | Notes |
|----------|--------|-------|
| AT.L2-3.2.1 – 3.2.3 | 🏛 | Organizational training. Out of scope for the tool. (This page contributes to operator awareness.) |

## 3.3 Audit and Accountability (AU)

```{admonition} Implemented (CMMC-1)
:class: note
Structured audit logging is now built in (`secagent.audit`): every agent action and MCP
tool call is written as one append-only JSONL record — UTC timestamp, run id,
principal, action, target, model/endpoint (credentials stripped), and outcome —
SHA-256 **hash-chained** so insertion/deletion/edit is detectable. Enable via
`audit.enabled`; verify with `secagent audit verify`; `secagent doctor` reports status and
checks integrity. Disabled by default. Storage protection + SIEM correlation remain
the environment's responsibility.
```

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| AU.L2-3.3.1 | Create/retain audit records to enable monitoring & investigation | ✅ 🟡 | Append-only JSONL of all agent/MCP actions when enabled. Retention/rotation is the environment's. |
| AU.L2-3.3.2 | Ensure actions are traceable to individuals/processes | ✅ | Each record carries `principal` (service identity / `$SECAGENT_PRINCIPAL`) and a per-process `run_id`. |
| AU.L2-3.3.4 | Alert on audit logging failure | 🟡 | Write failures degrade to stderr (captured by container logging); a dedicated alert hook is a future enhancement. |
| AU.L2-3.3.8 | Protect audit info from unauthorized access/modification | ✅ 🏛 | Records are tamper-evident (hash chain; `audit verify`); at-rest protection + access control are the environment's (forward to SIEM, restrict perms). |
| AU.L2-3.3.5/3.3.6 | Correlate & report | 🏛 | SIEM responsibility; JSONL is forwarder-friendly. |
| AU.L2-3.3.3/3.3.7/3.3.9 | Review logged events, time sync, restrict audit management | 🏛 | Environment responsibilities (records use UTC timestamps). |

## 3.4 Configuration Management (CM)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| CM.L2-3.4.1 | Baseline configuration & inventory | ✅ 🟡 | Pinned dependency ranges in `pyproject.toml`; reproducible images. A **CycloneDX SBOM** is generated in CI (`make sbom`) (CMMC-5). |
| CM.L2-3.4.2 | Enforce security config settings | ✅ | Central, typed config (`Settings`) with safe defaults (`verify_tls=true`, FIPS toggles); `secagent config` shows the effective, redacted config. |
| CM.L2-3.4.6 | Least functionality | ✅ | Optional extras (`docs`/`review`/`tokenizer`); no bundled model server; no telemetry. |
| CM.L2-3.4.7 | Restrict nonessential programs/ports | 🟡 | Review webhook is the only listener (one port); disable it if unused. |
| CM.L2-3.4.8/3.4.9 | Allow/deny-list software; control user-installed software | 🏛 | Image build + platform policy. |
| CM.L2-3.4.3/3.4.4/3.4.5 | Change control, impact analysis, access restrictions for changes | 🟡 🏛 | CI gate (ruff/mypy/pytest/doctor) + PR review; org change-management owns the rest. |

## 3.5 Identification and Authentication (IA)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| IA.L2-3.5.1 | Identify users/processes | 🟡 | secagent authenticates *to* GitLab/LLM as a service identity (tokens). Human identity is the IdP's job. |
| IA.L2-3.5.2 | Authenticate identities | ✅ 🟡 | Bearer/PRIVATE-TOKEN auth to endpoints; webhook shared secret (constant-time) with optional **mTLS** client-cert verification (CMMC-4). |
| IA.L2-3.5.10 | Store/transmit only cryptographically-protected passwords | ✅ 🟡 | Secrets come from env/secret mounts, are redacted in `config`/logs, never written to the store. Secret *storage* is the platform's (e.g. k8s/Vault). |
| IA.L2-3.5.3 | MFA for privileged/remote access | 🏛 | IdP. The service account is non-interactive; gate token issuance with org controls. |
| IA.L2-3.5.7-3.5.9, 3.5.11 | Password complexity/reuse, obscure feedback | 🏛 | Not applicable to a token-based service; IdP owns user auth. |

## 3.6 Incident Response (IR)

| Practice | Status | Notes |
|----------|--------|-------|
| IR.L2-3.6.1 – 3.6.3 | 🏛 | Organizational IR process. secagent's contribution — an attributable audit trail — is now available (CMMC-1). |

## 3.7 Maintenance (MA)

| Practice | Status | Notes |
|----------|--------|-------|
| MA.L2-3.7.1 – 3.7.6 | 🏛 | Host/equipment maintenance. Tool updates follow your patch process (see SI.L2-3.14.1). |

## 3.8 Media Protection (MP)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| MP.L2-3.8.1 | Protect (CUI) media, physical & digital | ✅ 🏛 | The store is created **owner-only** (`0700`/`0600`) and the audit log `0600` (CMMC-2). At-rest *encryption* is provided by full-disk/volume encryption (the accepted baseline control, inherited from the environment). |
| MP.L2-3.8.3 | Sanitize media before disposal/reuse | ✅ 🟡 | `secagent purge` securely deletes the store (overwrite + unlink) (CMMC-2); overwrite is best-effort on CoW/SSD, so combine with platform sanitization. |
| MP.L2-3.8.9 | Protect backups of CUI | 🏛 | Backup system responsibility. |
| MP.L2-3.8.2/3.8.4-3.8.8 | Limit access, mark media, transport, removable media | 🏛 ✅ | Mostly environmental. CUI **marking** of generated docs (furo banner + footer) and MR comments is implemented via `marking.banner` (CMMC-6). |

## 3.9 Personnel Security (PS)

| Practice | Status | Notes |
|----------|--------|-------|
| PS.L2-3.9.1 – 3.9.2 | 🏛 | Screening/transfer. Organizational. |

## 3.10 Physical Protection (PE)

| Practice | Status | Notes |
|----------|--------|-------|
| PE.L2-3.10.1 – 3.10.6 | 🏛 | Data-center/facility controls. Organizational. |

## 3.11 Risk Assessment (RA)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| RA.L2-3.11.2 | Scan for vulnerabilities | ✅ 🟡 | CI runs a `pip-audit` dependency CVE scan (reports findings; remediation is a tracked process) plus lint/type/test/doctor (CMMC-5). Image scanning is recommended at the registry. |
| RA.L2-3.11.1/3.11.3 | Risk assessments; remediate vulnerabilities | 🏛 🟡 | Org process; secagent consumes results (patch deps, rebuild images). |

## 3.12 Security Assessment (CA)

| Practice | Status | Notes |
|----------|--------|-------|
| CA.L2-3.12.1 – 3.12.4 | 🏛 | SSP, POA&M, control assessment, continuous monitoring are organizational. This page is an input to the SSP and POA&M. |

## 3.13 System and Communications Protection (SC)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| SC.L2-3.13.8 | Protect CUI confidentiality in transit | ✅ | All HTTP uses system-OpenSSL TLS with verification on (`verify_tls=true`). `network.require_tls` refuses non-HTTPS endpoints (loopback exempt), enforced before any outbound call and surfaced by `doctor` (CMMC-3, implemented). |
| SC.L2-3.13.11 | Use FIPS-validated cryptography | ✅ | SHA-256-only hashing surface (`security.py`), system OpenSSL, no bundled crypto. `fips.require_fips` enforces FIPS at startup (agents abort on a non-FIPS host); `fips.allow_non_fips` is an explicit dev/test escape hatch. `secagent doctor` verifies enforcement and scans for forbidden hashes; Node/pi runs `--enable-fips`. See {doc}`fips`. |
| SC.L2-3.13.16 | Protect CUI at rest | ✅ 🏛 | Store/audit are owner-only with a secure `purge` (CMMC-2); at-rest **encryption** is provided by volume encryption (accepted baseline, inherited). |
| SC.L2-3.13.1/3.13.5 | Monitor/control comms at boundaries; subnetwork separation | 🟡 🏛 | One outbound path each to LLM/GitLab; segment at the network. |
| SC.L2-3.13.6 | Deny network traffic by default (deny-all) | 🟡 🏛 | `network.allowed_hosts` provides an in-tool egress allow-list (CMMC-3); a network-layer deny-all is still the primary control. |
| SC.L2-3.13.10 | Manage cryptographic keys | 🟡 🏛 | Keys/tokens via secret mounts; KMS/Vault is the platform's. |
| SC.L2-3.13.2/3.13.3/3.13.4 | Architectural security, user/system separation, shared-resource control | ✅ 🏛 | Clear component boundaries; container isolation from the platform. |
| SC.L2-3.13.15 | Protect authenticity of comms sessions | ✅ 🟡 | TLS server auth; webhook secret (constant-time) plus optional **mTLS** client-cert verification (CMMC-4). |

## 3.14 System and Information Integrity (SI)

| Practice | Requirement | Status | Notes |
|----------|-------------|--------|-------|
| SI.L2-3.14.1 | Identify/correct flaws timely | ✅ 🟡 | CI `pip-audit` surfaces vulnerable dependencies (CMMC-5); patch via dependency updates + image rebuilds on your cadence. |
| SI.L2-3.14.2/3.14.4/3.14.5 | Malicious-code protection & updates; scan files | 🏛 ✅ | Host AV/EDR. **secagent-specific risk** (prompt injection from code/MR text) is mitigated: untrusted content is wrapped in unguessable delimiters with breakout-stripping and the system prompt is hardened (`secagent.sanitize`, CMMC-7); agents are non-destructive by default (the reviewer only posts comments — no code-write/shell tools). |
| SI.L2-3.14.3 | Monitor security alerts/advisories & act | 🏛 | Org process. |
| SI.L2-3.14.6/3.14.7 | Monitor systems & detect unauthorized use | 🏛 🟡 | SIEM monitoring, fed by secagent's audit logs (CMMC-1, implemented). |

---

(cmmc-deficiencies)=

## Deficiencies requiring development

These are the secagent-side engineering items needed to maximize CMMC L2 coverage. Each
should become a tracked issue and a POA&M line where applicable.

| ID | Gap | Controls | Suggested implementation | Rough effort |
|----|-----|----------|--------------------------|--------------|
| ~~**CMMC-1**~~ ✅ **Done** | Structured, attributable, tamper-evident **audit log** of agent + MCP actions | AU.L2-3.3.1/.2/.4/.8, IR, SI.L2-3.14.6 | **Implemented** in `secagent.audit`: append-only, SHA-256 hash-chained JSONL (run id, principal, action, target, model/endpoint, outcome); `audit.enabled` config, `secagent audit verify`, doctor integrity check. SIEM forwarding + storage protection remain environmental. | — |
| ~~**CMMC-7**~~ ✅ **Done** | **Untrusted input → model** (prompt injection) | SI.L2-3.14.2/.5, AC.L2-3.1.22 | **Implemented** in `secagent.sanitize`: untrusted code/diffs/MR comments wrapped in per-content unguessable delimiters with forged-marker stripping; system prompts hardened. Agents remain non-destructive (comment-only). | — |
| ~~**CMMC-2**~~ ✅ **Done** | At-rest protection + secure delete of the store | MP.L2-3.8.1/.3, SC.L2-3.13.16 | **Implemented:** owner-only perms (`0700`/`0600`) on store + audit log; `secagent purge` secure delete. At-rest **encryption is provided by volume encryption** — the accepted baseline control, inherited from the environment (app-level DB encryption intentionally not added, to keep the FIPS-audited dependency surface lean). | — |
| ~~**CMMC-3**~~ ✅ **Done** | Technical **egress control / TLS enforcement** in-tool | AC.L2-3.1.3, SC.L2-3.13.6/.8 | **Implemented** in `secagent.netpolicy`: `network.require_tls` (loopback exempt) + `network.allowed_hosts`, enforced at agent entry points and reported by `doctor`. | — |
| ~~**CMMC-4**~~ ✅ **Done** | Webhook authentication hardened beyond a shared secret | AC.L2-3.1.12, IA.L2-3.5.2, SC.L2-3.13.15 | **Implemented**: constant-time token check, `webhook_allowed_ips` source allow-list, and TLS/**mTLS** (`review serve --tls-cert/--tls-key/--tls-ca`, client cert required + verified). | — |
| ~~**CMMC-5**~~ ✅ **Done** | **SBOM** + dependency **vulnerability scanning** | CM.L2-3.4.1, RA.L2-3.11.2, SI.L2-3.14.1 | **Implemented**: CI `supply-chain` job generates a CycloneDX SBOM (uploaded artifact) and runs `pip-audit`; `make sbom` / `make audit-deps` locally. Image scanning at the registry + a full lockfile remain recommended follow-ups. | — |
| ~~**CMMC-6**~~ ✅ **Done** | **CUI marking** on generated documentation + comments | MP.L2-3.8.4 | **Implemented** via `marking.banner`: a furo announcement banner + footer on generated docs, and a marking wrapper on MR comments. | — |

```{note}
Effort: **S** ≈ days, **M** ≈ 1–2 weeks, for a single engineer. None require
re-architecture; CMMC-1 (audit logging) is the highest-leverage item and unblocks
several AU/IR/SI controls.
```

## Summary

- **Strong today:** FIPS cryptography (SC.L2-3.13.11), secret handling, least
  functionality, safe configuration defaults, and an in-boundary (local-model) data
  path that keeps CUI off external services.
- **Configure correctly:** TLS endpoints, least-privilege tokens, network
  segmentation/egress control, FIPS host + `require_fips`, and log forwarding.
- **All seven identified tool-side gaps are addressed.** Audit logging (CMMC-1),
  at-rest protection (CMMC-2: perms + secure purge + volume-encryption baseline),
  TLS/egress enforcement (CMMC-3), webhook hardening + mTLS (CMMC-4), SBOM + scanning
  (CMMC-5), CUI marking (CMMC-6), and prompt-injection defenses (CMMC-7) are
  **implemented**. Remaining CMMC L2 coverage is environmental/organizational
  (the SSP, host hardening, volume encryption, IdP/MFA, SIEM, and policy).
- **Always inherited:** PE, PS, AT, MA, most IR/CA/RA, and human identity/MFA —
  owned by the environment and the organization's SSP.
