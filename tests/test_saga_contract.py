"""Machine-readable Component Contract validation for IS-006 (Issue #18).

The contract is checked the way the other components' contracts are: the
required fields must exist, and — because a contract that drifts from the code
is worse than none — the declared state machine, terminal states and audited
actions must be *the ones the implementation actually has*.
"""

from __future__ import annotations

import json
from pathlib import Path

from saga import executor as saga_executor
from saga.models import (
    COMPONENT_CLASS,
    COMPONENT_ID,
    COMPONENT_VERSION,
    SAGA_TRANSITIONS,
    STEP_TRANSITIONS,
    TERMINAL_SAGA_STATES,
    SagaState,
    StepState,
)

REQUIRED = {
    "api",
    "events",
    "data_export_cdc",
    "ui",
    "configuration_schema",
    "data_ownership",
    "authn",
    "authz",
    "compatibility_policy",
    "dependencies",
}

CONTRACT_PATH = Path("components/saga/contract/component_contract.json")
OPENAPI_PATH = Path("components/saga/contract/openapi.yaml")


def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_component_contract_has_the_required_fields():
    data = contract()
    assert not REQUIRED - set(data)
    assert data["component_id"] == COMPONENT_ID == "saga"
    assert data["component_version"] == COMPONENT_VERSION
    assert data["class"] == COMPONENT_CLASS == "platform_service"
    assert data["maturity_level"] == "level_0_modular_monolith"
    assert data["task"] == "IS-006 (GitHub Issue #18)"


def test_the_declared_state_model_is_the_implemented_one():
    data = contract()
    model = data["state_model"]

    assert set(model["saga_states"]) == {state.value for state in SagaState}
    assert set(model["step_states"]) == {state.value for state in StepState}
    assert set(model["terminal_saga_states"]) == {
        state.value for state in TERMINAL_SAGA_STATES
    }
    assert {
        state: sorted(targets) for state, targets in model["saga_transitions"].items()
    } == {
        state.value: sorted(target.value for target in targets)
        for state, targets in SAGA_TRANSITIONS.items()
    }
    assert {
        state: sorted(targets) for state, targets in model["step_transitions"].items()
    } == {
        state.value: sorted(target.value for target in targets)
        for state, targets in STEP_TRANSITIONS.items()
    }
    # The Issue's minimal state model, stated explicitly in the contract.
    assert model["saga_transitions"]["STARTED"] == ["RUNNING"]
    assert model["saga_transitions"]["RUNNING"] == [
        "COMPLETED",
        "COMPENSATING",
        "FAILED",
    ]
    assert model["saga_transitions"]["COMPENSATING"] == ["COMPENSATED", "FAILED"]
    assert "RETRYING" in model["step_states"]
    assert "RETRYING" not in model["saga_states"]  # step-level, as the Issue requires


def test_the_saga_contract_fields_of_architecture_6_4_are_declared():
    fields = contract()["saga_contract"]["fields"]
    assert set(fields) == {
        "saga_id",
        "correlation_id",
        "tenant_id",
        "step_id",
        "state",
        "retry_policy",
        "timeout",
        "compensation",
        "idempotency",
        "terminal_state",
    }


def test_the_declared_audit_actions_are_the_ones_the_executor_writes():
    declared = set(contract()["observability"]["audited_actions"])
    implemented = {
        value
        for name, value in vars(saga_executor).items()
        if name.startswith("ACTION_") and isinstance(value, str)
    }
    assert implemented
    assert implemented <= declared
    required = {
        "saga.created",
        "saga.transition",
        "saga.terminal",
        "saga.start_refused",
        "saga.delivery_refused",
        "saga.step.failed",
        "saga.step.retrying",
        "saga.step.compensating",
        "saga.step.compensated",
        "saga.step.compensation_failed",
    }
    assert required <= declared
    context = set(contract()["observability"]["context_fields"])
    assert {
        "saga_id",
        "tenant_id",
        "request_id",
        "correlation_id",
        "trace_id",
    } <= context
    assert {"identity_id", "actor_service_id"} <= context


