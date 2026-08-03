"""Render rule-scan findings to a summary + Markdown report."""

from __future__ import annotations

from collections import Counter

from .models import ScanFinding


def summarize(findings: list[ScanFinding]) -> dict[str, object]:
    by_sev = Counter(f.severity for f in findings)
    by_cat = Counter(f.category for f in findings)
    by_rule = Counter(f.rule_id for f in findings)
    return {
        "total": len(findings),
        "by_severity": dict(by_sev),
        "by_category": dict(by_cat),
        "by_rule": dict(by_rule),
        "critical": by_sev.get("critical", 0),
        "high": by_sev.get("high", 0),
    }


def render_markdown(
    findings: list[ScanFinding], *, project: str, ruleset_name: str,
    summary: dict[str, object], banner: str = "",
) -> str:
    lines: list[str] = []
    if banner:
        lines += [f"**{banner}**", ""]
    lines += [f"# Memory & stability scan — {project}", "",
              f"Rule set: `{ruleset_name}`.", ""]
    # An incomplete scan must never read as an all-clear. This is a memory-safety tool:
    # "no findings" when the analysis never ran is its most dangerous possible output.
    raw_failed = summary.get("files_failed", 0)
    failed = int(raw_failed) if isinstance(raw_failed, (int, str)) else 0
    raw_partial = summary.get("files_partial", 0)
    partial = int(raw_partial) if isinstance(raw_partial, (int, str)) else 0
    if partial:
        # A file the model saw only part of is reported as analyzed, so a clean result
        # for it covers only the bytes that were sent. Same danger as an outright
        # failure, and easier to miss because nothing errored.
        lines += [
            f"> ⚠️ **PARTIAL COVERAGE — {partial} file(s) were only partly analysed.**",
            "> Either the file was truncated before analysis, or some rule groups were "
            "never applied to it. Findings for those files cover only what was "
            "examined. See `partial` in `scan.json` for which, and why.",
            "",
        ]
    raw_cpp = summary.get("files_cpp", 0)
    cpp = int(raw_cpp) if isinstance(raw_cpp, (int, str)) else 0
    if cpp:
        # Measured capability, stated where a reader will see it. The profile accepts
        # C++ because refusing it would report zero eligible files and print "no
        # findings" over an unexamined codebase — a silent all-clear, which is worse
        # than a bad answer. So the answer is given, with what it is worth attached.
        lines += [
            f"> ⚠️ **C++ IS NOT RECOMMENDED — {cpp} C++ file(s) in this scan.**",
            "> Measured on PX4 GNSS parsers: **24% of findings are real defects** (0% "
            "before the MEM-002/ISR-002 rule fixes). Precision improved; recall did not "
            "— a known defect surfaced in roughly **1 run in 3**, and run frequency does "
            "not distinguish a real defect from a safe one. Treat findings below as "
            "leads to verify, and their **absence as no evidence at all**. "
            "See `quality/PRECISION_CPP_POSTFIX.md`.",
            "",
        ]
    raw_skipped = summary.get("files_skipped_by_cap", 0)
    skipped = int(raw_skipped) if isinstance(raw_skipped, (int, str)) else 0
    if skipped:
        lines += [
            f"> ⚠️ **BOUNDED SCAN — {skipped} eligible file(s) were not scanned** "
            f"(`scan.max_files`).",
            "> They were never examined, so nothing below says anything about them.",
            "",
        ]
    if failed:
        lines += [
            f"> ⚠️ **INCOMPLETE SCAN — {failed} file(s) could not be analyzed.**",
            "> The finding count below is a LOWER BOUND and does **not** mean those "
            "files are clean; they were never examined. See `failures` in `scan.json`.",
            "",
        ]
    if not findings:
        lines += [
            "No findings against the configured rules."
            if not (failed or partial or skipped)
            else "No findings in the source that was actually examined — but see the "
                 "warning(s) above; this is a lower bound, not an all-clear.",
            "",
        ]
        if banner:
            lines += ["", f"**{banner}**"]
        return "\n".join(lines)

    lines += [
        f"**{summary.get('total', 0)}** findings — "
        f"{summary.get('critical', 0)} critical, {summary.get('high', 0)} high.",
        "",
        "| severity | count |",
        "|----------|-------|",
    ]
    by_sev = summary.get("by_severity", {})
    if isinstance(by_sev, dict):
        for sev in ("critical", "high", "medium", "low", "info"):
            if by_sev.get(sev):
                lines.append(f"| {sev} | {by_sev[sev]} |")
    lines.append("")

    for f in sorted(findings, key=lambda x: x.sort_key()):
        loc = f"{f.file}:{f.line}" if f.line else f.file
        comp = f" _(component: {f.component})_" if f.component else ""
        lines.append(f"### `{loc}` — {f.severity.upper()} [{f.rule_id}]{comp}")
        lines.append("")
        lines.append(f.message or f.rule_id)
        if f.file_purpose:
            lines.append("")
            lines.append(f"> File purpose: {f.file_purpose}")
        lines.append("")

    if banner:
        lines += ["", "----", "", f"**{banner}**"]
    return "\n".join(lines)
