"""Published consumer surface of the Learning component (SCS-001).

A consumer receives exactly one object: :class:`LearningClient`. It offers
the read operations of the published contract — read one submission and
list one Assignment's submissions — and returns immutable
:class:`learning_service.contracts.SubmissionView` values or raises
:class:`learning_service.errors.AccessRefused`.

Its state is one value: an opaque contract-channel handle. No credential
is bound to the client — the subject credential is supplied per access,
because it is the subject, not the consumer, whom this boundary
authenticates through the decision. No callable, closure or cell is
stored, so the component's ASGI application, deployment, engine, stores
and audit journal are unreachable through any attribute of the client.
This module keeps no reference to the internal transport either — the
executor is resolved at call time — so importing the published surface
does not open a door to the channel table or to the application behind it.

What the client cannot do is as important as what it can: it cannot read
the store, read the audit journal, register records or reach submissions
except through the published operations — which is to say, without going
through the enforcement chain.

Refusals arrive in the approved SCS-001 envelope (``error.code`` /
``error.message`` / ``error.details`` plus top-level ``request_id`` /
``correlation_id``). Any other shape — including the legacy ``detail``
shape — is a contract violation and fails closed.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from learning_service.contracts import PUBLISHED_DENY_REASONS, SubmissionView
from learning_service.errors import AccessRefused, ContractViolation

__all__ = [
    "LearningClient",
    "SubmissionView",
    "build_client",
]


def _view_from(payload: Mapping[str, Any]) -> SubmissionView:
    """Rebuild the published value; an answer outside the contract fails closed."""
    try:
        content = payload["content"]
        if not isinstance(content, dict):
            raise ValueError("content must be an object")
        return SubmissionView(
            submission_id=str(payload["submission_id"]),
            assignment_id=str(payload["assignment_id"]),
            student_identity_id=str(payload["student_identity_id"]),
            attempt=int(payload["attempt"]),
            content=dict(content),
            status=str(payload["status"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published submission model"
        ) from exc


class LearningClient:
    """Value-only access reader over the published learning contract.

    It performs the two read operations of the published API — the
    single-submission read and the teacher submission-discovery list; the
    exact same API is available to a remote consumer.

    ``channel`` is the opaque handle of a contract channel opened by the
    provider. It is deliberately not a callable: a consumer holding values
    cannot walk from them into the object graph of the component that
    issued them.
    """

    __slots__ = ("_channel", "__weakref__")

    def __init__(self, channel: str) -> None:
        if not isinstance(channel, str) or not channel:
            raise ValueError("a learning contract channel handle is required")
        self._channel = channel

    # -------------------------------------------------------------- operations
    def read_submission(
        self,
        subject_credential: str | None,
        submission_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SubmissionView:
        """Read one owned submission through the enforcement chain.

        ``subject_credential`` is the credential presented by the subject;
        it is verified by IS-001 inside the IS-003 decision, so a consumer
        cannot assert who the subject is. ``claimed_tenant_id`` is forwarded
        as a cross-check only: it can never select the effective tenant.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/submissions/{quote(submission_id, safe='')}",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _view_from(payload)

    def list_submissions(
        self,
        subject_credential: str | None,
        assignment_id: str,
        *,
        claimed_tenant_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[SubmissionView]:
        """List one Assignment's submissions through the enforcement chain.

        The teacher discovery operation of the published contract: an empty
        Assignment answers an empty list, and a caller outside the
        Assignment's tenant — or without the list grant — receives a
        refusal, never a partial set.
        """
        status, payload = self._call(
            "GET",
            f"/api/v1/learning/assignments/{quote(assignment_id, safe='')}/submissions",
            subject_credential=subject_credential,
            claimed_tenant_id=claimed_tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return _views_from(payload)

    # --------------------------------------------------------------- internals
    def _call(
        self,
        method: str,
        path: str,
        *,
        subject_credential: str | None,
        claimed_tenant_id: str | None,
        request_id: str | None,
        correlation_id: str | None,
    ) -> tuple[int, Mapping[str, Any]]:
        headers: list[tuple[str, str]] = []
        if subject_credential is not None:
            headers.append(("authorization", f"Bearer {subject_credential}"))
        if claimed_tenant_id:
            headers.append(("x-tenant-id", claimed_tenant_id))
        if request_id:
            headers.append(("x-request-id", request_id))
        if correlation_id:
            headers.append(("x-correlation-id", correlation_id))
        return _call_contract()(self._channel, method, path, headers, None)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> BaseException:
        refusal = _refusal_of(payload)
        if refusal is None:
            if status >= 500:
                return ContractViolation(f"learning contract failed with status {status}")
            return ContractViolation(
                "the learning contract refused without a published reason"
            )
        reason, request_id, correlation_id = refusal
        return AccessRefused(
            reason,
            status_code=status,
            request_id=request_id,
            correlation_id=correlation_id,
        )


def _views_from(payload: Mapping[str, Any]) -> list[SubmissionView]:
    """Rebuild the published ``{items: [...]}`` list; anything else fails closed."""
    try:
        items = payload["items"]
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        return [_view_from(item) for item in items]
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(
            "the learning contract answered outside its published list model"
        ) from exc


def _refusal_of(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read the approved error envelope: (reason, request_id, correlation_id).

    The envelope shape is strict: ``error.code`` must be present, the
    internal reason must come from ``error.details.reason`` and belong to
    the published vocabulary, and top-level ``request_id`` /
    ``correlation_id`` must be present. Anything else fails closed.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None
    details = error.get("details")
    if not isinstance(details, Mapping):
        return None
    reason = details.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    if reason != "malformed_request" and reason not in PUBLISHED_DENY_REASONS:
        return None
    request_id = payload.get("request_id")
    correlation_id = payload.get("correlation_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(correlation_id, str) or not correlation_id:
        return None
    return (reason, request_id, correlation_id)


def _call_contract() -> Any:
    """Resolve the internal contract executor at call time, keeping no reference.

    A local import is not a shortcut around the boundary, it is the
    boundary: after ``import learning_service.reader`` this module's
    namespace holds no module object, no channel table, no transport class
    and no application.
    """
    from learning_service.transport import call_contract

    return call_contract


def build_client(app: Any) -> LearningClient:
    """Publish the component for one consumer: contract app in, value-only client out.

    ``app`` — the component's published ASGI contract — is handed to the
    provider's own channel table and stays there: the client receives the
    handle back, not the application. A client dropped by its consumer
    revokes that handle, so the table is not a place where applications
    accumulate.
    """
    from learning_service.transport import open_channel, revoke_on_death

    channel = open_channel(app)
    client = LearningClient(channel)
    revoke_on_death(client, channel)
    return client
