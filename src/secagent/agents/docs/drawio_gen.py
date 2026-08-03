"""Generate architecture diagrams from the affordance IO map.

The diagrams are derived directly from the IO graph, so they accurately reflect the
detected architecture rather than depending on a model to draw it correctly. The same
:class:`Diagram` model is rendered two ways:

* ``.drawio`` (``mxfile``) XML — opens in the draw.io editor for manual touch-ups and
  is the input for the faithful "drawio"/"chromium" render backends.
* SVG, directly, by :mod:`secagent.agents.docs.svg_render` — the default backend, which
  needs no external binary.

Both share :func:`layout` so the two renderings agree on geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from ...affordances.models import Component, IOEdge

# Per-group visual metadata, shared by the drawio style string and the SVG renderer so
# the two backends stay visually consistent. ``shape`` drives the SVG geometry.
GROUP_STYLE: dict[str, dict[str, str]] = {
    "component": {
        "drawio": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
        "fill": "#dae8fc", "stroke": "#6c8ebf", "shape": "rounded",
    },
    "datastore": {
        "drawio": "shape=cylinder3;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
        "fill": "#d5e8d4", "stroke": "#82b366", "shape": "cylinder",
    },
    "external": {
        "drawio": "rounded=0;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;",
        "fill": "#ffe6cc", "stroke": "#d79b00", "shape": "rect",
    },
    "endpoint": {
        "drawio": "shape=hexagon;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;",
        "fill": "#fff2cc", "stroke": "#d6b656", "shape": "hexagon",
    },
    "env": {
        "drawio": "shape=note;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;",
        "fill": "#f8cecc", "stroke": "#b85450", "shape": "note",
    },
}

# Layout constants (shared by drawio XML and SVG).
NODE_W = 180
NODE_H = 60
COL_ORDER = ["env", "component", "endpoint", "datastore", "external"]
COL_GAP = 240
ROW_GAP = 90
MARGIN = 40


@dataclass
class _Node:
    id: str
    label: str
    group: str


@dataclass
class _Edge:
    src: str
    dst: str
    label: str = ""


@dataclass
class Diagram:
    name: str
    nodes: list[_Node] = field(default_factory=list)
    edges: list[_Edge] = field(default_factory=list)


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


def _safe_id(raw: str) -> str:
    return "n_" + "".join(c if c.isalnum() else "_" for c in raw)


def _build_component_diagram(components: list[Component], edges: list[IOEdge]) -> Diagram:
    """Internal component dependency graph (import edges only).

    KNOWN GAP, recorded rather than fixed, and smaller than it looks. On a C/C++ repo both
    diagrams come out with **zero edges** — measured on the PX4 GPS drivers: three boxes,
    no connecting lines, so a reader concludes `src`, `(root)` and `tools` are unrelated
    when `src` is the entire system. The cause is upstream: ``FileSummary.imports`` is
    populated only by ``python_imports`` and ``rust_imports``; every other language gets
    ``imports = []``, so there is no ``kind == "import"`` edge for this filter to draw.
    C/C++ ``#include`` resolution does not exist in the IO map.

    The part nobody had reached: **the relationship data does exist**, from a different
    source. The clang call map on that same repo carries 36 inter-file edges and 139
    intra-file ones, spot-checked correct — `ashtech.cpp -> rtcm.cpp`, `nmea.cpp ->
    unicore.cpp`, `ubx.cpp -> gps_helper.cpp` — and `callmap.rst` renders them. So the
    diagram is blind to structure the same build already knows. Aggregating call edges to
    the component level is a far smaller job than writing an `#include` resolver, which is
    what this looked like when we thought there was no data at all.
    """
    d = Diagram("Component Dependencies")
    names = {c.name for c in components}
    for c in components:
        d.nodes.append(_Node(_safe_id(c.name), f"{c.name}\n({c.language})", "component"))
    seen = set()
    for e in edges:
        if e.kind != "import" or e.src not in names or e.dst not in names:
            continue
        key = (e.src, e.dst)
        if key in seen:
            continue
        seen.add(key)
        d.edges.append(_Edge(_safe_id(e.src), _safe_id(e.dst)))
    return d


def _build_io_diagram(components: list[Component], edges: list[IOEdge]) -> Diagram:
    """System IO diagram: components plus externals (datastores, calls, endpoints)."""
    d = Diagram("System IO Overview")
    comp_names = {c.name for c in components}
    added: dict[str, _Node] = {}

    def ensure(label: str, group: str) -> str:
        nid = _safe_id(group + "_" + label)
        if nid not in added:
            added[nid] = _Node(nid, label, group)
            d.nodes.append(added[nid])
        return nid

    for c in components:
        ensure(c.name, "component")

    for e in edges:
        if e.kind == "import" and e.src in comp_names and e.dst in comp_names:
            d.edges.append(_Edge(_safe_id("component_" + e.src), _safe_id("component_" + e.dst)))
        elif e.kind == "datastore":
            dst = ensure(e.dst, "datastore")
            d.edges.append(_Edge(_safe_id("component_" + e.src), dst, "uses"))
        elif e.kind == "http_call":
            dst = ensure(e.dst, "external")
            d.edges.append(_Edge(_safe_id("component_" + e.src), dst, "calls"))
        elif e.kind == "http_endpoint":
            dst = ensure(e.detail or e.dst, "endpoint")
            d.edges.append(_Edge(_safe_id("component_" + e.src), dst, "exposes"))
    return d


def layout(d: Diagram) -> dict[str, Rect]:
    """Position nodes in columns grouped by kind, top to bottom.

    Shared by the drawio and SVG renderers so they agree on geometry.
    """
    col_x = {g: MARGIN + i * COL_GAP for i, g in enumerate(COL_ORDER)}
    col_count: dict[str, int] = {}
    rects: dict[str, Rect] = {}
    for n in d.nodes:
        idx = col_count.get(n.group, 0)
        col_count[n.group] = idx + 1
        x = col_x.get(n.group, MARGIN)
        y = MARGIN + idx * ROW_GAP
        rects[n.id] = Rect(x, y, NODE_W, NODE_H)
    return rects


def to_drawio_xml(d: Diagram) -> str:
    """Render a Diagram to ``mxfile`` (draw.io) XML."""
    rects = layout(d)
    cells: list[str] = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for n in d.nodes:
        r = rects[n.id]
        style = GROUP_STYLE.get(n.group, GROUP_STYLE["component"])["drawio"]
        label = escape(n.label).replace("\n", "&#10;")
        cells.append(
            f'<mxCell id="{n.id}" value="{label}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{r.x}" y="{r.y}" width="{r.w}" height="{r.h}" as="geometry"/></mxCell>'
        )
    for i, e in enumerate(d.edges):
        label = escape(e.label)
        cells.append(
            f'<mxCell id="e{i}" value="{label}" '
            f'style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;" '
            f'edge="1" parent="1" source="{e.src}" target="{e.dst}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    body = "".join(cells)
    name = escape(d.name)
    return (
        '<mxfile host="secagent" type="device">'
        f'<diagram id="{_safe_id(d.name)}" name="{name}">'
        '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" '
        'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageWidth="1169" '
        'pageHeight="826" math="0" shadow="0">'
        f"<root>{body}</root>"
        "</mxGraphModel></diagram></mxfile>"
    )


# --- Backwards-compatible helpers (return XML directly) --------------------------

def component_diagram(components: list[Component], edges: list[IOEdge]) -> str:
    return to_drawio_xml(_build_component_diagram(components, edges))


def io_diagram(components: list[Component], edges: list[IOEdge]) -> str:
    return to_drawio_xml(_build_io_diagram(components, edges))


# A diagram past these sizes is unreadable and produces a multi-megabyte SVG/drawio.
# Keep the most-connected nodes and bound the edges; note what was dropped.
_MAX_DIAGRAM_NODES = 40
_MAX_DIAGRAM_EDGES = 60


def _cap_diagram(d: Diagram) -> Diagram:
    """Trim a diagram to a legible size, keeping the most-connected nodes."""
    if len(d.nodes) <= _MAX_DIAGRAM_NODES and len(d.edges) <= _MAX_DIAGRAM_EDGES:
        return d
    degree: dict[str, int] = {}
    for e in d.edges:
        degree[e.src] = degree.get(e.src, 0) + 1
        degree[e.dst] = degree.get(e.dst, 0) + 1
    order = {n.id: i for i, n in enumerate(d.nodes)}
    ranked = sorted(d.nodes, key=lambda n: (-degree.get(n.id, 0), order[n.id]))
    kept_ids = {n.id for n in ranked[:_MAX_DIAGRAM_NODES]}
    kept_nodes = [n for n in d.nodes if n.id in kept_ids]  # stable original order
    kept_edges = [e for e in d.edges
                  if e.src in kept_ids and e.dst in kept_ids][:_MAX_DIAGRAM_EDGES]
    dropped_n = len(d.nodes) - len(kept_nodes)
    dropped_e = len(d.edges) - len(kept_edges)
    capped = Diagram(d.name, kept_nodes, kept_edges)
    if dropped_n or dropped_e:
        capped.nodes.append(_Node(
            _safe_id(d.name + "_more"),
            f"+{dropped_n} more node(s),\n{dropped_e} more edge(s) not shown", "note"))
    return capped


def build_diagrams(components: list[Component], edges: list[IOEdge]) -> dict[str, Diagram]:
    """Return the standard diagram set as :class:`Diagram` models, keyed by stem."""
    return {
        "components": _cap_diagram(_build_component_diagram(components, edges)),
        "system_io": _cap_diagram(_build_io_diagram(components, edges)),
    }


def generate_diagrams(components: list[Component], edges: list[IOEdge]) -> dict[str, str]:
    """Return ``{filename: drawio_xml}`` for the standard diagram set."""
    return {f"{stem}.drawio": to_drawio_xml(d)
            for stem, d in build_diagrams(components, edges).items()}
