"""Validation of an assembled Platform Instance against its manifest.

An instance document is never valid in isolation: it is valid only against
the Platform Manifest it binds to and against the applicable factory
contracts. This module enforces, deterministically and totally (every
violation reported, sorted, no file-order, time, randomness or network
dependence):

* **Structure** — the normative instance schema
  (``factory/platform_instance/schema/platform_instance.schema.json``).
* **Assembly identity** — ``instance_digest`` matches the canonical content
  digest; a hand-edited instance document is rejected.
* **Authoritative manifest** — the bound manifest document is re-validated by
  the normative Slice C validation (fail-closed): identity/version/digest,
  lifecycle, components against the authoritative Component Registry, artifact
  pinning, Golden Bundle reference, configuration/extensions/branding rules.
  An unavailable or invalid registry is a validation error, never a silent
  skip (the Slice C loader already fails closed).
* **Applicable factory contract surfaces** — the manifest's fixed composition
  is re-checked through the existing authoritative Composer verification
  surfaces (dependency compatibility §18, contract validity, configuration
  §20, extensions §21) over the pinned selections themselves. Nothing is
  re-resolved or re-selected: the composition view is reconstructed from the
  manifest entries, so an instance can never carry a composition these
  surfaces reject.
* **Exact binding** (LAW-07, LAW-09) — the instance binds the manifest's
  ``manifest_id + manifest_version + manifest_digest``, and the declared
  ``manifest_digest`` equals the computed digest of the bound manifest
  document: a tampered or substituted manifest is rejected.
* **Existing lifecycle vocabulary only** (ARCHITECTURE.md §16) —
  ``manifest_state`` equals the bound manifest's lifecycle state and is one
  of the states a concrete instance may be assembled from. No new states are
  introduced.
* **Exact preservation** — components (identity/version/artifact), Golden
  Bundle reference (or explicit ``null``), configuration, extensions and
  branding are canonically equal to the manifest's content: assembly never
  re-interprets, upgrades or drops a value, and validation rejects any
  divergence between the instance and its authoritative manifest.
* **Platform Instance identity consistency** — a component-scoped
  configuration entry that names the Platform Instance its component serves
  (the existing ``platform_id`` and identity's ``current_platform_id``
  configuration keys) must name exactly this instance.
* **No floating selectors** — defense in depth: no value inside the instance
  content may select ``latest``/``current``/``default``/``stable``/``edge``/
  ``main``/``master``/``head``/``tip``/``*``.

Nothing here reads business data, touches a component database or imports a
component's internals (ARCHITECTURE.md §1.1, §18; ADR-0015 §8, §11).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_instance.schema import load_schema
from platform_manifest.lifecycle import LIFECYCLE_STATES
from platform_manifest.manifest import (
    canonical_json,
    compute_manifest_digest,
)
from platform_manifest.schema import validate_structure
from platform_manifest.validation import is_floating_selector

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Manifest lifecycle states a concrete Platform Instance may be assembled
#: from (ARCHITECTURE.md §16, existing vocabulary only). ``draft`` has not
#: passed manifest validation yet; ``superseded`` and ``retired`` are
#: historical states of compositions that have been replaced or withdrawn.
ASSEMBLABLE_MANIFEST_STATES = frozenset(
    {"validated", "approved", "published", "deployed"}
)

#: Component-scoped configuration keys that name the Platform Instance a
#: component serves, as published by the component contracts shipped in this
#: repository ("Identifier of the Platform Instance this data owner serves";
#: identity declares the same fact as ``current_platform_id``). All such
#: values must agree with the instance identity.
PLATFORM_IDENTITY_CONFIG_KEYS = ("platform_id", "current_platform_id")

_PRESERVED_FIELDS = (
    "components",
    "golden_bundle",
    "configuration",
    "extensions",
    "branding",
)


def _canonical_equal(left: object, right: object) -> bool:
    """True when two JSON values are canonically equal."""
    return canonical_json(left) == canonical_json(right)


def _contains_floating_selector(value: object, path: str) -> list[str]:
    """Recursively search an instance content section for floating selectors."""
    errors: list[str] = []
    if isinstance(value, str):
        if is_floating_selector(value):
            errors.append(
                f"{path}: {value!r} is a floating selector; "
                "concrete values are required"
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if is_floating_selector(key):
                errors.append(f"{path}.{key}: key {key!r} is a floating selector")
            errors.extend(_contains_floating_selector(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_contains_floating_selector(item, f"{path}[{index}]"))
    return errors


def _identity_errors(document: Document) -> list[str]:
    """Instance-level identity checks: platform_id and assembly digest."""
    errors: list[str] = []

    platform_id = document.get("platform_id")
    if is_floating_selector(platform_id):
        errors.append(
            f"$.platform_id: {platform_id!r} is a floating selector, not a "
            "stable Platform Instance identity"
        )

    from platform_instance.instance import compute_instance_digest

    declared = document.get("instance_digest")
    if isinstance(declared, str):
        computed = compute_instance_digest(document)
        if declared != computed:
            errors.append(
                "$.instance_digest: declared digest does not match the "
                f"canonical content digest {computed!r}; the assembly "
                "representation is immutable once computed"
            )

    # Defense in depth: the digest already pins the content, so a floating
    # selector cannot enter a digest-valid document; the explicit walk keeps
    # the rejection reason readable if the digest model itself is misused.
    for field in ("configuration", "branding", "extensions"):
        if field in document:
            errors.extend(_contains_floating_selector(document[field], f"$.{field}"))

    return errors


def _manifest_binding_errors(document: Document, manifest: Document) -> list[str]:
    """Exact binding of the instance to its manifest identity/version/digest."""
    errors: list[str] = []

    binding = document.get("manifest")
    if not isinstance(binding, Mapping):
        return ["$.manifest: an exact manifest binding object is required"]

    manifest_id = binding.get("manifest_id")
    if manifest_id != manifest.get("manifest_id"):
        errors.append(
            "$.manifest.manifest_id: instance is not bound to the manifest "
            f"identity {manifest.get('manifest_id')!r}; got {manifest_id!r}"
        )

    manifest_version = binding.get("manifest_version")
    if manifest_version != manifest.get("manifest_version"):
        errors.append(
            "$.manifest.manifest_version: instance is not bound to the "
            "manifest version "
            f"{manifest.get('manifest_version')!r}; got {manifest_version!r}"
        )

    declared_digest = binding.get("manifest_digest")
    computed_digest = compute_manifest_digest(manifest)
    if declared_digest != manifest.get("manifest_digest"):
        errors.append(
            "$.manifest.manifest_digest: binding does not restate the "
            f"manifest's own digest {manifest.get('manifest_digest')!r}; "
            f"got {declared_digest!r}"
        )
    if manifest.get("manifest_digest") != computed_digest:
        errors.append(
            "$.manifest.manifest_digest: the bound manifest document's "
            f"declared digest {manifest.get('manifest_digest')!r} does not "
            f"match its computed content digest {computed_digest!r}; the "
            "manifest was tampered with or is not the published form"
        )

    return errors


def _manifest_state_errors(document: Document, manifest: Document) -> list[str]:
    """Instance state must restate the manifest's existing lifecycle state."""
    errors: list[str] = []

    state = document.get("manifest_state")
    manifest_lifecycle = manifest.get("lifecycle")
    manifest_state = (
        manifest_lifecycle.get("state")
        if isinstance(manifest_lifecycle, Mapping)
        else None
    )
    if state not in LIFECYCLE_STATES:
        errors.append(
            f"$.manifest_state: {state!r} is not a Platform Manifest "
            "lifecycle state (ARCHITECTURE.md §16); no instance-specific "
            "lifecycle vocabulary exists"
        )
    if state != manifest_state:
        errors.append(
            "$.manifest_state: instance states "
            f"{state!r} but the bound manifest is in state {manifest_state!r}"
        )
    if state in LIFECYCLE_STATES and state not in ASSEMBLABLE_MANIFEST_STATES:
        errors.append(
            "$.manifest_state: a Platform Instance cannot be assembled from "
            f"a manifest in state {state!r}; assemblable states: "
            f"{sorted(ASSEMBLABLE_MANIFEST_STATES)!r}"
        )

    return errors


