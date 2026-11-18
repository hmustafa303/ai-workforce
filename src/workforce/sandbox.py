"""Git worktree sandbox: every task works on its own branch in its own folder.

    project repo (yours)          workspace/<task-id>/  (worktree, branch workforce/<task-id>)
    ────────────────────          ─────────────────────────────────────────────────────────
    untouched until you approve   the worker writes here only; we commit its output;
                                  Claude reads the diff and runs the checks here

Only the workforce commits on the task branch (identity "workforce"). Merging
into your branch happens only through `merge()`, which the Parent calls after
you said yes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from workforce.settings import Settings

BRANCH_PREFIX = "workforce/"
GIT_IDENTITY = ["-c", "user.name=workforce", "-c", "user.email=workforce@localhost"]
# Never committed from a worktree, even if the project forgot to ignore them.
NEVER_COMMIT_DIRS = (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                     ".ruff_cache", ".mypy_cache")
NEVER_COMMIT_FILES = (".cursor/cli.json",)
# Changes to these deserve a close look: they can make checks pass dishonestly.
SENSITIVE_PATTERNS = (
    "**/test_*.py", "**/*_test.py", "**/tests/**", "**/__tests__/**", "**/*.test.*",
    "**/*.spec.*", "**/conftest.py", "**/.workforce.yaml", "**/package.json",
    "**/pyproject.toml", "**/setup.cfg", "**/pytest.ini", "**/tox.ini", "**/ruff.toml",
    "**/.eslintrc*", "**/eslint.config.*", "**/tsconfig*.json", "**/.github/**",
    "**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
)


class SandboxError(RuntimeError):
    """A git or path-safety problem. The message is safe to show."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base_ref: str      # commit sha the task started from
    base_branch: str   # your branch at that time (merge target)


# ---------- git helpers ----------

def git(cwd: Path | str, *args: str, check: bool = True, timeout: float = 120
        ) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout,
                         stdin=subprocess.DEVNULL)
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise SandboxError(f"git {args[0]} failed: {' | '.join(msg[-3:])}")
    return res


def repo_root(path: Path) -> Path:
    res = git(path, "rev-parse", "--show-toplevel", check=False)
    if res.returncode != 0:
        raise SandboxError(f"not a git repository: {path} (run `git init` and commit first)")
    return Path(res.stdout.strip()).resolve()


# ---------- policy checks ----------

def selftest_root(settings: Settings) -> Path:
    return settings.resolve(settings.policies.paths.workspace_dir) / "_selftest"


def check_project_allowed(settings: Settings, project: Path | str) -> Path:
    """Resolve the project path and make sure workers may touch it."""
    p = Path(project).expanduser().resolve()
    if not p.is_dir():
        raise SandboxError(f"project folder does not exist: {p}")
    roots = [settings.resolve(Path(r)) for r in settings.policies.paths.allowed_project_roots]
    roots.append(selftest_root(settings))
    if not any(p.is_relative_to(r.resolve()) for r in roots):
        raise SandboxError(
            f"project {p} is not in policies.paths.allowed_project_roots "
            f"(config/policies.yaml). Add it there first.")
    workforce = settings.root.resolve()
    if p == workforce or (p.is_relative_to(workforce) and not p.is_relative_to(
            selftest_root(settings).resolve())):
        raise SandboxError("workers may not work on the ai-workforce repo itself")
    return p


def is_denied(rel_path: str, denied_globs: list[str]) -> bool:
    rel = PurePosixPath(rel_path.replace("\\", "/"))
    return any(rel.full_match(g) for g in denied_globs)


def safe_join(root: Path, rel_path: str, denied_globs: list[str]) -> Path:
    """`root/rel_path`, refusing escapes (.., absolute paths, links) and denied files."""
    root = root.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise SandboxError(f"path escapes the task folder: {rel_path}")
    rel = target.relative_to(root).as_posix()
    if rel != "." and is_denied(rel, denied_globs):
        raise SandboxError(f"access to {rel} is denied by policy")
    return target


def is_sensitive(rel_path: str) -> bool:
    rel = PurePosixPath(rel_path)
    return any(rel.full_match(g) for g in SENSITIVE_PATTERNS)


# ---------- lifecycle ----------

