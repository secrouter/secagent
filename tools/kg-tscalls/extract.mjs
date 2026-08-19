#!/usr/bin/env node
// Semantic (type-resolved) TS/JS call extraction for secagent's KG — the tsc-based
// counterpart to pyjedi.py. Reads a JSON job on stdin:
//   { "repoRoot": "<abs path>", "files": ["<repo-relative path>", ...] }
// and writes a JSON array of resolved call edges to stdout:
//   [{ "caller": "...", "caller_file": "...", "callee": "...", "callee_file": "..." }, ...]
//
// Each call's callee is resolved via the TypeScript checker's `getResolvedSignature`,
// which operates on the receiver's inferred TYPE — so `obj.method()` resolves to the
// method actually reachable on `obj`'s type, not just any same-named method in the repo
// (what the syntactic tree-sitter extractor has to guess at). Edges whose resolved
// declaration is outside the repo (lib.d.ts, node_modules, any ambient/ bodiless
// declaration) are dropped: their definition doesn't live in this codebase.
//
// A resolved MethodDeclaration/GetAccessor/SetAccessor is tagged `"kind": "virtual"`
// (default "direct" otherwise) when a subclass in the analyzed file set overrides the
// same-named member: the checker's static resolution still lands on the base
// declaration, but at runtime the receiver could be any subtype, so the edge is
// potentially-polymorphic dispatch, not an unconditional call. The Python side maps
// that to the graph's honest `may_call` predicate, the same convention the Go helper
// uses for interface/virtual dispatch (see gocalls_semantic.py's `_PREDICATE`).
//
// Never throws past main(): any failure prints to stderr and exits non-zero, so the
// Python side's degrade-gracefully contract (helper error -> 0 edges) holds.

import ts from "typescript";
import path from "node:path";
import fs from "node:fs";

// Function-like declaration kinds we treat as real definitions. Each requires a `.body`
// to count (excludes interface/ambient method SIGNATURES and .d.ts declarations, which
// have no body — this is what makes the "ambient/stdlib" skip generic instead of a
// path-based special case).
const DEF_KINDS = new Set([
  ts.SyntaxKind.FunctionDeclaration,
  ts.SyntaxKind.MethodDeclaration,
  ts.SyntaxKind.Constructor,
  ts.SyntaxKind.GetAccessor,
  ts.SyntaxKind.SetAccessor,
  ts.SyntaxKind.FunctionExpression,
  ts.SyntaxKind.ArrowFunction,
]);

function readStdin() {
  return fs.readFileSync(0, "utf8");
}

// The spelled name of a function-like declaration: its own name if any (FunctionDeclaration,
// MethodDeclaration, object-literal shorthand method, ...), else derived from the binding
// it's assigned to (`const f = () => {}`, `exports.f = function () {}`, `obj.m = function
// () {}`) since arrow functions and anonymous function expressions have no name of their own.
function declaredName(node) {
  if (node.name) {
    try {
      return node.name.getText();
    } catch {
      return null;
    }
  }
  const p = node.parent;
  if (!p) return null;
  if (ts.isVariableDeclaration(p) && p.name && ts.isIdentifier(p.name)) return p.name.text;
  if (ts.isPropertyAssignment(p) && p.name) return p.name.getText();
  if (ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
    if (ts.isPropertyAccessExpression(p.left)) return p.left.name.text;
    if (ts.isIdentifier(p.left)) return p.left.text;
  }
  return null;
}

function compilerOptions(repoRoot) {
  const configPath = ts.findConfigFile(repoRoot, ts.sys.fileExists, "tsconfig.json");
  const base = {
    allowJs: true,
    checkJs: false,
    jsx: ts.JsxEmit.Preserve,
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    skipLibCheck: true,
    noEmit: true,
    esModuleInterop: true,
    resolveJsonModule: true,
  };
  if (!configPath) return base;
  try {
    const read = ts.readConfigFile(configPath, ts.sys.readFile);
    if (read.error) return base;
    const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, path.dirname(configPath));
    // Keep the repo's own settings (path aliases, target, jsx, ...) but force the bits
    // this extractor needs regardless of what the repo built for: no emit, allow JS so
    // .js/.jsx files in the explicit file list still get checked, skip full lib
    // checking so a partial/vendored lib set doesn't abort the whole program.
    return { ...parsed.options, allowJs: true, noEmit: true, skipLibCheck: true };
  } catch {
    return base;
  }
}