def _preservation_errors(document: Document, manifest: Document) -> list[str]:
    """Every preserved value must be canonically equal to the manifest's."""
    errors: list[str] = []
    for field in _PRESERVED_FIELDS:
        instance_value = document.get(field)
        manifest_value = manifest.get(field)
        present_in_instance = field in document
        present_in_manifest = field in manifest
        if present_in_instance != present_in_manifest or not _canonical_equal(
            instance_value, manifest_value
        ):
            errors.append(
                f"$.{field}: instance content diverges from the "
                "authoritative manifest; assembly preserves manifest values "
                "exactly and never re-interprets them"
            )
    return errors


def _platform_identity_errors(document: Document, manifest: Document) -> list[str]:
    """Component configuration must not name a different Platform Instance."""
    errors: list[str] = []
    platform_id = document.get("platform_id")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping) or not isinstance(platform_id, str):
        return errors
    for component_id, entry in configuration.items():
        if not isinstance(entry, Mapping):
            continue
        for key in PLATFORM_IDENTITY_CONFIG_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value != platform_id:
                errors.append(
                    f"$.platform_id: manifest configuration for "
                    f"{component_id!r} names Platform Instance {value!r} "
                    f"({key}); the instance is assembled as {platform_id!r}"
                )
    return errors


def _composition_surface_errors(manifest: Document, base: Path) -> list[str]:
    """Re-check the manifest's fixed composition through existing surfaces.

    The bound manifest pins explicit, registry-matched component versions
    (enforced by Slice C validation). This check re-runs the authoritative
    Composer verification surfaces over exactly those pinned selections —
    dependency compatibility (ARCHITECTURE.md §18), contract validity
    (identity, version, dependencies, ownership), configuration against
    published configuration schemas (§20) and extensions through published
    contracts (§21). No version is resolved, selected or substituted here:
    the composition view is reconstructed from the manifest entries
    themselves, so an instance can never carry a composition the factory
    surfaces reject.
    """
    try:
        from component_registry import RegistryError, load_registry
    except ImportError as error:  # pragma: no cover - fail-closed
        return [
            (
                "$.manifest: the authoritative Component Registry loader is "
                f"unavailable ({error.__class__.__name__}); assembly "
                "validation is fail-closed (ADR-0015 §4)"
            )
        ]

    try:
        registry = load_registry(base)
    except (RegistryError, OSError, ValueError, KeyError) as error:
        detail = getattr(error, "errors", None)
        first = str(detail[0]) if detail else str(error).splitlines()[0]
        return [
            (
                "$.manifest: the authoritative Component Registry is "
                f"unavailable or invalid ({first}); assembly validation is "
                "fail-closed (ADR-0015 §4)"
            )
        ]

    from composer.resolution import (
        SELECTED_AS_EXPLICIT_VERSION,
        Resolution,
        SelectedComponent,
    )
    from composer.verification import verify_composition

    selected = tuple(
        SelectedComponent(
            component_id=entry.get("component_id", ""),
            component_version=entry.get("component_version", ""),
            artifact=entry.get("artifact", {}),
            selected_as=SELECTED_AS_EXPLICIT_VERSION,
            constraint=None,
        )
        for entry in manifest.get("components", [])
        if isinstance(entry, Mapping)
    )
    resolution = Resolution(selected=selected)

    configuration = manifest.get("configuration")
    extensions = manifest.get("extensions")

    result = verify_composition(
        base,
        registry,
        resolution,
        configuration=configuration if isinstance(configuration, Mapping) else {},
        extensions=tuple(extensions) if isinstance(extensions, list) else (),
        golden_bundle=None,
    )
    return list(result.errors)