def test_ownership_declares_workflow_state_and_no_business_data():
    ownership = contract()["data_ownership"]
    assert ownership["owner"] == "saga"
    scopes = {dataset["name"]: dataset["scope"] for dataset in ownership["datasets"]}
    assert scopes == {
        "saga_records": "tenant-scoped",
        "step_records": "tenant-scoped",
        "workflow_audit": "platform-scoped",
    }
    notes = " ".join(ownership["notes"]).lower()
    assert "no copy of business data" in notes
    assert "no credential is persisted" in notes


def test_the_component_claims_no_authorization_or_identity_mechanism_of_its_own():
    data = contract()
    assert data["authz"]["enforcement_boundary"] == "data_owner"
    assert data["authz"]["decision_source"] == "authorization"
    assert data["authz"]["tenant_context_source"] == "identity"
    assert data["authz"]["tenant_lifecycle_source"] == "tenant_authority"
    assert data["authz"]["default_decision"] == "DENY"
    assert data["authz"]["authentication_does_not_imply_authorization"] is True
    assert data["authz"]["operations_guarded"] == []
    assert data["authn"]["service"] == "delegated"
    assert data["authn"]["human"] == "delegated"


def test_events_and_cdc_are_declared_only_because_no_infrastructure_exists():
    data = contract()
    assert data["events"]["status"] == "declared_only"
    assert data["events"]["published"] == []
    assert data["events"]["consumed"] == []
    assert data["data_export_cdc"]["status"] == "declared_only"
    assert data["data_export_cdc"]["streams"] == []
    assert data["api"]["openapi"] == "not_applicable"
    assert data["ui"]["kind"] == "none"


def test_dependencies_are_the_two_consumed_boundaries_only():
    dependencies = contract()["dependencies"]
    assert {item["component_id"] for item in dependencies} == {
        "idempotency_guard",
        "identity",
    }
    for item in dependencies:
        assert item["kind"] in {"api", "internal-consumer-surface"}
        assert Path(item["contract"]).is_file()
    guard = next(
        item for item in dependencies if item["component_id"] == "idempotency_guard"
    )
    assert "no second idempotency mechanism" in guard["note"]


def test_the_non_goals_of_the_issue_are_declared_in_the_contract():
    declared = " ".join(contract()["non_goals"]).lower()
    for forbidden in (
        "kafka",
        "rabbitmq",
        "redis",
        "celery",
        "temporal",
        "distributed transaction coordinator",
        "distributed lock",
        "event-bus",
        "universal dal",
        "component catalog",
        "golden bundles",
        "release trains",
    ):
        assert forbidden in declared, forbidden


def test_the_level_0_allowance_states_its_own_limitations():
    allowance = contract()["level_0_allowance"]
    assert "bounded in-memory" in allowance["store"]
    assert allowance["stated_limitations"]
    assert any("timeout" in item for item in allowance["stated_limitations"])
    assert any("in-process" in item for item in allowance["stated_limitations"])


def test_the_published_surface_names_the_operations_and_errors_of_the_code():
    surface = contract()["api"]["consumer_surface"]
    assert surface["module"] == "saga"
    assert set(surface["operations"]) == {
        "start",
        "run",
        "deliver_step",
        "snapshot",
        "audit_trail",
    }

    import saga

    for name in surface["published_as"]:
        assert hasattr(saga, name), name
    for name in surface["errors"]:
        assert hasattr(saga, name), name
    assert set(surface["internal_modules"]) == {
        "saga.executor",
        "saga.store",
        "saga.models",
        "saga.ports",
        "saga.consumed",
        "saga.errors",
    }


def test_the_twelve_invariants_of_the_issue_are_declared():
    invariants = contract()["invariants"]
    assert len(invariants) == 12
    assert [item.split()[0] for item in invariants] == [
        f"S-{i:03d}" for i in range(1, 13)
    ]


def test_the_configuration_schema_describes_the_only_two_knobs():
    schema = contract()["configuration_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"actor_service_id", "max_sagas"}
    assert schema["properties"]["max_sagas"]["default"] == 256


def test_openapi_describes_the_published_values_and_no_endpoint_is_claimed():
    assert OPENAPI_PATH.is_file()
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    for schema in ("SagaSnapshot", "StepSnapshot", "FailureInfo", "SagaAuditEvent"):
        assert f"{schema}:" in text
    assert "paths:" not in text  # no HTTP surface is published in this slice
    assert "no HTTP endpoint" in text
