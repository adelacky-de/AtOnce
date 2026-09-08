"""Declarative operation definitions for the free-form workflow graph.

The registry is deliberately QGIS-free.  It describes the contract a canvas,
validator, and executor must agree on; the actual QGIS adapters live at the
infrastructure boundary.
"""

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .lineage import RESERVED_LINEAGE_FIELDS
from ..models.dependency_graph import OperationKind


@dataclass(frozen=True)
class PortDefinition:
    """One named operation input or output port."""

    port_id: str
    label: str
    required: bool = True
    min_count: int = 1
    max_count: Optional[int] = 1
    allow_fanout: bool = False


@dataclass(frozen=True)
class ParameterDefinition:
    """The persisted type/default/choice contract for one operation value."""

    parameter_id: str
    value_type: str = "string"
    required: bool = False
    default: object = None
    choices: Tuple[object, ...] = ()
    ui_kind: str = ""
    row_fields: Tuple[str, ...] = ()
    context_port: str = "input"

    @property
    def type(self):
        """Compatibility alias for callers describing the contract as ``type``."""

        return self.value_type

    @property
    def type_name(self):
        return self.value_type


@dataclass(frozen=True)
class SchemaField:
    """QGIS-free field shape used for authoring-time schema inference."""

    name: str
    type_name: str = "string"
    length: int = 0
    precision: int = 0


class InferredSchema:
    """Minimal layer-shaped view for pre-execution authoring validation."""

    def __init__(self, fields=()):
        self._fields = tuple(
            type("SchemaQField", (), {
                "name": lambda self, value=item.name: value,
                "type": lambda self, value=item.type_name: value,
            })()
            for item in OperationDefinition._schema_fields(fields)
        )

    def fields(self):
        return self._fields


