"""Local Running Platform identity producer (owner side).

LOCAL DOCKER / NON-PRODUCTION. This service stands in for the Running Platform
owner's identity producer in the local stack (``local/compose.yaml``) so the
O4-C acceptance path can be exercised against a real owner-side process instead
of D&O reading an ambient ``<runtime_root>/running_platform_identity.json``.
Every transcript produced with it must be labelled ``LOCAL DOCKER /
NON-PRODUCTION``.

Owner-side properties it models:

* it runs as its own process/container, physically separate from Deployment &
  Operations, and imports nothing from ``deployment_operations``;
* it owns authoritative local identity state — its own document, in its own
  directory, written by the owner-side preparation tool — and re-reads it for
  every observation;
* it answers across an explicit transport boundary and accepts exactly one
  opaque evaluation binding; a request carrying anything else is refused and
  recorded, so forwarded expected state cannot cross unnoticed;
* every received request and every answer is recorded verbatim in an audit log
  (stdout and, optionally, a file), which is the transport transcript.

* its answer states — as the owner's own facts — the correlation of the
  observation it produced: the evaluation handle it answered (the echo), the
  binding space this platform serves, the binding position the handle states,
  the platform identity it actually observed, and the attribution of the
  observation. It never receives and never echoes an expected identity.

Scenario modes are owner-side behaviours that exercise D&O acceptance and
refusal: ``correct``, ``foreign``, ``stale``, ``wrong-correlation``,
``contradictory``, ``mismatch``, ``delay``, ``unavailable``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CLASSIFICATION = "LOCAL DOCKER / NON-PRODUCTION"
SERVICE_ROLE = "running-platform-local-identity-producer"
BINDING_TOKEN_KEY = "binding_token"
IDENTITY_KEYS = ("platform_id", "current_platform_id")
SCENARIOS = (
    "correct",
    "foreign",
    "stale",
    "wrong-correlation",
    "contradictory",
    "mismatch",
    "delay",
    "unavailable",
)
#: The prefix of one evaluation handle, as the evaluating side issues it
#: (``deployment_operations.deployment.EVALUATION_HANDLE_PREFIX``).
EVALUATION_HANDLE_PREFIX = "ev-"

#: The handle this producer answers with when it deliberately answers another
#: evaluation (the ``wrong-correlation`` scenario).
OTHER_EVALUATION_HANDLE = "ev-rp-answer-for-another-evaluation"

#: How many random hex characters one evaluation handle carries (128 bits).
EVALUATION_HANDLE_HEX_LENGTH = 32


def is_evaluation_handle(token: str) -> bool:
    """True when the token has the shape of an opaque evaluation handle.

    The check is on the *shape* only, and shape is not semantics: a token that
    looks like a handle says nothing about which evaluation it belongs to. The
    handle is the evaluation's nonce — it names no platform, no environment, no
    attempt and no digest, and this producer reads nothing out of it except that
    it is opaque. In particular the position of an answer in the binding space
    (``sequence``) is never derived from the handle: it comes from this
    producer's own authoritative state, as every other fact in the answer does.
    """

    body = token.removeprefix(EVALUATION_HANDLE_PREFIX)
    if len(body) != EVALUATION_HANDLE_HEX_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in body)


MAX_REQUEST_BYTES = 65_536
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_TOO_LARGE = 413
HTTP_UNAVAILABLE = 503


def _answered_token(answer: dict[str, Any]) -> object:
    """The evaluation handle the answer states it was produced for."""
    correlation = answer.get("correlation")
    return correlation.get("token") if isinstance(correlation, dict) else None


def _utc_now() -> str:
    """Timestamp for the audit transcript, with microseconds."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ProducerError(Exception):
    """The producer cannot honour the configured scenario or state."""


