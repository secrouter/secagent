# Rust trait-resolution fixture (`trait-fixture.json`)

`trait-fixture.json` is a `secagent-analysis/v1` report, hand-trimmed from the real
`secagent-analyzer-rust:latest` output produced by running the shipped Rust analyzer
container against the real `aho-corasick` crate
(https://github.com/BurntSushi/aho-corasick). The full report is 345KB with 103 type
records (73 struct, 11 enum, 19 trait); it is not vendored here, but it is exactly
reproducible — clone the crate and run the shipped image over it:

```
docker run --rm --network none -v /path/to/aho-corasick:/src:ro \
    secagent-analyzer-rust:latest /src > aho-corasick-report.json
```

(`--network none` and the read-only mount are how `heavy.py::run_rust_analyzer` invokes
it, so this reproduces what secagent itself would ingest.) This fixture is 8 of those 103 `types[]` entries, copied
byte-for-byte (not retyped) via a one-off script that loaded the full report, looked up
each qualified name, and wrote out only the matching records with `functions`/`calls`
emptied (this fixture is about type/interface resolution, not the call map). `schema`,
`language`, `backend`, and `build` are carried over unchanged.

It exists for `tests/test_rust_parity.py`, which pins the fix to
`src/secagent/affordances/analysis.py::_resolve_trait_interfaces` for the defect where
`tools/secagent-rust-analyzer/src/main.rs`'s SCIP symbol parser qualifies an implemented
trait with the **impl site's** namespace instead of the trait's own defining module. One
real trait, `automaton::Automaton`, therefore surfaces in the raw report as three
different (wrong) interface strings, one per implementing module:

- `nfa::noncontiguous::NFA` -> interfaces `["nfa::noncontiguous::Automaton"]`
- `nfa::contiguous::NFA` -> interfaces `["nfa::contiguous::Automaton"]`
- `dfa::DFA` -> interfaces `["dfa::Automaton"]`

None of `nfa::noncontiguous::Automaton`, `nfa::contiguous::Automaton`, or
`dfa::Automaton` appears anywhere in the real report as its own `kind == "trait"`
record — only `automaton::Automaton` (kind `trait`, `src/automaton.rs:198`) does. That
asymmetry (three broken references, one real definition) is the defect, straight from
production data, not invented for the test.

The fixture also carries the real record that must be left alone: `util::prefilter::Packed`
implements `util::prefilter::PrefilterI`, and that qualified name IS already a correct
trait definition in the report (also included here). The already-correct case is real
data too, not a synthetic silence check.

Finally, the fixture carries both real `Pointer` trait definitions —
`util::int::Pointer` (`src/util/int.rs:270`) and `packed::ext::Pointer`
(`src/packed/ext.rs:2`) — the one genuinely ambiguous short name in the whole
aho-corasick report (verified by scanning all 19 trait records for duplicate short
names; `Pointer` is the only collision). This is real, not invented: two distinct traits
really do share the short name `Pointer` in this crate, so a resolver that keys on short
names alone has a real chance of merging them.

One caveat, checked against the full 345KB report and worth stating plainly: **no type
in the real aho-corasick report references `Pointer` as an interface at all**, let alone
at a wrong namespace — the ambiguity exists in the trait *definitions*, but nothing in
the crate exercises it as a broken *reference*. So the over-merge attack in
`test_rust_parity.py` cannot be pinned by replaying the real report alone. That test
constructs one extra type on top of these real `Pointer` definitions — a struct whose
`interfaces` names a wrong-namespace `Pointer` (e.g. `"other::Pointer"`, matching neither
real definition) — and documents inline that this specific reference is synthetic, built
on top of real ambiguous definitions, because the shape doesn't occur in this particular
crate even though the ambiguity it guards against does.

Verified counts (computed directly from the full regenerated report above, not from
this trimmed fixture): of the 4 `interfaces` entries in the whole 103-type report, exactly 3 are the
wrong-namespace `Automaton` references above and exactly 1 (`Packed` -> `PrefilterI`) is
already correct. After the fix, ingesting the *full* report should rewrite exactly 3
interface references and leave exactly 1 untouched.
