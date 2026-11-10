import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from r2.tools import browser
from r2.types import Risk

PAGE = b"""<!doctype html><html><head><title>Contact page</title></head><body>
<h1>Contact</h1>
<form onsubmit="document.getElementById('out').textContent='submitted:'+document.getElementById('n').value; return false">
<label for="n">Name</label><input id="n">
<label for="pw">Password</label><input id="pw" type="password">
<label for="c">Country</label><select id="c"><option>India</option><option>Nepal</option></select>
<label><input type="checkbox" id="t"> Agree</label>
<button type="button" id="next" onclick="document.getElementById('out').textContent='next'">Next</button>
<button type="submit">Submit</button>
<button type="button">Go and submit</button>
</form><div id="out"></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture(scope="module")
def session(tmp_path_factory, site):
    s = browser.BrowserSession(tmp_path_factory.mktemp("profile"), headless=True)
    try:
        s.open(site)
    except Exception as exc:  # no usable Chrome/Edge on this machine
        s.close()
        pytest.skip(f"no browser available: {exc}")
    yield s
    s.close()


def test_open_returns_title_and_url(session, site):
    out = session.open(site)
    assert "Contact page" in out and site in out


def test_read_lists_fields_and_buttons(session):
    out = session.read()
    assert "Contact" in out
    assert "Name" in out and "Country" in out
    assert "Submit" in out and "Next" in out


def test_fill_text_select_and_checkbox(session, site):
    session.open(site)
    assert "Name" in session.fill("Name", "Garv")
    session.fill("Country", "Nepal")
    session.fill("Agree", "yes")
    state = session.page_eval(
        "() => [document.getElementById('n').value, document.getElementById('c').value, document.getElementById('t').checked]"
    )
    assert state == ["Garv", "Nepal", True]


def test_fill_refuses_password_fields(session, site):
    session.open(site)
    with pytest.raises(PermissionError):
        session.fill("Password", "hunter2")


def test_fill_unknown_field_lists_available_ones(session, site):
    session.open(site)
    with pytest.raises(ValueError, match="Name"):
        session.fill("Nonexistent", "x")


def test_click_plain_button_and_submit(session, site):
    session.open(site)
    session.fill("Name", "Garv")
    session.click("Next")
    assert session.page_eval("() => document.getElementById('out').textContent") == "next"
    session.click("Submit")
    assert session.page_eval("() => document.getElementById('out').textContent") == "submitted:Garv"


def test_click_refuses_when_real_button_text_is_more_sensitive(session, site):
    session.open(site)
    with pytest.raises(PermissionError):
        session.click("Go")


def test_open_refuses_non_http_urls(session):
    for url in ("javascript:alert(1)", "file:///C:/Windows/win.ini", "chrome://settings"):
        with pytest.raises(ValueError):
            session.open(url)


@pytest.mark.parametrize(
    "text,risk,action",
    [
        ("Next", Risk.SAFE, None),
        ("Search", Risk.SAFE, None),
        ("Submit", Risk.CONFIRM, "submit"),
        ("Confirm booking", Risk.CONFIRM, "submit"),
        ("Post", Risk.CONFIRM, "post"),
        ("Publish now", Risk.CONFIRM, "post"),
        ("Share", Risk.CONFIRM, "post"),
        ("Send message", Risk.CONFIRM, "send"),
        ("Reply", Risk.CONFIRM, "send"),
        ("Pay now", Risk.CONFIRM, "payment"),
        ("Place order", Risk.CONFIRM, "payment"),
        ("Delete account", Risk.CONFIRM, "delete"),
    ],
)
def test_button_text_classification(text, risk, action):
    info = browser.classify_click({"text": text})
    assert (info.risk, info.action_type) == (risk, action)
