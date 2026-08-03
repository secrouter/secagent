"""Render diagrams to SVG with a selectable backend.

Backends (see ``diagrams.renderer`` config):

* ``svg`` — render directly from secagent's diagram model (default). No binary, no X
  server, always available; the result is accurate but not pixel-identical to draw.io.
* ``chromium`` — faithful draw.io render via a headless Chromium (no X server). Needs
  a chromium binary and the draw.io viewer JS; falls back to ``svg`` if either is
  missing.
* ``drawio`` — faithful render via drawio-desktop + ``xvfb-run`` (heaviest, legacy);
  falls back to ``svg`` if the binary is missing.

Every backend writes ``<stem>.svg`` next to the ``<stem>.drawio`` source, so the docs
build embeds a static image and never needs a render step at Sphinx time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .drawio_gen import Diagram
from .svg_render import diagram_to_svg

# Candidate names for an installed Chromium/Chrome.
_CHROMIUM_NAMES = ("chromium", "chromium-browser", "chromium-headless-shell",
                   "headless-shell", "google-chrome", "chrome")
# Where the Dockerfile drops the draw.io viewer bundle when chromium support is built.
_DRAWIO_VIEWER_JS = os.environ.get(
    "SECAGENT_DRAWIO_VIEWER_JS", "/usr/share/secagent/drawio-viewer.min.js"
)


@dataclass
class RenderResult:
    rendered: list[str] = field(default_factory=list)   # .svg filenames produced
    skipped: list[str] = field(default_factory=list)
    backend: str = "svg"                                # backend that produced output
    reason: str = ""


def drawio_available() -> tuple[bool, str]:
    binary = shutil.which("drawio") or shutil.which("drawio-desktop")
    if not binary:
        return False, "drawio binary not found"
    return True, binary


def chromium_binary(configured: str = "") -> str | None:
    if configured:
        return configured if Path(configured).exists() else None
    for name in _CHROMIUM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def render_diagrams(
    diagrams: dict[str, Diagram],
    diagrams_dir: Path,
    *,
    renderer: str = "svg",
    chromium_path: str = "",
    timeout: int = 120,
) -> RenderResult:
    """Render each diagram in ``diagrams`` to ``<stem>.svg`` in ``diagrams_dir``.

    ``diagrams`` is keyed by stem; the matching ``<stem>.drawio`` is expected to exist
    already for the faithful backends. Returns a report; the ``svg`` default cannot
    fail, and faithful backends fall back to it.
    """
    result = RenderResult(backend=renderer)
    for stem, d in diagrams.items():
        svg_path = diagrams_dir / f"{stem}.svg"
        drawio_path = diagrams_dir / f"{stem}.drawio"
        ok = False
        if renderer == "drawio":
            ok = _render_drawio(drawio_path, svg_path, timeout)
        elif renderer == "chromium":
            ok = _render_chromium(drawio_path, svg_path, chromium_path, timeout)
        if not ok:
            svg_path.write_text(diagram_to_svg(d), encoding="utf-8")
            ok = True
            if renderer != "svg" and result.backend == renderer:
                result.backend = f"svg (fallback from {renderer})"
        result.rendered.append(svg_path.name)
    return result


def _render_drawio(drawio_path: Path, svg_path: Path, timeout: int) -> bool:
    available, info = drawio_available()
    if not available or not drawio_path.exists():
        return False
    cmd: list[str] = []
    if shutil.which("xvfb-run"):
        cmd += ["xvfb-run", "-a"]
    cmd += [info, "--no-sandbox", "-x", "-f", "svg", "-o", str(svg_path), str(drawio_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0 and svg_path.exists()


# Minimal HTML harness: the draw.io viewer renders the embedded diagram XML to SVG in
# the DOM, which headless Chromium's --dump-dom then emits.
_HARNESS = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="mxgraph" data-mxgraph='{config}'></div>
<script src="file://{viewer}"></script>
</body></html>"""


def _render_chromium(drawio_path: Path, svg_path: Path, configured: str, timeout: int) -> bool:
    chrome = chromium_binary(configured)
    viewer = Path(_DRAWIO_VIEWER_JS)
    if not chrome or not viewer.exists() or not drawio_path.exists():
        return False
    import json

    xml = drawio_path.read_text(encoding="utf-8")
    config = json.dumps({"xml": xml, "border": 8})
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.html"
        harness.write_text(_HARNESS.format(config=_attr_escape(config), viewer=viewer),
                           encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--virtual-time-budget=8000", "--run-all-compositor-stages-before-draw",
            "--dump-dom", f"file://{harness}",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,  # noqa: S603
                                  timeout=timeout, check=False)
        except (subprocess.SubprocessError, OSError):
            return False
    svg = _extract_svg(proc.stdout)
    if not svg:
        return False
    svg_path.write_text(svg, encoding="utf-8")
    return True


def _attr_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("'", "&#39;")


def _extract_svg(dom: str) -> str:
    start = dom.find("<svg")
    end = dom.rfind("</svg>")
    if start == -1 or end == -1:
        return ""
    return dom[start:end + len("</svg>")]
