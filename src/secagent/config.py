"""Configuration for secagent.

Settings are layered: built-in defaults < YAML config file < environment variables.
Environment variables are prefixed ``SECAGENT_`` (nested via ``__``), e.g.
``SECAGENT_LLM__BASE_URL``. A YAML file may be supplied via ``SECAGENT_CONFIG`` or the
``--config`` CLI flag.

The configuration deliberately decouples the toolset from any particular model
server: ``llm.base_url`` points at any OpenAI-compatible endpoint (llama.cpp
``llama-server`` or vLLM).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_config_path(rel_path: str | os.PathLike[str]) -> Path:
    """Resolve a config file path, including bundled defaults like
    ``config/rules/embedded-cpp.yaml`` / ``config/alignment/default.yaml``.

    Absolute paths, and relative paths that exist relative to the current directory, are
    used as-is. A relative path that isn't found is then resolved against the secagent
    install root (where ``config/`` ships) — so ``scan``/``review`` work regardless of the
    working directory. This matters because pi runs secagent from the *project* directory,
    not the secagent repo, where a CWD-relative ``config/...`` would not be found.
    """
    p = Path(rel_path)
    if p.is_absolute() or p.exists():
        return p
    # This file is <root>/src/secagent/config.py; the bundled config/ lives at <root>/config/.
    root = Path(__file__).resolve().parents[2]
    candidate = root / p
    return candidate if candidate.exists() else p


class LLMConfig(BaseModel):
    """Connection + budgeting for the external OpenAI-compatible endpoint."""

    base_url: str = "http://localhost:8000/v1"
    api_key: str = "not-needed"  # llama.cpp/vLLM usually ignore this; never logged
    model: str = "gemma-3-12b-it"
    # Sized for the models secagent is actually deployed against: Gemma 3 4B/12B/27B are
    # 128k and Gemma 4 goes to 256k. This used to default to 8192 (a Gemma 2 figure),
    # which silently capped far more than throughput — see max_output_tokens below.
    # `secagent doctor --probe` reads your server's real window and flags a mismatch in
    # either direction, so a smaller deployment is told rather than left to overflow.
    context_window: int = 131_072
    # Fraction of the window reserved for the agent's working context (affordances,
    # tool results, scratchpad). The remainder is left for the model's response.
    context_budget_ratio: float = 0.6
    # Reasoning models emit `reasoning_content` before any `content`, and measured
    # against a local Gemma-4 an ordinary analysis prompt spends 13-15k output tokens
    # before emitting a character: budgets of 1024 (the old default), 8192 and 12288 all
    # returned EMPTY, 16000 answered. A generous cap is not wasteful — the model stops
    # when it is done — but too small a cap costs a wasted call and a retry every time.
    max_output_tokens: int = 16_384
    temperature: float = 0.2
    request_timeout_s: float = 120.0
    max_retries: int = 3
    # Prefer the server's native OpenAI tool-calling. If your llama.cpp build lacks
    # it, set to False to use the embedded JSON tool-protocol fallback.
    native_tool_calls: bool = True

    @property
    def context_budget_tokens(self) -> int:
        usable = int(self.context_window * self.context_budget_ratio)
        return max(512, usable - self.max_output_tokens)


class SecSSOConfig(BaseModel):
    """OIDC settings for secagent's TWO distinct SecSSO identities. Backs both
    ``secagent token`` (service) and ``secagent login``/``secagent token --user``
    (per-user) — see ``secsso.py``.

    Distinct from ``llm.api_key``: this section *produces* a bearer token (for
    SecRouter, or any other SecSSO-protected endpoint); ``llm.api_key`` (directly, via
    ``"!secagent token"``/``"!secagent token --user"`` — see ``secretval.py``) or a pi
    ``models.json`` provider (``apiKey: "!secagent token --user"``) then *consumes* it.

    **Service identity** (``client_id``/``username``/``client_secret_env``/``scope``/
    ``token_cache_path``) — OAuth2 ``client_credentials`` (RFC 6749 SS4.4), one shared
    confidential client for headless/automated calls (``secagent index``/``scan``/...,
    or a service-mode pi). Unchanged by the per-user fields below.

    **Per-user identity** (``device_authorization_url``/``device_client_id``/
    ``device_scope``/``user_token_cache_path``) — OIDC device authorization (RFC 8628),
    a PUBLIC client (no secret — device-code flows need none, RFC 8628 SS3.1) that
    authenticates as the individual developer running ``secagent login``. Reuses
    ``token_url`` below: SecSSO (Authentik) serves one token endpoint per instance,
    shared by every OAuth2 provider/grant on it (verified against
    ``secsso/bootstrap/secsso.sh``'s ``secagent-config`` output and
    ``secsso/blueprints/{secagent-service,secagent-pi}.yaml`` — both clients' token
    requests hit the identical URL, only ``client_id``/``grant_type``/credentials
    differ), so a second token-endpoint field would just be a duplicate of the first
    that could drift out of sync with it.
    """

    # SecSSO's OIDC token endpoint, e.g. https://secsso.<domain>:9000/application/o/
    # token/ (Authentik-style; adjust to your IdP). Empty refuses to run (`secagent
    # token` / `secagent token --user` fail loudly rather than guessing an endpoint).
    # Shared by the service AND per-user grants — see the class docstring.
    token_url: str = ""
    client_id: str = "secagent"
    # Informational only: RFC 6749's client_credentials grant carries no
    # resource-owner identity, so this is never sent as a request parameter. Many
    # OIDC IdPs (Keycloak service accounts included) still label the resulting
    # service identity with a username; this documents what SecSSO is expected to
    # resolve `client_id` to, and is surfaced in error messages for that reason.
    username: str = "svc-secagent"
    # NAME of the environment variable holding the client secret — never the secret
    # itself, and never written to a config file. Set SECAGENT_CLIENT_SECRET (or
    # point this at a different variable) out of band (secret mount, CI secret, ...).
    client_secret_env: str = "SECAGENT_CLIENT_SECRET"
    scope: str = "openid secrouter"
    # Where `secagent token` caches the fetched SERVICE token between invocations. pi
    # re-invokes a models.json "!command" apiKey on every request (see docs/models.md
    # "Value Resolution"), so without a cache here every LLM call anywhere in the
    # suite would cost a network round trip to SecSSO. Deliberately NOT under any
    # repo's .secagent/ store — this is a service identity, not a per-repo artifact —
    # and written with owner-only (0600) permissions.
    token_cache_path: str = "~/.secagent/auth/secsso-token.json"
    # Refresh this many seconds before actual expiry, so a token already in flight to
    # SecRouter does not expire mid-request. Shared by both caches below.
    expiry_buffer_s: float = 60.0

    # -- per-user (device-code) fields -- see `secagent login` / `secagent token --user`
    #
    # SecSSO's OIDC device_authorization endpoint, e.g. https://secsso.<domain>:9000/
    # application/o/device/. Empty refuses to run (`secagent login` fails loudly
    # rather than guessing an endpoint). Normally set by `secagent init --domain ...`,
    # not by hand.
    device_authorization_url: str = ""
    # The PUBLIC client `secagent login` authenticates as (no secret — see the class
    # docstring). Distinct from `client_id` above (the confidential service client);
    # matches secsso/blueprints/secagent-pi.yaml's `client_id: !Context pi_client_id`.
    device_client_id: str = "secagent-pi"
    # Distinct from `scope` above: includes `profile`/`email` so `secagent login` can
    # show *who* just signed in (best-effort, from the token response's OIDC
    # `id_token` — see `secsso.py`); `secrouter` is still required, exactly as for the
    # service grant, or the issued token's audience won't include SecRouter.
    device_scope: str = "openid profile email secrouter"
    # Where `secagent login` caches {access_token, refresh_token, expires_at, sub,
    # email} (0600). Separate from `token_cache_path` — the two are different
    # people's tokens (a shared service identity vs. this one developer) and must
    # never be conflated into a single file.
    user_token_cache_path: str = "~/.secagent/auth/user-token.json"


class GitLabConfig(BaseModel):
    """GitLab connection for the MR review agent (UC100)."""

    url: str = "https://gitlab.com"
    token: str = ""  # supplied via env/secret mount, never logged
    # Username/path the bot answers to when @-mentioned in discussions.
    bot_username: str = "secagent-bot"
    # Verify TLS using the system (FIPS) trust store. Do not disable in production.
    verify_tls: bool = True
    webhook_secret: str = ""  # X-Gitlab-Token shared secret for the webhook receiver
    # Serve the webhook with NO authentication at all. The receiver refuses to start when
    # `webhook_secret` is empty, because the auth check used to be skipped entirely in
    # that case: an operator who missed one env var deployed an open POST endpoint that
    # would review-and-post to any project the GitLab token could reach. This flag is the
    # deliberate way to say "yes, really" (a test harness, a loopback-only bind behind an
    # authenticating proxy) — a blank secret is not.
    webhook_allow_unauthenticated: bool = False
    # Optional source-IP allow-list for the webhook ([] = allow any source).
    webhook_allowed_ips: list[str] = Field(default_factory=list)
    # Polling fallback for air-gapped instances without webhook delivery.
    poll_interval_s: int = 0  # 0 disables polling
    # Where `review poll` records which merge requests it has already reviewed, relative
    # to the repo (or cwd). `--once` from cron starts a fresh process every tick, so a
    # seen-set held only in memory made every open MR look new and reposted a full public
    # review on every run, forever.
    poll_state_file: str = ".secagent/review-seen.json"


class MattermostConfig(BaseModel):
    """UC101: Mattermost chat-ops front end (``secagent chat serve``).

    secagent's own transport (not the ``pi-mattermost`` plugin): a small FastAPI
    receiver accepting Mattermost slash-command and outgoing-webhook deliveries,
    replying in-thread via the REST API as the ``secagent`` bot. Same hardening
    posture as ``gitlab`` (fail-closed webhook auth, constant-time token compare,
    optional source-IP allow-list, TLS/mTLS via ``chat serve --tls-*``).
    """

    # Mattermost server base URL, e.g. https://chat.example.com (no trailing /api/v4).
    url: str = ""
    # Bot/personal access token for OUTBOUND REST calls (posting replies). Distinct
    # from webhook_secret below, which authenticates INBOUND deliveries. Never logged.
    bot_token: str = ""
    # Team name/ID the bot operates in (informational; also used to sanity-check an
    # inbound payload's team_domain/team_id when present).
    team: str = ""
    # Recognized mention prefix; also used to ignore the bot's own posts.
    bot_username: str = "secagent"
    verify_tls: bool = True
    # Shared `token` Mattermost sends with every slash-command/outgoing-webhook
    # delivery. `chat serve` refuses to start if this is empty, unless
    # webhook_allow_unauthenticated is set — the same fail-closed contract as
    # gitlab.webhook_secret (a missing/empty secret used to mean "accept everything").
    webhook_secret: str = ""
    webhook_allow_unauthenticated: bool = False
    # Optional source-IP allow-list for the webhook ([] = allow any source).
    webhook_allowed_ips: list[str] = Field(default_factory=list)


class AffordanceConfig(BaseModel):
    """Affordance-engine behaviour."""

    # Where the content-addressed affordance store lives (relative to repo or abs).
    store_dir: str = ".secagent"
    # Files larger than this are summarised structurally only (no full LLM read).
    max_file_bytes: int = 200_000
    # Ceiling on file summaries in an assembled context block. The token budget is the
    # real constraint below this; the ceiling stops a very large window from burying the
    # relevant files under marginal ones. Raise it if your model has context to spare.
    max_context_files: int = 60
    # Whether file summaries may call the LLM (vs. heuristic-only extraction).
    llm_summaries: bool = True
    # Force-regenerate LLM summaries/descriptions, ignoring cached output on read (the
    # fresh result is still cached). The cache key already includes the model name, so a
    # different model regenerates automatically; this flag is for re-evaluating the SAME
    # model (e.g. temperature>0 sampling). Set per-run by `--refresh-summaries`.
    refresh_summaries: bool = False
    # Exclude the project's own version-control metadata/tooling from architecture/doc
    # scans: the `.git` directory (at any depth, so submodules and monorepos are
    # covered), submodule `.git` pointer files, Git dotfiles (.gitignore, .gitmodules,
    # .gitattributes, .gitkeep, .mailmap), other VCS dirs (.svn, .hg, .bzr, _darcs),
    # and repository-host platform config (.github, .gitlab). This targets VCS material
    # by its reserved names only — source files that use or implement Git as a feature
    # (e.g. a vcs/git module, a GitLab client) are matched by content/name, NOT here.
    ignore_vcs: bool = True
    # C/C++ analysis (clang AST): used when libclang is installed to extract accurate
    # functions and the inter-file call map. Point at a compile_commands.json for best
    # results; empty = autodiscover (build/, repo root) or parse best-effort. The agent
    # can generate one, e.g. `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` / `bear -- make`.
    clang_compile_db: str = ""
    # Extra include directories for best-effort clang parsing (no compile DB). Project
    # headers are auto-discovered from the repo root; add system/SDK include dirs here.
    clang_extra_includes: list[str] = Field(default_factory=list)
    # Heavy (compiled) analysis backend for `secagent analyze deep`. "light" keeps the
    # default syntactic backends; "heavy"/"auto" run the optional analyzer container when
    # present (see docs/design/heavy-analysis-pipeline.md). Container runtime + image:
    analysis_backend: str = "light"  # light | heavy | auto
    analyzer_runtime: str = "docker"  # container runtime (docker | podman)
    analyzer_image_dotnet: str = "secagent-analyzer-dotnet:latest"
    analyzer_image_rust: str = "secagent-analyzer-rust:latest"
    # Cap on per-function LLM descriptions generated during a docs build (one model
    # call each; cached). 0 disables function descriptions; -1 describes ALL functions
    # (no cap). On large codebases the default 120 covers only a fraction — raise it (or
    # set -1) for full coverage; the docs note when the cap truncated coverage.
    max_function_docs: int = 120
    # KNOWN GAP, recorded rather than fixed — two structural facts about the docs build
    # that the next person to touch it will want, and that no code comment currently says.
    #
    # 1. There is NO worker knob for docs. `scan.workers` and `testgen.workers` exist;
    #    `describe_functions` hardcodes its own default (`workers: int = 4` in
    #    affordances/file_summary.py) and `agents/docs/agent.py` never passes one. So
    #    when the advice for 400 Bad Request responses is "lower workers", there is
    #    nothing to lower for the one command that makes the most calls — a Click-sized
    #    repo is ~180 file-summary calls plus up to `max_function_docs` more.
    #
    # 2. There is NO incremental page output. Indexing, every file summary, every
    #    function description and then page-writing run as one monolithic pass; nothing
    #    appears under the output `source/` directory until the whole pipeline finishes.
    #    Time-to-first-page equals time-to-last-page, so a 50-minute build is
    #    indistinguishable from a hung one while it runs. This was the top recommendation
    #    of the original UC1 evaluation and is still open.
    # Directory/file globs to ignore during indexing (general; VCS is handled above).
    ignore_globs: list[str] = Field(
        default_factory=lambda: [
            ".secagent/**",
            "**/__pycache__/**",
            "**/node_modules/**",
            "**/target/**",  # Rust/Cargo build output
            "**/.venv/**",
            "**/venv/**",
            "**/dist/**",
            "**/build/**",
            "**/*.min.js",
            "**/*.lock",
        ]
    )


class DiagramsConfig(BaseModel):
    """UC1 diagram rendering backend selection."""

    # How architecture diagrams are turned into images for the docs site:
    #   "svg"      — render directly from secagent's own diagram model. No external
    #                binary, FIPS-clean, fastest; the default. Not pixel-identical to
    #                the draw.io editor, but accurate to the detected architecture.
    #   "chromium" — faithful draw.io render via headless Chromium (no X server). Uses
    #                the bundled draw.io export assets; needs a chromium binary.
    #   "drawio"   — faithful render via drawio-desktop + Xvfb (heaviest; legacy).
    # The "chromium"/"drawio" backends fall back to "svg" when their tool is absent,
    # so the docs always get inline diagrams.
    renderer: str = "svg"
    # Path to the chromium/chrome binary for renderer="chromium". Empty = autodetect
    # (looks for chromium, chromium-browser, chrome, google-chrome on PATH).
    chromium_binary: str = ""


class PersonaConfig(BaseModel):
    """UC100 review persona — the 'simple way to edit alignment and verbosity'."""

    # Path to a persona/alignment YAML profile (see config/alignment/*.yaml).
    profile: str = "config/alignment/default.yaml"


class FIPSConfig(BaseModel):
    """FIPS posture toggles checked by ``secagent doctor`` and at startup."""

    # When True, FIPS mode is expected; startup aborts on a non-FIPS host unless
    # ``allow_non_fips`` is also set.
    require_fips: bool = False
    # Escape hatch: permit running on a non-FIPS host even when require_fips is True
    # (e.g. dev/test of a FIPS-configured image). The non-FIPS state is still warned.
    # Has no effect when require_fips is False (secagent already runs anywhere then).
    allow_non_fips: bool = False
    # Refuse to use non-approved hash algorithms anywhere in the process.
    forbid_weak_hashes: bool = True


# Sampling temperature for scan's rule-checking calls. NOT the global default: see
# `ScanConfig.temperature`. Measured on rtcm.cpp (3.4KB) against the deployed Gemma,
# all seven category groups at each temperature, outcomes classified with scan's OWN
# parser so they are the states production distinguishes:
#
#     temp   OK  empty  no-json-array  unparseable
#      0.2    1      6              0            0
#      0.7    7      0              0            0
#      1.0    7      0              0            0
#
# At 0.2 six of seven groups return EMPTY after burning the whole 16384-token budget —
# the model degenerates into repetition (a measured run repeated one finding line 334
# times). That, not file size or model capacity, was the cause of scan's C++ failures.
#
# The worry worth measuring was that a high temperature might buy convergence at the
# cost of *parseable* output, since scan needs a JSON array and not merely non-empty
# content. It does not: zero malformed responses at any temperature, so there is no
# trade being made here.
#
# 0.7 over 1.0 because they are indistinguishable on this evidence — both 7/7, both 10
# findings, wall time 11.1 vs 12.0 minutes (within noise at one sample per cell, and
# note 1.0 was NOT faster in this shape) — and a lower temperature samples less
# randomly, so two scans of unchanged code agree more often. The same reproducibility
# argument the shared ranking makes in `affordances/priority.py`.
_SCAN_TEMPERATURE_DEFAULT = 0.7


class ScanConfig(BaseModel):
    """UC4: LLM rule-based memory/stability scan.

    ``rules_profile`` points at a YAML rule set under ``config/rules/``, edited the
    same way as the review persona — add/remove rules or swap profiles without code
    changes.
    """

    rules_profile: str = "config/rules/embedded-cpp.yaml"
    # 0 = scan the whole project. One model call per file, so on a local model a large
    # repo runs for hours; bound it with `--max-files N` or SECAGENT_SCAN__MAX_FILES when
    # you want a quick pass rather than a complete one.
    max_files: int = 0
    # A budget knob, nothing more. An earlier comment here claimed it was "load-bearing"
    # because raising it to 50000 made the endpoint return 400 Bad Request, and inferred
    # a context-window limit. That was wrong twice over: the failure does not reproduce
    # (the same file at the same cap with the same output budget now answers fine), and
    # the arithmetic never supported it — a 40KB file is ~12k prompt tokens against a
    # 128k window.
    #
    # The 400s are real but they track CONCURRENCY, not this setting. The same ~47k-char
    # payload that failed 13 of 21 rule-group calls at `workers=7` answers 3 of 3 times
    # when issued sequentially, and a larger 48.7k payload answered fine on its own. Seven
    # concurrent ~12k-token requests is ~84k tokens of simultaneous context, which is a
    # server-side aggregate limit rather than a per-request one. If you see 400s on large
    # files, lower `scan.workers` before touching this.
    max_file_bytes: int = 40000
    # Send the file's associated header alongside it as context. OFF BY DEFAULT — this
    # was a good idea that measurement rejected. Read quality/SCAN_HEADER_CONTEXT.md
    # before turning it back on; it is kept as a flag so that result stays reproducible.
    #
    # The idea: `_scan_file` sends one raw source file, so for C++ the model never sees a
    # member's declaration — they live in the header. MEM-006 called `_rtcm_parsing`
    # uninitialized eight times out of eight because `sbf.cpp` declares it zero times and
    # `sbf.h:432` declares it `{nullptr}`. Supplying the header fixed exactly that.
    #
    # What it also did, measured on ashtech.cpp (3 runs per arm, 21/21 coverage both):
    # total findings 48 -> 22, INT-003 13 -> 0, CTL-004 8 -> 0, MEM-006 0 -> 6 — and all
    # six of those MEM-006 hits are one false claim about `_rx_buffer`, which the caller
    # fills before the read. The header does not give the model more context for the same
    # analysis; it moves the model's attention onto the header. Findings about the source
    # body's logic collapse and findings about declarations appear, whether or not they
    # are true. Recall of a known defect did not measurably improve.
    #
    # The failure is attentional, not informational. Anyone revisiting this should supply
    # declarations inline at the point of use, not append a second file to hunt in.
    #
    # The header is labelled as context and deliberately NOT line-numbered, so a finding
    # can only be attributed to the source file's numbering.
    include_header: bool = False
    # Concurrent per-file model calls. Measured against a local Gemma-4, four concurrent
    # requests finished in 42.7s where the same four sequential took 81.6s — 1.9x for
    # free. Beyond ~4 the server is compute-bound and adds little. 1 restores strictly
    # serial behaviour.
    #
    # THAT 1.9x IS ENDPOINT-SPECIFIC AND DOES NOT HOLD EVERYWHERE. Re-measured against
    # the larger `google/gemma-4-26b-a4b-qat`, concurrency is a straight loss — 4 workers
    # had lower throughput than 1 AND failed 2 of 4 calls on a 300s deadline (workings in
    # the `testgen.workers` comment, which was lowered to 2 on that evidence). A single
    # large local model serialises, so per-call latency scales with concurrency and slow
    # calls become failed ones. This default is left at 4 pending a decision rather than
    # changed silently; lower it to 2 if you are running a model that size. The 400 Bad
    # Request responses documented in docs/use-cases.md track the same lever.
    workers: int = 4
    # Hard ceiling on wall-clock per model call for a file, INCLUDING every transport
    # retry and the empty-content escalation. A 3894-byte header was measured consuming a
    # 58988-token budget over 10.5 minutes and still producing nothing; without a bound,
    # one such file can consume an entire run. On expiry the file is reported NOT
    # ANALYZED, which is true and useful, rather than silently waited out.
    #
    # This comment used to claim that and be false: the value was passed to httpx as a
    # PER-REQUEST timeout, which `LLMClient._post_with_retry` then wrapped in a
    # `max_retries` loop (default 3) with backoff, and the empty-content path could issue
    # a second full cycle — so 300 admitted roughly 1800s. `chat()` now converts it to a
    # single deadline shared by every attempt. Note the ceiling is per model call: at
    # `rule_granularity = "category"` a file makes one call per category, so the per-file
    # total is this value times the number of categories.
    per_file_timeout_s: float = 300.0
    # How finely the rule set is sent to the model: "all" (one call), "category" (one
    # call per rule category), or "rule" (one call per rule).
    #
    # This trades wall time for recall. Measured on a 3.4KB C++ file at temperature 1.0:
    #
    #     all        32 rules, 1 call      67s      3 findings
    #     category   7 calls               8.5 min  8 findings
    #     rule       32 calls              23.2 min 16 findings
    #
    # Default "category" because it is what shipped — nobody's findings should change
    # underneath them — NOT because it is a known optimum. And deliberately not "rule"
    # despite it finding the most: whether those extra findings are true positives is
    # unmeasured, and measured C precision on cFS is 25-50% depending on whether a
    # missing NULL check nobody can reach counts (quality/PRECISION_C_TEMP10.md), so more
    # findings may be more noise. Worse for this argument: six of the nine true positives
    # in that sample were TWO defects, re-reported by every category that could see them —
    # so a finding count overstates what finer granularity actually buys.
    #
    # An earlier version of this comment claimed splitting was "the difference between an
    # answer and no answer" because 32 rules in one call returned empty. That was an
    # artifact of `llm.temperature` defaulting to 0.2, which drove the model into
    # degenerate repetition; see `scan.temperature` below. The single call works fine.
    rule_granularity: Literal["all", "category", "rule"] = "category"
    # Deprecated alias for `rule_granularity` (True -> "category", False -> "all"). Kept
    # so existing configs keep working rather than silently changing behaviour; consulted
    # only when `rule_granularity` was not itself set. See `effective_granularity`.
    split_rules_by_category: bool = True
    # Output budget per file. 0 = follow llm.max_output_tokens (floor 900). A reasoning
    # model spends part of this before emitting content, so too small a value produces an
    # empty response, a retry, and roughly double the wall time.
    max_tokens: int = 0
    # Sampling temperature for rule-checking calls ONLY — deliberately not the global
    # `llm.temperature`, which summarisation may legitimately want low for determinism.
    # "Is this code safe?" is not that kind of question. See `effective_granularity`'s
    # sibling note: at the global 0.2 default this model fell into degenerate repetition
    # (one run repeated a single finding line 334 times, hit the 16384-token cap and
    # returned nothing), and every scan failure previously blamed on file size or model
    # capacity was that. 0 means "follow llm.temperature".
    temperature: float = _SCAN_TEMPERATURE_DEFAULT
    # How many times to scan each file, aggregating the findings.
    #
    # Individual runs are NOT reproducible: measured on one file with one rule group,
    # twelve runs produced a mean pairwise Jaccard of 0.00 — no finding was ever reported
    # twice (quality/SCAN_REPRODUCIBILITY.md). Lowering the temperature does not fix it.
    # So a single run is a sample, and rerunning gives a user a different answer.
    #
    # Aggregating over N runs is the fix, and it buys a confidence signal for free: every
    # finding carries the fraction of runs it appeared in, so a 5-of-5 finding is
    # distinguishable from a 1-of-5 one. Default 1 preserves today's behaviour exactly.
    runs: int = 1
    # Drop findings seen in fewer than this fraction of runs. 0.0 keeps everything, which
    # is the default because a threshold that silently removes findings is the failure
    # this project keeps finding. Whatever is dropped is counted and reported under
    # `filtered` in scan.json and named in a warning — never silent.
    min_run_fraction: float = 0.0
    # Findings are aggregated on (file, rule, line) EXACTLY by default. The model drifts
    # line numbers between runs — one measured group reported 66/70/76/79/84 across three
    # runs for what may be as few as two defects — so exact keying splits one wandering
    # defect into several low-frequency findings, and `run_fraction` is therefore a LOWER
    # BOUND on how stable a finding really is. Set >0 to merge same-file, same-rule
    # findings within that many lines. It is 0 by default because merging can also fuse
    # two genuinely distinct defects, and which window is right is unmeasured.
    line_tolerance: int = 0

    def effective_granularity(self) -> str:
        """Resolve `rule_granularity` against the deprecated `split_rules_by_category`.

        Read at use time rather than validated at construction, because callers (tests
        and library users alike) set these by assignment on an already-built Settings,
        and a construction-time validator would silently not apply to that.

        An explicitly-set `rule_granularity` always wins; the deprecated boolean applies
        only when it alone was set. Pydantic records both cases in `model_fields_set`,
        including on assignment.
        """
        fields_set = self.model_fields_set
        if "rule_granularity" in fields_set:
            return self.rule_granularity
        if "split_rules_by_category" in fields_set:
            return "category" if self.split_rules_by_category else "all"
        return self.rule_granularity


class TestGenConfig(BaseModel):
    """UC5: automatic test generation.

    Output goes to ``out_dir`` — a NEW top-level folder, kept entirely separate from
    the project's own structure. Run UC1 (``secagent docs build``) first for richer
    context.
    """

    out_dir: str = "secagent-tests"
    unit: bool = True            # generate per-file unit tests
    functional: bool = True      # generate component I/O tests from the IO map
    # Per-file model calls run concurrently; 1 restores serial behaviour.
    #
    # LOWERED FROM 4 ON MEASUREMENT. At 4, an evaluation run lost 19 of 23 unit files to
    # timeouts — every truncated-input file among them — while a serial call to the same
    # endpoint returned real content in 24s. Only files under ~9000 chars survived the
    # contention. Measured directly against the deployed endpoint
    # (`google/gemma-4-26b-a4b-qat`), ~3.5KB payload, 300s deadline:
    #
    #     workers=1   wall  58.5s   mean per-call  58s   0/1 failed   1.0 calls/min
    #     workers=2   wall 103.6s   mean per-call  82s   0/2 failed   1.2 calls/min
    #     workers=4   wall 300.0s   mean per-call 223s   2/4 FAILED   0.8 calls/min
    #
    # A single large local model serialises: per-call latency scales with concurrency, so
    # 4 workers has LOWER throughput than 1 and converts slow calls into failed ones. 2 is
    # the only setting that buys anything (+20%) without losing files.
    #
    # WHAT 2 ACTUALLY BUYS, re-measured end to end on the PX4 corpus (25 attempts):
    #
    #     workers=4   6 written   25 min   largest survivor  8,781 chars
    #     workers=2   6 written   49 min   largest survivor 22,912 chars
    #
    # The COUNT does not move. What moves is WHICH files get through: at 4 nothing above
    # ~9KB survived; at 2 a 22.9KB file does (while an 8.8KB one does not — it is
    # stochastic, not a clean threshold). So 2 buys better coverage per surviving file at
    # double the wall clock, and buys nothing in yield. Kept at 2 because losing the large
    # files means losing the most complex code, but the honest summary is that lowering
    # this fixed the wrong problem — see the top open item in docs/use-cases.md (UC5).
    #
    # The comment on `scan.workers` still cites "four concurrent finished in 42.7s where
    # four sequential took 81.6s — 1.9x for free". That was a different, smaller model on
    # a different server. It does not hold here, and it is not a general property.
    workers: int = 2
    # Temperature for generation calls. 0 means "follow llm.temperature".
    #
    # THE LARGEST DEFECT IN UC5 WAS THIS. 19-21 of 25 files produced "no usable test
    # content" — unmoved by `workers` and unmoved by the framework convention — because
    # testgen had no temperature of its own and ran on `llm.temperature = 0.2`. The same
    # global did the same thing to scan, which is why `scan.temperature` exists; testgen
    # was never checked.
    #
    # Measured directly against the deployed endpoint (`google/gemma-4-26b-a4b-qat`), 9
    # files that had failed in BOTH corpus runs, one call each per arm:
    #
    #     temperature 0.2   produced content 1 of 9   mean duplicate 12-gram rate 0.257
    #     temperature 1.0   produced content 7 of 9   mean duplicate 12-gram rate 0.080
    #
    # The failure signature is exactly scan's: `finish_reason="length"`, EMPTY content,
    # and 21-47k characters of `reasoning_content` looping on itself before the budget
    # runs out. At 0.2 that was 8 of 9 calls.
    #
    # 1.0 rather than scan's 0.7 because 1.0 is what was measured here. 0.7 was NOT
    # tested on testgen, and copying a value across on the assumption that what held for
    # scan holds here is the mistake that left this undiagnosed for the whole programme.
    #
    # Caveat kept deliberately: one call per cell, and this model is stochastic — the same
    # file at 0.2 returned 2469 characters in one call and 0 in another. 1-of-9 against
    # 7-of-9 is far larger than that noise, but it is not a per-file guarantee.
    temperature: float = 1.0
    per_file_timeout_s: float = 300.0
    max_unit_files: int = 30
    max_functional_components: int = 15
    max_file_bytes: int = 24000  # cap per-file source sent to the model
    # Test framework per language (informational; steers the generated code).
    frameworks: dict[str, str] = Field(
        default_factory=lambda: {
            "Python": "pytest",
            "C": "Unity",
            "C++": "GoogleTest",
            "JavaScript": "Jest",
            "TypeScript": "Vitest",
            "Go": "go test",
            "Java": "JUnit 5",
            "Rust": "cargo test",
            "C#": "xUnit",
            "Ruby": "RSpec",
        }
    )


class AnalysisConfig(BaseModel):
    """UC3: C/C++ static analysis via IKOS."""

    # IKOS report filters (passed through to ikos --status-filter / --analyses-filter).
    status_filter: str = "error,warning"
    analyses_filter: str = ""
    # Cap how many findings get an LLM triage note (0 disables triage).
    max_triage: int = 10
    # IKOS speed/precision knobs. The default (empty) uses IKOS's precise defaults, which
    # are accurate but can be very slow on large/pointer-heavy code (e.g. NASA cFS) — the
    # pointer analysis is the bottleneck. For a fast, imprecise sweep set domain="interval"
    # + no_pointer=True; for actionable results keep the defaults and scope to a few files.
    domain: str = ""            # IKOS abstract domain, e.g. "interval" (empty = default)
    procedural: str = ""        # "inter" | "intra" (empty = IKOS default, inter)
    no_pointer: bool = False    # disable the (slow, precise) pointer analysis
    # Analyzing a *library* translation unit (no main): secagent enumerates its functions as
    # entry points. compile_db supplies the -I/-D flags cFS-style code needs to compile.
    library_mode: bool = False
    compile_db: str = ""            # path to a compile_commands.json (target flag lookup)
    compile_flags: list[str] = []   # explicit -I/-D/-U flags (override the compile_db)
    entry_points: list[str] = []    # explicit IKOS entry points (override library_mode)


class MarkingConfig(BaseModel):
    """CUI / classification marking (CMMC-6 / NIST 800-171 MP.3.8.4).

    When ``banner`` is non-empty, generated documentation and merge-request comments
    carry the marking. Example: ``"CUI"`` or ``"CUI//SP-PRVCY"``. Empty disables it.
    """

    banner: str = ""


class NetworkConfig(BaseModel):
    """Egress controls (CMMC-3 / NIST 800-171 AC.3.1.3, SC.3.13.6/.8).

    Disabled by default. ``require_tls`` refuses non-HTTPS endpoints (loopback is
    exempt — that traffic never leaves the host). ``allowed_hosts`` is an egress
    allow-list of hostnames; empty means no allow-list.
    """

    require_tls: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)


class AuditConfig(BaseModel):
    """Structured audit logging (CMMC-1 / NIST 800-171 AU controls).

    Disabled by default; enable in CMMC/CUI deployments. Records are append-only
    JSONL chained with SHA-256 so tampering is detectable; forward the file to a SIEM
    and restrict its permissions.
    """

    enabled: bool = False
    # Path to the JSONL audit log (use an absolute, protected, SIEM-forwarded path).
    path: str = ".secagent/audit/audit.jsonl"
    # Identity recorded in each event; falls back to $SECAGENT_PRINCIPAL. This is the
    # SERVICE/process identity (e.g. "service:secagent-bot"), not the end user — chat
    # interactions (UC101) additionally carry a per-request `end_user` (the Mattermost
    # username), recorded via AuditLogger.record_chat, so one bot principal does not
    # collapse many different people into a single attribution.
    principal: str = ""
    # Also echo each record to stderr (useful for container log collection).
    echo_stderr: bool = False
    # Chat interaction content (the user's message, the bot's reply) is CUI-sensitive.
    # False (default): AuditLogger.record_chat writes only a SHA-256 digest of each —
    # CUI-free, safe to forward to an unrestricted SIEM. True: the verbatim text is
    # recorded too, and the record is tagged `cui: true` so it can be routed, retained,
    # or access-controlled as CUI downstream. Only affects record_chat; every other
    # event type never carries message content either way.
    capture_content: bool = False


# Pinned LeanCTX versions (supply-chain — never floating). The `lean-ctx` binary and the
# `pi-lean-ctx` npm extension track the same release; the Python client is versioned
# separately. Bump deliberately, re-verifying against the deployed daemon.
LEANCTX_VERSION = "3.9.17"          # lean-ctx binary + pi-lean-ctx (npm)
LEANCTX_CLIENT_VERSION = "0.1.0"    # lean-ctx-client (PyPI — the SDK secagent imports)


class LeanCtxConfig(BaseModel):
    """LeanCTX context-compression integration (see docs/leanctx.md).

    `LeanCTX <https://github.com/yvgude/lean-ctx>`_ (Apache-2.0) is a local
    context-compression layer: agent-side ``ctx_*`` tools for pi, plus a wire compressor
    that shrinks model requests before they reach SecRouter (typically far fewer tokens).
    It is ON by default and **locked down for the CMMC/air-gapped posture** — every field
    below defaults to the safe choice.

    CUI note: LeanCTX sees prompt content, so it sits INSIDE the accreditation boundary.
    Its endpoint is loopback-only, its update-check + telemetry are disabled, and its
    persistent memory is OFF by default (nothing writes potential CUI to disk). It reuses
    the suite's egress/audit posture (see :class:`NetworkConfig` / :class:`AuditConfig`)
    rather than opening a new network path.
    """

    # Master switch. When false, secagent + pi run with NO LeanCTX at all — nothing is
    # installed, configured, or routed through it, and behaviour is exactly as it was
    # before this section existed (a clean kill-switch for a strict deployment).
    enabled: bool = True

    # LeanCTX's local daemon (its OpenAI-shaped /v1 HTTP API — what the SDK talks to and
    # the wire proxy exposes). ALWAYS loopback: LeanCTX sees CUI, so it must never bind a
    # routable address. ``secagent doctor`` flags a non-loopback host here (see is_loopback).
    endpoint: str = "http://127.0.0.1:4444"

    # pi tool exposure: "additive" keeps pi's builtins (read/bash/grep/…) alongside the
    # ctx_* tools; "replace" exposes only the compressed ctx_* tools (LEAN_CTX_PI_MODE).
    pi_mode: Literal["additive", "replace"] = "additive"

    # Register LeanCTX's advanced MCP tools with pi (ctx_session/knowledge/semantic_search/
    # repomap/callgraph/impact/pack). OFF by default: several read the persistent store
    # (below) and widen the tool surface; the always-available CLI-backed ctx_* tools give
    # the compression wins without it (LEAN_CTX_PI_ENABLE_MCP).
    pi_enable_mcp: bool = False

    # Compress secagent's OWN SecRouter calls (Mattermost chat bridge UC101, MR review
    # UC100) through the local daemon before posting. Independent of the pi-side wire
    # compression. GRACEFUL: if the daemon is unreachable the request is sent uncompressed
    # rather than blocked — a compression outage must never drop a governed request.
    compress_own_calls: bool = True

    # PERSISTENT CONTEXT/KNOWLEDGE STORE (LeanCTX session + knowledge memory). OFF by
    # default: enabling it writes (potentially CUI) context to disk on the secagent host —
    # data at rest inside the accreditation boundary. When true, the store is kept under
    # ``state_dir``, owner-only, and MUST be treated as CUI (marked, protected, in
    # media-protection scope). See docs/cmmc.md.
    persist_context: bool = False
    # Where LeanCTX keeps its state (config + any enabled store), owner-only, inside the
    # boundary. Also the base handed to LeanCTX so it never falls back to $HOME defaults.
    state_dir: str = "~/.secagent/leanctx"

    # ── suite-enforced lockdown (applied to LeanCTX's env/config by onboarding regardless;
    #    surfaced here so ``secagent doctor`` can VERIFY them and fail loud on drift) ──
    # Disable LeanCTX's update-check phone-home (LEAN_CTX_NO_UPDATE_CHECK=1). Air-gap-safe.
    no_update_check: bool = True
    # Apply ``lean-ctx harden`` / LEAN_CTX_HARDEN=1 — tightens the MCP config + shell surface.
    harden: bool = True
    # Keep LeanCTX telemetry OFF (it is opt-in upstream; the suite never enables it).
    telemetry: bool = False
    # Cache-aware history keeps the request prefix byte-stable so SecRouter/SecLLM prompt
    # caching keeps hitting; NEVER "rolling" on a cached rail — it rewrites a stable message
    # every turn, turning cheap cache reads into full-price writes (LEAN_CTX_PROXY_HISTORY_MODE).
    proxy_history_mode: Literal["cache-aware", "rolling", "off"] = "cache-aware"

    # Pinned versions — supply-chain control (never floating). See the module constants above.
    version: str = LEANCTX_VERSION
    client_version: str = LEANCTX_CLIENT_VERSION

    @property
    def is_loopback(self) -> bool:
        """Whether ``endpoint`` is loopback-only — the CUI-containment invariant
        ``secagent doctor`` enforces (a routable LeanCTX would expose CUI prompts)."""
        from urllib.parse import urlsplit

        return (urlsplit(self.endpoint).hostname or "") in ("127.0.0.1", "::1", "localhost")


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_prefix="SECAGENT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    llm: LLMConfig = Field(default_factory=LLMConfig)
    leanctx: LeanCtxConfig = Field(default_factory=LeanCtxConfig)
    secsso: SecSSOConfig = Field(default_factory=SecSSOConfig)
    gitlab: GitLabConfig = Field(default_factory=GitLabConfig)
    mattermost: MattermostConfig = Field(default_factory=MattermostConfig)
    affordances: AffordanceConfig = Field(default_factory=AffordanceConfig)
    diagrams: DiagramsConfig = Field(default_factory=DiagramsConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    fips: FIPSConfig = Field(default_factory=FIPSConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    marking: MarkingConfig = Field(default_factory=MarkingConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    testgen: TestGenConfig = Field(default_factory=TestGenConfig)

    def safe_dict(self) -> dict[str, Any]:
        """Config dump with secrets redacted — safe to log."""
        data = self.model_dump()
        if data.get("llm", {}).get("api_key"):
            data["llm"]["api_key"] = "***"
        if data.get("gitlab", {}).get("token"):
            data["gitlab"]["token"] = "***"
        if data.get("gitlab", {}).get("webhook_secret"):
            data["gitlab"]["webhook_secret"] = "***"
        if data.get("mattermost", {}).get("bot_token"):
            data["mattermost"]["bot_token"] = "***"
        if data.get("mattermost", {}).get("webhook_secret"):
            data["mattermost"]["webhook_secret"] = "***"
        # secsso holds no secret value itself (client_secret_env is only a variable
        # NAME — see SecSSOConfig), so there is nothing to redact there.
        return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _user_config_path() -> Path:
    """``~/.secagent/config.yaml`` — the per-user layer ``secagent init`` writes.

    A function (not a module-level constant) so it re-reads ``$HOME`` on every call
    rather than freezing whatever it was at import time — the same reason
    ``secsso.py``'s ``_cache_path`` is a function. That matters for tests: importing
    this module once and then ``monkeypatch.setenv("HOME", tmp_path)`` in a later test
    must still change what this resolves to.
    """
    return Path("~/.secagent/config.yaml").expanduser()


def _load_yaml_config_layer(p: Path) -> dict[str, Any]:
    """Load + validate one YAML config layer. Shared by the per-user file and
    ``--config``/``$SECAGENT_CONFIG`` so both get the identical "unknown section"
    guard (see ``load_settings``'s docstring on why that guard exists)."""
    loaded = yaml.safe_load(p.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config file must be a YAML mapping: {p}")
    unknown = sorted(set(loaded) - set(Settings.model_fields))
    if unknown:
        known = ", ".join(sorted(Settings.model_fields))
        raise ValueError(
            f"unknown config section(s) in {p}: {', '.join(unknown)}. "
            f"Valid sections: {known}"
        )
    return loaded


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from defaults, an optional per-user file, an optional explicit
    file, then environment vars.

    Resolution order (lowest to highest precedence):
      1. Model defaults.
      2. ``~/.secagent/config.yaml``, if present. Written by ``secagent init`` for a
         developer's own SecRouter/SecSSO wiring; a clearly per-user, opt-in file —
         when it does not exist (the common case before ``secagent init``), this layer
         is a no-op and behavior is exactly what it was before this layer existed.
      3. YAML file (``config_path`` arg, else ``$SECAGENT_CONFIG``).
      4. ``SECAGENT_*`` environment variables (handled by pydantic-settings).
    """
    file_values: dict[str, Any] = {}

    user_config = _user_config_path()
    if user_config.exists():
        file_values = _load_yaml_config_layer(user_config)

    path = config_path or os.environ.get("SECAGENT_CONFIG")
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        # Reject unknown top-level sections rather than ignoring them. A misspelled or
        # misplaced key ("testgen:" written as "test_gen:") silently changed nothing, so
        # the run proceeded with defaults and the user drew conclusions about settings
        # that were never applied. Config that does nothing must not look like config
        # that worked.
        file_values = _deep_merge(file_values, _load_yaml_config_layer(p))

    # Desired precedence: defaults < per-user file < --config/SECAGENT_CONFIG file <
    # environment. pydantic-settings applies env at construction, so we compute which
    # keys env actually overrode and layer those on top of the file values (env wins;
    # file fills the rest).
    env_settings = Settings()
    env_overrides = _diff(_pristine_dump(), env_settings.model_dump())
    merged = _deep_merge(file_values, env_overrides)
    return Settings(**merged)


def _pristine_dump() -> dict[str, Any]:
    """Settings with built-in defaults only (sub-model defaults, no env applied)."""
    return Settings.model_validate(
        {
            "llm": LLMConfig().model_dump(),
            "leanctx": LeanCtxConfig().model_dump(),
            "secsso": SecSSOConfig().model_dump(),
            "gitlab": GitLabConfig().model_dump(),
            "mattermost": MattermostConfig().model_dump(),
            "affordances": AffordanceConfig().model_dump(),
            "diagrams": DiagramsConfig().model_dump(),
            "persona": PersonaConfig().model_dump(),
            "fips": FIPSConfig().model_dump(),
            "audit": AuditConfig().model_dump(),
            "network": NetworkConfig().model_dump(),
            "marking": MarkingConfig().model_dump(),
            "analysis": AnalysisConfig().model_dump(),
            "scan": ScanConfig().model_dump(),
            "testgen": TestGenConfig().model_dump(),
        }
    ).model_dump()


def _diff(defaults: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, cur in current.items():
        base = defaults.get(key)
        if isinstance(cur, dict) and isinstance(base, dict):
            sub = _diff(base, cur)
            if sub:
                out[key] = sub
        elif cur != base:
            out[key] = cur
    return out