class AuditLog:
    """Append-only JSONL transcript of everything that crosses the boundary."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def record(self, event: str, **fields: object) -> dict[str, object]:
        entry: dict[str, object] = {"at": _utc_now(), "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return entry


class IdentityProducer:
    """Authoritative local identity state plus the scenario behaviour."""

    def __init__(
        self,
        *,
        state_path: Path,
        foreign_state_path: Path | None,
        mismatch_state_path: Path | None,
        scenario: str,
        delay_seconds: float,
        audit: AuditLog,
    ) -> None:
        if scenario not in SCENARIOS:
            message = f"unknown scenario: {scenario}"
            raise ProducerError(message)
        self._state_path = state_path
        self._foreign_state_path = foreign_state_path
        self._mismatch_state_path = mismatch_state_path
        self._scenario = scenario
        self._delay_seconds = delay_seconds
        self._audit = audit

    @property
    def state_path(self) -> Path:
        """The authoritative local identity state this producer owns."""
        return self._state_path

    @property
    def scenario(self) -> str:
        """The owner-side behaviour currently configured."""
        return self._scenario

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            message = (
                f"identity state is unreadable at {path}: {error.__class__.__name__}"
            )
            raise ProducerError(message) from error
        if not isinstance(document, dict):
            message = f"identity state is not an object at {path}"
            raise ProducerError(message)
        return document

    def answer(self, binding_token: str) -> dict[str, Any]:
        """Build the owner's answer for one opaque evaluation binding.

        The binding token is the only input. Nothing else from the requesting
        side is used, and the answer is produced from owner-side state only.

        The answer states, as the owner's own facts, the correlation of the
        observation: the handle it answered (the echo), the binding space this
        platform serves, this answer's position in that space, the platform
        identity it actually observed, and the attribution of the observation.
        The handle is opaque and nothing is read out of it: the received token is
        only recognised as a handle and echoed back.
        The evaluator checks that statement against the binding it established
        (:func:`deployment_operations.platform_identity.require_correlated_evidence`);
        this producer never receives an expected identity and never echoes one.
        """
        if self._scenario == "delay":
            time.sleep(self._delay_seconds)
        if self._scenario == "unavailable":
            message = "owner-side identity is deliberately unavailable"
            raise ProducerError(message)

        if not is_evaluation_handle(binding_token):
            message = "the request carries no opaque evaluation handle"
            raise ProducerError(message)

        if self._scenario == "foreign":
            if self._foreign_state_path is None:
                message = "the foreign scenario needs a foreign identity state"
                raise ProducerError(message)
            document = copy.deepcopy(self._load(self._foreign_state_path))
        elif self._scenario == "mismatch":
            if self._mismatch_state_path is None:
                message = "the mismatch scenario needs a mismatched identity state"
                raise ProducerError(message)
            document = copy.deepcopy(self._load(self._mismatch_state_path))
        else:
            document = copy.deepcopy(self._load(self._state_path))

        platform_id = document.get("platform_id")
        if not isinstance(platform_id, str) or not platform_id:
            message = "identity state carries no platform_id"
            raise ProducerError(message)

        scope = document.get("binding_scope")
        authority = document.get("observation_authority")
        basis = document.get("observation_basis")
        sequence = document.get("observation_sequence")
        for name, value in (
            ("binding_scope", scope),
            ("observation_authority", authority),
            ("observation_basis", basis),
        ):
            if not isinstance(value, str) or not value:
                message = f"identity state states no {name}"
                raise ProducerError(message)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            message = "identity state states no observation_sequence"
            raise ProducerError(message)

        # The received handle is echoed, never parsed: every other field of the
        # statement is this producer's own fact.
        document["correlation"] = {
            "token": binding_token,
            "scope": scope,
            "sequence": sequence,
            "target": platform_id,
            "authority": authority,
            "basis": basis,
            "established_at": _utc_now(),
        }
        if self._scenario == "wrong-correlation":
            document["correlation"]["token"] = OTHER_EVALUATION_HANDLE

        if self._scenario == "stale":
            document["freshness_current"] = False
        if self._scenario == "contradictory":
            self._contradict_identity(document, platform_id)
        return document

    @staticmethod
    def _contradict_identity(document: dict[str, Any], platform_id: str) -> None:
        """Serve a configuration that contradicts the served platform identity."""
        configuration = document.get("configuration")
        value = configuration.get("value") if isinstance(configuration, dict) else None
        if not isinstance(value, dict):
            message = "the contradictory scenario needs object configuration state"
            raise ProducerError(message)
        contradicted = 0
        for section in value.values():
            if not isinstance(section, dict):
                continue
            for key in IDENTITY_KEYS:
                if key in section:
                    section[key] = f"not-{platform_id}"
                    contradicted += 1
        if not contradicted:
            message = "the contradictory scenario needs an identity-bearing key"
            raise ProducerError(message)


class _Handler(BaseHTTPRequestHandler):
    """The transport boundary: POST /observe and GET /healthz (owner side)."""

    protocol_version = "HTTP/1.1"
    server_version = SERVICE_ROLE
    sys_version = ""

    @property
    def producer(self) -> IdentityProducer:
        return self.server.producer  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if self.path in ("/", "/healthz"):
            self._send_json(
                HTTP_OK,
                {
                    "status": "ok",
                    "role": SERVICE_ROLE,
                    "classification": CLASSIFICATION,
                    "scenario": self.producer.scenario,
                    "state": str(self.producer.state_path),
                },
            )
            return
        self._send_json(HTTP_NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/observe":
            self._send_json(HTTP_NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._refuse_boundary("observation has no readable length")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._refuse_boundary("observation has an unusable body length")
            return
        raw = self.rfile.read(length)
        received: dict[str, object] = {
            "event": "observation_received",
            "scenario": self.producer.scenario,
            "client": self.address_string(),
            "body_length": len(raw),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body": raw.decode("utf-8", errors="replace"),
        }
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            self._audit.record(**received, accepted=False, reason="not JSON")
            self._send_json(HTTP_BAD_REQUEST, {"error": "observation is not JSON"})
            return
        if not isinstance(parsed, dict):
            self._audit.record(**received, accepted=False, reason="not an object")
            self._send_json(HTTP_BAD_REQUEST, {"error": "observation is not an object"})
            return
        keys = sorted(parsed)
        received["keys"] = keys
        extra = [key for key in keys if key != BINDING_TOKEN_KEY]
        if extra or keys != [BINDING_TOKEN_KEY]:
            # The boundary refuses anything but the opaque binding: D&O cannot
            # forward expected deployment state, and this side would not carry it.
            self._audit.record(
                **received,
                accepted=False,
                reason="observation carries more than the opaque binding",
                extra_keys=extra,
            )
            self._send_json(
                HTTP_BAD_REQUEST,
                {
                    "error": "only the opaque binding may cross this boundary",
                    "extra_keys": extra,
                },
            )
            return
        token = parsed[BINDING_TOKEN_KEY]
        if not isinstance(token, str) or not token:
            self._audit.record(
                **received, accepted=False, reason="binding is not a non-empty string"
            )
            self._send_json(
                HTTP_BAD_REQUEST, {"error": "the binding must be a non-empty string"}
            )
            return
        self._audit.record(**received, accepted=True)
        started = time.monotonic()
        try:
            answer = self.producer.answer(token)
        except ProducerError as error:
            self._audit.record(
                event="observation_refused",
                scenario=self.producer.scenario,
                reason=str(error),
                elapsed_seconds=round(time.monotonic() - started, 6),
            )
            self._send_json(HTTP_UNAVAILABLE, {"error": str(error)})
            return
        body = json.dumps(answer, ensure_ascii=False, sort_keys=True).encode("utf-8")
        answered: dict[str, object] = {
            "event": "observation_answered",
            "scenario": self.producer.scenario,
            "binding_token": token,
            "answered_correlation_handle": _answered_token(answer),
            "answered_platform_id": answer.get("platform_id"),
            "answered_freshness_current": answer.get("freshness_current"),
            "answered_sha256": hashlib.sha256(body).hexdigest(),
            "answered_length": len(body),
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
        try:
            self._send_bytes(HTTP_OK, body)
        except (BrokenPipeError, ConnectionResetError):
            # The requesting side already stopped waiting: the answer is late
            # by its own declared bound, and this is recorded as such.
            self._audit.record(
                **answered, delivered=False, reason="client stopped waiting"
            )
            return
        self._audit.record(**answered, delivered=True)

    def _refuse_boundary(self, reason: str) -> None:
        self._audit.record(
            event="observation_received",
            scenario=self.producer.scenario,
            client=self.address_string(),
            accepted=False,
            reason=reason,
        )
        self._send_json(HTTP_TOO_LARGE, {"error": reason})

    @property
    def _audit(self) -> AuditLog:
        return self.server.audit  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send_bytes(status, body)

    def _send_bytes(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    """Threaded transport endpoint carrying the producer and its audit log."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        producer: IdentityProducer,
        audit: AuditLog,
    ) -> None:
        super().__init__(address, _Handler)
        self.producer = producer
        self.audit = audit


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--foreign-state", type=Path, default=None)
    parser.add_argument("--mismatch-state", type=Path, default=None)
    parser.add_argument("--scenario", default="correct", choices=SCENARIOS)
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--audit", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local identity producer (owner side) until terminated."""
    args = _parse_args(argv)
    audit = AuditLog(args.audit)
    try:
        producer = IdentityProducer(
            state_path=args.state,
            foreign_state_path=args.foreign_state,
            mismatch_state_path=args.mismatch_state,
            scenario=args.scenario,
            delay_seconds=args.delay_seconds,
            audit=audit,
        )
    except ProducerError as error:
        sys.stderr.write(f"producer is unusable: {error}\n")
        return 2
    try:
        server = _Server((args.host, args.port), producer, audit)
    except OSError as error:
        sys.stderr.write(f"transport endpoint is unusable: {error}\n")
        return 2
    bound = server.server_address
    audit.record(
        event="service_started",
        role=SERVICE_ROLE,
        classification=CLASSIFICATION,
        scenario=args.scenario,
        state=str(args.state),
        host=str(bound[0]),
    )
    sys.stdout.write(f"PORT={bound[1]}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
