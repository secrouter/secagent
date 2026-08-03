"""Tests for the FIPS runtime policy + the allow_non_fips escape hatch.

The CI/test host is not in FIPS mode, so these exercise the non-FIPS branches.
"""

from __future__ import annotations

import pytest

from secagent.config import Settings
from secagent.doctor import check_fips
from secagent.security import FIPSComplianceError, enforce_fips_policy, openssl_fips_enabled

NON_FIPS_HOST = not openssl_fips_enabled()


def test_default_runs_on_non_fips_host():
    # require_fips defaults False -> never blocks (current default behavior).
    enforce_fips_policy(require_fips=False, allow_non_fips=False)


@pytest.mark.skipif(not NON_FIPS_HOST, reason="requires a non-FIPS host")
def test_require_fips_blocks_non_fips_host():
    with pytest.raises(FIPSComplianceError):
        enforce_fips_policy(require_fips=True, allow_non_fips=False)


@pytest.mark.skipif(not NON_FIPS_HOST, reason="requires a non-FIPS host")
def test_allow_non_fips_permits_running():
    # The escape hatch: FIPS configured, but explicitly allowed on a non-FIPS host.
    enforce_fips_policy(require_fips=True, allow_non_fips=True)


def test_settings_expose_allow_non_fips():
    s = Settings()
    assert s.fips.allow_non_fips is False  # strict by default when require_fips set
    s.fips.allow_non_fips = True
    assert s.fips.allow_non_fips is True


@pytest.mark.skipif(not NON_FIPS_HOST, reason="requires a non-FIPS host")
def test_doctor_fips_check_warns_when_allowed():
    fail = check_fips(require=True, allow_non_fips=False)
    assert fail.ok is False  # hard fail without the escape hatch

    warn = check_fips(require=True, allow_non_fips=True)
    assert warn.ok is True
    assert warn.severity == "warn"
    assert "allow_non_fips" in warn.detail


def test_index_repo_blocked_without_allow(tmp_path):
    from pathlib import Path

    from secagent.affordances.api import index_repo

    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.fips.require_fips = True
    fixture = Path(__file__).parent / "fixtures" / "sample_repo"
    if NON_FIPS_HOST:
        with pytest.raises(FIPSComplianceError):
            index_repo(fixture, s)
        # With the escape hatch, indexing proceeds.
        s.fips.allow_non_fips = True
    report = index_repo(fixture, s)
    assert report["files_indexed"] >= 6
