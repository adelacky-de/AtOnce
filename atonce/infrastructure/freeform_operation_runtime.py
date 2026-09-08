"""Non-spatial free-form operation adapters."""

import json
import re
from functools import cmp_to_key
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from qgis.PyQt.QtCore import QDate, Qt, QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsExpression,
    QgsExpressionContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsProject,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.export_snapshot import choose_gpkg_fid_column
from ..core.kml_delivery import plan_kml_crs
from ..core.kmz_delivery import stage_kmz_from_kml
from ..core.delivery_artifacts import (
    SHAPEFILE_CORE_SUFFIXES,
    existing_shapefile_artifacts,
    validate_shapefile_field_names,
)
from ..core.lineage import (
    LINEAGE_FIELD_ANCESTORS,
    LINEAGE_FIELD_EXPORT,
    LINEAGE_FIELD_REVISION,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_WORKFLOW,
    LINEAGE_FIELD_WORKFLOW_EXPLICIT,
    RESERVED_LINEAGE_FIELDS,
    SourceKeyPlan,
    canonical_feature_ancestors,
    derived_feature_identity,
    encode_feature_ancestors,
    feature_ancestors,
    plan_source_keys,
)
from ..core.propagation import (
    DerivedSchemaPlan,
    FieldSpec,
    build_derived_schema_plan,
    project_attributes,
)
from ..models.workflow import LayerRef, SOURCE_ROLE


