/**
 * secagent extension for the pi coding agent (pi.dev).
 *
 * pi provides the agent loop, sessions, and provider selection. This extension makes
 * secagent's context-frugal affordances available to the model as first-class tools
 * (so it reads summaries / IO edges / slices instead of whole files), plus two slash
 * commands for the headline use cases.
 *
 * Everything shells out to the Python `secagent` CLI — the single source of truth — so
 * there is no logic duplicated here. Install secagent (`pip install secagent`) and ensure
 * `secagent` is on PATH.
 *
 * The API shape follows pi's documented extension model:
 *   export default function (pi: ExtensionAPI) { pi.registerTool(...); pi.registerCommand(...) }
 * Field names target the current pi extension API; adjust if your pi version differs.
 */

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// Minimal structural typing so this compiles without pi's types present; at runtime
// pi injects the real ExtensionAPI. A tool result's `content` is an ARRAY of content
// blocks (MCP-style) — pi iterates/filters over it, so a bare string crashes the loop
// with "result.content.filter is not a function".
type ContentBlock = { type: "text"; text: string };
type ToolResult = { content: ContentBlock[]; isError?: boolean };
interface ExtensionAPI {
  registerTool(tool: {
    name: string;
    label: string;
    description: string;
    parameters: Record<string, unknown>;
    // pi's REAL signature: the tool-call ID comes FIRST, the arguments SECOND.
    //   execute(toolCallId, params, signal, onUpdate, ctx)
    // Reading args off the first parameter yields undefined for every field — see
    // the comment on withArgs().
    execute: (
      toolCallId: string,
      params: Record<string, any>,
      signal?: unknown,
      onUpdate?: unknown,
      ctx?: unknown,
    ) => Promise<ToolResult>;
  }): void;
  // pi's command API: the field is `handler` (not `run`), it returns void, and output
  // is emitted via pi.sendUserMessage (a returned string is ignored). Getting this wrong
  // fails with "command.handler is not a function".
  registerCommand(
    name: string,
    spec: { description?: string; handler: (args: string, ctx: unknown) => Promise<void> },
  ): void;
  // Send a message to the agent as if from the user (always triggers a turn).
  sendUserMessage(content: string): void;
  cwd?: string;
}

const REPO = () => process.env.SECAGENT_REPO || process.cwd();

// Best-effort "starting…" feedback so a long-running command isn't silent. Guarded for
// older pi builds that may not expose ctx.ui.notify.
function note(ctx: unknown, message: string): void {
  const ui = (ctx as { ui?: { notify?: (m: string, t?: string) => void } })?.ui;
  ui?.notify?.(message);
}

// Run the secagent CLI and return its stdout (or an error message) as plain text.
function runSecAgentText(args: string[]): Promise<{ text: string; isError: boolean }> {
  return new Promise((resolve) => {
    execFile("secagent", args, { maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        resolve({ text: `ERROR: ${stderr || err.message}`, isError: true });
      } else {
        resolve({ text: stdout.trim(), isError: false });
      }
    });
  });
}

// Tool wrapper: wrap the CLI output in the content-block array pi expects.
async function runSecAgent(args: string[]): Promise<ToolResult> {
  const { text, isError } = await runSecAgentText(args);
  return { content: [{ type: "text", text }], isError };
}

function toolError(text: string): ToolResult {
  return { content: [{ type: "text", text }], isError: true };
}

/**
 * Read a REQUIRED string argument, or return null.
 *
 * Without this, `String(a.name)` turns a missing argument into the literal string
 * "undefined", and secagent dutifully searches for a symbol named "undefined" and reports
 * "No symbols matching 'undefined'". That reads as a legitimate empty result: observed in
 * a real session where the model then abandoned secagent entirely after one such "failure"
 * and fell back to grep — with nothing in the visible output to show a tool had misfired.
 */
