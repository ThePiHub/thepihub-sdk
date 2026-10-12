"""Registration manifest builder: the SDK produces the registration manifest
automatically, so developers never build protocol messages by hand.

`Device.start()` needs to build a `RegistrationMessage` from its attached
programs' widgets; this module is that logic, factored out into a
reusable, independently testable function - `Device.start()` delegates to
it rather than duplicating it inline.

`build_device_manifest()` takes a `device_id` and an iterable of
program-like objects (anything with `.to_registration() ->
ProgramRegistration`, matching the *existing* `Program.to_registration()`
- see program.py - which itself delegates to each attached `Widget`'s own
`to_registration()`). So building a manifest exercises the existing
Device/Program/Widget classes end to end without reimplementing any of
their logic.

A local, structurally-equivalent redeclaration of the "program-like"
contract is used here rather than importing `device.py`'s own
`RegistrableProgram`: `device.py` imports this module to delegate to it,
so this module must not import `device.py` back. Since Python's
`Protocol` types are structural, any real `Program` (or anything else
satisfying either declaration) works with both without conflict.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol, runtime_checkable

from pihub.device_identity import DeviceFingerprint
from pihub.protocol.messages import ProgramRegistration, RegistrationMessage


@runtime_checkable
class RegistrableProgram(Protocol):
    """The minimal shape this builder needs from a program-like object."""

    id: str

    def to_registration(self) -> ProgramRegistration: ...


def build_device_manifest(
    device_id: str,
    programs: Iterable[RegistrableProgram],
    fingerprint: Optional[DeviceFingerprint] = None,
) -> RegistrationMessage:
    """Build the `registration` message for a device's current programs.

    Per "Device Manifest Generation", this is what `device.start()` sends
    automatically - developers never build this message by hand.

    `fingerprint` is optional and defaults to `None` (omitted from the
    wire payload entirely) - this builder stays a pure function with no
    I/O of its own. `Device.start()` is what automatically collects one
    via `pihub.device_identity.collect_fingerprint()` and passes it in,
    because collection must be automatic - callers of this function directly (tests, or any future
    caller that already has its own fingerprint) can supply their own.
    """
    return RegistrationMessage(
        device_id=device_id,
        programs=[program.to_registration() for program in programs],
        fingerprint=fingerprint.to_dict() if fingerprint is not None else None,
    )
