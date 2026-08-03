"""Render a :class:`~secagent.agents.docs.drawio_gen.Diagram` directly to SVG.

This is the default diagram backend: it draws the same boxes/edges secagent already
lays out, with no external binary, browser, or X server — so it is FIPS-clean, fast,
and always available. It is not pixel-identical to the draw.io editor (which is what
the "chromium"/"drawio" backends are for), but it is accurate to the detected
architecture and self-contained.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from .drawio_gen import GROUP_STYLE, Diagram, Rect, layout

_FONT = (
    "font-family='-apple-system,Segoe UI,Helvetica,Arial,sans-serif' font-size='12'"
)


def diagram_to_svg(d: Diagram) -> str:
    """Render ``d`` to a standalone SVG document string."""
    rects = layout(d)
    width = (max((r.x + r.w for r in rects.values()), default=0)) + 40
    height = (max((r.y + r.h for r in rects.values()), default=0)) + 40

    parts: list[str] = [
        f"<?xml version='1.0' encoding='UTF-8'?>\n"
        f"<svg xmlns='http://www.w3.org/2000/svg' "
        f"viewBox='0 0 {width} {height}' width='{width}' height='{height}'>",
        "<defs>"
        "<marker id='arrow' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
        "markerHeight='7' orient='auto-start-reverse'>"
        "<path d='M0,0 L10,5 L0,10 z' fill='#4d4d4d'/></marker></defs>",
        f"<rect width='{width}' height='{height}' fill='#ffffff'/>",
    ]

    # Edges first, so nodes render on top of the lines.
    for e in d.edges:
        r1, r2 = rects.get(e.src), rects.get(e.dst)
        if r1 is None or r2 is None:
            continue
        parts.append(_edge_svg(r1, r2, e.label))

    for n in d.nodes:
        r = rects[n.id]
        style = GROUP_STYLE.get(n.group, GROUP_STYLE["component"])
        parts.append(_node_svg(r, n.label, style["shape"], style["fill"], style["stroke"]))

    parts.append("</svg>")
    return "".join(parts)


def _node_svg(r: Rect, label: str, shape: str, fill: str, stroke: str) -> str:
    x, y, w, h = r.x, r.y, r.w, r.h
    common = f"fill='{fill}' stroke='{stroke}' stroke-width='1.5'"
    if shape == "rounded":
        body = f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='8' ry='8' {common}/>"
    elif shape == "hexagon":
        ix = w * 0.18
        pts = (f"{x + ix},{y} {x + w - ix},{y} {x + w},{y + h / 2} "
               f"{x + w - ix},{y + h} {x + ix},{y + h} {x},{y + h / 2}")
        body = f"<polygon points='{pts}' {common}/>"
    elif shape == "cylinder":
        ry = 8
        body = (
            f"<path d='M{x},{y + ry} A{w / 2},{ry} 0 0 1 {x + w},{y + ry} "
            f"L{x + w},{y + h - ry} A{w / 2},{ry} 0 0 1 {x},{y + h - ry} Z' {common}/>"
            f"<path d='M{x},{y + ry} A{w / 2},{ry} 0 0 0 {x + w},{y + ry}' "
            f"fill='none' stroke='{stroke}' stroke-width='1.5'/>"
        )
    elif shape == "note":
        fold = 14
        pts = (f"{x},{y} {x + w - fold},{y} {x + w},{y + fold} "
               f"{x + w},{y + h} {x},{y + h}")
        body = (f"<polygon points='{pts}' {common}/>"
                f"<path d='M{x + w - fold},{y} L{x + w - fold},{y + fold} L{x + w},{y + fold}' "
                f"fill='none' stroke='{stroke}' stroke-width='1.5'/>")
    else:  # rect
        body = f"<rect x='{x}' y='{y}' width='{w}' height='{h}' {common}/>"
    return body + _label_svg(x + w / 2, y + h / 2, label)


def _label_svg(cx: float, cy: float, label: str) -> str:
    lines = label.split("\n")
    line_h = 14
    start = cy - (len(lines) - 1) * line_h / 2
    spans = "".join(
        f"<tspan x='{cx:.0f}' y='{start + i * line_h:.0f}'>{escape(line)}</tspan>"
        for i, line in enumerate(lines)
    )
    return (
        f"<text text-anchor='middle' dominant-baseline='middle' fill='#1a1a1a' "
        f"{_FONT}>{spans}</text>"
    )


def _edge_svg(r1: Rect, r2: Rect, label: str) -> str:
    cy1, cy2 = r1.y + r1.h / 2, r2.y + r2.h / 2
    if r1.x == r2.x:
        # Same column: route along the right side.
        x_out = r1.x + r1.w + 20
        sx, sy = r1.x + r1.w, cy1
        path = f"M{sx},{sy:.0f} H{x_out} V{cy2:.0f} H{r2.x + r2.w}"
        mx, my = float(x_out), (cy1 + cy2) / 2
    elif r2.x > r1.x:
        sx, ex = r1.x + r1.w, r2.x
        mx = (sx + ex) / 2
        path = f"M{sx},{cy1:.0f} H{mx:.0f} V{cy2:.0f} H{ex}"
        my = cy2
    else:
        sx, ex = r1.x, r2.x + r2.w
        mx = (sx + ex) / 2
        path = f"M{sx},{cy1:.0f} H{mx:.0f} V{cy2:.0f} H{ex}"
        my = cy2
    out = (
        f"<path d='{path}' fill='none' stroke='#4d4d4d' stroke-width='1.5' "
        f"marker-end='url(#arrow)'/>"
    )
    if label:
        out += (
            f"<text x='{mx:.0f}' y='{my - 4:.0f}' text-anchor='middle' fill='#4d4d4d' "
            f"{_FONT} paint-order='stroke' stroke='#ffffff' stroke-width='3'>"
            f"{escape(label)}</text>"
        )
    return out
