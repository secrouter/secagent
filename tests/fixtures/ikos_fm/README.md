# Real IKOS output, and the source it describes

Both files are real, and they are here together on purpose: the defect they pin could only
be caught by checking one against the other.

- `fm_cmd_utils.c` — verbatim from NASA cFS' File Manager app
  (`cFS/apps/fm/fsw/src/fm_cmd_utils.c`), unmodified.
- `ikos-report.sarif` — real IKOS 3.4 SARIF produced by `secagent analyze run . 
  fsw/src/fm_cmd_utils.c --library --compile-db ... --no-llm` against that exact file,
  during the UC3 C re-test.

The SARIF is a **subset** of the 145-result original: results whose location is another
file were dropped, and of the 129 results with no `stacks[]` only 6 were kept. Every
result that remains is byte-identical to what IKOS emitted — nothing was edited, and all
10 results that carry `stacks[]` are present, because those are the ones the attribution
bug turns on.

## Why not a synthetic fixture

`Finding.function` was populated from SARIF `stacks[]` on the reading that `frames[0]` is
"the function that directly contains the flagged statement". A hand-written fixture would
have encoded that same reading and passed. Against the real pair, it does not: the frames
are spelled `"Call from <f>"`, so `frames[0]` names a **caller**, and the containing
function does not appear in the stack at all.

Ground truth, read from `fm_cmd_utils.c` itself:

| finding lines | contained by | reported by `frames[0]` |
|---|---|---|
| 147, 149, 166, 180 | `FM_GetFilenameState` (135-219) | `FM_VerifyFileState` |
| 191, 192, 193 | `FM_GetFilenameState` (135-219) | `FM_VerifyNameValid` |
| 346, 347 | `FM_VerifyFileState` (247-363) | `FM_VerifyDirExists` |

Ten of ten wrong, all one level out — each named function is a real caller of the real
containing function.
