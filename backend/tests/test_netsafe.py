"""SSRF protection for the caller-supplied image_url."""
import socket

import pytest

from fleet import netsafe


def _resolve_to(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, port))])


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)",
    "", None, "not a url", "http://", "//example.com/x",
])
def test_non_http_urls_are_refused(url):
    assert netsafe.is_public_http_url(url) is False


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
    "0.0.0.0", "100.64.0.1", "::1", "fe80::1", "fc00::1", "224.0.0.1",
])
def test_internal_addresses_are_refused(monkeypatch, ip):
    _resolve_to(monkeypatch, ip)
    assert netsafe.is_public_http_url("http://anything.example/x.jpg") is False


def test_public_address_is_allowed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert netsafe.is_public_http_url("https://example.com/a.jpg") is True


def test_a_host_with_any_internal_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ])
    assert netsafe.is_public_http_url("http://rebind.example/") is False


def test_unresolvable_host_is_refused(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert netsafe.is_public_http_url("http://nope.invalid/") is False


class _Resp:
    def __init__(self, status=200, headers=None, chunks=(b"data",)):
        self.status_code, self.headers, self._chunks = status, headers or {}, chunks
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(netsafe, "is_public_http_url", lambda url: "internal" not in url)


def _get(monkeypatch, responses):
    calls, it = [], iter(responses)

    def fake(url, **kw):
        calls.append((url, kw))
        return next(it)
    monkeypatch.setattr(netsafe.requests, "get", fake)
    return calls


def test_fetch_returns_body(public, monkeypatch):
    calls = _get(monkeypatch, [_Resp(chunks=(b"ab", b"cd"))])
    assert netsafe.fetch_public_image("https://img.example/a.jpg") == b"abcd"
    assert calls[0][1]["allow_redirects"] is False and calls[0][1]["timeout"] == 10


def test_fetch_refuses_internal_url_without_any_request(public, monkeypatch):
    calls = _get(monkeypatch, [])
    with pytest.raises(ValueError):
        netsafe.fetch_public_image("http://internal.example/a.jpg")
    assert calls == []


def test_redirect_to_internal_address_is_blocked(public, monkeypatch):
    calls = _get(monkeypatch, [_Resp(302, {"Location": "http://internal.example/secret"})])
    with pytest.raises(ValueError):
        netsafe.fetch_public_image("https://img.example/a.jpg")
    assert len(calls) == 1                      # the internal hop was never requested


def test_safe_redirects_are_followed_and_relative_ones_resolved(public, monkeypatch):
    calls = _get(monkeypatch, [_Resp(301, {"Location": "/b.jpg"}), _Resp(chunks=(b"ok",))])
    assert netsafe.fetch_public_image("https://img.example/a.jpg") == b"ok"
    assert calls[1][0] == "https://img.example/b.jpg"


def test_redirect_loops_are_cut_off(public, monkeypatch):
    _get(monkeypatch, [_Resp(302, {"Location": "https://img.example/a.jpg"})] * 10)
    with pytest.raises(ValueError, match="redirect"):
        netsafe.fetch_public_image("https://img.example/a.jpg")


def test_redirect_without_location_is_an_error(public, monkeypatch):
    _get(monkeypatch, [_Resp(302, {})])
    with pytest.raises(ValueError):
        netsafe.fetch_public_image("https://img.example/a.jpg")


def test_oversized_bodies_are_refused(public, monkeypatch):
    _get(monkeypatch, [_Resp(headers={"Content-Length": "999999999"})])
    with pytest.raises(ValueError, match="too large"):
        netsafe.fetch_public_image("https://img.example/a.jpg", max_bytes=1000)
    _get(monkeypatch, [_Resp(chunks=(b"x" * 600, b"x" * 600))])       # lies about / omits the length
    with pytest.raises(ValueError, match="too large"):
        netsafe.fetch_public_image("https://img.example/a.jpg", max_bytes=1000)


def test_http_errors_propagate_and_connection_is_closed(public, monkeypatch):
    resp = _Resp(500)
    _get(monkeypatch, [resp])
    with pytest.raises(RuntimeError):
        netsafe.fetch_public_image("https://img.example/a.jpg")
    assert resp.closed
