"""WebSocketTransport: the protocol carried over a WebSocket.

`Device` only needs the minimal duck-typed `Transport` Protocol defined in
device.py (`send(data: str) -> None`). `ConsoleTransport` is the
no-networking implementation of that contract; this module provides a real
one, backed by a WebSocket connection (via the `websocket-client` package),
so a `Device` can talk to an actual platform endpoint:

    transport = WebSocketTransport(url="wss://platform.example.com/devices")
    device = Device(id="birdbox01", transport=transport)
    ...
    transport.connect()
    device.start()          # sends the registration manifest over the socket
    temperature.set(22.5)   # sends the runtime update over the socket

`WebSocketTransport` explicitly extends `pihub.device.Transport` (a
`Protocol`, which Python allows subclassing directly, turning it into a
regular base class for this concrete implementation) rather than just
duck-typing it, matching `ConsoleTransport`.

Unlike `ConsoleTransport`, this transport talks to a real endpoint, so
`connect()` can fail (unreachable host, refused connection, timeout) and
the connection can drop mid-session. `reconnect()` retries `connect()`
with a backoff delay between attempts, and `send()` treats a dropped
socket the same way `ConsoleTransport.send()` treats an explicit
`disconnect()` - it raises rather than silently failing.

The actual socket is created via `connection_factory` (defaulting to
`websocket.create_connection`), which is a constructor argument purely so
tests can substitute a fake connection instead of opening a real socket -
this is not a new transport abstraction, just dependency injection around
the one WebSocket connection this transport manages.
"""

from __future__ import annotations

import socket
import time
from enum import Enum
from typing import Callable, Dict, Optional, Protocol

import websocket

from pihub.device import Transport
from pihub.security import InsecureTransportError, require_secure_url


class TransportDisconnectedError(ConnectionError):
    """Raised by `send()` when the transport is not currently connected."""


class TransportConnectionError(ConnectionError):
    """Raised by `connect()` / `reconnect()` when the WebSocket connection
    cannot be established.

    `status` is the HTTP status when the server answered the handshake and
    refused it (a bad or rate-limited login shows up as 403), and `None` when
    it could not be reached at all - the difference between "try again
    later" and "something is wrong on this device".
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class DeviceRefusedError(TransportConnectionError):
    """The platform answered a device's first connection and said no (HTTP 401 or 403).

    A `TransportConnectionError`, so code that caught that keeps working; `status` is the HTTP status.
    """

    def __init__(self, device_id: str, status: int) -> None:
        from pihub.reconnect import REFUSED_ADVICE  # not at the top: reconnect.py is imported by the package before this module

        super().__init__(f"ThePiHub refused device {device_id!r} (HTTP {status}). {REFUSED_ADVICE}", status=status)


# A connection whose far end has vanished (a pulled cable, a router that lost
# its uplink, a NAT table that forgot us) sends no FIN, so a blocking read
# would wait for hours. TCP keepalive makes the OS probe an idle connection
# and fail the read after roughly IDLE + INTERVAL * COUNT = 60 seconds.
KEEPALIVE_IDLE_SECONDS = 30
KEEPALIVE_INTERVAL_SECONDS = 10
KEEPALIVE_PROBES = 3


def _enable_keepalive(connection: object) -> None:
    """Best effort: not every platform has every option, and a test double has no socket."""
    sock = getattr(connection, "sock", None)
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in (
            ("TCP_KEEPIDLE", KEEPALIVE_IDLE_SECONDS),
            ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_SECONDS),
            ("TCP_KEEPCNT", KEEPALIVE_PROBES),
        ):
            if hasattr(socket, option):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
    except OSError:
        pass


class ConnectionState(Enum):
    """The lifecycle states a `WebSocketTransport` moves through.

    ```text
    DISCONNECTED --connect()--> CONNECTED
    CONNECTED --disconnect()--> DISCONNECTED
    (DISCONNECTED | CONNECTED) --reconnect()--> RECONNECTING --> CONNECTED | DISCONNECTED
    ```
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class WebSocketConnection(Protocol):
    """The minimal shape `WebSocketTransport` needs from a socket object.

    `websocket.create_connection()` returns a `WebSocket` instance that
    satisfies this already; tests substitute a fake one via
    `connection_factory`.
    """

    def send(self, data: str) -> None: ...

    def recv(self) -> str: ...

    def close(self) -> None: ...

    def settimeout(self, timeout: Optional[float]) -> None: ...


ConnectionFactory = Callable[..., WebSocketConnection]


