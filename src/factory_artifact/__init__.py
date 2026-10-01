"""Factory-defined canonical artifact representations and their identities.

Two concerns live here, and they are deliberately separated:

* **canonical representation** — :func:`build_source_package_canonical` and
  :func:`build_container_image_canonical` define the only normative canonical
  forms (``source_package/v1``, ``container_image/v1``);
* **artifact identity** — :class:`ArtifactDescriptor` binds one canonical
  representation to an immutable SHA-256 digest, and :func:`verify_artifact`
  proves fail-closed that a physical artifact still is that artifact.

Nothing in this package selects a component set, a deployment target or an
external registry, and nothing here declares a component deployable or
publishable.
"""

from .container_image import build_container_image_canonical
from .descriptor import (
    CANONICAL_FORMS,
    CANONICAL_MANIFEST_PATTERN,
    CANONICAL_MANIFEST_TEMPLATE,
    DIGEST_PATTERN,
    DIGEST_PREFIX,
    ArtifactDescriptor,
    ArtifactError,
    ArtifactMetadataError,
    ArtifactNotPublishedError,
    ArtifactVerificationError,
    artifact_violations,
    canonical_manifest_reference,
    describe_artifact,
    describe_container_image,
    describe_source_package,
    descriptor_from_registry_metadata,
    registry_metadata,
    registry_metadata_violations,
    verify_artifact,
)
from .source_package import build_source_package_canonical

__all__ = [
    "CANONICAL_FORMS",
    "CANONICAL_MANIFEST_PATTERN",
    "CANONICAL_MANIFEST_TEMPLATE",
    "DIGEST_PATTERN",
    "DIGEST_PREFIX",
    "ArtifactDescriptor",
    "ArtifactError",
    "ArtifactMetadataError",
    "ArtifactNotPublishedError",
    "ArtifactVerificationError",
    "artifact_violations",
    "build_container_image_canonical",
    "build_source_package_canonical",
    "canonical_manifest_reference",
    "describe_artifact",
    "describe_container_image",
    "describe_source_package",
    "descriptor_from_registry_metadata",
    "registry_metadata",
    "registry_metadata_violations",
    "verify_artifact",
]
