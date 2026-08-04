# Working on just your changes

By default, `secagent scan` and `secagent review local` look only at **the files your
current branch actually changed** — not the whole repository. This page covers what
"scope" means, the flags that control it, and — because a partial run is only useful
if you know it is one — how secagent tells you exactly what it did and did not look
at.

```{admonition} What this is not
:class: important
Every use case scoped by this page stays **read-only**, exactly as it was before
scoping existed. Narrowing the scope changes which files get analyzed or reviewed; it
never adds a capability that edits your source. `secagent scan` still only writes its
own report; `secagent review local` still only prints to your terminal.
```

## The default: since your base branch

Run `secagent scan <repo>` or `secagent review local <repo>` with no flags, and the
scope is **the delta since your branch forked from its base** — `main` if present,
`master` if not, or your remote's default branch — computed as:

1. `git merge-base <base> HEAD` — where this branch actually diverged.
2. Diff that point against your **working tree**, not just `HEAD` — so both what you
   have already committed on this branch *and* any uncommitted edits are included.
3. Plus any **untracked, not-`.gitignore`d** new files.

That is deliberately the most inclusive reasonable default: "everything about this
piece of work, whether or not you have committed it yet."

```bash
secagent scan path/to/repo                 # scope: since main/master, auto-detected
secagent review local path/to/repo         # same default scope, different engine
```

If your repository has neither `main` nor `master` nor a remote default branch,
secagent says so and asks for `--base` explicitly — it does not guess.

## Choosing a different scope

Six sources, and **exactly one may be active at a time** (secagent rejects combining
them, e.g. `--staged --working-tree`, rather than silently picking one):

| Flag | Scope | Notes |
|---|---|---|
| *(none)* | since the base branch (default) | see above |
| `--base <ref>` | since `<ref>`, same "merge-base then working tree" logic | overrides auto-detection |
| `--since <ref>` | since `<ref>` | like `--base`, but no auto-detect fallback — `<ref>` must exist |
| `--staged` | only what is `git add`ed | the index vs `HEAD`; untracked files are never staged, so none are included |
| `--working-tree` | only uncommitted changes vs `HEAD` | skips anything already committed on this branch, unlike the default |
| `--path <p>` | exactly these files (repeatable) | no git involved — a plain list, still scoped the same way |
| `--all` (`scan` only) | the whole repository | secagent's original, pre-scoping behavior |

```bash
secagent scan path/to/repo --staged                       # about to commit? review just that
secagent scan path/to/repo --working-tree                  # uncommitted edits only
secagent scan path/to/repo --base develop                  # a different base branch
secagent scan path/to/repo --path src/a.c --path src/b.c   # exactly these files
secagent scan path/to/repo --all                            # the whole repo, as before

secagent review local path/to/repo --staged
secagent review local path/to/repo --since v1.2.0
```

`secagent review local` has no `--all`: reviewing an entire repository with no diff
to react to is a different tool, not this one with a flag added.

## Full index, scoped output

Scoping never means secagent knows *less* about your project — only that it *reports
on* less of it. Both `scan` and `review local` still build (or reuse) the full
affordance index — the whole-repository structure map, symbol index, and IO map —
exactly as they always have. What scoping narrows is:

- **which files are sent to the model** for `scan` (one LLM call per file is the
  expensive part; the file walk that builds context is not), and
- **which files' diffs go into the reviewed prompt** for `review local` — while the
  affordance context assembled *around* that diff is still ranked and drawn from the
  whole repository, so a change to `services/api/db.py` still pulls in relevant
  context from files it calls into or is called by, even if those files were not
  themselves part of the delta.

This is the same split `secagent review mr` (GitLab merge-request review) has always
used — diff-scoped generation over full-repo-scoped grounding — extended to
`scan` and to the new local-review path rather than invented twice.

## Honesty: a scoped run is a partial run

A scoped run answers a narrower question than a full one, and it must never be
mistaken for the broader claim. Every scoped `scan` prints an explicit banner to
**stderr** before it does anything else:

```text
scan: SCOPED run (since_base vs main (merge-base 392e5ca9) .. HEAD c5c82364 + working
tree): 1 of 3 analyzable file(s) in scope. This is a PARTIAL result over the
repository — files outside the delta were not examined. Pass --all for a full-repo
run.
```

...and the same information lands as structured data in `scan.json` (and, trimmed, in
the JSON `secagent scan` prints to stdout):

```json
{
  "scope": {
    "kind": "since_base",
    "base_ref": "main",
    "base_sha": "392e5ca9a263b3cc57ed554e28253a5fda6ad9fc",
    "head_sha": "c5c82364178a26ad63c7f68a99ff404910aab95d",
    "files_changed": 1,
    "total_analyzable": 3,
    "in_scope_files": 1,
    "dropped_non_analyzable": 0,
    "partial": true
  }
}
```

`total_analyzable` is the size of the population a full (`--all`) run would have
scanned — computed by walking the whole repository, every time, specifically so "1 of
3" is an honest fraction and not a guess. `dropped_non_analyzable` counts changed
files that were part of the delta but could not be scanned anyway (wrong language,
matched an ignore glob, binary) — a real gap in coverage, named rather than hidden.

`scan.json`'s Markdown twin, `scan.md`, carries the identical caveat as its first
admonition, and `analysis_complete` in the JSON result is **unconditionally `false`**
for a scoped run — never derived from "did every file I looked at succeed", because
that question does not answer "is the repository clean".

```{warning}
This matters most for `scan`, a memory-safety tool: **a scoped scan with zero
findings is not a clean bill of health for the repository** — only for the files
that were in scope. Read the banner before trusting a quiet result, and reach for
`--all` when you actually need whole-repo coverage (expect it to take much longer —
see {doc}`use-cases`'s UC4 section on cost).
```

`secagent review local` prints a shorter, non-alarmed version of the same idea to
stderr — which scope was resolved and how many files changed — because a code review
was never a claim about the whole repository's health to begin with; unlike `scan`,
there is no prior "complete" state for a diff-shaped tool to fall short of.

```text
Reviewing since_base vs main (merge-base 392e5ca9) .. HEAD c5c82364 + working tree —
1 file(s) changed.
```

That line goes to stderr and the review text itself goes to stdout, so
`secagent review local <repo> > review.txt` captures exactly the review, nothing else.

## `--all` is exactly today's behavior

`secagent scan --all` reproduces the original, pre-scoping whole-repo scan
byte-for-byte: no `scope` key anywhere in its output, no banner, `analysis_complete`
computed the same way it always was. If you are scripting against `scan.json` and
want the old, unscoped shape unconditionally, `--all` is the way to keep it.

## See also

- {doc}`use-cases` — UC4 (scan) and UC100 (review) in full, including `scan`'s cost
  model and `review`'s persona system.
- {doc}`cli` — the full flag reference for every command.
- {doc}`gitlab-watch` — running `review mr`/`review serve` continuously against a
  GitLab project (the non-local review path).
