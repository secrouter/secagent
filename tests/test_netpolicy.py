"""Tests for the network egress policy (CMMC-3)."""

from __future__ import annotations

import pytest

from secagent.config import Settings
from secagent.netpolicy import NetworkPolicyError, check_endpoint, enforce, host_of, is_loopback


def test_host_and_loopback():
    assert host_of("https://gitlab.example.com:443/api") == "gitlab.example.com"
    assert is_loopback("localhost")
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert not is_loopback("gitlab.example.com")


def test_policy_disabled_allows_anything():
    net = Settings().network  # require_tls=False, no allow-list
    assert check_endpoint("http://anywhere:8000/v1", net)[0]


def test_require_tls_rejects_plaintext_but_allows_loopback():
    s = Settings()
    s.network.require_tls = True
    assert not check_endpoint("http://remote-host:8000/v1", s.network)[0]
    assert check_endpoint("https://remote-host:8000/v1", s.network)[0]
    # Loopback is exempt — traffic never leaves the host.
    assert check_endpoint("http://localhost:8000/v1", s.network)[0]
    assert check_endpoint("http://127.0.0.1:8000/v1", s.network)[0]


def test_allowed_hosts_enforced():
    s = Settings()
    s.network.allowed_hosts = ["gemma.internal", "gitlab.internal"]
    ok, _ = check_endpoint("https://evil.example.com/v1", s.network)
    assert not ok
    assert check_endpoint("https://gemma.internal/v1", s.network)[0]


def test_enforce_raises_on_violation():
    s = Settings()
    s.network.require_tls = True
    s.llm.base_url = "http://remote-gemma:8000/v1"
    with pytest.raises(NetworkPolicyError):
        enforce(s)


def test_enforce_passes_for_https_and_loopback():
    s = Settings()
    s.network.require_tls = True
    s.llm.base_url = "http://localhost:8000/v1"   # loopback exempt
    s.gitlab.url = "https://gitlab.internal"
    enforce(s)  # should not raise