def validate_instance_document(
    document: object,
    manifest_document: object,
    *,
    root: Path | None = None,
) -> list[str]:
    """Validate an instance document against its bound manifest.

    Returns every violation, sorted — the same document pair always yields
    the same list. Never raises for an invalid input pair.
    """
    from platform_instance.instance import discover_root

    base = root if root is not None else discover_root()
    errors: list[str] = []

    if not isinstance(document, Mapping):
        return ["$: platform instance must be a JSON object"]

    errors.extend(validate_structure(document, load_schema(base)))

    if not isinstance(manifest_document, Mapping):
        errors.append(
            "$.manifest: the bound Platform Manifest document is required "
            "to validate an instance; an instance is never valid in "
            "isolation"
        )
        return sorted(set(errors))

    errors.extend(_identity_errors(document))
    errors.extend(_manifest_binding_errors(document, manifest_document))
    errors.extend(_manifest_state_errors(document, manifest_document))
    errors.extend(_preservation_errors(document, manifest_document))
    errors.extend(_platform_identity_errors(document, manifest_document))

    # Authoritative factory contracts, fail-closed: the bound manifest must
    # be a valid Platform Manifest here and now (registry availability,
    # component versions, artifact pinning, bundle reference, lifecycle —
    # all enforced by the normative Slice C surface).
    from platform_manifest import validate_document as validate_manifest_document

    manifest_errors = validate_manifest_document(manifest_document, root=base)
    errors.extend(
        f"$.manifest: the bound Platform Manifest is invalid — {error}"
        for error in manifest_errors
    )

    # Applicable factory contract surfaces over the manifest's fixed
    # composition (§18, §20, §21; contract validity) — the same authoritative
    # surfaces the Composer applies at composition time, re-applied to the
    # pinned selections without re-resolving anything.
    errors.extend(
        f"$.manifest: the bound Platform Manifest is invalid — {error}"
        for error in _composition_surface_errors(manifest_document, base)
    )

    return sorted(set(errors))


__all__ = [
    "ASSEMBLABLE_MANIFEST_STATES",
    "PLATFORM_IDENTITY_CONFIG_KEYS",
    "validate_instance_document",
]
