"""Published consumer surface of the Resource Boundary component (IS-004).

A consumer receives exactly one object: :class:`RecordsClient`. It offers the
two operations of the published contract — read one resource, transition one
resource — and returns an immutable :class:`records_service.contracts.ResourceView`
or raises :class:`records_service.errors.AccessRefused`.

Its state is one value: an opaque contract-channel handle. No credential is
bound to the client — the subject credential is supplied per access, because
it is the subject, not the consumer, whom this boundary authenticates through
the decision. No callable, closure or cell is stored, so the component's ASGI
application, deployment, engine, resource store and audit journal are
unreachable through any attribute of the client. This module keeps no
reference to the internal transport either — the executor is resolved at call
time — so importing the published surface does not open a door to the channel
table or to the application behind it.

What the client cannot do is as important as what it can: it cannot list
resources, read the store, read the audit journal, register a resource or
change a state without going through the published operations — which is to
say, without going through the enforcement chain (invariant 6).
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from records_service.contracts import PUBLISHED_DENY_REASONS, ResourceView
from records_service.errors import AccessRefused, ContractViolation

__all__ = [
    "RecordsClient",
    "ResourceView",
    "build_client",
]


def _view_from(payload: Mapping[str, Any]) -> ResourceView:
    """Rebuild the published value; an answer outside the contract fails closed."""
    try:
        return ResourceView(
            resource_id=str(payload["resource_id"]),
            resource_type=str(payload["resource_type"]),
            owner_component=str(payload["owner_component"]),
            tenant_id=str(payload["tenant_id"]),
            state=str(payload["state"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the records contract answered outside its published resource model"
        ) from exc


class RecordsClient:
    """Value-only access reader over the published records contract.

    ``GET /api/v1/resources/{resource_id}`` and
    ``POST /api/v1/resources/{resource_id}/transitions`` are the only
    operations it performs; the exact same API is available to a remote
    consumer.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that issued
    them.
    """

    __slots__ = ("_channel", "__weakref__")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a records contract channel handle is required")
        self._channel = channel

    # -------------------------------------------------------------- operations
    def read_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ResourceView:
        """Read one owned resource through the enforcement chain.

        ``subject_credential`` is the credential presented by the subject; it
        is verified by IS-001 inside the IS-003 decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded
        as a cross-check only: it can never select the effective tenant.
        """
        return self._request(
            "GET",
            f"/api/v1/resources/{quote(resource_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            body=None,
        )

    def transition_resource(
        self,
        subject_credential: str | None,
        resource_id: str,
        transition: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ResourceView:
        """Transition the state of one owned resource through the chain."""
        return self._request(
            "POST",
            f"/api/v1/resources/{quote(resource_id, safe='')}/transitions",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            body={"transition": transition},
        )

    # --------------------------------------------------------------- internals
    def _request(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
        body: Mapping[str, Any] | None,
    ) -> ResourceView:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        status, payload = _call_contract()(self._channel, method, path, headers, body)
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _view_from(payload)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        if status >= 500:
            # A fail-closed dependency is still a refusal of this boundary,
            # and the contract says so with a 503: it is not a transport
            # failure, so a consumer receives the refusal as data.
            reason = _reason_of(payload)
            if reason is not None:
                return AccessRefused(reason, status_code=status)
            return ContractViolation(f"records contract failed with status {status}")
        reason = _reason_of(payload)
        if reason is None:
            return ContractViolation(
                "the records contract refused without a published reason"
            )
        return AccessRefused(reason, status_code=status)


def _reason_of(payload: Mapping[str, Any]) -> str | None:
    """The machine-readable reason of a refusal, if the answer states one."""
    detail = payload.get("detail")
    if not isinstance(detail, Mapping):
        return None
    reason = detail.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if reason == "malformed_request" or reason in PUBLISHED_DENY_REASONS:
        return reason
    # A reason outside the published vocabulary is not repeated: the consumer
    # fails closed on a contract violation instead.
    return None


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the boundary:
    after ``import records_service.reader`` this module's namespace holds no
    module object, no channel table, no transport class and no application.
    """
    from records_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> RecordsClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer revokes
    that handle, so the table is not a place where applications accumulate.
    """
    from records_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = RecordsClient(channel)
    revoke_on_death(client, channel)
    return client
