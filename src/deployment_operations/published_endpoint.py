"""Serving a component's published contract on its declared endpoint.

ADR-0021 §2.1/§3 fixes the runtime dependency transport as a **network**
transport, with the endpoint **statically declared** by the authoritative
environment binding and the provider's listener served by the provider's own
runtime process. Owner decision P1 adds that nothing here allocates an endpoint
dynamically: the address is read from the binding and bound, or the component
fails to start.

The provider side is deliberately small and deliberately in-process to the
provider: one thread serves the ASGI application the component itself publishes
(``contract_app()``), and one thread drives that application's event loop. The
component never learns about sockets — it publishes an ASGI contract exactly as
before, and this module makes that contract reachable over the network at the
declared address.

**No technology was selected by an ADR for this.** ADR-0017 §40 leaves the
deployment technology unselected and ADR-0021 prescribes only the transport
*kind*. The listener is built from the Python standard library
(:mod:`http.server` + :mod:`asyncio`) so that this slice introduces **no new
third-party dependency** and no new architectural decision; it is an
implementation detail of this adapter seam, replaceable without the Platform
Instance, the Manifest or any contract changing (ADR-0016 §11, §23).

Failing to bind the declared endpoint is a hard failure. There is no fallback to
an in-process contract and no substitution of another address, because both
would break the guarantee a consumer relies on: the endpoint the binding
declares is the endpoint the provider serves (ADR-0021 §3).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import unquote

from deployment_operations.environment import ENDPOINT_RE

#: How long one request may take before this process stops waiting for the
#: component's application. A slow component is a component failure, not a
#: transport one, and the consumer must get an answer rather than hang.
REQUEST_TIMEOUT_SECONDS = 30.0


class PublishedEndpointError(Exception):
    """The declared endpoint could not be served by this runtime process."""


class _Handler(BaseHTTPRequestHandler):
    """Bridge one HTTP request into the component's ASGI contract."""

    #: Set by :class:`PublishedEndpointServer` on the server instance.
    asgi_app: Any = None
    loop: asyncio.AbstractEventLoop | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS

    protocol_version = "HTTP/1.1"
    server_version = "ApplicationFactory/1.0"

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_DELETE(self) -> None:
        self._serve("DELETE")

    def do_PATCH(self) -> None:
        self._serve("PATCH")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress the default stderr access log.

        A component's runtime log is deployment evidence, and an access log
        would put request paths and headers into it. What this process reports
        is what the component's published surface answered.
        """

    # ------------------------------------------------------------------ internals
    def _serve(self, method: str) -> None:
        if self.asgi_app is None or self.loop is None:
            raise PublishedEndpointError("no ASGI application is bound to this server")
        target = self.path or "/"
        raw_path, _, query = target.partition("?")
        body = self._read_body()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "root_path": "",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in self.headers.items()
            ],
            "client": self.client_address[:2] if self.client_address else None,
            "server": self.server.server_address[:2],
        }
        try:
            status, headers, payload = asyncio.run_coroutine_threadsafe(
                _invoke(self.asgi_app, scope, body), self.loop
            ).result(timeout=self.timeout_seconds)
        except TimeoutError as error:
            raise PublishedEndpointError(
                f"{method} {target}: the component's application did not answer "
                f"within {self.timeout_seconds}s"
            ) from error
        except Exception as error:
            raise PublishedEndpointError(
                f"{method} {target}: {error.__class__.__name__}: {error}"
            ) from error
        self.send_response(status)
        for name, value in headers:
            self.send_header(name.decode("latin-1"), value.decode("latin-1"))
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if method != "HEAD" and payload:
            self.wfile.write(payload)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            raise PublishedEndpointError("content-length is not a number") from None
        if length <= 0:
            return b""
        return self.rfile.read(length)


async def _invoke(
    app: Any,
    scope: Mapping[str, Any],
    body: bytes,
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Call an ASGI application once and collect its response."""
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 500
    headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal status, headers
        kind = message.get("type")
        if kind == "http.response.start":
            status = int(message.get("status", 500))
            headers = [
                (bytes(name), bytes(value))
                for name, value in message.get("headers", [])
            ]
        elif kind == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    await app(dict(scope), receive, send)
    return status, headers, b"".join(chunks)


class PublishedEndpointServer:
    """The provider-side listener of one declared endpoint.

    Started with the component's runtime process and stopped with it: the
    listener is part of the provider's own runtime, not a sidecar and not a
    shared process (ADR-0021 §3).
    """

    __slots__ = ("_app", "_endpoint", "_host", "_loop", "_port", "_server", "_threads")

    def __init__(self, app: Any, endpoint: str) -> None:
        match = ENDPOINT_RE.fullmatch(endpoint or "")
        if match is None:
            raise PublishedEndpointError(
                f"{endpoint!r} is not a declared '<host>:<port>' endpoint; the "
                "address comes from the environment binding and is never "
                "derived, guessed or substituted"
            )
        host = match.group("host")
        raw_port = match.group("port")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise PublishedEndpointError(
                f"{endpoint!r} declares port {port}, which is outside 1..65535"
            )
        self._app = app
        self._endpoint = endpoint
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._threads: list[threading.Thread] = []

    @property
    def endpoint(self) -> str:
        """The endpoint this listener serves, exactly as it was declared."""
        return self._endpoint

    def serve(self) -> None:
        """Bind the declared endpoint and start serving the contract."""
        if self._server is not None:
            return
        handler = type(
            "_BoundHandler",
            (_Handler,),
            {"asgi_app": self._app, "loop": None},
        )
        try:
            server = ThreadingHTTPServer((self._host, self._port), handler)
        except OSError as error:
            raise PublishedEndpointError(
                f"the declared endpoint {self._endpoint} could not be bound "
                f"({error.__class__.__name__}: {error}); the component cannot "
                "serve the contract its consumers are bound to reach, and no "
                "other address may be substituted"
            ) from error
        loop = asyncio.new_event_loop()
        handler.loop = loop
        server.daemon_threads = True
        loop_thread = threading.Thread(
            target=loop.run_forever, name="endpoint-loop", daemon=True
        )
        serve_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"endpoint-{self._endpoint}",
            daemon=True,
        )
        loop_thread.start()
        serve_thread.start()
        self._loop = loop
        self._server = server
        self._threads = [loop_thread, serve_thread]

    def stop(self) -> None:
        """Stop serving and release the declared endpoint."""
        server, self._server = self._server, None
        loop, self._loop = self._loop, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []

    def __enter__(self) -> Self:
        self.serve()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


__all__ = [
    "REQUEST_TIMEOUT_SECONDS",
    "PublishedEndpointError",
    "PublishedEndpointServer",
]
