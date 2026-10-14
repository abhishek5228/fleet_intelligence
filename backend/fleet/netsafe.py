"""Safe fetching of user-supplied URLs (SSRF protection).

`image_url` on /api/triage is chosen by the caller, and the dashboard's API key ships in the
browser bundle, so the server must never be tricked into fetching internal addresses such as
127.0.0.1, a private network or a cloud metadata endpoint (169.254.169.254).

Every hop (including redirects, which are followed manually) is checked: http/https only, and
every address the hostname resolves to must be a public one.
"""
import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3


def is_public_http_url(url: Optional[str]) -> bool:
    """True only for an http(s) URL whose host resolves exclusively to public addresses."""
    try:
        parsed = urlparse(url or "")
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            return False
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (ValueError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def fetch_public_image(url: str, *, timeout: int = 10, max_bytes: int = MAX_BYTES) -> bytes:
    """Download `url` and return its bytes. Raises ValueError for an unsafe URL or oversized body."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_http_url(current):
            raise ValueError("refusing to fetch a non-public or non-http(s) URL")
        resp = requests.get(current, timeout=timeout, allow_redirects=False, stream=True)
        try:
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise ValueError("redirect without a Location header")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValueError("response body too large")
            body = b"".join(_chunks(resp, max_bytes))
            return body
        finally:
            resp.close()
    raise ValueError("too many redirects")


def _chunks(resp: "requests.Response", max_bytes: int):
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response body too large")
        yield chunk
