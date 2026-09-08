"""Normalize MERGE input schemas for free-form validation.

The free-form validator stores a single source schema as ``tuple[FieldSpec, ...]``.
When a second source is attached to MERGE, the legacy packing path can flatten the
first schema and append the second schema tuple, producing a shape like::

    (FieldSpec(...), FieldSpec(...), (FieldSpec(...), FieldSpec(...)))

``OperationDefinition.infer_output_schema`` historically assumed the first item
was itself a schema tuple, so it tried to iterate one ``FieldSpec`` and failed.

This focused compatibility patch makes MERGE inference accept all three shapes:

* one schema: ``(FieldSpec, FieldSpec, ...)``
* correctly nested multi-schema: ``((FieldSpec, ...), (FieldSpec, ...))``
* legacy flattened multi-schema: ``(FieldSpec, ..., (FieldSpec, ...))``

MERGE output schema is defined by the first compatible input schema, so recovering
the first schema is sufficient and preserves the existing compatibility checks.
"""

from collections.abc import Mapping

from .models.dependency_graph import OperationKind


_APPLIED = False


def _field_like(value):
    if isinstance(value, Mapping):
        return bool(value.get("name"))
    name = getattr(value, "name", None)
    return callable(name) or bool(name)


def _first_merge_schema(value):
    """Return the first field collection from any supported MERGE schema shape."""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        return (value,)

    items = tuple(value)
    if not items:
        return ()

    first = items[0]
    if isinstance(first, (list, tuple)):
        # Correctly nested multi-input schema collection.
        return tuple(first)

    if _field_like(first):
        # Single schema, or the legacy flattened first schema followed by one or
        # more nested schemas. Keep the leading field-like items only.
        fields = []
        for item in items:
            if isinstance(item, (list, tuple)):
                break
            if _field_like(item):
                fields.append(item)
        return tuple(fields)

    return items


def apply_merge_schema_fix():
    """Patch MERGE schema inference exactly once per QGIS plugin process."""

    global _APPLIED
    if _APPLIED:
        return

    from .core.operation_registry import OperationDefinition

    original = OperationDefinition.infer_output_schema

    def infer_output_schema(self, input_schemas=None, parameters=None):
        if self.kind != OperationKind.MERGE:
            return original(self, input_schemas, parameters)

        inputs = dict(input_schemas or {})
        first_schema = _first_merge_schema(inputs.get("input"))
        return tuple(self._schema_fields(first_schema))

    OperationDefinition.infer_output_schema = infer_output_schema
    _APPLIED = True
