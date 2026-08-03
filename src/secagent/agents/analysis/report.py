"""Render static-analysis findings to a summary + Markdown report."""

from __future__ import annotations

from collections import Counter

from .models import Finding


def summarize(findings: list[Finding]) -> dict[str, object]:
    by_sev = Counter(f.severity for f in findings)
    by_checker = Counter(f.checker for f in findings)
    return {
        "total": len(findings),
        "by_severity": dict(by_sev),
        "by_checker": dict(by_checker),
        "high": by_sev.get("high", 0),
        "medium": by_sev.get("medium", 0),
    }


def render_markdown(
    findings: list[Finding], *, project: str, summary: dict[str, object], banner: str = ""
) -> str:
    lines: list[str] = []
    if banner:
        lines += [f"**{banner}**", ""]
    lines += [f"# Static analysis (IKOS) — {project}", ""]
    if not findings:
        lines += ["No findings. IKOS reported no errors or warnings.", ""]
        if banner:
            lines += ["", f"**{banner}**"]
        return "\n".join(lines)

    lines += [
        f"**{summary.get('total', 0)}** findings — "
        f"{summary.get('high', 0)} high, {summary.get('medium', 0)} medium.",
        "",
        "| severity | count |",
        "|----------|-------|",
    ]
    by_sev = summary.get("by_severity", {})
    if isinstance(by_sev, dict):
        for sev in ("high", "medium", "low", "info"):
            if by_sev.get(sev):
                lines.append(f"| {sev} | {by_sev[sev]} |")
    lines.append("")

    # Group by file, ordered by worst severity then path.
    for f in sorted(findings, key=lambda x: x.sort_key()):
        loc = f"{f.file}:{f.line}" + (f":{f.column}" if f.column else "")
        comp = f" _(component: {f.component})_" if f.component else ""
        lines.append(f"### `{loc}` — {f.severity.upper()} [{f.checker}]{comp}")
        lines.append("")
        lines.append(f"{f.message}" + (f" (in `{f.function}`)" if f.function else ""))
        if f.file_purpose:
            lines.append("")
            lines.append(f"> File purpose: {f.file_purpose}")
        if f.triage:
            verdict = f.triage.get("assessment") or f.triage.get("summary")
            if verdict:
                lines.append("")
                lines.append(f"**Triage:** {verdict}")
        lines.append("")

    if banner:
        lines += ["", "----", "", f"**{banner}**"]
    return "\n".join(lines)