@dataclass(frozen=True)
class OperationDefinition:
    """Stable, serializable operation capability metadata."""

    kind: OperationKind
    title: str
    category: str
    input_ports: Tuple[PortDefinition, ...] = ()
    output_ports: Tuple[PortDefinition, ...] = (
        PortDefinition("result", "Result", allow_fanout=True),
    )
    accepted_data_types: Tuple[str, ...] = ("vector",)
    accepted_geometry_families: Tuple[str, ...] = ()
    parameter_schema: Tuple[str, ...] = ()
    parameter_definitions: Tuple[ParameterDefinition, ...] = ()
    validator: object = None
    executor_key: str = ""
    lineage_policy: str = "preserve_source_lineage"
    consumed_fields: Tuple[str, ...] = ()
    produced_fields: Tuple[str, ...] = ()
    schema_transform: str = "preserve"
    version: int = 1

    def __post_init__(self):
        if not self.parameter_schema and self.parameter_definitions:
            object.__setattr__(
                self,
                "parameter_schema",
                tuple(item.parameter_id for item in self.parameter_definitions),
            )

    @property
    def min_inputs(self) -> int:
        return sum(port.min_count for port in self.input_ports)

    @property
    def max_inputs(self) -> Optional[int]:
        if any(port.max_count is None for port in self.input_ports):
            return None
        return sum(port.max_count or 0 for port in self.input_ports)

    def input_port(self, port_id: str) -> Optional[PortDefinition]:
        wanted = str(port_id or "")
        return next((port for port in self.input_ports if port.port_id == wanted), None)

    def parameter_defaults(self) -> Dict[str, object]:
        return {
            item.parameter_id: item.default
            for item in self.parameter_definitions
            if item.default is not None
        }

    @property
    def field_dependencies(self) -> Dict[str, object]:
        """Stable metadata for a future field-aware propagation planner."""

        return {
            "consumed": tuple(self.consumed_fields),
            "produced": tuple(self.produced_fields),
            "schema_transform": self.schema_transform,
        }

    def validate_parameters(self, parameters=None, inputs=None, qgis=None) -> Tuple[str, ...]:
        values = dict(self.parameter_defaults())
        values.update(dict(parameters or {}))
        allowed = {item.parameter_id for item in self.parameter_definitions}
        errors = [
            f"{self.title} has unsupported parameter {key!r}."
            for key in sorted(set(values) - allowed)
        ]
        definitions = {item.parameter_id: item for item in self.parameter_definitions}
        for key, definition in definitions.items():
            value = values.get(key)
            missing = value is None or value == "" or (
                isinstance(value, (list, tuple)) and not value
            )
            if definition.required and missing:
                errors.append(f"{self.title} requires parameter {key!r}.")
                continue
            if missing:
                continue
            if definition.value_type == "bool" and type(value) is not bool:
                errors.append(f"{self.title} parameter {key!r} must be boolean.")
            elif definition.value_type == "list" and not isinstance(value, (list, tuple)):
                errors.append(f"{self.title} parameter {key!r} must be a list.")
            elif definition.value_type == "float":
                try:
                    if not math.isfinite(float(value)):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{self.title} parameter {key!r} must be numeric.")
            elif definition.value_type == "int":
                try:
                    if isinstance(value, bool) or int(value) != float(value):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{self.title} parameter {key!r} must be an integer.")
            if definition.choices and value not in definition.choices:
                errors.append(
                    f"{self.title} parameter {key!r} must be one of {definition.choices!r}."
                )
        if callable(self.validator):
            errors.extend(self.validator(values, dict(inputs or {}), qgis) or ())
        return tuple(dict.fromkeys(errors))

    @staticmethod
    def _schema_fields(value) -> Tuple[SchemaField, ...]:
        result = []
        for item in value or ():
            if isinstance(item, (list, tuple)) and not isinstance(item, (str, bytes)):
                result.extend(OperationDefinition._schema_fields(item))
                continue
            name = str(getattr(item, "name", "") or "").strip()
            if callable(getattr(item, "name", None)):
                name = str(item.name() or "").strip()
            if isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
                type_name = str(item.get("type_name") or item.get("type") or "string")
            else:
                type_name = str(
                    getattr(item, "type_name", getattr(item, "type", "string"))
                )
                if callable(getattr(item, "type", None)):
                    type_name = str(item.type())
            if name:
                result.append(SchemaField(name, type_name))
        return tuple(result)

    def resolve_field_dependencies(self, parameters=None, input_schemas=None):
        """Resolve actual consumed/produced fields for this registry entry."""

        values = dict(self.parameter_defaults())
        values.update(dict(parameters or {}))
        schemas = dict(input_schemas or {})
        consumed = {port_id: set() for port_id in schemas}

        def names(port):
            return {
                field.name
                for field in self._schema_fields(schemas.get(port))
                if field.name not in RESERVED_LINEAGE_FIELDS
            }

        def add(port, *items):
            if port in consumed:
                consumed[port].update(str(item) for item in items if str(item))

        def expression_fields(expression):
            values = {
                left or right
                for left, right in re.findall(
                    r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b',
                    str(expression or ""),
                )
            }
            return values

        if self.kind == OperationKind.FILTER:
            add("input", *(
                expression_fields(values.get("expression")) & names("input")
            ))
        elif self.kind == OperationKind.JOIN:
            add("left", values.get("left_field"))
            right_fields = _list_value(values.get("right_fields"))
            if not right_fields:
                right_fields = sorted(names("right"))
            add("right", values.get("right_field"), *right_fields)
        elif self.kind == OperationKind.SPATIAL_JOIN:
            fields = _list_value(values.get("fields")) or sorted(names("join"))
            add("join", *fields)
        elif self.kind == OperationKind.COMPARE_CHANGES:
            compare_fields = _list_value(values.get("compare_fields"))
            if not compare_fields:
                compare_fields = sorted(names("current"))
            add("previous", values.get("previous_key_field"), *compare_fields)
            add("current", values.get("current_key_field"), *compare_fields)
        elif self.kind == OperationKind.CALCULATE_FIELD:
            add("input", *(
                expression_fields(values.get("expression")) & names("input")
            ))
        elif self.kind in {
            OperationKind.KEEP_FIELDS,
            OperationKind.REMOVE_DUPLICATES,
        }:
            add("input", *_list_value(values.get("fields")))
        elif self.kind == OperationKind.FIELD_MAPPING:
            add("input", *(row.get("source_field") for row in _mapping_rows(values.get("mappings"))))
        elif self.kind == OperationKind.RENAME_FIELD:
            add("input", values.get("source_field"))
        elif self.kind == OperationKind.CHANGE_FIELD_TYPE:
            add("input", values.get("field"))
        elif self.kind == OperationKind.SORT:
            add("input", *(row.get("field") for row in _mapping_rows(values.get("sort_fields"))))
        elif self.kind == OperationKind.AGGREGATE:
            add("input", *_list_value(values.get("group_fields")))
            add("input", *(row.get("field") for row in _mapping_rows(values.get("aggregations"))))
        produced = set()
        if self.kind == OperationKind.CALCULATE_FIELD:
            produced.add(str(values.get("field_name") or ""))
        elif self.kind == OperationKind.FIELD_MAPPING:
            produced.update(
                str(row.get("output_field") or "")
                for row in _mapping_rows(values.get("mappings"))
            )
        elif self.kind == OperationKind.KEEP_FIELDS:
            produced.update(_list_value(values.get("fields")))
        elif self.kind == OperationKind.RENAME_FIELD:
            produced.add(str(values.get("target_name") or ""))
        elif self.kind == OperationKind.CHANGE_FIELD_TYPE:
            produced.add(str(values.get("field") or ""))
        elif self.kind == OperationKind.AGGREGATE:
            produced.update(_list_value(values.get("group_fields")))
            produced.update(
                str(row.get("output_field") or "")
                for row in _mapping_rows(values.get("aggregations"))
            )
        elif self.kind in {OperationKind.JOIN, OperationKind.SPATIAL_JOIN}:
            right_port = "right" if self.kind == OperationKind.JOIN else "join"
            right_names = names(right_port)
            selected = _list_value(values.get("right_fields") or values.get("fields"))
            produced.update(
                field for field in (selected or sorted(right_names))
                if field in right_names and field not in RESERVED_LINEAGE_FIELDS
            )
        return {
            "consumed": {
                port: tuple(sorted(fields)) for port, fields in consumed.items()
            },
            "produced": tuple(sorted(produced or self.produced_fields)),
            "schema_transform": self.schema_transform,
        }

    def infer_output_schema(self, input_schemas=None, parameters=None):
        """Infer a user-facing output schema without executing QGIS features."""

        values = dict(self.parameter_defaults())
        values.update(dict(parameters or {}))
        inputs = dict(input_schemas or {})

        def fields(port):
            return [
                field
                for field in self._schema_fields(inputs.get(port))
                if field.name not in RESERVED_LINEAGE_FIELDS
            ]

        base = fields("input")
        if self.kind in {
            OperationKind.FILTER, OperationKind.PASSTHROUGH, OperationKind.SORT,
            OperationKind.REMOVE_DUPLICATES, OperationKind.BUFFER,
            OperationKind.SELECT_BY_LOCATION, OperationKind.CLIP,
            OperationKind.REPROJECT,
        }:
            return tuple(base)
        if self.kind == OperationKind.FIELD_MAPPING:
            return tuple(
                SchemaField(str(row.get("output_field") or ""), str(row.get("output_type") or "string"))
                for row in _mapping_rows(values.get("mappings"))
                if row.get("output_field")
            )
        if self.kind == OperationKind.KEEP_FIELDS:
            wanted = set(_list_value(values.get("fields")))
            return tuple(field for field in base if field.name in wanted)
        if self.kind == OperationKind.RENAME_FIELD:
            source = str(values.get("source_field") or "")
            target = str(values.get("target_name") or "")
            return tuple(
                SchemaField(target if field.name == source else field.name, field.type_name)
                for field in base
            )
        if self.kind == OperationKind.CHANGE_FIELD_TYPE:
            field_name = str(values.get("field") or "")
            target = str(values.get("target_type") or "string")
            return tuple(
                SchemaField(field.name, target if field.name == field_name else field.type_name)
                for field in base
            )
        if self.kind == OperationKind.CALCULATE_FIELD:
            field_name = str(values.get("field_name") or "")
            target = str(values.get("field_type") or "string")
            result = [field for field in base if field.name != field_name]
            result.append(SchemaField(field_name, target))
            return tuple(result)
        if self.kind in {OperationKind.JOIN, OperationKind.SPATIAL_JOIN}:
            left = fields("left") if self.kind == OperationKind.JOIN else fields("target")
            right = fields("right") if self.kind == OperationKind.JOIN else fields("join")
            selected = set(_list_value(values.get("right_fields") or values.get("fields")))
            suffix = str(values.get("collision_suffix") or "_join")
            result = list(left)
            for field in right:
                if field.name in RESERVED_LINEAGE_FIELDS or (selected and field.name not in selected):
                    continue
                name = field.name
                if any(item.name == name for item in result):
                    name += suffix
                result.append(SchemaField(name, field.type_name))
            return tuple(result)
        if self.kind == OperationKind.MERGE:
            inputs_list = inputs.get("input") or ()
            if isinstance(inputs_list, (list, tuple)) and inputs_list:
                return tuple(self._schema_fields(inputs_list[0]))
            return tuple(self._schema_fields(inputs_list))
        if self.kind == OperationKind.AGGREGATE:
            by_name = {field.name: field for field in base}
            result = [by_name[name] for name in _list_value(values.get("group_fields")) if name in by_name]
            for row in _mapping_rows(values.get("aggregations")):
                function = str(row.get("function") or "").lower()
                output = str(row.get("output_field") or "")
                type_name = "integer" if function == "count" else "double" if function in {"sum", "mean"} else by_name.get(str(row.get("field") or ""), SchemaField("", "string")).type_name
                if output:
                    result.append(SchemaField(output, type_name))
            return tuple(result)
        if self.kind == OperationKind.COMPARE_CHANGES:
            previous = fields("previous")
            key = next((field for field in previous if field.name == values.get("previous_key_field")), None)
            return tuple(
                [SchemaField("key", key.type_name if key else "string"),
                 SchemaField("change_status", "string"),
                 SchemaField("changed_fields", "string"),
                 SchemaField("previous_values", "string"),
                 SchemaField("current_values", "string")]
            )
        if self.kind == OperationKind.SPATIAL_JOIN:
            return tuple(self.infer_output_schema({"target": inputs.get("target"), "join": inputs.get("join")}, values))
        return tuple(base)

    def infer_schema(self, input_schemas=None, parameters=None):
        return self.infer_output_schema(input_schemas, parameters)


