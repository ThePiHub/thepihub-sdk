# Runtime Module

Status: Stable

## Responsibilities

- RuntimeValue
- RuntimeStore

## Principles

The Runtime Store is:

```text
Cache
```

The Runtime Store is NOT:

```text
Source Of Truth
```

The device owns runtime state.

## Key Structure

```text
deviceId.programId.widgetId
```

## Examples

```text
greenhouse01.weather.temperature
birdbox01.health.cpu
```

## Supported Data

### Single Value

```json
{
  "value": 22.5
}
```

### Multi Value

```json
{
  "values": {
    "cpu": 10,
    "memory": 40
  }
}
```

## Must Not

- Own business state
- Persist data
- Perform network I/O