"""Message types and their payload shapes.

Supported message types: runtime, command, lifecycle, notification,
registration, and (added for WebRTC) signal. Per the protocol's
backward-compatibility rule, unknown message types must be ignored rather
than raising - see `parse_message`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pihub.protocol.bindings import Binding
from pihub.protocol.envelope import Envelope

# Message types

MESSAGE_TYPE_RUNTIME = "runtime"
MESSAGE_TYPE_COMMAND = "command"
MESSAGE_TYPE_LIFECYCLE = "lifecycle"
MESSAGE_TYPE_NOTIFICATION = "notification"
MESSAGE_TYPE_REGISTRATION = "registration"
MESSAGE_TYPE_SIGNAL = "signal"

# Lifecycle events

LIFECYCLE_FIRST_USER_CONNECTED = "firstUserConnected"
LIFECYCLE_LAST_USER_DISCONNECTED = "lastUserDisconnected"
LIFECYCLE_VIEWER_COUNT_CHANGED = "viewerCountChanged"
LIFECYCLE_DASHBOARD_OPENED = "dashboardOpened"
LIFECYCLE_DASHBOARD_CLOSED = "dashboardClosed"

# Signal kinds. `media` and `usage` were added with live video: purely additive, so an older device ignores them.

SIGNAL_CAPABILITIES = "capabilities"   # device -> platform, right after registration
SIGNAL_OPEN = "open"                   # platform -> device: a viewer session grant
SIGNAL_ACCEPT = "accept"               # device -> platform
SIGNAL_REJECT = "reject"               # device -> platform
SIGNAL_OFFER = "offer"                 # platform -> device (browser's offer)
SIGNAL_ANSWER = "answer"               # device -> platform
SIGNAL_CANDIDATE = "candidate"         # platform -> device (browser's trickled candidates)
SIGNAL_STATE = "state"                 # device -> platform: connected | failed | disconnected
SIGNAL_RENEW = "renew"                 # platform -> device: restart the grant countdown
SIGNAL_CLOSE = "close"                 # either direction
SIGNAL_RESYNC = "resync"               # device -> platform, after a reconnect
SIGNAL_MEDIA = "media"                 # platform -> device: action grant | release (a media grant)
SIGNAL_USAGE = "usage"                 # device -> platform: mediaActive, when the device itself ended a grant

SIGNAL_KINDS = frozenset(
    {
        SIGNAL_CAPABILITIES,
        SIGNAL_OPEN,
        SIGNAL_ACCEPT,
        SIGNAL_REJECT,
        SIGNAL_OFFER,
        SIGNAL_ANSWER,
        SIGNAL_CANDIDATE,
        SIGNAL_STATE,
        SIGNAL_RENEW,
        SIGNAL_CLOSE,
        SIGNAL_RESYNC,
        SIGNAL_MEDIA,
        SIGNAL_USAGE,
    }
)

# Notification severities

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

# Widget capabilities

CAPABILITY_DISPLAY = "display"
CAPABILITY_COMMAND = "command"
CAPABILITY_LAYOUT = "layout"


class _Message:
    """Shared envelope <-> payload plumbing for concrete message types."""

    MESSAGE_TYPE: str

    def to_payload(self) -> Dict[str, Any]:
        raise NotImplementedError  # pragma: no cover

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "_Message":
        raise NotImplementedError  # pragma: no cover

    def to_envelope(self) -> Envelope:
        return Envelope(message_type=self.MESSAGE_TYPE, payload=self.to_payload())

    @classmethod
    def from_envelope(cls, envelope: Envelope) -> "_Message":
        return cls.from_payload(envelope.payload)

    def to_json(self) -> str:
        return self.to_envelope().to_json()

    @classmethod
    def from_json(cls, data: str) -> "_Message":
        return cls.from_envelope(Envelope.from_json(data))


@dataclass
class RuntimeMessage(_Message):
    """Device -> Browser: updates output widgets with runtime value(s)."""

    MESSAGE_TYPE = MESSAGE_TYPE_RUNTIME

    binding: Binding
    value: Optional[Any] = None
    values: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.values is None):
            raise ValueError(
                "RuntimeMessage requires exactly one of 'value' or 'values'"
            )

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"binding": self.binding.to_dict()}
        if self.value is not None:
            payload["value"] = self.value
        else:
            payload["values"] = self.values
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RuntimeMessage":
        if "binding" not in payload:
            raise ValueError("runtime payload missing 'binding'")
        has_value = "value" in payload
        has_values = "values" in payload
        if has_value == has_values:
            raise ValueError(
                "runtime payload must contain exactly one of 'value' or 'values'"
            )
        return cls(
            binding=Binding.from_dict(payload["binding"]),
            value=payload.get("value"),
            values=payload.get("values"),
        )


@dataclass
class CommandMessage(_Message):
    """Browser -> Device: invokes an action on a command widget."""

    MESSAGE_TYPE = MESSAGE_TYPE_COMMAND

    binding: Binding
    action: Optional[str] = None
    values: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if (self.action is None) == (self.values is None):
            raise ValueError(
                "CommandMessage requires exactly one of 'action' or 'values'"
            )

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"binding": self.binding.to_dict()}
        if self.action is not None:
            payload["action"] = self.action
        else:
            payload["values"] = self.values
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "CommandMessage":
        if "binding" not in payload:
            raise ValueError("command payload missing 'binding'")
        has_action = "action" in payload
        has_values = "values" in payload
        if has_action == has_values:
            raise ValueError(
                "command payload must contain exactly one of 'action' or 'values'"
            )
        return cls(
            binding=Binding.from_dict(payload["binding"]),
            action=payload.get("action"),
            values=payload.get("values"),
        )


@dataclass
class LifecycleMessage(_Message):
    """Platform -> Device: informs programs about platform activity."""

    MESSAGE_TYPE = MESSAGE_TYPE_LIFECYCLE

    event: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"event": self.event}
        payload.update(self.data)
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "LifecycleMessage":
        if "event" not in payload:
            raise ValueError("lifecycle payload missing 'event'")
        data = {key: value for key, value in payload.items() if key != "event"}
        return cls(event=payload["event"], data=data)

    @classmethod
    def first_user_connected(cls) -> "LifecycleMessage":
        return cls(event=LIFECYCLE_FIRST_USER_CONNECTED)

    @classmethod
    def last_user_disconnected(cls) -> "LifecycleMessage":
        return cls(event=LIFECYCLE_LAST_USER_DISCONNECTED)

    @classmethod
    def viewer_count_changed(cls, viewer_count: int) -> "LifecycleMessage":
        return cls(
            event=LIFECYCLE_VIEWER_COUNT_CHANGED,
            data={"viewerCount": viewer_count},
        )

    @classmethod
    def dashboard_opened(cls, dashboard_id: str) -> "LifecycleMessage":
        return cls(
            event=LIFECYCLE_DASHBOARD_OPENED,
            data={"dashboardId": dashboard_id},
        )

    @classmethod
    def dashboard_closed(cls, dashboard_id: str) -> "LifecycleMessage":
        return cls(
            event=LIFECYCLE_DASHBOARD_CLOSED,
            data={"dashboardId": dashboard_id},
        )


@dataclass
class NotificationMessage(_Message):
    """Device -> Platform: informs users about program activity."""

    MESSAGE_TYPE = MESSAGE_TYPE_NOTIFICATION

    device_id: str
    program_id: str
    severity: str
    message: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "programId": self.program_id,
            "severity": self.severity,
            "message": self.message,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "NotificationMessage":
        missing = [
            f
            for f in ("deviceId", "programId", "severity", "message")
            if f not in payload
        ]
        if missing:
            raise ValueError(
                f"notification payload missing required field(s): {', '.join(missing)}"
            )
        return cls(
            device_id=payload["deviceId"],
            program_id=payload["programId"],
            severity=payload["severity"],
            message=payload["message"],
        )

    @classmethod
    def info(cls, device_id: str, program_id: str, message: str) -> "NotificationMessage":
        return cls(
            device_id=device_id, program_id=program_id, severity=SEVERITY_INFO, message=message
        )

    @classmethod
    def warning(cls, device_id: str, program_id: str, message: str) -> "NotificationMessage":
        return cls(
            device_id=device_id,
            program_id=program_id,
            severity=SEVERITY_WARNING,
            message=message,
        )

    @classmethod
    def error(cls, device_id: str, program_id: str, message: str) -> "NotificationMessage":
        return cls(
            device_id=device_id, program_id=program_id, severity=SEVERITY_ERROR, message=message
        )


@dataclass
class WidgetRegistration:
    """A single widget entry within a program's registration.

    `property_schema` and `default_properties` are optional, per the
    Widget Metadata and Property Schema Registration Prompt - a widget
    declared with no `properties=[...]` (the common case for existing
    code) has neither, and `to_dict()` omits both keys entirely rather
    than emitting empty `[]`/`{}`, so a registration with no property
    metadata round-trips to exactly the same wire shape as before this
    feature existed. This mirrors `RegistrationMessage.fingerprint`'s own
    "omit when absent" precedent and the protocol's extension rule
    (add optional fields only).
    """

    widget_id: str
    type: str
    capabilities: List[str] = field(default_factory=list)
    property_schema: List[Dict[str, Any]] = field(default_factory=list)
    default_properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "widgetId": self.widget_id,
            "type": self.type,
            "capabilities": list(self.capabilities),
        }
        if self.property_schema:
            payload["propertySchema"] = list(self.property_schema)
        if self.default_properties:
            payload["defaultProperties"] = dict(self.default_properties)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WidgetRegistration":
        missing = [f for f in ("widgetId", "type") if f not in data]
        if missing:
            raise ValueError(
                f"widget registration missing required field(s): {', '.join(missing)}"
            )
        return cls(
            widget_id=data["widgetId"],
            type=data["type"],
            capabilities=list(data.get("capabilities", [])),
            property_schema=list(data.get("propertySchema", [])),
            default_properties=dict(data.get("defaultProperties", {})),
        )


@dataclass
class ProgramRegistration:
    """A single program entry within a device's registration."""

    program_id: str
    name: str
    widgets: List[WidgetRegistration] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "programId": self.program_id,
            "name": self.name,
            "widgets": [widget.to_dict() for widget in self.widgets],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProgramRegistration":
        missing = [f for f in ("programId", "name") if f not in data]
        if missing:
            raise ValueError(
                f"program registration missing required field(s): {', '.join(missing)}"
            )
        widgets = [
            WidgetRegistration.from_dict(widget)
            for widget in data.get("widgets", [])
        ]
        return cls(
            program_id=data["programId"],
            name=data["name"],
            widgets=widgets,
        )


