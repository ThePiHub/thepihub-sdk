"""DeviceRuntimeService, per the Device Runtime Service Implementation
Prompt.

Per that prompt's "Current Gap": before this module, an application had
to hand-write its own receive loop for `device.start()` to be "sufficient
to run a device":

    while True:
        message = transport.receive()
        device.handle_incoming(message)

This module *is* that loop, run on a background thread so `device.start()`
returns immediately (per "Background Receive Loop": "Does not block the
main thread") while continuously feeding inbound `command` and
`lifecycle` messages into the device's existing `handle_incoming()` ->
`EventBus` -> `Program` handler pipeline. No new dispatch logic, and no
changes to `Device`, `Program` or the `Transport` protocol - this only
drives calls that already exist.

`Device.start()` / `Device.stop()` own an instance of this internally
(see device.py); application code never constructs one directly.

Only usable with a transport that can *receive* - `ReceivingTransport`
below is deliberately a separate, minimal Protocol from `device.py`'s own
`Transport` (which only requires `send`), since not every transport can
receive (`ConsoleTransport` has no inbound message source at all - there
is nothing to receive). `Device` only starts a runtime service when the
attached transport structurally satisfies this, using the same
optional-hook pattern `add_program()` already uses for `_bind_device`.

Threading model, per the prompt's "V1 Requirements": thread-based
(`threading.Thread`), no `asyncio`. The existing `EventBus` is already
synchronous and thread-safe, so handlers
invoked from this background thread need no changes to work correctly.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# How long the loop waits, after an unexpected (non-disconnect) receive or
# dispatch error, before retrying - just enough to avoid a hot loop if the
# transport keeps failing the same way; not a backoff strategy.
_RETRY_DELAY_SECONDS = 0.1


class ReceivingTransport(Protocol):
    """The minimal shape this service needs beyond `device.py`'s own
    `Transport` protocol: something that can also `receive()`."""

    def receive(self) -> str: ...


class RuntimeDevice(Protocol):
    """The minimal shape this service needs from a device-like object."""

    id: str

    def handle_incoming(self, data: str) -> None: ...


class DeviceRuntimeService:
    """Runs a device's background receive loop on its own thread.

    One instance per running device. `start()` spawns the loop; `stop()`
    signals it to exit and waits (up to `timeout`) for the thread to
    finish - both are idempotent, so repeated calls (or calling `stop()`
    on a service that was never started) are safe.
    """

    def __init__(
        self,
        device: RuntimeDevice,
        transport: ReceivingTransport,
        on_disconnect: Optional[Callable[[threading.Event], bool]] = None,
    ) -> None:
        self._device = device
        self._transport = transport
        # Called on this thread when the connection is lost. It returns True
        # once the connection is back (the loop carries on) or False to give
        # up (the loop exits, as it always did). Given the stop event so its
        # waiting can end the moment `stop()` is called. `None` keeps the
        # original behaviour: a lost connection ends the loop.
        self._on_disconnect = on_disconnect
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        """Whether the background receive loop is currently active."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background receive loop. A no-op if already running."""
        if self.running:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"pihub-device-runtime-{self._device.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Signal the loop to stop and wait up to `timeout` for it to exit.

        Safe to call multiple times, and safe to call even if `start()`
        was never called.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        """The background thread body: receive, dispatch, repeat.

        Per "Error Handling": a `ConnectionError` (the base class both
        `TransportDisconnectedError` variants already subclass) means the
        transport is gone - the loop logs it and exits cleanly, matching
        "The runtime loop should exit cleanly on disconnect". Any other
        unexpected exception, from either `receive()` or
        `handle_incoming()`, is logged and the loop keeps running -
        "Transport receive failures must not crash the process".
        """
        while not self._stop_event.is_set():
            try:
                message = self._transport.receive()
            except ConnectionError:
                if self._restored():
                    continue
                logger.info(
                    "device %r: transport disconnected, stopping receive loop",
                    self._device.id,
                )
                return
            except Exception:
                logger.exception(
                    "device %r: unexpected error receiving a message", self._device.id
                )
                self._stop_event.wait(_RETRY_DELAY_SECONDS)
                continue

            if not message:
                # A closed socket surfaces as an empty frame rather than
                # an exception (`websocket-client`'s `recv()` returns
                # `''` once the peer has gone). Treated as data it
                # becomes a JSON error, logged, and retried immediately
                # against the same dead socket - a hot loop spewing
                # tracebacks. It means the same thing a ConnectionError
                # does: the transport is gone.
                if self._restored():
                    continue
                logger.info(
                    "device %r: transport closed by the platform, stopping receive loop",
                    self._device.id,
                )
                return

            try:
                self._device.handle_incoming(message)
            except Exception:
                logger.exception(
                    "device %r: unexpected error handling an incoming message",
                    self._device.id,
                )

    def _restored(self) -> bool:
        """Whether a lost connection was re-established (only if a handler was given)."""
        if self._on_disconnect is None or self._stop_event.is_set():
            return False
        return self._on_disconnect(self._stop_event)
