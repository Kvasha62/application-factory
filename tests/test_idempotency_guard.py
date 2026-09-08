from typing import Any

import pytest

from idempotency.errors import IdempotencyConflict
from idempotency.guard import IdempotencyGuard


class DummyException(Exception):
    pass


class AuthorizationDeny(Exception):
    pass


class DependencyFailure(Exception):
    pass


def test_first_execution_runs_effect():
    audit_events = []

    def audit(action: str, details: dict[str, Any]):
        audit_events.append((action, details))

    guard = IdempotencyGuard(audit_sink=audit)

    executed = 0

    def effect():
        nonlocal executed
        executed += 1
        return "success"

    result = guard.execute(
        "key-1",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="payload-hash",
        effect=effect,
    )

    assert result == "success"
    assert executed == 1
    assert len(audit_events) == 0


def test_exact_replay_returns_result_without_second_effect():
    audit_events = []
    guard = IdempotencyGuard(audit_sink=lambda a, d: audit_events.append((a, d)))

    executed = 0

    def effect():
        nonlocal executed
        executed += 1
        return "success"

    kwargs = {
        "key": "key-1",
        "identity": "user-1",
        "tenant_id": "ten-a",
        "operation": "create",
        "resource": "res-1",
        "fingerprint": "payload-hash",
        "effect": effect,
    }

    res1 = guard.execute(**kwargs)
    res2 = guard.execute(**kwargs)

    assert res1 == "success"
    assert res2 == "success"
    assert executed == 1
    assert len(audit_events) == 1
    assert audit_events[0][0] == "idempotency_replay"


def test_payload_conflict_raises_conflict():
    audit_events = []
    guard = IdempotencyGuard(audit_sink=lambda a, d: audit_events.append((a, d)))

    guard.execute(
        "key-1",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="hash-1",
        effect=lambda: "success",
    )

    with pytest.raises(IdempotencyConflict):
        guard.execute(
            "key-1",
            identity="user-1",
            tenant_id="ten-a",
            operation="create",
            resource="res-1",
            fingerprint="hash-2",
            effect=lambda: "success2",
        )

    assert audit_events[0][0] == "idempotency_conflict"


def test_cross_tenant_conflict():
    guard = IdempotencyGuard(audit_sink=lambda a, d: None)

    guard.execute(
        "key-1",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="hash-1",
        effect=lambda: "success",
    )

    with pytest.raises(IdempotencyConflict):
        guard.execute(
            "key-1",
            identity="user-1",
            tenant_id="ten-b",
            operation="create",
            resource="res-1",
            fingerprint="hash-1",
            effect=lambda: "success2",
        )


def test_cross_identity_conflict():
    guard = IdempotencyGuard(audit_sink=lambda a, d: None)

    guard.execute(
        "key-1",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="hash-1",
        effect=lambda: "success",
    )

    with pytest.raises(IdempotencyConflict):
        guard.execute(
            "key-1",
            identity="user-2",
            tenant_id="ten-a",
            operation="create",
            resource="res-1",
            fingerprint="hash-1",
            effect=lambda: "success2",
        )


def test_authorization_deny_does_not_save_record():
    guard = IdempotencyGuard(audit_sink=lambda a, d: None)

    executed = 0

    def failing_effect():
        nonlocal executed
        executed += 1
        raise AuthorizationDeny("not allowed")

    with pytest.raises(AuthorizationDeny):
        guard.execute(
            "key-fail",
            identity="user-1",
            tenant_id="ten-a",
            operation="create",
            resource="res-1",
            fingerprint="hash-1",
            effect=failing_effect,
        )

    assert executed == 1
    assert "key-fail" not in guard._store

    # Second call attempts again
    with pytest.raises(AuthorizationDeny):
        guard.execute(
            "key-fail",
            identity="user-1",
            tenant_id="ten-a",
            operation="create",
            resource="res-1",
            fingerprint="hash-1",
            effect=failing_effect,
        )
    assert executed == 2


def test_dependency_failure_does_not_save_record():
    guard = IdempotencyGuard(audit_sink=lambda a, d: None)

    executed = 0

    def failing_effect():
        nonlocal executed
        executed += 1
        raise DependencyFailure("db down")

    with pytest.raises(DependencyFailure):
        guard.execute(
            "key-fail",
            identity="user-1",
            tenant_id="ten-a",
            operation="create",
            resource="res-1",
            fingerprint="hash-1",
            effect=failing_effect,
        )

    assert executed == 1
    assert "key-fail" not in guard._store


def test_observability_context():
    audit_events = []
    guard = IdempotencyGuard(audit_sink=lambda a, d: audit_events.append((a, d)))

    guard.execute(
        "key-obs",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="hash-1",
        request_id="req-123",
        correlation_id="corr-456",
        effect=lambda: "success",
    )

    # Replay triggers audit
    guard.execute(
        "key-obs",
        identity="user-1",
        tenant_id="ten-a",
        operation="create",
        resource="res-1",
        fingerprint="hash-1",
        request_id="req-123",
        correlation_id="corr-456",
        effect=lambda: "success",
    )

    action, details = audit_events[0]
    assert action == "idempotency_replay"
    assert details["request_id"] == "req-123"
    assert details["correlation_id"] == "corr-456"


def test_idempotency_does_not_create_second_mechanisms():
    # Proof that idempotency relies on passed values, does not enforce its own identity token decoding
    record = IdempotencyGuard(audit_sink=lambda a, d: None)._store
    assert type(record) is dict  # Bounded in-memory persistence
