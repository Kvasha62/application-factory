from identity_service.engine import IdentityEngine
from identity_service.store import IdentityStore


def engine() -> IdentityEngine:
    store = IdentityStore()
    store.seed_demo()
    return IdentityEngine(store)