class WebSocketTransport(Transport):
    """A `Transport` that sends outgoing messages over a real WebSocket connection."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout: float = 10.0,
        max_reconnect_attempts: int = 5,
        reconnect_delay: float = 1.0,
        reconnect_backoff: float = 2.0,
        connection_factory: ConnectionFactory = websocket.create_connection,
        sleep: Callable[[float], None] = time.sleep,
        allow_insecure: Optional[bool] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        # Security: a plaintext `ws://`
        # URL to anything but loopback is refused here, at construction,
        # rather than discovered by a packet capture later. `None` means
        # "inherit the deployment's PIHUB_ALLOW_INSECURE setting".
        require_secure_url(url, allow_insecure=allow_insecure)
        self._url = url
        self._connect_timeout = connect_timeout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_delay = reconnect_delay
        self._reconnect_backoff = reconnect_backoff
        self._connection_factory = connection_factory
        self._sleep = sleep
        # Security: a device's secret
        # belongs in an `Authorization` header, not in the connection URL
        # where every access log and proxy will record it. A browser
        # cannot do this (the WebSocket API has no way to set headers) -
        # a Python client can, so it should.
        self._headers = dict(headers or {})

        self._connection: Optional[WebSocketConnection] = None
        self._state = ConnectionState.DISCONNECTED

    @property
    def state(self) -> ConnectionState:
        """The transport's current lifecycle state."""
        return self._state

    @property
    def connected(self) -> bool:
        """Whether the transport currently has a live connection."""
        return self._state == ConnectionState.CONNECTED

    def connect(self) -> None:
        """Open the WebSocket connection.

        A no-op if already connected. Raises `TransportConnectionError` if
        the connection attempt fails; the transport is left `DISCONNECTED`
        in that case (never left dangling in `CONNECTING`).

        `connect_timeout` is passed to `connection_factory` to bound how
        long *establishing* the connection may take - but
        `websocket.create_connection()` (and anything satisfying its
        signature) applies that same value as the socket's timeout for
        every subsequent read too, not just the handshake. Left alone,
        that means `receive()` would raise a plain socket timeout - which
        looks exactly like a dead connection - after any idle period
        longer than `connect_timeout`, silently killing perfectly healthy
        long-lived connections. Once connected, the timeout is cleared
        (`settimeout(None)`) so `send()`/`receive()` block indefinitely
        again, matching how a persistent WebSocket connection is actually
        meant to behave; only the initial connection attempt is bounded.
        """
        if self._state == ConnectionState.CONNECTED:
            return

        self._state = ConnectionState.CONNECTING
        try:
            connection = self._connection_factory(
                self._url,
                timeout=self._connect_timeout,
                header=[f"{name}: {value}" for name, value in self._headers.items()],
            )
            connection.settimeout(None)
        except Exception as exc:
            self._state = ConnectionState.DISCONNECTED
            raise TransportConnectionError(
                f"failed to connect to {self._url}: {exc}", status=getattr(exc, "status_code", None)
            ) from exc

        _enable_keepalive(connection)
        self._connection = connection
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        """Close the WebSocket connection, if any.

        Idempotent, and tolerant of a connection that is already broken
        (mirrors `close()` best-effort semantics - the goal is to reach
        `DISCONNECTED`, not to report close errors).
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None
        self._state = ConnectionState.DISCONNECTED

    def reconnect(self) -> None:
        """Re-establish the connection, retrying with backoff.

        Disconnects first (if connected), then retries `connect()` up to
        `max_reconnect_attempts` times, sleeping `reconnect_delay *
        reconnect_backoff ** attempt` between attempts. Raises
        `TransportConnectionError` if every attempt fails; the transport is
        left `DISCONNECTED` in that case.
        """
        self.disconnect()
        self._state = ConnectionState.RECONNECTING

        delay = self._reconnect_delay
        last_error: Optional[Exception] = None
        for attempt in range(self._max_reconnect_attempts):
            if attempt > 0:
                self._sleep(delay)
                delay *= self._reconnect_backoff
            try:
                self.connect()
                return
            except TransportConnectionError as exc:
                last_error = exc
                self._state = ConnectionState.RECONNECTING

        self._state = ConnectionState.DISCONNECTED
        raise TransportConnectionError(
            f"failed to reconnect to {self._url} after {self._max_reconnect_attempts} attempts"
        ) from last_error

    def send(self, data: str) -> None:
        """Send `data` (a serialized protocol envelope) over the WebSocket.

        Raises `TransportDisconnectedError` if the transport is not
        currently connected, or if the underlying send fails (the
        connection is treated as dead and the transport moves to
        `DISCONNECTED`).
        """
        if not self.connected or self._connection is None:
            raise TransportDisconnectedError("cannot send: WebSocketTransport is disconnected")

        try:
            self._connection.send(data)
        except Exception as exc:
            self._connection = None
            self._state = ConnectionState.DISCONNECTED
            raise TransportDisconnectedError(f"send failed, connection lost: {exc}") from exc

    def receive(self) -> str:
        """Block until one message arrives on the connection, and return it.

        The wire is bidirectional even though most traffic so far has
        been outbound (`send()`): a real device process uses this to pull
        a server-pushed `command` or `lifecycle` frame off the same
        connection, then hands it to `Device.handle_incoming()` - exactly
        the pattern `send()` already establishes, just in the other
        direction. Raises `TransportDisconnectedError` if not connected,
        or if the underlying receive fails (the connection is treated as
        dead, same as `send()`).
        """
        if not self.connected or self._connection is None:
            raise TransportDisconnectedError("cannot receive: WebSocketTransport is disconnected")

        try:
            return self._connection.recv()
        except Exception as exc:
            self._connection = None
            self._state = ConnectionState.DISCONNECTED
            raise TransportDisconnectedError(f"receive failed, connection lost: {exc}") from exc
