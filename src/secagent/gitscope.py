"""Local git delta computation (read-only) for the "surgical" use cases.

Every use case built on this module stays report-only, exactly like the whole-repo
mode it scopes: nothing here ever writes to the working tree, the index, or the
repository's refs. It shells out to the system ``git`` binary via ``subprocess`` —
no GitPython, no pygit2, no new dependency — so it works wherever ``git`` itself
does.

The central idea is a **delta**: the set of files that changed on the current branch
relative to some reference point, plus their diffs. `ChangeSet`/`FileChange` are
shaped to match ``mcp/gitlab_harness.py``'s merge-request-changes payload
(``new_path``/``old_path``/``diff``) on purpose — see `ChangeSet.to_gitlab_style` —
so a review engine can consume a local delta and a GitLab merge request through the
exact same code path (``agents/review/agent.py``).

Six scope resolvers answer "which delta": `since_base` (the default — vs the branch's
fork point from ``main``/``master``/the remote default), `since_ref` (vs an explicit
ref), `working_tree` (uncommitted only), `staged` (the index only), `range` (two fixed
commits, no working tree), and `explicit` (a caller-supplied path list, no git at
all). `resolve_scope` turns a CLI's scope flags into one of these, and is meant to be
shared by every scoped command — `secagent scan`, `secagent review local`, and future
incremental commands — so the flag semantics never drift between them.

Scoping here only ever narrows *which files a use case spends model budget on*. It
never narrows the grounding a use case builds for itself (the affordance store still
indexes the whole repository) — see the module docstrings in ``agents/scan/agent.py``
and ``agents/review/agent.py`` for how each consumer keeps that split. Because a
scoped run therefore covers less than a full one, `analyzable` and `coverage_banner`
exist to make that gap countable and visible rather than silent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

# Git calls here are all metadata/diff reads against a local checkout — seconds at
# most, even on a large repo — so one generous, fixed timeout covers every call
# without needing to be a config knob.
_GIT_TIMEOUT_S = 30.0


class GitScopeError(RuntimeError):
    """A git delta could not be computed. The message is meant to be shown as-is:
    what went wrong, and (where there is one) what to pass instead."""


@dataclass(frozen=True)
class FileChange:
    """One changed file, shaped to line up with a GitLab merge-request change entry.

    ``path`` is the file's current (new) repo-relative path — for a deletion, the
    path it had before being removed, since there is no "new" path. ``old_path`` is
    set only for a rename (`status == "renamed"`); every other status leaves it
    ``None``. ``diff_text`` is the unified-diff *body* for this file (from the first
    ``@@`` hunk onward — no ``diff --git``/``index``/``---``/``+++`` header lines,
    matching the shape of GitLab's per-file ``diff`` field), or ``None`` when there is
    none to show: an untracked file (nothing to diff against) or a binary file (git
    reports "Binary files ... differ" instead of a hunk). ``binary`` disambiguates
    those two ``None`` cases for `analyzable`, which must drop the latter but keep
    the former.
    """

    path: str
    old_path: str | None
    status: str  # "added" | "modified" | "deleted" | "renamed"
    diff_text: str | None
    binary: bool = False


@dataclass
class ChangeSet:
    """The result of resolving one git scope: which files changed, and against what.

    ``scope`` names which resolver produced this (``"since_base"``, ``"since_ref"``,
    ``"working_tree"``, ``"staged"``, ``"range"``, or ``"explicit"``). ``base_ref`` is
    the human-readable ref the delta is measured from (a branch/tag name, ``"HEAD"``,
    or ``None`` for `explicit`, which has no git reference at all); ``base_sha``/
    ``head_sha`` are its resolved commit shas (also ``None`` where not applicable).

    ``total_analyzable`` starts unset: none of the resolvers below know the *whole
    repository's* analyzable-file count — that needs a language/ruleset a git
    operation has no opinion on. A caller that computes it (typically after calling
    `analyzable`, then walking the full repo the same way a whole-repo run would)
    fills it in here so `summary_dict`/`coverage_banner` can report it; left ``None``
    it just means "not computed for this run".
    """

    files: list[FileChange]
    scope: str
    base_ref: str | None
    base_sha: str | None
    head_sha: str | None
    total_analyzable: int | None = None

    def paths(self) -> list[str]:
        """Repo-relative paths that still exist — every change except a deletion.

        Not language- or ignore-filtered; see `analyzable` for that. This is the
        "what changed and can still be read" list — the same role
        ``GitLabClient.changed_paths`` plays for a merge request, minus the deleted
        entries a use case cannot point a reader (human or model) at any longer.
        """
        return [f.path for f in self.files if f.status != "deleted"]

    def unified_diff(self) -> str:
        """Every file's diff concatenated into one blob, each preceded by a
        ``diff --git`` header line — for a caller that wants one blob of readable
        diff text rather than iterating `files` itself. Files with nothing to show
        (untracked, binary, or a content-identical rename) are skipped.
        """
        parts: list[str] = []
        for f in self.files:
            if f.diff_text:
                old = f.old_path or f.path
                parts.append(f"diff --git a/{old} b/{f.path}\n{f.diff_text}")
        return "\n".join(parts)

    def to_gitlab_style(self) -> dict[str, Any]:
        """Render as the ``{"changes": [...]}`` shape
        ``GitLabClient.get_merge_request_changes`` returns, so a review engine can
        walk a local delta and a GitLab merge request through the identical code
        path (see ``agents/review/agent.py``)."""
        return {
            "changes": [
                {
                    "old_path": f.old_path or f.path,
                    "new_path": f.path,
                    "diff": f.diff_text or "",
                    "new_file": f.status == "added",
                    "deleted_file": f.status == "deleted",
                    "renamed_file": f.status == "renamed",
                }
                for f in self.files
            ]
        }

    def summary_dict(self) -> dict[str, Any]:
        """The scope's own identity as plain data — the common fields every scoped
        command's coverage/scope block starts from (see `agents/scan/agent.py` and
        `agents/review/agent.py`, which each add their own extra keys on top)."""
        return {
            "kind": self.scope,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "files_changed": len(self.files),
            "total_analyzable": self.total_analyzable,
        }


# ---------------------------------------------------------------------------
# git subprocess plumbing
# ---------------------------------------------------------------------------


def _git_bin() -> str:
    binary = shutil.which("git")
    if binary is None:
        raise GitScopeError("git is not installed or not on PATH")
    return binary


def _git(
    repo: Path, args: list[str], *, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``repo``. Never raises on a nonzero exit — callers decide
    whether that means "empty result" (e.g. a failed `rev-parse --verify -q` probe) or
    a real error; see `_git_ok` for the latter."""
    binary = _git_bin()
    # `-c core.quotePath=false` so a path with non-ASCII/special characters comes back
    # as raw UTF-8 rather than octal-escaped and quoted; `-c color.ui=false` so a
    # user's global color config can never leak ANSI escapes into diff text we parse.
    cmd = [binary, "-C", str(repo), "-c", "core.quotePath=false", "-c", "color.ui=false", *args]
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitScopeError(f"git {' '.join(args)} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise GitScopeError(f"failed to run git {' '.join(args)}: {exc}") from exc


def _git_ok(repo: Path, args: list[str], *, timeout: float = _GIT_TIMEOUT_S) -> str:
    """Like `_git`, but raises `GitScopeError` (naming git's own stderr) on failure."""
    proc = _git(repo, args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitScopeError(f"git {' '.join(args)} failed: {detail or f'exit {proc.returncode}'}")
    return proc.stdout


def _preflight(repo: str | Path) -> Path:
    """Resolve ``repo`` and confirm it is a usable git working tree. Every resolver
    below except `explicit` (which touches no git state at all) calls this first."""
    root = Path(repo).resolve()
    if not root.is_dir():
        raise GitScopeError(f"not a directory: {root}")
    proc = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        detail = (proc.stderr or "").strip()
        raise GitScopeError(f"not a git repository: {root}" + (f" ({detail})" if detail else ""))
    return root


def _require_commits(repo: Path) -> None:
    """Fail clearly on an unborn HEAD, rather than let each resolver hit its own
    confusing git error (git's own wording for this varies by command and version)."""
    proc = _git(repo, ["rev-parse", "--verify", "-q", "HEAD"])
    if proc.returncode != 0:
        raise GitScopeError(
            f"{repo} has no commits yet (unborn HEAD) — create an initial commit before "
            "using a git-scoped command"
        )


def _rev_parse(repo: Path, ref: str) -> str | None:
    """The commit sha ``ref`` resolves to, or ``None`` if it does not resolve at all
    (a nonexistent branch/tag, a typo, ...) — never raises, so callers can turn a
    missing ref into their own clear message rather than git's."""
    proc = _git(repo, ["rev-parse", "--verify", "-q", ref])
    return proc.stdout.strip() if proc.returncode == 0 else None


def _merge_base(repo: Path, ref: str) -> str:
    proc = _git(repo, ["merge-base", ref, "HEAD"])
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise GitScopeError(
            f"could not find a common ancestor between '{ref}' and HEAD"
            + (f" ({detail})" if detail else "")
            + " — the ref may be unrelated history, or not a commit at all"
        )
    return proc.stdout.strip()


_STATUS_LETTERS = {"A": "added", "M": "modified", "D": "deleted"}


def _parse_name_status(output: str) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff --name-status -M`` output into ``(status, path, old_path)``.

    Rename detection (``-M``) is the only similarity detection ever requested in this
    module — copies (``C``) are not, so that status is handled only defensively, as an
    addition of the new path, in case a future git default ever changes.
    """
    out: list[tuple[str, str, str | None]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        letter = parts[0][0]
        if letter == "R" and len(parts) >= 3:
            out.append(("renamed", parts[2], parts[1]))
        elif letter == "C" and len(parts) >= 3:
            out.append(("added", parts[2], None))
        else:
            out.append((_STATUS_LETTERS.get(letter, "modified"), parts[1], None))
    return out


_HUNK_START_RE = re.compile(r"^@@.*$", re.MULTILINE)
_BINARY_RE = re.compile(r"^Binary files .+ differ$", re.MULTILINE)


def _extract_hunks(diff_output: str) -> str:
    """From a raw ``git diff`` block, keep only the hunks (from the first ``@@``
    onward) — matching the shape of GitLab's per-file ``diff`` field, which likewise
    carries no ``diff --git``/``index``/``---``/``+++`` header. A rename or mode
    change with no content difference has no ``@@`` line at all; that is not an
    error, just nothing to show, so it returns ``""``.
    """
    m = _HUNK_START_RE.search(diff_output)
    return diff_output[m.start():] if m else ""


def _file_diff(
    repo: Path, diff_args: list[str], path: str, old_path: str | None
) -> tuple[str | None, bool]:
    """Return ``(diff_text, binary)`` for one path, given the ``git diff`` arguments
    (before ``--``) that select the comparison (a base sha, ``--cached``, a range).

    Both the old and new names are passed as the pathspec for a rename: git computes
    rename pairing over the *whole* changed set before pathspec filtering is applied,
    and limiting to only the new name risks the file being reported as if it were
    unrelated to (rather than paired with) its old content.
    """
    pathspec = [old_path, path] if (old_path and old_path != path) else [path]
    out = _git_ok(repo, ["diff", "-M", *diff_args, "--", *pathspec])
    if _BINARY_RE.search(out):
        return None, True
    return _extract_hunks(out), False


def _build_changes(repo: Path, diff_args: list[str]) -> list[FileChange]:
    """``git diff --name-status -M <diff_args>``, then one per-file diff call each —
    shared by every resolver that compares two real git states (everything except
    `explicit`, which has no git state, and the untracked-file listing, which has no
    "other side" to diff against).
    """
    out = _git_ok(repo, ["diff", "--name-status", "-M", *diff_args])
    changes: list[FileChange] = []
    for status, path, old_path in _parse_name_status(out):
        diff_text, binary = _file_diff(repo, diff_args, path, old_path)
        changes.append(
            FileChange(path=path, old_path=old_path, status=status,
                      diff_text=diff_text, binary=binary)
        )
    return changes


_BINARY_SNIFF_BYTES = 8192


def _looks_binary_file(path: Path) -> bool:
    """A NUL byte in the first few KB is the same heuristic git itself uses to decide
    whether to show a real diff for a file; applied here to UNTRACKED files, which
    never go through git's own diff machinery at all (there is nothing to diff them
    against), so nothing else classifies them as binary."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" in chunk


def _untracked_files(repo: Path) -> list[FileChange]:
    out = _git_ok(repo, ["ls-files", "--others", "--exclude-standard"])
    return [
        FileChange(path=p, old_path=None, status="added", diff_text=None,
                  binary=_looks_binary_file(repo / p))
        for p in out.splitlines() if p.strip()
    ]


def _auto_detect_base(repo: Path) -> str:
    """Prefer ``main``, then ``master``, then the remote's default branch
    (``origin/HEAD``, e.g. resolving to ``origin/main``). Raises with a message
    telling the caller to pass ``--base`` when none of those resolve."""
    for candidate in ("main", "master"):
        if _rev_parse(repo, candidate) is not None:
            return candidate
    proc = _git(repo, ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
    if proc.returncode == 0:
        short = proc.stdout.strip()
        if short and _rev_parse(repo, short) is not None:
            return short
    raise GitScopeError(
        "could not auto-detect a base branch in this repository (tried 'main', "
        "'master', and the remote's default branch via origin/HEAD) — pass "
        "--base <ref> to say what to diff against"
    )


def _since(repo: Path, base_name: str, *, scope_name: str) -> ChangeSet:
    mb = _merge_base(repo, base_name)
    head_sha = _rev_parse(repo, "HEAD")
    files = _build_changes(repo, [mb]) + _untracked_files(repo)
    return ChangeSet(files=files, scope=scope_name, base_ref=base_name,
                     base_sha=mb, head_sha=head_sha)


# ---------------------------------------------------------------------------
# public scope resolvers
# ---------------------------------------------------------------------------


def since_base(repo: str | Path, base: str | None = None) -> ChangeSet:
    """THE DEFAULT scope: the delta from where the current branch forked off
    ``base`` (``git merge-base <base> HEAD``) to the WORKING TREE — so it includes
    both what is already committed on this branch and any uncommitted edits — plus
    untracked, not-ignored new files.

    ``base=None`` (the default) auto-detects: ``main``, then ``master``, then the
    remote's default branch; see `_auto_detect_base`. An explicit ``base`` that does
    not resolve to a real ref raises rather than silently falling back.
    """
    root = _preflight(repo)
    _require_commits(root)
    if base:
        if _rev_parse(root, base) is None:
            raise GitScopeError(
                f"base ref '{base}' was not found in this repository — check the "
                "branch/tag name, or try a different --base"
            )
        base_name = base
    else:
        base_name = _auto_detect_base(root)
    return _since(root, base_name, scope_name="since_base")


def since_ref(repo: str | Path, ref: str) -> ChangeSet:
    """Like `since_base`, but against an explicit, caller-given ref — no auto-detect
    fallback chain, since the caller already said exactly what they mean."""
    root = _preflight(repo)
    _require_commits(root)
    if _rev_parse(root, ref) is None:
        raise GitScopeError(f"ref '{ref}' was not found in this repository")
    return _since(root, ref, scope_name="since_ref")


def working_tree(repo: str | Path) -> ChangeSet:
    """Uncommitted changes only (staged and unstaged) vs HEAD, plus untracked,
    not-ignored new files. Unlike `since_base`, this ignores anything already
    committed on the current branch."""
    root = _preflight(repo)
    _require_commits(root)
    head_sha = _rev_parse(root, "HEAD")
    files = _build_changes(root, ["HEAD"]) + _untracked_files(root)
    return ChangeSet(files=files, scope="working_tree", base_ref="HEAD",
                     base_sha=head_sha, head_sha=head_sha)


def staged(repo: str | Path) -> ChangeSet:
    """Only what is staged via ``git add`` — the index vs HEAD. Untracked files are
    never staged by definition, so none are included (contrast `working_tree`)."""
    root = _preflight(repo)
    _require_commits(root)
    head_sha = _rev_parse(root, "HEAD")
    files = _build_changes(root, ["--cached"])
    return ChangeSet(files=files, scope="staged", base_ref="HEAD",
                     base_sha=head_sha, head_sha=head_sha)


def range(repo: str | Path, a: str, b: str) -> ChangeSet:  # noqa: A001 - public API name
    """A fixed historical range, ``git diff a..b`` — no working tree, no untracked
    files, since both endpoints are real commits rather than live state."""
    root = _preflight(repo)
    _require_commits(root)
    a_sha = _rev_parse(root, a)
    if a_sha is None:
        raise GitScopeError(f"ref '{a}' was not found in this repository")
    b_sha = _rev_parse(root, b)
    if b_sha is None:
        raise GitScopeError(f"ref '{b}' was not found in this repository")
    files = _build_changes(root, [f"{a}..{b}"])
    return ChangeSet(files=files, scope="range", base_ref=a, base_sha=a_sha, head_sha=b_sha)


def explicit(repo: str | Path, paths: list[str]) -> ChangeSet:
    """Wrap a caller-supplied path list as a `ChangeSet` — no git involved — so
    ``--path`` shares the same type every other scope source produces. Each path is
    guarded against escaping ``repo`` (a traversal guard, same discipline as
    ``affordances/queries.py``'s ``read_slice``). Status is reported as
    ``"modified"`` for every entry (no git status lookup is performed) and
    ``diff_text`` is always ``None`` — there is no diff to show for a path list the
    caller picked directly, only content for a use case to analyze.
    """
    root = Path(repo).resolve()
    changes = []
    for p in paths:
        _guard_path(root, p)
        changes.append(FileChange(path=p, old_path=None, status="modified", diff_text=None))
    return ChangeSet(files=changes, scope="explicit", base_ref=None, base_sha=None, head_sha=None)


def _guard_path(repo: Path, rel: str) -> None:
    candidate = (repo / rel).resolve()
    try:
        candidate.relative_to(repo)
    except ValueError:
        raise GitScopeError(f"path outside the repository: {rel}") from None


# ---------------------------------------------------------------------------
# honesty / coverage helpers
# ---------------------------------------------------------------------------


def analyzable(changeset: ChangeSet, settings: Settings) -> tuple[list[str], int]:
    """Filter a `ChangeSet`'s existing paths down to ones a scoped run can actually
    analyze: not version-control metadata, not matched by the project's
    ``ignore_globs``, not binary. (Deleted paths are already excluded by
    `ChangeSet.paths`.)

    Returns ``(in_scope_paths, dropped_count)`` — ``dropped_count`` is every
    changed-but-excluded path, for a caller's honesty banner. This is a
    repo-hygiene-level filter only; it has no opinion on language or file type, so
    callers with a narrower notion of "analyzable" (e.g. `agents/scan/agent.py`'s
    rule-set languages) apply their own filter on top and fold what THAT drops into
    their own dropped count.
    """
    from .affordances.languages import is_ignored, is_vcs_metadata

    ignore_globs = settings.affordances.ignore_globs
    ignore_vcs = settings.affordances.ignore_vcs
    by_path = {f.path: f for f in changeset.files}
    in_scope: list[str] = []
    dropped = 0
    for rel in changeset.paths():
        fc = by_path.get(rel)
        if fc is not None and fc.binary:
            dropped += 1
            continue
        if ignore_vcs and is_vcs_metadata(rel):
            dropped += 1
            continue
        if is_ignored(rel, ignore_globs):
            dropped += 1
            continue
        in_scope.append(rel)
    return in_scope, dropped


def _short(sha: str | None) -> str:
    return sha[:8] if sha else "?"


def describe_scope(changeset: ChangeSet) -> str:
    """A short, human-readable description of what a `ChangeSet` compares — the
    common fragment both the scan coverage banner and the local-review scope line
    are built from."""
    kind = changeset.scope
    if kind in ("since_base", "since_ref"):
        return (f"{kind} vs {changeset.base_ref} (merge-base {_short(changeset.base_sha)}) "
                f".. HEAD {_short(changeset.head_sha)} + working tree")
    if kind == "working_tree":
        return f"working tree vs HEAD {_short(changeset.base_sha)}"
    if kind == "staged":
        return f"staged changes vs HEAD {_short(changeset.base_sha)}"
    if kind == "range":
        return (f"range {changeset.base_ref} {_short(changeset.base_sha)} .. "
                f"{_short(changeset.head_sha)}")
    if kind == "explicit":
        return f"an explicit list of {len(changeset.files)} file(s)"
    return kind


def coverage_banner(changeset: ChangeSet, *, in_scope: int, dropped: int) -> str:
    """The honesty banner text for a scoped run: scope kind, resolved base ref +
    short shas, in-scope vs total-analyzable files, and an explicit statement that
    the run is partial. ``changeset.total_analyzable`` must be set by the caller
    first (see `ChangeSet`'s docstring) — read here, not recomputed, so the banner
    and the caller's own JSON block can never disagree about it.
    """
    total = changeset.total_analyzable
    total_str = str(total) if total is not None else "an unknown number of"
    msg = (f"SCOPED run ({describe_scope(changeset)}): {in_scope} of {total_str} "
           "analyzable file(s) in scope")
    if dropped:
        msg += f"; {dropped} changed file(s) were not analyzable and were dropped"
    msg += (". This is a PARTIAL result over the repository — files outside the "
            "delta were not examined. Pass --all for a full-repo run.")
    return msg


def current_branch(repo: str | Path) -> str:
    """The current branch's short name, or ``"detached HEAD @ <short sha>"`` when
    HEAD is not on a branch. Used only for a human-readable label (e.g. a local
    review's synthesized title) — every resolver above works off HEAD directly and
    never needs this to compute a diff."""
    root = Path(repo).resolve()
    proc = _git(root, ["symbolic-ref", "-q", "--short", "HEAD"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    sha = _rev_parse(root, "HEAD")
    return f"detached HEAD @ {_short(sha)}" if sha else "HEAD"


# ---------------------------------------------------------------------------
# CLI scope-flag resolver — shared by every scoped command
# ---------------------------------------------------------------------------


def _parse_range(repo: str | Path, spec: str) -> ChangeSet:
    """Parse a ``--range`` value (``A..B``, two dots) and resolve it via :func:`range`.

    Git ref names cannot contain ``..``, so a plain partition is unambiguous; the
    three-dot ``A...B`` (symmetric-difference) form is rejected rather than silently
    mishandled, since :func:`range` only implements the two-dot ``git diff A..B``.
    """
    if "..." in spec:
        raise GitScopeError(
            f"--range takes a two-dot range 'A..B'; three-dot 'A...B' is not "
            f"supported (got {spec!r})"
        )
    a, sep, b = spec.partition("..")
    if sep != ".." or not a or not b or ".." in b:
        raise GitScopeError(f"--range must be 'A..B' — two refs separated by '..' (got {spec!r})")
    return range(repo, a, b)


def resolve_scope(
    repo: str | Path,
    *,
    base: str | None = None,
    since: str | None = None,
    staged_only: bool = False,
    working_tree_only: bool = False,
    paths: list[str] | None = None,
    all_files: bool = False,
    range_spec: str | None = None,
) -> ChangeSet | None:
    """Turn a scoped CLI command's flags into a `ChangeSet`, or ``None`` for
    ``--all`` (the whole-repo opt-out). Shared by every scoped command (`secagent
    scan`, `secagent review local`, and future incremental commands) so the flag
    semantics — which source wins, what the default is — never drift between them.

    Exactly one scope source may be active at a time; with none given at all, the
    default is `since_base` (auto-detected base branch). A command that does not
    expose one of these flags (e.g. `review local` has no ``--all``) simply never
    passes that keyword as active — this function does not require every caller to
    support every source.
    """
    active = [
        flag for flag, on in (
            ("--base", base is not None),
            ("--since", since is not None),
            ("--staged", staged_only),
            ("--working-tree", working_tree_only),
            ("--path", bool(paths)),
            ("--range", range_spec is not None),
            ("--all", all_files),
        ) if on
    ]
    if len(active) > 1:
        raise GitScopeError(
            "pass only one scope option, got " + " and ".join(active) + " — "
            "--base/--since/--staged/--working-tree/--path/--range/--all are mutually exclusive"
        )
    if all_files:
        return None
    if since is not None:
        return since_ref(repo, since)
    if staged_only:
        return staged(repo)
    if working_tree_only:
        return working_tree(repo)
    if paths:
        return explicit(repo, paths)
    if range_spec is not None:
        return _parse_range(repo, range_spec)
    return since_base(repo, base=base)
