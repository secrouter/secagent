"""Tests for UC5: automatic test generation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.testgen.agent import _extract_code, _unit_path, generate_tests
from secagent.agents.testgen.frameworks import detect_framework
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # heuristic store; UC5 should still work + recommend
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_extract_code_strips_fence():
    assert _extract_code("```python\nassert 1\n```").strip() == "assert 1"
    assert _extract_code("no fence here").strip() == "no fence here"
    assert _extract_code("") == ""


def test_unit_path_conventions():
    assert _unit_path("services/api/db.py", "Python").as_posix() == \
        "unit/services/api/test_db.py"
    assert _unit_path("pkg/util.go", "Go").as_posix() == "unit/pkg/util_test.go"
    assert _unit_path("src/Foo.java", "Java").as_posix() == "unit/src/FooTest.java"


def test_generate_tests_writes_to_separate_folder(tmp_path):
    s = _settings(tmp_path)
    out = tmp_path / "secagent-tests"
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content="```python\ndef test_generated():\n    assert True\n```")))
    result = generate_tests(FIXTURE, s, out_dir=out, llm=llm)

    # Output is entirely under the separate folder, not inside the project.
    assert Path(result["out_dir"]) == out
    assert result["unit_count"] >= 1
    assert result["functional_count"] >= 1
    # Unit tests mirror the source tree under unit/.
    assert (out / "unit" / "services" / "api" / "test_db.py").exists()
    # Functional tests are grouped by component under functional/.
    assert any(p.name.startswith("test_") for p in (out / "functional").glob("*.py"))
    # Manifest + README written.
    assert (out / "manifest.json").exists()
    assert (out / "README.md").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["tests"]


def test_recommends_uc1_first(tmp_path):
    s = _settings(tmp_path)  # heuristic-only store (no LLM summaries)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content="```python\nassert True\n```")))
    result = generate_tests(FIXTURE, s, out_dir=tmp_path / "t", llm=llm)
    joined = " ".join(result["recommendations"]).lower()
    assert "uc1" in joined or "docs build" in joined
    # README surfaces the recommendation too.
    readme = (Path(result["out_dir"]) / "README.md").read_text().lower()
    assert "recommended first" in readme


def test_toggles_unit_and_functional(tmp_path):
    s = _settings(tmp_path)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content="```python\nassert True\n```")))
    only_functional = generate_tests(
        FIXTURE, s, out_dir=tmp_path / "f", unit=False, functional=True, llm=llm)
    assert only_functional["unit_count"] == 0
    assert only_functional["functional_count"] >= 1


# --- framework detection (defect: testgen recommended a framework it never checked for) --
#
# `TestGenConfig.frameworks` hardcodes C++ -> GoogleTest, C -> Unity, Python -> pytest,
# etc. and told the model to write tests in that framework even on a repo with no such
# dependency anywhere — the generated tests then cannot run. `detect_framework` looks
# for cheap, real repo evidence per language and only falls back to the configured
# default (disclosed as ASSUMED, not observed) when none is found.
#
# Fixtures below are real fragments of the files a genuine project of each framework
# would have — not the minimum string that makes a regex match.

_REAL_CSPROJ_XUNIT = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.8.0" />
    <PackageReference Include="xunit" Version="2.6.2" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.5.6" />
  </ItemGroup>
</Project>
"""

_REAL_CSPROJ_NUNIT = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="NUnit" Version="4.0.1" />
    <PackageReference Include="NUnit3TestAdapter" Version="4.5.0" />
  </ItemGroup>
</Project>
"""

_REAL_PYPROJECT_WITH_PYTEST = """\
[project]
name = "sample"
version = "0.1.0"

