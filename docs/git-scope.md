# Working on just your changes

By default, `secagent scan`, `secagent review local`, `secagent docs build`, and
`secagent testgen` look only at **the files your current branch actually changed** —
not the whole repository. This page covers what "scope" means, the flags that
control it, and — because a partial run is only useful if you know it is one — how
secagent tells you exactly what it did and did not look at.

```{admonition} What this is not
:class: important
Every use case scoped by this page stays **exactly as safe as it was before scoping
existed**. `scan` and `review local` stay read-only. `docs build` and `testgen` stay
exactly as read-only-against-your-source as they always were — `docs build` still
only writes a doc site to `-o`, `testgen` still only writes generated tests into its
own side tree (never your project). Narrowing the scope changes which files get
analyzed, reviewed, or spend fresh model budget on a doc/test; it never adds a
capability that edits your source, and it never changes WHERE any of these four
commands writes.
```

## The default: since your base branch

Run `secagent scan <repo>`, `secagent review local <repo>`, `secagent docs build
<repo>`, or `secagent testgen <repo>` with no flags, and the scope is **the delta
since your branch forked from its base** — `main` if present, `master` if not, or
your remote's default branch — computed as:

1. `git merge-base <base> HEAD` — where this branch actually diverged.
2. Diff that point against your **working tree**, not just `HEAD` — so both what you
   have already committed on this branch *and* any uncommitted edits are included.
3. Plus any **untracked, not-`.gitignore`d** new files.

That is deliberately the most inclusive reasonable default: "everything about this
piece of work, whether or not you have committed it yet."

```bash
secagent scan path/to/repo                 # scope: since main/master, auto-detected
secagent review local path/to/repo         # same default scope, different engine
secagent docs build path/to/repo -o ./site # same default scope: refresh only the delta
secagent testgen path/to/repo              # same default scope: generate for the delta
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
| `--all` (`scan`/`docs build`/`testgen`) | the whole repository | secagent's original, pre-scoping behavior |

```bash
secagent scan path/to/repo --staged                       # about to commit? review just that
secagent scan path/to/repo --working-tree                  # uncommitted edits only
secagent scan path/to/repo --base develop                  # a different base branch
secagent scan path/to/repo --path src/a.c --path src/b.c   # exactly these files
secagent scan path/to/repo --all                            # the whole repo, as before

secagent review local path/to/repo --staged
secagent review local path/to/repo --since v1.2.0

secagent docs build path/to/repo -o ./site --staged        # refresh only staged files
secagent docs build path/to/repo -o ./site --all            # refresh the whole site

