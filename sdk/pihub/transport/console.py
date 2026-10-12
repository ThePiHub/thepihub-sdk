"""ConsoleTransport: the protocol carried over standard output.

`Device` already defines the transport contract it needs as a minimal
duck-typed `Transport` Protocol (see device.py): anything with
`send(data: str) -> None`. This module provides the first concrete
implementation of it - one with no real networking at all, so the SDK can
be exercised end-to-end (Device, Program, and the protocol's
RegistrationMessage / RuntimeMessage / NotificationMessage - all
unchanged) by just printing every outgoing message to stdout:

    device = Device(id="birdbox01", transport=ConsoleTransport())
    ...
    device.start()          # prints the registration manifest
    temperature.set(22.5)   # prints the runtime update

`ConsoleTransport` explicitly extends `pihub.device.Transport` (a
`Protocol`, which Python allows subclassing directly, turning it into a
regular base class for this concrete implementation) rather than just
duck-typing it, per "Extend the existing Transport base class".

Since there is no real connection to establish, `ConsoleTransport` starts
connected - `connect()` / `disconnect()` exist to simulate a transport
going online/offline (and to exercise `send()`'s disconnected-raises
behaviour), not because printing to stdout needs a handshake first.
"""

from __future__ import annotations

import json

from pihub.device import Transport


class TransportDisconnectedError(ConnectionError):
    """Raised by `send()` when the transport is not currently connected."""


class ConsoleTransport(Transport):
    """A no-networking `Transport` that prints every outgoing message to stdout.

    For local development and debugging - never for production traffic.
    """

    def __init__(self) -> None:
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Mark the transport as connected."""
        self._connected = True
        print("[ConsoleTransport] connected")

    def disconnect(self) -> None:
        """Mark the transport as disconnected."""
        self._connected = False
        print("[ConsoleTransport] disconnected")

    def send(self, data: str) -> None:
        """Print `data` to stdout in a readable form.

        `data` is whatever `Device.send()` hands a transport: the
        `to_json()` output of a `RegistrationMessage`, `RuntimeMessage`,
        or `NotificationMessage` envelope. It's pretty-printed here for
        readability, falling back to the raw string if it isn't valid
        JSON (this transport's job is to print, not to validate).
        """
        if not self._connected:
            raise TransportDisconnectedError("cannot send: ConsoleTransport is disconnected")

        try:
            envelope = json.loads(data)
            label = envelope.get("messageType", "message") if isinstance(envelope, dict) else "message"
            body = json.dumps(envelope, indent=2)
        except (TypeError, ValueError):
            label = "message"
            body = data

        print(f"[ConsoleTransport] {label}\n{body}\n")