@dataclass
class RegistrationMessage(_Message):
    """Device -> Platform: registers a device's programs and widgets.

    `fingerprint` is optional, following the protocol's extension rules (add
    optional fields only; unknown fields must be ignored) - a `RegistrationMessage` built
    without one (or received from a platform/SDK version that never
    started sending one) round-trips exactly as before, with no
    `fingerprint` key on the wire at all. It's a plain dict (the shape
    `DeviceFingerprint.to_dict()` produces - see `pihub.device_identity`)
    rather than a dataclass, since the protocol layer doesn't need to
    depend on the device identity layer to carry it.
    """

    MESSAGE_TYPE = MESSAGE_TYPE_REGISTRATION

    device_id: str
    programs: List[ProgramRegistration] = field(default_factory=list)
    fingerprint: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "deviceId": self.device_id,
            "programs": [program.to_dict() for program in self.programs],
        }
        if self.fingerprint is not None:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RegistrationMessage":
        if "deviceId" not in payload:
            raise ValueError("registration payload missing 'deviceId'")
        programs = [
            ProgramRegistration.from_dict(program)
            for program in payload.get("programs", [])
        ]
        return cls(
            device_id=payload["deviceId"],
            programs=programs,
            fingerprint=payload.get("fingerprint"),
        )


@dataclass
class SignalMessage(_Message):
    """WebRTC signalling: viewer session grants, offers, answers, candidates.

    Platform <-> device over the device's authenticated WebSocket (and, later,
    in-band over a DataChannel). One additive message type.
    `viewer_session_id` is absent for the
    kinds that are about the connection rather than a session
    (`capabilities`, `resync`).

    `kind` is not validated against `SIGNAL_KINDS`: a kind this version does
    not know is carried through and ignored by whoever handles it, the same
    forward-compatibility rule the protocol applies to message types.
    """

    MESSAGE_TYPE = MESSAGE_TYPE_SIGNAL

    kind: str
    viewer_session_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"kind": self.kind}
        if self.viewer_session_id is not None:
            payload["viewerSessionId"] = self.viewer_session_id
        payload.update(self.data)
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SignalMessage":
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("signal payload missing 'kind'")
        session_id = payload.get("viewerSessionId")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("signal 'viewerSessionId' must be a string")
        data = {key: value for key, value in payload.items() if key not in ("kind", "viewerSessionId")}
        return cls(kind=kind, viewer_session_id=session_id, data=data)


_MESSAGE_CLASSES_BY_TYPE = {
    MESSAGE_TYPE_RUNTIME: RuntimeMessage,
    MESSAGE_TYPE_COMMAND: CommandMessage,
    MESSAGE_TYPE_LIFECYCLE: LifecycleMessage,
    MESSAGE_TYPE_NOTIFICATION: NotificationMessage,
    MESSAGE_TYPE_REGISTRATION: RegistrationMessage,
    MESSAGE_TYPE_SIGNAL: SignalMessage,
}

KNOWN_MESSAGE_TYPES = frozenset(_MESSAGE_CLASSES_BY_TYPE.keys())


def is_known_message_type(message_type: str) -> bool:
    return message_type in KNOWN_MESSAGE_TYPES


def parse_message(envelope: Envelope) -> Optional[_Message]:
    """Parse an envelope's payload into its specific message dataclass.

    Returns None for unknown message types rather than raising, per the
    protocol's rule: "Unknown Message Type -> Ignore It". A known type
    with an invalid payload still raises ValueError.
    """
    message_class = _MESSAGE_CLASSES_BY_TYPE.get(envelope.message_type)
    if message_class is None:
        return None
    return message_class.from_envelope(envelope)
