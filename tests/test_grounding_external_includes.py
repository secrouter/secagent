"""A third-party include must not look like a fabricated repo symbol.

On four generated C++ test files, the ONLY thing that made `affordance verify` return
`ok: false` was `gtest/gtest.h` — a header that is *supposed* to come from outside the
repo. So a wholly fabricated test importing a non-existent `GpsParser.h` and a perfectly
good one produced the same boolean, and a pipeline gate reading it could not tell them
apart. That is the second time this checker has offered false reassurance.

The language already draws the distinction: `#include <x>` asks for a header on the
system/third-party search path, `#include "x"` claims the header belongs to this project.
"""

from __future__ import annotations

import pytest

from secagent.affordances import grounding
from secagent.affordances.api import index_repo
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings

_FABRICATED = '''\
#include <gtest/gtest.h>
#include <gmock/gmock.h>
#include <string>
// Assuming the component provides these headers
#include "GpsParser.h"

TEST(GpsParserTest, EmptyInput) {
    GpsParser parser;
    ASSERT_EQ(parser.Parse("").code, ErrorCode::EMPTY_INPUT);
}
'''

_HONEST = '''\
#include <gtest/gtest.h>
#include <cstdint>
#include "crc.h"

TEST(CrcTest, KnownVector) {
    ASSERT_EQ(calculateCRC32(0, nullptr, 0), 0u);
}
'''


@pytest.fixture
def store(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "crc.h").write_text(
        "#pragma once\nunsigned calculateCRC32(unsigned, unsigned char *, unsigned);\n")
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    index_repo(repo, s)
    st = AffordanceStore(repo, s.affordances.store_dir)
    yield st
    st.close()


def test_the_boolean_separates_a_fabrication_from_a_real_test(store):
    """The property that was false. Both files include `<gtest/gtest.h>`; only one
    invents a project header."""
    fabricated = grounding.check(store, _FABRICATED)
    honest = grounding.check(store, _HONEST)

    assert fabricated.ok is False, "an invented project header must fail the check"
    assert honest.ok is True, "a real test must not fail on its third-party includes"
    assert "GpsParser.h" in fabricated.unverified_paths


def test_third_party_includes_are_disclosed_not_silently_dropped(store):
    """Excluding them from the verdict must not mean hiding them: "we did not check
    these" has to be visible, or the next reader assumes everything was checked."""
    result = grounding.check(store, _HONEST)

    assert "gtest/gtest.h" in result.external
    assert "cstdint" in result.external
    assert "gtest/gtest.h" not in result.unverified_paths
    assert "external_includes" in result.to_dict()


def test_a_quoted_include_is_still_checked(store):
    """Paired silence, and the whole basis of the distinction. `#include "x"` claims the
    header is part of this project, so an unresolvable one is still a real finding."""
    result = grounding.check(store, '#include "does_not_exist.h"\n')
    assert result.ok is False
    assert "does_not_exist.h" in result.unverified_paths

    real = grounding.check(store, '#include "crc.h"\n')
    assert real.ok is True, "a quoted include that DOES resolve must pass"


def test_prose_is_unaffected(store):
    """The angle-bracket rule is a C-family source convention. Prose mentioning a
    non-existent file must still be flagged exactly as before."""
    result = grounding.check(store, "The helper in `imaginary.py` duplicates this.")
    assert result.ok is False
    assert "imaginary.py" in result.unverified_paths
    assert result.external == []
