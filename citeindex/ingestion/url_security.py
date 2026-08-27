"""Small, shared URL policy for network-facing ingestion paths."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
_ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


class UnsafeUrlError(ValueError):
    """Raised when a URL violates the public HTTP(S) fetch policy."""


def validate_public_url(url: str, *, resolve: bool = True) -> str:
    """Validate an HTTP(S) URL and, by default, all of its resolved addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("only HTTP(S) URLs with a hostname are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL userinfo is not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _BLOCKED_HOSTNAMES:
        raise UnsafeUrlError("localhost URLs are not allowed")

    addresses: set[Any] = set()
    try:
        addresses.add(ipaddress.ip_address(hostname))
    except ValueError:
        if resolve:
            try:
                infos = socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            except OSError as exc:
                raise UnsafeUrlError("URL hostname could not be resolved") from exc
            addresses = {
                ipaddress.ip_address(info[4][0])
                for info in infos
                if info[4] and info[4][0]
            }

    if addresses and any(not address.is_global for address in addresses):
        raise UnsafeUrlError("private, loopback, link-local, or reserved addresses are not allowed")
    return url


def fetch_text(
    url: str,
    *,
    session: Any = requests,
    timeout: float = 30,
    user_agent: str = "CiteIndex/0.13",
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> str:
    """Fetch bounded HTML while validating every redirect target."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)
        response = session.get(
            current_url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            close = getattr(response, "close", None)
            if close:
                close()
            if not location:
                raise UnsafeUrlError("redirect response has no Location header")
            current_url = urljoin(current_url, location)
            continue

        try:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
                raise UnsafeUrlError(f"unsupported URL content type: {content_type}")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeUrlError("URL response exceeds the configured size limit")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(encoding, errors="replace")
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    raise UnsafeUrlError("too many URL redirects")
