/**
 * secagent-loopbreak — break identical-tool-call repetition loops.
 *
 * Observed live (gemma-4-26B, temp-0 greedy, long agent loop): the model re-emits
 * the IDENTICAL tool call after seeing its result — same command, same result,
 * same command — indefinitely, until the context window fills and the session
 * dies a silent length-death. Sampling-level fixes underreach: a mild
 * repetition_penalty (1.1) demonstrably did not break the attractor, because the
 * loop is *result-identity* driven — every turn appends the same
 * (call, result) pair, so the greedy continuation stays the same.
 *
 * This extension breaks the attractor at the only reliable point: the tool
 * boundary. It tracks consecutive identical calls (same tool, same JSON input);
 * the FIRST immediate repeat is allowed (a legitimate retry after a transient
 * failure is identical by nature), and from the SECOND repeat on the call is
 * blocked with a reason the model reads INSTEAD of the familiar result — new
 * text in context is what can snap a greedy continuation out of the loop.
 * The reason text is deliberately VARIED per block, and after MAX_BLOCKS the
 * session is terminated outright: observed live, a model can loop on the block
 * itself when every block injects identical text (890 calls / 422 blocks / a
 * ground-down context), and a fast visible kill beats that every time.
 *
 * Mirrors pi/extensions/secagent-kg.ts: minimal structural typing (pi injects
 * the real ExtensionAPI at runtime). Attach with `-e pi/extensions/secagent-loopbreak.ts`.
 */

// Structural subset of pi's ExtensionAPI — just the one hook this extension uses.
interface ToolCallEvent {
  toolName: string;
  input: unknown;
}
interface ToolCallResult {
  block?: boolean;
  reason?: string;
  terminate?: boolean;
}
interface ExtensionAPI {
  on(
    event: "tool_call",
    handler: (
      event: ToolCallEvent,
      ctx: unknown,
    ) => Promise<ToolCallResult | void> | ToolCallResult | void,
  ): void;
}

/** Repeats of one identical call tolerated before blocking: the original plus one
 * retry. The third identical consecutive call is where a retry stops being a retry
 * and starts being an attractor. */
const FREE_REPEATS = 2;

/** Blocks of one identical call tolerated before terminating the session. */
const MAX_BLOCKS = 5;

/** Reason templates cycled per block so consecutive blocks never inject IDENTICAL
 * text — textual identity in context is precisely what feeds a greedy loop. */
const REASONS = [
  (n: number, tool: string) =>
    `LOOP DETECTED: ${n} consecutive identical ${tool} calls. The result has not ` +
    `changed. Reread the earlier result and take a DIFFERENT action (e.g. write ` +
    `the output file, or move to the next step).`,
  (n: number, tool: string) =>
    `STOP. You repeated the same ${tool} call ${n} times. It is now blocked. Your ` +
    `next message must NOT contain this call. Produce the deliverable with what ` +
    `you already know.`,
  (n: number, tool: string) =>
    `Blocked again (${n}x). New rule: no more ${tool} calls at all this turn. ` +
    `Write your final answer or the requested file(s) NOW using information ` +
    `already in this conversation.`,
];

export default function (pi: ExtensionAPI) {
  let lastKey = "";
  let runLength = 0;

  pi.on("tool_call", (event) => {
    let key: string;
    try {
      key = `${event.toolName} ${JSON.stringify(event.input)}`;
    } catch {
      return; // unserializable input — never let bookkeeping break a real call
    }
    if (key === lastKey) {
      runLength += 1;
    } else {
      lastKey = key;
      runLength = 1;
    }
    if (runLength <= FREE_REPEATS) return;

    const blocks = runLength - FREE_REPEATS;
    if (blocks >= MAX_BLOCKS) {
      // The model is looping on the block itself — end the session visibly rather
      // than let identical cycles grind the context to a silent length-death.
      return {
        block: true,
        terminate: true,
        reason:
          `LOOP UNRECOVERABLE: ${runLength} consecutive identical ${event.toolName} ` +
          `calls (${blocks} blocked). Terminating the session — see pi-guard's ` +
          `post-run report.`,
      };
    }
    return {
      block: true,
      reason: REASONS[(blocks - 1) % REASONS.length](runLength, event.toolName),
    };
  });
}
