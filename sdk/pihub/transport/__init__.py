"""Device SDK transports. The protocol is independent of how it is carried.

    from pihub.transport import ConsoleTransport
    from pihub.transport import WebSocketTransport
"""

from pihub.transport.console import ConsoleTransport
from pihub.transport.console import TransportDisconnectedError as ConsoleTransportDisconnectedError
from pihub.security import InsecureTransportError
from pihub.transport.websocket import (
    ConnectionState,
    TransportConnectionError,
    TransportDisconnectedError as WebSocketTransportDisconnectedError,
    WebSocketTransport,
)

__all__ = [
    "ConsoleTransport",
    "ConsoleTransportDisconnectedError",
    "WebSocketTransport",
    "WebSocketTransportDisconnectedError",
    "TransportConnectionError",
    "InsecureTransportError",
    "ConnectionState",
]