class OperationRegistry:
    """Lookup boundary used by graph editing, UI, validation, and execution."""

    def __init__(self, definitions: Iterable[OperationDefinition] = ()):
        self._definitions: Dict[OperationKind, OperationDefinition] = {
            OperationKind(item.kind): item for item in definitions
        }

    def get(self, kind) -> Optional[OperationDefinition]:
        try:
            return self._definitions.get(OperationKind(kind))
        except (TypeError, ValueError):
            return None

    def require(self, kind) -> OperationDefinition:
        definition = self.get(kind)
        if definition is None:
            raise KeyError(f"No operation definition is registered for {kind!r}.")
        return definition

    def all(self) -> Tuple[OperationDefinition, ...]:
        return tuple(self._definitions.values())

    def resolve_field_dependencies(self, kind, parameters=None, input_schemas=None):
        return self.require(kind).resolve_field_dependencies(parameters, input_schemas)

    def infer_output_schema(self, kind, input_schemas=None, parameters=None):
        return self.require(kind).infer_output_schema(input_schemas, parameters)

    def infer_schema(self, kind, input_schemas=None, parameters=None):
        return self.require(kind).infer_output_schema(input_schemas, parameters)


def _param(
    parameter_id,
    value_type="string",
    *,
    required=False,
    default=None,
    choices=(),
    ui_kind="",
    row_fields=(),
    context_port="input",
):
    return ParameterDefinition(
        parameter_id,
        value_type,
        required,
        default,
        tuple(choices),
        ui_kind,
        tuple(row_fields),
        context_port,
    )


