"""Search providers. Each returns candidate URLs; nothing is fetched here."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..settings import Settings


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    age: str | None = None


class SearchProvider(Protocol):
    name: str
    cost_per_call: float

    def search(self, query: str, count: int = 20) -> list[SearchResult]: ...


class SearchError(RuntimeError):
    pass


class _Retryable(Exception):
    pass


class BraveSearch:
    """Brave Search API (web). https://api.search.brave.com/app/documentation/web-search"""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, cost_per_call: float = 0.005, client: httpx.Client | None = None):
        if not api_key:
            raise SearchError("INTENTLIST_BRAVE_API_KEY is not set")
        self.api_key = api_key
        self.cost_per_call = cost_per_call
        self.client = client or httpx.Client(timeout=20)

    @retry(retry=retry_if_exception_type(_Retryable), wait=wait_exponential(min=1, max=30), stop=stop_after_attempt(4),
           reraise=True)
    def _get(self, params: dict) -> dict:
        resp = self.client.get(self.endpoint, params=params, headers={
            "Accept": "application/json", "X-Subscription-Token": self.api_key})
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"brave {resp.status_code}")
        if resp.status_code != 200:
            raise SearchError(f"brave search failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def search(self, query: str, count: int = 20) -> list[SearchResult]:
        data = self._get({"q": query, "count": min(count, 20), "safesearch": "off", "text_decorations": "false"})
        out = []
        for r in (data.get("web") or {}).get("results", []):
            if r.get("url"):
                out.append(SearchResult(url=r["url"], title=r.get("title", ""), snippet=r.get("description", ""),
                                        age=r.get("page_age") or r.get("age")))
        return out


class FixtureSearch:
    """Offline search over the fixture corpus (tests and demos). Scores pages by term overlap."""

    name = "fixture"
    cost_per_call = 0.0

    def __init__(self, fixture_dir: Path):
        index_file = Path(fixture_dir) / "pages" / "index.json"
        self.index = json.loads(index_file.read_text()) if index_file.exists() else {}
        self.pages_dir = Path(fixture_dir) / "pages"

    def search(self, query: str, count: int = 20) -> list[SearchResult]:
        site = None
        m = re.search(r"site:(\S+)", query)
        if m:
            site = m.group(1)
            query = query.replace(m.group(0), "")
        terms = [t.lower() for t in re.findall(r"[\w-]+", query) if len(t) > 1]
        scored = []
        for url, meta in self.index.items():
            if site and site not in url:
                continue
            text = (self.pages_dir / meta["file"]).read_text(encoding="utf-8").lower()
            score = sum(1 for t in terms if t in text)
            if terms and score == len(terms):
                scored.append((score, url, meta))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [SearchResult(url=u, title=m.get("title", ""), snippet=m.get("snippet", "")) for _, u, m in scored[:count]]


def build_search_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "brave":
        return BraveSearch(settings.brave_api_key or "", settings.cost_search_call)
    if settings.search_provider == "fixture":
        return FixtureSearch(settings.fixture_dir)
    raise SearchError(f"unknown search provider {settings.search_provider!r}")