def create_worktree(settings: Settings, project: Path, task_id: str) -> Worktree:
    root = repo_root(project)
    head = git(root, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode != 0:
        raise SandboxError(f"{root} has no commits yet. Commit your work first.")
    base_ref = head.stdout.strip()
    base_branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = f"{BRANCH_PREFIX}{task_id}"
    path = settings.resolve(settings.policies.paths.workspace_dir) / task_id
    if path.exists():
        raise SandboxError(f"worktree folder already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "-b", branch, str(path), base_ref)
    return Worktree(path.resolve(), branch, base_ref, base_branch)


def dirty_files(project: Path) -> list[str]:
    """Uncommitted changes in your checkout (workers will NOT see these)."""
    out = git(project, "status", "--porcelain", "--untracked-files=no").stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def _never_commit_pathspecs() -> list[str]:
    specs = [f":(exclude,glob)**/{d}/**" for d in NEVER_COMMIT_DIRS]
    return specs + [f":(exclude,glob)**/{f}" for f in NEVER_COMMIT_FILES]


def commit_changes(worktree: Path, message: str, denied_globs: list[str]
                   ) -> tuple[str | None, list[str]]:
    """Commit everything the worker changed. Returns (sha or None, skipped denied paths)."""
    git(worktree, "add", "-A", "--", ".", *_never_commit_pathspecs())
    staged = git(worktree, "diff", "--cached", "--name-only", "-z").stdout.split("\0")
    skipped = [p for p in staged if p and is_denied(p, denied_globs)]
    if skipped:
        git(worktree, "reset", "-q", "--", *skipped)
    if not git(worktree, "diff", "--cached", "--name-only").stdout.strip():
        return None, skipped
    git(worktree, *GIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    return git(worktree, "rev-parse", "HEAD").stdout.strip(), skipped


@dataclass(frozen=True)
class ChangedFile:
    status: str   # A, M, D, R...
    path: str
    sensitive: bool


def changed_files(worktree: Path, base_ref: str) -> list[ChangedFile]:
    out = git(worktree, "diff", "--name-status", "-M", base_ref, "HEAD").stdout
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][0], parts[-1]
        files.append(ChangedFile(status, path, is_sensitive(path) or status == "D"))
    return files


def diff(worktree: Path, base_ref: str, *, paths: list[str] | None = None,
         max_chars: int = 60_000) -> str:
    text = git(worktree, "diff", "-M", base_ref, "HEAD", "--", *(paths or [])).stdout
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff truncated at {max_chars} chars; " \
                                  f"ask for specific paths]"
    return text


def uncommitted(worktree: Path) -> list[str]:
    """Changes not yet committed on the task branch (build/cache folders ignored)."""
    out = git(worktree, "status", "--porcelain", "--untracked-files=all", "--", ".",
              *_never_commit_pathspecs()).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def merge(project: Path, branch: str, base_branch: str, message: str) -> str:
    """Merge the task branch into your branch (no fast-forward). Returns the merge sha."""
    root = repo_root(project)
    current = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != base_branch:
        raise SandboxError(f"your checkout is on '{current}', but the task started from "
                           f"'{base_branch}'. Switch back first.")
    if dirty_files(root):
        raise SandboxError("your checkout has uncommitted changes; commit or stash them "
                           "before merging")
    res = git(root, *GIT_IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, branch,
              check=False)
    if res.returncode != 0:
        git(root, "merge", "--abort", check=False)
        detail = (res.stdout + res.stderr).strip().splitlines()
        raise SandboxError("merge conflict, nothing was changed: " + " | ".join(detail[-4:]))
    return git(root, "rev-parse", "HEAD").stdout.strip()


def remove_worktree(project: Path, worktree: Path | None, branch: str | None, *,
                    delete_branch: bool) -> list[str]:
    root = repo_root(project)
    notes = []
    if worktree and Path(worktree).exists():
        git(root, "worktree", "remove", "--force", str(worktree))
        notes.append(f"removed worktree {worktree}")
    git(root, "worktree", "prune", check=False)
    if branch and delete_branch:
        if git(root, "branch", "-D", branch, check=False).returncode == 0:
            notes.append(f"deleted branch {branch}")
    elif branch:
        notes.append(f"kept branch {branch} (not merged; delete it with "
                     f"`git branch -D {branch}` if you don't need it)")
    return notes
