import subprocess

import pytest
from conftest import edit_config, load_settings, make_project

from workforce import sandbox
from workforce.sandbox import SandboxError

DENIED = ["**/.env", "**/.git/**", "**/*.pem"]


def test_safe_join_blocks_escapes_and_denied(tmp_path):
    (tmp_path / "a.py").write_text("x")
    assert sandbox.safe_join(tmp_path, "a.py", DENIED) == (tmp_path / "a.py").resolve()
    for bad in ("../x", "..\\x", "C:/Windows/win.ini", "/etc/passwd"):
        with pytest.raises(SandboxError, match="escapes"):
            sandbox.safe_join(tmp_path, bad, DENIED)
    for denied in (".env", "sub/.env", ".git/config", "k.pem"):
        with pytest.raises(SandboxError, match="denied"):
            sandbox.safe_join(tmp_path, denied, DENIED)


def test_sensitive_paths():
    assert sandbox.is_sensitive("tests/test_x.py")
    assert sandbox.is_sensitive("package.json")
    assert sandbox.is_sensitive("web/src/a.spec.ts")
    assert not sandbox.is_sensitive("src/app.py")


def test_project_must_be_allowed(cfg_root, tmp_path):
    proj = make_project(tmp_path / "elsewhere")
    s = load_settings(cfg_root)
    with pytest.raises(SandboxError, match="allowed_project_roots"):
        sandbox.check_project_allowed(s, proj)
    edit_config(cfg_root, "policies",
                lambda d: d["paths"].__setitem__("allowed_project_roots", [str(tmp_path)]))
    s = load_settings(cfg_root)
    assert sandbox.check_project_allowed(s, proj) == proj.resolve()
    with pytest.raises(SandboxError, match="ai-workforce repo itself"):
        sandbox.check_project_allowed(s, cfg_root)


def test_worktree_commit_diff_merge_cleanup(cfg_root, tmp_path):
    proj = make_project(tmp_path / "p")
    s = load_settings(cfg_root)
    wt = sandbox.create_worktree(s, proj, "t1")
    assert wt.branch == "workforce/t1" and wt.base_branch in ("main", "master")
    (wt.path / "new.py").write_text("print(1)\n")
    (wt.path / ".env").write_text("SECRET=1\n")
    (wt.path / "node_modules").mkdir()
    (wt.path / "node_modules" / "x.js").write_text("x")
    sha, skipped = sandbox.commit_changes(wt.path, "work", ["**/.env"])
    assert sha and skipped == [".env"]
    files = sandbox.changed_files(wt.path, wt.base_ref)
    assert [(f.status, f.path) for f in files] == [("A", "new.py")]
    assert "print(1)" in sandbox.diff(wt.path, wt.base_ref)
    # nothing new -> no commit; caches and venvs never count as uncommitted work
    assert sandbox.commit_changes(wt.path, "again", ["**/.env"])[0] is None
    (wt.path / "__pycache__").mkdir()
    (wt.path / "__pycache__" / "x.pyc").write_bytes(b"0")
    assert sandbox.uncommitted(wt.path) == [".env"]

    merged = sandbox.merge(proj, wt.branch, wt.base_branch, "merge it")
    assert (proj / "new.py").is_file() and merged
    notes = sandbox.remove_worktree(proj, wt.path, wt.branch, delete_branch=True)
    assert not wt.path.exists() and any("deleted branch" in n for n in notes)


def test_merge_refuses_dirty_checkout_and_conflicts(cfg_root, tmp_path):
    proj = make_project(tmp_path / "p")
    s = load_settings(cfg_root)
    wt = sandbox.create_worktree(s, proj, "t2")
    (wt.path / "README.md").write_text("worker version\n")
    sandbox.commit_changes(wt.path, "work", [])
    (proj / "README.md").write_text("my local edit\n")
    with pytest.raises(SandboxError, match="uncommitted"):
        sandbox.merge(proj, wt.branch, wt.base_branch, "m")
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qam",
                    "mine"], cwd=proj, check=True)
    with pytest.raises(SandboxError, match="conflict"):
        sandbox.merge(proj, wt.branch, wt.base_branch, "m")
    assert (proj / "README.md").read_text() == "my local edit\n"  # merge aborted cleanly


def test_create_worktree_needs_a_commit(cfg_root, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    with pytest.raises(SandboxError, match="no commits"):
        sandbox.create_worktree(load_settings(cfg_root), empty, "t3")
