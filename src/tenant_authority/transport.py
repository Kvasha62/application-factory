"""Contract transport for a Level 0 modular monolith (IS-002).

ARCHITECTURE.md §1.1 allows exactly four ways for components to interact: API,
events, Data Export/CDC and official integration mechanisms. At Level 0 the
component boundary is made real by serving the *published API* in-process: a
request is executed against the component's ASGI application and the response is
returned as plain data.

Values cross the boundary — never live objects. The transport returns
``(status, payload)`` only, so a consumer cannot reach the engine, the registry
store, the audit journal, the idempotency table or any mutation operation
through it. Requests run through the same routing, validation, service-identity
authentication, authorization and audit code as network requests would.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Mapping, Sequence

from tenant_authority.errors import ContractViolation

#: Contract transport signature: (method, path, headers, json_body) -> (status, payload).
ContractTransport = Callable[
    [str, str, Sequence[tuple[str, str]], "Mapping[str, Any] | None"],
    "tuple[int, Mapping[str, Any]]",
]



class ASGIContractTransport:
    """Execute one request against an ASGI application, in-process.

    A dedicated event loop is used only when the caller already runs inside a
    loop; the ordinary synchronous path runs the exchange on the current thread.
    """

    __slots__ = ("_app", "_portal_loop", "_lock")

    def __init__(self, app: Any) -> None:
        self._app = app
        self._portal_loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def __call__(
        self,
        method: str,
        path: str,
        headers: Sequence[tuple[str, str]] = (),
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._perform(method, path, tuple(headers), body))
        return self._submit(self._perform(method, path, tuple(headers), body))

    # ------------------------------------------------------------------ internals
    def _submit(self, coroutine: Any) -> tuple[int, Mapping[str, Any]]:
        loop = self._ensure_portal_loop()
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def _ensure_portal_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._portal_loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                threading.Thread(target=run, name="tenant-authority-transport", daemon=True).start()
                ready.wait()
                self._portal_loop = loop
            return self._portal_loop

    async def _perform(
        self,
        method: str,
        path: str,
        headers: Sequence[tuple[str, str]],
        body: Mapping[str, Any] | None,
    ) -> tuple[int, Mapping[str, Any]]:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "root_path": "",
            "client": ("tenant-authority-consumer", 0),
            "server": ("tenant-authority", 80),
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
            "content_type": "application/json" if raw else "",
            "content_length": len(raw),
        }
        messages: list[dict[str, Any]] = []
        consumed = False

        async def receive() -> dict[str, Any]:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": raw, "more_body": bool(raw)}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await self._app(scope, receive, send)

        status: int | None = None
        chunks: list[bytes] = []
        for message in messages:
            kind = message.get("type")
            if kind == "http.response.start" and status is None:
                status = int(message["status"])
            elif kind == "http.response.body":
                chunks.append(message.get("body", b"") or b"")
        if status is None:
            raise ContractViolation(
                f"tenant authority contract produced no response for {method} {path}"
            )
        payload_bytes = b"".join(chunks)
        if not payload_bytes:
            return status, {}
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except ValueError as exc:  # non-JSON answer is a contract violation
            raise ContractViolation(
                f"tenant authority contract returned a non-JSON payload for {method} {path}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ContractViolation(
                f"tenant authority contract returned a non-object payload for {method} {path}"
            )
        return status, payload


def asgi_transport(app: Any) -> ContractTransport:
    """Published factory of the Level 0 contract transport for this component.

    The result is a plain callable: it carries no attributes of its own, so a
    consumer holding it gains no attribute path to the application behind it —
    only ``(method, path, headers, body) -> (status, payload)``.
    """
    transport = ASGIContractTransport(app)

    def call(
        method: str,
        path: str,
        headers: Sequence[tuple[str, str]],
        body: "Mapping[str, Any] | None",
    ) -> tuple[int, Mapping[str, Any]]:
        return transport(method, path, headers, body)

    return call

