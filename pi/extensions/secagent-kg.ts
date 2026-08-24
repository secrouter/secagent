/**
 * secagent-kg — push-retrieval knowledge-graph injection for pi.
 *
 * The mechanism from the "Applying Knowledge Graphs" design: retrieval happens BEFORE
 * the model wakes, deterministically. On `before_agent_start` (fired after the user
 * submits a prompt, before the agent loop) this shells to `secagent kg recall`, which
 * seeds on the prompt and walks the graph in milliseconds, and appends the recalled
 * facts to the system prompt for that turn. The model reads the answer instead of
 * grep-searching for it; a miss injects nothing.
 *
 * Mirrors pi/extensions/secagent.ts: minimal structural typing (pi injects the real
 * ExtensionAPI at runtime), and everything shells to the `secagent` CLI. Attach with
 * `pi --extension pi/extensions/secagent-kg.ts` (or let `secagent pi run` auto-attach it
 * when `[knowledge_graph].inject` is set).
 */
import { execFile } from "node:child_process";

// Structural subset of pi's ExtensionAPI — just the one hook this extension uses.
interface BeforeAgentStartEvent {
  prompt: string;
  systemPrompt: string;
}
interface BeforeAgentStartResult {
  systemPrompt?: string;
}
interface ExtensionAPI {
  on(
    event: "before_agent_start",
    handler: (
      event: BeforeAgentStartEvent,
      ctx: unknown,
    ) => Promise<BeforeAgentStartResult | void> | BeforeAgentStartResult | void,
  ): void;
}

const REPO = (): string => process.env.SECAGENT_REPO || process.cwd();

// The empty-recall sentinel `secagent kg recall` prints on a miss — inject nothing then.
const NO_MATCHES = "memory: no matches";

/** Run `secagent kg recall` for a prompt and return its text (or "" on any failure). */
function recall(prompt: string): Promise<string> {
  return new Promise((resolve) => {
    const args = ["kg", "recall", REPO(), "--prompt", prompt];
    // Config knobs flow through as env (set by `secagent pi run`); omit to use defaults.
    if (process.env.SECAGENT_KG_HOPS) args.push("--hops", process.env.SECAGENT_KG_HOPS);
    if (process.env.SECAGENT_KG_TOP_K) args.push("--top-k", process.env.SECAGENT_KG_TOP_K);
    // Retrieval must never block or crash the turn: a timeout or non-zero exit -> "".
    execFile(
      "secagent",
      args,
      { maxBuffer: 8 * 1024 * 1024, timeout: 10_000 },
      (err, stdout) => resolve(err ? "" : (stdout || "").trim()),
    );
  });
}

export default function (pi: ExtensionAPI): void {
  pi.on("before_agent_start", async (event) => {
    const facts = await recall(event.prompt);
    if (!facts || facts === NO_MATCHES) return; // honest empty: add nothing
    // Assert the facts as authoritative — a weaker model otherwise sometimes contradicts
    // injected context (e.g. denies a symbol exists). These come from deterministic static
    // analysis of THIS repo, so they are ground truth for what exists and what calls what.
    const block =
      "## Knowledge graph — verified facts about THIS repository\n" +
      "The following relationships were extracted by deterministic static analysis and are " +
      "ground truth. Treat them as authoritative and prefer them over guessing. Do NOT claim " +
      "a listed symbol is undefined, and do NOT claim a listed relationship is absent — if a " +
      "`calls` fact is listed for a symbol, that call exists (these facts may be incomplete, " +
      "but every one shown is real). `A --[calls]--> B` means A calls B; " +
      "`X --[defined_in]--> file` means X is defined in that file.\n\n" +
      facts;
    return { systemPrompt: `${event.systemPrompt}\n\n${block}` };
  });
}
