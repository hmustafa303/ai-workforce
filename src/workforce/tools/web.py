"""Free web search (DuckDuckGo via `ddgs`) for the research worker."""

from __future__ import annotations

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

MAX_RESULTS = 8


class _SearchArgs(BaseModel):
    query: str = Field(description="search query")
    max_results: int = Field(default=5, ge=1, le=MAX_RESULTS)


def search(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS

    hits = DDGS().text(query, max_results=min(max_results, MAX_RESULTS)) or []
    return [{"title": h.get("title", ""), "url": h.get("href") or h.get("url", ""),
             "snippet": h.get("body", "")} for h in hits]


class WebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = ("Search the web. Returns title, URL and snippet per result. "
                        "Cite the URLs you rely on.")
    args_schema: type[BaseModel] = _SearchArgs
    log_path: str | None = None  # every returned URL is appended here (source checking)

    def _run(self, query: str, max_results: int = 5) -> str:
        try:
            hits = search(query, max_results)
        except Exception as exc:  # noqa: BLE001 - network/provider errors go back to the agent
            return f"ERROR: search failed: {type(exc).__name__}: {str(exc)[:200]}"
        if not hits:
            return "no results"
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.writelines(h["url"] + "\n" for h in hits if h["url"])
        return "\n\n".join(f"[{i}] {h['title']}\n{h['url']}\n{h['snippet']}"
                           for i, h in enumerate(hits, 1))
