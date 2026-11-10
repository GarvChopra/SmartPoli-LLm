import httpx
import pytest

from r2.tools import web

SEARCH_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&rut=abc">First <b>hit</b></a>
  <a class="result__snippet" href="x">Snippet one.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.net/b">Second</a>
  <a class="result__snippet" href="x">Snippet two.</a>
</div>
</body></html>
"""


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_web_search_parses_titles_urls_and_snippets():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params.get("q")
        return httpx.Response(200, text=SEARCH_HTML)

    out = web.web_search("python news", client=client_for(handler))
    assert seen["q"] == "python news"
    assert "1. First hit" in out
    assert "https://example.org/a" in out
    assert "Snippet one." in out
    assert "2. Second" in out and "https://example.net/b" in out


def test_web_search_with_no_results():
    out = web.web_search("zzz", client=client_for(lambda r: httpx.Response(200, text="<html></html>")))
    assert out == "No results"


def test_web_fetch_strips_markup_and_labels_content_as_data():
    html = "<html><head><style>p{}</style><script>evil()</script></head><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>"
    out = web.web_fetch("https://93.184.216.34/page", client=client_for(lambda r: httpx.Response(200, text=html)))
    assert "Title" in out and "Hello world" in out
    assert "evil()" not in out and "p{}" not in out
    assert out.startswith("[web content")


def test_web_fetch_truncates():
    out = web.web_fetch(
        "https://93.184.216.34/", max_chars=20,
        client=client_for(lambda r: httpx.Response(200, text="<p>" + "a" * 500 + "</p>")),
    )
    assert "truncated" in out


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765/",
        "http://127.0.0.1/",
        "http://192.168.1.5/",
        "http://10.0.0.2/x",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "file:///C:/Windows/win.ini",
        "ftp://example.com/",
        "javascript:alert(1)",
    ],
)
def test_web_fetch_refuses_local_and_non_http_urls(url):
    def handler(request):
        raise AssertionError("must not make a request")

    with pytest.raises(ValueError):
        web.web_fetch(url, client=client_for(handler))


def test_web_fetch_refuses_redirect_to_private_address():
    def handler(request):
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api"})
        raise AssertionError("followed a redirect to a private address")

    with pytest.raises(ValueError):
        web.web_fetch("https://93.184.216.34/", client=client_for(handler))


def test_web_fetch_follows_safe_redirect():
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "https://93.184.216.35/b"})
        return httpx.Response(200, text="<p>landed</p>")

    assert "landed" in web.web_fetch("https://93.184.216.34/a", client=client_for(handler))


# ---------- exfiltration guards ----------
def test_long_urls_need_approval_short_ones_do_not():
    from r2.types import Risk

    assert web.classify_url({"url": "https://example.org/page?q=hello"}).risk is Risk.SAFE
    long_url = "https://example.org/?d=" + "A" * 400
    info = web.classify_url({"url": long_url})
    assert info.risk is Risk.CONFIRM and info.action_type == "long_url"


def test_long_search_queries_need_approval():
    from r2.types import Risk

    assert web.classify_query({"query": "python news"}).risk is Risk.SAFE
    assert web.classify_query({"query": "x" * 300}).risk is Risk.CONFIRM
