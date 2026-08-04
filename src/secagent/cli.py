"""The ``secagent`` command-line interface.

Heavy subsystems are imported lazily inside each command so that lightweight
commands (``doctor``, ``version``, ``config``) work even when optional extras
(Sphinx, FastAPI) are not installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import load_settings

app = typer.Typer(
    name="secagent",
    help="Looping pi-agents for local (Gemma) models — FIPS-compatible.",
    no_args_is_help=True,
    add_completion=False,
)
docs_app = typer.Typer(help="Use case 1: deep-dive Sphinx docs with Draw.io diagrams.")
review_app = typer.Typer(help="Use case 2: GitLab merge-request review.")
analyze_app = typer.Typer(help="Use case 3: C/C++ static analysis via IKOS.")
mcp_app = typer.Typer(help="Expose secagent tools over the Model Context Protocol.")
aff_app = typer.Typer(
    help="Affordance queries (bash-callable; the surface the pi agent drives)."
)
audit_app = typer.Typer(help="Audit log operations (CMMC-1 / NIST 800-171 AU).")
chat_app = typer.Typer(help="Use case 101: Mattermost chat-ops front end.")
app.add_typer(docs_app, name="docs")
app.add_typer(review_app, name="review")
app.add_typer(analyze_app, name="analyze")
app.add_typer(mcp_app, name="mcp")
app.add_typer(aff_app, name="affordance")
app.add_typer(audit_app, name="audit")
app.add_typer(chat_app, name="chat")

console = Console()


def _settings(config: str | None):
    return load_settings(config)


@app.command()
def version() -> None:
    """Print the secagent version."""
    from . import __version__

    console.print(f"secagent {__version__}")


@app.command()
def doctor(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config YAML"),
    probe: bool = typer.Option(
        False, "--probe",
        help="Probe the configured LLM endpoint, plus (best-effort) SecRouter/SecSSO reachability"),
    fix: bool = typer.Option(
        False, "--fix",
        help="Pre-create + harden (0700) the token-cache directories before checking"),
) -> None:
    """Run FIPS + dependency self-checks, plus developer-onboarding status: Python /
    secagent / pi / Node versions, whether `secagent init` has run, and whether
    `secagent login` has a live cached token."""
    from .doctor import doctor_failed, fix_permissions, run_doctor

    settings = _settings(config)
    if fix:
        fixed = fix_permissions(settings)
        console.print("[green]--fix: hardened (0700):[/green] "
                     + ", ".join(str(p) for p in fixed))
    checks = run_doctor(settings, probe_endpoint=probe)

    table = Table(title="secagent doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for c in checks:
        if c.ok and c.severity != "warn":
            status = "[green]OK[/green]"
        elif c.ok and c.severity == "warn":
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[red]FAIL[/red]"
        table.add_row(c.name, status, c.detail)
    console.print(table)

    if doctor_failed(checks):
        console.print("[red]doctor: one or more required checks failed[/red]")
        raise typer.Exit(code=1)
    console.print("[green]doctor: ok[/green]")


@audit_app.command("verify")
def audit_verify(
    path: Path | None = typer.Argument(None, help="Audit log path (default: config)"),
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Verify the integrity of the audit log's SHA-256 hash chain."""
    from .audit import verify_chain

    settings = _settings(config)
    target = str(path) if path else settings.audit.path
    ok, message = verify_chain(target)
    color = "green" if ok else "red"
    console.print(f"[{color}]{'OK' if ok else 'FAIL'}[/{color}] {target}: {message}")
    if not ok:
        raise typer.Exit(code=1)


