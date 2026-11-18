"""Cursor CLI engine (Cursor account login): READ-ONLY on Windows.

Live guard-rail tests on this PC (2026-11-18) showed that, on Windows, Cursor's
file writes cannot be confined to the workspace:
  * `--force` + allow Write(**)          -> wrote a file OUTSIDE the workspace
  * no `--force` + allow Write(**)       -> still wrote outside
  * deny absolute Write(C:/**)           -> blocked inside writes too
  * `--sandbox enabled`                  -> "Sandbox requires macOS or Linux"
So write mode is refused (supports_write = False). Cursor is used for planning,
review, analysis and second opinions, in `--mode ask` (read-only).

Each run also gets a temporary `.cursor/cli.json` that denies shell, web, MCP,
writes and .env reads (this was verified to block shell: "Permission denied").
An existing project `.cursor/cli.json` is backed up and restored afterwards, and
the temporary file is listed in .git/info/exclude so it never shows in diffs.

node.exe (signed) is called directly with index.js, not via the .cmd launcher,
so the prompt never passes through cmd.exe parsing. The prompt goes on stdin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from workforce.engines.base import Engine, EngineUnavailable

_VERSION_DIR = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$")

READ_PERMISSIONS = {
    "allow": ["Read(**)"],
    "deny": ["Shell(*)", "WebFetch(*)", "Mcp(*:*)", "Write(**)",
             "Read(**/.env*)", "Read(**/*.pem)", "Read(**/*.key)"],
}
PERMISSIONS_REL = Path(".cursor") / "cli.json"


def _version_key(name: str) -> tuple[int, ...]:
    m = _VERSION_DIR.match(name)
    return tuple(int(g) for g in m.groups()[:3]) if m else (0,)


class CursorEngine(Engine):
    supports_write = False
    _backup: bytes | None = None
    _created_dir: bool = False

    def resolve_binary(self) -> Path:
        if self.spec.binary:
            path = Path(self.spec.binary)
            if path.is_file():
                return path
            raise EngineUnavailable(f"cursor node.exe not found: {path}")
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
        versions = sorted((d for d in root.glob("*") if d.is_dir() and _VERSION_DIR.match(d.name)),
                          key=lambda d: _version_key(d.name))
        if not versions:
            raise EngineUnavailable("Cursor CLI not installed (see cursor.com/docs/cli)")
        node = versions[-1] / "node.exe"
        if not (node.is_file() and (versions[-1] / "index.js").is_file()):
            raise EngineUnavailable(f"incomplete Cursor install: {versions[-1]}")
        return node

    def command(self, binary: Path, workdir: Path, *, write: bool,
                out_file: Path) -> list[str]:
        cmd = [str(binary), str(binary.parent / "index.js"),
               "--print", "--output-format", "json", "--trust",
               "--workspace", str(workdir), "--mode", "ask"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd  # prompt is read from stdin

    def extra_env(self) -> dict[str, str]:
        return {"CURSOR_INVOKED_AS": "cursor-agent"}

    def final_message(self, stdout: str, out_file: Path) -> str:
        for line in reversed(stdout.strip().splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return "" if event.get("is_error") else str(event.get("result", "")).strip()
        return ""

    # ---- per-run permission file ----

    def prepare(self, workdir: Path, *, write: bool) -> None:
        path = workdir / PERMISSIONS_REL
        self._created_dir = not path.parent.exists()
        self._backup = path.read_bytes() if path.is_file() else None
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"permissions": READ_PERMISSIONS}, indent=2),
                        encoding="utf-8")
        _git_exclude(workdir, PERMISSIONS_REL.as_posix())

    def cleanup(self, workdir: Path) -> None:
        path = workdir / PERMISSIONS_REL
        if self._backup is not None:
            path.write_bytes(self._backup)
        else:
            path.unlink(missing_ok=True)
            if self._created_dir:
                try:
                    path.parent.rmdir()
                except OSError:
                    pass  # something else is there; let the diff show it


def _git_exclude(workdir: Path, pattern: str) -> None:
    """Add `pattern` to the repo's info/exclude (works for worktrees too)."""
    try:
        res = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"],
                             cwd=workdir, capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return
    if res.returncode != 0:
        return  # not a git repo
    exclude = Path(res.stdout.strip())
    if not exclude.is_absolute():
        exclude = workdir / exclude
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
