"""Branch 3: compliant fetching and evidence capture."""
import httpx
import respx

from intentlist.discovery.fetcher import CaptureStore, Fetcher, SourceRegistry, html_to_text
from intentlist.models import Source


@respx.mock
def test_fetcher_registry_and_robots(settings):
    settings.fetch_mode = "http"
    reg = SourceRegistry([
        Source(id=1, domain="ok.example", name="ok", allowed=True, respect_robots=True, rate_limit_rps=100),
        Source(id=2, domain="off.example", name="off", allowed=False),
    ])
    respx.get("https://ok.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://ok.example/t/1").mock(return_value=httpx.Response(
        200, text="<html><body>WTB</body></html>", headers={"content-type": "text/html"}))
    f = Fetcher(settings, reg)
    assert f.fetch("https://ok.example/t/1", "ok.example")[1].status == "fetched"
    assert f.fetch("https://ok.example/private/2", "ok.example")[1].reason == "robots_disallow"
    assert f.fetch("https://off.example/t/1", "off.example")[1].reason == "source_not_allowed"
    assert f.fetch("https://unknown.example/t/1", "unknown.example")[1].reason == "not_in_registry"


def test_subdomains_inherit_registry_entry():
    reg = SourceRegistry([Source(id=1, domain="forum.example", name="f", allowed=True)])
    assert reg.gate("www.forum.example")[1] is None
    assert reg.gate("evilforum.example")[1] == "not_in_registry"


def test_html_to_text_exposes_mailto_and_drops_chrome():
    html = ('<html><head><title>T</title><script>x=1</script></head><body><nav>Menu</nav>'
            '<p>WTB Daytona</p><a href="mailto:a@b.org">email me</a><footer>info@site.test</footer></body></html>')
    text, title = html_to_text(html)
    assert title == "T" and "a@b.org" in text and "Menu" not in text and "info@site.test" not in text


def test_capture_store_is_content_addressed(tmp_path):
    store = CaptureStore(tmp_path)
    a = store.save("<p>x</p>", "x")
    b = store.save("<p>x</p>", "x")
    assert a == b and CaptureStore.read_text(a["text_path"]) == "x"
