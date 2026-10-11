"""Regression: Google Fonts query parameters must survive the pinned HTTP fetch."""

from app.core import selfcontained as sc


def test_google_fonts_query_is_preserved(monkeypatch):
    observed = {}

    class FakeResponse:
        status = 200

        def getheader(self, name, default=None):
            return {
                "Content-Type": "text/css",
                "Content-Length": "3",
            }.get(name, default)

        def read1(self, size):
            if not observed.get("read"):
                observed["read"] = True
                return b"abc"
            return b""

    class FakeConnection:
        sock = None

        def __init__(self, host, address, timeout):
            observed["host"] = host

        def request(self, method, target, headers):
            observed["method"] = method
            observed["target"] = target

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        sc,
        "_resolve_public_addresses",
        lambda host, resolver: ["142.250.0.1"],
    )

    url = (
        "https://fonts.googleapis.com/css2?"
        "family=Playfair+Display:ital,wght@0,400..900;1,400..900"
        "&family=Inter:wght@300..700&display=swap"
    )

    result = sc._fetch(
        url,
        hosts=("fonts.googleapis.com",),
        allowed_types=("text/css",),
        max_bytes=1024,
        magic=None,
        connection_factory=FakeConnection,
    )

    assert result == b"abc"
    assert observed["host"] == "fonts.googleapis.com"
    assert observed["method"] == "GET"
    assert observed["target"] == url.split("fonts.googleapis.com", 1)[1]


def test_google_fonts_url_without_query_remains_valid(monkeypatch):
    observed = {}

    class FakeResponse:
        status = 200

        def getheader(self, name, default=None):
            return {
                "Content-Type": "text/css",
                "Content-Length": "3",
            }.get(name, default)

        def read1(self, size):
            if not observed.get("read"):
                observed["read"] = True
                return b"abc"
            return b""

    class FakeConnection:
        sock = None

        def __init__(self, host, address, timeout):
            pass

        def request(self, method, target, headers):
            observed["target"] = target

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        sc,
        "_resolve_public_addresses",
        lambda host, resolver: ["142.250.0.1"],
    )

    sc._fetch(
        "https://fonts.googleapis.com/css2",
        hosts=("fonts.googleapis.com",),
        allowed_types=("text/css",),
        max_bytes=1024,
        magic=None,
        connection_factory=FakeConnection,
    )

    assert observed["target"] == "/css2"


def test_font_vendor_requests_modern_woff2_capable_user_agent():
    from app.core import selfcontained as sc

    assert "Mozilla/5.0" in sc._UA_FONT
    assert "Chrome/" in sc._UA_FONT
    assert "Safari/" in sc._UA_FONT
    assert sc._FONT_MAGIC["woff2"] == b"wOF2"
