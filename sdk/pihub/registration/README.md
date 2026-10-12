# Registration Module

Status: Active

## Responsibilities

Build device registration manifests.

## Output

Produces:

```python
RegistrationMessage
```

from:

```text
Device
Programs
Widgets
Properties
Fingerprint
```

## Registration Includes

- Device Identity
- Programs
- Widgets
- Property Metadata
- Default Properties

## Source Of Truth

ManifestBuilder is the only registration generator.

Do not duplicate registration logic.

## Must Not

- Perform network I/O
- Authenticate devices