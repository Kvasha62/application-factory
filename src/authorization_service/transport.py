"""Contract transport for a Level 0 modular monolith (IS-003).

ARCHITECTURE.md §1.1 allows exactly four ways for components to interact: API,
events, Data Export/CDC and official integration mechanisms. At Level 0 the
component boundary is made real by serving the *published API* in-process: a
request is executed against the component's ASGI application and the response is
returned as plain data.

Values cross the boundary — never live objects. The transport returns
``(status, payload)`` only, and the consumer holds nothing but an opaque channel
handle: the application itself stays on the provider side, in this module's
private table. So a consumer cannot reach the engine, the grant store, the
audit journal, any grant mutation or the component's
ASGI application through it. Requests run through the same routing, validation,
service-identity authentication, permission check and audit code as network
requests would.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import weakref
from typing import Any, Callable, Mapping, Sequence

from authorization_service.errors import ContractViolation

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

                threading.Thread(target=run, name="authorization-contract-transport", daemon=True).start()
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
            "client": ("authorization_service-consumer", 0),
            "server": ("authorization_service", 80),
            # A JSON body must be announced in the request headers, not only in the
            # scope: the application parses the body exactly as it would over the
            # network, so an in-process request cannot take a shortcut a remote one
            # does not have.
            "headers": [
                *(
                    [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
                    if raw
                    else []
                ),
                *((name.lower().encode(), value.encode()) for name, value in headers),
            ],
            "content_type": "application/json" if raw else "",
            "content_length": len(raw),
        }
        messages: list[dict[str, Any]] = []
        consumed = False

        async def receive() -> dict[str, Any]:
            nonlocal consumed
            if not consumed:
                consumed = True
                # The whole body is delivered in one message: announcing more of it
                # and then disconnecting would make every request with a body look
                # like a truncated one to the application.
                return {"type": "http.request", "body": raw, "more_body": False}
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
                f"authorization contract produced no response for {method} {path}"
            )
        payload_bytes = b"".join(chunks)
        if not payload_bytes:
            return status, {}
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except ValueError as exc:  # non-JSON answer is a contract violation
            raise ContractViolation(
                f"authorization contract returned a non-JSON payload for {method} {path}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ContractViolation(
                f"authorization contract returned a non-object payload for {method} {path}"
            )
        return status, payload


#: Provider-side channel table: handle -> live transport. A consumer is handed the
#: handle (a string) and never the transport, so no attribute path from the
#: published client leads to the application, the deployment, the engine or the
#: store. ``authorization_service.transport`` is an internal module of this component
#: (see ``api.consumer_surface.internal_modules`` in the component contract), and
#: a consumer component that imported it would break its own import guard.
_CHANNELS: dict[str, ASGIContractTransport] = {}


def open_channel(app: Any) -> str:
    """Register one contract application and return its opaque handle.

    The handle is a value: unguessable, not chosen or enumerable by a consumer,
    and useless on its own — every request made through it is authenticated,
    permission-checked, ownership-checked and audited by the application behind it
    exactly as a network request would be.
    """
    if not callable(app):
        raise TypeError("a contract channel needs the component's ASGI application")
    handle = f"azc-{secrets.token_urlsafe(16)}"
    _CHANNELS[handle] = ASGIContractTransport(app)
    return handle


def call_contract(
    handle: str,
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]] = (),
    body: "Mapping[str, Any] | None" = None,
) -> tuple[int, Mapping[str, Any]]:
    """Execute one contract request over a handle; data in, data out.

    An unknown or revoked handle is a closed channel, not a permissive default.
    """
    channel = _CHANNELS.get(handle) if isinstance(handle, str) else None
    if channel is None:
        raise ContractViolation("the authorization contract channel is closed")
    return channel(method, path, headers, body)


def close_channel(handle: str) -> None:
    """Revoke one channel; later requests through it fail closed."""
    _CHANNELS.pop(handle, None)


def _revoke(handle: str) -> None:
    """Revoke on behalf of a finalizer: a no-op once teardown has cleared globals."""
    try:
        _CHANNELS.pop(handle, None)
    except AttributeError:  # pragma: no cover - interpreter is shutting down
        pass


def revoke_on_death(owner: Any, handle: str) -> None:
    """Close ``handle`` when its owner object dies; keeps no reference itself.

    The channel belongs to the consumer object it was published to: while that
    object lives the contract is executable, and once it is gone the table holds
    nothing — so the provider never keeps an application alive on its own account.
    ``atexit`` is off: a half-torn-down interpreter has nothing to revoke.
    """
    finalizer = weakref.finalize(owner, _revoke, handle)
    finalizer.atexit = False


def channel_count() -> int:
    """Number of open channels — introspection for lifecycle tests, not a lookup.

    Handles are not returned: a count cannot be turned into a path to an
    application, and a consumer has nothing legitimate to enumerate here.
    """
    return len(_CHANNELS)


def asgi_transport(app: Any) -> ContractTransport:
    """Direct transport of this component: internal helper for tests and tooling.

    It returns a callable bound to ``app``, which is exactly why it is not the
    published path — a consumer receives a channel handle from :func:`open_channel`
    instead, so nothing it holds references the application.
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

