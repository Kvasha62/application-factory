from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    identity: str | None
    tenant_id: str | None
    operation: str
    resource: str | None
    fingerprint: str
    result: Any
