# Protocol Module

Status: Frozen

## Responsibilities

- Envelope
- Binding
- Message Types
- Serialization
- Deserialization
- Validation

## Protocol Principles

All messages use:

```json
{
  "protocolVersion": 1,
  "messageType": "...",
  "payload": {}
}
```

## Supported Message Types

- registration
- runtime
- command
- lifecycle
- notification

## Canonical Runtime Key

```text
deviceId.programId.widgetId
```

Example:

```text
greenhouse01.weather.temperature
```

## Rules

Unknown message types:

```text
Ignore
```

Unknown fields:

```text
Ignore
```

## Must Not

- Open sockets
- Know about transports
- Know about UI
- Know about dashboards