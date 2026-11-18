"""Tools that CrewAI workers may use. Each one is scoped by construction:
file tools are bound to one folder (the task worktree) and refuse escapes."""

from __future__ import annotations

from pathlib import Path

from workforce.tools.fs import ListFilesTool, ReadFileTool, WriteFileTool
from workforce.tools.web import WebSearchTool


def build_tools(names: list[str], *, workdir: Path | None, denied_globs: list[str],
                allow_write: bool, search_log: Path | None = None) -> list:
    """CrewAI tool objects for the names in agents.yaml."""
    tools: list = []
    if "web_search" in names:
        tools.append(WebSearchTool(log_path=str(search_log) if search_log else None))
    if "fs_read" in names and workdir is not None:
        tools += [ListFilesTool(root=str(workdir), denied_globs=denied_globs),
                  ReadFileTool(root=str(workdir), denied_globs=denied_globs)]
    if "worktree_write" in names and workdir is not None and allow_write:
        tools.append(WriteFileTool(root=str(workdir), denied_globs=denied_globs))
    return tools


__all__ = ["ListFilesTool", "ReadFileTool", "WebSearchTool", "WriteFileTool", "build_tools"]
