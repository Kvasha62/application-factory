"""SHA-256 digests for Requirements/Configuration documents.

Digest scope (owner-ratified): a document digest is derived only from the
immutable version payload — the whole document minus its ``digest`` field,
which therefore includes the declared ``schema_version`` identity. The
digest never covers mutable aggregate pointers, and it never substitutes
for Manifest/Instance digests, D&O verification digests, or ADR-0018
artifact identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from factory_control_plane.canonical import canonical_bytes

#: Digest format aligned with the repository's digest convention.
DIGEST_PREFIX = "sha256:"


def digest_of(document: Mapping[str, Any]) -> str:
    """Compute the digest of the immutable payload of ``document``.

    The ``digest`` field itself is excluded, so a document that carries
    its declared digest verifies against the same value.
    """
    payload = {key: value for key, value in document.items() if key != "digest"}
    checksum = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return f"{DIGEST_PREFIX}{checksum}"


def with_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``document`` carrying its computed ``digest``."""
    settled = dict(document)
    settled["digest"] = digest_of(settled)
    return settled


def verify_digest(document: Mapping[str, Any]) -> bool:
    """True when the declared ``digest`` matches the payload exactly."""
    declared = document.get("digest")
    if not isinstance(declared, str):
        return False
    return declared == digest_of(document)
