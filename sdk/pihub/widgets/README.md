# Widgets Module

Status: Active

## Responsibilities

Device-side widget definitions.

## Widget Types

### Output

Examples:

- Gauge
- Status
- Video
- Chart

Capability:

```text
display
```

### Command

Examples:

- Button
- Toggle
- Slider
- Joystick

Capability:

```text
command
```

### Presentation

Examples:

- Heading
- Folder

Capability:

```text
layout
```

## Important

These are:

```text
Runtime Endpoints
```

not dashboard widgets.

Dashboard layout and presentation belong to the platform.

## Must Not

- Store dashboard position
- Store layout
- Know about GridStack
- Know about Vue