import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .errors import IdempotencyConflict
from .models import IdempotencyRecord

T = TypeVar("T")


class IdempotencyGuard:
    """Executable guard enforcing exactly-once business effect (IS-005)."""

    def __init__(self, audit_sink: Callable[[str, dict[str, Any]], None]):
        self._store: dict[str, IdempotencyRecord] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._audit = audit_sink

    def execute(
        self,
        key: str | None,
        *,
        identity: str | None,
        tenant_id: str | None,
        operation: str,
        resource: str | None,
        fingerprint: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
        effect: Callable[[], T],
    ) -> T:
        """
        Executes `effect` safely against replays.

        If `key` is missing or empty, execution is refused to prevent bypass.
        """
        if not key:
            raise IdempotencyConflict(
                "Idempotency key is required.", details={"key": key}
            )

        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            key_lock = self._locks[key]

        with key_lock:
            existing = self._store.get(key)
            if existing:
                # Replay attempt. Validate identity and fingerprint.
                if (
                    existing.identity != identity
                    or existing.tenant_id != tenant_id
                    or existing.operation != operation
                    or existing.resource != resource
                    or existing.fingerprint != fingerprint
                ):
                    self._audit(
                        "idempotency_conflict",
                        {
                            "key": key,
                            "request_id": request_id,
                            "correlation_id": correlation_id,
                            "identity": identity,
                            "tenant_id": tenant_id,
                            "operation": operation,
                        },
                    )
                    raise IdempotencyConflict(
                        "Idempotency conflict: key already used with different context.",
                        details={"key": key},
                    )

                # Exact replay. Return the existing result, DO NOT execute effect.
                self._audit(
                    "idempotency_replay",
                    {
                        "key": key,
                        "request_id": request_id,
                        "correlation_id": correlation_id,
                        "identity": identity,
                        "tenant_id": tenant_id,
                        "operation": operation,
                    },
                )
                return existing.result

            # No existing record. Execute the effect.
            # If effect raises (e.g. AuthorizationDenied), it bubbles up and
            # no idempotency record is saved.
            result = effect()

            self._store[key] = IdempotencyRecord(
                key=key,
                identity=identity,
                tenant_id=tenant_id,
                operation=operation,
                resource=resource,
                fingerprint=fingerprint,
                result=result,
            )
            return result