secagent testgen path/to/repo --working-tree                # tests for uncommitted work
secagent testgen path/to/repo --all                          # the whole repo, as before
```

`secagent review local` has no `--all`: reviewing an entire repository with no diff
to react to is a different tool, not this one with a flag added. `scan`, `docs
build`, and `testgen` all expose it, each meaning "the whole-repo behavior this
command had before scoping existed" for that command specifically — see the
per-command sections below.

## Full index, scoped output

Scoping never means secagent knows *less* about your project — only that it *reports
on* (or spends fresh model budget on) less of it. Every command on this page still
builds (or reuses) the full affordance index — the whole-repository structure map,
symbol index, and IO map — exactly as it always has. What scoping narrows is:

- **which files are sent to the model** for `scan` (one LLM call per file is the
  expensive part; the file walk that builds context is not),
- **which files' diffs go into the reviewed prompt** for `review local` — while the
  affordance context assembled *around* that diff is still ranked and drawn from the
  whole repository, so a change to `services/api/db.py` still pulls in relevant
  context from files it calls into or is called by, even if those files were not
  themselves part of the delta,
- **which files get a fresh LLM purpose summary / function description** for `docs
  build` — the generated SITE still has a page for every file, and files outside the
  delta keep whatever summary is already in the affordance store (see
  {ref}`docs-incremental` below), and
- **which files/components get a freshly generated test** for `testgen` — the
  whole-repo affordance index is still read for grounding, and tests still land in
  the same side tree, at the same `--out` (see {ref}`testgen-partial` below).

This is the same split `secagent review mr` (GitLab merge-request review) has always
used — diff-scoped generation over full-repo-scoped grounding — extended to every
command on this page rather than invented anew for each one.

(docs-incremental)=
## Docs: the incremental-refresh model

`secagent docs build` never trims the site itself: `index_repo` still walks and
structurally indexes every file (symbols, language, the project/IO/call maps), and
the Sphinx pages it renders — overview, architecture, components, data-flow, API
reference — still cover every file the store knows about, scoped or not. What a
scope narrows is which files' **one-line purpose summary** and **per-function
descriptions** are worth a fresh model call this run:

- A file **inside** the delta gets summarized (or re-summarized) by the model, same
  as an unscoped build.
- A file **outside** the delta reuses whatever summary is already sitting in the
  affordance store — from a previous full or scoped build. If the store has never
  seen it before (a first-ever index run scoped from the start), it still gets
  indexed so the site stays complete, but with the heuristic (no-model) purpose,
  never a fresh model call.
- `--refresh-summaries` (re-evaluate the same model even on unchanged content) still
  applies **only within scope** — it cannot widen a scoped run's model spend to files
  the scope deliberately excluded. Pass `--all --refresh-summaries` to force a full
  re-evaluation of the whole repository.

The console banner and `summaries.json`'s `scope` block report **refreshed vs
reused**, not "in scope vs dropped" (there is no `dropped_non_analyzable`-style
language-restriction for docs — any changed file's purpose is worth refreshing):

```json
{
  "scope": {
    "kind": "since_base",
    "base_ref": "main",
    "base_sha": "392e5ca9a263b3cc57ed554e28253a5fda6ad9fc",
    "head_sha": "c5c82364178a26ad63c7f68a99ff404910aab95d",
    "files_changed": 1,
    "total_analyzable": 42,
    "in_scope_files": 1,
    "dropped_non_analyzable": 0,
    "partial": true,
    "refreshed": 1,
    "reused": 41
  }
}
```

`total_analyzable` here is every file the store tracks (the population a full,
`--all` build would summarize); `refreshed` is how many actually got a fresh model
call THIS run; `reused` is the rest — carried over untouched, not regenerated and not
downgraded to heuristic. `summaries.md` opens with the same SCOPED RUN callout `-
scan.md` uses, and `--all` omits the `scope` key entirely, matching the original
whole-repo shape byte-for-byte.

```{note}
The rendered HTML/RST site is never partial — only the *freshness* of the prose in
it is. A scoped build is the right default for iterating on a branch (fast, cheap);
reach for `--all` before publishing a site you want every page freshly reviewed by
the model.
```

(testgen-partial)=
## Testgen: a scoped run is partial

`secagent testgen` writes to the same side tree regardless of scope (`--out`,
default `<repo>/secagent-tests/`) — scoping never changes WHERE tests land, only
WHICH targets get a freshly generated test this run:

- The **unit** pass targets only files in the delta (`unit/` mirrors the source tree,
  same as always — a scoped run simply generates fewer of those files).
- The **functional** pass targets only components that own at least **one** file in
  the delta — not "every file in the component changed", but "this component was
  touched". A component with no file in the delta gets no functional test this run,
  even if it already has one from a previous run.
- The whole-repo affordance index (file summaries, the IO map) is still read for
  grounding on every generated test, in scope or not — a delta file's generated unit
  test is exactly as well-grounded as it would be in an unscoped run.

Because a scoped suite genuinely does not cover the whole project, `manifest.json`
carries the same structured `scope` block `summaries.json` does, with one pair of
counts per pass:

```json
{
  "scope": {
    "kind": "since_base",
    "base_ref": "main",
    "base_sha": "392e5ca9a263b3cc57ed554e28253a5fda6ad9fc",
    "head_sha": "c5c82364178a26ad63c7f68a99ff404910aab95d",
    "files_changed": 1,
    "dropped_non_analyzable": 0,
    "partial": true,
    "unit_files_in_scope": 1,
    "unit_files_total_analyzable": 12,
    "functional_components_in_scope": 1,
    "functional_components_total_analyzable": 4
  }
}
```

`README.md` in the generated side tree opens with a "Scoped run" callout naming the
same counts, right after the intro paragraph — the same "frame how to read the rest"
placement `scan.md`'s own admonition uses — so a reviewer opening the suite cold
sees immediately that it covers a delta, not the project. `--all` drops the `scope`
key from both `manifest.json` and the README, reproducing the original whole-repo
generation exactly.

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
computed the same way it always was. `secagent docs build --all` and `secagent
testgen --all` hold to the identical contract for their own outputs — no `scope` key
in `summaries.json`/`manifest.json`, every file/component (re)processed, the site or
test suite exactly as complete as it always was. If you are scripting against any of
these JSON artifacts and want the old, unscoped shape unconditionally, `--all` is the
way to keep it — on any of the three commands.

## See also

- {doc}`use-cases` — UC1 (docs), UC4 (scan), UC5 (testgen), and UC100 (review) in
  full, including `scan`'s cost model and `review`'s persona system.
- {doc}`cli` — the full flag reference for every command.
- {doc}`gitlab-watch` — running `review mr`/`review serve` continuously against a
  GitLab project (the non-local review path).
