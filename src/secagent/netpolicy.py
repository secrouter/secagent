"""Network egress policy: TLS enforcement and an endpoint allow-list (CMMC-3).

Addresses AC.L2-3.1.3 (control flow of CUI), SC.L2-3.13.8 (transmit confidentiality),
and SC.L2-3.13.6 (deny by default). Enforcement is opt-in via the ``network`` config;
loopback endpoints are exempt because that traffic never leaves the host.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .config import Settings


class NetworkPolicyError(RuntimeError):
    """Raised when a configured endpoint violates the network policy."""


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_endpoint(url: str, net) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for one URL under the network policy."""
    if not url:
        return True, "empty"
    scheme = urlsplit(url).scheme.lower()
    host = host_of(url)
    loopback = is_loopback(host)
    if net.require_tls and scheme != "https" and not loopback:
        return False, f"plaintext endpoint not allowed under require_tls: {url}"
    if net.allowed_hosts and not loopback and host not in net.allowed_hosts:
        return False, f"host '{host}' not in network.allowed_hosts"
    return True, "ok"


def enforce(settings: Settings) -> None:
    """Validate the endpoints the agents will use; raise on the first violation."""
    net = settings.network
    if not net.require_tls and not net.allowed_hosts:
        return  # policy disabled
    for url in (settings.llm.base_url, settings.gitlab.url):
        ok, reason = check_endpoint(url, net)
        if not ok:
            raise NetworkPolicyError(reason)
