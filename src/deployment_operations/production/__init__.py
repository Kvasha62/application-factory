"""The production composition root: explicit owner-side seams, injected.

This is the entry point the approved F-1 slice adds to Deployment & Operations
(ADR-0016 §18): production mode is *one explicit declaration* of the owner-side
Running Platform, resolved once, and then the existing operations are called
with those seams. There is no default, no discovery, no environment variable
and no fallback to the engine's local composition: the module cannot express
``LocalProcessRuntime`` or the D&O-owned ``<runtime_root>/running_platform_identity.json``
as production authority, and ``tests/test_production_composition.py`` checks
that structurally.

The boundaries this root does **not** cross, because Deployment & Operations
cannot honestly express them:

* ``RuntimeAdapter.stop`` is the established **detach**: it releases this
  operation's reference and leaves the lifecycle of the runtime to its owner.
  Fail-closed cleanup therefore ends with a detach, a restart's failed attempt
  detaches what it started (`_stop_fresh_elements`), and nothing here retires,
  kills or shuts down what the owner supervises. A restart whose stop phase
  fails records the conservative claim — ``running`` stands, ``ready`` is
  withdrawn — instead of asserting a stop that did not happen.
* An **interrupted** deploy (the Layer O process dies after Layer R started
  work) is recovered by nothing automatic. The engine persists the operation's
  record before it addresses Layer R, so the stage that was reached is on disk;
  this root refuses to re-run that attempt (never renumbering it) and ``attach``
  refuses a record that does not state an honest realized, running,
  identity-verified platform. Whether the standing runtime element is finished,
  adopted or retired is the **owner's** decision: this capability neither
  invents runtime state nor assumes partial success. The operator's next steps
  are an explicit new attempt number, or the owner's own action on its runtime,
  and whichever they choose is recorded by the engine as usual.

What this module adds to the engine, and why it belongs here:

* **validation of the composition itself.** A root is built from a resolved
  :class:`~deployment_operations.production.layer_r.LayerRWiring` and validates
  its seams at construction, so an object that reports a validated wiring in
  :meth:`ProductionCompositionRoot.document` cannot hold unvalidated seams —
  including one constructed by hand instead of through
  :meth:`ProductionCompositionRoot.from_declaration` (ADR-0016 §7, §20).
* **the duplicate-attempt pre-flight.** A deployment attempt is an identity
  derived from the environment, the exact instance and the attempt number
  (ADR-0016 §8, §9). Running the same attempt twice would rewrite the record
  that attempt already earned — history overwritten, not extended — so
  :meth:`ProductionCompositionRoot.deploy` refuses *before* Layer R is
  addressed, changes no state, and never silently renumbers the attempt.
* **cross-process serialization.** :meth:`ProductionCompositionRoot.mutation`
  holds a host-local file lock for one deployment operation, which is what lets
  the entry point run ``attach`` and a restart or reconciliation as one
  indivisible unit (see :mod:`deployment_operations.production.locking`).

It is not production evidence. A transcript from this entry point states which
dependencies were wired; a correspondence claim still has to be earned by a
real deployment against a real Running Platform, verified by the existing
acceptance seam, and audited independently (D1–D8).

Everything this module can refuse, it refuses **before** any operation is
called: a missing, relative, unreadable or D&O-owned declaration, a factory
that fails, an answer that is not the owner's runtime seam, an identity source
or provider that is not owner-side code, and a re-run of an attempt that
already has authoritative state.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from deployment_operations.deployment import (
    Deployment,
    DeploymentRequest,
    attach,
    deploy,
)
from deployment_operations.platform_identity import PlatformIdentityProvider
from deployment_operations.production.errors import ProductionOperationRefused
from deployment_operations.production.layer_r import (
    LayerREntryPoint,
    LayerRWiring,
    ProductionCompositionError,
    resolve_layer_r,
    validate_wiring,
)
from deployment_operations.production.locking import deployment_mutation
from deployment_operations.reconciliation import (
    Reconciliation,
    ReconciliationRequest,
    reconcile,
)
from deployment_operations.restart import Restart, RestartRequest, restart
from deployment_operations.runtime import RuntimeAdapter
from deployment_operations.state import DeploymentStateStore, derive_deployment_id

__all__ = [
    "LayerREntryPoint",
    "LayerRWiring",
    "ProductionCompositionError",
    "ProductionCompositionRoot",
    "ProductionOperationRefused",
    "compose_production_root",
    "deployment_identity",
    "existing_attempt_errors",
]


def deployment_identity(request: DeploymentRequest) -> str:
    """The deployment identity one request names (ADR-0016 §8, §9).

    Derived exactly as the engine derives it — the environment, the exact
    instance digest and the attempt number, never time or randomness — so the
    production path addresses the same operation the engine would.
    """
    environment = request.environment
    reference = request.instance
    return derive_deployment_id(
        reference.platform_id,
        reference.instance_digest,
        environment.environment_id,
        request.attempt,
    )


def existing_attempt_errors(request: DeploymentRequest) -> list[str]:
    """Why this request may not be deployed, when its attempt already exists.

    The check reads authoritative state only: the existence of the record the
    attempt's identity names. It never reads the record's contents, never
    inspects Layer R, and never mutates anything — a refusal here leaves the
    record, the journal and the Running Platform exactly as they stand
    (ADR-0016 §9, §20).
    """
    deployment_id = deployment_identity(request)
    environment = request.environment
    reference = request.instance
    state_path = DeploymentStateStore.path_for(
        environment.operations_dir, deployment_id
    )
    if not state_path.is_file():
        return []
    return [
        (
            f"{deployment_id}: this deployment attempt already exists — "
            f"environment {environment.environment_id!r}, instance "
            f"{reference.instance_digest!r}, attempt {request.attempt}. An "
            "attempt is an identity, not a slot: re-running it would overwrite "
            "the record it already earned, so this operation was refused before "
            "Layer R was addressed and nothing was changed. Read the existing "
            "record (deployment state, "
            f"{state_path}), continue from it with attach/reconcile/restart, or "
            "request a new attempt number — this path never renumbers an "
            "attempt by itself."
        )
    ]


@dataclass(frozen=True)
class ProductionCompositionRoot:
    """One production composition: the resolved Layer R wiring, validated.

    The root holds exactly one thing — the wiring a declared entry point
    resolved — and does exactly one thing with it: it passes the seams to the
    existing operations. It never calls the adapter, the provider or an
    owner-side producer itself, and it adds no rule the engine does not already
    enforce, except the two production pre-flights described in the module
    docstring (duplicate attempt, serialized mutation).
    """

    layer_r: LayerRWiring

    def __post_init__(self) -> None:
        validate_wiring(self.layer_r)

    @property
    def runtime(self) -> RuntimeAdapter:
        """The owner-side runtime seam this composition injects."""
        return self.layer_r.runtime_adapter

    @property
    def identity_provider(self) -> PlatformIdentityProvider:
        """The owner-side identity seam this composition injects."""
        return self.layer_r.identity_provider

    @classmethod
    def from_declaration(
        cls,
        declaration: LayerREntryPoint,
    ) -> ProductionCompositionRoot:
        """Resolve one declared Running Platform into a production composition."""
        return cls(layer_r=resolve_layer_r(declaration))

    def document(self) -> dict[str, object]:
        """The composition as a credential-free record.

        Recorded for every operation so that the transcript shows which
        owner-side module answered and states explicitly that the D&O-owned
        runtime and identity authority were not used.
        """
        document = dict(self.layer_r.document())
        document["local_defaults_used"] = False
        return document

    @contextmanager
    def mutation(
        self,
        request: DeploymentRequest,
        *,
        timeout: float | None = None,
    ) -> Iterator[None]:
        """Serialize one deployment operation's mutations on this host.

        The caller holds this across everything one indivisible unit mutates —
        for a restart or reconciliation, the ``attach`` re-binding *and* the
        operation — so no second process can interleave between them
        (ADR-0016 §9; :mod:`deployment_operations.production.locking`).
        """
        with deployment_mutation(
            request.environment, deployment_identity(request), timeout=timeout
        ):
            yield

    # -- the existing operations, with the production seams -----------------
    def deploy(self, request: DeploymentRequest) -> Deployment:
        """Realize one accepted Platform Instance through the Layer R seams.

        Refuses a re-run of an attempt that already has authoritative state
        before anything is addressed; see :func:`existing_attempt_errors`.
        """
        refusal = existing_attempt_errors(request)
        if refusal:
            raise ProductionOperationRefused(refusal)
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
