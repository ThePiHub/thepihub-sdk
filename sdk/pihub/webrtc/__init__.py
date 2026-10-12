"""WebRTC for a device: part of `pip install thepihub` (except on a 32-bit Raspberry Pi, where it comes from apt).

A browser can open a direct connection to
this device, and the device reports how it connected. Nothing else moves over
it yet - a heartbeat only. Without the library installed this package still
imports; `webrtc_available()` says False, and the device behaves exactly as it
always has (it never announces WebRTC support, so the platform gives its viewers
a plain relayed session).

Importing this module never imports the WebRTC library. `PeerManager` does that
when it starts, so `import pihub` stays as light as it was.
"""

from pihub.webrtc.limits import DeviceLimits
from pihub.webrtc.peer_manager import PeerManager, webrtc_available

__all__ = ["DeviceLimits", "PeerManager", "webrtc_available"]
