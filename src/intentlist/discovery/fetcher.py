"""Compliant page fetching and immutable evidence capture.

Order of checks for every URL: source registry -> robots.txt -> per-domain rate limit -> fetch.
Nothing behind a login is fetched: no cookies or credentials are ever sent.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser

import httpx
from selectolax.parser import HTMLParser

from ..models import Source
from ..normalize import domain_matches, sha256_text
from ..settings import Settings

log = logging.getLogger(__name__)

_MAILTO = re.compile(r'<a\b[^>]*href=["\']mailto:([^"\'?]+)[^"\']*["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


@dataclass
class FetchResult:
    ok: bool
    status: str  # fetched | blocked | failed
    http_status: int | None = None
    html: str | None = None
    final_url: str | None = None
    screenshot: bytes | None = None
    reason: str | None = None


class SourceRegistry:
    def __init__(self, sources: list[Source]):
        self.sources = sorted(sources, key=lambda s: -len(s.domain))  # most specific first

    def lookup(self, domain: str) -> Source | None:
        return next((s for s in self.sources if domain_matches(domain, s.domain)), None)

    def gate(self, domain: str) -> tuple[Source | None, str | None]:
        source = self.lookup(domain)
        if source is None:
            return None, "not_in_registry"
        if not source.allowed:
            return source, "source_not_allowed"
        return source, None

    def allowed_sites(self, category_slug: str) -> list[str]:
        return [s.domain for s in self.sources if s.allowed and (not s.categories or category_slug in s.categories)]


class RateLimiter:
    def __init__(self, default_rps: float):
        self.default_rps = default_rps
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str, rps: float | None) -> None:
        interval = 1.0 / (rps or self.default_rps)
        with self._lock:
            now = time.monotonic()
            ready_at = self._last.get(domain, 0.0) + interval
            delay = max(0.0, ready_at - now)
            self._last[domain] = now + delay
        if delay:
            time.sleep(delay)


class RobotsCache:
    def __init__(self, client: httpx.Client, user_agent: str):
        self.client, self.user_agent = client, user_agent
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str, base: str) -> bool:
        if base not in self._cache:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.client.get(f"{base}/robots.txt", headers={"User-Agent": self.user_agent})
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError:
                rp = None  # unknown -> be conservative
            self._cache[base] = rp
        rp = self._cache[base]
        return bool(rp and rp.can_fetch(self.user_agent, url))


class Fetcher:
    def __init__(self, settings: Settings, registry: SourceRegistry, client: httpx.Client | None = None):
        self.settings = settings
        self.registry = registry
        self.client = client or httpx.Client(timeout=settings.request_timeout_s, follow_redirects=True,
                                             headers={"User-Agent": settings.user_agent})
        self.robots = RobotsCache(self.client, settings.user_agent)
        self.limiter = RateLimiter(settings.default_rate_limit_rps)
        self._fixture_index: dict | None = None

    # ------------------------------------------------------------------
    def fetch(self, url: str, domain: str) -> tuple[Source | None, FetchResult]:
        source, block = self.registry.gate(domain)
        if block:
            return source, FetchResult(ok=False, status="blocked", reason=block)
        if self.settings.fetch_mode == "fixture":
            return source, self._fetch_fixture(url)

        parts = httpx.URL(url)
        base = f"{parts.scheme}://{parts.host}" + (f":{parts.port}" if parts.port else "")
        if source.respect_robots and not self.robots.allowed(url, base):
            return source, FetchResult(ok=False, status="blocked", reason="robots_disallow")
        self.limiter.wait(source.domain, source.rate_limit_rps)
        if source.render_js:
            return source, self._fetch_browser(url)
        try:
            with self.client.stream("GET", url) as resp:
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200:
                    return source, FetchResult(False, "failed", resp.status_code, reason=f"http_{resp.status_code}")
                if "html" not in ctype and "text" not in ctype:
                    return source, FetchResult(False, "failed", resp.status_code, reason=f"content_type:{ctype}")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self.settings.max_page_bytes:
                        break
                    chunks.append(chunk)
                html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                return source, FetchResult(True, "fetched", resp.status_code, html, str(resp.url))
        except httpx.HTTPError as exc:
            return source, FetchResult(False, "failed", reason=f"{type(exc).__name__}: {exc}"[:500])

    def _fetch_fixture(self, url: str) -> FetchResult:
        pages = Path(self.settings.fixture_dir) / "pages"
        if self._fixture_index is None:
            idx = pages / "index.json"
            self._fixture_index = json.loads(idx.read_text()) if idx.exists() else {}
        meta = self._fixture_index.get(url)
        if not meta:
            return FetchResult(False, "failed", 404, reason="http_404")
        return FetchResult(True, "fetched", 200, (pages / meta["file"]).read_text(encoding="utf-8"), url)

    def _fetch_browser(self, url: str) -> FetchResult:  # pragma: no cover - needs a browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(False, "failed", reason="playwright not installed (pip install .[browser])")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=self.settings.user_agent)
                resp = page.goto(url, wait_until="networkidle", timeout=self.settings.request_timeout_s * 1000)
                status = resp.status if resp else None
                if status != 200:
                    return FetchResult(False, "failed", status, reason=f"http_{status}")
                return FetchResult(True, "fetched", status, page.content(), page.url,
                                   screenshot=page.screenshot(full_page=True))
            finally:
                browser.close()


# ----------------------------------------------------------------------
def html_to_text(html: str) -> tuple[str, str | None]:
    """Readable text with mailto addresses made visible, so the evidence lock can see them."""
    html = _MAILTO.sub(lambda m: f"{m.group(2)} &lt;{m.group(1)}&gt;" if m.group(1).lower() not in m.group(2).lower()
                       else m.group(2), html)
    tree = HTMLParser(html)
    title = tree.css_first("title").text(strip=True) if tree.css_first("title") else None
    for node in tree.css("script, style, noscript, svg, iframe, form, nav, footer"):
        node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, title


class CaptureStore:
    """Content-addressed files: <dir>/<sha[:2]>/<sha>.html|.txt|.png"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, html: str, text: str, screenshot: bytes | None = None) -> dict:
        sha = sha256_text(html)
        folder = self.root / sha[:2]
        folder.mkdir(parents=True, exist_ok=True)
        html_path, text_path = folder / f"{sha}.html", folder / f"{sha}.txt"
        if not html_path.exists():
            html_path.write_text(html, encoding="utf-8")
            text_path.write_text(text, encoding="utf-8")
        shot_path = None
        if screenshot:
            shot_path = folder / f"{sha}.png"
            shot_path.write_bytes(screenshot)
        return {"sha": sha, "html_path": str(html_path), "text_path": str(text_path),
                "screenshot_path": str(shot_path) if shot_path else None}

    @staticmethod
    def read_text(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")
