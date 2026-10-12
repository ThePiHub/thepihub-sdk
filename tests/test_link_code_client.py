"""Tests for `pihub.link_code_client` - the request/poll logic shared by `pihub setup`
(`cli.py::run_setup`) and `Device(..., register_if_needed=True)`.
"""

from __future__ import annotations

import pytest

from pihub.link_code_client import LinkExpiredError, poll_until_approved, request_link
from pihub.registration_client import RegistrationError

LINK = {"linkCode": "M7FR-XQ4B", "pollSecret": "poll-secret", "expiresIn": 600, "linkUrl": "https://x/link"}


class ScriptedPost:
    """Answers each call from a queue; records every call."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        step = self.responses.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_request_link_posts_device_name_only_when_no_id_is_given():
    post = ScriptedPost([LINK])

    request_link("https://x", "birdbox", post=post)

    assert post.calls == [("https://x/device-link", {"deviceName": "birdbox"})]


def test_request_link_includes_a_requested_device_id_when_given():
    post = ScriptedPost([LINK])

    request_link("https://x", "birdbox", device_id="my-own-id", post=post)

    assert post.calls == [("https://x/device-link", {"deviceName": "birdbox", "deviceId": "my-own-id"})]


def test_poll_until_approved_returns_the_approval():
    approved = {"status": "approved", "registrationToken": "TOKEN", "deviceId": "birdbox-8f4c2d"}
    poll_post = ScriptedPost([approved])

    result = poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=Clock())

    assert result == approved


def test_poll_until_approved_keeps_polling_while_pending():
    poll_post = ScriptedPost(
        [{"status": "pending"}, {"status": "pending"}, {"status": "approved", "registrationToken": "T", "deviceId": "d"}]
    )
    clock = Clock()

    result = poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=clock)

    assert result["status"] == "approved"
    assert len(poll_post.calls) == 3


def test_poll_until_approved_raises_link_expired_on_a_404():
    poll_post = ScriptedPost([RegistrationError("gone", status=404)])

    with pytest.raises(LinkExpiredError):
        poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=Clock())


def test_poll_until_approved_raises_link_expired_when_the_deadline_passes():
    clock = Clock()

    def advancing_clock():
        clock.now += 700  # past the 600s expiresIn on the very first check
        return clock.now

    poll_post = ScriptedPost([{"status": "pending"}])

    with pytest.raises(LinkExpiredError):
        poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=advancing_clock)


def test_poll_until_approved_reraises_an_unrecoverable_client_error():
    poll_post = ScriptedPost([RegistrationError("bad request", status=400)])

    with pytest.raises(RegistrationError):
        poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=Clock())


def test_poll_until_approved_retries_past_a_transient_server_error():
    approved = {"status": "approved", "registrationToken": "T", "deviceId": "d"}
    poll_post = ScriptedPost([RegistrationError("bad gateway", status=502), approved])
    sleeps: list = []

    result = poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=sleeps.append, clock=Clock())

    assert result == approved
    assert sleeps == [3.0]


def test_poll_until_approved_retries_past_being_rate_limited():
    approved = {"status": "approved", "registrationToken": "T", "deviceId": "d"}
    poll_post = ScriptedPost([RegistrationError("slow down", status=429), approved])

    result = poll_until_approved("https://x", LINK, poll_post=poll_post, sleep=lambda _s: None, clock=Clock())

    assert result == approved
