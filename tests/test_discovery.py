"""Branch 2: search providers, query planning, yield loop."""
from types import SimpleNamespace

import httpx
import respx

from intentlist.discovery.planner import ClaudePlanner, TemplatePlanner
from intentlist.discovery.search import BraveSearch
from intentlist.llm import ClaudeClient
from intentlist.yieldloop import YieldStat, rank_templates


@respx.mock
def test_brave_search_parses_results():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(return_value=httpx.Response(200, json={
        "web": {"results": [{"url": "https://f.com/t/1", "title": "WTB Daytona", "description": "d", "page_age": "x"}]}}))
    out = BraveSearch("key").search('"WTB" "Rolex"')
    assert out[0].url == "https://f.com/t/1" and out[0].title == "WTB Daytona"


@respx.mock
def test_brave_search_retries_rate_limit():
    route = respx.get("https://api.search.brave.com/res/v1/web/search")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json={"web": {"results": []}})]
    b = BraveSearch("key")
    b._get.retry.wait = lambda *a, **k: 0
    assert b.search("x") == []


def test_template_planner_targets_allowed_sites_only(tax):
    plans = TemplatePlanner(tax).plan(tax.categories["watches"], ["forum-a.test"], ["Rolex Daytona"])
    assert plans and all(p.site == "forum-a.test" and "site:forum-a.test" in p.text for p in plans)
    assert plans[0].template_key.startswith("wtb|rolex daytona|")  # proven items are planned first


def test_claude_planner_falls_back_when_llm_fails(settings, tax):
    def boom(**_):
        raise RuntimeError("api down")

    llm = ClaudeClient(settings, client=SimpleNamespace(messages=SimpleNamespace(create=boom)))
    items = ClaudePlanner(tax, llm).items(tax.categories["cars"], [])
    assert "Ferrari" in items  # template makers still planned


def test_rank_prefers_productive_templates():
    stats = {"good": YieldStat("good", 100, 90, 80, 60), "dead": YieldStat("dead", 100, 5, 0, 0)}
    assert all(rank_templates(["good", "dead"], stats, 1, seed=i)[0] == "good" for i in range(50))
    # unexplored templates still get tried against a mediocre one
    stats2 = {"meh": YieldStat("meh", 50, 10, 5, 2)}
    assert sum(rank_templates(["meh", "new"], stats2, 1, seed=i)[0] == "new" for i in range(200)) > 50
