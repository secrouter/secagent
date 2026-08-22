"""Guard rails for HEADLESS pi runs (``secagent pi run -- -p ...``).

Two failure modes observed in live agent evals motivate this module, and both share
the same nasty property: the run LOOKS successful.

1. **The silent no-op.** A headless (``-p``) pi session that exhausts its context
   window prints nothing and exits 0, leaving no file changes — indistinguishable
   from "the task needed no changes". The evidence lives only in the session
   transcript (a JSONL under ``<agent_dir>/sessions/``), whose final assistant
   message carries ``stopReason: "length"`` and/or empty ``content``. So the guard
   snapshots the worktree before/after, reads that transcript, and turns the
   invisible failure into a loud stderr report (see ``session_verdict`` +
   ``snapshot_worktree``/``diff_worktree``, wired in ``cli.pi_run``).

2. **The context-knob mismatch.** pi budgets its output tokens from the
   ``contextWindow`` declared in ``models.json`` — but the SERVER's real ceiling is
   whatever vLLM was launched with (``max_model_len``, exposed per model on
   ``GET <base>/v1/models``). When models.json claims more than the server allows,
   pi's requests die at the server cap — which is exactly how failure mode 1 starts.
   ``preflight_context`` clamps the declared window down to the served one before
   launch, so the mis-budgeting never happens.

Every function here is deliberately non-fatal: a guard that can block a launch (or
crash after a successful run) is worse than the failures it reports, so network and
parse problems degrade to "no adjustment / no verdict" rather than raising. Only
interactive runs are exempt — a human at the keyboard sees pi's own output, and the
exec launch path (``leanctx.launch_pi``) stays byte-for-byte what it was.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


def is_headless(pi_args: list[str]) -> bool:
    """Whether these pass-through pi args request a headless (print-and-exit) run.

    pi's ``-p``/``--print`` flag is what makes a session non-interactive — the mode
    where the silent-no-op failure is invisible. A plain membership check is enough:
    the args come straight from ``secagent pi run -- ...``, and a literal ``-p``
    anywhere in them is pi's flag (pi has no positional argument that would collide).
    """
    return any(arg in ("-p", "--print") for arg in pi_args)


def _served_windows(base_url: str, timeout: float) -> dict[str, int]:
    """Map of served model id → ``max_model_len`` from one provider's ``/models``.

    ``max_model_len`` is a vLLM extension field; other OpenAI-compatible servers
    simply omit it and their models drop out of the map (no clamp — we only act when
    the server states a ceiling). Any network/HTTP/parse problem returns an empty
    map for the same reason: the preflight must never block a launch, and "could not
    ask" is not evidence of a mismatch.
    """
    try:
        resp = httpx.get(f"{base_url}/models", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 — best-effort probe; unreachable/odd server = skip
        return {}
    if not isinstance(payload, dict):
        return {}
    served: dict[str, int] = {}
    data = payload.get("data")
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        max_len = entry.get("max_model_len")
        if isinstance(model_id, str) and isinstance(max_len, int) and max_len > 0:
            served[model_id] = max_len
    return served


def preflight_context(agent_dir: Path, timeout: float = 3.0) -> list[str]:
    """Clamp ``models.json`` ``contextWindow`` values down to what the server serves.

    For each provider in ``<agent_dir>/models.json``, asks its ``baseUrl`` (which
    already ends in ``/v1``) for the served models and their ``max_model_len``; any
    declared ``contextWindow`` EXCEEDING the served ceiling is rewritten down to it,
    and the file is written back (``indent=2``, matching how ``secagent init`` writes
    it). Returns one human-readable note per adjustment, for the caller to surface.

    Only clamps DOWN: a declared window smaller than the served one is a valid
    operator choice (and harmless), whereas a larger one makes pi budget output
    tokens the server will refuse — the launch-killing direction. Never raises;
    a missing/unparseable models.json, an unreachable provider, or a failed
    write-back all degrade to "no adjustments" so the launch always proceeds.
    """
    models_path = agent_dir / "models.json"
    try:
        data = json.loads(models_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return []

    notes: list[str] = []
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        base_url = str(provider.get("baseUrl") or "").rstrip("/")
        models = provider.get("models")
        if not base_url or not isinstance(models, list):
            continue
        served = _served_windows(base_url, timeout)
        if not served:
            continue
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            declared = entry.get("contextWindow")
            ceiling = served.get(model_id) if isinstance(model_id, str) else None
            if isinstance(declared, int) and ceiling is not None and declared > ceiling:
                entry["contextWindow"] = ceiling
                notes.append(
                    f"{model_id}: contextWindow {declared} -> {ceiling} (served max_model_len)"
                )
    if notes:
        try:
            models_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            # Could not persist the clamp, so pi will still read the old values —
            # reporting adjustments that did not land would be lying to the operator.
            return []
    return notes


def _content_empty(content: Any) -> bool:
    """Whether an assistant message's ``content`` amounts to nothing.

    pi transcripts carry content either as a string or as a list of parts (text
    blocks, tool calls, ...). Whitespace-only text counts as empty — that is what a
    context-dead session emits — but any non-text part (e.g. a tool call) counts as
    real content: the model was still doing work, just not narrating it.
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") not in (None, "text"):
                    return False  # a tool call / non-text part is real output
                if str(part.get("text") or "").strip():
                    return False
            elif str(part).strip():
                return False
        return True
    return False


