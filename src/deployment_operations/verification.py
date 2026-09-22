"""Verification of the deployment input — exact Platform Instance identity.

The deployment input of this capability is an accepted Platform Instance
(``docs/ARCHITECTURE.md`` §2.2; ADR-0016 §5, §8): the desired state produced by
the Factory, consumed here and never edited. This module is the door through
which that input is verified before any action is taken.

Two dimensions are checked and kept separate, because they answer different
questions and only both together justify a deployment claim (ADR-0017 §34):

* **deployment-input verification** — is this exactly the instance it claims to
  be, and does every deployable component carry an exact, non-floating binding
  of component / version / artifact identity / digest (AC1, AC2, AC14)?
* **authoritative validity** — is the instance valid against its bound
  Manifest and the factory contracts, as judged by the existing normative
  Slice F surface (AC3)? This capability reuses that surface instead of
  re-implementing factory validation.

Nothing here mutates its input: the documents are read, never rewritten
(ADR-0016 §7 — the Immutable Manifest Principle). ``verify_input_unchanged``
re-checks the same digests after the operation has touched the environment, so
immutability is verified operationally, not merely asserted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_instance.instance import compute_instance_digest, discover_root
from platform_instance.validation import (
    ASSEMBLABLE_MANIFEST_STATES,
    validate_instance_document,
)
from platform_manifest.manifest import compute_manifest_digest
from platform_manifest.validation import (
    ARTIFACT_TYPES,
    is_floating_selector,
    parse_semver,
)

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Digest of an artifact: ``sha256:`` + 64 lowercase hex characters.
_ARTIFACT_DIGEST_RE = re.compile(r"^(sha256:)?[0-9a-f]{64}$")

#: Identity fields of a Platform Instance that must never be a floating selector.
_IDENTITY_FIELDS = ("platform_id",)

#: Manifest lifecycle states an instance may be consumed from. Deployment adds
#: no state vocabulary of its own: it reuses the assemblable states of the
#: ratified lifecycle (ARCHITECTURE.md §16).
DEPLOYABLE_INSTANCE_STATES = ASSEMBLABLE_MANIFEST_STATES


def canonical_digest(value: object) -> object:
    """Normalise a digest value for comparison.

    The optional ``sha256:`` prefix is notation, not content: ``sha256:<hex>``
    and ``<hex>`` denote the same artifact content. The same rule the factory's
    artifact authority check applies is reused here so both boundaries read
    digests identically.
    """
    if not isinstance(value, str):
        return value
    return value.strip().removeprefix("sha256:")


def compute_content_digest(path: Path) -> str:
    """Return ``sha256:<hex>`` of a file's bytes (artifact content digest)."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _contains_floating_selector(value: object, path: str) -> list[str]:
    """Recursively report floating selectors in a deployment-input section."""
    errors: list[str] = []
    if isinstance(value, str):
        if is_floating_selector(value):
            errors.append(
                f"{path}: {value!r} is a floating selector; the deployment "
                "input requires concrete versions and identities"
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if is_floating_selector(key):
                errors.append(f"{path}.{key}: the key itself is a floating selector")
            errors.extend(_contains_floating_selector(item, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            errors.extend(_contains_floating_selector(item, f"{path}[{index}]"))
    return errors


@dataclass(frozen=True)
class ComponentBinding:
    """Everything the deployment path pins for one component of the instance.

    ``component_id`` + ``component_version`` are the exact component identity;
    ``artifact_type`` + ``artifact_digest`` are the exact artifact identity. An
    artifact declaration of type ``none`` means *no published artifact exists*
    for that component in the authoritative registry — it is never an implicit
    artifact, and no digest is invented for it (ADR-0016 §7).
    """

    component_id: str
    component_version: str
    artifact_type: str
    artifact_digest: str | None
    artifact_pinned: bool
    artifact_canonical_form: str | None
    configuration_keys: tuple[str, ...] = ()

    @property
    def has_published_artifact(self) -> bool:
        """True when the pinned component declares a published artifact."""
        return self.artifact_type != "none"

    def document(self) -> dict[str, Any]:
        """Return the binding as a JSON-serialisable document."""
        return {
            "component_id": self.component_id,
            "component_version": self.component_version,
            "artifact": {
                "artifact_type": self.artifact_type,
                "digest": self.artifact_digest,
                "pinned": self.artifact_pinned,
                "canonical_form": self.artifact_canonical_form,
            },
            "configuration_keys": list(self.configuration_keys),
        }


@dataclass(frozen=True)
class InstanceVerification:
    """The verified identity of one deployment input.

    ``errors`` are deployment-input verification failures: the input is not
    exactly what it claims to be, an exact binding is missing, or a floating
    selector appeared. ``validity_errors`` are the authoritative factory
    validation failures: the instance is not a valid instance of its bound
    Manifest. Either non-empty means no deployment may start (AC1–AC3).
    """

    platform_id: str | None
    manifest_state: str | None
    manifest_id: str | None
    manifest_version: str | None
    manifest_digest: str | None
    computed_manifest_digest: str | None
    instance_digest: str | None
    computed_instance_digest: str | None
    components: tuple[ComponentBinding, ...] = ()
    instance_configuration: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    validity_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the instance passed both dimensions of verification."""
        return not self.errors and not self.validity_errors

    def all_errors(self) -> tuple[str, ...]:
        """Return every failure of both dimensions, deterministically ordered."""
        return tuple(self.errors) + tuple(self.validity_errors)

    def component(self, component_id: str) -> ComponentBinding | None:
        for binding in self.components:
            if binding.component_id == component_id:
                return binding
        return None

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(binding.component_id for binding in self.components)


def _identity_errors(document: Document) -> tuple[list[str], str | None]:
    errors: list[str] = []

    platform_id = document.get("platform_id")
    if not isinstance(platform_id, str) or not platform_id.strip():
        errors.append(
            "$.platform_id: a concrete Platform Instance identity is required; "
            "a deployment is never requested without one"
        )
        platform_id = None
    else:
        for field in _IDENTITY_FIELDS:
            if is_floating_selector(platform_id):
                errors.append(
                    f"$.{field}: {platform_id!r} is a floating selector, not a "
                    "stable Platform Instance identity"
                )

    declared = document.get("instance_digest")
    if not isinstance(declared, str) or not declared.strip():
        errors.append(
            "$.instance_digest: the instance's own content digest is required; "
            "a name-only reference cannot be deployed"
        )
    else:
        computed = compute_instance_digest(document)
        if not _ARTIFACT_DIGEST_RE.fullmatch(declared):
            errors.append(
                f"$.instance_digest: {declared!r} is not an immutable sha256 digest"
            )
        if declared != computed:
            errors.append(
                "$.instance_digest: the declared digest does not match the "
                f"canonical content digest {computed!r}; the instance document "
                "was altered after assembly"
            )

    return errors, platform_id


def _manifest_binding_errors(document: Document, manifest: Document) -> list[str]:
    errors: list[str] = []

    binding = document.get("manifest")
    if not isinstance(binding, Mapping):
        return ["$.manifest: an exact manifest binding object is required"]

    declared_id = binding.get("manifest_id")
    if declared_id != manifest.get("manifest_id"):
        errors.append(
            "$.manifest.manifest_id: the instance does not bind the manifest "
            f"identity {manifest.get('manifest_id')!r}; got {declared_id!r}"
        )

    declared_version = binding.get("manifest_version")
    if declared_version != manifest.get("manifest_version"):
        errors.append(
            "$.manifest.manifest_version: the instance does not bind the "
            f"manifest version {manifest.get('manifest_version')!r}; "
            f"got {declared_version!r}"
        )

    declared_digest = binding.get("manifest_digest")
    own_digest = manifest.get("manifest_digest")
    computed_digest = compute_manifest_digest(manifest)
    if declared_digest != own_digest:
        errors.append(
            "$.manifest.manifest_digest: the binding does not restate the "
            f"manifest's own digest {own_digest!r}; got {declared_digest!r}"
        )
    if own_digest != computed_digest:
        errors.append(
            "$.manifest.manifest_digest: the supplied manifest document's "
            f"declared digest {own_digest!r} does not match its computed "
            f"content digest {computed_digest!r}; the manifest is not the "
            "published form"
        )

    state = document.get("manifest_state")
    if state not in DEPLOYABLE_INSTANCE_STATES:
        errors.append(
            "$.manifest_state: a deployment consumes an instance assembled "
            f"from a manifest in one of {sorted(DEPLOYABLE_INSTANCE_STATES)!r}; "
            f"got {state!r}"
        )

    return errors


def _component_errors(
    entry: object,
    index: int,
    configuration: Mapping[str, Any],
) -> tuple[list[str], ComponentBinding | None]:
    errors: list[str] = []
    path = f"$.components[{index}]"

    if not isinstance(entry, Mapping):
        return [f"{path}: a component entry object is required"], None

    component_id = entry.get("component_id")
    if not isinstance(component_id, str) or not component_id:
        errors.append(f"{path}.component_id: a concrete component identity is required")
        component_id = None

    component_version = entry.get("component_version")
    if parse_semver(component_version) is None:
        errors.append(
            f"{path}.component_version: an explicit SemVer version is required; "
            f"got {component_version!r}"
        )
        component_version = None

    artifact = entry.get("artifact")
    artifact_type: str | None = None
    artifact_digest: str | None = None
    artifact_pinned = False
    artifact_canonical_form: str | None = None
    if not isinstance(artifact, Mapping):
        errors.append(f"{path}.artifact: an explicit artifact identity is required")
    else:
        artifact_type = artifact.get("artifact_type")
        artifact_digest = artifact.get("digest")
        artifact_pinned = bool(artifact.get("pinned"))
        artifact_canonical_form = artifact.get("canonical_form")
        if artifact_type not in ARTIFACT_TYPES:
            errors.append(
                f"{path}.artifact.artifact_type: {artifact_type!r} is not a "
                f"declared artifact type {sorted(ARTIFACT_TYPES)!r}"
            )
        if artifact_type == "none":
            if artifact_digest is not None:
                errors.append(
                    f"{path}.artifact.digest: an artifact of type 'none' must "
                    "not declare a digest; no artifact identity may be invented"
                )
                artifact_digest = None
            if artifact.get("pinned"):
                errors.append(
                    f"{path}.artifact.pinned: an artifact of type 'none' must "
                    "not be pinned"
                )
            artifact_pinned = False
            if artifact_canonical_form is not None:
                errors.append(
                    f"{path}.artifact.canonical_form: an artifact of type "
                    "'none' must declare null"
                )
                artifact_canonical_form = None
        elif artifact_type in ARTIFACT_TYPES:
            if not isinstance(
                artifact_digest, str
            ) or not _ARTIFACT_DIGEST_RE.fullmatch(artifact_digest):
                errors.append(
                    f"{path}.artifact.digest: a published artifact must be "
                    f"selected by immutable digest; got {artifact_digest!r}"
                )
                artifact_digest = None
            if not artifact.get("pinned"):
                errors.append(
                    f"{path}.artifact.pinned: a published artifact must be "
                    "pinned; an unpinned artifact is a floating selection"
                )
            else:
                artifact_pinned = True

            expected_canonical_form = {
                "source_package": "source_package/v1",
                "container_image": "container_image/v1",
            }.get(artifact_type)
            if artifact_canonical_form != expected_canonical_form:
                errors.append(
                    f"{path}.artifact.canonical_form: a published artifact of "
                    f"type {artifact_type!r} must declare "
                    f"{expected_canonical_form!r}; got "
                    f"{artifact_canonical_form!r}"
                )
                artifact_canonical_form = expected_canonical_form

    errors.extend(_contains_floating_selector(artifact, f"{path}.artifact"))
    if isinstance(component_version, str):
        errors.extend(
            _contains_floating_selector(component_version, f"{path}.component_version")
        )

    if component_id is None or component_version is None or artifact_type is None:
        return errors, None

    declared_keys = configuration.get(component_id)
    configuration_keys: tuple[str, ...] = ()
    if isinstance(declared_keys, Mapping):
        configuration_keys = tuple(sorted(declared_keys))

    binding = ComponentBinding(
        component_id=component_id,
        component_version=component_version,
        artifact_type=str(artifact_type),
        artifact_digest=(
            f"sha256:{canonical_digest(artifact_digest)}"
            if isinstance(artifact_digest, str)
            else None
        ),
        artifact_pinned=artifact_pinned,
        artifact_canonical_form=artifact_canonical_form,
        configuration_keys=configuration_keys,
    )
    return errors, binding


def _components_errors(
    document: Document,
) -> tuple[list[str], tuple[ComponentBinding, ...]]:
    errors: list[str] = []
    components = document.get("components")
    if not isinstance(components, list) or not components:
        return ["$.components: the instance must pin at least one component"], ()

    configuration = document.get("configuration")
    if not isinstance(configuration, Mapping):
        configuration = {}

    bindings: list[ComponentBinding] = []
    for index, entry in enumerate(components):
        entry_errors, binding = _component_errors(entry, index, configuration)
        errors.extend(entry_errors)
        if binding is not None:
            bindings.append(binding)

    seen: set[str] = set()
    for binding in bindings:
        if binding.component_id in seen:
            errors.append(
                f"$.components: component {binding.component_id!r} is pinned "
                "more than once"
            )
        seen.add(binding.component_id)

    order = [binding.component_id for binding in bindings]
    if order != sorted(order):
        errors.append(
            "$.components: the composition is not in deterministic order "
            "(sorted by component_id) as assembled by the factory"
        )

    return errors, tuple(bindings)


def verify_instance(
    instance_document: object,
    manifest_document: object,
    *,
    root: Path | None = None,
    environment_id: str | None = None,
) -> InstanceVerification:
    """Verify a deployment input: identity, bindings and authoritative validity.

    Never raises for an invalid input pair; every failure is reported in the
    result, deterministically ordered. ``root`` selects the factory root whose
    authoritative registry and contracts judge validity (defaults to the
    repository root found above this module).
    """
    base = root if root is not None else discover_root()
    errors: list[str] = []
    validity_errors: list[str] = []

    if not isinstance(instance_document, Mapping):
        return InstanceVerification(
            platform_id=None,
            manifest_state=None,
            manifest_id=None,
            manifest_version=None,
            manifest_digest=None,
            computed_manifest_digest=None,
            instance_digest=None,
            computed_instance_digest=None,
            errors=("$: a Platform Instance document is required",),
        )

    document: Document = instance_document
    identity_errors, platform_id = _identity_errors(document)
    errors.extend(identity_errors)

    if environment_id is not None:
        errors.extend(_contains_floating_selector(environment_id, "$.environment_id"))

    components_errors, components = _components_errors(document)
    errors.extend(components_errors)

    binding = document.get("manifest")
    manifest_id: str | None = None
    manifest_version: str | None = None
    manifest_digest: str | None = None
    if isinstance(binding, Mapping):
        raw_id = binding.get("manifest_id")
        manifest_id = raw_id if isinstance(raw_id, str) else None
        raw_version = binding.get("manifest_version")
        manifest_version = raw_version if isinstance(raw_version, str) else None
        raw_digest = binding.get("manifest_digest")
        manifest_digest = raw_digest if isinstance(raw_digest, str) else None
        errors.extend(_contains_floating_selector(binding, "$.manifest"))

    manifest_state = document.get("manifest_state")
    state = manifest_state if isinstance(manifest_state, str) else None

    computed_manifest: str | None = None
    if isinstance(manifest_document, Mapping):
        errors.extend(_manifest_binding_errors(document, manifest_document))
        computed_manifest = compute_manifest_digest(manifest_document)
        errors.extend(
            _contains_floating_selector(manifest_document, "$.manifest_document")
        )
    else:
        errors.append(
            "$.manifest: the bound Platform Manifest document is required to "
            "verify a deployment input; an instance is never verified in isolation"
        )

    declared = document.get("instance_digest")
    computed_instance = compute_instance_digest(document)

    if isinstance(manifest_document, Mapping):
        validity_errors.extend(
            validate_instance_document(document, manifest_document, root=base)
        )

    declared_configuration = document.get("configuration")
    configuration: dict[str, Any] = {}
    if isinstance(declared_configuration, Mapping):
        for component_id, values in declared_configuration.items():
            if isinstance(values, Mapping):
                configuration[str(component_id)] = dict(values)

    return InstanceVerification(
        platform_id=platform_id,
        manifest_state=state,
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        manifest_digest=manifest_digest if isinstance(manifest_digest, str) else None,
        computed_manifest_digest=computed_manifest,
        instance_digest=declared if isinstance(declared, str) else None,
        computed_instance_digest=computed_instance,
        components=components,
        instance_configuration=configuration,
        errors=tuple(sorted(set(errors))),
        validity_errors=tuple(sorted(set(validity_errors))),
    )


def verify_input_unchanged(
    verification: InstanceVerification,
    instance_document: object,
    manifest_document: object,
) -> list[str]:
    """Re-verify the digests of the deployment input after an operation (§7, §8).

    The Immutable Manifest Principle is not a promise about the past: the
    digest of the instance and of its bound Manifest are re-computed here, so
    any mutation of the desired state during deployment is detected instead of
    silently realized.
    """
    errors: list[str] = []

    if isinstance(instance_document, Mapping):
        recomputed = compute_instance_digest(instance_document)
        if verification.instance_digest is not None and (
            recomputed != verification.instance_digest
        ):
            errors.append(
                "$.instance_digest: the Platform Instance document changed "
                f"during the deployment operation ({recomputed!r} != "
                f"{verification.instance_digest!r}); desired state is never "
                "edited at runtime"
            )
    else:
        errors.append("$: the Platform Instance document is no longer available")

    if isinstance(manifest_document, Mapping):
        recomputed = compute_manifest_digest(manifest_document)
        if verification.manifest_digest is not None and (
            recomputed != verification.manifest_digest
        ):
            errors.append(
                "$.manifest.manifest_digest: the bound Platform Manifest changed "
                f"during the deployment operation ({recomputed!r} != "
                f"{verification.manifest_digest!r}); what is deployed is exactly "
                "what was assembled"
            )
    else:
        errors.append("$: the bound Platform Manifest document is no longer available")

    return errors


def verify_artifact_digest(
    binding: ComponentBinding,
    observed_digest: str | None,
) -> list[str]:
    """Compare a materialized artifact identity with the pinned artifact (§7).

    A component whose declaration is ``artifact_type: none`` has no artifact to
    verify and this function reports nothing for it; a component that declares
    a published artifact must match it exactly. Substitution, absence and
    digest mismatch are all fail-closed.
    """
    if not binding.has_published_artifact:
        return []
    if observed_digest is None:
        message = (
            f"{binding.component_id}: the pinned artifact "
            f"{binding.artifact_digest!r} could not be verified against any "
            "content; no artifact was materialized"
        )
        return [message]
    if canonical_digest(observed_digest) != canonical_digest(binding.artifact_digest):
        message = (
            f"{binding.component_id}: materialized artifact digest "
            f"{observed_digest!r} does not match the pinned artifact identity "
            f"{binding.artifact_digest!r}; no version or artifact substitution "
            "is permitted"
        )
        return [message]
    return []


def render_verification(verification: InstanceVerification) -> str:
    """Serialize a verification result deterministically (for diagnostics)."""
    return json.dumps(
        {
            "platform_id": verification.platform_id,
            "instance_digest": verification.instance_digest,
            "manifest": {
                "manifest_id": verification.manifest_id,
                "manifest_version": verification.manifest_version,
                "manifest_digest": verification.manifest_digest,
                "manifest_state": verification.manifest_state,
            },
            "components": [entry.document() for entry in verification.components],
            "errors": list(verification.errors),
            "validity_errors": list(verification.validity_errors),
        },
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = [
    "DEPLOYABLE_INSTANCE_STATES",
    "ComponentBinding",
    "InstanceVerification",
    "canonical_digest",
    "compute_content_digest",
    "render_verification",
    "verify_artifact_digest",
    "verify_input_unchanged",
    "verify_instance",
]
