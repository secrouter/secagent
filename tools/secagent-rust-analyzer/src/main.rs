//! Convert a rust-analyzer SCIP index into a secagent-analysis/v1 report.
//!
//! Usage: `secagent-rust-analyzer <index.scip>` — prints the report JSON to stdout.
//!
//! SCIP gives fully-resolved symbols + occurrences. We derive:
//!   - functions/methods and types (struct/enum/trait/union) from Definition occurrences,
//!   - the type -> trait "interfaces" graph from method `is_implementation` relationships,
//!   - resolved call edges by attributing each reference-to-a-callable to the function
//!     whose body encloses it (`enclosing_range` when the indexer emits it, else the
//!     nearest preceding function definition in the same file).

use std::collections::{BTreeSet, HashMap};
use std::io::Read;

use protobuf::Message;
use scip::symbol::parse_symbol;
use scip::types::{descriptor::Suffix, symbol_information::Kind, Index, SymbolRole};
use serde::Serialize;

#[derive(Serialize)]
struct Func {
    name: String,
    qualified_name: String,
    signature: String,
    file: String,
    line: i32,
    kind: String,
    owning_type: String,
}

#[derive(Serialize)]
struct Ty {
    qualified_name: String,
    kind: String,
    file: String,
    line: i32,
    bases: Vec<String>,
    interfaces: Vec<String>,
}

#[derive(Serialize)]
struct Call {
    caller_qualified: String,
    callee_qualified: String,
    callee_file: String,
    line: i32,
    edge_kind: String,
}

#[derive(Serialize)]
struct Report {
    schema: String,
    language: String,
    backend: String,
    functions: Vec<Func>,
    types: Vec<Ty>,
    calls: Vec<Call>,
    build: serde_json::Value,
}

/// Parsed shape of a SCIP symbol: its `::`-joined path, the owning type (if a method or
/// field), the trait it implements (for a trait-impl method), and whether it is callable.
///
/// rust-analyzer encodes impl blocks specially: `mod/impl#[Self][Trait]method().` — an
/// `impl` Type descriptor followed by the Self type and (for a trait impl) the trait as
/// TypeParameter descriptors, then the method. So `impl#[Handler][Handle]handle()` is
/// `Handler`'s implementation of `Handle::handle`, which we normalize to
/// `mod::Handler::handle` (owning `mod::Handler`, implementing `mod::Handle`).
struct Parsed {
    qualified: String,
    owning_type: String,
    implemented_trait: String,
    is_callable: bool,
}

fn parse(sym: &str) -> Option<Parsed> {
    let s = parse_symbol(sym).ok()?;
    let mut ns: Vec<String> = Vec::new();
    let mut type_seg: Option<String> = None;
    let mut trait_seg: Option<String> = None;
    let mut member: Option<String> = None;
    let mut in_impl = false;
    let mut is_callable = false;
    for d in &s.descriptors {
        match d.suffix.enum_value_or_default() {
            Suffix::Namespace => ns.push(d.name.clone()),
            Suffix::Type => {
                if d.name == "impl" {
                    in_impl = true; // the `impl` pseudo-type; Self/Trait follow as TypeParams
                } else {
                    type_seg = Some(d.name.clone());
                }
            }
            Suffix::TypeParameter => {
                // Inside an impl these are [Self, Trait]; elsewhere they're real generic
                // params and irrelevant to the path.
                if in_impl {
                    if type_seg.is_none() {
                        type_seg = Some(d.name.clone());
                    } else if trait_seg.is_none() {
                        trait_seg = Some(d.name.clone());
                    }
                }
            }
            Suffix::Method => {
                member = Some(d.name.clone());
                is_callable = true;
            }
            Suffix::Term => member = Some(d.name.clone()),
            _ => return None, // Local / Parameter / Meta / Macro — not addressable
        }
    }
    let mut path = ns.clone();
    if let Some(t) = &type_seg {
        path.push(t.clone());
    }
    if let Some(m) = &member {
        path.push(m.clone());
    }
    if path.is_empty() {
        return None;
    }
    let owning = if member.is_some() && type_seg.is_some() {
        let mut o = ns.clone();
        o.push(type_seg.clone().unwrap());
        o.join("::")
    } else {
        String::new()
    };
    // KNOWN WRONG: `ns` here is the namespace of the IMPL SITE, not of the trait's own
    // definition. rust-analyzer's SCIP descriptor for a trait impl carries the trait as
    // a bare name with no path of its own, so this prefixes it with wherever the `impl`
    // block happens to live. A trait implemented by types in N different modules is
    // therefore reported N times, under N different (wrong) qualified names, instead of
    // once under its real one — measured on aho-corasick: one real `automaton::Automaton`
    // became `dfa::Automaton`, `nfa::contiguous::Automaton`, `nfa::noncontiguous::Automaton`.
    // Do NOT "fix" this in isolation: this binary is a Docker image this project cannot
    // rebuild/verify here, and a fix here would not repair reports already produced.
    // The correction lives on the ingest side instead, using ground truth already
    // present in the same report (every trait's real qualified name, from its own
    // Definition occurrence): see `_resolve_trait_interfaces` in
    // `src/secagent/affordances/analysis.py`. If you change this namespacing, you must
    // also revisit that function — it assumes exactly this failure mode.
    let itrait = trait_seg
        .map(|t| {
            let mut v = ns.clone();
            v.push(t);
            v.join("::")
        })
        .unwrap_or_default();
    Some(Parsed {
        qualified: path.join("::"),
        owning_type: owning,
        implemented_trait: itrait,
        is_callable,
    })
}