function main() {
  const job = JSON.parse(readStdin());
  const repoRoot = path.resolve(job.repoRoot);
  const relFiles = Array.isArray(job.files) ? job.files : [];
  if (relFiles.length === 0) {
    process.stdout.write("[]");
    return;
  }
  const rootFileNames = relFiles.map((f) => path.resolve(repoRoot, f));
  const wanted = new Set(rootFileNames);

  const options = compilerOptions(repoRoot);
  const program = ts.createProgram({ rootNames: rootFileNames, options });
  const checker = program.getTypeChecker();

  const repoPrefix = repoRoot + path.sep;
  const nodeModulesMarker = `${path.sep}node_modules${path.sep}`;

  function insideRepo(sourceFile) {
    const fileName = path.resolve(sourceFile.fileName);
    if (!fileName.startsWith(repoPrefix)) return false;
    if (fileName.includes(nodeModulesMarker)) return false;
    if (program.isSourceFileDefaultLibrary(sourceFile)) return false;
    return true;
  }

  function relOf(sourceFile) {
    return path.relative(repoRoot, path.resolve(sourceFile.fileName)).split(path.sep).join("/");
  }

  // Member kinds that can participate in override-based (virtual) dispatch: a subclass
  // declaring one of these with the same name shadows the base implementation at runtime.
  const OVERRIDABLE_KINDS = new Set([
    ts.SyntaxKind.MethodDeclaration,
    ts.SyntaxKind.GetAccessor,
    ts.SyntaxKind.SetAccessor,
  ]);

  // Class hierarchy pre-pass: collect every ClassDeclaration reachable from the program
  // (not just the wanted/root files — a subclass living outside the explicit file list,
  // e.g. pulled in via an import, still creates override potential for the base's call
  // sites) and link each to its direct subclasses, so `resolveCallee` can ask "does any
  // subtype of this method's declaring class override it?" in O(children) per call.
  const classBySymbol = new Map(); // ts.Symbol -> ClassDeclaration
  const childrenOf = new Map(); // ClassDeclaration -> ClassDeclaration[]

  for (const sourceFile of program.getSourceFiles()) {
    if (program.isSourceFileDefaultLibrary(sourceFile)) continue;
    if (sourceFile.fileName.includes(nodeModulesMarker)) continue;
    ts.forEachChild(sourceFile, function visit(node) {
      if (ts.isClassDeclaration(node) && node.name) {
        const sym = checker.getSymbolAtLocation(node.name);
        if (sym) classBySymbol.set(sym, node);
      }
      ts.forEachChild(node, visit);
    });
  }
  for (const classNode of classBySymbol.values()) {
    const extendsClause = (classNode.heritageClauses || []).find(
      (h) => h.token === ts.SyntaxKind.ExtendsKeyword
    );
    if (!extendsClause || !extendsClause.types.length) continue;
    let parentSym;
    try {
      parentSym = checker.getSymbolAtLocation(extendsClause.types[0].expression);
    } catch {
      parentSym = null;
    }
    const parentNode = parentSym && classBySymbol.get(parentSym);
    if (!parentNode) continue;
    if (!childrenOf.has(parentNode)) childrenOf.set(parentNode, []);
    childrenOf.get(parentNode).push(classNode);
  }

  // True if `classNode` or any transitive subclass declares its own member named `name`
  // among OVERRIDABLE_KINDS — i.e. a call statically resolved to `classNode`'s member
  // could, at runtime, dispatch to an overriding subtype instead.
  function hasOverride(classNode, name, seen = new Set()) {
    if (seen.has(classNode)) return false; // guard against (invalid but possible) cycles
    seen.add(classNode);
    for (const child of childrenOf.get(classNode) || []) {
      const overridesHere = child.members.some(
        (m) => OVERRIDABLE_KINDS.has(m.kind) && m.name && m.name.getText() === name
      );
      if (overridesHere || hasOverride(child, name, seen)) return true;
    }
    return false;
  }

  const edges = [];

  function resolveCallee(node) {
    const expr = node.expression;
    let nameNode = null;
    if (ts.isIdentifier(expr)) nameNode = expr;
    else if (ts.isPropertyAccessExpression(expr)) nameNode = expr.name;
    else return null; // computed / dynamic callee (obj["x"](), IIFE, ...) — not resolved

    let decl = null;
    try {
      const sig = checker.getResolvedSignature(node);
      if (sig && sig.declaration) decl = sig.declaration;
    } catch {
      decl = null;
    }
    if (!decl) {
      try {
        let sym = checker.getSymbolAtLocation(nameNode);
        if (sym && sym.flags & ts.SymbolFlags.Alias) sym = checker.getAliasedSymbol(sym);
        decl = sym && sym.declarations && sym.declarations[0];
      } catch {
        decl = null;
      }
    }
    if (!decl || !DEF_KINDS.has(decl.kind) || !decl.body) return null;

    const sourceFile = decl.getSourceFile();
    if (!insideRepo(sourceFile)) return null;

    const name = declaredName(decl) || (ts.isIdentifier(nameNode) ? nameNode.text : nameNode.getText());
    if (!name) return null;

    let kind = "direct";
    if (OVERRIDABLE_KINDS.has(decl.kind)) {
      const classNode = ts.findAncestor(decl, ts.isClassDeclaration);
      if (classNode && hasOverride(classNode, name)) kind = "virtual";
    }
    return { callee: name, callee_file: relOf(sourceFile), kind };
  }

  function walk(node, enclosing, callerFile) {
    let enc = enclosing;
    if (DEF_KINDS.has(node.kind)) {
      const name = declaredName(node);
      if (name) enc = name;
    }
    if (ts.isCallExpression(node) && enc) {
      const resolved = resolveCallee(node);
      if (resolved) {
        edges.push({ caller: enc, caller_file: callerFile, ...resolved });
      }
    }
    ts.forEachChild(node, (child) => walk(child, enc, callerFile));
  }

  for (const sourceFile of program.getSourceFiles()) {
    if (!wanted.has(path.resolve(sourceFile.fileName))) continue;
    if (!insideRepo(sourceFile)) continue;
    walk(sourceFile, null, relOf(sourceFile));
  }

  process.stdout.write(JSON.stringify(edges));
}

try {
  main();
} catch (err) {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
}
