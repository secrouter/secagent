"""Tests for pi-compatible secret-value resolution (literal / $VAR / !command)."""

from __future__ import annotations

import pytest

from secagent.secretval import SecretResolutionError, resolve_secret


def test_empty_value_passthrough():
    assert resolve_secret("") == ""


def test_plain_literal_passthrough():
    assert resolve_secret("not-needed") == "not-needed"
    assert resolve_secret("sk-ant-literal") == "sk-ant-literal"


def test_env_var_dollar_brace(monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_KEY", "abc123")
    assert resolve_secret("${SECAGENT_TEST_KEY}") == "abc123"


def test_env_var_dollar_plain(monkeypatch):
    monkeypatch.setenv("SECAGENT_TEST_KEY", "abc123")
    assert resolve_secret("$SECAGENT_TEST_KEY") == "abc123"


def test_env_var_interpolation_inside_larger_literal(monkeypatch):
    monkeypatch.setenv("KEY_PREFIX", "sk")
    monkeypatch.setenv("KEY_SUFFIX", "live")
    assert resolve_secret("${KEY_PREFIX}_${KEY_SUFFIX}") == "sk_live"


def test_missing_env_var_raises():
    with pytest.raises(SecretResolutionError, match="NOPE_NOT_SET_XYZ"):
        resolve_secret("$NOPE_NOT_SET_XYZ")


def test_command_execution_captures_stdout_stripped():
    assert resolve_secret("!echo dynamic-token") == "dynamic-token"


def test_command_execution_runs_a_real_pipeline():
    # Proves the whole remainder is handed to a real shell (pi's documented
    # behavior), not just a bare argv[0] executed literally.
    assert resolve_secret("!printf 'a-%s-b' mid") == "a-mid-b"


def test_command_nonzero_exit_raises():
    with pytest.raises(SecretResolutionError, match="exited"):
        resolve_secret("!exit 1")


def test_command_not_found_raises():
    with pytest.raises(SecretResolutionError):
        resolve_secret("!this-command-does-not-exist-anywhere-xyz")


def test_dollar_dollar_escape_is_literal_dollar():
    assert resolve_secret("$$literal-dollar-prefix") == "$literal-dollar-prefix"


def test_dollar_bang_escape_is_literal_bang_not_a_command():
    # Without the escape, a leading "!" would execute; "$!" must NOT.
    assert resolve_secret("$!literal-bang-prefix") == "!literal-bang-prefix"


def test_bare_bang_always_means_command_even_when_command_fails():
    # A literal string that merely starts with "!" (no $ escape) is always a command.
    with pytest.raises(SecretResolutionError):
        resolve_secret("!this-command-does-not-exist-anywhere-xyz")
