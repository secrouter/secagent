"""Sphinx configuration for the secagent project documentation.

Build with:  sphinx-build -b html docs docs/_build/html
(or: make docs-html). Theme: furo.
"""

from __future__ import annotations

import importlib.metadata

project = "secagent"
author = "secagent"
copyright = "2026, secagent"

try:
    release = importlib.metadata.version("secagent")
except importlib.metadata.PackageNotFoundError:  # docs built without installing
    release = "0.1.0"
version = release

# No intersphinx: keep the docs build fully offline / air-gap friendly.
extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

# MyST (Markdown) configuration.
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
myst_heading_anchors = 3

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# autodoc: mock optional/heavy deps so the API page builds in a docs-only env.
autodoc_mock_imports = ["fastapi", "uvicorn", "tokenizers", "starlette"]
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# --- HTML / furo ---------------------------------------------------------------
html_theme = "furo"
html_title = f"secagent {version}"
html_static_path = ["_static"]
html_theme_options = {
    "sidebar_hide_name": False,
    "source_repository": "https://github.com/secrouter/secagent",
    "source_branch": "main",
    "source_directory": "docs/",
    "footer_icons": [],
}
