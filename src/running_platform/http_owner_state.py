"""Transport reader for the owner-published Running Platform identity surface.

LOCAL / NON-PRODUCTION (local Docker stack, ``local/compose.yaml``; the
classification every transcript produced with it carries is ``LOCAL DOCKER /
NON-PRODUCTION``). This module is the D&O-side end of an explicit transport
boundary. It:

* sends exactly one opaque evaluation binding and nothing else — no expected
  Platform Instance, no Manifest, no configuration, no Golden Bundle, no
  artifact identity, no DeploymentRecord or request value;
* validates whatever the Running Platform owner side answers with the same
  surface validation the file reader uses
  (:func:`running_platform.owner_state.read_owner_state_surface`), so a
  transport cannot weaken, reorder or re-implement the acceptance rules;
* enforces a declared bound on every blocking socket operation and re-checks a
  monotonic deadline before any answer is accepted, so a stalled or
  slow-answering producer can neither make Deployment & Operations wait past
  the bound nor turn a late answer into acceptance.

It authors no identity, holds no identity state, and reads no ambient file: it
is a reader of the owner's surface across a transport, not an identity source.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from deployment_operations.platform_identity import (
    ActualIdentityUnavailable,
    PlatformIdentityBinding,
)
from running_platform.owner_state import (
    RunningPlatformOwnerState,
    read_owner_state_surface,
)

#: The only request key that may cross the boundary: the opaque binding.
BINDING_TOKEN_KEY = "binding_token"

#: Response cap: the owner identity surface is a small identity document.
MAX_RESPONSE_BYTES = 262_144


def _utc_now() -> str:
    """Timestamp for the observation transcript, with microseconds."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class HttpRunningPlatformOwnerStateReader:
    """Read the owner-published surface over an explicit transport boundary.

    ``observations`` is the D&O-side transcript: for every observation it
    records the exact request bytes, their digest, the declared bound, how long
    the reader waited, and the outcome. It records no secret material.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
            message = "identity endpoint must be an explicit http://host:port/path"
            raise ValueError(message)
        if not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
            message = "timeout_seconds is the declared bound and must be positive"
            raise ValueError(message)
        self._endpoint = endpoint
        self._host = parsed.hostname
        self._port = parsed.port
        self._path = parsed.path or "/"
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self.observations: list[dict[str, object]] = []

    @property
    def endpoint(self) -> str:
        """The transport endpoint this reader crosses."""
        return self._endpoint

    @property
    def timeout_seconds(self) -> float:
        """The declared bound of one observation, in seconds."""
        return self._timeout_seconds

    def request_body(self, binding: PlatformIdentityBinding) -> bytes:
        """Return the exact bytes that cross the boundary.

        One opaque evaluation binding. The method is public because the
        non-forwarding property is checkable: what crosses is exactly these
        bytes, and nothing else is ever sent.
        """
        token = binding.token
        if not isinstance(token, str) or not token:
            raise ActualIdentityUnavailable(
                "the evaluation binding is not a transportable opaque token"
            )
        return json.dumps(
            {BINDING_TOKEN_KEY: token},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")

    def read(self, binding: PlatformIdentityBinding) -> RunningPlatformOwnerState:
        """Observe the owner surface once, within the declared bound."""
        body = self.request_body(binding)
        record: dict[str, object] = {
            "endpoint": self._endpoint,
            "declared_bound_seconds": self._timeout_seconds,
            "request_keys": sorted(json.loads(body)),
            "request_body": body.decode("utf-8"),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "started_at": _utc_now(),
            "outcome": "pending",
        }
        self.observations.append(record)
        started = time.monotonic()
        deadline = started + self._timeout_seconds
        try:
            raw, status = self._exchange(body, deadline)
        except (OSError, http.client.HTTPException) as error:
            self._refuse(
                record,
                started,
                f"identity transport failed closed ({error.__class__.__name__})",
            )
        if len(raw) > self._max_response_bytes:
            self._refuse(record, started, "identity answer exceeds the response cap")
        if time.monotonic() > deadline:
            self._refuse(
                record,
                started,
                "identity answer arrived after the declared bound",
            )
        if status != http.client.OK:
            self._refuse(record, started, f"owner side answered HTTP {status}")
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            self._refuse(record, started, "identity answer is not UTF-8")
        try:
            state = read_owner_state_surface(text, binding)
        except ActualIdentityUnavailable as error:
            self._refuse(record, started, f"identity surface refused: {error}")
        self._finish(record, started, "accepted")
        return state

    def _exchange(self, body: bytes, deadline: float) -> tuple[bytes, int]:
        """One bounded HTTP exchange: connect, send, receive, close."""
        connection = http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                self._path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            self._apply_remaining_bound(connection, deadline)
            response = connection.getresponse()
            self._apply_remaining_bound(connection, deadline)
            raw = response.read(self._max_response_bytes + 1)
            return raw, response.status
        finally:
            connection.close()

    def _apply_remaining_bound(
        self, connection: http.client.HTTPConnection, deadline: float
    ) -> None:
        """Narrow the socket timeout to the remaining part of the bound."""
        if connection.sock is None:  # pragma: no cover - connect() sets it
            return
        connection.sock.settimeout(max(deadline - time.monotonic(), 0.001))

    def _refuse(
        self,
        record: dict[str, object],
        started: float,
        reason: str,
    ) -> None:
        self._finish(record, started, f"refused: {reason}")
        raise ActualIdentityUnavailable(reason)

    def _finish(
        self,
        record: dict[str, object],
        started: float,
        outcome: str,
    ) -> None:
        record["waited_seconds"] = round(time.monotonic() - started, 6)
        record["finished_at"] = _utc_now()
        record["outcome"] = outcome


__all__ = [
    "BINDING_TOKEN_KEY",
    "MAX_RESPONSE_BYTES",
    "HttpRunningPlatformOwnerStateReader",
]
