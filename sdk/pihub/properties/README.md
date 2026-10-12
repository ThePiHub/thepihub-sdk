# Properties Module

Status: Active

## Responsibilities

Define:

- Property Schemas
- Default Values

## Property Types

- StringProperty
- NumberProperty
- BooleanProperty
- SelectProperty

## Produces

### Property Schema

```json
{
  "name": "title",
  "label": "Title",
  "control": "text"
}
```

### Default Values

```json
{
  "title": "Temperature"
}
```

## Rules

Property definitions are the source of truth.

Registration metadata is generated from properties.

## Must Not

- Perform UI rendering
- Store runtime values