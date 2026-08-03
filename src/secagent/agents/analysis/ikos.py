"""Invoke IKOS and parse its findings.

IKOS (NASA-SW-VnV/ikos) analyzes C/C++ and writes a report. We drive it with
``--format=json`` and parse the result. Because IKOS's JSON field names are not
contractually documented and vary by version, the parser is deliberately tolerant
(multiple key fallbacks) and an **ingest mode** lets secagent consume a report produced
elsewhere — which is also how the use case is tested without the native toolchain.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .models import Finding, severity_for

log = logging.getLogger(__name__)

# clang / llvm-nm binaries used to enumerate a library TU's functions. The analysis image
# ships versioned names (clang-14, llvm-nm-14); prefer an unversioned one if present.
_CLANG_CANDIDATES = ("clang", "clang-18", "clang-17", "clang-16", "clang-15", "clang-14")
_NM_CANDIDATES = ("llvm-nm", "llvm-nm-18", "llvm-nm-17", "llvm-nm-16", "llvm-nm-15",
                  "llvm-nm-14")

# Common IKOS analysis (checker) codes -> human labels, for nicer reports.
CHECKER_LABELS = {
    "boa": "buffer overflow",
    "dbz": "division by zero",
    "nullity": "null pointer dereference",
    "uva": "uninitialized variable",
    "sio": "signed integer overflow",
    "uio": "unsigned integer overflow",
    "shc": "invalid shift",
    "poa": "pointer overflow",
    "prover": "assertion",
    "dfa": "dead code",
    "fca": "function call",
}


def ikos_available() -> tuple[bool, str]:
    binary = shutil.which("ikos")
    return (bool(binary), binary or "ikos not found on PATH")


def compile_flags_for(compile_db: str | Path, target: str | Path) -> list[str]:
    """The ``-I``/``-D``/``-U`` flags for ``target`` from a ``compile_commands.json``.

    cFS-style code won't compile without its include/define flags; this looks them up so
    ``secagent analyze run`` can analyze a file the way its build compiles it.
    """
    try:
        db = json.loads(Path(compile_db).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tgt = str(Path(target).resolve())
    for e in db if isinstance(db, list) else []:
        f = str(e.get("file", ""))
        cand = f if Path(f).is_absolute() else str(Path(e.get("directory", ""), f))
        if str(Path(cand).resolve()) == tgt or f == str(target):
            args = e.get("arguments") or shlex.split(e.get("command", ""))
            return [a for a in args if a.startswith(("-I", "-D", "-U"))]
    return []


_CPP_EXTS = (".c", ".cc", ".cpp", ".cxx", ".c++")


def iter_compile_units(compile_db: str | Path):
    """Yield ``(absolute_file, [-I/-D/-U flags])`` for each C/C++ TU in a compile database.

    Deduplicates by resolved path (a file compiled for several targets appears once), so a
    whole-repo scan analyzes each source once.
    """
    try:
        db = json.loads(Path(compile_db).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    seen: set[str] = set()
    for e in db if isinstance(db, list) else []:
        f = str(e.get("file", ""))
        if not f.lower().endswith(_CPP_EXTS):
            continue
        path = f if Path(f).is_absolute() else str(Path(e.get("directory", ""), f))
        rp = str(Path(path).resolve())
        if rp in seen:
            continue
        seen.add(rp)
        args = e.get("arguments") or shlex.split(e.get("command", ""))
        yield rp, [a for a in args if a.startswith(("-I", "-D", "-U"))]


def enumerate_functions(target: str | Path, compile_flags: list[str] | None = None) -> list[str]:
    """Defined function names in a C/C++ TU, for use as IKOS entry points.

    cFS apps are libraries (no ``main``), so IKOS needs the functions listed explicitly.
    Compiles the TU to bitcode and reads its symbol table; returns ``[]`` if clang/llvm-nm
    aren't available (caller then falls back to IKOS's default ``main`` entry point).
    """
    clang = next((b for b in _CLANG_CANDIDATES if shutil.which(b)), "")
    nm = next((b for b in _NM_CANDIDATES if shutil.which(b)), "")
    if not (clang and nm):
        return []
    with tempfile.TemporaryDirectory() as td:
        bc = Path(td) / "tu.bc"
        cc = subprocess.run(  # noqa: S603
            [clang, "-g", "-c", "-emit-llvm", *(compile_flags or []), str(target), "-o", str(bc)],
            capture_output=True, text=True, check=False)
        if cc.returncode != 0 or not bc.exists():
            return []
        out = subprocess.run([nm, "--defined-only", str(bc)],  # noqa: S603
                             capture_output=True, text=True, check=False)
    entry_points, aliased = defined_function_symbols(out.stdout)
    if aliased:
        # Disclosed, not silent: this changes which names IKOS is asked to analyse, and a
        # reader comparing two runs needs to know why a symbol they expected is absent.
        log.info(
            "analysis: %d complete-object constructor/destructor symbol(s) are Clang "
            "aliases to their base-object siblings, which carry the body; IKOS cannot "
            "resolve an alias as an entry point and refuses the whole file if given one. "
            "Using the targets instead. First: %s",
            len(aliased), ", ".join(aliased[:3]))
    return entry_points


# Symbol classes `llvm-nm` reports for defined CODE. `T`/`t` are strong text symbols;
# `W`/`w` are weak ones — C++ implicit and `= default` special members, inline functions,
# template instantiations. Data symbols (`D`, `B`, `R`, `V`, ...) are not functions and
# were never entry points.
#
# Weak symbols USED to be held back here, on the theory that they might exist in our
# bitcode and not in the one IKOS builds from the same source, and that handing IKOS an
# entry point it cannot find aborts the whole run. The abort is real. The rest was
# reasoned, not observed — written on a machine with no clang, llvm-nm or ikos on it —
# and it is now measured and false. With IKOS 3.4 and clang-14:
#
#   * A TU with a `= default` ctor, an inline member and a template instantiation emits
#     `_ZNK5Thing5twiceEv` and `_Z5addupIiET_S0_S0_` as `W`. Passing BOTH to `ikos -e`
#     exits 0 and returns real findings inside them. IKOS does not refuse weak symbols.
#   * Across the three analysable TUs of the real PX4 GPS drivers (crc, unicore, rtcm),
#     `llvm-nm` reports ZERO weak symbols. The filter never fired on the corpus whose
#     failure motivated it.
#
# So it bought nothing and cost the coverage of every inline and template function in a
# header. It is gone. The abort it was meant to prevent has a different cause, below.
_FUNCTION_TEXT = ("T", "t", "W", "w")

# Itanium ABI complete-object ctor/dtor markers, and the base-object sibling that holds
# the actual body. Clang's `-mconstructor-aliases`/`-mdestructor-aliases` (ON BY DEFAULT)
# emit `C1`/`D1` as an LLVM `GlobalAlias` to `C2`/`D2` whenever a class has no virtual
# bases. `llvm-nm` prints an alias exactly like a strong function — `T`, indistinguishable
# — but IKOS's entry-point resolver walks real functions only, so it refuses the name and
# aborts the ENTIRE translation unit before analysing any of the others.
#
# Measured on the real PX4 `rtcm.cpp` with IKOS 3.4. `RTCMParsing` has no virtual bases,
# so `llvm-dis` shows `@_ZN11RTCMParsingC1Ev = ... alias ... @_ZN11RTCMParsingC2Ev`:
#
#   entry points as shipped (C1/D1 included) -> exit 9, "could not find function", 0 findings
#   the same list with C1->C2 and D1->D2     -> exit 0, 116 findings
#
# Every stateful C++ class in a codebase hits this, so the cost was 100% of a file's
# findings, silently, on any class with a non-trivial constructor.
#
# `C3` (allocating ctor) and `D0` (deleting dtor) are deliberately NOT remapped: `D0` is a
# real function body rather than an alias, and neither was observed aliasing in the
# measured output. Adding them would be reasoning, which is what produced the last two
# wrong answers here.
_COMPLETE_OBJECT_ALIASES = {"C1": "C2", "D1": "D2"}


def _base_object_sibling(name: str, defined: set[str]) -> str | None:
    """The base-object ctor/dtor ``name`` aliases, when that sibling is in this TU.

    Substitution is confirmed against the symbol table rather than parsed out of the
    mangling: a candidate is only accepted when it is itself a defined symbol here, so a
    class whose *name* happens to contain ``C1`` cannot be misread into a false match.
    """
    if not name.startswith("_Z"):
        return None                      # not Itanium-mangled: C has no ctor aliases
    for marker, base in _COMPLETE_OBJECT_ALIASES.items():
        start = 0
        while (i := name.find(marker, start)) >= 0:
            candidate = f"{name[:i]}{base}{name[i + len(marker):]}"
            if candidate != name and candidate in defined:
                return candidate
            start = i + 1
    return None


def defined_function_symbols(nm_output: str) -> tuple[list[str], list[str]]:
    """Parse ``llvm-nm --defined-only`` output into ``(entry_points, aliases_resolved)``.

    Split out from the subprocess plumbing so the classification can be tested against
    real ``llvm-nm`` output without a toolchain.

    The second element names the complete-object ctor/dtor symbols dropped in favour of
    the base-object siblings that carry their bodies — reported, not silent, because it
    changes which names IKOS is asked to analyse.
    """
    functions: list[str] = []
    for line in nm_output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        cls, name = parts[-2], parts[-1]
        if cls in _FUNCTION_TEXT:
            functions.append(name)
    defined = set(functions)
    entry_points, aliased = [], []
    for name in functions:
        if _base_object_sibling(name, defined):
            aliased.append(name)
        else:
            entry_points.append(name)
    return entry_points, aliased


# Compiler flags whose value is a path, in both the joined (-Ifoo) and split (-I foo)
# spellings IKOS accepts.
_PATH_FLAG_PREFIXES = ("-I", "-isystem", "-iquote", "-idirafter", "-F")


def _abs_flag(flag: str) -> str:
    """Absolutise a path-carrying compiler flag; leave everything else untouched."""
    for prefix in _PATH_FLAG_PREFIXES:
        if flag.startswith(prefix) and len(flag) > len(prefix):
            value = flag[len(prefix):]
            if value.startswith("="):          # -I=path
                return flag
            path = Path(value)
            return f"{prefix}{path.resolve()}" if not path.is_absolute() else flag
    return flag


def run_ikos(
    target: str | Path,
    *,
    report_path: str | Path,
    status_filter: str = "error,warning",
    analyses_filter: str = "",
    domain: str = "",
    procedural: str = "",
    no_pointer: bool = False,
    compile_flags: list[str] | None = None,
    entry_points: list[str] | None = None,
    timeout: int = 1800,
) -> Path:
    """Run IKOS on ``target`` (a .c/.cpp/.bc file) and write a SARIF report.

    ``domain``/``procedural``/``no_pointer`` are the speed/precision knobs; ``compile_flags``
    are ``-I``/``-D`` passed to IKOS's compiler; ``entry_points`` list the functions to
    analyze (needed for a library TU with no ``main``). Raises ``RuntimeError`` if IKOS is
    unavailable or the run fails.
    """
    available, info = ikos_available()
    if not available:
        raise RuntimeError(
            f"IKOS is not installed ({info}). Install ikos, or use ingest mode "
            "(`secagent analyze ingest <repo> <ikos-report.json>`)."
        )
    report_path = Path(report_path)
    # SARIF (2.1.0) is a stable, standard format with inline file/line/message/level —
    # far more robust than IKOS's raw `--format=json`, whose fields are undocumented and
    # normalized (results reference statements/files by id) and vary by version.
    # Give IKOS a report-specific analysis DB via -o (it defaults to ./output.db); without
    # this, a parallel scan's workers share one output.db and corrupt each other's SQLite.
    cmd = ["ikos", "--format=sarif", f"--report-file={report_path}",
           "-o", str(report_path.with_suffix(".db"))]
    if status_filter:
        cmd.append(f"--status-filter={status_filter}")
    if analyses_filter:
        cmd.append(f"--analyses-filter={analyses_filter}")
    if domain:
        cmd += ["-d", domain]
    if procedural:
        cmd.append(f"--proc={procedural}")
    if no_pointer:
        cmd.append("--no-pointer")
    for ep in entry_points or []:
        cmd += ["-e", ep]
    # Anything path-like must be made absolute BEFORE we change directory below. A
    # relative target — `analyze run . src/crc.cpp`, the form the docs use — was passed
    # through unchanged and then resolved against the output directory, so IKOS reported
    # `no such file or directory: src/crc.cpp` for a file plainly present in the user's
    # working directory. Include-style flags fail the same way and just as invisibly:
    # a dropped -I turns into "missing header", not "bad path".
    cmd += [_abs_flag(f) for f in (compile_flags or [])]
    cmd.append(str(Path(target).resolve()))
    # IKOS writes its analysis database (output.db) into the current directory, so run
    # from a writable dir (the report's output dir) rather than secagent's CWD.
    workdir = report_path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,  # noqa: S603
                              check=False, cwd=str(workdir))
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"IKOS invocation failed: {exc}") from exc
    if not report_path.exists() or report_path.stat().st_size == 0:
        raise RuntimeError(
            f"IKOS produced no report (exit {proc.returncode}): {proc.stderr[-500:]}"
        )
    return report_path


def parse_ikos_report(data: str | bytes | dict[str, Any] | list[Any]) -> list[Finding]:
    """Parse IKOS JSON (string, bytes, or already-decoded) into findings.

    Tolerant of shape: a top-level list, or a dict keyed by results/findings/reports/
    checks; and of per-item key naming (file/filename/location.file, etc.).
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid IKOS JSON: {exc}") from exc

    # SARIF (2.1.0) — the format `run_ikos` now requests, and the common interchange
    # format for ingested reports. Detected by its ``runs`` container.
    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        return parse_sarif(data)

    items = _extract_items(data)
    findings: list[Finding] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        status = str(_first(it, "status", "result", "kind") or "warning").lower()
        if status in ("ok", "safe"):
            continue  # don't report proven-safe statements
        loc_raw = it.get("location")
        loc: dict[str, Any] = loc_raw if isinstance(loc_raw, dict) else {}
        file = str(_first(it, "file", "filename", "path") or loc.get("file") or "?")
        line = _to_int(_first(it, "line", "lineno") or loc.get("line"))
        column = _to_int(_first(it, "column", "col") or loc.get("column"))
        checker = str(_first(it, "checker", "check", "analysis", "kind") or "ikos")
        message = str(_first(it, "message", "msg", "description", "text") or "").strip()
        function = str(_first(it, "function", "fun", "func") or "")
        findings.append(
            Finding(
                checker=checker, status=status, severity=severity_for(status),
                file=_norm_path(file), line=line, column=column,
                message=message or CHECKER_LABELS.get(checker, checker), function=function,
            )
        )
    return findings


