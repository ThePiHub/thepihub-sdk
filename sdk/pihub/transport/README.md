# Transport Module

Status: Active

## Responsibilities

Move messages between:

```text
Device
↔
Platform
```

## Current Transports

### ConsoleTransport

Purpose:

```text
Development
Testing
```

### WebSocketTransport

Purpose:

```text
Platform Communications
```

## WebRTC (optional)

Not a `Transport`: a device has *N* peer connections plus one control
connection, so WebRTC is `pihub.webrtc.PeerManager` beside the transport, not
a replacement for it. The WebSocket transport is the control connection and
carries the `signal` messages that set a peer connection up.

```text
pip install thepihub              includes it (not on a 32-bit Pi: apt install python3-aiortc python3-av)
Device(webrtc=False)             or PIHUB_WEBRTC=0, to turn it off
```

Over a peer connection a device sends a heartbeat, runtime values and live video, and receives commands.

## Future

### Runtime traffic, commands, video, photos, recordings

Over the peer connection, in later phases.

## Transport Contract

Required:

```python
send()
```

Optional:

```python
connect()
disconnect()
receive()
```

## Principles

Transports move data.

They do not:

```text
Understand Runtime Values
Understand Commands
Understand Widgets
```

## Must Not

- Know business logic
- Know dashboard logic
## Transport Security

TLS is mandatory:

```text
wss://  required
ws://   refused, except to loopback
```

Plaintext to anything but `localhost` raises
`InsecureTransportError` at construction, so it is a deliberate choice
rather than a silent default. Override for local development with:

```text
PIHUB_ALLOW_INSECURE=1
```

The same rule applies to the registration token exchange
(`registration_client.py`), which carries a registration token out and a
device secret back.
