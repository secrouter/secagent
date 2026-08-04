"""Tests for `gitscope`: local git delta computation via subprocess git.

Every scope resolver is exercised against a REAL git repository built fresh in
`tmp_path` (subprocess `git`, not a fake) — the only way to trust `-M` rename
detection, `--name-status` parsing, and the auto-detect fallback chain actually
behave the way the module claims.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from secagent import gitscope
from secagent.config import Settings

# ---------------------------------------------------------------------------
# repo-building helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _init_repo(root: Path, *, branch: str = "main") -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _commit(repo: Path, message: str, *paths: str) -> None:
    _git(repo, "add", *(paths or ("-A",)))
    _git(repo, "commit", "-q", "-m", message)


def _base_settings() -> Settings:
    return Settings()


def _by_path(cs: gitscope.ChangeSet) -> dict[str, gitscope.FileChange]:
    return {f.path: f for f in cs.files}


# ---------------------------------------------------------------------------
# since_base — the default scope
# ---------------------------------------------------------------------------


def test_since_base_combines_committed_uncommitted_and_untracked(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "line1\nline2\n")
    _write(repo, "keep.txt", "unchanged\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")

    # committed on the branch
    _write(repo, "a.txt", "line1\nline2\nline3\n")
    _commit(repo, "branch commit")
    # uncommitted edit on top
    _write(repo, "a.txt", "line1\nline2\nline3\nline4\n")
    # untracked new file
    _write(repo, "new.txt", "brand new\n")

    cs = gitscope.since_base(repo)
    assert cs.scope == "since_base"
    assert cs.base_ref == "main"
    assert cs.base_sha and cs.head_sha
    files = _by_path(cs)
    assert set(files) == {"a.txt", "new.txt"}
    assert "keep.txt" not in files  # never touched -> not part of the delta

    a = files["a.txt"]
    assert a.status == "modified"
    assert a.diff_text is not None and "line3" in a.diff_text and "line4" in a.diff_text
    new = files["new.txt"]
    assert new.status == "added" and new.diff_text is None and not new.binary
    assert set(cs.paths()) == {"a.txt", "new.txt"}


def test_since_base_excludes_files_never_touched_on_the_branch(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "untouched.c", "int f(void) { return 0; }\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "touched.c", "int g(void) { return 1; }\n")
    _commit(repo, "add touched")

    cs = gitscope.since_base(repo)
    assert cs.paths() == ["touched.c"]


def test_since_base_explicit_base_argument(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "develop")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "on develop")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "c.txt", "z\n")
    _commit(repo, "on feature")

    cs = gitscope.since_base(repo, base="develop")
    assert cs.base_ref == "develop"
    assert cs.paths() == ["c.txt"]


# ---------------------------------------------------------------------------
# base auto-detection
# ---------------------------------------------------------------------------


def test_base_auto_detect_prefers_main(tmp_path):
    repo = _init_repo(tmp_path, branch="main")
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "branch", "master")  # both exist; main must win
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "feature")

    cs = gitscope.since_base(repo)
    assert cs.base_ref == "main"


def test_base_auto_detect_falls_back_to_master(tmp_path):
    repo = _init_repo(tmp_path, branch="master")
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "feature")

    cs = gitscope.since_base(repo)
    assert cs.base_ref == "master"


def test_base_auto_detect_raises_when_nothing_resolves(tmp_path):
    repo = _init_repo(tmp_path, branch="trunk")
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")

    with pytest.raises(gitscope.GitScopeError, match="auto-detect"):
        gitscope.since_base(repo)


def test_explicit_base_not_found_raises_with_the_ref_named(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")

    with pytest.raises(gitscope.GitScopeError, match="nope-branch"):
        gitscope.since_base(repo, base="nope-branch")


# ---------------------------------------------------------------------------
# working_tree / staged
# ---------------------------------------------------------------------------


def test_working_tree_excludes_already_committed_branch_changes(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "line1\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "committed.txt", "on the branch, committed\n")
    _commit(repo, "branch work")
    _write(repo, "a.txt", "line1\nuncommitted edit\n")
    _write(repo, "untracked.txt", "new\n")

    cs = gitscope.working_tree(repo)
    assert cs.scope == "working_tree"
    paths = set(cs.paths())
    assert paths == {"a.txt", "untracked.txt"}
    assert "committed.txt" not in paths  # already committed -> not "uncommitted"


def test_staged_only_sees_the_index_not_unstaged_or_untracked(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "line1\n")
    _write(repo, "b.txt", "line1\n")
    _commit(repo, "initial")
    _write(repo, "a.txt", "line1\nstaged edit\n")
    _git(repo, "add", "a.txt")
    _write(repo, "b.txt", "line1\nunstaged edit\n")  # not added
    _write(repo, "untracked.txt", "new\n")  # never added

    cs = gitscope.staged(repo)
    assert cs.scope == "staged"
    assert cs.paths() == ["a.txt"]
    assert cs.files[0].diff_text is not None and "staged edit" in cs.files[0].diff_text


# ---------------------------------------------------------------------------
# since_ref / range
# ---------------------------------------------------------------------------


def test_since_ref_behaves_like_since_base_with_an_explicit_ref(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "tag", "v1")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "feature work")

    cs = gitscope.since_ref(repo, "v1")
    assert cs.scope == "since_ref"
    assert cs.base_ref == "v1"
    assert cs.paths() == ["b.txt"]


def test_since_ref_not_found_raises(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    with pytest.raises(gitscope.GitScopeError, match="not-a-real-ref"):
        gitscope.since_ref(repo, "not-a-real-ref")


def test_range_compares_two_fixed_commits_ignoring_the_working_tree(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "c1")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "c2")
    # dirty working tree AFTER c2 — must be invisible to a pure range diff
    _write(repo, "c.txt", "dirty, uncommitted\n")

    cs = gitscope.range(repo, "HEAD~1", "HEAD")
    assert cs.scope == "range"
    assert cs.paths() == ["b.txt"]
    assert cs.base_sha != cs.head_sha


def test_range_ref_not_found_raises(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "c1")
    with pytest.raises(gitscope.GitScopeError, match="ghost-ref"):
        gitscope.range(repo, "HEAD", "ghost-ref")


# ---------------------------------------------------------------------------
# explicit
# ---------------------------------------------------------------------------


def test_explicit_wraps_paths_without_touching_git(tmp_path, monkeypatch):
    repo = tmp_path / "not_even_a_repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")

    def _boom(*a, **k):
        raise AssertionError("explicit() must not invoke git at all")

    monkeypatch.setattr(gitscope.subprocess, "run", _boom)
    cs = gitscope.explicit(repo, ["a.py", "sub/b.py"])
    assert cs.scope == "explicit"
    assert cs.base_ref is None and cs.base_sha is None and cs.head_sha is None
    assert [f.status for f in cs.files] == ["modified", "modified"]
    assert all(f.diff_text is None for f in cs.files)
    assert cs.paths() == ["a.py", "sub/b.py"]


def test_explicit_rejects_a_path_outside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(gitscope.GitScopeError, match="outside"):
        gitscope.explicit(repo, ["../secrets.env"])


# ---------------------------------------------------------------------------
# renames and deletions
# ---------------------------------------------------------------------------


def test_rename_is_reported_with_old_path_and_a_real_diff(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "src/old_name.c", "int f(void) {\n  return 0;\n}\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _git(repo, "mv", "src/old_name.c", "src/new_name.c")
    _write(repo, "src/new_name.c", "int f(void) {\n  return 0;\n}\n// comment\n")
    _git(repo, "add", "src/new_name.c")
    _commit(repo, "rename with a small edit")

    cs = gitscope.since_base(repo)
    assert len(cs.files) == 1
    f = cs.files[0]
    assert f.status == "renamed"
    assert f.old_path == "src/old_name.c"
    assert f.path == "src/new_name.c"
    assert f.diff_text is not None and "comment" in f.diff_text
    # deletions are excluded from paths(); a clean rename has none here.
    assert cs.paths() == ["src/new_name.c"]


def test_deletion_is_excluded_from_paths_but_keeps_its_diff(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "gone.txt", "line1\nline2\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "gone.txt").unlink()
    _git(repo, "add", "-A")
    _commit(repo, "remove gone.txt")

    cs = gitscope.since_base(repo)
    files = _by_path(cs)
    assert files["gone.txt"].status == "deleted"
    assert files["gone.txt"].diff_text is not None  # the removed content is still shown
    assert "gone.txt" not in cs.paths()  # but not offered up as something to (re-)read
    entry = cs.to_gitlab_style()["changes"][0]
    assert entry["deleted_file"] is True and entry["new_path"] == "gone.txt"


# ---------------------------------------------------------------------------
# binary files
# ---------------------------------------------------------------------------


def test_tracked_binary_file_has_no_diff_text_and_is_flagged(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "readme.txt", "text\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary\xff\xfe")
    _git(repo, "add", "blob.bin")
    _commit(repo, "add binary")

    cs = gitscope.since_base(repo)
    files = _by_path(cs)
    assert files["blob.bin"].binary is True
    assert files["blob.bin"].diff_text is None


def test_untracked_binary_file_is_sniffed_and_flagged(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "raw.bin").write_bytes(b"\x00\x00binary-ish")  # untracked, never added

    cs = gitscope.since_base(repo)
    files = _by_path(cs)
    assert files["raw.bin"].status == "added"
    assert files["raw.bin"].binary is True
    assert files["raw.bin"].diff_text is None


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_not_a_git_repository_raises_clearly(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x\n")
    with pytest.raises(gitscope.GitScopeError, match="not a git repository"):
        gitscope.since_base(plain)


@pytest.mark.parametrize(
    "call",
    [
        lambda repo: gitscope.since_base(repo),
        lambda repo: gitscope.working_tree(repo),
        lambda repo: gitscope.staged(repo),
        lambda repo: gitscope.range(repo, "HEAD", "HEAD"),
    ],
)
def test_no_commits_yet_raises_on_every_git_backed_resolver(tmp_path, call):
    repo = _init_repo(tmp_path)  # init only — zero commits
    with pytest.raises(gitscope.GitScopeError, match="no commits yet"):
        call(repo)


def test_git_binary_missing_raises_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(gitscope.shutil, "which", lambda _name: None)
    with pytest.raises(gitscope.GitScopeError, match="not installed"):
        gitscope.since_base(tmp_path)


def test_path_outside_repo_is_the_only_way_explicit_can_fail(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    with pytest.raises(gitscope.GitScopeError):
        gitscope.explicit(repo, ["../../etc/passwd"])


# ---------------------------------------------------------------------------
# detached HEAD
# ---------------------------------------------------------------------------


def test_detached_head_still_resolves_a_delta(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "second")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", sha)  # detach
    _write(repo, "c.txt", "z\n")  # untracked while detached

    cs = gitscope.working_tree(repo)
    assert cs.paths() == ["c.txt"]
    assert gitscope.current_branch(repo).startswith("detached HEAD @ ")


def test_current_branch_named_when_on_a_branch(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature/thing")
    assert gitscope.current_branch(repo) == "feature/thing"


# ---------------------------------------------------------------------------
# ChangeSet convenience methods
# ---------------------------------------------------------------------------


def test_to_gitlab_style_matches_the_gitlab_harness_shape(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.txt", "x\ny\n")
    _commit(repo, "edit")

    cs = gitscope.since_base(repo)
    changes = cs.to_gitlab_style()
    assert list(changes) == ["changes"]
    entry = changes["changes"][0]
    assert entry["new_path"] == "a.txt"
    assert entry["old_path"] == "a.txt"
    assert "diff" in entry and entry["new_file"] is False and entry["deleted_file"] is False


def test_unified_diff_concatenates_with_git_headers(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _write(repo, "b.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.txt", "x\ny\n")
    _write(repo, "b.txt", "x\nz\n")
    _commit(repo, "edit both")

    cs = gitscope.since_base(repo)
    blob = cs.unified_diff()
    assert blob.count("diff --git") == 2
    assert "a.txt" in blob and "b.txt" in blob


def test_summary_dict_carries_total_analyzable_once_the_caller_sets_it(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "add b")

    cs = gitscope.since_base(repo)
    assert cs.summary_dict()["total_analyzable"] is None
    cs.total_analyzable = 42
    assert cs.summary_dict()["total_analyzable"] == 42
    assert cs.summary_dict()["files_changed"] == 1


# ---------------------------------------------------------------------------
# analyzable()
# ---------------------------------------------------------------------------


def test_analyzable_drops_ignored_vcs_and_binary_paths(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "keep.py", "x = 1\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/real.py", "x = 2\n")
    _write(repo, "build/generated.py", "# built artifact\n")  # matches ignore_globs
    (repo / "blob.bin").write_bytes(b"\x00binary")
    _git(repo, "add", "-A")
    _commit(repo, "mixed changes")

    cs = gitscope.since_base(repo)
    settings = _base_settings()  # default ignore_globs includes "**/build/**"
    in_scope, dropped = gitscope.analyzable(cs, settings)
    assert in_scope == ["src/real.py"]
    assert dropped == 2  # build/generated.py (ignored) + blob.bin (binary)


def test_analyzable_never_offers_a_deleted_path(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "gone.py", "x = 1\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "gone.py").unlink()
    _git(repo, "add", "-A")
    _commit(repo, "remove")

    cs = gitscope.since_base(repo)
    in_scope, dropped = gitscope.analyzable(cs, _base_settings())
    assert in_scope == []
    assert dropped == 0  # excluded via paths(), not counted as a "drop" a second time


# ---------------------------------------------------------------------------
# describe_scope / coverage_banner
# ---------------------------------------------------------------------------


def test_describe_scope_and_coverage_banner_name_the_scope_and_say_partial(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "add b")

    cs = gitscope.since_base(repo)
    cs.total_analyzable = 7
    desc = gitscope.describe_scope(cs)
    assert "since_base" in desc and "main" in desc

    banner = gitscope.coverage_banner(cs, in_scope=1, dropped=2)
    assert "SCOPED run" in banner
    assert "1 of 7" in banner
    assert "2 changed file(s)" in banner
    assert "PARTIAL" in banner
    assert "--all" in banner


def test_coverage_banner_omits_dropped_clause_when_nothing_was_dropped(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "add b")

    cs = gitscope.since_base(repo)
    cs.total_analyzable = 3
    banner = gitscope.coverage_banner(cs, in_scope=1, dropped=0)
    assert "were not analyzable" not in banner


# ---------------------------------------------------------------------------
# resolve_scope — the shared CLI flag resolver
# ---------------------------------------------------------------------------


def test_resolve_scope_defaults_to_since_base(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.txt", "y\n")
    _commit(repo, "add b")

    cs = gitscope.resolve_scope(repo)
    assert cs is not None and cs.scope == "since_base"


def test_resolve_scope_all_files_returns_none_sentinel(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    assert gitscope.resolve_scope(repo, all_files=True) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base": "main", "staged_only": True},
        {"since": "main", "working_tree_only": True},
        {"paths": ["a.txt"], "all_files": True},
        {"base": "main", "since": "main"},
    ],
)
def test_resolve_scope_rejects_more_than_one_active_source(tmp_path, kwargs):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _commit(repo, "initial")
    with pytest.raises(gitscope.GitScopeError, match="mutually exclusive"):
        gitscope.resolve_scope(repo, **kwargs)


def _two_commit_repo(tmp_path):
    """A repo with two commits: returns (repo, first_sha, second_sha)."""
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "one\n")
    _commit(repo, "first")
    first = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo, "a.txt", "one\ntwo\n")
    _write(repo, "b.txt", "new\n")
    _commit(repo, "second")
    second = _git(repo, "rev-parse", "HEAD").strip()
    return repo, first, second


def test_resolve_scope_range_dispatches_to_range(tmp_path):
    repo, first, second = _two_commit_repo(tmp_path)
    cs = gitscope.resolve_scope(repo, range_spec=f"{first}..{second}")
    assert cs is not None and cs.scope == "range"
    assert set(cs.paths()) == {"a.txt", "b.txt"}


def test_resolve_scope_range_conflicts_with_another_source(tmp_path):
    repo, first, second = _two_commit_repo(tmp_path)
    with pytest.raises(gitscope.GitScopeError, match="mutually exclusive"):
        gitscope.resolve_scope(repo, staged_only=True, range_spec=f"{first}..{second}")


@pytest.mark.parametrize("bad", ["nodots", "a...b", "..b", "a..", ""])
def test_resolve_scope_range_rejects_malformed(tmp_path, bad):
    repo, _first, _second = _two_commit_repo(tmp_path)
    with pytest.raises(gitscope.GitScopeError):
        gitscope.resolve_scope(repo, range_spec=bad)


def test_resolve_scope_dispatches_staged_working_tree_and_paths(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "a.txt", "x\n")
    _write(repo, "b.txt", "x\n")
    _commit(repo, "initial")
    _write(repo, "a.txt", "x\nstaged\n")
    _git(repo, "add", "a.txt")
    _write(repo, "b.txt", "x\nunstaged\n")

    staged_cs = gitscope.resolve_scope(repo, staged_only=True)
    assert staged_cs.scope == "staged" and staged_cs.paths() == ["a.txt"]

    wt_cs = gitscope.resolve_scope(repo, working_tree_only=True)
    assert wt_cs.scope == "working_tree"
    assert set(wt_cs.paths()) == {"a.txt", "b.txt"}

    explicit_cs = gitscope.resolve_scope(repo, paths=["b.txt"])
    assert explicit_cs.scope == "explicit" and explicit_cs.paths() == ["b.txt"]

    since_cs = gitscope.resolve_scope(repo, since="HEAD")
    assert since_cs.scope == "since_ref" and since_cs.base_ref == "HEAD"