def parse_sarif(data: dict[str, Any]) -> list[Finding]:
    """Parse an IKOS SARIF (2.1.0) document into findings.

    Each ``runs[].results[]`` carries an inline ``ruleId``, ``level`` (error/warning/note),
    a ``message.text``, and a physical location (file/line/column) — no id resolution
    needed, unlike IKOS's raw ``--format=json``. The enclosing function (when IKOS reports
    innermost CALLER (not the containing function) comes from ``stacks[]``; see
    ``_caller_from_stacks``. The containing function is resolved later, from the
    symbol index, in ``agents/analysis/agent.py::_enrich``.
    """
    findings: list[Finding] = []
    for run in data.get("runs", []):
        if not isinstance(run, dict):
            continue
        for res in run.get("results", []):
            if not isinstance(res, dict):
                continue
            level = str(res.get("level") or "warning").lower()
            if level in ("none", "ok", "safe"):
                continue
            rule = str(res.get("ruleId") or "ikos")
            msg = res.get("message")
            text = str(msg.get("text") if isinstance(msg, dict) else "").strip()
            file, line, column = "?", 0, 0
            locs = res.get("locations") or []
            if locs and isinstance(locs[0], dict):
                phys = locs[0].get("physicalLocation") or {}
                art = phys.get("artifactLocation") or {}
                file = _norm_path(str(art.get("uri") or "?"))
                region = phys.get("region") or {}
                line = _to_int(region.get("startLine"))
                column = _to_int(region.get("startColumn"))
            findings.append(
                Finding(
                    checker=rule, status=level, severity=severity_for(level),
                    file=file, line=line, column=column,
                    message=text or CHECKER_LABELS.get(rule, rule),
                    called_from=_caller_from_stacks(res),
                )
            )
    return findings