[project.optional-dependencies]
dev = ["pytest>=7.4", "pytest-cov"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

_REAL_CMAKE_GTEST = """\
cmake_minimum_required(VERSION 3.14)
project(myproject)

include(FetchContent)
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip
)
FetchContent_MakeAvailable(googletest)

add_executable(myproject_tests test_main.cpp)
target_link_libraries(myproject_tests GTest::gtest_main)
"""

_REAL_MAKEFILE_UNITY = """\
CC = gcc
CFLAGS = -Wall -Iunity/src

TESTS = build/test_runner

$(TESTS): test/test_widget.c unity/src/unity.c src/widget.c
\t$(CC) $(CFLAGS) -o $@ $^

test: $(TESTS)
\t./$(TESTS)
"""


def test_csharp_framework_detected_from_xunit_package_reference(tmp_path):
    (tmp_path / "MyLib.Tests.csproj").write_text(_REAL_CSPROJ_XUNIT)
    fw, detected = detect_framework(tmp_path, "C#", "xUnit")
    assert fw == "xUnit" and detected is True


def test_csharp_framework_detected_from_nunit_package_reference(tmp_path):
    (tmp_path / "MyLib.Tests.csproj").write_text(_REAL_CSPROJ_NUNIT)
    fw, detected = detect_framework(tmp_path, "C#", "xUnit")
    assert fw == "NUnit" and detected is True


def test_csharp_framework_falls_back_without_a_csproj(tmp_path):
    """Silence pair: no .csproj at all (or one with no test package) — the configured
    default is used but must be reported as ASSUMED."""
    (tmp_path / "readme.txt").write_text("no dotnet project here\n")
    fw, detected = detect_framework(tmp_path, "C#", "xUnit")
    assert fw == "xUnit" and detected is False


def test_python_framework_detected_from_pyproject_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text(_REAL_PYPROJECT_WITH_PYTEST)
    fw, detected = detect_framework(tmp_path, "Python", "pytest")
    assert fw == "pytest" and detected is True


def test_python_framework_detected_from_conftest(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef sample():\n    return 1\n")
    fw, detected = detect_framework(tmp_path, "Python", "pytest")
    assert fw == "pytest" and detected is True


def test_python_framework_falls_back_with_no_evidence(tmp_path):
    """Silence pair: a plain repo of .py files with no pytest marker anywhere — the
    default is used but must be reported as ASSUMED, not observed."""
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    fw, detected = detect_framework(tmp_path, "Python", "pytest")
    assert fw == "pytest" and detected is False


def test_cpp_framework_detected_from_cmake_gtest_reference(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(_REAL_CMAKE_GTEST)
    fw, detected = detect_framework(tmp_path, "C++", "GoogleTest")
    assert fw == "GoogleTest" and detected is True


def test_cpp_framework_detected_from_vendored_googletest_dir(tmp_path):
    gtest_dir = tmp_path / "third_party" / "googletest"
    gtest_dir.mkdir(parents=True)
    (gtest_dir / "CMakeLists.txt").write_text("# vendored googletest\n")
    fw, detected = detect_framework(tmp_path, "C++", "GoogleTest")
    assert fw == "GoogleTest" and detected is True


def test_cpp_framework_falls_back_with_no_evidence(tmp_path):
    """Silence pair: a plain C++ repo with no gtest anywhere — must be ASSUMED."""
    (tmp_path / "widget.cpp").write_text("int main() { return 0; }\n")
    fw, detected = detect_framework(tmp_path, "C++", "GoogleTest")
    assert fw == "GoogleTest" and detected is False


def test_c_framework_detected_from_makefile_unity_reference(tmp_path):
    (tmp_path / "Makefile").write_text(_REAL_MAKEFILE_UNITY)
    fw, detected = detect_framework(tmp_path, "C", "Unity")
    assert fw == "Unity" and detected is True


def test_c_framework_detected_from_vendored_unity_c(tmp_path):
    unity_dir = tmp_path / "unity" / "src"
    unity_dir.mkdir(parents=True)
    (unity_dir / "unity.c").write_text("/* Unity test framework source */\n")
    (unity_dir / "unity.h").write_text("#ifndef UNITY_H\n#define UNITY_H\n#endif\n")
    fw, detected = detect_framework(tmp_path, "C", "Unity")
    assert fw == "Unity" and detected is True


def test_c_framework_falls_back_with_no_evidence(tmp_path):
    """Silence pair: a plain C repo — must be ASSUMED, not observed."""
    (tmp_path / "widget.c").write_text("int main(void) { return 0; }\n")
    fw, detected = detect_framework(tmp_path, "C", "Unity")
    assert fw == "Unity" and detected is False


def test_rust_framework_is_a_language_fact_not_an_assumption(tmp_path):
    """`cargo test` ships with the toolchain — a `Cargo.toml` is a fact about the repo,
    not a third-party dependency to detect, so this is real evidence, not a guess."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n')
    fw, detected = detect_framework(tmp_path, "Rust", "cargo test")
    assert fw == "cargo test" and detected is True


def test_rust_framework_falls_back_without_cargo_toml(tmp_path):
    """A lone .rs file with no Cargo.toml (e.g. compiled directly with rustc) has no
    project-level evidence — must be ASSUMED."""
    (tmp_path / "main.rs").write_text("fn main() {}\n")
    fw, detected = detect_framework(tmp_path, "Rust", "cargo test")
    assert fw == "cargo test" and detected is False


def test_unsupported_language_always_falls_back(tmp_path):
    """Languages with no cheap, defensible file-only signal (JS/TS/Go/Java/Ruby) must
    never be guessed — always the configured default, always disclosed as assumed."""
    (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29.0.0"}}')
    fw, detected = detect_framework(tmp_path, "JavaScript", "Jest")
    assert fw == "Jest" and detected is False


# --- disclosure: assumed frameworks must say so; observed ones must not -----------

def _py_settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_functional_components = 0
    return s


def test_assumed_python_framework_is_disclosed(tmp_path):
    """No pytest evidence anywhere in this repo — the manifest and recommendations must
    say the framework was ASSUMED, not silently presented as fact."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def f():\n    return 1\n")
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    result = generate_tests(repo, _py_settings(tmp_path), out_dir=tmp_path / "o",
                            functional=False, llm=llm)

    assert result["unit_count"] == 1
    assert all(g["framework_assumed"] for g in result["generated"])
    joined = " ".join(result["recommendations"])
    assert "ASSUMED" in joined and "Python: pytest" in joined


def test_observed_python_framework_is_not_disclosed_as_assumed(tmp_path):
    """Silence pair: a repo with a real conftest.py (genuine pytest evidence) must NOT
    be told its framework was assumed — the same discipline `test_testgen_truncation.py`
    applies to the truncation warning: a notice that always fires is one nobody reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def f():\n    return 1\n")
    (repo / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef sample():\n    return 1\n")
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    result = generate_tests(repo, _py_settings(tmp_path), out_dir=tmp_path / "o",
                            functional=False, llm=llm)

    assert result["unit_count"] >= 1
    assert not any(g["framework_assumed"] for g in result["generated"])
    joined = " ".join(result["recommendations"])
    assert "ASSUMED" not in joined

