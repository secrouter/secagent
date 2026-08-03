# Use cases

## UC0 — Full project analysis

Drop pi into an unfamiliar project and have it produce a complete, structured analysis:
**bin the components by language, run the right secagent tools on each, then synthesize a
summary.** UC0 orchestrates the other use cases rather than adding a new engine — it is a
set of pi action templates (a Skill + slash commands) over the affordances.

```bash
secagent affordance plan <repo>     # deterministic: components binned by language + tools to run
```

Driven by pi (the recommended path — drop in and go):

```
/secagent-analyze-all ./out         # index, bin, build docs + summaries, plan, then summary + diagrams
                                  # add --no-summary to stop after the per-language tools
```

Flow:

1. **Orient + bin** — `secagent affordance plan` returns the language→component bins and,
   for each language, the secagent commands to run (deterministic; the agent doesn't infer
   the toolchain).
2. **Per-language tools** — C/C++ → call map + `scan` (memory/stability) + `analyze`
   (IKOS); C# → `analyze deep` (Roslyn: qualified calls + type hierarchy); others →
   summaries + call map. Missing heavy backends degrade to the light path.
3. **Summary + diagrams** (default) — the docs site + `summaries.md`, plus an
   `ANALYSIS.md` the model writes from the tool outputs (components by language,
   architecture, per-language findings, risks) **embedding secagent's Draw.io diagrams** —
   the architecture (`components`) and data-flow (`system_io`) graphs the docs build
   produces deterministically from the IO map (the model doesn't hand-author diagrams).
   Optional: `--no-summary` stops after the per-language tools. See the `secagent-analysis`
   skill (`pi/skills/secagent-analysis/SKILL.md`).

## UC1 — Documentation deep-dive

Generates a comprehensive Sphinx site with Draw.io architecture diagrams. Diagrams are
derived **deterministically from the IO map** (accurate by construction); the local
model only writes prose, which keeps it robust on small Gemma variants.

```bash
secagent index path/to/repo                 # build the affordance store (incremental)
secagent docs build path/to/repo -o ./site  # outline -> diagrams -> Sphinx HTML
secagent docs build path/to/repo -o ./site --no-llm   # heuristic prose, no endpoint
```

Pipeline:

1. Index the repo into the affordance store.
2. Propose a documentation IA (overview, architecture, components, data-flow, API)
   from the structure and IO maps.
3. Generate `.drawio` diagrams from the IO map (component graph + system IO).
4. Write Sphinx pages (furo theme); render diagrams headlessly via `drawio` + Xvfb.
5. Build HTML.

Run via pi for an interactive, exploratory deep-dive (the model calls the affordance
tools to stay context-frugal), or as the one-shot command above.

## UC3 — C/C++ static analysis (IKOS)

### Measured signal quality — read this first

**High recall, low precision, poor localisation.** All three matter, and quoting any one
of them alone gives the wrong impression.

**Recall — 5 of 5.** On a file with five deliberately seeded defects, IKOS flagged every
one at the correct line. Workings in `quality/UC3_SIGNAL_QUALITY.md`.

**Precision — 1.1% on mature code, and that number is not a usefulness verdict.** It was
measured on cFS and PX4-GPSDrivers: production code, heavily reviewed, where the base rate
of real defects is near zero. On such a corpus almost every finding is false *by
construction*, so the figure cannot tell "the tool is noisy" apart from "there was nothing
to find". Read it as a measure of noise on clean code, which is what it is.

| | C (NASA cFS File Manager) | C++ (PX4 GNSS drivers) |
|---|---|---|
| findings classified | ~134 defect-shaped, of 194 | **266**, all of them |
| confirmed true positives | **0** | **3 (1.1%)** |
| false positives | the rest | **241 (90.6%)** |
| unclear | 1 | 22 (8.3%) |

**Localisation — 1 of 5.** This is the weakest link and the least visible. On the seeded
file it took **25 findings to cover 5 defects**, and four of the five messages describe a
symptom rather than the cause: the `i <= Count` off-by-one surfaces as *"variable 'Count'
might be uninitialized"*, never as a loop bound. `buffer-overflow` — the checker whose name
matches three of the five defects — fired exactly once, on the one that is *not* a buffer
defect.

So: it will find your defect, and it will make you read five findings and a misleading
message to get there.

**Nearly all of it is one mechanism, and it is not a C++ problem.** In `--library` mode
IKOS analyses each function as a standalone entry point with no caller, so every
parameter, every global and every `this` is unconstrained — "might be uninitialized" is
then definitionally true and definitionally useless. This was previously written up here
as C++ `this`-pointer noise. That was wrong: `crc.cpp`, which is C-style with no `this`
anywhere, has **9 of 9** findings of the same species. C++ only makes it worse in
*volume*, because a class with N methods gives IKOS N unconstrained entry points instead
of one `main`.

**Triage does not rescue this, and its accuracy number flatters it.** On the C++ corpus
it labelled **266 of 266** findings false positive — including all three that are
genuinely correct — which scores 90.6% "accurate" purely because most findings are in
fact false. The reasoning is confabulated rather than analytical: one note explains a
null-pointer finding by a `uint32_t` cast "preventing a potential integer overflow", and
the correct reason (an unconstrained parameter in library mode) appears in none of the
266. On the C corpus roughly 4 in 10 justifications contain a specific, checkable false
claim about the code, including "`BufferSize` is never actually used" about a variable
used on that exact line. **Use the verdicts to find where to look, never to decide what
is true.**

**Grounding does not catch that class of error, by construction.** `affordances.grounding`
resolves the paths and backticked symbols a note names against the index — it is a
hallucinated-*name* detector, not a hallucinated-*behaviour* detector. It fired
identically on accurate and fabricated notes, and 83% of C++ triage notes self-flag
`UNVERIFIED` mostly because local variable names are not in a project-wide symbol index.
A note marked grounded means *its references resolve*, not *its claims are right*.

### Preconditions

Driver and app code usually does not build alone, and IKOS needs it to.

- **A `compile_commands.json` is effectively required** for anything cFS- or PX4-shaped.
  Without the `-I`/`-D` flags the TU fails to preprocess and you get a clean, honest
  "not analysed" rather than findings.
- **Headers generated by the parent project must be present.** PX4-GPSDrivers is designed
  to sit inside a PX4-Autopilot checkout: `gps_helper.h` includes `"../../definitions.h"`,
  which supplies uORB-generated structs (`sensor_gps_s`, `satellite_info_s`) and the
  `GPS_WARN`/`GPS_ERR` macros. It is a real dependency on the parent build tree, not a
  stub you can write. **8 of 11** driver files in that repo are un-analysable without it.
  This is the normal case for embedded driver code, not an edge case — budget for
  supplying the parent tree before running UC3 on a submodule.

Runs [IKOS](https://github.com/NASA-SW-VnV/ikos) (NASA's abstract-interpretation
static analyzer) over C/C++ to detect runtime errors — buffer overflows, null
dereferences, integer overflows, division by zero, uninitialized reads — then
enriches each finding with secagent affordances (the component and file purpose it
touches), optionally triages it with the local model, and writes a Markdown + JSON
report.

```bash
# Run IKOS on a translation unit / .bc, then report:
secagent analyze run path/to/repo path/to/file.c -o ./analysis
# Or ingest a report produced elsewhere (no IKOS binary needed):
secagent analyze ingest path/to/repo ikos-report.json -o ./analysis
secagent analyze ingest path/to/repo ikos-report.json --no-llm   # skip triage
```

Each finding gets its severity (IKOS `error` → high, `warning` → medium), the owning
component, the file's one-line purpose, and an optional one-sentence LLM triage
(likely true/false positive). The code slice fed to the model is wrapped as untrusted
(CMMC-7) and the report carries the CUI marking when `marking.banner` is set.

```{note}
IKOS is a heavyweight, LLVM-based native toolchain and is **not** bundled in the main
secagent image. Three ways to run it:

- **Optional analysis image** (recommended for `analyze run`): build the dedicated,
  opt-in image which compiles IKOS + installs secagent —
  `make docker-analysis` (or `docker compose --profile analysis build analysis`).
  See `docker/analysis.Dockerfile`.
- **Local IKOS install**: install IKOS yourself and run `secagent analyze run` directly.
- **Ingest anywhere**: produce an IKOS JSON report on your build host and run
  `secagent analyze ingest` — no IKOS binary needed.

For whole projects, build with `ikos-scan make` and analyze the resulting `.bc`.
Tuning: `analysis.status_filter`, `analysis.analyses_filter`, `analysis.max_triage`
(also `--max-triage`; it defaults to 10, so a 139-finding run triages 10 unless you
raise it).
```

## UC4 — LLM memory/stability scan (configurable rules)

Asks the local model to review C/C++ against a **configurable, heuristic rule set**
focused on memory safety and runtime stability for embedded/real-time systems. Where
UC3 (IKOS) is a formal analyzer, UC4 catches issues that are easy to state as a rule
but hard to encode in a checker — and the rules are edited like the review persona.

### Measured capability, by language — read this first

The `embedded-cpp` profile accepts C and C++. On measurement it is useful for one of
them. Both figures below are **one model** (`gemma-4-26b-a4b-qat`) against **one corpus**
each, and neither has been replicated.

```{warning}
The precision figures below were measured on mature, production, heavily reviewed code,
where the base rate of real defects is near zero — the same confound that made UC3's
precision misread as a usefulness verdict. They are facts about **noise on clean code**.
They are not statements about what the scanner does on code that contains defects, and
should not be quoted as if they were. On the seeded-defect file UC4 scores 5/5.
```

| | C (NASA cFS) | C++ (PX4 GNSS parsers) |
|---|---|---|
| single run, strict / lenient | 25% / 50% | **24% / 43%** |
| at ≥3 of 5 runs | 86% / 100% | no signal — see below |
| true positives found | 9 of 37 findings | 9 of 37 findings |

*Strict* counts only reachable defects; *lenient* also counts interface-hardening
suggestions. Full workings in `quality/PRECISION_C_TEMP10.md` and
`quality/PRECISION_CPP.md`.

**C++ is not recommended.** Three measured reasons, in order of severity:

*(C++ was 0% / 28% before the `MEM-002` and `ISR-002` rule fixes; re-measured after them
in `quality/PRECISION_CPP_POSTFIX.md`. The 9 true positives are two underlying defects,
one of them reported eight times.)*

1. **A real defect was reported inverted — since fixed, and the fix is measured.**
   `rtcm.cpp:84` contains `if (!new_buffer)` after `new uint8_t[...]`. Plain
   `operator new[]` *throws*; it never returns null, so that check is dead code and is
   itself the defect. The scanner did not report it — it reported the **opposite**,
   recommending a NULL check be *added* after `new`. On flight software, following that
   advice makes the code worse. `MEM-002` now distinguishes the allocators, and over 4
   runs it flagged the dead check correctly 4 times and produced the inversion **zero**
   times, and a full re-scan now reports it correctly at `rtcm.cpp:84`. Note the same
   wrong claim then reappeared under `CTL-004`, whose keep-list still says "allocation" —
   the inversion migrated rather than died, and that is not yet fixed.
2. **It takes the semantically-hard bait**: findings on lines that are safe only by an
   invariant a reader must trace across functions. Precision is now 24%, but the
   9 true positives are two underlying defects, and a rule producing eight wrong
   "uninitialized read" claims replaced the concurrency noise that was removed.
3. **Run frequency does not help.** On C, every finding seen in ≥3 of 5 runs was real. On
   C++ a confirmed stack-buffer overflow and a confirmed non-defect both appeared in
   **1 of 3** runs — indistinguishable. A frequency threshold would discard the most
   serious defect in the corpus.

The scanner still runs on C++ and still reports findings, because refusing the language
would select zero files and print "no findings" over an unexamined codebase — a silent
all-clear, which is worse than a bad answer. It warns instead, in the log and in
`scan.md`. **Treat C++ findings as leads to verify, never as advice to act on.**

Even on C, the ≥3/5 figure rests on 6 true positives in one corpus and costs a third of
them in recall. It is not a default and should not be quoted without its run count.

### Running it

```bash
secagent scan path/to/repo -o ./scan
secagent scan path/to/repo --rules config/rules/memory-critical.yaml   # alternate profile
secagent scan path/to/repo               # the whole project (hours on a local model)
secagent scan path/to/repo --max-files 20   # a bounded pass
secagent scan path/to/repo --path src/a.c --path src/b.c   # exactly these files
```

Each file is reviewed against the rules; findings carry the **rule id**, severity,
`file:line`, a one-line explanation, and the owning component. Output is `scan.md` +
`scan.json`, with CUI marking (`marking.banner`) and an audit record. The code sent to
the model is wrapped as untrusted (CMMC-7).

### The rule set

The default rules (`config/rules/embedded-cpp.yaml`) are distilled from the
[NASA/JPL Power of Ten](https://spinroot.com/gerard/pdf/P10.pdf), MISRA C:2012, the
SEI CERT C standard, and BARR-C:2018 — covering dynamic-allocation discipline, buffer
bounds, pointer/NULL safety, integer overflow, fixed loop bounds, no recursion,
return-value checks, ISR/`volatile` concurrency, and resource release.

Edit that file (or point `scan.rules_profile` at another, e.g.
`config/rules/memory-critical.yaml`) to add, remove, or re-scope rules — no code
changes, reloaded on every run. Each rule has an `id`, `category`, `severity`, and
`guidance`; the `settings` block sets `min_severity`, `max_findings_per_file`, and
`focus`.

```{note}
UC4 is a heuristic LLM reviewer, not a certified checker — pair it with UC3 (IKOS) and
your normal MISRA/CERT tooling. Findings are advisory and should be confirmed.
```

### Cost and shape of a scan

One model call per file per rule group, so a scan is the slowest use case. Three settings
decide what it costs:

| setting | default | effect |
|---|---|---|
| `scan.workers` | 4 | concurrent calls; measured 1.9x over serial |
| `scan.per_file_timeout_s` | 300 | bounds a file the model cannot converge on |
| `scan.rule_granularity` | `category` | `all` / `category` / `rule` — how finely rules are sent |
| `scan.temperature` | 0.7 | sampling temperature for rule-checking calls only |
| `scan.runs` | 1 | scan each file N times and aggregate; findings carry `run_fraction` |

Two operational facts worth knowing before you tune any of these:

- **`scan.max_file_bytes` (40000) is an ordinary budget knob.** An earlier version of
  this note claimed it was "load-bearing" because raising it to 50000 returned **400 Bad
  Request**, and inferred a context-window limit. That was wrong twice: the failure does
  not reproduce, and the arithmetic never supported it — a 40KB file is ~12k prompt tokens
  against a 128k window. The 400s are real but track **concurrency**: the same payload
  that failed 13 of 21 calls at `workers=7` answers 3 of 3 sequentially. Seven concurrent
  ~12k-token requests is ~84k tokens at once — a server-side aggregate limit, not a
  per-request one. Lower `scan.workers` on large files before touching this.
- **`scan.include_header` is off by default, and that is a measured decision.** Sending a
  C++ file with its header does fix the specific defect it was built for — the model
  otherwise never sees a member's declaration, which is why it called `_rtcm_parsing`
  uninitialized eight times out of eight. But on `ashtech.cpp` it also cut total findings
  48 -> 22, zeroed INT-003 (13 -> 0) and CTL-004 (8 -> 0), and produced six MEM-006 hits
  that are all the same false claim. Adding context to a small model is not free and not
  monotonic: the shape of the prompt competes with its content. See
  `quality/SCAN_HEADER_CONTEXT.md`. The flag is kept so the result stays reproducible.
- **C++ costs materially more per byte than C.** Three C files at `runs=1` took 18.9
  minutes; one 40KB C++ file at `runs=3` took 57.9. The "5 runs for 3.1x" figure measured
  on a single file at `rule_granularity = "all"` does **not** generalise — per-category
  calls hit far more empty-content retries, each re-issued at a larger token budget.
  Measure your own configuration rather than scaling from either number.

**A correction.** This section used to say that sending a 32-rule profile in one call
returned **empty**, and that splitting was therefore "not an optimisation, it is the
difference between an answer and no answer". That was measured with `llm.temperature` at
its 0.2 default and it was wrong. At 0.2 the model fell into degenerate repetition — one
run repeated a single finding line 334 times before hitting the token cap and returning
nothing — and every scan failure previously blamed on file size, concurrency or model
capacity was that. At temperature 1.0 the single call answers fine, and per-category
groups go from 0 of 5 converging to 5 of 5. `scan.temperature` is separate from the global
`llm.temperature` because summarisation may legitimately want determinism; "is this code
safe?" does not.

The obvious worry about raising it is that a high temperature might buy convergence at the
cost of *parseable* output — scan needs a JSON array, not merely non-empty content. That
was measured rather than reasoned about, across all seven category groups, classifying
each response with scan's own parser:

| `scan.temperature` | parsed OK | empty | no JSON array | unparseable |
|---|---|---|---|---|
| 0.2 | 1 | 6 | 0 | 0 |
| 0.7 | 7 | 0 | 0 | 0 |
| 1.0 | 7 | 0 | 0 | 0 |

There is no trade: zero malformed responses at any temperature. 0.7 is the default rather
than 1.0 because the two are indistinguishable here — same convergence, same findings,
wall time within noise — and a lower temperature samples less randomly, so two scans of
unchanged code agree with each other more often.

What granularity actually buys is **recall, against wall time** (same 3.4KB C++ file,
temperature 1.0):

| `rule_granularity` | calls | time | findings |
|---|---|---|---|
| `all` | 1 | 67s | 3 |
| `category` | 7 | 8.5 min | 8 |
| `rule` | 32 | 23.2 min | 16 |

**That table is unverified and may be entirely noise.** Each row is a single run, and
single runs of this scanner do not reproduce: twelve repeats on one file produced a mean
pairwise Jaccard of 0.00, with no finding ever reported twice
(`quality/SCAN_REPRODUCIBILITY.md`). The finding counts above could differ that much
between two runs of the *same* setting. Treat the ordering as a hypothesis until it is
re-measured with `scan.runs > 1`.

`category` is the default because it is what shipped, so nobody's findings change
underneath them — not because it is a known optimum. `rule` is deliberately **not** the
default despite finding the most: whether those extra findings are true positives is
unmeasured, and measured C precision on cFS is 25-50% depending on how you count
(`quality/PRECISION_C_TEMP10.md`). Note also that a raw finding count flatters finer
granularity: in that sample six of the nine true positives were two underlying defects,
re-reported by each rule category able to see them.

Splitting does still make failure partial rather than total: when some groups exhaust
their budget, the report names which ones went unexamined instead of dropping the file.
That part was always true and does not depend on temperature.

## UC5 — Automatic test generation

### What actually gets produced, and what is known about it

Two different questions, and conflating them is how this section was wrong twice.

**Yield — fixed, and it was a setting.** 19–21 of 25 attempts used to produce nothing. The
cause was `llm.temperature = 0.2` driving the model into degenerate repetition: empty
content, `finish_reason="length"`, and 21–47k characters of reasoning looping on itself
until the budget ran out. The identical problem had already been diagnosed and fixed for
`scan`, which is why `scan.temperature` exists — testgen was simply never checked.

| | attempts | written | timed out | empty content |
|---|---|---|---|---|
| `temperature 0.2` | 25 | 4 | — | dominant |
| **`temperature 1.0`** | 24 | **18** | 6 | **0** |

Empty-content failures went to **zero**. Every remaining failure is a timeout against
`testgen.per_file_timeout_s`, which is a knob — a far more tractable problem than
degenerate repetition. (24 rather than 25 attempts because the project's own test files are
no longer used as generation targets.)

**Quality — still not established, and the honest answer is that we cannot see it yet.**
Running `secagent verify-tests` over the 14 unique files produced:

| verdict | n | meaning |
|---|---|---|
| `unverifiable` | 8 | no implementation could be linked — see below |
| `environment` | 5 | needs PX4's `definitions.h`, supplied by the parent project |
| `uncompilable` | 1 | a real, mechanical compile error |
| **`useful`** | **0** | |
| **`vacuous`** | **0** | |

The `unverifiable` files are a limit of the harness, not a verdict on the tests.
Generated C++ tends to hand-declare its subject (`extern uint32_t calculateCRC32(...)`)
rather than include the header, and the dependency closure follows quoted includes — so
nothing gets linked and nothing is proven.

Falling back to the affordance index — which already records the file defining every
function — recovered **2 of those 8**, and the distribution became
`environment 6 · unverifiable 6 · wrong 1 · uncompilable 1`. One of the two independently
reproduced a hand-relink done earlier: it compiles and fails its assertion, so `wrong`,
not `uncompilable`. The remaining 6 are honest refusals — their subjects are declared in
header-only components with no implementation file to link at all.

So: **the only end-to-end quality measurement that exists remains an independent hand
audit** — 1 of 6 files compiled; within it, 4 real cases, 3 vacuous, 3 wrong. Nothing here
supersedes that, and nothing here claims tests got better.

### Measured quality — read this first

**Delivery is fixed; quality is unchanged.** The plumbing defects are gone — truncation is
disclosed, empty generations are no longer counted as successes, the framework is named
when it was assumed rather than detected. What the model writes has not improved, and this
is what it looks like when compiled and run.

On the one C++ file from a re-test that both generated and **compiled**, out of 10 test
cases:

| | count | what it means |
|---|---|---|
| **actively wrong** | **3/10** | fail against correct, unmodified code — a developer sees false CI failures |
| **vacuous** | **3/10** | every assertion sits behind `if (result == GotXxx)` where the model's guessed protocol string never matches, so nothing inside ever runs — **green whatever the implementation does** |
| genuinely useful | 4/10 | |

The vacuous third is the dangerous one: it passes, it looks like coverage, and it tests
nothing. A green suite is not evidence, and neither is a test count.

**A whole functional test can be fabricated.** On the same repo, `functional/test_root.cpp`
imports `GpsParser.h` and asserts on `ParseResult` and `ErrorCode::EMPTY_INPUT` — none of
which exist anywhere in the repository. The functional path is **never given source**, only
the component's IO map and its member files' one-line purposes; for that component the
purposes were a licence excerpt, "GPS Drivers", and two `"Other file (N bytes)."` strings.
The model invented an API rather than declining. Its own first comment says so: *"Since the
implementation is not provided, we define the expected interface"*.

**The framework is a guess unless the repo says otherwise, and it was wrong here.** The
generated tests are GoogleTest; `gps/CMakeLists.txt` shows the project's actual convention
is a plain `assert()`/`main()` executable with no GoogleTest anywhere. This is exactly what
the `framework_assumed` disclosure exists to tell you — read it, and expect to port.

**Check the output before trusting it.** `secagent affordance verify <repo> <generated file>`
resolves every project-relative include and symbol against the index; it exits non-zero
when something does not. On C++ it is a weak check — raw `.cpp` has no backticked tokens,
so most references land in `unchecked_member_calls` rather than being verified — but it
does catch an invented header, which is the loudest failure mode.

Walks the project (via the UC1 affordance store) and drafts tests at two levels:

- **Unit** — one test module per source file, exercising its public symbols.
- **Functional / component I/O** — driven by the **IO map**: each component's exposed
  endpoints, outbound calls, and datastore usage become input/output tests.

```bash
secagent docs build path/to/repo          # UC1 first (recommended) — richer context
secagent testgen path/to/repo             # -> path/to/repo/secagent-tests/
secagent testgen path/to/repo -o ./gen-tests --no-functional
```

Output goes to a **new top-level folder** (default `secagent-tests/`), kept entirely
separate from the project's own structure:

```
secagent-tests/
  unit/         # mirrors the source tree (e.g. unit/services/api/test_db.py)
  functional/   # one test per component, derived from the IO map
  manifest.json # what was generated, for which targets
  README.md     # how to run + the recommendation to run UC1 first
```

Test framework is chosen per language (`testgen.frameworks`): pytest, GoogleTest/Unity
(C/C++), Jest/Vitest, `go test`, JUnit, etc. The source fed to the model is wrapped as
untrusted (CMMC-7); the README/manifest carry the CUI marking when set.

```{important}
**Run UC1 first.** `secagent testgen` reads UC1's file summaries and IO map; without a
model-backed index it still produces drafts but recommends running
`secagent docs build` first. Generated tests are **drafts** — review them (and expect
C/C++ ones to need a build harness) before adding to CI.
```

### Verifying tests, generated or hand-written

`secagent verify-tests <repo> <test files>` grades tests mechanically. It is a standalone
command on purpose — there is far more hand-written test code in the world than generated,
and *"is our existing suite vacuous?"* is the question worth answering.

```bash
secagent verify-tests . tests/test_parser.cpp        # exits non-zero on vacuous or wrong
secagent testgen . --verify                          # gate what was just generated
```

Three gates on **disjoint** defects. Measured on one real generated C++ file of 10 cases:

| gate | catches |
|---|---|
| compile | nothing here — all 10 built |
| run | the **3** that fail against correct code (false CI failures) |
| **mutation** | the **3** that compile, run, pass — and assert nothing |

The mutation gate is the one that matters. A vacuous test has every assertion sealed inside
a branch that never executes, so it is green in CI while testing nothing; compile and run
gates both wave it through. The gate breaks the implementation on purpose and requires the
test to notice.

`affordance verify` answers a fourth, different question — do the names a test cites exist?
**It is weak in both languages**, for the same structural reason: it matches backticked
tokens and raw source has none. On 5 real generated Python files it reported
`unverified_symbols: []` every time, including the one containing an invented 1-arg
`read_config`; on that same file it flagged two `.ini` fixtures the test *creates at
runtime*. It catches whole-missing-files (a fabricated `#include "GpsParser.h"`) and
nothing finer. Firing on the wrong thing while missing the real one is worse than silence,
because a reader takes the flag as evidence the check ran.

**A mutation score, not a boolean.** "Killed 7 of 10" is actionable; "survived mutation" is
a cliff that gets gamed the moment anyone optimises against it. Mutants that fail to build
are excluded from the denominator — they prove nothing about the test.

**Generated tests are executed, so they run in a container**: no network, read-only repo
copy, pid/memory/cpu caps, non-root, hard wall-clock kill. There is deliberately no
local-execution path. Everything else in secagent guards what goes *into* the model; this is
the first thing that runs what comes *out*.

#### The tool caught its own silent wrongness — twice

A verifier built to catch tests that pass while proving nothing produced exactly that
failure itself, and both instances were found only by checking against independent ground
truth rather than by trusting the tool.

**A real test was called `vacuous` because every mutant landed in a docstring.** Click's
`_utils.py` is a 36-line enum with no logic; the Python mutation skip was line-based with
no block state, so mutants landed on a URL inside a docstring and a return-annotation
arrow, all survived, and a file with real assertions was branded vacuous. Now blanked via
`tokenize`. `vacuous` is a claim about the *test*; `unverifiable` is a claim about what was
*proven*. Collapsing the first into the second when the subject cannot be mutated is the
exact dishonesty this tool exists to prevent, and it was committing it.

**A `vacuous` verdict was right by luck, not mechanism.** On the two tests an independent
hand audit had flagged as fully vacuous:

| test | verdict | why |
|---|---|---|
| `test_read_config_file_not_found` | **`vacuous`** | passes against correct code and all mutants |
| `test_sentinel_is_instance` | `unverifiable` | its subject (`_utils.py`) has no mutable logic — the honest limit |

The `vacuous` verdict matched the hand audit — but a control caught that it was
meaningless. A *genuinely good* test of the same `read_config` **also** scored `vacuous`,
because `read_config`'s only branch is `if value is None` and `is`/`is not` was not a
mutation operator: the function under test produced **zero** mutants, so good and vacuous
were indistinguishable. Adding the operator restored the distinction — the vacuous test now
survives the mutant *inside* `read_config` (0/4), the good test kills it (`useful`, 1/4).
Without the control, a false `vacuous` on any good test of an identity-branching function
would have shipped looking like the tool working.

**A known limitation this exposed, not yet addressed:** mutation targets the whole module,
not the specific function the test claims to cover, so a good test's score is deflated by
mutants in functions it correctly never calls (the good `read_config` test scores 1/4, not
1/1). The verdict is right; the score understates. Resolving it means function-level
mutation scoping via the affordance index's line ranges — deferred.

#### What this cost, and what found it

Four defects in this work were invisible to fixtures and only appeared by running it
against real repositories. Each was **silent at the point of failure**:

- the sandbox piped the test binary into `tail`, whose exit status masks the program's —
  every test passed, every mutant survived, and the harness would have pronounced the world
  vacuous while appearing to work;
- `testgen -o /elsewhere` made the written path non-repo-relative, so verification silently
  no-opped for every out-of-tree run — inside the module built to prevent silent no-ops;
- an unterminated code fence went to disk as line 1, because the stripper required a
  *closing* fence and truncated output has none;
- a test asserting on `is_test_path` passed with and without its fix, because the
  classifier was always right — the bug was that target selection never called it. Caught
  by the mutation discipline, applied to our own work.

A fifth was found while writing this section: 4 of 5 `uncompilable` verdicts were the
harness failing to link, not the tests failing to compile.

**A repair loop was designed and deliberately not built.** The failure class it targeted —
hand-written mocks colliding with real headers, typo'd fixtures — was created by generating
the wrong test framework, and an unrelated one-line convention fix erased it. Had it
shipped when proposed, it would have looked like it was working right up until the day it
had nothing left to repair. The design and its safeguards are kept in
`quality/TEST_VERIFICATION_DESIGN.md` with the criterion for revisiting.

## Integrations (UC100-series)

The 100-series are chat-ops / external-integration front ends over the same
affordance-backed engines as UC0–UC5. Merge-request review (UC100) and Mattermost
(UC101) both reach the reviewer/analysis engines through the audited MCP + affordance
layer, so a change reviewed from GitLab and one requested from chat run identically.

## UC100 — GitLab merge-request review

Reviews new MRs with an initial structured comment and replies in-thread when the bot
is @-mentioned. It reuses the affordance store to reason about the cross-component
impact of a change, not just the diff lines.

```bash
secagent review mr group/project 42 --dry-run   # print a review, don't post
secagent review mr group/project 42 --repo .    # post, with local affordance context
secagent review serve --port 8080               # webhook receiver (automation)
```

Webhook setup: point a GitLab **Merge request events** + **Comments** webhook at
`https://<host>:8080/webhook`, with the secret token matching
`SECAGENT_GITLAB__WEBHOOK_SECRET`. **`review serve` refuses to start if that variable is
unset** — the auth check used to be skipped entirely when the secret was empty, so a
missed env var produced an endpoint that would review-and-post to any project the GitLab
token could reach. Set `gitlab.webhook_allow_unauthenticated` if you genuinely want an
open endpoint (behind an authenticating proxy, say); a blank secret will not do it for
you. New/reopened MRs get an initial review; comments mentioning `@secagent-bot` get an
in-thread reply. For instances without webhook delivery, set `gitlab.poll_interval_s` to
use the polling fallback.

`secagent review poll <project> --once` from cron records what it has already reviewed in
`gitlab.poll_state_file` (under `--repo` if given, else the working directory). Keep that
path on durable storage: if it is lost, the next tick treats every open MR as new and
posts a second review on each.

**Hardening the webhook (CMMC-4).** The shared token is compared in constant time.
Optionally restrict callers with `gitlab.webhook_allowed_ips`, and serve over TLS —
adding a client CA enables **mTLS** (client certificate required and verified):

```bash
secagent review serve --tls-cert server.crt --tls-key server.key --tls-ca client-ca.crt
```

To run the reviewer continuously against a project — including the GitLab-side
bot/token/webhook setup and the polling alternative — see
{doc}`watching a GitLab repository <gitlab-watch>`.

### Editing the reviewer's alignment & verbosity

Behaviour is a plain YAML persona (`config/alignment/*.yaml`), reloaded per review —
no restart needed:

```yaml
name: "default"
alignment: >
  You are a constructive, pragmatic senior engineer reviewing a merge request...
verbosity: "normal"        # terse | normal | detailed
focus_areas: [correctness, security, error-handling, tests, readability]
tone: [professional, collaborative]
limits:
  max_inline_comments: 8
  max_reply_paragraphs: 3
use_affordances: true
```

Point `persona.profile` at another file (e.g. `security-strict.yaml`) to switch
stance.

## UC101 — Mattermost interaction

Drive SecAgent from a Mattermost channel: mention the bot to kick off a review or an
analysis and it replies in-thread with the result — the same engines as UC100/UC0,
reachable from chat. This is the chat-ops front end for teams running **SecChat**
(Mattermost) in the suite; it connects through the `@whonixnetworks/pi-mattermost` plugin.

```bash
# (lands with SecChat) receive Mattermost bot events and dispatch to a use case
secagent chat serve --port 8070
```

Setup (planned): register a Mattermost bot account, install the `pi-mattermost` plugin in
SecChat, and point it at SecAgent. Mentions like `@secagent review group/project 42` or
`@secagent analyze <repo>` map to the matching use case; results post back in-thread.

**Hardening.** Same posture as UC100: the bot token and the plugin shared secret are
required (fail-closed), callers can be restricted, traffic runs over TLS/mTLS, and every
chat-triggered action goes through the same audit trail as the CLI.

> **Status:** integration scaffold. The chat transport lands with **SecChat** (Mattermost
> + `pi-mattermost`); the engines it calls (UC0, UC100) are already here.
