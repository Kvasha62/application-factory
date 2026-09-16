"""Composer — Slice E of Level 2 Component Factory (ADR-0015 §15).

ARCHITECTURE.md §17 defines the Composer: it resolves dependencies, verifies
compatibility, selects versions, applies configuration, checks extensions,
produces a reproducible Platform Manifest and hands it to validation.

This module is the single programmatic door to that behaviour:

* :func:`compose` — compose a platform from a validated composition request;
* :func:`compose_request_document` — validate and compose a request document;
* :func:`compose_request_path` — validate and compose a request file;
* :func:`compose_diagnostics` — every violation of a request/composition,
  deterministically, without raising;
* :class:`Composition` — the produced draft manifest, the deterministic
  selection decisions and the composition report.

The Composer assembles **Platform Manifests**, and nothing else. It does not
approve, validate-as-a-lifecycle-step, publish, deploy or provision a Platform
Instance, does not create an availability index of its own, does not read
business data, does not access a component database and does not import a
component's internals (ADR-0015 §8, §11, §13). Its output is a ``draft``
manifest that continues through the Platform Manifest lifecycle, where
approval, publication and deployment remain the owner's acts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from component_registry import RegistryError, load_registry
from composer.errors import (
    ComposerError,
    ComposerNotFoundError,
    ComposerValidationError,
    CompositionRejectedError,
)
from composer.request import (
    EXAMPLE_REQUEST_PATH,
    REQUEST_DIR,
    REQUEST_SCHEMA_PATH,
    CompositionRequest,
    build_request_document,
    discover_root,
    load_request,
    load_request_document,
    render_request_document,
    validate_request_document,
)
from composer.resolution import Resolution, resolve_closure
from composer.verification import VerificationResult, verify_composition
from platform_manifest import build_manifest_document, render_manifest_document
from platform_manifest import validate_document as validate_manifest_document

Document = Mapping[str, Any]
Entry = Mapping[str, Any]

#: Composer directory: request schema, rules and the deterministic example.
COMPOSER_DIR = REQUEST_DIR

#: The lifecycle state the Composer produces. Validation, approval, publication
#: and deployment stay with the Platform Manifest lifecycle (ARCHITECTURE.md
#: §16): the Composer never approves its own output.
PRODUCED_LIFECYCLE_STATE = "draft"


@dataclass(frozen=True)
class Composition:
    """A composed platform: a draft manifest plus its explicit decisions."""

    request: CompositionRequest
    document: Document
    resolution: Resolution
    root: Path
    golden_bundle: Entry | None = None

    @property
    def manifest_id(self) -> str | None:
        value = self.document.get("manifest_id")
        return value if isinstance(value, str) else None

    @property
    def manifest_version(self) -> str | None:
        value = self.document.get("manifest_version")
        return value if isinstance(value, str) else None

    @property
    def manifest_digest(self) -> str | None:
        value = self.document.get("manifest_digest")
        return value if isinstance(value, str) else None

    @property
    def lifecycle_state(self) -> str | None:
        lifecycle = self.document.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            return None
        state = lifecycle.get("state")
        return state if isinstance(state, str) else None

    @property
    def component_ids(self) -> tuple[str, ...]:
        return self.resolution.component_ids

    @property
    def decisions(self) -> tuple[dict[str, Any], ...]:
        return self.resolution.decisions

    @property
    def certified(self) -> bool:
        """True when the composition is built from a certified Golden Bundle.

        A composition without a bundle is explicitly uncertified
        (ARCHITECTURE.md §31) — expressed by ``golden_bundle: null`` in the
        manifest, never by an implied certification.
        """
        return self.golden_bundle is not None

    def validate(self) -> list[str]:
        """Return every validation violation of the produced manifest."""
        return validate_manifest_document(self.document, root=self.root)

    def require_valid(self) -> list[str]:
        """Raise :class:`CompositionRejectedError` when the manifest is invalid."""
        errors = self.validate()
        if errors:
            raise CompositionRejectedError(errors)
        return errors

    def render(self) -> str:
        """Serialize the produced manifest deterministically for storage."""
        return render_manifest_document(self.document)

    def summary(self) -> dict[str, Any]:
        """Return the deterministic composition report.

        The report is what makes a composition auditable: which component was
        requested, which one entered as a declared dependency, which version was
        selected and from which explicit input — so a composition change is
        never silent (ARCHITECTURE.md §17).
        """
        return {
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_digest": self.manifest_digest,
            "lifecycle_state": self.lifecycle_state,
            "components": [
                {
                    "component_id": component.component_id,
                    "component_version": component.component_version,
                }
                for component in self.resolution.selected
            ],
            "explicit_components": list(self.resolution.explicit_ids()),
            "resolved_components": list(self.resolution.resolved_ids()),
            "decisions": [dict(decision) for decision in self.decisions],
            "golden_bundle": dict(self.golden_bundle) if self.golden_bundle else None,
            "certified": self.certified,
        }


@dataclass(frozen=True)
class _Outcome:
    """Internal result of evaluating one composition request."""

    errors: tuple[str, ...]
    document: dict[str, Any] | None = None
    resolution: Resolution | None = None
    golden_bundle: dict[str, Any] | None = None


def _registry_failure(error: BaseException) -> str:
    detail = getattr(error, "errors", None)
    first = str(detail[0]) if detail else str(error).splitlines()[0]
    return (
        f"$: the authoritative Component Registry is unavailable or invalid ({first}); "
        f"composition is fail-closed"
    )


def _load_registry(root: Path) -> tuple[Any | None, list[str]]:
    try:
        return load_registry(root), []
    except (RegistryError, OSError, ValueError, KeyError) as error:
        return None, [_registry_failure(error)]


def _evaluate(request: CompositionRequest, root: Path) -> _Outcome:
    """Evaluate a structurally valid request; never raise for a rejection."""
    registry, errors = _load_registry(root)
    if registry is None:
        return _Outcome(errors=tuple(sorted(set(errors))))

    resolution, resolution_errors = resolve_closure(registry, request.components)
    verification: VerificationResult = verify_composition(
        root,
        registry,
        resolution,
        configuration=request.configuration,
        extensions=request.extensions,
        golden_bundle=request.golden_bundle,
    )

    errors = [*resolution_errors, *verification.errors]
    if errors:
        return _Outcome(errors=tuple(sorted(set(errors))))

    manifest_id = request.manifest_id
    manifest_version = request.manifest_version
    if (
        manifest_id is None or manifest_version is None
    ):  # pragma: no cover - guarded by validation
        return _Outcome(
            errors=(
                (
                    "$.manifest: a manifest_id and an explicit manifest_version are "
                    "required to produce a Platform Manifest"
                ),
            )
        )

    document = build_manifest_document(
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        lifecycle_state=PRODUCED_LIFECYCLE_STATE,
        components=resolution.manifest_entries(),
        predecessor=request.predecessor,
        golden_bundle=verification.golden_bundle,
        configuration=dict(request.configuration) or None,
        extensions=[dict(extension) for extension in request.extensions] or None,
        branding=dict(request.branding) if request.branding else None,
    )

    # Handoff (ARCHITECTURE.md §17): the produced manifest is validated by the
    # normative Platform Manifest validation of Slice C. A manifest the Composer
    # cannot hand over valid is a rejected composition, never a written artifact.
    manifest_errors = validate_manifest_document(document, root=root)
    if manifest_errors:
        return _Outcome(
            errors=tuple(
                sorted(
                    {
                        f"$: the produced Platform Manifest is invalid — {error}"
                        for error in manifest_errors
                    }
                )
            )
        )

    return _Outcome(
        errors=(),
        document=document,
        resolution=resolution,
        golden_bundle=verification.golden_bundle,
    )


def compose(request: CompositionRequest, *, root: Path | None = None) -> Composition:
    """Compose a platform from a validated composition request.

    Any incompatibility or contract violation rejects the composition
    deterministically and produces no manifest (ADR-0015 §8). The Composer
    never substitutes a version, drops a dependency or repairs a composition to
    make it succeed.
    """
    request.require_valid()
    base = root if root is not None else request.root
    outcome = _evaluate(request, base)
    if outcome.errors or outcome.document is None or outcome.resolution is None:
        raise CompositionRejectedError(
            outcome.errors or ("$: composition could not be produced",)
        )
    return Composition(
        request=request,
        document=outcome.document,
        resolution=outcome.resolution,
        root=base,
        golden_bundle=outcome.golden_bundle,
    )


def compose_request_document(
    document: object, *, root: Path | None = None
) -> Composition:
    """Validate and compose a composition request document."""
    base = root if root is not None else discover_root()
    request = CompositionRequest(document=_require_mapping(document), root=base)
    return compose(request, root=base)


def compose_request_path(path: Path, *, root: Path | None = None) -> Composition:
    """Load, validate and compose a composition request file."""
    base = root if root is not None else discover_root()
    request = load_request(path, base)
    return compose(request, root=base)


def compose_diagnostics(document: object, *, root: Path | None = None) -> list[str]:
    """Return every violation of a composition request, without raising.

    Request validation and composition verification are reported together, so
    a caller sees the complete, deterministic list. A request that is not a JSON
    object is reported as a single violation.
    """
    base = root if root is not None else discover_root()
    if not isinstance(document, Mapping):
        return ["$: composition request must be a JSON object"]

    errors = validate_request_document(document, root=base)
    if errors:
        return errors

    request = CompositionRequest(document=document, root=base)
    outcome = _evaluate(request, base)
    return list(outcome.errors)


def _require_mapping(document: object) -> Document:
    if not isinstance(document, Mapping):
        raise ComposerValidationError(["$: composition request must be a JSON object"])
    return document


__all__ = [
    "COMPOSER_DIR",
    "EXAMPLE_REQUEST_PATH",
    "PRODUCED_LIFECYCLE_STATE",
    "REQUEST_SCHEMA_PATH",
    "ComposerError",
    "ComposerNotFoundError",
    "ComposerValidationError",
    "Composition",
    "CompositionRejectedError",
    "build_request_document",
    "compose",
    "compose_diagnostics",
    "compose_request_document",
    "compose_request_path",
    "discover_root",
    "load_request",
    "load_request_document",
    "render_request_document",
    "validate_request_document",
]
