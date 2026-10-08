"""Owner-side (Layer R) declaration and fail-closed loading for production.

This composition root contains no Running Platform. Layer R is an external
dependency: it is created, supervised and kept alive — and its actual identity
facts are produced — by its own owner, outside Deployment & Operations
(ADR-0016 §18; ADR-0020 §4). Its code therefore lives outside this repository,
in the S4 runbook's ``<RP_CELL_HOME>`` — the approved production layout names
that directory ``/srv/running-platform/cell``.

This module is the only place in Deployment & Operations that resolves an
externally declared Running Platform implementation, and it resolves it through
an **explicit declaration**:

    <absolute-path.py>:<factory>    the owner's module file and factory under
                                    <RP_CELL_HOME>
    <dotted.module>:<factory>       the owner's importable module and factory

The declared factory is called once with the declared options as keyword
arguments (option names from the command line are normalised so ``-`` becomes
``_``) and must answer with the owner's own wiring:

    ``runtime_adapter``    the ``RuntimeAdapter`` implementation of that Layer R;
    ``identity_provider``  the owner-side ``PlatformIdentityProvider``, or
    ``identity_source``    the owner-side ``RunningPlatformIdentitySource`` this
                           composition adapts with the shipped
                           ``OwnerSuppliedPlatformIdentityProvider``.

Nothing here is discovered. There is no directory scan, no environment
variable, no PATH lookup, no naming convention and **no default**: a declaration
that is missing, relative, unreadable, inside this repository, or whose answer
is not conformant fails closed as :class:`ProductionCompositionError`.

Two ownership rules are enforced here because they are the rules that make the
production path honest, and because each is independently checkable:

* the Layer R implementation must be defined **outside this repository** — the
  declared module and the classes it returns are checked against the deployed
  ``deployment_operations`` package directory and, when the package is imported
  from a source checkout, against that checkout too;
* the actual identity facts must not come from the Deployment & Operations
  package — an identity source or provider whose class is defined inside that
  package (the D&O-owned file surface included) is refused.

Falling back to the D&O-owned ``LocalProcessRuntime`` or to the D&O-owned
``<runtime_root>/running_platform_identity.json`` is not an available outcome of
this module: it cannot express either of them, and
``tests/test_production_composition.py`` checks that structurally.

The declared factory owns everything that touches its own state: how the
runtime elements are supervised, where the platform state is kept, and how an
observation is grounded. Deployment & Operations only ever receives the two
seam objects and the opaque evaluation handle of one binding.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import deployment_operations
from deployment_operations.errors import DeploymentOperationsError
from deployment_operations.platform_identity import PlatformIdentityProvider
from deployment_operations.platform_identity_source import (
    OwnerSuppliedPlatformIdentityProvider,
)
from deployment_operations.runtime import RuntimeAdapter

#: The complete ``RuntimeAdapter`` surface a Layer R implementation must provide.
#: ``stop`` carries the detach semantics of the established runtime contract: the
#: fail-closed cleanup path of ``deploy`` calls it for every handle, so it
#: releases this composition's reference to a Layer R element and never destroys
#: the layer (ADR-0016 §18; S4 runbook §7).
RUNTIME_ADAPTER_OPERATIONS = (
    "materialize",
    "migrate",
    "start",
    "attach",
    "request",
    "stop",
)

_FACTORY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_LOCATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ProductionCompositionError(DeploymentOperationsError):
    """The production composition could not be constructed; nothing was started.

    A composition failure is not a deployment failure: no operation exists, no
    environment is touched and no runtime element is addressed. It is raised
    before any Deployment & Operations operation is called, and it is never
    downgraded into a default (ADR-0016 §7, §20).
    """

    def __init__(
        self,
        errors: Sequence[str],
        *,
        message: str = (
            "the production composition could not be constructed; no operation "
            "was started"
        ),
    ) -> None:
        super().__init__(message, errors=list(errors), stage="composition")


def _refusal(reason: str) -> ProductionCompositionError:
    """One composition refusal, carrying its reason."""
    return ProductionCompositionError([reason])


def deployment_package_dir() -> Path:
    """The directory holding the deployed ``deployment_operations`` package."""
    return Path(deployment_operations.__file__).resolve().parent


def shipped_owner_side_dir() -> Path | None:
    """The ``running_platform`` package shipped beside this one, when it exists.

    This repository ships both packages — ``deployment_operations`` and the
    ``running_platform`` owner-state transport/readers — so neither is
    production Layer R code (ADR-0020 §4). The directory is taken from the
    deployed layout rather than searched for.
    """
    candidate = deployment_package_dir().parent / "running_platform"
    return candidate if candidate.is_dir() else None


def source_checkout_dir() -> Path | None:
    """The checkout this package was imported from, when it is a source checkout.

    ``None`` for an installed package: the deployed package directory is then
    the only boundary this module can name, which is why it is always checked.
    """
    current = deployment_package_dir()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists():
            return candidate
    return None


def governed_roots() -> tuple[Path, ...]:
    """Everything that is this repository's own code, as far as it is knowable."""
    roots = [deployment_package_dir()]
    shipped = shipped_owner_side_dir()
    if shipped is not None:
        roots.append(shipped)
    checkout = source_checkout_dir()
    if checkout is not None:
        roots.append(checkout)
    return tuple(roots)


