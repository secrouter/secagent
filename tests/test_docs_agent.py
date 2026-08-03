"""End-to-end test for UC1: docs build on the fixture repo (no LLM, no drawio)."""

from __future__ import annotations

from pathlib import Path

import pytest

from secagent.affordances.models import Component, IOEdge
from secagent.agents.docs.agent import build_docs
from secagent.agents.docs.drawio_gen import build_diagrams, generate_diagrams, io_diagram
from secagent.agents.docs.render import render_diagrams
from secagent.agents.docs.svg_render import diagram_to_svg
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # heuristic prose, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_drawio_xml_is_wellformed():
    import xml.etree.ElementTree as ET

    comps = [Component("api", "api", "package", language="Python"),
             Component("common", "common", "package", language="Python")]
    edges = [
        IOEdge("api", "common", "import"),
        IOEdge("api", "SQLite", "datastore", "SQLite"),
        IOEdge("api", "HTTP /users", "http_endpoint", "/users"),
    ]
    xml = io_diagram(comps, edges)
    root = ET.fromstring(xml)  # raises if malformed
    assert root.tag == "mxfile"
    # Two component nodes + datastore + endpoint = 4 vertices, plus edges.
    assert "SQLite" in xml and "/users" in xml


def test_generate_diagrams_returns_two():
    diagrams = generate_diagrams([], [])
    assert set(diagrams) == {"components.drawio", "system_io.drawio"}


def test_build_docs_end_to_end(settings, tmp_path):
    out = tmp_path / "docs"
    report = build_docs(FIXTURE, out, settings, run_sphinx=True)

    # Sources written.
    src = Path(report["write"]["source_dir"])
    assert (src / "index.rst").exists()
    assert (src / "architecture.rst").exists()
    assert (src / "_diagrams" / "components.drawio").exists()
    assert (src / "_diagrams" / "system_io.drawio").exists()

    # Default "svg" backend renders both diagrams to static SVG (no binary needed).
    assert report["render"]["backend"] == "svg"
    assert set(report["render"]["rendered"]) == {"components.svg", "system_io.svg"}
    assert (src / "_diagrams" / "components.svg").exists()
    # Architecture page embeds the rendered image, not a drawio directive.
    arch = (src / "architecture.rst").read_text()
    assert ".. image:: /_diagrams/components.svg" in arch
    assert "drawio-image" not in arch

    # Sphinx built HTML — and needs no drawio extension (images are static).
    conf = (src / "conf.py").read_text()
    assert "sphinxcontrib.drawio" not in conf
    assert report["sphinx"]["ok"] is True, report["sphinx"].get("stderr_tail")
    assert Path(report["sphinx"]["index_html"]).exists()
    html = Path(report["sphinx"]["index_html"]).read_text()
    assert "documentation" in html.lower()


def test_direct_svg_render_is_wellformed():
    import xml.etree.ElementTree as ET

    comps = [Component("api", "api", "package", language="Python")]
    edges = [
        IOEdge("api", "SQLite", "datastore", "SQLite"),
        IOEdge("api", "/users", "http_endpoint", "/users"),
    ]
    svg = diagram_to_svg(build_diagrams(comps, edges)["system_io"])
    root = ET.fromstring(svg)  # raises if malformed
    assert root.tag.endswith("svg")
    assert "SQLite" in svg and "/users" in svg


def test_render_diagrams_svg_default(tmp_path):
    comps = [Component("api", "api", "package", language="Python")]
    diagrams = build_diagrams(comps, [])
    result = render_diagrams(diagrams, tmp_path, renderer="svg")
    assert result.backend == "svg"
    assert "system_io.svg" in result.rendered
    assert (tmp_path / "system_io.svg").read_text().lstrip().startswith("<?xml")


def test_unavailable_faithful_backend_falls_back_to_svg(tmp_path):
    # "chromium" with no chromium binary present must still produce an SVG.
    diagrams = build_diagrams([Component("api", "api", "package", language="Python")], [])
    (tmp_path / "system_io.drawio").write_text("<mxfile/>")
    (tmp_path / "components.drawio").write_text("<mxfile/>")
    result = render_diagrams(diagrams, tmp_path, renderer="chromium",
                             chromium_path="/nonexistent/chrome")
    assert result.backend.startswith("svg (fallback")
    assert (tmp_path / "system_io.svg").exists()


def test_build_docs_no_sphinx(settings, tmp_path):
    out = tmp_path / "docs2"
    report = build_docs(FIXTURE, out, settings, run_sphinx=False)
    assert report["sphinx"]["skipped"] is True
    assert Path(report["write"]["source_dir"], "components.rst").exists()
