"""The scanner sends a file's header alongside it, because it was guessing without one.

`_scan_file` used to send one raw source file and nothing else. For C++ that means the
model never sees a class member's declaration — members are declared in the header. It
called `_rtcm_parsing` uninitialized eight times out of eight while `sbf.cpp` declares it
zero times and `sbf.h:432` declares it `{nullptr}`. The model reasoned correctly from an
input we had crippled.

Measured on the real PX4 driver with the pre-fix MEM-006 text, so the header's effect is
isolated from the rule fix that also suppresses these claims: false "member is
uninitialized" claims about `_rtcm_parsing`/`_configured` — the two whose initialisers
live in the header — went 2 of 5 runs to 0 of 5 with the header present. Claims about
`_buf`, which genuinely has no initialiser, persisted, which is the right behaviour.

The header is REFERENCE ONLY and is deliberately not line-numbered, so a finding cannot
be attributed to it. Across 20 measured runs no finding was ever reported past the end of
the source file.

BUT THE FEATURE IS OFF BY DEFAULT. Everything above is true and it was still the wrong
trade. On `ashtech.cpp` (3 runs per arm, 21/21 coverage both) the header cut total
findings 48 -> 22, took INT-003 from 13 to 0 and CTL-004 from 8 to 0, and produced six
MEM-006 hits that are all one false claim about `_rx_buffer`. It does not add context to
the same analysis; it relocates the model's attention onto the header. These tests cover
the mechanism, which still works and is still reachable via `scan.include_header` — they
are not an argument for switching it on. See `quality/SCAN_HEADER_CONTEXT.md`.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from secagent.agents.scan.agent import _find_header, _scan_file
from secagent.agents.scan.rules import load_rules

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _cpp_pair(tmp_path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "driver.cpp").write_text(
        '#include "driver.h"\nvoid Driver::go() { _p->reset(); }\n')
    (tmp_path / "src" / "driver.h").write_text(
        "class Driver {\npublic:\n  void go();\nprivate:\n  Thing *_p{nullptr};\n};\n")
    return tmp_path


# --- resolution -----------------------------------------------------------------

def test_the_same_stem_header_is_found(tmp_path):
    """The case that matters: a C++ member's initialiser lives next door."""
    repo = _cpp_pair(tmp_path)
    found = _find_header(repo, "src/driver.cpp", 40_000)
    assert found is not None
    rel, text = found
    assert rel == "src/driver.h"
    assert "_p{nullptr}" in text, "the initialiser is the whole point"


def test_an_included_header_is_found_when_the_stem_does_not_match(tmp_path):
    """Falls back to the first same-directory header the file `#include`s."""
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "main.cpp").write_text('#include "types.h"\nint main(){return 0;}\n')
    (tmp_path / "src" / "types.h").write_text("struct T { int x{0}; };\n")
    found = _find_header(tmp_path, "src/main.cpp", 40_000)
    assert found is not None and found[0] == "src/types.h"


def test_a_file_with_no_header_resolves_to_nothing(tmp_path):
    """The silence half. A lone C file must not drag in an unrelated header, and a
    missing header is not an error."""
    (tmp_path / "a.c").write_text("int main(void){return 0;}\n")
    assert _find_header(tmp_path, "a.c", 40_000) is None


def test_a_system_include_is_not_treated_as_a_header(tmp_path):
    """`#include <string.h>` is angle-bracketed and outside the repo; only quoted,
    same-directory headers resolve."""
    (tmp_path / "a.cpp").write_text("#include <string.h>\nint main(){return 0;}\n")
    assert _find_header(tmp_path, "a.cpp", 40_000) is None


# --- what reaches the model ------------------------------------------------------

def _capture(header):
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        sent.append(_json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json=make_chat_response(content="[]"))

    _scan_file(mock_client(handler), "sys", "src/driver.cpp", "void go() {}\n",
               load_rules(RULES), header=header)
    return sent[0]


def test_the_header_is_labelled_reference_only_and_not_numbered():
    """A finding attributed to the header would be a new failure mode. The header is
    named as reference, the source is named as the subject, and only the source carries
    line numbers — so there is exactly one numbering the model can quote."""
    body = _capture(("src/driver.h", "class Driver { Thing *_p{nullptr}; };"))

    assert "File under review: src/driver.cpp" in body
    assert "REFERENCE ONLY — src/driver.h" in body
    assert "Do NOT report findings in src/driver.h" in body
    assert "_p{nullptr}" in body, "the declaration must actually reach the model"
    # The source is numbered; the header is not.
    assert "1: void go() {}" in body
    assert "1: class Driver" not in body


def test_without_a_header_the_prompt_is_unchanged_in_shape():
    """Paired silence: a C file with no header gets no reference section at all."""
    body = _capture(None)
    assert "REFERENCE ONLY" not in body
    assert "File under review: src/driver.cpp" in body


def test_include_header_is_off_by_default():
    """The default is a measured decision, not an oversight.

    Supplying the header fixes the MEM-006 case the tests above cover, and still made the
    scan worse overall: 48 findings -> 22, INT-003 13 -> 0, CTL-004 8 -> 0, and six new
    MEM-006 hits that are one repeated falsehood. If this assertion ever fails, the
    default was flipped — re-read `quality/SCAN_HEADER_CONTEXT.md` before changing it
    here, because the reasoning that makes turning it on look obvious is the reasoning we
    already followed and measured.
    """
    from secagent.config import ScanConfig

    assert ScanConfig().include_header is False
