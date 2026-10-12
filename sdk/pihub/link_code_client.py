"""Shared client logic for the device-link-code flow, used by both `pihub setup`
(`cli.py::run_setup`) and `Device(..., register_if_needed=True)`'s fallback
(`device.py::start()`) - the poll-until-approved loop asked for once, used by
both, rather than duplicated.

Deliberately just the network/timing logic - no printing, no confirmation
prompts. `cli.py` keeps its own `out`/`err`/`input_fn` around these calls;
`device.py` uses `print`/`input` directly. Neither duplicates the request/poll
mechanics.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Dict, Optional

from pihub.registration_client import HttpPost, RegistrationError, default_http_post

# Ask the server to hold each poll open this long, rather than looping.
POLL_WAIT_SECONDS = 20
# Slightly longer than that wait, or every long poll would time out first.
POLL_HTTP_TIMEOUT_SECONDS = POLL_WAIT_SECONDS + 10
RETRY_DELAY_SECONDS = 3.0


class LinkExpiredError(Exception):
    """The link code expired before it was approved."""


def request_link(
    server: str,
    device_name: Optional[str],
    *,
    device_id: Optional[str] = None,
    post: HttpPost = default_http_post,
) -> Dict[str, Any]:
    """`POST /device-link`, returning the raw `{linkCode, pollSecret, expiresIn, linkUrl}` response.

    `device_id` (used by `register_if_needed`): request this exact
    id instead of letting the platform derive one from `device_name` - omitted (not merely `None`)
    when not given, so the ordinary `pihub setup` path sends exactly the payload it always has.
    """
    payload: Dict[str, Any] = {"deviceName": device_name}
    if device_id is not None:
        payload["deviceId"] = device_id
    return post(f"{server}/device-link", payload)


def poll_until_approved(
    server: str,
    link: Dict[str, Any],
    *,
    poll_post: Optional[HttpPost] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Long-poll `/device-link/poll` until approved.

    Raises `LinkExpiredError` if the code expires before that (either the server says so, or the
    caller's own clock reaches `link`'s `expiresIn` deadline first), or re-raises any other
    `RegistrationError` the server refuses outright (anything but a transient 5xx/429, which is
    retried after `RETRY_DELAY_SECONDS` instead - a network drop or a server hiccup should not cost
    anyone their link code).
    """
    if poll_post is None:
        poll_post = functools.partial(default_http_post, timeout=POLL_HTTP_TIMEOUT_SECONDS)
    deadline = clock() + float(link["expiresIn"])
    while True:
        try:
            response = poll_post(
                f"{server}/device-link/poll", {"pollSecret": link["pollSecret"], "wait": POLL_WAIT_SECONDS}
            )
        except RegistrationError as error:
            if error.status == 404:
                raise LinkExpiredError() from error
            if error.status is not None and error.status < 500 and error.status != 429:
                raise
            response = None
            sleep(RETRY_DELAY_SECONDS)

        if response is not None and response.get("status") == "approved":
            return response
        if clock() >= deadline:
            raise LinkExpiredError()
