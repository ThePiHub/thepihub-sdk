"""Registration client: the device registration flow.

Performs the one-time HTTP exchange of a registration token (created by
a user via `POST /registration-token`) for a device secret. This is
deliberately separate from, and prior to, the ongoing WebSocket-based
protocol (`RegistrationMessage` et al. in `pihub.protocol`): the token
exchange happens once per registration token, over plain HTTP, before
any WebSocket connection exists - it is not a protocol envelope message
and does not go through `Envelope`/`parse_message`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from pihub.security import require_secure_url

HttpPost = Callable[[str, Dict[str, Any]], Dict[str, Any]]

# The platform this SDK talks to when nothing else says otherwise - `pihub`'s own default (`cli.py`)
# and `Device(..., register_if_needed=True)`'s fallback (`device.py`) both resolve from here, so
# there is exactly one place this URL is written down.
DEFAULT_SERVER = "https://thepihub.com"
SERVER_ENV = "PIHUB_SERVER"


class RegistrationError(Exception):
    """Raised when the registration token exchange fails.

    `status` is the HTTP status when the server answered at all, and
    `None` when it could not be reached - a caller (the `pihub` CLI) needs
    that difference to decide between retrying and giving up.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def default_http_post(url: str, payload: Dict[str, Any], *, timeout: float = 10.0) -> Dict[str, Any]:
    """POST `payload` as JSON to `url` and return the decoded JSON response.

    The only HTTP client this SDK needs - a single POST/JSON round trip -
    so this uses the standard library rather than adding a dependency.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read()).get("detail", "")
        except (ValueError, AttributeError):
            pass
        message = f"registration request failed with status {error.code}"
        if detail:
            message += f": {detail}"
        raise RegistrationError(message, status=error.code) from error
    except urllib.error.URLError as error:
        raise RegistrationError(f"registration request failed: {error.reason}") from error


def exchange_registration_token(
    server: str,
    device_id: str,
    token: str,
    fingerprint: Dict[str, Any],
    *,
    http_post: HttpPost = default_http_post,
    allow_insecure: Optional[bool] = None,
) -> Dict[str, Any]:
    """Exchange `token` for a device secret.

    `server` is the platform's HTTPS base URL (e.g.
    `https://platform.example.com`, or `http://localhost:8000` for local
    development). Returns the decoded
    `{"deviceId": ..., "deviceSecret": ...}` response.

    This exchange carries a registration token in the request and a
    device secret in the response - the two most valuable things a device
    ever sends or receives - so a plaintext `http://` URL to anything but
    loopback is refused. Set
    `PIHUB_ALLOW_INSECURE=1` (or pass `allow_insecure=True`) to override.
    """
    require_secure_url(server, allow_insecure=allow_insecure)
    url = f"{server.rstrip('/')}/devices/register"
    payload = {"deviceId": device_id, "registrationToken": token, "fingerprint": fingerprint}
    return http_post(url, payload)
