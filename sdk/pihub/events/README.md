# Events Module

Status: Frozen

## Responsibilities

- EventBus
- Event subscription
- Event publishing
- Event dispatch

## Principles

- Synchronous
- Thread-safe
- In-process only
- No persistence

## Event Naming

Examples:

```text
lifecycle.firstUserConnected
lifecycle.lastUserDisconnected

command.snapshot
command.restart

notification.info
notification.warning
notification.error