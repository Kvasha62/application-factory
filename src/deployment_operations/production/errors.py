"""The refusal vocabulary of the production entry point.

A *refusal* is not a failure of an operation: it is the deterministic answer of
the production path before any operation exists. Nothing was addressed, no
deployment state was written and no runtime element was touched, and the answer
names what was refused so the operator can act on it (ADR-0016 §8, §9, §20).

Kept separate from :mod:`deployment_operations.production.layer_r` so that a
refusal raised while a mutation is being serialized does not depend on the
Layer R loader: the entry point must be able to refuse an attempt whose
owner-side declaration was never loaded.
"""

from __future__ import annotations

from collections.abc import Sequence

from deployment_operations.errors import DeploymentOperationsError

__all__ = ["ProductionOperationRefused"]


class ProductionOperationRefused(DeploymentOperationsError):
    """The requested production operation was refused before it began.

    The production entry point reports a refusal as an input/composition
    refusal (exit code 2): it is neither an operation failure that changed
    something nor an identity refusal, and treating either as the other would
    misstate what the state on disk says (ADR-0016 §9, §20).
    """

    def __init__(
        self,
        errors: Sequence[str],
        *,
        message: str = (
            "the production operation was refused before it began; nothing was "
            "addressed and nothing was written"
        ),
        stage: str = "request",
    ) -> None:
        super().__init__(message, errors=list(errors), stage=stage)