def session_verdict(agent_dir: Path, cwd: Path, since: float) -> str | None:
    """Diagnose THIS run's pi session transcript, or ``None`` when it looks healthy.

    Finds the newest ``*.jsonl`` under ``<agent_dir>/sessions/`` (any subdirectory —
    pi buckets sessions per cwd-slug, but re-deriving pi's slug encoding here would
    just drift; the ``since`` filter, set to the launch time by the caller, already
    scopes the search to the session this run created) and inspects its LAST line.
    ``cwd`` is accepted for the call-site contract should slug-matching ever become
    necessary; it is not consulted today.

    A final assistant message with ``stopReason == "length"`` or empty ``content``
    is the transcript signature of a context-exhausted headless run — the one that
    otherwise exits 0 having printed nothing. Returns a short diagnostic naming the
    file and the stop reason. Anything malformed or missing returns ``None``: this
    is a best-effort post-mortem, never a new failure mode of its own.
    """
    del cwd  # reserved — see the docstring
    sessions = agent_dir / "sessions"
    try:
        candidates = [p for p in sessions.rglob("*.jsonl") if p.stat().st_mtime > since]
        newest = max(candidates, key=lambda p: p.stat().st_mtime, default=None)
        if newest is None:
            return None
        lines = [ln for ln in newest.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        record = json.loads(lines[-1])
    except Exception:  # noqa: BLE001 — best-effort post-mortem; malformed = no verdict
        return None
    if not isinstance(record, dict):
        return None
    # Transcript lines wrap the assistant turn as {"message": {...}}; tolerate a flat
    # record too, since the guard should survive a transcript-format tweak.
    inner = record.get("message")
    message = inner if isinstance(inner, dict) else record
    if message.get("role") not in (None, "assistant"):
        return None
    stop_reason = message.get("stopReason")
    empty = _content_empty(message.get("content"))
    if stop_reason != "length" and not empty:
        return None

    usage = message.get("usage")
    input_tokens = None
    if isinstance(usage, dict):
        for key in ("input", "input_tokens", "prompt_tokens"):
            if isinstance(usage.get(key), int):
                input_tokens = usage[key]
                break
    parts = ["final message empty" if empty else "final message truncated",
             f"stopReason={stop_reason}"]
    if input_tokens is not None:
        parts.append(f"input={input_tokens} tokens")
    return (f"{newest.name}: " + ", ".join(parts)
            + " — the session likely died of context exhaustion")


def snapshot_worktree(cwd: Path) -> dict[str, str]:
    """Capture ``relative path → mtime+size`` for every file under ``cwd``.

    Cheap by design (one stat per file, no hashing): the guard only needs "did pi
    change ANYTHING", and mtime-ns + size catches every edit a coding agent makes.
    ``.git`` and ``.secagent`` are skipped — git's own bookkeeping and secagent's
    store both churn without representing user-visible work; gitignored build
    output is deliberately NOT excluded (a false "files changed" is harmless, a
    false "no files changed" is the failure this guard exists to prevent).
    """
    snapshot: dict[str, str] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (".git", ".secagent")]
        for name in files:
            path = Path(root) / name
            try:
                st = path.stat()
            except OSError:
                continue  # racing deletion / permission oddity — not worth failing over
            snapshot[str(path.relative_to(cwd))] = f"{st.st_mtime_ns}:{st.st_size}"
    return snapshot


def diff_worktree(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Human-readable delta between two :func:`snapshot_worktree` captures.

    One line per file (``added:``/``modified:``/``removed:``), sorted within each
    category so the report is stable across runs. Empty list = nothing changed —
    the signal the caller combines with :func:`session_verdict` to detect a
    silent no-op.
    """
    lines = [f"added: {path}" for path in sorted(after.keys() - before.keys())]
    lines += [f"modified: {path}"
              for path in sorted(before.keys() & after.keys()) if before[path] != after[path]]
    lines += [f"removed: {path}" for path in sorted(before.keys() - after.keys())]
    return lines
