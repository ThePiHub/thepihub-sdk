"""Transport security checks shared by the SDK's outbound connections (TLS is
mandatory unless a developer explicitly opts out).

The platform's stated position is that every transport channel is
encrypted - HTTPS, WSS, DTLS/SRTP. That is only true if something
actually refuses the alternatives, so plaintext becomes a choice a
developer has to make explicitly rather than a default they can reach by
mistyping a URL.

Loopback is exempt, deliberately: traffic to `localhost` never crosses a
network, so requiring a certificate for it would buy nothing and would
make local development need a TLS setup. Everything else needs `wss://`
or `https://`, or an explicit opt-out.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

SECURE_SCHEMES = frozenset({"https", "wss"})
INSECURE_SCHEMES = frozenset({"http", "ws"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

ALLOW_INSECURE_ENV = "PIHUB_ALLOW_INSECURE"


class InsecureTransportError(ValueError):
    """Raised when a plaintext URL is used where TLS is required."""


def insecure_allowed_by_env() -> bool:
    """Whether `PIHUB_ALLOW_INSECURE=1` is set in this process.

    The environment variable is the deliberate opt-out - one place, and
    visible in a process listing, rather than a quiet keyword argument
    somewhere in application code.
    """
    return os.environ.get(ALLOW_INSECURE_ENV, "0") == "1"


def is_loopback(host: str | None) -> bool:
    return (host or "").strip("[]").lower() in LOOPBACK_HOSTS


def require_secure_url(url: str, *, allow_insecure: bool | None = None) -> None:
    """Raise `InsecureTransportError` if `url` is plaintext and not exempt.

    `allow_insecure=None` (the default) means "decide from the
    environment", so a caller that has no opinion inherits the
    deployment's. An explicit `True` always wins - that is what tests and
    deliberate local experiments use.
    """
    if allow_insecure is None:
        allow_insecure = insecure_allowed_by_env()
    if allow_insecure:
        return

    parsed = urlparse(url)
    if parsed.scheme not in INSECURE_SCHEMES:
        return
    if is_loopback(parsed.hostname):
        return

    secure = "wss" if parsed.scheme == "ws" else "https"
    raise InsecureTransportError(
        f"refusing to use plaintext {parsed.scheme}:// for {parsed.hostname!r}: "
        f"use {secure}:// instead, or set {ALLOW_INSECURE_ENV}=1 to override for local development"
    )
