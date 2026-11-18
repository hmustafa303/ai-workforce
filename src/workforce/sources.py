"""Source checking for research reports (no CrewAI import: used by the MCP server)."""

from __future__ import annotations

import re
from pathlib import Path

_URL = re.compile(r"https?://[^\s)\]>\"'`]+")


def cited_urls(text: str) -> list[str]:
    return sorted({u.rstrip(".,;:") for u in _URL.findall(text or "")})


def unverified_sources(report: str, log_path: Path) -> list[str]:
    """URLs cited in the report that no web_search call ever returned (likely invented)."""
    if not log_path.is_file():
        return cited_urls(report)
    seen = {u.rstrip("/") for u in log_path.read_text(encoding="utf-8").split()}
    return [u for u in cited_urls(report) if u.rstrip("/") not in seen]