def _single_input(
    kind,
    title,
    category,
    executor_key,
    *,
    parameter_definitions=(),
    lineage="preserve_source_lineage",
    validator=None,
    consumed_fields=(),
    produced_fields=(),
    schema_transform="preserve",
    accepted_data_types=("vector",),
):
    parameter_definitions = tuple(parameter_definitions)
    return OperationDefinition(
        kind=kind,
        title=title,
        category=category,
        input_ports=(PortDefinition("input", "Input"),),
        accepted_data_types=tuple(accepted_data_types),
        parameter_schema=tuple(item.parameter_id for item in parameter_definitions),
        parameter_definitions=parameter_definitions,
        validator=validator,
        executor_key=executor_key,
        lineage_policy=lineage,
        consumed_fields=tuple(consumed_fields),
        produced_fields=tuple(produced_fields),
        schema_transform=schema_transform,
    )


def _layer(inputs, port_id):
    value = (inputs or {}).get(port_id)
    return value[0] if isinstance(value, (tuple, list)) and len(value) == 1 else value


def _layers(inputs, port_id):
    value = (inputs or {}).get(port_id)
    if value is None:
        return ()
    if isinstance(value, (tuple, list)):
        return tuple(value)
    return (value,)


def _field(layer, name):
    if layer is None or not name or not hasattr(layer, "fields"):
        return None
    return next((field for field in layer.fields() if field.name() == name), None)


def _field_type(field):
    if field is None:
        return None
    value = field.type() if callable(getattr(field, "type", None)) else getattr(field, "type_name", "")
    return str(value)


def _field_type_name(field):
    if field is None:
        return ""
    type_name = getattr(field, "typeName", None)
    if callable(type_name):
        value = str(type_name() or "").strip().lower()
        if value:
            return value
    return _field_type(field).strip().lower()


def _is_numeric_field(field):
    value = _field_type_name(field)
    return any(
        token in value
        for token in ("int", "real", "double", "float", "decimal", "numeric")
    )