function requireArg(a: Record<string, any>, key: string): string | null {
  const v = a?.[key];
  if (typeof v === "string" && v.trim() !== "") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}

/** Optional string argument; "" when absent (callers omit it from argv). */
function optArg(a: Record<string, any>, key: string): string {
  const v = a?.[key];
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/**
 * Build an executor that validates required args before shelling out.
 *
 * NOTE the parameter order. pi calls `execute(toolCallId, params, ...)` — the arguments
 * object is the SECOND parameter. Reading fields off the first one gives `undefined`
 * for everything, which previously reached secagent as the literal string "undefined"
 * (a search for a symbol named "undefined", reported as a normal empty result).
 */
function withArgs(
  required: string[],
  build: (a: Record<string, any>) => string[],
): (toolCallId: string, params: Record<string, any>) => Promise<ToolResult> {
  return async (_toolCallId, params) => {
    const a = params ?? {};
    for (const key of required) {
      if (requireArg(a, key) === null) {
        return toolError(
          `ERROR: missing required argument '${key}'. Call this tool again with ` +
            `'${key}' set to a non-empty string. (Received: ${JSON.stringify(a)})`,
        );
      }
    }
    return runSecAgent(build(a));
  };
}

const obj = (props: Record<string, unknown>, required: string[] = []) => ({
  type: "object",
  properties: props,
  required,
});

// ── Analysis containers (heavyweight tooling that is NOT in this runtime's own image) ─────────
//
// Two runtimes, one tool:
//   • KUBERNETES POOL POD: the deployment attached analysis SIDECARS sharing this pod's
//     /workspace volume (see secchat docs/agent-pool.md). `SECCHAT_ANALYSIS` names them; the
//     invocation seam is a file work-queue on the shared volume (containers can't exec into
//     each other): write `<dir>/.analysis/<name>/request`, the sidecar runs it with its own
//     tooling, poll `exit` + read `output`.
//   • LOCAL DOCKER (desktop daemon / host pi): `SECAGENT_ANALYSIS_IMAGES` (name=image CSV)
//     names locally runnable analyzer images; the tool `docker run`s the image with the
//     workspace mounted, entrypoint overridden to `sh -c <command>` — the SAME command
//     contract as the sidecar queue. Offline by default (`--network none`), matching the
//     analyzer images' own documented posture; opt out with SECAGENT_ANALYSIS_EGRESS=1.
// A name present in BOTH prefers the in-pod sidecar (no docker needed there).

function queueAnalyzers(): string[] {
  return (process.env.SECCHAT_ANALYSIS ?? "").split(",").map((s) => s.trim()).filter(Boolean);
}

function dockerAnalyzers(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const entry of (process.env.SECAGENT_ANALYSIS_IMAGES ?? "").split(",")) {
    const eq = entry.indexOf("=");
    if (eq > 0) {
      const name = entry.slice(0, eq).trim();
      const image = entry.slice(eq + 1).trim();
      if (name && image) out[name] = image;
    }
  }
  return out;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Run a command in an in-pod analysis sidecar via the shared-volume work queue. */
async function runQueueAnalysis(name: string, command: string, timeoutS: number): Promise<ToolResult> {
  const dir = join(process.env.SECCHAT_ANALYSIS_DIR || "/workspace", ".analysis", name);
  try {
    mkdirSync(dir, { recursive: true });
    // A leftover result from a prior request must not be mistaken for this one's.
    for (const f of ["exit", "output"]) {
      try { unlinkSync(join(dir, f)); } catch { /* absent is fine */ }
    }
    writeFileSync(join(dir, "request"), command, "utf8");
  } catch (err) {
    return toolError(`ERROR: could not queue the request for '${name}': ${(err as Error).message}`);
  }
  const deadline = Date.now() + timeoutS * 1000;
  while (Date.now() < deadline) {
    if (existsSync(join(dir, "exit"))) {
      const code = readFileSync(join(dir, "exit"), "utf8").trim();
      let output = "";
      try { output = readFileSync(join(dir, "output"), "utf8"); } catch { /* no output */ }
      const isError = code !== "0";
      return { content: [{ type: "text", text: `exit ${code}\n${output}`.trim() }], isError };
    }
    await sleep(1000);
  }
  return toolError(
    `ERROR: analyzer '${name}' did not finish within ${timeoutS}s — the request may still be ` +
    "running; retry with a larger timeout_s, or check /workspace/.analysis/" + name + "/",
  );
}

/** Run a command in a local analyzer container (docker), workspace mounted, offline by default. */
function runDockerAnalysis(image: string, command: string, timeoutS: number, cwd: string): Promise<ToolResult> {
  const network = process.env.SECAGENT_ANALYSIS_EGRESS === "1" ? [] : ["--network", "none"];
  const argv = [
    "run", "--rm", ...network,
    "-v", `${cwd}:/workspace`, "-w", "/workspace",
    "--entrypoint", "sh", image, "-c", command,
  ];
  return new Promise((resolve) => {
    execFile("docker", argv, { maxBuffer: 8 * 1024 * 1024, timeout: timeoutS * 1000 },
      (err, stdout, stderr) => {
        const text = `${stdout ?? ""}${stderr ? `\n${stderr}` : ""}`.trim();
        if (err) {
          resolve({ content: [{ type: "text", text: `ERROR: ${text || err.message}` }], isError: true });
        } else {
          resolve({ content: [{ type: "text", text: text || "(no output)" }], isError: false });
        }
      });
  });
}

export default function (pi: ExtensionAPI): void {
  const repoArg = () => REPO();

  // --- read-only affordance tools (keep the model's context small) ----------
  pi.registerTool({
    name: "secagent_structure",
    label: "secagent structure",
    description:
      "Project structure outline: components, languages, entrypoints. " +
      "Returns an indented text outline, not JSON.",
    parameters: obj({}),
    execute: () => runSecAgent(["affordance", "structure", repoArg()]),
  });

  pi.registerTool({
    name: "secagent_io_map",
    label: "secagent IO map",
    description: "IO map: internal imports, HTTP endpoints, outbound calls, datastores.",
    parameters: obj({}),
    execute: () => runSecAgent(["affordance", "io", repoArg()]),
  });

  pi.registerTool({
    name: "secagent_components",
    label: "secagent components",
    description:
      "The repo's components (cohesive dirs): name, language, file count, and a " +
      "representative one-line purpose. Use to get the lay of the land before drilling in.",
    parameters: obj({}),
    execute: () => runSecAgent(["affordance", "components", repoArg()]),
  });

  pi.registerTool({
    name: "secagent_search",
    label: "secagent search",
    description: "Rank files by relevance to a query (returns paths + one-line purposes).",
    parameters: obj({ query: { type: "string" } }, ["query"]),
    execute: withArgs(["query"], (a) => ["affordance", "search", repoArg(), String(a.query)]),
  });

  pi.registerTool({
    name: "secagent_file_summary",
    label: "secagent file summary",
    description: "Affordance summary for one file (purpose, key symbols, IO signals).",
    parameters: obj({ path: { type: "string" } }, ["path"]),
    execute: withArgs(["path"], (a) => ["affordance", "summary", repoArg(), String(a.path)]),
  });

  pi.registerTool({
    name: "secagent_find_symbol",
    label: "secagent find symbol",
    description: "Find functions/classes by (partial) name across the repo.",
    parameters: obj({ name: { type: "string" } }, ["name"]),
    execute: withArgs(["name"], (a) => ["affordance", "find-symbol", repoArg(), String(a.name)]),
  });

  pi.registerTool({
    name: "secagent_functions",
    label: "secagent functions",
    description:
      "Functions defined in a file — signatures + one-line descriptions — so you can grasp " +
      "a file's API without reading it.",
    parameters: obj({ path: { type: "string" } }, ["path"]),
    execute: withArgs(["path"], (a) => ["affordance", "functions", repoArg(), String(a.path)]),
  });

  pi.registerTool({
    name: "secagent_calls",
    label: "secagent call map",
    description:
      "Inter-file call map (file → file: callees). Pass a path to filter to one file's edges. " +
      "Returns text lines `src -> dst: callee, …`, not JSON.",
    parameters: obj({ path: { type: "string" } }),
    execute: (_id, params) =>
      runSecAgent(
        optArg(params ?? {}, "path")
          ? ["affordance", "calls", repoArg(), optArg(params ?? {}, "path")]
          : ["affordance", "calls", repoArg()],
      ),
  });

  pi.registerTool({
    name: "secagent_callers",
    label: "secagent callers",
    description:
      "Who calls a function (reverse call map) — what depends on it. Use before changing a " +
      "symbol to gauge blast radius. Rows are {path, caller, line, dispatch}; `path` and " +
      "`line` feed secagent_read_slice directly. `line` is absent on a store indexed before " +
      "call sites were recorded — reindex to get it.",
    parameters: obj({ symbol: { type: "string" } }, ["symbol"]),
    execute: withArgs(["symbol"], (a) => ["affordance", "callers", repoArg(), String(a.symbol)]),
  });

  pi.registerTool({
    name: "secagent_types",
    label: "secagent types",
    description:
      "Declared types and their inheritance (bases/interfaces) from the heavy backends. " +
      "Optional name filter.",
    parameters: obj({ name: { type: "string" } }),
    execute: (_id, params) =>
      runSecAgent(
        optArg(params ?? {}, "name")
          ? ["affordance", "types", repoArg(), optArg(params ?? {}, "name")]
          : ["affordance", "types", repoArg()],
      ),
  });

  pi.registerTool({
    name: "secagent_plan",
    label: "secagent plan",
    description:
      "UC0 analysis plan: components binned by language + the secagent tools to run on " +
      "each. Use this first when doing a full analysis of an unfamiliar project.",
    parameters: obj({}),
    execute: () => runSecAgent(["affordance", "plan", repoArg()]),
  });

  pi.registerTool({
    name: "secagent_context",
    label: "secagent context",
    description: "Assemble a budget-bounded context block relevant to a query.",
    parameters: obj({ query: { type: "string" } }, ["query"]),
    execute: withArgs(["query"], (a) => ["affordance", "context", repoArg(), String(a.query)]),
  });

  pi.registerTool({
    name: "secagent_read_slice",
    label: "secagent read slice",
    description: "Read a bounded, traversal-guarded slice of a real file.",
    parameters: obj(
      { path: { type: "string" }, start: { type: "integer" }, end: { type: "integer" } },
      ["path"],
    ),
    execute: withArgs(["path"], (a) => [
      "affordance", "slice", repoArg(), String(a.path),
      "--start", String(a.start ?? 1), "--end", String(a.end ?? 50),
    ]),
  });

  // --- slash commands -------------------------------------------------------

  // UC0: full project analysis. Index, bin components by language, build the docs, and
  // report the per-language tool plan; the model then runs the recommended per-language
  // tools and synthesizes a summary (see the secagent-analysis skill).
  pi.registerCommand("secagent-plan", {
    description: "Show the UC0 analysis plan: components binned by language + tools. Usage: /secagent-plan",
    handler: async (_args, ctx) => {
      note(ctx, "secagent: computing the analysis plan…");
      const res = await runSecAgentText(["affordance", "plan", repoArg()]);
      pi.sendUserMessage(res.text);
    },
  });

  pi.registerCommand("secagent-analyze-all", {
    description:
      "UC0: full analysis — index, bin by language, build docs, report the per-language " +
      "plan, then (by default) write a codebase summary + diagrams. " +
      "Usage: /secagent-analyze-all [out] [--no-summary]",
    handler: async (args, ctx) => {
      // [out] is the first non-flag token; --no-summary skips the final summary+diagrams.
      const tokens = args.trim().split(/\s+/).filter(Boolean);
      const noSummary = tokens.includes("--no-summary");
      const out = tokens.find((t) => !t.startsWith("--")) || "secagent-analysis";
      note(ctx, "secagent: full analysis started — indexing the repo (this can take a while)…");
      const index = await runSecAgentText(["index", repoArg()]);
      if (index.isError) {
        pi.sendUserMessage(index.text);
        return;
      }
      note(ctx, "secagent: binning by language, then building the docs site…");
      const plan = await runSecAgentText(["affordance", "plan", repoArg()]);
      const docs = await runSecAgentText(["docs", "build", repoArg(), "-o", `${out}/docs`]);
      note(ctx, "secagent: full analysis complete.");
      // By default UC0 finishes with a summary that embeds secagent's deterministic drawio
      // diagrams (already produced by the docs build above); --no-summary stops after the
      // per-language tools and leaves only the raw outputs.
      const dia = `${out}/docs/source/_diagrams`;
      const nextStep = noSummary
        ? "Next: run the per-language tools above (e.g. C/C++ → `secagent scan`; C# → " +
          "`secagent analyze deep`). Summary + diagrams skipped (--no-summary)."
        : "Next: run the per-language tools above (e.g. C/C++ → `secagent scan`; C# → " +
          "`secagent analyze deep`), then write **ANALYSIS.md** — a codebase summary that " +
          "**embeds the drawio diagrams secagent already generated** (architecture: " +
          `\`${dia}/components.svg\`; data flow: \`${dia}/system_io.svg\`; editable ` +
          "`.drawio` sources alongside) and links the Architecture / Data Flow & IO / Call " +
          "Map pages of the docs site. Don't hand-author diagrams — the drawio diagrams are " +
          "deterministic from the IO map. Include only the views that fit the project. This " +
          "summary step is on by default — pass --no-summary to skip it. Follow the " +
          "secagent-analysis skill.";
      pi.sendUserMessage([
        "# UC0 full analysis",
        docs.isError
          ? `Docs build failed: ${docs.text}`
          : `Docs + summaries written to ${out}/docs (open ${out}/docs/build/html/index.html; ` +
            `per-file/function summaries in ${out}/docs/summaries.md).`,
        "",
        "## Plan — components binned by language, with the tools to run on each:",
        plan.text,
        "",
        nextStep,
      ].join("\n"));
    },
  });

  pi.registerCommand("secagent-docs", {
    description: "Generate Sphinx docs + Draw.io diagrams for a repo. Usage: /secagent-docs [out]",
    handler: async (args, ctx) => {
      const out = args.trim() || "secagent-docs";
      note(ctx, "secagent: building the docs site (index → diagrams → Sphinx)…");
      const res = await runSecAgentText(["docs", "build", repoArg(), "-o", out]);
      pi.sendUserMessage(res.text);
    },
  });

  pi.registerCommand("secagent-review", {
    description: "Review a GitLab MR. Usage: /secagent-review <project> <mr_iid> [--dry-run]",
    handler: async (args, ctx) => {
      const parts = args.trim().split(/\s+/).filter(Boolean);
      if (parts.length < 2) {
        pi.sendUserMessage("Usage: /secagent-review <project> <mr_iid> [--dry-run]");
        return;
      }
      note(ctx, "secagent: reviewing the merge request…");
      const res = await runSecAgentText(["review", "mr", ...parts, "--repo", repoArg()]);
      pi.sendUserMessage(res.text);
    },
  });

  pi.registerCommand("secagent-testgen", {
    description:
      "Generate unit + functional tests into a separate folder (run UC1 first). " +
      "Usage: /secagent-testgen [out]",
    handler: async (args, ctx) => {
      const out = args.trim() || "secagent-tests";
      note(ctx, "secagent: generating tests…");
      const res = await runSecAgentText(["testgen", repoArg(), "-o", out]);
      pi.sendUserMessage(res.text);
    },
  });

  pi.registerCommand("secagent-scan", {
    description:
      "LLM memory/stability scan of C/C++ against a configurable rule set. " +
      "Usage: /secagent-scan [out] [rules.yaml]",
    handler: async (args, ctx) => {
      const parts = args.trim().split(/\s+/).filter(Boolean);
      const [out = "secagent-scan", rules] = parts;
      const cmd = ["scan", repoArg(), "-o", out];
      if (rules) cmd.push("--rules", rules);
      note(ctx, "secagent: scanning for memory/stability issues…");
      const res = await runSecAgentText(cmd);
      pi.sendUserMessage(res.text);
    },
  });

  pi.registerCommand("secagent-analyze", {
    description:
      "C/C++ static analysis (IKOS). Usage: /secagent-analyze <ikos-report.json> [out] " +
      "(ingests a report; use the secagent CLI 'analyze run' to invoke IKOS directly)",
    handler: async (args, ctx) => {
      const parts = args.trim().split(/\s+/).filter(Boolean);
      if (parts.length < 1) {
        pi.sendUserMessage("Usage: /secagent-analyze <ikos-report.json> [out]");
        return;
      }
      const [report, out = "secagent-analysis"] = parts;
      note(ctx, "secagent: ingesting the analysis report…");
      const res = await runSecAgentText(["analyze", "ingest", repoArg(), report, "-o", out]);
      pi.sendUserMessage(res.text);
    },
  });

  // --- analysis containers (in-pod sidecars OR local docker) -----------------
  // Registered ONLY when at least one analyzer is available in this runtime, so the model
  // never sees a tool it can't use. The description enumerates what IS available — that's
  // how the agent learns the analyzers exist (tools are self-describing; no primer needed).
  const queued = queueAnalyzers();
  const dockered = dockerAnalyzers();
  const available = [...new Set([...queued, ...Object.keys(dockered)])].sort();
  if (available.length > 0) {
    pi.registerTool({
      name: "analysis_run",
      label: "analysis container",
      description:
        `Run a shell command inside an ANALYSIS TOOLING container that shares this workspace — ` +
        `use it for heavyweight analyzers not installed here. Available: ${available.join(", ")}. ` +
        `The command runs with the analyzer's own tooling on PATH and the workspace as cwd ` +
        `(e.g. analyzer "rust" → "rust-analyzer --version", or the secagent analyzers' own CLIs). ` +
        `Output (stdout+stderr) and the exit code come back when it finishes.`,
      parameters: obj(
        {
          analyzer: { type: "string", description: `Which analyzer: ${available.join(" | ")}` },
          command: { type: "string", description: "Shell command to run in the analyzer container (cwd = the shared workspace)." },
          timeout_s: { type: "number", description: "Seconds to wait before giving up (default 300)." },
        },
        ["analyzer", "command"],
      ),
      execute: async (_toolCallId, params) => {
        const a = params ?? {};
        const analyzer = requireArg(a, "analyzer");
        const command = requireArg(a, "command");
        if (!analyzer || !command) {
          return toolError("ERROR: 'analyzer' and 'command' are both required non-empty strings.");
        }
        const timeoutS = typeof a.timeout_s === "number" && a.timeout_s > 0 ? a.timeout_s : 300;
        // In-pod sidecar first (no docker inside a pod); local docker otherwise.
        if (queued.includes(analyzer)) {
          return runQueueAnalysis(analyzer, command, timeoutS);
        }
        if (dockered[analyzer]) {
          return runDockerAnalysis(dockered[analyzer]!, command, timeoutS, REPO());
        }
        return toolError(`ERROR: no such analyzer '${analyzer}'. Available: ${available.join(", ")}.`);
      },
    });
  }
}
