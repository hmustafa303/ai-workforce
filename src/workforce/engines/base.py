"""Shared machinery for running a CLI AI tool headless and safely.

Safety rules applied to every engine:
  * working directory = the task workdir (a git worktree in later phases)
  * stdin closed (the prompt is fed once, then EOF), so nothing can hang on input
  * secrets stripped from the environment: provider keys from .env and anything
    that looks like a key or token; tools use their own login instead
  * hard timeout that kills the whole process tree
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from workforce.settings import EngineSpec

_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


class EngineUnavailable(RuntimeError):
    """The tool is not installed, not logged in, or disabled."""


@dataclass(frozen=True)
class EngineResult:
    engine: str
    ok: bool
    exit_code: int | None
    seconds: float
    final_message: str
    log_tail: str
    timed_out: bool = False


def child_env(extra_secret_names: set[str] | None = None) -> dict[str, str]:
    """A copy of the environment without secrets."""
    drop = {n.upper() for n in (extra_secret_names or set())}
    return {k: v for k, v in os.environ.items()
            if k.upper() not in drop and not _SECRET_NAME.search(k)}


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False, stdin=subprocess.DEVNULL)
    else:
        proc.kill()


class Engine(ABC):
    #: False when the tool cannot be confined to the workdir on this OS.
    supports_write: bool = True

    def __init__(self, name: str, spec: EngineSpec, tier: str | None = None):
        self.name = name
        self.spec = spec
        self.tier = tier  # difficulty tier: picks effort / model from the spec

    @property
    def model(self) -> str | None:
        return self.spec.model_for(self.tier)

    @property
    def effort(self) -> str | None:
        return self.spec.effort.get(self.tier) if self.tier else None

    def label(self) -> str:
        parts = [p for p in (self.model, f"effort={self.effort}" if self.effort else None) if p]
        return self.name + (f" ({', '.join(parts)})" if parts else "")

    @abstractmethod
    def resolve_binary(self) -> Path:
        """Locate the tool's executable or raise EngineUnavailable."""

    @abstractmethod
    def command(self, binary: Path, workdir: Path, *, write: bool,
                out_file: Path) -> list[str]:
        """Command line for one headless run. The prompt goes to stdin."""

    def final_message(self, stdout: str, out_file: Path) -> str:
        """Extract the tool's final answer. Default: whole stdout."""
        return stdout.strip()

    def extra_env(self) -> dict[str, str]:
        """Non-secret environment variables the tool needs."""
        return {}

    def prepare(self, workdir: Path, *, write: bool) -> None:  # noqa: B027 - optional hook
        """Set up per-run guard rails in the workdir (e.g. permission files)."""

    def cleanup(self, workdir: Path) -> None:  # noqa: B027 - optional hook
        """Undo `prepare`. Always called, even on failure or timeout."""

    def run(self, prompt: str, workdir: Path | str, *, write: bool = False,
            secret_names: set[str] | None = None,
            timeout: float | None = None) -> EngineResult:
        if not self.spec.enabled:
            raise EngineUnavailable(f"engine '{self.name}' is disabled in config/engines.yaml")
        if write and not self.supports_write:
            raise EngineUnavailable(
                f"engine '{self.name}' is read-only here: its writes cannot be confined "
                f"to the workdir on this OS")
        workdir = Path(workdir).resolve()
        if not workdir.is_dir():
            raise EngineUnavailable(f"workdir does not exist: {workdir}")
        binary = self.resolve_binary()
        timeout = timeout or self.spec.timeout_seconds

        with tempfile.TemporaryDirectory(prefix=f"wf-{self.name}-") as tmp:
            out_file = Path(tmp) / "last_message.txt"
            cmd = self.command(binary, workdir, write=write, out_file=out_file)
            env = {**child_env(secret_names), **self.extra_env()}
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            self.prepare(workdir, write=write)
            try:
                started = time.monotonic()
                proc = subprocess.Popen(
                    cmd, cwd=workdir, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=flags)
                timed_out = False
                try:
                    stdout, _ = proc.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_tree(proc)
                    stdout, _ = proc.communicate()
                seconds = round(time.monotonic() - started, 1)
                message = self.final_message(stdout or "", out_file)
            finally:
                self.cleanup(workdir)

        code = proc.returncode
        return EngineResult(
            engine=self.name,
            ok=(not timed_out and code == 0 and bool(message)),
            exit_code=code,
            seconds=seconds,
            final_message=message,
            log_tail=(stdout or "")[-2000:],
            timed_out=timed_out,
        )
