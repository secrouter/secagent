"""`ignore_vcs`: exclude project version-control metadata from scans, but never
exclude source files that merely use or implement a VCS as a feature."""

from __future__ import annotations

from secagent.affordances.languages import is_vcs_metadata, walk_files


def _make_repo(root):
    # Version-control metadata (must be excluded when ignore_vcs is on).
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".gitignore").write_text("*.pyc\n")
    (root / ".gitmodules").write_text("[submodule]\n")
    sub = root / "apps" / "cf"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/apps/cf\n")  # submodule pointer FILE
    (sub / "main.c").write_text("int main(void){return 0;}\n")
    # Repository-host platform config (also excluded).
    gh = root / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yml").write_text("name: ci\n")
    sub_gh = root / "apps" / "cf" / ".github"
    sub_gh.mkdir(parents=True)
    (sub_gh / "CODEOWNERS").write_text("* @team\n")
    gl = root / ".gitlab" / "issue_templates"
    gl.mkdir(parents=True)
    (gl / "bug.md").write_text("## Bug\n")
    # Source that *uses/implements* Git/host as a feature — must NOT be excluded.
    vcs = root / "src" / "vcs"
    vcs.mkdir(parents=True)
    (vcs / "git.py").write_text("def clone(url): ...\n")
    (root / "src" / "gitlab_client.py").write_text("def review_mr(): ...\n")
    (root / "tools" / "github").mkdir(parents=True)
    (root / "tools" / "github" / "runner.c").write_text("int main(void){return 0;}\n")
    (root / "docs").mkdir()
    (root / "docs" / "git-workflow.md").write_text("# Git workflow\n")


def test_ignore_vcs_excludes_metadata_keeps_git_features(tmp_path):
    _make_repo(tmp_path)
    got = {p.relative_to(tmp_path).as_posix()
           for p in walk_files(tmp_path, [], ignore_vcs=True)}

    # VCS metadata excluded (incl. submodule pointer file and dotfiles).
    assert ".git/config" not in got
    assert ".gitignore" not in got
    assert ".gitmodules" not in got
    assert "apps/cf/.git" not in got
    # Repo-host platform config excluded (at any depth).
    assert ".github/workflows/ci.yml" not in got
    assert "apps/cf/.github/CODEOWNERS" not in got
    assert ".gitlab/issue_templates/bug.md" not in got

    # Real source kept — including files that use/implement Git as functionality.
    assert "apps/cf/main.c" in got
    assert "src/vcs/git.py" in got
    assert "src/gitlab_client.py" in got
    assert "docs/git-workflow.md" in got
    assert "tools/github/runner.c" in got  # a 'github' dir (no dot) is plain source


def test_ignore_vcs_false_includes_metadata(tmp_path):
    _make_repo(tmp_path)
    got = {p.relative_to(tmp_path).as_posix()
           for p in walk_files(tmp_path, [], ignore_vcs=False)}
    assert ".gitignore" in got
    assert ".git/config" in got
    assert "apps/cf/.git" in got


def test_is_vcs_metadata_classification():
    # metadata
    assert is_vcs_metadata(".git/config")
    assert is_vcs_metadata("apps/cf/.git")            # submodule pointer
    assert is_vcs_metadata("sub/mod/.git/HEAD")       # nested .git dir (monorepo)
    assert is_vcs_metadata(".gitignore")
    assert is_vcs_metadata("third_party/lib/.svn/entries")
    assert is_vcs_metadata(".github/workflows/ci.yml")     # repo-host platform config
    assert is_vcs_metadata("apps/x/.gitlab/templates/a.md")
    # NOT metadata — Git used as a feature, or a non-reserved name
    assert not is_vcs_metadata("src/vcs/git.py")
    assert not is_vcs_metadata("src/gitlab_client.py")
    assert not is_vcs_metadata("docs/git-workflow.md")
    assert not is_vcs_metadata("tools/github_actions_runner.c")
    assert not is_vcs_metadata("tools/github/runner.c")    # 'github' dir without the dot
