"""Production composition root — the explicit Layer O wiring of a real Running Platform.

The shipped Deployment & Operations engine is dependency-injected and
environment-agnostic: ``deploy``, ``attach``, ``reconcile`` and ``restart`` take
their runtime and identity seams as keyword arguments, and each of them falls
back to a **local** default when none is supplied (``LocalProcessRuntime`` and
the D&O-owned ``<runtime_root>/running_platform_identity.json`` surface). This
module is the missing production wiring: it constructs the two seams from an
explicitly declared, owner-side Running Platform and injects them, so a
production invocation never reaches those defaults.

    runtime            = <the declared Layer R RuntimeAdapter>
    identity_provider  = <the declared owner-side PlatformIdentityProvider>

    deploy(request, runtime=runtime, identity_provider=identity_provider)
    attach(request, runtime=runtime, identity_provider=identity_provider)
    reconcile(request, identity_provider=identity_provider)
    restart(request, runtime=runtime, identity_provider=identity_provider)

The entry point is explicit::

    python -m deployment_operations.production deploy \\
        --instance  <platform-instance.json> \\
        --manifest  <platform-manifest.json> \\
        --environment <environment.json> \\
        --layer-r /srv/running-platform/cell/rp_runtime_adapter.py:<factory>

What this module is, and is not
-------------------------------

It is **dependency construction only**. It contains no deployment semantics, no
runtime implementation, no identity production and no fallback: the engine is
untouched, and the Running Platform remains an external, owner-operated
Layer R dependency (ADR-0016 §18; ADR-0020 §4, §5). It creates nothing that
lives, owns no lifecycle and writes no authoritative Running Platform state.

It is not production evidence. A transcript from this entry point states which
dependencies were wired; a correspondence claim still has to be earned by a
real deployment against a real Running Platform, verified by the existing
acceptance seam, and audited independently (D1–D8).

Everything this module can refuse, it refuses **before** any operation is
called: a missing, relative, unreadable or D&O-owned declaration, a factory that
fails, an answer that is not the owner's runtime seam, and an identity source or
provider that is not owner-side code are all
:class:`~deployment_operations.production.layer_r.ProductionCompositionError`s.
There is no path from here to ``LocalProcessRuntime`` or to the D&O-owned
identity file: the module cannot express either, and the structural guard in
``tests/test_production_composition.py`` keeps it that way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    attach,
    deploy,
)
from deployment_operations.platform_identity import PlatformIdentityProvider
from deployment_operations.production.layer_r import (
    LayerREntryPoint,
    LayerRWiring,
    ProductionCompositionError,
    resolve_layer_r,
)
from deployment_operations.reconciliation import (
    Reconciliation,
    ReconciliationRequest,
    reconcile,
)
from deployment_operations.restart import Restart, RestartRequest, restart
from deployment_operations.runtime import RuntimeAdapter

__all__ = [
    "LayerREntryPoint",
    "LayerRWiring",
    "ProductionCompositionError",
    "ProductionCompositionRoot",
    "compose_production_root",
]


@dataclass(frozen=True)
class ProductionCompositionRoot:
    """One production composition: the Layer R seams, injected into the engine.

    The root holds exactly three things and does exactly one thing with them:
    it passes them to the existing operations. It never calls the adapter, the
    provider or an owner-side producer itself, and it adds no rule the engine
    does not already enforce.
    """

    layer_r: LayerRWiring
    runtime: RuntimeAdapter
    identity_provider: PlatformIdentityProvider

    @classmethod
    def from_declaration(
        cls,
        declaration: LayerREntryPoint,
    ) -> ProductionCompositionRoot:
        """Resolve one declared Running Platform into a production composition."""
        wiring = resolve_layer_r(declaration)
        return cls(
            layer_r=wiring,
            runtime=wiring.runtime_adapter,
            identity_provider=wiring.identity_provider,
        )

    def document(self) -> dict[str, object]:
        """The composition as a credential-free record.

        Recorded for every operation so that the transcript shows which
        owner-side module answered and states explicitly that the D&O-owned
        runtime and identity authority were not used.
        """
        document = dict(self.layer_r.document())
        document["local_defaults_used"] = False
        return document

    # -- the existing operations, with the production seams -----------------
    def deploy(self, request: DeploymentRequest) -> Deployment:
        """Realize one accepted Platform Instance through the Layer R seams."""
        return deploy(
            request,
            runtime=self.runtime,
            identity_provider=self.identity_provider,
        )

    def attach(self, request: DeploymentRequest) -> Deployment:
        """Re-bind one already-realized operation to the standing Running Platform.

        The re-binding path of the approved F-1 slice (ADR-0016 §18): it binds
        the runtime elements Layer R already supervises and re-verifies the
        actual platform identity through the same seam. It creates, starts,
        stops, restarts and migrates nothing — that is what makes
        ``reconcile`` and ``restart`` reachable in a fresh Layer O process.
        """
        return attach(
            request,
            runtime=self.runtime,
            identity_provider=self.identity_provider,
        )

    def reconcile(self, request: ReconciliationRequest) -> Reconciliation:
        """Observe the Running Platform through the owner-side identity seam.

        The operation takes no runtime action of any kind: this method injects
        the identity provider only, because reconciliation observes and records
        (ADR-0016 §18, §20).
        """
        return reconcile(request, identity_provider=self.identity_provider)

    def restart(self, request: RestartRequest) -> Restart:
        """Re-execute the runtime of one realized operation through both seams."""
        return restart(
            request,
            runtime=self.runtime,
            identity_provider=self.identity_provider,
        )


def compose_production_root(
    reference: str,
    options: Mapping[str, str] | None = None,
) -> ProductionCompositionRoot:
    """Build the production composition from one explicit Layer R declaration.

    ``reference`` is ``<absolute-path.py>:<factory>`` or
    ``<dotted.module>:<factory>``; ``options`` are passed to that factory as
    keyword arguments. There is no default declaration and no environment
    variable to fall back to: without a declaration this raises
    :class:`ProductionCompositionError`.
    """
    declaration = LayerREntryPoint(reference=reference, options=dict(options or {}))
    return ProductionCompositionRoot.from_declaration(declaration)