fn short_name(qualified: &str) -> String {
    qualified.rsplit("::").next().unwrap_or(qualified).to_string()
}

fn type_kind(kind: Kind) -> Option<&'static str> {
    match kind {
        Kind::Struct => Some("struct"),
        Kind::Enum => Some("enum"),
        Kind::Trait => Some("trait"),
        Kind::Union => Some("union"),
        Kind::Interface => Some("interface"),
        Kind::Class => Some("class"),
        _ => None,
    }
}

/// range = [startLine, startChar, endLine, endChar] or [startLine, startChar, endChar].
fn start_line(range: &[i32]) -> i32 {
    *range.first().unwrap_or(&0)
}
fn end_line(range: &[i32]) -> i32 {
    match range.len() {
        4 => range[2],
        _ => range[0], // single-line form
    }
}

fn main() {
    let path = std::env::args().nth(1).unwrap_or_else(|| {
        eprintln!("usage: secagent-rust-analyzer <index.scip>");
        std::process::exit(2);
    });
    let mut bytes = Vec::new();
    std::fs::File::open(&path)
        .and_then(|mut f| f.read_to_end(&mut bytes))
        .unwrap_or_else(|e| {
            eprintln!("cannot read {path}: {e}");
            std::process::exit(2);
        });
    let index = Index::parse_from_bytes(&bytes).unwrap_or_else(|e| {
        eprintln!("invalid SCIP index: {e}");
        std::process::exit(2);
    });

    let mut functions: Vec<Func> = Vec::new();
    let mut types: Vec<Ty> = Vec::new();
    let mut calls: Vec<Call> = Vec::new();

    // symbol -> defining file (for callee_file resolution).
    let mut def_file: HashMap<String, String> = HashMap::new();
    // callable symbol -> qualified name (only symbols we saw defined as callables).
    let mut callable_qual: HashMap<String, String> = HashMap::new();
    // owning type (qualified) -> set of implemented trait qualifieds (interfaces).
    let mut impls: HashMap<String, BTreeSet<String>> = HashMap::new();

    // Pass 1: definitions (functions, types) + interface relationships.
    for doc in &index.documents {
        let file = doc.relative_path.clone();
        // symbol -> SymbolInformation for this doc (kind + relationships).
        let mut info: HashMap<&str, &scip::types::SymbolInformation> = HashMap::new();
        for si in &doc.symbols {
            info.insert(si.symbol.as_str(), si);
        }
        for occ in &doc.occurrences {
            let is_def = occ.symbol_roles & (SymbolRole::Definition as i32) != 0;
            if !is_def {
                continue;
            }
            let Some(p) = parse(&occ.symbol) else { continue };
            def_file.insert(occ.symbol.clone(), file.clone());
            let line = start_line(&occ.range) + 1;
            let si = info.get(occ.symbol.as_str());
            let kind = si.map(|s| s.kind.enum_value_or_default()).unwrap_or(Kind::UnspecifiedKind);

            if let Some(tk) = type_kind(kind) {
                types.push(Ty {
                    qualified_name: p.qualified.clone(),
                    kind: tk.to_string(),
                    file: file.clone(),
                    line,
                    bases: Vec::new(),
                    interfaces: Vec::new(),
                });
            } else if p.is_callable || matches!(kind, Kind::Function | Kind::Method) {
                let sig = si
                    .map(|s| s.signature_documentation.text.clone())
                    .filter(|t| !t.is_empty())
                    .unwrap_or_default();
                let fn_kind = if p.owning_type.is_empty() { "function" } else { "method" };
                callable_qual.insert(occ.symbol.clone(), p.qualified.clone());
                functions.push(Func {
                    name: short_name(&p.qualified),
                    qualified_name: p.qualified.clone(),
                    signature: sig.lines().next().unwrap_or("").trim().to_string(),
                    file: file.clone(),
                    line,
                    kind: fn_kind.to_string(),
                    owning_type: p.owning_type.clone(),
                });
            }

            // Interface (trait-impl) edge, read straight off the impl method symbol
            // (`impl#[Self][Trait]method`): Self implements Trait.
            if !p.owning_type.is_empty() && !p.implemented_trait.is_empty() {
                impls
                    .entry(p.owning_type.clone())
                    .or_default()
                    .insert(p.implemented_trait.clone());
            }
        }
    }

    // Pass 2: calls. Attribute each reference-to-a-callable to its enclosing function.
    for doc in &index.documents {
        // Function definitions in this file: (start_line, end_line, qualified).
        let mut defs: Vec<(i32, i32, String)> = Vec::new();
        for occ in &doc.occurrences {
            let is_def = occ.symbol_roles & (SymbolRole::Definition as i32) != 0;
            if !is_def {
                continue;
            }
            if let Some(q) = callable_qual.get(&occ.symbol) {
                let s = start_line(&occ.range);
                // Prefer the definition's enclosing_range (whole body) when present.
                let e = if !occ.enclosing_range.is_empty() {
                    end_line(&occ.enclosing_range)
                } else {
                    end_line(&occ.range)
                };
                defs.push((s, e, q.clone()));
            }
        }
        defs.sort_by_key(|d| d.0);

        let enclosing = |line: i32| -> Option<&String> {
            // Innermost def whose [start, end] contains `line`; else nearest preceding.
            let mut best: Option<&(i32, i32, String)> = None;
            for d in &defs {
                if d.0 <= line && line <= d.1.max(d.0) {
                    match best {
                        Some(b) if b.0 >= d.0 => {}
                        _ => best = Some(d),
                    }
                }
            }
            if best.is_none() {
                best = defs.iter().filter(|d| d.0 <= line).max_by_key(|d| d.0);
            }
            best.map(|d| &d.2)
        };

        for occ in &doc.occurrences {
            let is_def = occ.symbol_roles & (SymbolRole::Definition as i32) != 0;
            if is_def {
                continue;
            }
            // Only references to callables we know were defined in the crate.
            let Some(callee_q) = callable_qual.get(&occ.symbol) else { continue };
            let line = start_line(&occ.range) + 1;
            let Some(caller_q) = enclosing(start_line(&occ.range)) else { continue };
            if caller_q == callee_q {
                continue; // ignore self-recursion noise
            }
            let callee_file = def_file.get(&occ.symbol).cloned().unwrap_or_default();
            calls.push(Call {
                caller_qualified: caller_q.clone(),
                callee_qualified: callee_q.clone(),
                callee_file,
                line,
                edge_kind: "direct".to_string(),
            });
        }
    }

    // Attach derived interfaces to their types.
    for t in &mut types {
        if let Some(set) = impls.get(&t.qualified_name) {
            t.interfaces = set.iter().cloned().collect();
        }
    }

    let report = Report {
        schema: "secagent-analysis/v1".to_string(),
        language: "Rust".to_string(),
        backend: "rust-analyzer-scip".to_string(),
        functions,
        types,
        calls,
        build: serde_json::json!({"system": "cargo", "offline": true, "restored": true}),
    };
    println!("{}", serde_json::to_string(&report).unwrap());
}
