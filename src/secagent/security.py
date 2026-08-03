"""FIPS-aware security helpers.

Everything that needs a digest in secagent routes through here so the codebase has a
single, audited hashing surface. We use SHA-256 exclusively for content addressing
and cache keys: it is FIPS 140-approved, unlike MD5/SHA-1 (forbidden) and BLAKE2
(fast but not FIPS-approved).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import ssl
from pathlib import Path

# Algorithms that are NOT permitted when running under FIPS. Used by the linter-style
# self-check in ``secagent doctor`` and by guards in this module.
FORBIDDEN_HASHES = ("md5", "sha1", "blake2b", "blake2s")


def content_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data`` (FIPS-approved)."""
    return hashlib.sha256(data).hexdigest()


def text_hash(text: str) -> str:
    return content_hash(text.encode("utf-8"))


def file_hash(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Stream a file through SHA-256 without loading it all into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(data: bytes | str, length: int = 12) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return content_hash(data)[:length]


def harden_path(path: str | Path, mode: int) -> bool:
    """Best-effort ``chmod`` for at-rest protection (CMMC-2 / MP, SC.3.13.16).

    Restricts access to the affordance store and audit log (e.g. ``0o700`` dirs,
    ``0o600`` files). No-op / False on platforms without POSIX permissions.
    """
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def secure_delete_file(path: str | Path) -> None:
    """Overwrite a file's bytes then unlink it (best-effort secure delete).

    Note: on copy-on-write filesystems and SSDs, overwrite-in-place is not a
    guaranteed erase; full-disk/volume encryption remains the primary at-rest control.
    """
    p = Path(path)
    with contextlib.suppress(OSError):
        size = p.stat().st_size
        with open(p, "r+b", buffering=0) as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    with contextlib.suppress(OSError):
        p.unlink()


def openssl_fips_enabled() -> bool:
    """Best-effort detection of whether the linked OpenSSL is in FIPS mode.

    On OpenSSL 3.x FIPS is provider-based. We probe by attempting a digest the FIPS
    provider rejects (MD5): if it raises, FIPS enforcement is active.
    """
    try:
        hashlib.md5(b"", usedforsecurity=True)  # noqa: S324 - probe only
    except ValueError:
        # OpenSSL refused a non-approved algorithm -> FIPS mode is enforced.
        return True
    except Exception:
        return False
    return False


def openssl_version() -> str:
    return ssl.OPENSSL_VERSION


class FIPSComplianceError(RuntimeError):
    """Raised when FIPS is required, the host is not in FIPS mode, and running on a
    non-FIPS host has not been explicitly allowed."""


def enforce_fips_policy(require_fips: bool, allow_non_fips: bool = False) -> None:
    """Enforce the FIPS runtime policy at startup.

    Refuses to run only when FIPS is required, the linked OpenSSL is *not* enforcing
    FIPS, and ``allow_non_fips`` has not been set. Otherwise it is a no-op (a non-FIPS
    host is surfaced as a warning by ``secagent doctor``). This makes it possible to run
    a FIPS-configured deployment on a non-FIPS host (e.g. for development) by setting
    ``fips.allow_non_fips=true``.
    """
    if not require_fips:
        return
    if openssl_fips_enabled():
        return
    if allow_non_fips:
        return
    raise FIPSComplianceError(
        "FIPS mode is required (fips.require_fips=true) but the linked OpenSSL is not "
        "enforcing FIPS. Run on a FIPS-enabled host, or set fips.allow_non_fips=true "
        "(or SECAGENT_FIPS__ALLOW_NON_FIPS=true) to permit running on a non-FIPS host."
    )
