"""`.h` was classified as C by extension alone (`languages.py`), so a C++ header full of
`class`/`template`/`namespace` was told it was C — and downstream (testgen) that meant
being told to write Unity (a C framework) tests for it. `detect_language` now sniffs a
`.h` file's content for real C++ constructs before falling back to the extension.

The attack: a header containing `class`/`template`/`namespace` must resolve to C++, AND
a genuine C header must stay C (a sniffer that calls every C header C++ is worse than
the extension guess it replaces). Both directions are required, plus the obvious false
positives — the word `class` inside a comment or a string — must not trip it, and an
unreadable path (some callers only have a repo-relative string, not a live file) must
fall back to the extension answer rather than crash.
"""

from __future__ import annotations

from secagent.affordances.languages import detect_language

_CPP_CLASS_HEADER = """\
#ifndef WIDGET_H
#define WIDGET_H

namespace myapp {

class Widget {
public:
    Widget();
    void render();
private:
    int id_;
};

}  // namespace myapp

#endif
"""

_CPP_TEMPLATE_HEADER = """\
#ifndef PAIR_H
#define PAIR_H

template <typename T>
struct Pair {
    T first;
    T second;
};

#endif
"""

_GENUINE_C_HEADER = """\
#ifndef POINT_H
#define POINT_H

#include <stdint.h>

// Represents a simple point in 2D space.
struct point_t {
    int x;
    int y;
};

int point_distance(const struct point_t *a, const struct point_t *b);

#endif // POINT_H
"""

# "class"/"namespace"/"template" all appear here, but only inside a block comment, a
# line comment, and a string literal — never as a real C++ declaration.
_C_HEADER_WITH_WORDS_IN_COMMENTS_AND_STRINGS = """\
#ifndef DRIVER_H
#define DRIVER_H

/* This header defines the base class-like dispatch table used by drivers.
 * See docs/namespace-note.txt for template naming conventions.
 */
struct driver_ops {
    void (*init)(void);
};

static const char *DRIVER_KIND = "class-A"; // not a C++ class

#endif
"""


def test_h_with_class_is_cpp(tmp_path):
    p = tmp_path / "widget.h"
    p.write_text(_CPP_CLASS_HEADER)
    assert detect_language(p) == "C++"


def test_h_with_template_is_cpp(tmp_path):
    p = tmp_path / "pair.h"
    p.write_text(_CPP_TEMPLATE_HEADER)
    assert detect_language(p) == "C++"


def test_genuine_c_header_stays_c(tmp_path):
    """Silence test paired with the two above: real C (struct, `//` comments — legal
    since C99 — an include guard) must not be relabeled."""
    p = tmp_path / "point.h"
    p.write_text(_GENUINE_C_HEADER)
    assert detect_language(p) == "C"


def test_class_in_comment_or_string_does_not_trigger_cpp(tmp_path):
    """The obvious false positive: the bare word appearing in a comment or a string
    literal must not be mistaken for a real declaration."""
    p = tmp_path / "driver.h"
    p.write_text(_C_HEADER_WITH_WORDS_IN_COMMENTS_AND_STRINGS)
    assert detect_language(p) == "C"


def test_unreadable_h_path_falls_back_to_extension(tmp_path):
    """Some callers (e.g. docs outline generation) hold only a repo-relative path
    string, not a live file — `detect_language(path)` must not crash and must fall back
    to the plain extension answer."""
    missing = tmp_path / "does-not-exist" / "ghost.h"
    assert detect_language(missing) == "C"


def test_c_file_extension_is_unaffected(tmp_path):
    """Sniffing only ever applies to `.h` — `.c` stays unconditionally C."""
    p = tmp_path / "impl.c"
    p.write_text("class_t *make_class(void) { return 0; }\n")  # `class` substring, not C++
    assert detect_language(p) == "C"