def _list_value(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _mapping_rows(value):
    return tuple(item for item in (value or ()) if isinstance(item, Mapping))


def _safe_field_name(name):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")))


def _spatial_crs_error(inputs, qgis, operation, ports):
    layers = [_layer(inputs, port) for port in ports]
    if any(layer is None for layer in layers) or not callable(
        getattr(qgis, "require_equivalent_crs", None)
    ):
        return ()
    try:
        qgis.require_equivalent_crs(layers, operation)
    except Exception as exc:
        return (str(exc),)
    return ()


def _validate_filter(values, inputs, qgis):
    layer = _layer(inputs, "input")
    if layer is None or not callable(getattr(qgis, "validate_expression", None)):
        return ()
    valid, error = qgis.validate_expression(str(values.get("expression") or ""), layer)
    return () if valid else (f"FILTER expression is invalid: {error}",)


def _validate_merge(_values, inputs, qgis):
    layers = _layers(inputs, "input")
    if layers and len(layers) < 2:
        return ("MERGE requires at least two inputs.",)
    if layers:
        signatures = []
        for layer in layers:
            fields = tuple(
                (
                    field.name(),
                    _field_type(field),
                )
                for field in layer.fields()
                if field.name() not in RESERVED_LINEAGE_FIELDS
            )
            signatures.append(fields)
        if any(signature != signatures[0] for signature in signatures[1:]):
            return (
                "MERGE source field schemas/types are incompatible; explicit FIELD MAPPING is required.",
            )
    if layers and callable(getattr(qgis, "preflight_merge_layers", None)):
        try:
            qgis.preflight_merge_layers(layers)
        except Exception as exc:
            return (f"MERGE inputs are incompatible: {exc}",)
    return ()


def _validate_join(values, inputs, _qgis):
    errors = []
    left = _layer(inputs, "left")
    right = _layer(inputs, "right")
    left_field = _field(left, str(values.get("left_field") or ""))
    right_field = _field(right, str(values.get("right_field") or ""))
    if str(values.get("left_field") or "") in RESERVED_LINEAGE_FIELDS or str(values.get("right_field") or "") in RESERVED_LINEAGE_FIELDS:
        errors.append("JOIN keys cannot use reserved lineage fields.")
    if left is not None and left_field is None:
        errors.append("JOIN left_field does not exist on the Left / Target input.")
    if right is not None and right_field is None:
        errors.append("JOIN right_field does not exist on the Right / Join input.")
    if left_field is not None and right_field is not None and _field_type(left_field) != _field_type(right_field):
        errors.append("JOIN fields must have compatible types; explicit field conversion is required.")
    right_fields = _list_value(values.get("right_fields"))
    if right is not None:
        missing = [name for name in right_fields if _field(right, name) is None]
        if missing:
            errors.append("JOIN right_fields do not exist: " + ", ".join(missing))
        reserved = [name for name in right_fields if name in _RESERVED_FIELDS]
        if reserved:
            errors.append("JOIN right_fields cannot copy reserved lineage fields: " + ", ".join(reserved))
    suffix = str(values.get("collision_suffix") or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", suffix):
        errors.append("JOIN collision_suffix must be a safe field-name suffix.")
    return errors


def _validate_calculate(values, inputs, _qgis):
    errors = []
    layer = _layer(inputs, "input")
    field_name = str(values.get("field_name") or "").strip()
    if field_name in RESERVED_LINEAGE_FIELDS:
        errors.append("CALCULATE FIELD cannot create or update reserved lineage fields.")
    if layer is not None and not field_name:
        errors.append("CALCULATE FIELD field_name is required.")
    if type(values.get("create_field")) is not bool:
        errors.append("CALCULATE FIELD create_field must be boolean.")
    if type(values.get("create_field")) is bool and not values["create_field"] and _field(layer, field_name) is None:
        errors.append("CALCULATE FIELD update mode requires an existing field_name.")
    validator = getattr(_qgis, "validate_expression", None)
    expression = str(values.get("expression") or "").strip()
    if layer is not None and expression and callable(validator):
        try:
            valid, error = validator(expression, layer)
        except TypeError:
            valid, error = validator(expression)
        if not valid:
            errors.append(f"CALCULATE FIELD expression is invalid: {error}")
    return errors


def _validate_field_mapping(values, inputs, _qgis):
    errors = []
    layer = _layer(inputs, "input")
    rows = _mapping_rows(values.get("mappings"))
    if not rows:
        return ("FIELD MAPPING requires at least one mapping.",)
    outputs = set()
    allowed_types = {"string", "integer", "double", "boolean", "date"}
    for row in rows:
        source = str(row.get("source_field") or "").strip()
        output = str(row.get("output_field") or "").strip()
        output_type = str(row.get("output_type") or "string").lower()
        if not source or not output:
            errors.append("FIELD MAPPING rows require source_field and output_field.")
        if layer is not None and _field(layer, source) is None:
            errors.append(f"FIELD MAPPING source field {source!r} does not exist.")
        if source in RESERVED_LINEAGE_FIELDS or output in RESERVED_LINEAGE_FIELDS:
            errors.append("FIELD MAPPING cannot map reserved lineage fields.")
        if not _safe_field_name(output):
            errors.append(f"FIELD MAPPING output field {output!r} is not a safe field name.")
        if output in outputs:
            errors.append(f"FIELD MAPPING output field {output!r} is duplicated.")
        outputs.add(output)
        if output_type not in allowed_types:
            errors.append(f"FIELD MAPPING output_type {output_type!r} is unsupported.")
    return errors


def _validate_fields(values, inputs, title):
    layer = _layer(inputs, "input")
    fields = _list_value(values.get("fields"))
    if not fields:
        return (f"{title} requires at least one field.",)
    reserved = [name for name in fields if name in RESERVED_LINEAGE_FIELDS]
    if reserved:
        return (f"{title} cannot use reserved lineage fields: " + ", ".join(reserved),)
    if layer is not None:
        missing = [name for name in fields if _field(layer, name) is None]
        if missing:
            return (f"{title} fields do not exist: " + ", ".join(missing),)
    return ()


def _validate_keep_fields(values, inputs, _qgis):
    fields = _list_value(values.get("fields"))
    errors = list(_validate_fields(values, inputs, "KEEP FIELDS"))
    if any(name in RESERVED_LINEAGE_FIELDS for name in fields):
        errors.append("KEEP FIELDS preserves reserved lineage fields automatically.")
    return errors


def _validate_rename_field(values, inputs, _qgis):
    layer = _layer(inputs, "input")
    source = str(values.get("source_field") or "").strip()
    target = str(values.get("target_name") or "").strip()
    errors = []
    if not source:
        errors.append("RENAME FIELD source_field is required.")
    if layer is not None and _field(layer, source) is None:
        errors.append(f"RENAME FIELD source field {source!r} does not exist.")
    if source in RESERVED_LINEAGE_FIELDS or target in RESERVED_LINEAGE_FIELDS:
        errors.append("RENAME FIELD cannot rename reserved lineage fields.")
    if not _safe_field_name(target):
        errors.append(f"RENAME FIELD target_name {target!r} is not a safe field name.")
    if layer is not None and target != source and _field(layer, target) is not None:
        errors.append(f"RENAME FIELD target field {target!r} already exists.")
    return errors


def _validate_change_field_type(values, inputs, _qgis):
    layer = _layer(inputs, "input")
    field = str(values.get("field") or "").strip()
    target_type = str(values.get("target_type") or "").lower()
    errors = []
    if not field:
        errors.append("CHANGE FIELD TYPE field is required.")
    if layer is not None and _field(layer, field) is None:
        errors.append(f"CHANGE FIELD TYPE field {field!r} does not exist.")
    if field in RESERVED_LINEAGE_FIELDS:
        errors.append("CHANGE FIELD TYPE cannot convert reserved lineage fields.")
    if target_type not in {"string", "integer", "double", "boolean", "date"}:
        errors.append(f"CHANGE FIELD TYPE target_type {target_type!r} is unsupported.")
    return errors


def _validate_sort(values, inputs, _qgis):
    layer = _layer(inputs, "input")
    rows = _mapping_rows(values.get("sort_fields"))
    if not rows:
        return ("SORT requires at least one sort field.",)
    errors = []
    seen = set()
    for row in rows:
        field = str(row.get("field") or "").strip()
        direction = str(row.get("direction") or "asc").lower()
        if field in RESERVED_LINEAGE_FIELDS:
            errors.append("SORT cannot use reserved lineage fields.")
        if not field:
            errors.append("SORT rows require field.")
        if field in seen:
            errors.append(f"SORT field {field!r} is duplicated.")
        seen.add(field)
        if direction not in {"asc", "desc"}:
            errors.append("SORT direction must be asc or desc.")
        if layer is not None and _field(layer, field) is None:
            errors.append(f"SORT field {field!r} does not exist.")
    return errors


def _validate_aggregate(values, inputs, _qgis):
    layer = _layer(inputs, "input")
    group_fields = _list_value(values.get("group_fields"))
    aggregations = _mapping_rows(values.get("aggregations"))
    errors = []
    reserved_groups = [name for name in group_fields if name in RESERVED_LINEAGE_FIELDS]
    if reserved_groups:
        errors.append("AGGREGATE cannot group by reserved lineage fields.")
    if layer is not None:
        missing = [name for name in group_fields if _field(layer, name) is None]
        if missing:
            errors.append("AGGREGATE group fields do not exist: " + ", ".join(missing))
    output_names = set(group_fields)
    if layer is not None:
        output_names.update(
            field.name()
            for field in layer.fields()
            if field.name() not in RESERVED_LINEAGE_FIELDS
        )
    for row in aggregations:
        field = str(row.get("field") or "").strip()
        function = str(row.get("function") or "").lower()
        if function == "count" and field == "*":
            field = ""
        output = str(row.get("output_field") or "").strip()
        if function not in {"count", "sum", "min", "max", "mean"}:
            errors.append(f"AGGREGATE function {function!r} is unsupported.")
        if function != "count" and not field:
            errors.append(f"AGGREGATE {function} requires a field.")
        if field in RESERVED_LINEAGE_FIELDS:
            errors.append("AGGREGATE cannot aggregate reserved lineage fields.")
        if layer is not None and field and _field(layer, field) is None:
            errors.append(f"AGGREGATE field {field!r} does not exist.")
        if layer is not None and field and function in {"sum", "mean"}:
            source_field = _field(layer, field)
            if source_field is not None and not _is_numeric_field(source_field):
                errors.append(f"AGGREGATE {function} requires a numeric field.")
        if not _safe_field_name(output) or output in RESERVED_LINEAGE_FIELDS:
            errors.append(f"AGGREGATE output field {output!r} is not safe.")
        if output in output_names:
            errors.append(f"AGGREGATE output field {output!r} is duplicated.")
        output_names.add(output)
    if not aggregations:
        errors.append("AGGREGATE requires at least one aggregation.")
    return errors


def _validate_compare_changes(values, inputs, _qgis):
    previous = _layer(inputs, "previous")
    current = _layer(inputs, "current")
    previous_key = str(values.get("previous_key_field") or "").strip()
    current_key = str(values.get("current_key_field") or "").strip()
    compare_fields = _list_value(values.get("compare_fields"))
    errors = []
    previous_definition = _field(previous, previous_key)
    current_definition = _field(current, current_key)
    if previous is not None and previous_definition is None:
        errors.append(f"COMPARE CHANGES previous key field {previous_key!r} does not exist.")
    if current is not None and current_definition is None:
        errors.append(f"COMPARE CHANGES current key field {current_key!r} does not exist.")
    if previous_definition is not None and current_definition is not None and _field_type(previous_definition) != _field_type(current_definition):
        errors.append("COMPARE CHANGES key fields must have compatible types.")
    if previous is not None and current is not None:
        if values.get("compare_all", True):
            compare_fields = [
                field.name()
                for field in current.fields()
                if field.name() not in RESERVED_LINEAGE_FIELDS
                and field.name() != current_key
            ]
        if not compare_fields:
            errors.append("COMPARE CHANGES requires at least one comparison field.")
        for field in compare_fields:
            if field in RESERVED_LINEAGE_FIELDS:
                errors.append("COMPARE CHANGES cannot compare reserved lineage fields.")
            if _field(previous, field) is None or _field(current, field) is None:
                errors.append(f"COMPARE CHANGES field {field!r} must exist in both inputs.")
            elif _field_type(_field(previous, field)) != _field_type(_field(current, field)):
                errors.append(f"COMPARE CHANGES field {field!r} must have compatible types.")
    if previous_key in RESERVED_LINEAGE_FIELDS or current_key in RESERVED_LINEAGE_FIELDS:
        errors.append("COMPARE CHANGES cannot use reserved lineage fields as keys.")
    return errors


def _validate_spatial_join(values, inputs, qgis):
    errors = list(_spatial_crs_error(inputs, qgis, "SPATIAL JOIN", ("target", "join")))
    join = _layer(inputs, "join")
    fields = _list_value(values.get("fields"))
    if join is not None:
        missing = [name for name in fields if _field(join, name) is None]
        if missing:
            errors.append("SPATIAL JOIN fields do not exist: " + ", ".join(missing))
        reserved = [name for name in fields if name in _RESERVED_FIELDS]
        if reserved:
            errors.append("SPATIAL JOIN fields cannot copy reserved lineage fields: " + ", ".join(reserved))
    suffix = str(values.get("collision_suffix") or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", suffix):
        errors.append("SPATIAL JOIN collision_suffix must be a safe field-name suffix.")
    return errors


def _validate_select_by_location(values, inputs, qgis):
    return _spatial_crs_error(
        inputs, qgis, "SELECT BY LOCATION", ("target", "predicate")
    )


def _validate_clip(values, inputs, qgis):
    return _spatial_crs_error(inputs, qgis, "CLIP", ("input", "overlay"))


def _validate_buffer(values, _inputs, _qgis):
    try:
        if float(values.get("distance")) < 0:
            return ("BUFFER distance must be zero or greater.",)
    except (TypeError, ValueError):
        return ("BUFFER distance must be numeric.",)
    try:
        if int(values.get("segments")) <= 0:
            return ("BUFFER segments must be a positive integer.",)
    except (TypeError, ValueError):
        return ("BUFFER segments must be a positive integer.",)
    return ()


def _validate_dissolve(values, inputs, _qgis):
    fields = _list_value(values.get("fields"))
    dissolve_all = values.get("dissolve_all")
    errors = []
    if type(dissolve_all) is not bool:
        errors.append("DISSOLVE dissolve_all must be boolean.")
    elif not dissolve_all and not fields:
        errors.append("DISSOLVE requires fields unless dissolve_all is enabled.")
    reserved = [name for name in fields if name in RESERVED_LINEAGE_FIELDS]
    if reserved:
        errors.append("DISSOLVE cannot use reserved lineage fields: " + ", ".join(reserved))
    layer = _layer(inputs, "input")
    if layer is not None:
        missing = [name for name in fields if _field(layer, name) is None]
        if missing:
            errors.append("DISSOLVE fields do not exist: " + ", ".join(missing))
    return errors


def _validate_reproject(values, _inputs, qgis):
    validator = getattr(qgis, "validate_crs", None)
    if callable(validator):
        valid, error = validator(str(values.get("target_crs") or ""))
        return () if valid else (f"REPROJECT target_crs is invalid: {error}",)
    return ()


def _validate_raster_reproject(values, _inputs, qgis):
    validator = getattr(qgis, "validate_crs", None)
    if callable(validator):
        valid, error = validator(str(values.get("target_crs") or ""))
        return (
            ()
            if valid
            else (f"Raster - CRS Conversion target_crs is invalid: {error}",)
        )
    return ()


def _validate_raster_convert(_values, _inputs, _qgis):
    # Format is chosen on OUTPUT only; convert just normalizes to GeoTIFF.
    return ()


_RESERVED_FIELDS = frozenset(RESERVED_LINEAGE_FIELDS)


def default_operation_registry() -> OperationRegistry:
    """Return the initial palette, including explicit future-capability entries."""

    return OperationRegistry(
        (
            _single_input(
                OperationKind.FILTER,
                "FILTER",
                "Data",
                "filter",
                parameter_definitions=(_param("expression", required=True),),
                validator=_validate_filter,
                consumed_fields=("expression",),
            ),
            OperationDefinition(
                OperationKind.JOIN,
                "JOIN",
                "Data",
                input_ports=(
                    PortDefinition("left", "Left / Target"),
                    PortDefinition("right", "Right / Join"),
                ),
                parameter_definitions=(
                    _param("left_field", required=True, ui_kind="field", context_port="left"),
                    _param("right_field", required=True, ui_kind="field", context_port="right"),
                    _param("right_fields", "list", default=[], ui_kind="field-list", context_port="right"),
                    _param("join_type", default="left", choices=("left", "inner")),
                    _param("collision_suffix", default="_join"),
                ),
                parameter_schema=("left_field", "right_field", "right_fields", "join_type", "collision_suffix"),
                validator=_validate_join,
                executor_key="join",
                lineage_policy="multi_ancestor",
                consumed_fields=("left_field", "right_field", "right_fields"),
                produced_fields=("right_fields",),
                schema_transform="join",
            ),
            OperationDefinition(
                OperationKind.MERGE,
                "MERGE",
                "Data",
                input_ports=(PortDefinition("input", "Input", min_count=2, max_count=None),),
                validator=_validate_merge,
                executor_key="merge",
                lineage_policy="multi_ancestor",
            ),
            OperationDefinition(
                OperationKind.COMPARE_CHANGES,
                "COMPARE CHANGES",
                "Data",
                input_ports=(
                    PortDefinition("previous", "Previous"),
                    PortDefinition("current", "Current"),
                ),
                parameter_definitions=(
                    _param("previous_key_field", required=True, ui_kind="field", context_port="previous"),
                    _param("current_key_field", required=True, ui_kind="field", context_port="current"),
                    _param("compare_fields", "list", default=[], ui_kind="field-list", context_port="both"),
                    _param("compare_all", "bool", default=True),
                ),
                validator=_validate_compare_changes,
                executor_key="compare_changes",
                lineage_policy="multi_ancestor",
                consumed_fields=("previous_key_field", "current_key_field", "compare_fields"),
                schema_transform="compare",
            ),
            _single_input(
                OperationKind.CALCULATE_FIELD,
                "CALCULATE FIELD",
                "Data",
                "calculate_field",
                parameter_definitions=(
                    _param("field_name", required=True, ui_kind="field"),
                    _param("create_field", "bool", default=True),
                    _param("field_type", default="string", choices=("string", "integer", "double", "boolean", "date")),
                    _param("expression", required=True),
                ),
                validator=_validate_calculate,
                consumed_fields=("expression",),
                produced_fields=("field_name",),
                schema_transform="add_or_update_field",
            ),
            _single_input(
                OperationKind.FIELD_MAPPING,
                "FIELD MAPPING",
                "Fields",
                "field_mapping",
                parameter_definitions=(_param("mappings", "list", required=True, ui_kind="structured", row_fields=("source_field", "output_field", "output_type")),),
                validator=_validate_field_mapping,
                consumed_fields=("mappings",),
                produced_fields=("mappings",),
                schema_transform="map",
            ),
            _single_input(
                OperationKind.KEEP_FIELDS,
                "KEEP FIELDS",
                "Fields",
                "keep_fields",
                parameter_definitions=(_param("fields", "list", required=True, ui_kind="field-list"),),
                validator=_validate_keep_fields,
                consumed_fields=("fields",),
                produced_fields=("fields",),
                schema_transform="project",
            ),
            _single_input(
                OperationKind.RENAME_FIELD,
                "RENAME FIELD",
                "Fields",
                "rename_field",
                parameter_definitions=(
                    _param("source_field", required=True, ui_kind="field"),
                    _param("target_name", required=True),
                ),
                validator=_validate_rename_field,
                consumed_fields=("source_field",),
                produced_fields=("target_name",),
                schema_transform="rename",
            ),
            _single_input(
                OperationKind.CHANGE_FIELD_TYPE,
                "CHANGE FIELD TYPE",
                "Fields",
                "change_field_type",
                parameter_definitions=(
                    _param("field", required=True, ui_kind="field"),
                    _param("target_type", required=True, choices=("string", "integer", "double", "boolean", "date")),
                ),
                validator=_validate_change_field_type,
                consumed_fields=("field",),
                produced_fields=("field",),
                schema_transform="cast",
            ),
            _single_input(
                OperationKind.REMOVE_DUPLICATES,
                "REMOVE DUPLICATES",
                "Data",
                "remove_duplicates",
                parameter_definitions=(_param("fields", "list", required=True, ui_kind="field-list"),),
                validator=lambda values, inputs, _qgis: _validate_fields(values, inputs, "REMOVE DUPLICATES"),
                consumed_fields=("fields",),
                schema_transform="deduplicate",
            ),
            _single_input(
                OperationKind.SORT,
                "SORT",
                "Data",
                "sort",
                parameter_definitions=(_param("sort_fields", "list", required=True, ui_kind="structured", row_fields=("field", "direction")),),
                validator=_validate_sort,
                consumed_fields=("sort_fields",),
                schema_transform="order",
            ),
            _single_input(
                OperationKind.AGGREGATE,
                "AGGREGATE",
                "Data",
                "aggregate",
                parameter_definitions=(
                    _param("group_fields", "list", default=[], ui_kind="field-list"),
                    _param("aggregations", "list", required=True, ui_kind="structured", row_fields=("field", "function", "output_field")),
                ),
                validator=_validate_aggregate,
                consumed_fields=("group_fields", "aggregations"),
                produced_fields=("group_fields", "aggregations"),
                lineage="multi_ancestor",
                schema_transform="aggregate",
            ),
            OperationDefinition(
                OperationKind.SELECT_BY_LOCATION,
                "SELECT BY LOCATION",
                "Spatial",
                input_ports=(
                    PortDefinition("target", "Target"),
                    PortDefinition("predicate", "Predicate Layer"),
                ),
                parameter_definitions=(
                    _param("predicate", default="intersects", choices=("intersects", "within", "contains")),
                ),
                validator=_validate_select_by_location,
                executor_key="select_by_location",
                lineage_policy="preserve_target_lineage",
                consumed_fields=("predicate",),
                schema_transform="filter",
            ),
            OperationDefinition(
                OperationKind.SPATIAL_JOIN,
                "SPATIAL JOIN",
                "Spatial",
                input_ports=(
                    PortDefinition("target", "Target"),
                    PortDefinition("join", "Join Layer"),
                ),
                parameter_definitions=(
                    _param("predicate", default="intersects", choices=("intersects", "within", "contains")),
                    _param("fields", "list", default=[], ui_kind="field-list", context_port="join"),
                    _param("multiple_match_policy", default="all", choices=("all", "first")),
                    _param("collision_suffix", default="_spatial"),
                ),
                parameter_schema=("predicate", "fields", "multiple_match_policy", "collision_suffix"),
                validator=_validate_spatial_join,
                executor_key="spatial_join",
                lineage_policy="multi_ancestor",
                consumed_fields=("predicate", "fields"),
                produced_fields=("fields",),
                schema_transform="join",
            ),
            OperationDefinition(
                OperationKind.CLIP,
                "CLIP",
                "Spatial",
                input_ports=(
                    PortDefinition("input", "Input Layer"),
                    PortDefinition("overlay", "Overlay / Mask"),
                ),
                validator=_validate_clip,
                executor_key="clip",
                lineage_policy="preserve_input_lineage",
                schema_transform="geometry_clip",
            ),
            _single_input(
                OperationKind.BUFFER,
                "BUFFER",
                "Spatial",
                "buffer",
                parameter_definitions=(
                    _param("distance", "float", required=True),
                    _param("unit", default="layer", choices=("layer",)),
                    _param("dissolve", "bool", default=False),
                    _param("segments", "int", default=8),
                ),
                validator=_validate_buffer,
                lineage="derived_geometry",
                schema_transform="geometry_buffer",
            ),
            _single_input(
                OperationKind.DISSOLVE,
                "DISSOLVE",
                "Spatial",
                "dissolve",
                parameter_definitions=(
                    _param("fields", "list", default=[], ui_kind="field-list"),
                    _param("dissolve_all", "bool", default=True),
                ),
                validator=_validate_dissolve,
                lineage="multi_ancestor",
                consumed_fields=("fields",),
                schema_transform="aggregate_geometry",
            ),
            _single_input(
                OperationKind.REPROJECT,
                "REPROJECT",
                "Spatial",
                "reproject",
                parameter_definitions=(_param("target_crs", required=True),),
                validator=_validate_reproject,
                lineage="preserve_source_lineage",
                schema_transform="reproject",
            ),
            _single_input(
                OperationKind.RASTER_REPROJECT,
                "Raster - CRS Conversion",
                "Raster",
                "raster_reproject",
                parameter_definitions=(_param("target_crs", required=True),),
                validator=_validate_raster_reproject,
                lineage="raster_file",
                schema_transform="raster_reproject",
                accepted_data_types=("raster",),
            ),
            _single_input(
                OperationKind.RASTER_CONVERT,
                "Raster - Format Conversion",
                "Raster",
                "raster_convert",
                parameter_definitions=(),
                validator=_validate_raster_convert,
                lineage="raster_file",
                schema_transform="raster_convert",
                accepted_data_types=("raster",),
            ),
            _single_input(
                OperationKind.PASSTHROUGH,
                "PASSTHROUGH",
                "Utility",
                "passthrough",
            ),
        )
    )


DEFAULT_OPERATION_REGISTRY = default_operation_registry()
