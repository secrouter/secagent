"""A relative target must survive IKOS being run from the output directory.

`analyze run . src/crc.cpp` — the form the docs use — failed with
`clang: error: no such file or directory: 'src/crc.cpp'` for a file plainly present in
the user's working directory, because IKOS is invoked with cwd set to the report's
output dir and the target was passed through unchanged. The error names the file as
missing, which reads as "you typed the wrong path" rather than "we resolved it wrong".
"""

from __future__ import annotations

from pathlib import Path

from secagent.agents.analysis.ikos import _abs_flag


def test_relative_include_flags_are_absolutised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc").mkdir()
    assert _abs_flag("-Iinc") == f"-I{(tmp_path / 'inc').resolve()}"
    assert _abs_flag("-isysteminc") == f"-isystem{(tmp_path / 'inc').resolve()}"


def test_absolute_and_non_path_flags_are_left_alone():
    assert _abs_flag("-I/usr/include") == "-I/usr/include"
    assert _abs_flag("-DFOO=1") == "-DFOO=1"
    assert _abs_flag("--no-pointer") == "--no-pointer"
    assert _abs_flag("-I") == "-I"          # bare flag, nothing to resolve


def test_target_is_resolved_before_the_working_directory_changes(tmp_path, monkeypatch):
    """The reported failure, end to end at the command-construction layer."""
    import secagent.agents.analysis.ikos as mod

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    target = repo / "src" / "crc.cpp"
    target.write_text("int main(void){return 0;}\n")
    out = tmp_path / "out"

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        Path(kw["cwd"], "r.json").write_text("{}")
        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(mod, "ikos_available", lambda: (True, "ikos 3.4"))
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.chdir(repo)

    mod.run_ikos("src/crc.cpp", report_path=out / "r.json")

    passed = captured["cmd"][-1]
    assert Path(passed).is_absolute(), f"target went through relative: {passed}"
    assert Path(passed) == target.resolve()
    # ...and the cwd really is elsewhere, which is what made it break.
    assert Path(captured["cwd"]).resolve() != repo.resolve()
