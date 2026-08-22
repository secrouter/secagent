// Command kg-gocallgraph builds a type-resolved call graph for a Go module using
// golang.org/x/tools/go/packages + golang.org/x/tools/go/callgraph, and prints one JSON
// object per line for every edge whose caller AND callee are both defined inside the
// target directory tree (stdlib / third-party endpoints are silently dropped).
//
// This is the "heavy" (semantic) counterpart to secagent's tree-sitter-based, name-only
// Go call extractor: it resolves method calls through their receiver's real type (CHA —
// Class Hierarchy Analysis — over every reachable method, or RTA — Rapid Type Analysis,
// tighter but requires a `main` package as an entry point — when the module has one).
//
// RTA seeds its reachability walk from each main package's init/main only, so it never
// visits a function that main's call graph never reaches (an unused helper, exported
// library API in a repo that is both a library and a cmd) — CHA has no such notion and
// covers every statically-typed call regardless of reachability. So when a main package
// exists, RTA supplies the tighter edges for whatever it does reach, and CHA is used as a
// fallback source of edges for any caller RTA's graph has no reachable node for at all —
// the union keeps RTA's precision where it has an opinion and CHA's whole-repo recall
// everywhere else, rather than silently dropping unreachable-from-main functions to zero
// edges.
//
// Usage: kg-gocallgraph -dir <module-root>
//
// Output line shape: {"caller_func","caller_file","callee_func","callee_file","kind"}
// where kind is "direct" (an unconditional static call), "interface" (a call through an
// interface value — the target is one of possibly several implementers), or "virtual" (a
// call through a func value/closure that isn't an interface invoke — same ambiguity).
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"go/token"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/tools/go/callgraph"
	"golang.org/x/tools/go/callgraph/cha"
	"golang.org/x/tools/go/callgraph/rta"
	"golang.org/x/tools/go/packages"
	"golang.org/x/tools/go/ssa"
	"golang.org/x/tools/go/ssa/ssautil"
)

type edgeOut struct {
	CallerFunc string `json:"caller_func"`
	CallerFile string `json:"caller_file"`
	CalleeFunc string `json:"callee_func"`
	CalleeFile string `json:"callee_file"`
	Kind       string `json:"kind"`
}

func main() {
	// Never let a panic anywhere in analysis (malformed input, an x/tools internal
	// invariant, ...) crash with a non-JSON stack trace on stdout; the Python caller
	// treats a nonzero exit / empty stdout as "no edges", never as an error to raise.
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintln(os.Stderr, "kg-gocallgraph: recovered:", r)
			os.Exit(1)
		}
	}()

	dir := flag.String("dir", ".", "module root to analyze")
	flag.Parse()

	absDir, err := filepath.Abs(*dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "kg-gocallgraph:", err)
		os.Exit(1)
	}

	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedCompiledGoFiles |
			packages.NeedImports | packages.NeedDeps | packages.NeedTypes |
			packages.NeedTypesSizes | packages.NeedSyntax | packages.NeedTypesInfo |
			packages.NeedModule,
		Dir:   absDir,
		Tests: false,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		fmt.Fprintln(os.Stderr, "kg-gocallgraph:", err)
		os.Exit(1)
	}
	if len(pkgs) == 0 {
		return
	}
	// Packages with build errors are kept best-effort (partial type info still lets
	// unaffected packages resolve); only a total load failure above is fatal.

	prog, ssaPkgs := ssautil.AllPackages(pkgs, ssa.InstantiateGenerics)
	prog.Build()

	var mains []*ssa.Package
	for _, p := range ssaPkgs {
		if p != nil && p.Pkg.Name() == "main" && p.Func("main") != nil {
			mains = append(mains, p)
		}
	}

	// CHA is always computed: with no main package it is the whole call graph; with one,
	// it is the fallback source of edges for callers RTA's reachability walk never visits
	// (see the module doc comment above).
	chaGraph := cha.CallGraph(prog)
	chaGraph.DeleteSyntheticNodes()

	var edges []*callgraph.Edge
	if len(mains) > 0 {
		rtaGraph := rta.Analyze(entryPoints(mains), true).CallGraph
		rtaGraph.DeleteSyntheticNodes()

		reached := map[*ssa.Function]bool{}
		for fn, node := range rtaGraph.Nodes {
			if fn == nil || node == nil || len(node.Out) == 0 {
				continue
			}
			reached[fn] = true
			edges = append(edges, node.Out...)
		}
		// Fall back to CHA's edges for any caller RTA's graph never reached at all.
		for fn, node := range chaGraph.Nodes {
			if fn == nil || node == nil || reached[fn] {
				continue
			}
			edges = append(edges, node.Out...)
		}
	} else {
		for fn, node := range chaGraph.Nodes {
			if fn == nil || node == nil {
				continue
			}
			edges = append(edges, node.Out...)
		}
	}

	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	enc := json.NewEncoder(w)
	seen := map[string]bool{}
	for _, edge := range edges {
		if edge == nil || edge.Caller == nil || edge.Callee == nil {
			continue
		}
		caller := edge.Caller.Func
		callee := edge.Callee.Func
		cFile, cOK := fileOf(prog.Fset, caller, absDir)
		dFile, dOK := fileOf(prog.Fset, callee, absDir)
		if !cOK || !dOK {
			continue // one endpoint is outside the module: stdlib / third-party
		}
		kind := "direct"
		if edge.Site != nil {
			common := edge.Site.Common()
			if common.IsInvoke() {
				kind = "interface"
			} else if common.StaticCallee() == nil {
				kind = "virtual"
			}
		}
		out := edgeOut{
			CallerFunc: caller.Name(),
			CallerFile: cFile,
			CalleeFunc: callee.Name(),
			CalleeFile: dFile,
			Kind:       kind,
		}
		key := out.CallerFunc + "\x00" + out.CallerFile + "\x00" + out.CalleeFunc +
			"\x00" + out.CalleeFile + "\x00" + out.Kind
		if seen[key] {
			continue
		}
		seen[key] = true
		if encErr := enc.Encode(out); encErr != nil {
			fmt.Fprintln(os.Stderr, "kg-gocallgraph:", encErr)
		}
	}
}

// entryPoints returns the RTA analysis roots for a set of `main` packages: each
// package's init and main functions, the two ways Go actually starts executing it.
func entryPoints(mains []*ssa.Package) []*ssa.Function {
	roots := make([]*ssa.Function, 0, len(mains)*2)
	for _, p := range mains {
		if f := p.Func("init"); f != nil {
			roots = append(roots, f)
		}
		if f := p.Func("main"); f != nil {
			roots = append(roots, f)
		}
	}
	return roots
}

// fileOf resolves fn's declaring position to a path relative to root, and reports false
// when fn has no usable position or resolves outside root (stdlib / a third-party
// dependency, or a fully synthetic function with no source at all).
func fileOf(fset *token.FileSet, fn *ssa.Function, root string) (string, bool) {
	if fn == nil {
		return "", false
	}
	pos := fn.Pos()
	if pos == token.NoPos && fn.Object() != nil {
		pos = fn.Object().Pos() // e.g. an embedding-promoted method wrapper
	}
	if pos == token.NoPos {
		return "", false
	}
	abs := fset.Position(pos).Filename
	if abs == "" {
		return "", false
	}
	if !filepath.IsAbs(abs) {
		var err error
		if abs, err = filepath.Abs(abs); err != nil {
			return "", false
		}
	}
	rel, err := filepath.Rel(root, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return filepath.ToSlash(rel), true
}