def _is_governed(path: Path) -> bool:
    """True when ``path`` lies inside the Deployment & Operations side."""
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in governed_roots())


def _class_origin(instance: Any) -> Path | None:
    """The file the implementation of ``instance`` was defined in, if any.

    ``inspect`` answers with whatever the object's own type tells it, so this is
    a read of owner-controlled information: an object may raise from any part of
    that read, and the answer is then "no verifiable origin" rather than an
    exception escaping the composition.
    """
    try:
        return Path(inspect.getfile(type(instance))).resolve()
    except Exception:  # noqa: BLE001 - any failure means "not verifiable"
        return None


def _class_name(instance: Any) -> str:
    """The dot path of ``instance``'s type, or a placeholder when unreadable."""
    try:
        instance_type = type(instance)
        return f"{instance_type.__module__}.{instance_type.__qualname__}"
    except Exception:  # noqa: BLE001 - owner-controlled attribute read
        return "<unreadable owner type>"


def _owner_attributes(instance: Any, names: Sequence[str]) -> dict[str, Any] | None:
    """Read a fixed set of attributes off an owner-controlled object.

    ``getattr`` on an owner-supplied object runs the owner's code, including a
    ``__getattr__`` or a property descriptor that raises. ``None`` means the
    read itself failed and the object is unusable; the caller refuses it.
    """
    found: dict[str, Any] = {}
    try:
        for name in names:
            found[name] = getattr(instance, name, None)
    except Exception:  # noqa: BLE001 - owner-controlled attribute read
        return None
    return found


def parse_layer_r_reference(reference: str) -> tuple[str, str]:
    """Split one declared entry point into its target and its factory name.

    The target is either an absolute path to a ``.py`` file or a dotted module
    name; there is no discovery and no default. A reference that cannot be
    interpreted is refused here, before anything is imported or called.
    """
    if not isinstance(reference, str) or not reference.strip():
        raise _refusal(
            "a Layer R entry point must be declared explicitly as "
            "'<path-or-module>:<factory>'; this composition has no default and "
            "will not fall back to a Deployment & Operations runtime"
        )
    target, separator, factory = reference.rpartition(":")
    target = target.strip()
    factory = factory.strip()
    if not separator or not target or not factory:
        raise _refusal(
            f"the Layer R entry point {reference!r} is not "
            "'<path-or-module>:<factory>'"
        )
    if _FACTORY_NAME.fullmatch(factory) is None:
        raise _refusal(f"the Layer R factory {factory!r} is not a callable name")
    looks_like_path = "/" in target or target.endswith(".py")
    if not looks_like_path and _MODULE_LOCATION.fullmatch(target) is None:
        raise _refusal(
            f"the Layer R target {target!r} is neither an absolute path to a "
            "Python file nor a dotted module name"
        )
    return target, factory


@dataclass(frozen=True)
class LayerREntryPoint:
    """The explicit, operator-declared entry point of one Running Platform.

    ``options`` are the declared construction options of the owner's factory
    (for example a platform state directory). They are operational inputs: the
    transcript records their names, never their values.
    """

    reference: str
    options: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parse_layer_r_reference(self.reference)

    @property
    def target(self) -> str:
        """The declared module location: an absolute path or a module name."""
        return parse_layer_r_reference(self.reference)[0]

    @property
    def factory_name(self) -> str:
        """The declared factory name of the owner-side wiring."""
        return parse_layer_r_reference(self.reference)[1]

    def document(self) -> dict[str, object]:
        """The declaration as a credential-free record."""
        return {
            "declared_reference": self.reference,
            "target": self.target,
            "factory": self.factory_name,
            "option_names": sorted(self.options),
        }