_STACK_FRAME_PREFIX = "Call from "


def _caller_from_stacks(res: dict[str, Any]) -> str:
    """The innermost enclosing function for a SARIF result, from its ``stacks[]``.

    IKOS's ``stacks[]`` encode the call path(s) TO the defect. Verified against 1305
    real results (1608 stack entries) in IKOS SARIF output produced against actual cFS
    and C++ sources (``cfs-build/scan-out/ikos/*.sarif``,
    ``longrun/*/analysis_scan/ikos/*.sarif``): ``stacks[i].frames[0]`` is always the
    innermost frame — the function that directly contains the flagged statement —
    with ``frames[1:]`` walking outward through callers (confirmed by a defect reached
    through three different call chains, all sharing the same ``frames[0]``). Taking the
    *last* frame instead would attribute every finding to its outermost caller — often
    the analysis entry point (``main``) — which is exactly backwards.

    IKOS spells the frame's function as ``frame.location.message.text`` = ``"Call from
    <function>"`` (a bare identifier for C, a demangled signature like
    ``Foo::bar(int)`` for C++). That is the only spelling observed in 1608 real stack
    frames — no ``logicalLocations`` or ``module`` field appears anywhere in that
    corpus — so no other shape is guessed at here. 814 of the 1305 real results carry
    no ``stacks`` at all; those (and any malformed ``stacks``) yield "" rather than a
    guess derived from the nearest symbol or file name.
    """
    stacks = res.get("stacks")
    if not isinstance(stacks, list):
        return ""
    for stack in stacks:
        if not isinstance(stack, dict):
            continue
        frames = stack.get("frames")
        if not isinstance(frames, list) or not frames:
            continue
        frame = frames[0]
        if not isinstance(frame, dict):
            continue
        loc = frame.get("location")
        if not isinstance(loc, dict):
            continue
        msg = loc.get("message")
        text = msg.get("text") if isinstance(msg, dict) else None
        if not isinstance(text, str):
            continue
        text = text.strip()
        if text.startswith(_STACK_FRAME_PREFIX):
            text = text[len(_STACK_FRAME_PREFIX):].strip()
        if text:
            return text
    return ""


def _extract_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "findings", "reports", "checks", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")
