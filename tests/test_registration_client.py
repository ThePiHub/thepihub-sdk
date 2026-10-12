"""Tests for `pihub.registration_client`: the device registration flow."""

from __future__ import annotations

import json
import urllib.error

import pytest

from pihub.registration_client import RegistrationError, exchange_registration_token


class RecordingHttpPost:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list = []

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        return self.response


def test_posts_to_devices_register_under_the_given_server():
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})

    exchange_registration_token(
        "http://localhost:8000", "birdbox01", "ABC123XY", {"deviceType": "linux"}, http_post=http_post
    )

    assert http_post.calls == [
        (
            "http://localhost:8000/devices/register",
            {
                "deviceId": "birdbox01",
                "registrationToken": "ABC123XY",
                "fingerprint": {"deviceType": "linux"},
            },
        )
    ]


def test_strips_a_trailing_slash_from_the_server_url():
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})

    exchange_registration_token("http://localhost:8000/", "birdbox01", "ABC123XY", {}, http_post=http_post)

    assert http_post.calls[0][0] == "http://localhost:8000/devices/register"


def test_returns_the_decoded_response():
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})

    response = exchange_registration_token(
        "http://localhost:8000", "birdbox01", "ABC123XY", {}, http_post=http_post
    )

    assert response == {"deviceId": "birdbox01", "deviceSecret": "s3cr3t"}


def _http_error(code: int, detail: str) -> urllib.error.HTTPError:
    body = json.dumps({"detail": detail}).encode()
    return urllib.error.HTTPError(
        url="http://localhost:8000/devices/register",
        code=code,
        msg=detail,
        hdrs=None,
        fp=__import__("io").BytesIO(body),
    )


def test_default_http_post_raises_registration_error_with_server_detail(monkeypatch):
    def raise_http_error(request, timeout):
        raise _http_error(404, "unknown registration token")

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError, match="404.*unknown registration token"):
        default_http_post("http://localhost:8000/devices/register", {})


def test_default_http_post_returns_the_decoded_json_response(monkeypatch):
    import io

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout):
        return FakeResponse(json.dumps({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from pihub.registration_client import default_http_post

    response = default_http_post("http://localhost:8000/devices/register", {"registrationToken": "X"})

    assert response == {"deviceId": "birdbox01", "deviceSecret": "s3cr3t"}


def test_default_http_post_falls_back_to_a_plain_message_when_error_body_is_not_json(monkeypatch):
    def raise_http_error(request, timeout):
        error = urllib.error.HTTPError(
            url="http://localhost:8000/devices/register",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=__import__("io").BytesIO(b"not json"),
        )
        raise error

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError, match="registration request failed with status 500$"):
        default_http_post("http://localhost:8000/devices/register", {})


def test_default_http_post_raises_registration_error_on_connection_failure(monkeypatch):
    def raise_url_error(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError, match="connection refused"):
        default_http_post("http://localhost:8000/devices/register", {})


def test_registration_error_carries_the_http_status_when_the_server_answered(monkeypatch):
    def raise_http_error(request, timeout):
        raise _http_error(404, "unknown or expired link")

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError) as raised:
        default_http_post("http://localhost:8000/device-link/poll", {})

    assert raised.value.status == 404


def test_registration_error_has_no_status_when_the_server_was_unreachable(monkeypatch):
    def raise_url_error(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError) as raised:
        default_http_post("http://localhost:8000/device-link", {})

    assert raised.value.status is None


def test_default_http_post_honours_a_longer_timeout_for_long_polls(monkeypatch):
    seen = {}

    def capture(request, timeout):
        seen["timeout"] = timeout
        raise urllib.error.URLError("stop here")

    monkeypatch.setattr("urllib.request.urlopen", capture)

    from pihub.registration_client import default_http_post

    with pytest.raises(RegistrationError):
        default_http_post("http://localhost:8000/device-link/poll", {}, timeout=30)

    assert seen["timeout"] == 30