@dataclass(frozen=True)
class LayerRWiring:
    """One resolved Running Platform: the owner's seams, as declared.

    ``module_path`` is where the declared implementation actually came from —
    recorded so that an operator (and an auditor) can see exactly which
    owner-side code answered, not merely which path was typed.
    """

    declaration: LayerREntryPoint
    module_path: Path
    runtime_adapter: RuntimeAdapter
    identity_provider: PlatformIdentityProvider
    #: ``True`` when the shipped adapter wrapped the owner's own source; the
    #: owner is then the source of the facts and this composition only adapts.
    adapted_owner_source: bool
    #: The owner's own identity source, when the wiring declared one. Carried so
    #: that the composition can be re-validated — and its claims checked against
    #: what it actually holds — without re-resolving the declaration.
    identity_source: Any = None

    def document(self) -> dict[str, object]:
        """The resolved composition as a credential-free record."""
        runtime_origin = _class_origin(self.runtime_adapter)
        identity_origin = _class_origin(self.identity_provider)
        return {
            "declaration": self.declaration.document(),
            "module": str(self.module_path),
            "runtime_adapter": {
                "type": _class_name(self.runtime_adapter),
                "module": str(runtime_origin) if runtime_origin else None,
                "operations": list(RUNTIME_ADAPTER_OPERATIONS),
            },
            "identity_provider": {
                "type": _class_name(self.identity_provider),
                "module": str(identity_origin) if identity_origin else None,
                "adapted_owner_source": self.adapted_owner_source,
            },
            # Recorded so a transcript states it rather than leaves it implicit:
            # this composition cannot construct either of them.
            "dno_owned_runtime_used": False,
            "dno_owned_identity_authority_used": False,
        }


