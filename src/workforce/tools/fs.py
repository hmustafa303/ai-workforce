"""File tools bound to one folder (the task worktree)."""

from __future__ import annotations

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from workforce.sandbox import SandboxError, is_denied, safe_join

MAX_READ_CHARS = 40_000
MAX_WRITE_CHARS = 200_000
MAX_LIST = 400
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
             ".ruff_cache", ".mypy_cache", "dist", "build", ".next"}


class _Scoped(BaseTool):
    root: str
    denied_globs: list[str] = Field(default_factory=list)

    def _path(self, rel: str) -> Path:
        return safe_join(Path(self.root), rel or ".", self.denied_globs)


class _ListArgs(BaseModel):
    directory: str = Field(default=".", description="folder relative to the project root")


class ListFilesTool(_Scoped):
    name: str = "list_files"
    description: str = ("List files (recursively) under a folder of the project. "
                        "Paths are relative to the project root.")
    args_schema: type[BaseModel] = _ListArgs

    def _run(self, directory: str = ".") -> str:
        try:
            base = self._path(directory)
        except SandboxError as exc:
            return f"ERROR: {exc}"
        if not base.is_dir():
            return f"ERROR: not a folder: {directory}"
        root = Path(self.root).resolve()
        out: list[str] = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if p.is_file() and not is_denied(rel, self.denied_globs):
                out.append(rel)
                if len(out) >= MAX_LIST:
                    out.append(f"... (stopped at {MAX_LIST} files; list a subfolder)")
                    break
        return "\n".join(out) or "(no files)"


class _ReadArgs(BaseModel):
    path: str = Field(description="file path relative to the project root")


class ReadFileTool(_Scoped):
    name: str = "read_file"
    description: str = "Read a text file of the project. Path relative to the project root."
    args_schema: type[BaseModel] = _ReadArgs

    def _run(self, path: str) -> str:
        try:
            target = self._path(path)
        except SandboxError as exc:
            return f"ERROR: {exc}"
        if not target.is_file():
            return f"ERROR: no such file: {path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n... [truncated at {MAX_READ_CHARS} chars]"
        return text


class _WriteArgs(BaseModel):
    path: str = Field(description="file path relative to the project root")
    content: str = Field(description="the COMPLETE new content of the file")


class WriteFileTool(_Scoped):
    name: str = "write_file"
    description: str = ("Create or overwrite a file in the task workspace with the complete "
                        "new content. Path relative to the project root. You cannot write "
                        "outside the workspace.")
    args_schema: type[BaseModel] = _WriteArgs

    def _run(self, path: str, content: str) -> str:
        if len(content) > MAX_WRITE_CHARS:
            return f"ERROR: content too large (> {MAX_WRITE_CHARS} chars)"
        try:
            target = self._path(path)
        except SandboxError as exc:
            return f"ERROR: {exc}"
        if target.is_dir():
            return f"ERROR: {path} is a folder"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return f"wrote {len(content)} chars to {path}"
