"""Deterministic verification: real commands that pass or fail.

Which checks run:
  1. `.workforce.yaml` in YOUR project checkout (not the worktree, so a worker
     cannot weaken its own checks):
         setup:  ["npm ci"]                       # optional, run once if needed
         checks:
           - {name: tests, cmd: "npm test"}
           - {name: lint,  cmd: ["npx", "eslint", "."]}
  2. otherwise auto-detected:
       Python (pyproject.toml / setup.py / requirements.txt):
           pytest (if there are tests), ruff (if configured)
       Node (package.json): npm scripts lint, typecheck, test, test:e2e / e2e, build

Commands run in the task worktree with secrets stripped from the environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from workforce.engines.base import _kill_tree, child_env

CONFIG_NAME = ".workforce.yaml"
NODE_SCRIPTS = ("lint", "typecheck", "test", "test:e2e", "e2e", "build")


@dataclass(frozen=True)
class CheckSpec:
    name: str
    cmd: list[str] | str   # str -> run through the shell (only from your own .workforce.yaml)


@dataclass
class CheckResult:
    name: str
    cmd: str
    passed: bool
    exit_code: int | None
    seconds: float
    output_tail: str
    timed_out: bool = False


@dataclass
class VerifyReport:
    passed: bool
    source: str                    # ".workforce.yaml" | "auto:python" | "auto:node" | "none"
    checks: list[CheckResult] = field(default_factory=list)
    setup: list[CheckResult] = field(default_factory=list)
    note: str = ""
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if not self.checks:
            return f"NOT VERIFIED: {self.note or 'no checks found'}"
        parts = [f"{c.name}={'pass' if c.passed else 'FAIL'}" for c in self.checks]
        return ("PASSED: " if self.passed else "FAILED: ") + ", ".join(parts)


# ---------- detection ----------

def _python_runner(project: Path) -> list[str] | None:
    if (project / "uv.lock").is_file() and shutil.which("uv"):
        return ["uv", "run", "--frozen", "python", "-m"]
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python",
                "venv/Scripts/python.exe", "venv/bin/python"):
        if (project / rel).is_file():
            return [str(project / rel), "-m"]
    if shutil.which("uv"):
        return ["uv", "run", "python", "-m"]
    py = shutil.which("python") or shutil.which("python3")
    return [py, "-m"] if py else None


def _has_python_tests(project: Path) -> bool:
    if any((project / d).is_dir() for d in ("tests", "test")):
        return True
    for pattern in ("test_*.py", "*_test.py"):
        for p in project.rglob(pattern):
            if not any(part in (".venv", "venv", "node_modules", ".git") for part in p.parts):
                return True
    return False


def _ruff_configured(project: Path) -> bool:
    if (project / "ruff.toml").is_file() or (project / ".ruff.toml").is_file():
        return True
    pyproject = project / "pyproject.toml"
    return pyproject.is_file() and "[tool.ruff" in pyproject.read_text(encoding="utf-8",
                                                                         errors="replace")


def _node_scripts(project: Path) -> dict[str, str]:
    import json

    try:
        data = json.loads((project / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") or {}
    return {k: v for k, v in scripts.items() if isinstance(v, str)}


def detect(project: Path) -> tuple[str, list[CheckSpec], list[CheckSpec]]:
    """(source, setup steps, checks) for a project checkout."""
    cfg = project / CONFIG_NAME
    if cfg.is_file():
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        setup = [CheckSpec(f"setup{i + 1}", c) for i, c in enumerate(data.get("setup") or [])]
        checks = [CheckSpec(str(c["name"]), c["cmd"]) for c in data.get("checks") or []]
        return CONFIG_NAME, setup, checks

    if (project / "package.json").is_file():
        scripts = _node_scripts(project)
        npm = shutil.which("npm") or "npm"
        checks = []
        for name in NODE_SCRIPTS:
            body = scripts.get(name, "")
            if not body or "no test specified" in body:
                continue
            checks.append(CheckSpec(name, [npm, "run", name]))
        install = "ci" if (project / "package-lock.json").is_file() else "install"
        return "auto:node", [CheckSpec("npm-install", [npm, install])], checks

    if any((project / f).is_file() for f in ("pyproject.toml", "setup.py", "requirements.txt")):
        runner = _python_runner(project)
        if runner is None:
            return "auto:python", [], []
        checks = []
        if _has_python_tests(project):
            checks.append(CheckSpec("pytest", [*runner, "pytest", "-q"]))
        if _ruff_configured(project):
            checks.append(CheckSpec("ruff", [*runner, "ruff", "check"]))
        return "auto:python", [], checks

    return "none", [], []


# ---------- running ----------

def _run(spec: CheckSpec, cwd: Path, env: dict[str, str], timeout: float) -> CheckResult:
    shell = isinstance(spec.cmd, str)
    shown = spec.cmd if shell else " ".join(spec.cmd)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    started = time.monotonic()
    try:
        proc = subprocess.Popen(spec.cmd, cwd=cwd, env=env, shell=shell,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", creationflags=flags)
    except OSError as exc:
        return CheckResult(spec.name, shown, False, None, 0.0, f"could not start: {exc}")
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, _ = proc.communicate()
    return CheckResult(
        name=spec.name, cmd=shown, passed=(not timed_out and proc.returncode == 0),
        exit_code=proc.returncode, seconds=round(time.monotonic() - started, 1),
        output_tail=(out or "")[-3000:], timed_out=timed_out)


def check_env(worktree: Path, secret_names: set[str] | None = None) -> dict[str, str]:
    env = child_env(secret_names)
    env["CI"] = "1"  # test runners: no watch mode, no prompts
    # Make the worktree's code win over an editable install of your checkout.
    paths = [str(worktree / "src"), str(worktree)] if (worktree / "src").is_dir() \
        else [str(worktree)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.pop("VIRTUAL_ENV", None)  # don't leak the workforce's own venv into `uv run`
    return env


def verify(worktree: Path, config_from: Path, *, timeout: float = 900,
           secret_names: set[str] | None = None) -> VerifyReport:
    """Run the project's checks inside `worktree`. Check definitions come from
    `config_from` (your own checkout), detected node/python details from the worktree."""
    source, setup, checks = detect(config_from)
    if source.startswith("auto:"):
        # Same type, but take script lists from the worktree in case the task added
        # e.g. a test script. The config file itself is never read from the worktree.
        w_source, w_setup, w_checks = detect(worktree)
        if w_source == source:
            setup, checks = w_setup, w_checks
    if not checks:
        return VerifyReport(False, source, note="no checks found: add .workforce.yaml to "
                                                "your project to define them",
                            finished_at=time.time())

    env = check_env(worktree, secret_names)
    report = VerifyReport(True, source)
    for step in setup:
        if step.name == "npm-install" and (worktree / "node_modules").is_dir():
            continue
        res = _run(step, worktree, env, timeout)
        report.setup.append(res)
        if not res.passed:
            report.passed = False
            report.note = f"setup step '{step.name}' failed"
            report.finished_at = time.time()
            return report
    for spec in checks:
        res = _run(spec, worktree, env, timeout)
        report.checks.append(res)
        report.passed &= res.passed
    report.finished_at = time.time()
    return report
