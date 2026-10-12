"""How a device behaves when its connection to the platform is lost.

A real device is a long-lived process on a machine whose network comes and
goes, talking to a server that is redeployed from time to time. Before this,
a dropped connection ended the receive loop for good: the program carried on
printing "online" while the platform saw an offline device, until a person
restarted it.

The policy is deliberately small:

```text
retry forever          a device that gives up is a bug, not a preference
1s, 2s, 4s ... 60s     exponential, capped, with +/-20% jitter so a fleet
                       does not stampede a server that has just come back
stable = 30s uptime    a connection that lasted 30s resets the delay; one
                       that flaps faster keeps backing off, because the
                       platform treats a reconnect storm as a violation
                       (flood_control.py: 10 connections/minute)
```

Never giving up applies to a rejected login too. The platform refuses a
device with bad credentials and a rate-limited one in the same way (an HTTP
403 before the socket is accepted), so the client cannot tell them apart -
and giving up on the second would leave a device offline for good after its
quarantine ends. It backs off and says what it suspects instead.
"""

from __future__ import annotations

import random
from typing import Callable

BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 60.0
JITTER = 0.2
STABLE_AFTER_SECONDS = 30.0

# HTTP statuses that mean "the platform answered and said no", as opposed to
# "could not reach it". The server closes a connection with a bad secret, or a
# rate-limited one, before accepting it, which a client sees as one of these.
REFUSED_STATUSES = frozenset({401, 403})

# What a person should do about a refusal. The platform gives the same answer for a deactivated device, a deleted device or account, wrong
# credentials and a rate-limited attempt, so each cause is named, the likeliest first: a device deactivated on the dashboard needs only a Reactivate.
REFUSED_ADVICE = (
    "Most likely the device was deactivated: open the ThePiHub dashboard and press Reactivate on it - the device needs nothing else. "
    "If it was deleted, or its account was, or its saved credentials are wrong or gone, it must be registered again: run `pihub setup`. "
    "(Too many recent attempts also causes this; wait a minute.)"
)


class Backoff:
    """Exponential delays with a cap and jitter."""

    def __init__(
        self,
        *,
        base: float = BASE_DELAY_SECONDS,
        cap: float = MAX_DELAY_SECONDS,
        jitter: float = JITTER,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._base = base
        self._cap = cap
        self._jitter = jitter
        self._rng = rng
        self._current = base

    def next_delay(self) -> float:
        """The delay to wait before the next attempt; each call doubles the next one."""
        delay = min(self._current, self._cap)
        self._current = min(self._current * 2, self._cap)
        return delay * (1 + self._jitter * (2 * self._rng() - 1))

    def reset(self) -> None:
        self._current = self._base
