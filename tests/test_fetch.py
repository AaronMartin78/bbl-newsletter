import httpx

from bbl_newsletter.fetch import Fetcher

UA = "bbl-newsletter-collector/0.1 (test)"


def make_fetcher(handler, **kw):
    return Fetcher(
        user_agent=UA,
        min_host_interval_s=0.0,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_robots_disallow_blocks_fetch():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /news/")
        return httpx.Response(200, text="should never be fetched")

    f = make_fetcher(handler)
    r = f.get("https://example.de/news/artikel")
    assert r.robots_denied and r.content is None
    assert f.get("https://example.de/ok").status == 200


def test_robots_unreachable_denies_run():
    def handler(request):
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="x")

    f = make_fetcher(handler)
    assert f.get("https://example.de/news").robots_denied


def test_robots_4xx_allows_fetch():
    # The games API answers 401 to every path, robots.txt included; RFC 9309
    # treats an unavailable (4xx) robots.txt as no restrictions.
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(401)
        return httpx.Response(200, text="ok")

    f = make_fetcher(handler)
    assert f.get("https://api.example.de/games").status == 200


def test_robots_5xx_denies_run():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, text="x")

    f = make_fetcher(handler)
    assert f.get("https://example.de/news").robots_denied


def test_conditional_get_304():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers.get("If-None-Match") == 'W/"abc"':
            return httpx.Response(304)
        return httpx.Response(200, text="body", headers={"ETag": 'W/"abc"'})

    f = make_fetcher(handler)
    first = f.get("https://example.de/feed")
    assert first.status == 200 and first.etag == 'W/"abc"'
    second = f.get("https://example.de/feed", etag=first.etag)
    assert second.not_modified and second.content is None


def test_max_bytes_enforced():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 2048)

    f = make_fetcher(handler, max_bytes=1024)
    r = f.get("https://example.de/big")
    assert r.content is None and "exceeded" in r.error