class FreeformOperationRuntime:
    def __init__(self, project, layer_runtime, source_identity):
        self.project = project
        self.layer_runtime = layer_runtime
        self.source_identity = source_identity

    # These narrow bridges keep the moved adapters independent of QgisGateway.
    def is_vector_layer(self, layer):
        return self.layer_runtime.is_vector_layer(layer)

    def field_names(self, layer):
        return self.layer_runtime.field_names(layer)

    def field_specs(self, layer):
        return self.layer_runtime.field_specs(layer)

    def has_field(self, layer, field_name):
        return self.layer_runtime.has_field(layer, field_name)

    def validate_expression(self, expression_text, layer=None):
        return self.layer_runtime.validate_expression(expression_text, layer)

    def require_equivalent_crs(self, layers, operation):
        return self.layer_runtime.require_equivalent_crs(layers, operation)

    def preflight_existing_source_keys(self, layer):
        return self.source_identity.preflight_existing_source_keys(layer)

    def build_filtered_memory_layer(
        self,
        workflow_id: str,
        node_id: str,
        display_name: str,
        source_layer,
        source_lineage_id: str,
        expression: str,
    ):
        """Build a complete, non-mutating explicit FILTER candidate."""

        if not self.is_vector_layer(source_layer):
            raise ValueError("An explicit FILTER source must be a vector layer.")
        valid, error = self.validate_expression(expression, source_layer)
        if not valid:
            raise ValueError(f"Invalid FILTER expression: {error}")
        source_records = self._freeform_feature_records(source_layer)

        output_wkb = source_layer.wkbType()
        memory = QgsVectorLayer(QgsWkbTypes.displayString(output_wkb), display_name, "memory")
        if not memory.isValid():
            raise RuntimeError("QGIS could not create the explicit FILTER candidate layer.")
        memory.setCrs(source_layer.crs())
        source_names = [field.name() for field in source_layer.fields()]
        output_fields = [QgsField(field) for field in source_layer.fields()]
        for field_name, field_type, length in (
            (LINEAGE_FIELD_WORKFLOW, QVariant.String, 0),
            (LINEAGE_FIELD_WORKFLOW_EXPLICIT, QVariant.String, 0),
            (LINEAGE_FIELD_SOURCE_LAYER, QVariant.String, 0),
            (LINEAGE_FIELD_SOURCE_KEY, QVariant.String, 36),
            (LINEAGE_FIELD_ANCESTORS, QVariant.String, 0),
        ):
            if field_name not in source_names:
                output_fields.append(QgsField(field_name, field_type, len=length))
        if not memory.dataProvider().addAttributes(output_fields):
            raise RuntimeError("Could not create the explicit FILTER candidate schema.")
        memory.updateFields()

        expression_object = QgsExpression(expression)
        context = QgsExpressionContext()
        context.setFields(source_layer.fields())
        if not expression_object.prepare(context):
            raise ValueError(
                "Invalid FILTER expression: "
                + (expression_object.evalErrorString() or "expression could not be prepared")
            )

        features = []
        for source_feature, source_key, _identity in source_records:
            context.setFeature(source_feature)
            result = expression_object.evaluate(context)
            if expression_object.hasEvalError():
                raise ValueError(
                    "FILTER evaluation failed: " + expression_object.evalErrorString()
                )
            if not bool(result):
                continue
            if not source_key:
                raise ValueError(
                    f"Feature {source_feature.id()} has no stable {LINEAGE_FIELD_SOURCE_KEY}."
                )
            feature = QgsFeature(memory.fields())
            for field_name in source_names:
                feature[field_name] = source_feature[field_name]
            feature[LINEAGE_FIELD_WORKFLOW] = workflow_id
            feature[LINEAGE_FIELD_WORKFLOW_EXPLICIT] = workflow_id
            if source_lineage_id:
                feature[LINEAGE_FIELD_SOURCE_LAYER] = source_lineage_id
            else:
                feature[LINEAGE_FIELD_SOURCE_LAYER] = source_feature[LINEAGE_FIELD_SOURCE_LAYER]
            feature[LINEAGE_FIELD_SOURCE_KEY] = source_key
            feature[LINEAGE_FIELD_ANCESTORS] = encode_feature_ancestors(
                ((feature[LINEAGE_FIELD_SOURCE_LAYER], source_key),)
            )
            if source_feature.hasGeometry():
                feature.setGeometry(QgsGeometry(source_feature.geometry()))
            features.append(feature)

        if features:
            result = memory.dataProvider().addFeatures(features)
            if not (result[0] if isinstance(result, tuple) else bool(result)):
                raise RuntimeError("Could not add features to the explicit FILTER candidate.")
        memory.updateExtents()
        return memory, len(features)

    def build_passthrough_memory_layer(
        self,
        workflow_id: str,
        node_id: str,
        display_name: str,
        source_layer,
        source_lineage_id: str = "",
    ):
        """Build an explicit no-op operation boundary without source mutation."""

        return self.build_filtered_memory_layer(
            workflow_id,
            node_id,
            display_name,
            source_layer,
            source_lineage_id,
            "TRUE",
        )

    @staticmethod
    def _freeform_internal_fields():
        return (
            (LINEAGE_FIELD_WORKFLOW, QVariant.String, 0),
            (LINEAGE_FIELD_WORKFLOW_EXPLICIT, QVariant.String, 0),
            (LINEAGE_FIELD_SOURCE_LAYER, QVariant.String, 0),
            (LINEAGE_FIELD_SOURCE_KEY, QVariant.String, 36),
            (LINEAGE_FIELD_ANCESTORS, QVariant.String, 0),
        )

    def _freeform_memory_layer(self, source_layer, display_name, wkb_type=None, extra_fields=()):
        output_wkb = source_layer.wkbType() if wkb_type is None else wkb_type
        memory = QgsVectorLayer(QgsWkbTypes.displayString(output_wkb), display_name, "memory")
        if not memory.isValid():
            raise RuntimeError("QGIS could not create the free-form candidate layer.")
        memory.setCrs(source_layer.crs())
        fields = [QgsField(field) for field in source_layer.fields()]
        names = {field.name() for field in fields}
        for name, field_type, length in (*self._freeform_internal_fields(), *extra_fields):
            if name not in names:
                fields.append(QgsField(name, field_type, len=length))
                names.add(name)
        if not memory.dataProvider().addAttributes(fields):
            raise RuntimeError("Could not create the free-form candidate schema.")
        memory.updateFields()
        return memory

    def _freeform_memory_layer_with_fields(
        self, source_layer, display_name, fields, wkb_type=None, crs=None
    ):
        """Create a candidate with an explicit user schema plus lineage fields."""

        output_wkb = source_layer.wkbType() if wkb_type is None else wkb_type
        memory = QgsVectorLayer(QgsWkbTypes.displayString(output_wkb), display_name, "memory")
        if not memory.isValid():
            raise RuntimeError("QGIS could not create the free-form candidate layer.")
        memory.setCrs(source_layer.crs() if crs is None else crs)
        output_fields = []
        names = set()
        for field in fields:
            candidate = QgsField(field) if isinstance(field, QgsField) else QgsField(*field)
            if candidate.name() in RESERVED_LINEAGE_FIELDS or candidate.name() in names:
                continue
            output_fields.append(candidate)
            names.add(candidate.name())
        for name, field_type, length in self._freeform_internal_fields():
            if name not in names:
                output_fields.append(QgsField(name, field_type, len=length))
                names.add(name)
        if not memory.dataProvider().addAttributes(output_fields):
            raise RuntimeError("Could not create the free-form candidate schema.")
        memory.updateFields()
        return memory

    @staticmethod
    def _freeform_field_type_code(type_name):
        values = {
            "string": QVariant.String,
            "integer": QVariant.Int,
            "double": QVariant.Double,
            "boolean": QVariant.Bool,
            "date": QVariant.Date,
        }
        try:
            return values[str(type_name or "").strip().lower()]
        except KeyError as exc:
            raise ValueError(
                "Supported field types are string, integer, double, boolean and date."
            ) from exc

    @staticmethod
    def _freeform_is_null(value):
        if value is None:
            return True
        try:
            return bool(value.isNull())
        except AttributeError:
            return False

    @classmethod
    def _freeform_convert_value(cls, value, target_type, field_name="value"):
        if cls._freeform_is_null(value):
            return None
        target = str(target_type or "string").lower()
        try:
            if target == "string":
                return str(value)
            if target == "integer":
                if isinstance(value, bool):
                    raise ValueError
                number = float(value)
                if not number.is_integer():
                    raise ValueError
                return int(number)
            if target == "double":
                return float(value)
            if target == "boolean":
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)) and value in (0, 1):
                    return bool(value)
                normalized = str(value).strip().lower()
                if normalized in {"true", "1", "yes", "y"}:
                    return True
                if normalized in {"false", "0", "no", "n"}:
                    return False
                raise ValueError
            if target == "date":
                if isinstance(value, QDate):
                    return value
                date = QDate.fromString(str(value), Qt.ISODate)
                if not date.isValid():
                    raise ValueError
                return date
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"Could not convert {field_name!r} value {value!r} to {target}."
            ) from exc
        raise ValueError(f"Unsupported field type {target!r}.")

    @staticmethod
    def _freeform_field_copy(field, name=None, type_code=None):
        if name is None and type_code is None:
            return QgsField(field)
        return QgsField(
            str(name or field.name()),
            field.type() if type_code is None else type_code,
            field.typeName(),
            field.length(),
            field.precision(),
        )

    @staticmethod
    def _freeform_unique_ancestors(*groups):
        return canonical_feature_ancestors(
            item for group in groups for item in (group or ())
        )

    @staticmethod
    def _freeform_safe_field_name(name):
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name or "")))

    def _freeform_features_with_keys(self, layer):
        return [
            (feature, source_key)
            for feature, source_key, _identity in self._freeform_feature_records(layer)
        ]

    def _freeform_feature_records(self, layer):
        """Return feature/key/identity tuples without raw-source checks on derived data."""

        names = set(layer.fields().names()) if hasattr(layer, "fields") else set()
        features = list(layer.getFeatures())
        if LINEAGE_FIELD_ANCESTORS in names:
            records = []
            for feature in features:
                source_key = str(
                    self._freeform_feature_value(feature, LINEAGE_FIELD_SOURCE_KEY) or ""
                ).strip()
                ancestors = self._freeform_feature_ancestors(feature, "", source_key)
                if not source_key and not ancestors:
                    raise ValueError("Derived feature is missing canonical AtOnce ancestry.")
                values = {
                    name: self._freeform_feature_value(feature, name)
                    for name in names
                }
                records.append(
                    (
                        feature,
                        source_key,
                        derived_feature_identity(values, "", source_key),
                    )
                )
            return sorted(records, key=lambda item: item[2])

        keys = self.preflight_existing_source_keys(layer)
        return sorted([
            (
                feature,
                keys.get(feature.id()),
                derived_feature_identity(
                    {
                        LINEAGE_FIELD_SOURCE_LAYER: "",
                        LINEAGE_FIELD_SOURCE_KEY: keys.get(feature.id()),
                    }
                ),
            )
            for feature in features
        ], key=lambda item: item[2])

    def _freeform_feature_order_key(self, feature, source_lineage_id="", source_key=""):
        values = {
            name: self._freeform_feature_value(feature, name)
            for name in feature.fields().names()
        }
        return derived_feature_identity(values, source_lineage_id, source_key)

    @staticmethod
    def _freeform_feature_value(feature, name):
        return feature[name] if name in feature.fields().names() else None

    def _freeform_lineage(self, workflow_id, feature, source_lineage_id, source_key):
        lineage = source_lineage_id or self._freeform_feature_value(feature, LINEAGE_FIELD_SOURCE_LAYER)
        key = source_key or self._freeform_feature_value(feature, LINEAGE_FIELD_SOURCE_KEY)
        if not lineage or not key:
            raise ValueError("Free-form candidate input is missing exact source feature lineage.")
        values = {
            name: self._freeform_feature_value(feature, name)
            for name in feature.fields().names()
        }
        ancestors = feature_ancestors(values, lineage, key)
        return {
            LINEAGE_FIELD_WORKFLOW: workflow_id,
            LINEAGE_FIELD_WORKFLOW_EXPLICIT: workflow_id,
            LINEAGE_FIELD_SOURCE_LAYER: lineage,
            LINEAGE_FIELD_SOURCE_KEY: key,
            LINEAGE_FIELD_ANCESTORS: encode_feature_ancestors(ancestors),
        }

    def _freeform_feature_ancestors(
        self, feature, source_lineage_id="", source_key=""
    ):
        values = {
            name: self._freeform_feature_value(feature, name)
            for name in feature.fields().names()
        }
        return feature_ancestors(values, source_lineage_id, source_key)

    def _freeform_copy_feature(
        self,
        memory,
        source_feature,
        workflow_id,
        source_lineage_id="",
        source_key="",
        geometry=None,
        values=None,
        ancestors=None,
    ):
        feature = QgsFeature(memory.fields())
        source_names = set(source_feature.fields().names())
        for field in memory.fields():
            name = field.name()
            if values and name in values:
                feature[name] = values[name]
            elif name in source_names:
                feature[name] = source_feature[name]
        lineage_values = self._freeform_lineage(
            workflow_id,
            source_feature,
            source_lineage_id,
            source_key,
        )
        if ancestors is not None:
            lineage_values[LINEAGE_FIELD_ANCESTORS] = encode_feature_ancestors(ancestors)
        for name, value in lineage_values.items():
            feature[name] = value
        if geometry is not None:
            feature.setGeometry(QgsGeometry(geometry))
        elif source_feature.hasGeometry():
            feature.setGeometry(QgsGeometry(source_feature.geometry()))
        return feature

    @staticmethod
    def _freeform_add_features(memory, features):
        if not features:
            memory.updateExtents()
            return 0
        result = memory.dataProvider().addFeatures(features)
        if not (result[0] if isinstance(result, tuple) else bool(result)):
            raise RuntimeError("Could not add features to the free-form candidate.")
        memory.updateExtents()
        return len(features)

    def build_field_mapping_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        rows = list(parameters.get("mappings") or [])
        if not rows:
            raise ValueError("FIELD MAPPING requires at least one mapping.")
        output_fields = []
        for row in rows:
            source_name = str(row.get("source_field") or "").strip()
            output_name = str(row.get("output_field") or "").strip()
            output_type = str(row.get("output_type") or "string").lower()
            source_field = parent_layer.fields().field(source_name)
            if source_field is None:
                raise ValueError(f"FIELD MAPPING source field {source_name!r} does not exist.")
            if output_name in RESERVED_LINEAGE_FIELDS or not self._freeform_safe_field_name(output_name):
                raise ValueError("FIELD MAPPING cannot create reserved lineage fields.")
            if not output_name or output_name in {field.name() for field in output_fields}:
                raise ValueError(f"FIELD MAPPING output field {output_name!r} is duplicated or empty.")
            output_fields.append(
                QgsField(output_name, self._freeform_field_type_code(output_type))
            )
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, output_fields)
        features = []
        for source_feature, source_key in self._freeform_features_with_keys(parent_layer):
            values = {}
            for row in rows:
                source_name = str(row.get("source_field") or "").strip()
                output_name = str(row.get("output_field") or "").strip()
                output_type = str(row.get("output_type") or "string").lower()
                values[output_name] = self._freeform_convert_value(
                    source_feature[source_name], output_type, source_name
                )
            features.append(
                self._freeform_copy_feature(
                    memory,
                    source_feature,
                    workflow_id,
                    source_lineage_id,
                    source_key,
                    values=values,
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_keep_fields_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        names = [str(item).strip() for item in (parameters.get("fields") or []) if str(item).strip()]
        if not names:
            raise ValueError("KEEP FIELDS requires at least one field.")
        missing = [name for name in names if parent_layer.fields().indexOf(name) < 0]
        if missing:
            raise ValueError("KEEP FIELDS field(s) missing: " + ", ".join(missing))
        if any(name in RESERVED_LINEAGE_FIELDS for name in names):
            raise ValueError("KEEP FIELDS preserves reserved lineage fields automatically.")
        fields = [parent_layer.fields().field(name) for name in names]
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, fields)
        features = [
            self._freeform_copy_feature(
                memory,
                feature,
                workflow_id,
                source_lineage_id,
                source_key,
            )
            for feature, source_key in self._freeform_features_with_keys(parent_layer)
        ]
        return memory, self._freeform_add_features(memory, features)

    def build_rename_field_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        source_name = str(parameters.get("source_field") or "").strip()
        target_name = str(parameters.get("target_name") or "").strip()
        source_field = parent_layer.fields().field(source_name)
        if source_field is None:
            raise ValueError(f"RENAME FIELD source field {source_name!r} does not exist.")
        if source_name in RESERVED_LINEAGE_FIELDS or target_name in RESERVED_LINEAGE_FIELDS:
            raise ValueError("RENAME FIELD cannot rename reserved lineage fields.")
        if not self._freeform_safe_field_name(target_name):
            raise ValueError(f"RENAME FIELD target field {target_name!r} is not safe.")
        if not target_name or (
            target_name != source_name and parent_layer.fields().indexOf(target_name) >= 0
        ):
            raise ValueError(f"RENAME FIELD target field {target_name!r} already exists or is empty.")
        fields = [
            self._freeform_field_copy(field, target_name if field.name() == source_name else None)
            for field in parent_layer.fields()
            if field.name() not in RESERVED_LINEAGE_FIELDS
        ]
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, fields)
        features = []
        for source_feature, source_key in self._freeform_features_with_keys(parent_layer):
            features.append(
                self._freeform_copy_feature(
                    memory,
                    source_feature,
                    workflow_id,
                    source_lineage_id,
                    source_key,
                    values={target_name: source_feature[source_name]},
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_change_field_type_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        field_name = str(parameters.get("field") or "").strip()
        target_type = str(parameters.get("target_type") or "").lower()
        source_field = parent_layer.fields().field(field_name)
        if source_field is None:
            raise ValueError(f"CHANGE FIELD TYPE field {field_name!r} does not exist.")
        if field_name in RESERVED_LINEAGE_FIELDS:
            raise ValueError("CHANGE FIELD TYPE cannot convert reserved lineage fields.")
        type_code = self._freeform_field_type_code(target_type)
        fields = [
            self._freeform_field_copy(field, type_code=type_code if field.name() == field_name else None)
            for field in parent_layer.fields()
            if field.name() not in RESERVED_LINEAGE_FIELDS
        ]
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, fields)
        features = []
        for source_feature, source_key in self._freeform_features_with_keys(parent_layer):
            value = self._freeform_convert_value(
                source_feature[field_name], target_type, field_name
            )
            features.append(
                self._freeform_copy_feature(
                    memory,
                    source_feature,
                    workflow_id,
                    source_lineage_id,
                    source_key,
                    values={field_name: value},
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_sort_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        rows = list(parameters.get("sort_fields") or [])
        if not rows:
            raise ValueError("SORT requires at least one sort field.")
        if any(str(row.get("field") or "").strip() in RESERVED_LINEAGE_FIELDS for row in rows):
            raise ValueError("SORT cannot use reserved lineage fields.")
        missing = [
            str(row.get("field") or "").strip()
            for row in rows
            if parent_layer.fields().indexOf(str(row.get("field") or "").strip()) < 0
        ]
        if missing:
            raise ValueError("SORT field(s) missing: " + ", ".join(missing))
        keyed = self._freeform_features_with_keys(parent_layer)

        def compare(left, right):
            left_feature, left_key = left
            right_feature, right_key = right
            for row in rows:
                name = str(row.get("field") or "").strip()
                direction = str(row.get("direction") or "asc").lower()
                left_value = left_feature[name]
                right_value = right_feature[name]
                left_null = self._freeform_is_null(left_value)
                right_null = self._freeform_is_null(right_value)
                if left_null != right_null:
                    result = -1 if left_null else 1
                elif left_null:
                    result = 0
                else:
                    try:
                        result = -1 if left_value < right_value else 1 if left_value > right_value else 0
                    except TypeError:
                        left_text, right_text = str(left_value), str(right_value)
                        result = -1 if left_text < right_text else 1 if left_text > right_text else 0
                if result:
                    return -result if direction == "desc" else result
            left_identity = self._freeform_feature_order_key(
                left_feature, source_lineage_id, left_key
            )
            right_identity = self._freeform_feature_order_key(
                right_feature, source_lineage_id, right_key
            )
            return -1 if left_identity < right_identity else 1 if left_identity > right_identity else 0

        keyed.sort(key=cmp_to_key(compare))
        memory = self._freeform_memory_layer_with_fields(
            parent_layer,
            display_name,
            [field for field in parent_layer.fields() if field.name() not in RESERVED_LINEAGE_FIELDS],
        )
        features = [
            self._freeform_copy_feature(
                memory, feature, workflow_id, source_lineage_id, source_key
            )
            for feature, source_key in keyed
        ]
        return memory, self._freeform_add_features(memory, features)

    def build_aggregate_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        group_fields = [str(item).strip() for item in (parameters.get("group_fields") or []) if str(item).strip()]
        aggregations = list(parameters.get("aggregations") or [])
        if any(name in RESERVED_LINEAGE_FIELDS for name in group_fields):
            raise ValueError("AGGREGATE cannot group by reserved lineage fields.")
        if any(str(row.get("field") or "").strip() in RESERVED_LINEAGE_FIELDS for row in aggregations):
            raise ValueError("AGGREGATE cannot aggregate reserved lineage fields.")
        missing = [
            name for name in group_fields
            if parent_layer.fields().indexOf(name) < 0
        ]
        missing.extend(
            str(row.get("field") or "").strip()
            for row in aggregations
            if str(row.get("field") or "").strip() not in {"", "*"}
            and parent_layer.fields().indexOf(str(row.get("field") or "").strip()) < 0
        )
        if missing:
            raise ValueError("AGGREGATE field(s) missing: " + ", ".join(sorted(set(missing))))
        if not aggregations:
            raise ValueError("AGGREGATE requires at least one aggregation.")
        groups = {}
        records = sorted(
            self._freeform_feature_records(parent_layer), key=lambda item: item[2]
        )
        for feature, source_key, _identity in records:
            key = tuple(
                (None if self._freeform_is_null(feature[name]) else feature[name])
                for name in group_fields
            ) or ("__all__",)
            groups.setdefault(key, []).append((feature, source_key))
        output_fields = [parent_layer.fields().field(name) for name in group_fields]
        for row in aggregations:
            function = str(row.get("function") or "").lower()
            field_name = str(row.get("field") or "").strip()
            if function == "count" and field_name == "*":
                field_name = ""
            output_name = str(row.get("output_field") or "").strip()
            if function == "count":
                output_type = QVariant.Int
            elif function in {"sum", "mean"}:
                output_type = QVariant.Double
            else:
                source_field = parent_layer.fields().field(field_name)
                output_type = source_field.type() if source_field is not None else QVariant.String
            output_fields.append(QgsField(output_name, output_type))
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, output_fields)
        features = []
        for group_key in sorted(groups, key=lambda value: repr(value)):
            group = groups[group_key]
            first, first_key = group[0]
            values = {
                name: group_key[index]
                for index, name in enumerate(group_fields)
            }
            for row in aggregations:
                function = str(row.get("function") or "").lower()
                field_name = str(row.get("field") or "").strip()
                if function == "count" and field_name == "*":
                    field_name = ""
                if function == "count" and field_name == "*":
                    field_name = ""
                output_name = str(row.get("output_field") or "").strip()
                values_list = [
                    feature[field_name]
                    for feature, _source_key in group
                    if field_name and not self._freeform_is_null(feature[field_name])
                ]
                if function == "count":
                    values[output_name] = len(values_list) if field_name else len(group)
                elif not values_list:
                    values[output_name] = None
                elif function == "sum":
                    values[output_name] = sum(values_list)
                elif function == "mean":
                    values[output_name] = sum(values_list) / len(values_list)
                elif function == "min":
                    values[output_name] = min(values_list)
                elif function == "max":
                    values[output_name] = max(values_list)
                else:
                    raise ValueError(f"AGGREGATE function {function!r} is unsupported.")
            ancestors = self._freeform_unique_ancestors(
                *(
                    self._freeform_feature_ancestors(feature, source_lineage_id, source_key)
                    for feature, source_key in group
                )
            )
            features.append(
                self._freeform_copy_feature(
                    memory, first, workflow_id, source_lineage_id, first_key,
                    values=values, ancestors=ancestors,
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_compare_changes_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layers,
        parent_lineage_ids,
        parameters,
    ):
        if len(parent_layers) != 2:
            raise ValueError("COMPARE CHANGES requires Previous and Current inputs.")
        previous, current = parent_layers
        previous_key = str(parameters.get("previous_key_field") or "").strip()
        current_key = str(parameters.get("current_key_field") or "").strip()
        previous_field = previous.fields().field(previous_key)
        current_field = current.fields().field(current_key)
        if previous_field is None or current_field is None:
            raise ValueError("COMPARE CHANGES key fields must exist on both inputs.")
        if previous_field.type() != current_field.type():
            raise ValueError("COMPARE CHANGES key fields must have compatible types.")
        compare_all = parameters.get("compare_all", True)
        if type(compare_all) is not bool:
            raise ValueError("COMPARE CHANGES compare_all must be a boolean.")
        compare_fields = [str(item).strip() for item in (parameters.get("compare_fields") or []) if str(item).strip()]
        if any(name in RESERVED_LINEAGE_FIELDS for name in compare_fields):
            raise ValueError("COMPARE CHANGES cannot compare reserved lineage fields.")
        if compare_all:
            compare_fields = [
                field.name() for field in current.fields()
                if field.name() not in RESERVED_LINEAGE_FIELDS and field.name() != current_key
            ]
        if not compare_fields:
            raise ValueError("COMPARE CHANGES requires at least one comparison field.")
        if any(
            previous.fields().indexOf(name) < 0 or current.fields().indexOf(name) < 0
            for name in compare_fields
        ):
            raise ValueError("COMPARE CHANGES fields must exist on both inputs.")

        def build_index(layer, field_name):
            result = {}
            records = self._freeform_feature_records(layer)
            for feature, source_key, _identity in records:
                value = feature[field_name]
                key = self._join_key(value)
                if key is None:
                    raise ValueError("COMPARE CHANGES key values cannot be NULL or empty.")
                if key in result:
                    raise ValueError("COMPARE CHANGES key fields must be unique on both inputs.")
                result[key] = (feature, source_key)
            return result

        previous_index = build_index(previous, previous_key)
        current_index = build_index(current, current_key)
        memory = self._freeform_memory_layer_with_fields(
            current,
            display_name,
            [
                QgsField("key", current_field.type()),
                QgsField("change_status", QVariant.String),
                QgsField("changed_fields", QVariant.String),
                QgsField("previous_values", QVariant.String),
                QgsField("current_values", QVariant.String),
            ],
        )
        features = []
        for key in sorted(set(previous_index) | set(current_index), key=lambda item: repr(item)):
            previous_item = previous_index.get(key)
            current_item = current_index.get(key)
            base_item = current_item or previous_item
            base_feature, base_source_key = base_item
            if previous_item is None:
                status = "Added"
                changed = []
            elif current_item is None:
                status = "Removed"
                changed = []
            else:
                changed = [
                    name for name in compare_fields
                    if previous_item[0][name] != current_item[0][name]
                ]
                status = "Modified" if changed else "Unchanged"
            values = {
                "key": current_item[0][current_key] if current_item else previous_item[0][previous_key],
                "change_status": status,
                "changed_fields": ",".join(changed),
                "previous_values": json.dumps(
                    {name: previous_item[0][name] for name in compare_fields}
                    if previous_item else {},
                    sort_keys=True, default=str,
                ),
                "current_values": json.dumps(
                    {name: current_item[0][name] for name in compare_fields}
                    if current_item else {},
                    sort_keys=True, default=str,
                ),
            }
            ancestors = self._freeform_unique_ancestors(
                self._freeform_feature_ancestors(
                    previous_item[0],
                    parent_lineage_ids[0] if previous_item else "",
                    previous_item[1] if previous_item else "",
                ) if previous_item else (),
                self._freeform_feature_ancestors(
                    current_item[0],
                    parent_lineage_ids[1] if current_item else "",
                    current_item[1] if current_item else "",
                ) if current_item else (),
            )
            features.append(
                self._freeform_copy_feature(
                    memory,
                    base_feature,
                    workflow_id,
                    parent_lineage_ids[1] if current_item else parent_lineage_ids[0],
                    base_source_key,
                    values=values, ancestors=ancestors,
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_join_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layers,
        parent_lineage_ids,
        parameters,
    ):
        if len(parent_layers) != 2:
            raise ValueError("JOIN requires Left / Target and Right / Join inputs.")
        left, right = parent_layers
        left_field = str(parameters.get("left_field") or "")
        right_field = str(parameters.get("right_field") or "")
        if left_field in RESERVED_LINEAGE_FIELDS or right_field in RESERVED_LINEAGE_FIELDS:
            raise ValueError("JOIN keys cannot use reserved lineage fields.")
        left_index = left.fields().indexOf(left_field)
        right_index = right.fields().indexOf(right_field)
        if left_index < 0 or right_index < 0:
            raise ValueError("JOIN fields must exist on both named input layers.")
        left_definition = left.fields().at(left_index)
        right_definition = right.fields().at(right_index)
        if left_definition.type() != right_definition.type():
            raise ValueError("JOIN fields must have compatible types; explicit field conversion is required.")
        suffix = str(parameters.get("collision_suffix") or "_join")
        if not suffix or not (suffix[0].isalpha() or suffix[0] == "_") or not all(
            character.isalnum() or character == "_" for character in suffix
        ):
            raise ValueError("JOIN collision_suffix must be a safe field-name suffix.")
        selected = parameters.get("fields") or parameters.get("right_fields")
        selected = list(selected) if selected else [field.name() for field in right.fields()]
        left_names = {field.name() for field in left.fields()}
        right_fields = [
            field for field in right.fields()
            if field.name() in selected
            and field.name() not in RESERVED_LINEAGE_FIELDS
        ]
        extra = []
        output_names = set(left_names)
        right_output_names = {}
        for field in right_fields:
            name = field.name()
            if name in output_names:
                name += suffix
            right_output_names[field.name()] = name
            extra.append((name, field.type(), field.length()))
            output_names.add(name)
        memory = self._freeform_memory_layer(left, display_name, extra_fields=extra)
        right_keys = {}
        right_records = self._freeform_feature_records(right)
        right_key_map = {
            feature.id(): source_key
            for feature, source_key, _identity in right_records
        }
        for feature, _source_key, _identity in right_records:
            key = self._join_key(feature[right_field])
            if key is not None:
                right_keys.setdefault(key, []).append(feature)
        for matches in right_keys.values():
            matches.sort(
                key=lambda feature: self._freeform_feature_order_key(
                    feature,
                    parent_lineage_ids[1] if len(parent_lineage_ids) > 1 else "",
                    right_key_map.get(feature.id()),
                )
            )
        left_records = self._freeform_feature_records(left)
        left_key_map = {
            feature.id(): source_key
            for feature, source_key, _identity in left_records
        }
        join_type = str(parameters.get("join_type") or "left").lower()
        if join_type not in {"left", "inner"}:
            raise ValueError("JOIN join_type must be left or inner.")
        features = []
        left_lineage = str(parent_lineage_ids[0] or "") if parent_lineage_ids else ""
        for left_feature, _left_key, _left_identity in left_records:
            matches = right_keys.get(self._join_key(left_feature[left_field]), [])
            if not matches and join_type == "inner":
                continue
            for right_feature in matches or [None]:
                values = {}
                ancestors = self._freeform_feature_ancestors(
                    left_feature,
                    left_lineage,
                    left_key_map.get(left_feature.id()),
                )
                if right_feature is not None:
                    for field in right_fields:
                        values[right_output_names[field.name()]] = right_feature[field.name()]
                    ancestors = ancestors + self._freeform_feature_ancestors(
                        right_feature,
                        parent_lineage_ids[1] if len(parent_lineage_ids) > 1 else "",
                        right_key_map.get(right_feature.id()),
                    )
                features.append(
                    self._freeform_copy_feature(
                        memory,
                        left_feature,
                        workflow_id,
                        left_lineage,
                        left_key_map.get(left_feature.id()),
                        values=values,
                        ancestors=ancestors,
                    )
                )
        return memory, self._freeform_add_features(memory, features)

    @staticmethod
    def _join_key(value):
        if value is None:
            return None
        try:
            if value.isNull():
                return None
        except AttributeError:
            pass
        if isinstance(value, str):
            return value if value.strip() else None
        try:
            hash(value)
        except TypeError:
            return str(value)
        return value

    def build_calculate_field_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        field_name = str(parameters.get("field_name") or "").strip()
        expression = str(parameters.get("expression") or "").strip()
        if not field_name or not expression:
            raise ValueError("CALCULATE FIELD requires a target field and expression.")
        if field_name in RESERVED_LINEAGE_FIELDS:
            raise ValueError("CALCULATE FIELD cannot create or update reserved lineage fields.")
        create = parameters.get("create_field", True)
        if type(create) is not bool:
            raise ValueError("CALCULATE FIELD create_field must be a boolean.")
        if parent_layer.fields().indexOf(field_name) < 0 and not create:
            raise ValueError(f"CALCULATE FIELD target field {field_name!r} does not exist.")
        field_types = {
            "string": QVariant.String,
            "integer": QVariant.Int,
            "double": QVariant.Double,
            "boolean": QVariant.Bool,
            "date": QVariant.Date,
        }
        field_type_name = str(parameters.get("field_type") or "string").lower()
        if field_type_name not in field_types:
            raise ValueError(
                "CALCULATE FIELD field_type must be one of string, integer, double, "
                "boolean or date."
            )
        target_index = parent_layer.fields().indexOf(field_name)
        output_fields = [
            self._freeform_field_copy(
                field,
                type_code=field_types[field_type_name] if field.name() == field_name else None,
            )
            for field in parent_layer.fields()
            if field.name() not in RESERVED_LINEAGE_FIELDS
        ]
        if target_index < 0:
            output_fields.append(QgsField(field_name, field_types[field_type_name]))
        memory = self._freeform_memory_layer_with_fields(parent_layer, display_name, output_fields)
        expression_object = QgsExpression(expression)
        context = QgsExpressionContext()
        context.setFields(parent_layer.fields())
        if not expression_object.prepare(context):
            raise ValueError("Invalid CALCULATE FIELD expression: " + expression_object.parserErrorString())
        records = self._freeform_feature_records(parent_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in records}
        features = []
        for source_feature, _source_key, _identity in records:
            context.setFeature(source_feature)
            value = expression_object.evaluate(context)
            if expression_object.hasEvalError():
                raise ValueError("CALCULATE FIELD evaluation failed: " + expression_object.evalErrorString())
            value = self._freeform_convert_value(value, field_type_name, field_name)
            features.append(
                self._freeform_copy_feature(
                    memory,
                    source_feature,
                    workflow_id,
                    source_lineage_id,
                    keys.get(source_feature.id()),
                    values={field_name: value},
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_remove_duplicates_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layer,
        source_lineage_id,
        parameters,
    ):
        fields = [str(item) for item in (parameters.get("fields") or []) if str(item)]
        if not fields:
            raise ValueError("REMOVE DUPLICATES requires at least one field.")
        missing = [name for name in fields if parent_layer.fields().indexOf(name) < 0]
        if missing:
            raise ValueError("REMOVE DUPLICATES field(s) missing: " + ", ".join(missing))
        memory = self._freeform_memory_layer(parent_layer, display_name)
        records = self._freeform_feature_records(parent_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in records}
        source_features = [
            feature
            for feature, _source_key, _identity in sorted(
                records, key=lambda item: item[2]
            )
        ]
        seen = set()
        features = []
        for source_feature in source_features:
            key = tuple(str(source_feature[name]) for name in fields)
            if key in seen:
                continue
            seen.add(key)
            features.append(
                self._freeform_copy_feature(
                    memory,
                    source_feature,
                    workflow_id,
                    source_lineage_id,
                    keys.get(source_feature.id()),
                )
            )
        return memory, self._freeform_add_features(memory, features)