@app.command("config")
def show_config(
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Print the effective configuration (secrets redacted)."""
    settings = _settings(config)
    console.print_json(json.dumps(settings.safe_dict()))


@app.command()
def token(
    config: str | None = typer.Option(None, "--config", "-c"),
    user: bool = typer.Option(
        False, "--user", "-u",
        help="Print YOUR OWN cached token from `secagent login`, not the service token"),
) -> None:
    """Print a SecSSO bearer token: secagent's service identity by default
    (client_credentials), or — with ``--user`` — the developer's own token cached by
    `secagent login` (OIDC device authorization).

    Both are cached on disk until near expiry (see ``secsso.token_cache_path`` /
    ``secsso.user_token_cache_path`` / ``expiry_buffer_s``), so either form is cheap
    to invoke on every request. That is exactly what pi does with a ``models.json``
    ``"!command"`` ``apiKey`` — it re-runs the command on every actual LLM call rather
    than caching the result itself — so a provider configured with ``apiKey:
    "!secagent token --user"`` gets a fresh-enough token on every call without ever
    touching ``models.json`` again. Set ``SECAGENT_LLM__API_KEY="!secagent token
    --user"`` to have secagent's OWN LLM calls (index/scan/testgen/...) reuse the
    identical helper as the logged-in developer; drop ``--user`` for the service
    identity, unchanged from before this flag existed.

    STDOUT on success is exactly the raw token and nothing else — safe to use
    directly as a credential value. All diagnostics go to stderr; a failure (with
    ``--user`` and no prior `secagent login`, in particular) exits non-zero and
    prints nothing to stdout.
    """
    import sys

    from .secsso import SecSSOError, get_token, get_user_token

    settings = _settings(config)
    try:
        # Deliberately NOT `console.print`: that goes to stdout with rich styling by
        # default, and stdout here must be exactly the token — nothing else, no ANSI
        # escapes — since pi's `!command` resolution and LLMConfig.api_key both use
        # this output verbatim as a credential.
        print(get_user_token(settings.secsso) if user else get_token(settings.secsso))
    except SecSSOError as exc:
        print(f"secagent token: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None


@app.command()
def login(
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Authenticate as YOURSELF against SecSSO (OIDC device authorization, RFC 8628).

    Prints a verification URL + code; approve it in any browser (this machine or a
    phone), and the CLI polls until you do. Caches the result at
    ``secsso.user_token_cache_path`` (0600) for `secagent token --user` — and, via
    that, pi's ``!secagent token --user`` ``apiKey`` (see `secagent init`) — to reuse.
    Safe to re-run any time: it always re-authenticates and overwrites the cache.
    """
    import sys

    from .secsso import DeviceAuthorization, SecSSOError
    from .secsso import login as device_login

    settings = _settings(config)

    def on_prompt(device: DeviceAuthorization) -> None:
        console.print("[bold]Sign in to SecSSO to continue:[/bold]")
        if device.verification_uri_complete:
            console.print(f"  {device.verification_uri_complete}")
            console.print(f"  (or open {device.verification_uri} and enter code: "
                         f"[bold]{device.user_code}[/bold])")
        else:
            console.print(f"  {device.verification_uri}")
            console.print(f"  Enter code: [bold]{device.user_code}[/bold]")
        console.print("Waiting for approval...")

    try:
        result = device_login(settings.secsso, on_prompt=on_prompt)
    except SecSSOError as exc:
        print(f"secagent login: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from None

    who = result.email or result.sub
    if who:
        console.print(f"[green]Logged in as {who}.[/green]")
    else:
        console.print("[green]Logged in.[/green]")
    cache_path = Path(settings.secsso.user_token_cache_path).expanduser()
    console.print(f"Token cached at {cache_path}")


@app.command()
def logout(
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Delete the per-user SecSSO token cached by `secagent login`."""
    from .secsso import logout as device_logout

    settings = _settings(config)
    if device_logout(settings.secsso):
        console.print("[green]Logged out (removed the cached user token).[/green]")
    else:
        console.print("Nothing to log out of (no cached user token).")


@app.command()
def init(
    domain: str | None = typer.Option(
        None, "--domain", help="Suite domain, e.g. sec.internal -- derives SecRouter/SecSSO URLs"),
    secrouter_url: str | None = typer.Option(
        None, "--secrouter-url",
        help="Override the derived SecRouter base URL (full URL incl. /v1)"),
    secsso_url: str | None = typer.Option(
        None, "--secsso-url",
        help="Override the derived SecSSO base URL (no path suffix, e.g. https://secsso.example.com:9000)"),
    model: str = typer.Option("balanced", "--model", help="Model id to register with pi and use"),
    force: bool = typer.Option(
        False, "--force", help="Back up + replace existing models.json/config.yaml"),
) -> None:
    """One-time per-developer onboarding: wire up pi and secagent's own CLI at a
    SecRouter deployment, so both authenticate as YOU (via `secagent login`
    afterwards).

    Writes ``~/.pi/agent/models.json`` (a ``secrouter`` provider only) and
    ``~/.secagent/config.yaml`` (this CLI's own per-user ``llm``/``secsso``
    settings — auto-loaded by every later `secagent` invocation, see
    docs/configuration.md), both using ``!secagent token --user`` as the credential —
    never a stored secret. Safe to re-run (idempotent): only the ``secrouter``
    models.json provider and the ``llm``/``secsso`` config sections are touched;
    anything else already in those files is left alone.
    """
    from .onboarding import OnboardingError, run_init

    try:
        result = run_init(
            domain=domain, secrouter_url=secrouter_url, secsso_url=secsso_url,
            model=model, force=force,
        )
    except OnboardingError as exc:
        console.print(f"[red]secagent init: {exc}[/red]")
        raise typer.Exit(code=1) from None

    for line in result.summary_lines():
        console.print(line)


@app.command()
def index(
    path: Path = typer.Argument(..., help="Repository root to index"),
    config: str | None = typer.Option(None, "--config", "-c"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Heuristic-only summaries"),
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Re-read and re-parse every file (ignores the incremental SHA skip and the "
             "AST parse cache). Does NOT regenerate cached LLM summaries — use "
             "--refresh-summaries for that, or both together."),
    refresh_summaries: bool = typer.Option(
        False, "--refresh-summaries",
        help="Regenerate LLM summaries even if cached for this model (re-evaluation)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show progress on stderr"),
) -> None:
    """Build or update the affordance store for a repository.

    The default pass asks the LLM for a one-line purpose per file, which dominates the
    runtime on a first index (minutes on a large repo, against a local model). Pass
    ``--no-llm`` for a structural-only pass that is orders of magnitude faster and still
    gives you the project map, symbols, the IO map, and the call map — everything except
    the written summaries. Re-indexing is incremental, so only the first run is slow.
    """
    if verbose:
        from .progress import enable_verbose

        enable_verbose()
    from .affordances.api import index_repo

    settings = _settings(config)
    if no_llm:
        settings.affordances.llm_summaries = False
    settings.affordances.refresh_summaries = refresh_summaries
    report = index_repo(path, settings, refresh=refresh)
    console.print_json(json.dumps(report))


def _aff_store(repo: Path, config: str | None):
    """Open (auto-indexing if needed) the affordance store for a repo."""
    from .affordances import queries

    settings = _settings(config)
    return queries.ensure_indexed(repo, settings), settings


@aff_app.command("structure")
def aff_structure(repo: Path = typer.Argument(...), config: str | None = typer.Option(None, "-c")):
    """Print the project structure outline."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.structure(store))
    finally:
        store.close()


@aff_app.command("io")
def aff_io(repo: Path = typer.Argument(...), config: str | None = typer.Option(None, "-c")):
    """Print the IO map (imports, endpoints, outbound calls, datastores)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.io(store))
    finally:
        store.close()


@aff_app.command("components")
def aff_components(repo: Path = typer.Argument(...), config: str | None = typer.Option(None, "-c")):
    """Print the components as JSON."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.components(store))
    finally:
        store.close()


@aff_app.command("cache")
def aff_cache(
    repo: Path = typer.Argument(...),
    prune_days: float | None = typer.Option(
        None, "--prune", help="Delete cache entries not modified in the last N days"),
    clear: bool = typer.Option(False, "--clear", help="Delete ALL cache entries"),
    config: str | None = typer.Option(None, "-c"),
):
    """Report or reclaim the LLM cache (``.secagent/cache``).

    Default: entry count + total bytes. ``--prune N`` removes entries older than N days;
    ``--clear`` removes everything. The cache is content-addressed and safe to drop — a
    missing entry just re-invokes the model on the next index. Entries from superseded
    file versions, prompt bumps, or retired models accumulate and are never overwritten,
    so periodic pruning reclaims that space.
    """
    from .affordances.store import AffordanceStore

    settings = _settings(config)
    store = AffordanceStore(repo, settings.affordances.store_dir)
    try:
        if clear and prune_days is not None:
            console.print("[red]pass only one of --clear / --prune[/red]")
            raise typer.Exit(code=1)
        if clear:
            result = {"action": "clear", "removed": store.cache.clear()}
        elif prune_days is not None:
            result = {"action": "prune", "older_than_days": prune_days,
                      "removed": store.cache.prune(prune_days)}
        else:
            result = {"action": "stats", **store.cache.stats()}
    finally:
        store.close()
    console.print_json(json.dumps(result))


@aff_app.command("summary")
def aff_summary(
    repo: Path = typer.Argument(...),
    path: str = typer.Argument(..., help="Repo-relative file path"),
    config: str | None = typer.Option(None, "-c"),
):
    """Print one file's affordance summary as JSON."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.summary(store, path))
    finally:
        store.close()


@aff_app.command("functions")
def aff_functions(
    repo: Path = typer.Argument(...),
    path: str = typer.Argument(..., help="Repo-relative file path"),
    config: str | None = typer.Option(None, "-c"),
):
    """Print a file's functions (signature + description) as JSON."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.functions(store, path))
    finally:
        store.close()


@aff_app.command("calls")
def aff_calls(
    repo: Path = typer.Argument(...),
    path: str = typer.Argument("", help="Optional file to filter the call map to"),
    config: str | None = typer.Option(None, "-c"),
):
    """Print the inter-file call map (file -> file: callees)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.calls(store, path))
    finally:
        store.close()


@aff_app.command("callers")
def aff_callers(
    repo: Path = typer.Argument(...),
    symbol: str = typer.Argument(..., help="Function whose callers to find"),
    config: str | None = typer.Option(None, "-c"),
):
    """Who calls a function — the reverse call map for impact analysis (JSON)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.callers(store, symbol))
    finally:
        store.close()


@aff_app.command("types")
def aff_types(
    repo: Path = typer.Argument(...),
    name: str = typer.Argument("", help="Optional type-name substring filter"),
    config: str | None = typer.Option(None, "-c"),
):
    """Declared types + inheritance (bases/interfaces) (JSON). Python is populated by
    `secagent index`; other languages need a heavy backend (e.g. `analyze deep` for C#)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.types(store, name))
    finally:
        store.close()


@aff_app.command("summaries")
def aff_summaries(
    repo: Path = typer.Argument(...),
    config: str | None = typer.Option(None, "-c"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write JSON here instead of stdout"),
    component: str = typer.Option(
        "", "--component",
        help="Scope to a component / path prefix (the whole-repo dump is large).",
    ),
    raw: bool = typer.Option(
        False, "--raw",
        help="Dump the raw cached values (purposes + docs) with counts, instead of the "
             "attributed manifest. Entries are hash-keyed, not file/function-labelled.",
    ),
):
    """Dump the per-model summaries manifest (file purposes + function docs) as JSON.

    Primarily a model-evaluation view: records which model produced them; diff two models'
    output to evaluate quality. Pass ``--component`` to scope it for agent use. With
    ``--raw``, dumps the literal content-cache entries and per-kind counts instead.
    """
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        data = queries.raw_cache(store) if raw else queries.summaries(store, component)
    finally:
        store.close()
    if out is not None:
        out.write_text(data, encoding="utf-8")
        console.print(f"Wrote {out}")
    else:
        print(data)


@aff_app.command("plan")
def aff_plan(repo: Path = typer.Argument(...), config: str | None = typer.Option(None, "-c")):
    """UC0: bin components by language + recommend the secagent tools to run on each (JSON)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.plan(store))
    finally:
        store.close()


@aff_app.command("find-symbol")
def aff_find_symbol(
    repo: Path = typer.Argument(...),
    name: str = typer.Argument(..., help="(partial) symbol name"),
    config: str | None = typer.Option(None, "-c"),
):
    """Find functions/classes by name (JSON)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.find_symbol(store, name))
    finally:
        store.close()


@aff_app.command("search")
def aff_search(
    repo: Path = typer.Argument(...),
    query: str = typer.Argument(...),
    config: str | None = typer.Option(None, "-c"),
):
    """Rank files by relevance to a query (JSON: path + purpose)."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.search(store, query))
    finally:
        store.close()


@aff_app.command("context")
def aff_context(
    repo: Path = typer.Argument(...),
    query: str = typer.Argument(...),
    config: str | None = typer.Option(None, "-c"),
):
    """Assemble a budget-bounded context block for a query (text)."""
    from .affordances import queries

    store, settings = _aff_store(repo, config)
    try:
        print(queries.context(store, query, settings.llm.context_budget_tokens,
                             max_files=settings.affordances.max_context_files))
    finally:
        store.close()


@aff_app.command("verify")
def affordance_verify(
    repo: Path = typer.Argument(..., help="Repository root"),
    target: str = typer.Argument(...,
        help="A file to check, or '-' to read the text from stdin."),
    config: str | None = typer.Option(None, "-c"),
):
    """Check generated text against the index: which paths and symbols are real?

    A cheap hallucination check for anything a model wrote — a docs page, a generated
    test, a review comment. Every path and every `backticked` symbol either exists in
    this repository or it does not, and the index already knows which. Exits non-zero
    when something does not resolve, so it drops into a pipeline.
    """
    import sys as _sys

    from .affordances import grounding

    text = _sys.stdin.read() if target == "-" else Path(target).read_text(
        encoding="utf-8", errors="replace")
    store, _settings = _aff_store(repo, config)
    try:
        result = grounding.check(store, text)
        console.print_json(json.dumps(result.to_dict()))
    finally:
        store.close()
    raise typer.Exit(0 if result.ok else 1)


@aff_app.command("slice")
def aff_slice(
    repo: Path = typer.Argument(...),
    path: str = typer.Argument(...),
    start: int = typer.Option(1, "--start"),
    end: int = typer.Option(50, "--end"),
    config: str | None = typer.Option(None, "-c"),
):
    """Read a bounded, traversal-guarded slice of a real file."""
    from .affordances import queries

    store, _ = _aff_store(repo, config)
    try:
        print(queries.read_slice(store, path, start, end))
    finally:
        store.close()


@app.command("verify-tests")
def verify_tests_cmd(
    repo: Path = typer.Argument(..., help="Repository root"),
    tests: list[str] = typer.Argument(..., help="Test files to grade, repo-relative"),
    max_mutants: int = typer.Option(10, "--max-mutants",
                                    help="Mutants per test (more = slower, finer score)."),
    timeout_s: float = typer.Option(120.0, "--timeout", help="Per build/run, seconds."),
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Grade tests mechanically: does each one compile, pass, and NOTICE a broken change?

    Works on hand-written tests as well as generated ones — arguably better, since there is
    far more hand-written code and "is our existing suite vacuous?" is the question worth
    answering.

    Three gates on disjoint defects. Measured on one real generated C++ file of 10 cases:
    the run gate catches 3 that fail against correct code, and the mutation gate catches 3
    that compile, run, pass and assert nothing because every assertion sits inside a branch
    that never executes. Exits non-zero if anything is `vacuous` or `wrong`.

    Tests are executed in a container with no network. See `secagent/verify/sandbox.py`.
    """
    from .verify import verify_test

    _settings(config)
    outcomes = [verify_test(repo, t, max_mutants=max_mutants, timeout_s=timeout_s)
                for t in tests]
    console.print_json(json.dumps({
        "tests": [o.to_dict() for o in outcomes],
        "summary": {v: sum(1 for o in outcomes if o.verdict == v)
                    for v in sorted({o.verdict for o in outcomes})},
    }))
    if any(o.verdict in ("vacuous", "wrong") for o in outcomes):
        raise typer.Exit(code=1)


@app.command()
def purge(
    path: Path = typer.Argument(..., help="Repository whose affordance store to purge"),
    config: str | None = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Securely delete a repository's affordance store (CMMC-2)."""
    from .affordances.api import purge_store

    settings = _settings(config)
    if not yes:
        typer.confirm(f"Securely delete the affordance store under {path}?", abort=True)
    report = purge_store(path, settings)
    console.print_json(json.dumps(report))


@docs_app.command("build")
def docs_build(
    path: Path = typer.Argument(..., help="Repository root to document"),
    out: Path = typer.Option(Path("secagent-docs"), "--out", "-o", help="Output directory"),
    config: str | None = typer.Option(None, "--config", "-c"),
    no_build: bool = typer.Option(False, "--no-build", help="Generate sources, skip sphinx-build"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Heuristic prose, no LLM endpoint"),
    refresh_summaries: bool = typer.Option(
        False, "--refresh-summaries",
        help="Regenerate LLM summaries/descriptions even if cached (re-evaluation)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show progress on stderr"),
) -> None:
    """Generate comprehensive Sphinx documentation with Draw.io diagrams."""
    if verbose:
        from .progress import enable_verbose

        enable_verbose()
    from .agents.docs.agent import build_docs

    settings = _settings(config)
    if no_llm:
        settings.affordances.llm_summaries = False
    settings.affordances.refresh_summaries = refresh_summaries
    result = build_docs(path, out, settings, run_sphinx=not no_build)
    console.print_json(json.dumps(result))


@app.command()
def testgen(
    repo: Path = typer.Argument(..., help="Repository root to generate tests for"),
    out: Path | None = typer.Option(None, "--out", "-o",
                                    help="Output folder (default: <repo>/secagent-tests)"),
    no_unit: bool = typer.Option(False, "--no-unit", help="Skip per-file unit tests"),
    no_functional: bool = typer.Option(False, "--no-functional",
                                       help="Skip component I/O tests"),
    config: str | None = typer.Option(None, "--config", "-c"),
    verify: bool = typer.Option(
        False, "--verify",
        help="Gate each written test: compiles, passes, and FAILS on broken code. "
             "Vacuous/wrong tests are moved to quarantine/."),
) -> None:
    """UC5: generate unit + functional tests into a separate folder.

    Tip: run UC1 first (`secagent docs build <repo>`) for richer context.

    `--verify` runs the mechanical gates over the result. Without it, `ok: true` means
    only that the model emitted text — measured on one real C++ file, 3 of 10 such tests
    failed against correct code and 3 more asserted nothing at all.
    """
    from .agents.testgen.agent import generate_tests

    settings = _settings(config)
    result = generate_tests(
        repo, settings, out_dir=out, unit=not no_unit, functional=not no_functional,
        verify=verify,
    )
    console.print_json(json.dumps(result))


@app.command()
def scan(
    repo: Path = typer.Argument(..., help="Repository root to scan (C/C++)"),
    out: Path = typer.Option(Path("secagent-scan"), "--out", "-o"),
    rules: str | None = typer.Option(None, "--rules", help="Path to a rules YAML profile"),
    max_files: int | None = typer.Option(
        None, "--max-files",
        help="Cap files scanned (default: the whole project; use this for a quick pass)."),
    paths: list[str] | None = typer.Option(
        None, "--path",
        help="Scan only these repo-relative files (repeatable). Beats --max-files, "
             "which just takes the first N in sorted order."),
    config: str | None = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Per-file progress on stderr. An LLM scan takes minutes per file on a local "
             "model; without this there is no output until it finishes."),
) -> None:
    """UC4: LLM rule-based memory/stability scan against a rule set.

    The rule profile decides the languages (C/C++ by default; see
    config/rules/ for the Rust profile).
    """
    if verbose:
        from .progress import enable_verbose

        enable_verbose()
    from .agents.scan.agent import scan_repo

    settings = _settings(config)
    if rules:
        settings.scan.rules_profile = rules
    if max_files is not None:
        settings.scan.max_files = max_files
    result = scan_repo(repo, settings, out_dir=out, paths=list(paths) if paths else None)
    console.print_json(json.dumps(result))


@analyze_app.command("run")
def analyze_run(
    repo: Path = typer.Argument(..., help="Repository root (for affordance context)"),
    target: Path = typer.Argument(..., help="C/C++ source or .bc file to analyze with IKOS"),
    out: Path = typer.Option(Path("secagent-analysis"), "--out", "-o"),
    config: str | None = typer.Option(None, "--config", "-c"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM triage"),
    library: bool = typer.Option(
        False, "--library",
        help="Target is a library TU (no main): analyze all its functions."),
    compile_db: str | None = typer.Option(
        None, "--compile-db",
        help="compile_commands.json to source the target's -I/-D flags (cFS-style code)."),
    domain: str | None = typer.Option(
        None, "--domain", help="IKOS abstract domain, e.g. 'interval' (default: precise)."),
    no_pointer: bool = typer.Option(
        False, "--no-pointer",
        help="Disable IKOS's pointer analysis — fast but imprecise (many warnings)."),
    max_triage: int = typer.Option(
        -1, "--max-triage",
        help="Findings to triage with the LLM (0 = none, -1 = use config, default 10)."),
) -> None:
    """Run IKOS on a C/C++ target and produce an enriched report.

    For a cFS-style library file: `--library --compile-db build/compile_commands.json`.
    For a fast (imprecise) sweep of heavy code: add `--domain interval --no-pointer`.
    """
    from .agents.analysis.agent import analyze_repo

    settings = _settings(config)
    # `analysis.max_triage` defaulted to 10 with no flag to raise it: a 139-finding
    # run silently triaged 10 and skipped 129, and only an env var could change it.
    if max_triage >= 0:
        settings.analysis.max_triage = max_triage
    if no_llm:
        settings.analysis.max_triage = 0   # no model at all beats any triage budget
    if library:
        settings.analysis.library_mode = True
    if compile_db:
        settings.analysis.compile_db = compile_db
    if domain:
        settings.analysis.domain = domain
    if no_pointer:
        settings.analysis.no_pointer = True
    result = analyze_repo(repo, settings, target=target, out_dir=out, run=True)
    console.print_json(json.dumps(result))


@analyze_app.command("scan")
def analyze_scan_cmd(
    repo: Path = typer.Argument(..., help="Repository root to scan with IKOS"),
    out: Path = typer.Option(Path("secagent-analysis"), "--out", "-o"),
    config: str | None = typer.Option(None, "--config", "-c"),
    compile_db: str | None = typer.Option(
        None, "--compile-db",
        help="compile_commands.json (auto-discovered under the repo if omitted)."),
    library: bool = typer.Option(
        True, "--library/--no-library",
        help="Treat each TU as a library (analyze all functions). On by default."),
    domain: str | None = typer.Option(
        None, "--domain", help="IKOS abstract domain, e.g. 'interval' (default: precise)."),
    no_pointer: bool = typer.Option(
        False, "--no-pointer",
        help="Disable pointer analysis — ~1000x faster on heavy code, but imprecise."),
    jobs: int = typer.Option(0, "--jobs", "-j", help="Parallel TU workers (0 = auto)."),
    tu_timeout: int = typer.Option(
        120, "--tu-timeout", help="Per-TU timeout in seconds (slow TUs are skipped)."),
    max_files: int = typer.Option(0, "--max-files", help="Cap TUs scanned (0 = all)."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM triage"),
    max_triage: int = typer.Option(
        -1, "--max-triage",
        help="Findings to triage with the LLM (0 = none, -1 = use config, default 10)."),
) -> None:
    """Run IKOS across a whole C/C++ repo and aggregate the findings.

    Fast, imprecise sweep of heavy code (e.g. cFS): `--domain interval --no-pointer`.
    Precise (slow) — scope with `--max-files` / a short `--tu-timeout`, or point at a
    subtree. Needs a compile_commands.json (build with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON).
    """
    from .agents.analysis.agent import analyze_scan

    settings = _settings(config)
    # `analysis.max_triage` defaulted to 10 with no flag to raise it: a 139-finding
    # run silently triaged 10 and skipped 129, and only an env var could change it.
    if max_triage >= 0:
        settings.analysis.max_triage = max_triage
    if no_llm:
        settings.analysis.max_triage = 0   # no model at all beats any triage budget
    settings.analysis.library_mode = library
    if compile_db:
        settings.analysis.compile_db = compile_db
    if domain:
        settings.analysis.domain = domain
    if no_pointer:
        settings.analysis.no_pointer = True
    result = analyze_scan(repo, settings, out_dir=out, jobs=jobs,
                          per_tu_timeout=tu_timeout, max_files=max_files)
    console.print_json(json.dumps(result))


@analyze_app.command("ingest")
def analyze_ingest(
    repo: Path = typer.Argument(..., help="Repository root (for affordance context)"),
    ikos_report: Path = typer.Argument(..., help="Existing IKOS JSON report to ingest"),
    out: Path = typer.Option(Path("secagent-analysis"), "--out", "-o"),
    config: str | None = typer.Option(None, "--config", "-c"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM triage"),
    max_triage: int = typer.Option(
        -1, "--max-triage",
        help="Findings to triage with the LLM (0 = none, -1 = use config, default 10)."),
) -> None:
    """Ingest an existing IKOS report and produce an enriched report (no IKOS needed)."""
    from .agents.analysis.agent import analyze_repo

    settings = _settings(config)
    # `analysis.max_triage` defaulted to 10 with no flag to raise it: a 139-finding
    # run silently triaged 10 and skipped 129, and only an env var could change it.
    if max_triage >= 0:
        settings.analysis.max_triage = max_triage
    if no_llm:
        settings.analysis.max_triage = 0   # no model at all beats any triage budget
    result = analyze_repo(repo, settings, ikos_report=ikos_report, out_dir=out, run=False)
    console.print_json(json.dumps(result))


@analyze_app.command("deep")
def analyze_deep(
    repo: Path = typer.Argument(..., help="Repository root to enrich"),
    ingest: Path | None = typer.Option(
        None, "--ingest", help="A secagent-analysis/v1 report (JSON) to ingest into the store"),
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Heavy (compiled) semantic analysis — symbols, types/inheritance, and the call map.

    Runs the optional analyzer container matching the project — Roslyn for C#
    (`make analyzer-dotnet`), rust-analyzer for Rust (`make analyzer-rust`) — offline, and
    ingests its fully-resolved symbols, type hierarchy, and call edges. With ``--ingest``
    it ingests a pre-produced ``secagent-analysis/v1`` report instead (no container needed).
    See ``docs/design/heavy-analysis-pipeline.md``.
    """
    from .affordances import analysis, heavy, queries

    settings = _settings(config)
    if ingest is not None:
        report = analysis.parse_report(json.loads(ingest.read_text(encoding="utf-8")))
    else:
        produced = heavy.run_heavy_analyzer(repo, settings)
        if produced is None:
            console.print(
                "[yellow]No heavy backend ran. Build the analyzer image for your project "
                "(`make analyzer-rust` for Rust, `make analyzer-dotnet` for C#), or pass "
                "--ingest <report.json>.[/yellow]")
            raise typer.Exit(code=2)
        report = produced

    store = queries.ensure_indexed(repo, settings)
    try:
        result = analysis.ingest_report(report, store)
    finally:
        store.close()
    console.print_json(json.dumps({"backend": report.backend, "language": report.language,
                                   **result}))


@review_app.command("mr")
def review_mr(
    project: str = typer.Argument(..., help="GitLab project id or path (group/name)"),
    mr_iid: int = typer.Argument(..., help="Merge request internal id (iid)"),
    config: str | None = typer.Option(None, "--config", "-c"),
    repo: Path | None = typer.Option(None, "--repo", help="Local checkout for affordances"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the review, don't post"),
) -> None:
    """Produce (and optionally post) an initial review for a merge request."""
    from .agents.review.agent import review_merge_request

    settings = _settings(config)
    result = review_merge_request(
        settings, project=project, mr_iid=mr_iid, repo=repo, post=not dry_run
    )
    console.print_json(json.dumps(result))


@review_app.command("serve")
def review_serve(
    config: str | None = typer.Option(None, "--config", "-c"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
    tls_cert: str | None = typer.Option(None, "--tls-cert", help="Server cert (enables HTTPS)"),
    tls_key: str | None = typer.Option(None, "--tls-key", help="Server private key"),
    tls_ca: str | None = typer.Option(None, "--tls-ca", help="Client CA (enables mTLS)"),
) -> None:
    """Run the GitLab webhook receiver for MR + comment events."""
    from .agents.review.webhook import serve

    settings = _settings(config)
    serve(settings, host=host, port=port,
          tls_certfile=tls_cert, tls_keyfile=tls_key, tls_ca_certs=tls_ca)


@review_app.command("poll")
def review_poll(
    project: str = typer.Argument(..., help="GitLab project id or path (group/name)"),
    config: str | None = typer.Option(None, "--config", "-c"),
    repo: Path | None = typer.Option(None, "--repo", help="Local checkout for affordances"),
    once: bool = typer.Option(False, "--once", help="Run a single pass and exit"),
) -> None:
    """Watch a project by polling for open merge requests (webhook-free fallback).

    Each new open MR is reviewed exactly as a webhook delivery would be. The loop
    interval is ``gitlab.poll_interval_s``; use ``--once`` for a single pass (e.g. from
    cron). Suited to air-gapped instances that cannot push webhooks to secagent.

    Which merge requests have already been reviewed is recorded in
    ``gitlab.poll_state_file`` (under ``--repo`` if given, else the working directory), so
    a cron tick does not repost a review that a previous tick already published.
    """
    from .agents.review.webhook import poll_open_merge_requests

    settings = _settings(config)
    state_path = Path(repo or ".") / settings.gitlab.poll_state_file
    poll_open_merge_requests(settings, project, repo=repo, once=once,
                             state_path=state_path)


@chat_app.command("serve")
def chat_serve(
    config: str | None = typer.Option(None, "--config", "-c"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8070, "--port"),
    tls_cert: str | None = typer.Option(None, "--tls-cert", help="Server cert (enables HTTPS)"),
    tls_key: str | None = typer.Option(None, "--tls-key", help="Server private key"),
    tls_ca: str | None = typer.Option(None, "--tls-ca", help="Client CA (enables mTLS)"),
) -> None:
    """Run the Mattermost bot: receive slash commands + outgoing webhooks, route to
    the review/affordance engines, reply in-thread, audit every interaction.

    secagent's own transport (not the pi-mattermost plugin) — mirrors ``review
    serve``'s hardening posture exactly: refuses to start without
    ``mattermost.webhook_secret`` (or an explicit unauthenticated opt-out), and
    supports the same ``--tls-*`` / mTLS options.
    """
    from .chat.webhook import serve

    settings = _settings(config)
    serve(settings, host=host, port=port,
          tls_certfile=tls_cert, tls_keyfile=tls_key, tls_ca_certs=tls_ca)


@mcp_app.command("affordances")
def mcp_affordances(
    path: Path = typer.Argument(
        Path("."),
        help="Repository root the tools operate on (default: the current directory)"),
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Serve affordance query tools over MCP (stdio).

    Defaults to the current directory so an editor can configure the server without
    hard-coding a repository path — MCP clients launch it with the workspace as cwd.
    """
    from .mcp.affordance_server import serve_stdio

    settings = _settings(config)
    serve_stdio(path, settings)


@mcp_app.command("gitlab")
def mcp_gitlab(
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Serve the GitLab harness tools over MCP (stdio)."""
    from .mcp.gitlab_server import serve_stdio

    settings = _settings(config)
    serve_stdio(settings)


if __name__ == "__main__":
    app()
