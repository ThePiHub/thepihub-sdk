"""Synchronous, thread-safe, in-process event bus.

It:

- Register handlers
- Remove handlers
- Publish events
- Thread-safe
- Synchronous
- In-process
- No persistence
- Handler exceptions are logged; remaining handlers continue executing.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

Handler = Callable[[Any], None]


class EventBus:
    """A synchronous, thread-safe, in-process publish/subscribe event bus.

    Handlers are invoked synchronously and in subscription order, on the
    calling thread of `publish`. The bus holds no persistence: it only
    delivers events to handlers already subscribed at publish time.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: Dict[str, List[Handler]] = {}

    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Register `handler` to be called whenever `event_name` is published.

        The same handler may be subscribed more than once, in which case
        it is invoked once per subscription.
        """
        if not callable(handler):
            raise TypeError("handler must be callable")

        with self._lock:
            self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name: str, handler: Handler) -> None:
        """Remove one subscription of `handler` from `event_name`.

        This is a no-op if `event_name` is unknown or `handler` is not
        currently subscribed to it, so callers do not need to guard
        teardown code with existence checks.
        """
        with self._lock:
            handlers = self._handlers.get(event_name)
            if not handlers:
                return

            try:
                handlers.remove(handler)
            except ValueError:
                return

            if not handlers:
                del self._handlers[event_name]

    def publish(self, event_name: str, payload: Any = None) -> None:
        """Synchronously invoke every handler currently subscribed to `event_name`.

        A snapshot of the handler list is taken under lock and then
        invoked outside the lock, so handlers may safely subscribe or
        unsubscribe during dispatch without deadlocking or mutating the
        list being iterated.

        A handler that raises is logged and does not stop remaining
        handlers from running, per the EventBus error handling rule.
        """
        with self._lock:
            handlers = list(self._handlers.get(event_name, ()))

        for handler in handlers:
            try:
                handler(payload)
            except Exception:
                logger.exception(
                    "Error in handler %r for event %r", handler, event_name
                )
