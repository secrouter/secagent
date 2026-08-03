"""Verbose progress reporting for long-running CLI commands.

``index`` and ``docs build`` do minutes of work (file summaries, clang parsing, LLM
function descriptions, Sphinx). They emit INFO-level checkpoints on the ``secagent``
logger; the CLI's ``-v/--verbose`` flag calls :func:`enable_verbose` to attach a
stderr handler so the user can see *where* the run is and roughly how far along.

Silent by default (no handler), and progress goes to **stderr** so a command's JSON
result on stdout stays clean and pipeable.
"""

from __future__ import annotations

import logging

# The root secagent logger; modules log via ``logging.getLogger(__name__)`` (children of
# this), so a single handler here surfaces all of them.
log = logging.getLogger("secagent")

_enabled = False


def enable_verbose() -> None:
    """Show ``secagent.*`` INFO progress on stderr (idempotent)."""
    global _enabled
    if _enabled:
        return
    from rich.console import Console
    from rich.logging import RichHandler

    handler = RichHandler(
        console=Console(stderr=True),
        show_path=False,
        show_level=False,
        markup=False,
        rich_tracebacks=False,
        log_time_format="[%H:%M:%S]",
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    _enabled = True


def progress_step(total: int, slices: int = 20) -> int:
    """How often to log inside a loop of ``total`` items (~``slices`` updates)."""
    return max(1, total // slices)