def _exec_module_from_file(path: Path) -> ModuleType:
    """Execute one declared owner-side module from its file.

    A declared file is executed once per process, as an import is: a cell's
    module-level state (its supervision registry, for instance) is the state of
    the owner's process, and a composition that re-executed the module would
    silently lose it.
    """
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    name = f"layer_r_declared_{digest}"
    existing = sys.modules.get(name)
    if existing is not None:
        # Already executed in this process: the module's own state (an owner's
        # supervision registry, for instance) is the state of this process, and
        # re-executing the file would silently reset it. A change to the file on
        # disk takes effect in the next process, exactly as it would for an
        # imported module.
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise _refusal(f"the declared Layer R module {path} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(name, None)
        raise _refusal(
            f"the declared Layer R module {path} failed to load "
            f"({error.__class__.__name__})"
        ) from error
    return module


def _load_declared_module(
    declaration: LayerREntryPoint,
) -> tuple[ModuleType, Path]:
    """Import the declared module from exactly where it was declared to be."""
    target = declaration.target
    if "/" in target or target.endswith(".py"):
        path = Path(target)
        if not path.is_absolute():
            raise _refusal(
                f"the declared Layer R path {target!r} is relative; a file "
                "declaration must be an absolute path outside this repository"
            )
        if not path.is_file():
            raise _refusal(f"the declared Layer R file {path} does not exist")
        resolved = path.resolve()
        if _is_governed(resolved):
            raise _refusal(
                f"the declared Layer R file {resolved} is inside the "
                "Deployment & Operations code; the Running Platform belongs to "
                "its own owner and is not provided by this repository"
            )
        return _exec_module_from_file(resolved), resolved

    try:
        module = importlib.import_module(target)
    except Exception as error:
        raise _refusal(
            f"the declared Layer R module {target!r} could not be imported "
            f"({error.__class__.__name__})"
        ) from error
    files = _owner_attributes(module, ("__file__",))
    origin = files["__file__"] if files is not None else None
    if not isinstance(origin, str) or not origin:
        raise _refusal(f"the declared Layer R module {target!r} has no source location")
    resolved = Path(origin).resolve()
    if _is_governed(resolved):
        raise _refusal(
            f"the declared Layer R module {target!r} resolves inside the "
            "Deployment & Operations code; the Running Platform belongs to its "
            "own owner and is not provided by this repository"
        )
    return module, resolved


def _call_declared_factory(
    factory: Any,
    declaration: LayerREntryPoint,
    module_path: Path,
) -> Any:
    """Call the owner's factory once, refusing anything but an answer."""
    context = f"{module_path}:{declaration.factory_name}"
    if not callable(factory):
        raise _refusal(f"the declared Layer R entry point {context} is not callable")
    options = {
        name.replace("-", "_"): value for name, value in declaration.options.items()
    }
    try:
        return factory(**options)
    except Exception as error:
        raise _refusal(
            f"the declared Layer R entry point {context} failed "
            f"({error.__class__.__name__})"
        ) from error


def _validate_runtime_adapter(adapter: Any) -> RuntimeAdapter:
    """Refuse anything that is not a Layer R runtime implementation."""
    if adapter is None:
        raise _refusal(
            "the declared Layer R answered with no runtime_adapter; this "
            "composition never creates a runtime of its own"
        )
    errors: list[str] = []
    attributes = _owner_attributes(adapter, RUNTIME_ADAPTER_OPERATIONS)
    if attributes is None:
        raise _refusal(
            "the declared Layer R runtime_adapter could not be inspected: "
            "reading its runtime seam raised an exception, so this composition "
            "cannot establish what was supplied"
        )
    missing = [name for name, value in attributes.items() if not callable(value)]
    if missing:
        errors.append(
            "the declared Layer R runtime_adapter does not implement the "
            "runtime seam: " + ", ".join(missing)
        )
    origin = _class_origin(adapter)
    if origin is None:
        errors.append(
            "the declared Layer R runtime_adapter has no verifiable source "
            "location, so it cannot be shown to be outside this repository"
        )
    elif _is_governed(origin):
        errors.append(
            f"the declared Layer R runtime_adapter ({_class_name(adapter)}) is "
            "defined inside the Deployment & Operations code; Deployment & "
            "Operations does not own the runtime it orchestrates"
        )
    if errors:
        raise ProductionCompositionError(errors)
    return adapter


def _validate_identity_provider(
    wiring: Any,
) -> tuple[PlatformIdentityProvider, bool, Any]:
    """Refuse anything that is not owner-produced actual identity evidence.

    The shipped ``OwnerSuppliedPlatformIdentityProvider`` is accepted only as
    the adapter over an owner-supplied source — never as an identity authority
    of its own. A provider the owner supplies directly must be the owner's own
    class, and an identity source must come from outside this repository in
    every case.
    """
    attributes = _owner_attributes(wiring, ("identity_provider", "identity_source"))
    if attributes is None:
        raise _refusal(
            "the declared Layer R wiring could not be inspected: reading its "
            "identity seam raised an exception, so this composition cannot "
            "establish what was supplied"
        )
    provider = attributes["identity_provider"]
    source = attributes["identity_source"]
    if provider is None and source is None:
        raise _refusal(
            "the declared Layer R answered with neither identity_provider nor "
            "identity_source; actual identity evidence must come from the "
            "Running Platform owner"
        )
    if isinstance(provider, OwnerSuppliedPlatformIdentityProvider):
        # The shipped adapter is accepted only as the adapter over the owner's
        # own source: the facts must still be the owner's, and the class the
        # composition holds is not itself an identity authority (ADR-0020 §4).
        # The source it actually carries is the one validated, whether the
        # wiring names it again or not.
        wrapped = getattr(provider, "_source", None)
        if source is not None and wrapped is not None and source is not wrapped:
            raise _refusal(
                "the declared Layer R wiring carries the shipped identity "
                "adapter over another source than the identity_source it names; "
                "this composition adapts exactly the owner's own source"
            )
        validated = _validate_owner_source(source if source is not None else wrapped)
        return provider, True, validated
    if provider is not None:
        return _validate_owner_provider(provider), False, None

    validated = _validate_owner_source(source)
    return OwnerSuppliedPlatformIdentityProvider(validated), True, validated


def _validate_owner_source(source: Any) -> Any:
    """Refuse anything that is not an owner-side actual-identity source.

    The facts belong to the Running Platform owner (ADR-0020 §4): a source
    defined inside this repository — the D&O-owned file surface included — is
    never the source of production actual identity.
    """
    seam = _owner_attributes(source, ("observe",))
    if seam is None:
        raise _refusal(
            "the declared Layer R identity_source could not be inspected: "
            "reading its observe seam raised an exception, so this composition "
            "cannot establish what was supplied"
        )
    if not callable(seam["observe"]):
        raise _refusal(
            "the declared Layer R identity_source does not implement the "
            "owner-side source contract (observe)"
        )
    origin = _class_origin(source)
    if origin is None:
        raise _refusal(
            "the declared Layer R identity_source has no verifiable source "
            "location, so it cannot be shown to be outside this repository"
        )
    if _is_governed(origin):
        raise _refusal(
            f"the declared Layer R identity_source ({_class_name(source)}) is "
            "not owner-side code: it is defined inside the Deployment & "
            "Operations code, and actual identity facts are produced by the "
            "Running Platform owner"
        )
    return source


def _validate_owner_provider(provider: Any) -> PlatformIdentityProvider:
    """Refuse anything that is not an owner-side identity provider.

    The same rule the resolver applies to a declared factory's answer, applied
    to a provider that is already in hand: a callable ``observe_identity``
    defined outside this repository. The D&O-owned file-backed surface is
    refused here as it is everywhere else (ADR-0020 §4).
    """
    seam = _owner_attributes(provider, ("observe_identity",))
    if seam is None:
        raise _refusal(
            "the production composition's identity provider could not be "
            "inspected: reading its identity seam raised an exception, so this "
            "composition cannot establish what was supplied"
        )
    if not callable(seam["observe_identity"]):
        raise _refusal(
            "the production composition's identity provider does not implement "
            "the identity seam (observe_identity)"
        )
    origin = _class_origin(provider)
    if origin is None or _is_governed(origin):
        raise _refusal(
            "the production composition's identity provider "
            f"({_class_name(provider)}) is not owner-side code; actual identity "
            "facts are produced by the Running Platform owner"
        )
    return provider


def validate_wiring(wiring: Any) -> None:
    """Refuse a wiring whose seams are not the owner-side seams it claims.

    The identical checks :func:`resolve_layer_r` applies to a declared
    factory's answer, applied to a wiring object: a wiring constructed by hand
    — bypassing the resolver — does not get to claim a validated composition,
    and a composition root may not report validated wiring in its document
    while holding seams that were never validated (ADR-0016 §7, §20).
    """
    if not isinstance(wiring, LayerRWiring):
        raise _refusal(
            "the production composition must be built from a resolved Layer R "
            "wiring; this object holds no wiring the resolver validated"
        )
    seams = _owner_attributes(
        wiring,
        (
            "runtime_adapter",
            "identity_provider",
            "identity_source",
            "adapted_owner_source",
        ),
    )
    if seams is None:
        # A wiring is a dataclass whose fields are read directly, but its fields
        # may hold owner-side objects whose own reads raise, and a subclass may
        # answer with a descriptor: whatever the read raises is a composition
        # refusal, never an arbitrary exception out of the composition (§7, §20).
        raise _refusal(
            "the production composition's wiring could not be inspected: reading "
            "its seams raised an exception, so this composition cannot establish "
            "what it holds"
        )
    _validate_runtime_adapter(seams["runtime_adapter"])
    _, adapted, _ = _validate_identity_provider(wiring)
    if bool(seams["adapted_owner_source"]) != adapted:
        raise _refusal(
            "the production composition's wiring misstates its identity seam: "
            f"it declares adapted_owner_source={bool(seams['adapted_owner_source'])} "
            "while carrying "
            + (
                "the shipped adapter over an owner-side source"
                if adapted
                else "the owner's own provider"
            )
            + "; a composition that reports a validated wiring has to show it"
        )


def resolve_layer_r(declaration: LayerREntryPoint) -> LayerRWiring:
    """Resolve one declared Running Platform into the two seams it supplies.

    Fail-closed and total: every outcome other than a conformant owner-side
    wiring is a :class:`ProductionCompositionError` raised before any
    Deployment & Operations operation exists.
    """
    module, module_path = _load_declared_module(declaration)
    attributes = _owner_attributes(module, (declaration.factory_name,))
    factory = attributes.get(declaration.factory_name) if attributes else None
    wiring = _call_declared_factory(factory, declaration, module_path)
    if wiring is None:
        raise _refusal(
            f"{module_path}:{declaration.factory_name} answered with no wiring; "
            "a Running Platform wiring is required"
        )
    seams = _owner_attributes(wiring, ("runtime_adapter",))
    if seams is None:
        raise _refusal(
            f"{module_path}:{declaration.factory_name} answered with an object "
            "whose runtime seam could not be inspected: reading it raised an "
            "exception, so this composition cannot establish what was supplied"
        )
    adapter = _validate_runtime_adapter(seams["runtime_adapter"])
    provider, adapted, validated_source = _validate_identity_provider(wiring)
    return LayerRWiring(
        declaration=declaration,
        module_path=module_path,
        runtime_adapter=adapter,
        identity_provider=provider,
        adapted_owner_source=adapted,
        identity_source=validated_source,
    )


__all__ = [
    "RUNTIME_ADAPTER_OPERATIONS",
    "LayerREntryPoint",
    "LayerRWiring",
    "ProductionCompositionError",
    "deployment_package_dir",
    "governed_roots",
    "parse_layer_r_reference",
    "resolve_layer_r",
    "shipped_owner_side_dir",
    "source_checkout_dir",
    "validate_wiring",
]
