"""Derived-layer compatibility, construction and materialisation runtime."""

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
from .freeform_operation_runtime import FreeformOperationRuntime
from .qgis_layer_runtime import QgisLayerRuntime


class DerivedMaterializationRuntime(FreeformOperationRuntime):
    @staticmethod
    def _merge_schema_specs(layer) -> List[FieldSpec]:
        return [
            spec
            for spec in QgisLayerRuntime.field_specs(layer)
            if spec.name not in RESERVED_LINEAGE_FIELDS
        ]

    def preflight_merge_layers(self, parent_layers):
        """Prove two or more append parents are homogeneous without mutation."""

        if len(parent_layers) < 2:
            raise ValueError("MERGE requires at least two vector source layers.")
        if any(not self.is_vector_layer(layer) for layer in parent_layers):
            raise ValueError("MERGE requires vector source layers.")
        crs_values = [layer.crs() for layer in parent_layers]
        if any(not crs.isValid() for crs in crs_values):
            raise ValueError("MERGE sources need valid CRS definitions.")
        first_crs = crs_values[0]
        if hasattr(first_crs, "isEquivalentTo"):
            equivalent = all(first_crs.isEquivalentTo(crs) for crs in crs_values[1:])
        else:
            equivalent = all(crs == first_crs for crs in crs_values[1:])
        if not equivalent:
            raise ValueError(
                "MERGE sources must use equivalent CRS; automatic reprojection is not supported."
            )
        output_wkb = self._compatible_output_wkb(*parent_layers)
        first_schema = self._merge_schema_specs(parent_layers[0])
        if any(self._merge_schema_specs(layer) != first_schema for layer in parent_layers[1:]):
            raise ValueError(
                "MERGE source field schemas/types are incompatible; "
                "exact compatible non-lineage fields are required."
            )
        return output_wkb

    def build_merged_memory_layer(
        self,
        workflow_id: str,
        node_id: str,
        display_name: str,
        parent_layers,
        parent_lineage_ids,
    ):
        """Build a deterministic, non-mutating two-or-more-parent append candidate.

        A parent may be either a registered source or an already-materialized
        operation result.  For derived parents the per-feature lineage fields
        remain authoritative; the parent layer's QGIS binding is never used as
        logical identity.
        """

        if len(parent_layers) < 2 or len(parent_lineage_ids) != len(parent_layers):
            raise ValueError("MERGE requires at least two parent layers and lineage IDs.")
        parent_descriptors = []
        for supplied_lineage, layer in zip(parent_lineage_ids, parent_layers):
            lineage = str(supplied_lineage or "").strip()
            records = self._freeform_feature_records(layer)
            lineage_values = tuple(
                sorted(
                    {
                        str(feature[LINEAGE_FIELD_SOURCE_LAYER] or "").strip()
                        for feature, _source_key, _identity in records
                        if LINEAGE_FIELD_SOURCE_LAYER in feature.fields().names()
                        and str(feature[LINEAGE_FIELD_SOURCE_LAYER] or "").strip()
                    }
                )
            )
            if not lineage_values and not lineage:
                raise ValueError("MERGE parent is missing immutable feature lineage.")
            row_signature = tuple(
                sorted(
                    (
                        identity,
                        tuple(
                            (field.name(), repr(feature[field.name()]))
                            for field in layer.fields()
                            if field.name() not in RESERVED_LINEAGE_FIELDS
                        ),
                    )
                    for feature, _source_key, identity in records
                )
            )
            identity = (tuple([lineage]) if lineage else lineage_values, row_signature)
            parent_descriptors.append((identity, layer, lineage))

        parent_descriptors.sort(key=lambda item: item[0])
        parent_layers = tuple(item[1] for item in parent_descriptors)
        lineages = tuple(item[2] for item in parent_descriptors)
        output_wkb = self.preflight_merge_layers(parent_layers)
        parent_records = [self._freeform_feature_records(layer) for layer in parent_layers]

        memory = QgsVectorLayer(QgsWkbTypes.displayString(output_wkb), display_name, "memory")
        if not memory.isValid():
            raise RuntimeError("QGIS could not create the explicit MERGE candidate layer.")
        memory.setCrs(parent_layers[0].crs())

        source_fields = [
            field
            for field in parent_layers[0].fields()
            if field.name() not in RESERVED_LINEAGE_FIELDS
        ]
        output_fields = [QgsField(field) for field in source_fields]
        for field_name, field_type, length in self._freeform_internal_fields():
            if field_name in {field.name() for field in output_fields}:
                continue
            output_fields.append(QgsField(field_name, field_type, len=length))
        if not memory.dataProvider().addAttributes(output_fields):
            raise RuntimeError("Could not create the explicit MERGE candidate schema.")
        memory.updateFields()

        provider = memory.dataProvider()
        total = 0
        batch = []
        for layer, lineage, records in zip(parent_layers, lineages, parent_records):
            features = sorted(records, key=lambda item: item[2])
            for source_feature, source_key, _identity in features:
                if not source_key:
                    raise ValueError(
                        f"Feature {source_feature.id()} in '{layer.name()}' has no stable "
                        f"{LINEAGE_FIELD_SOURCE_KEY}."
                    )
                feature = QgsFeature(memory.fields())
                for field in source_fields:
                    feature[field.name()] = source_feature[field.name()]
                feature[LINEAGE_FIELD_WORKFLOW] = workflow_id
                feature[LINEAGE_FIELD_WORKFLOW_EXPLICIT] = workflow_id
                output_lineage = (
                    lineage
                    or self._freeform_feature_value(
                        source_feature, LINEAGE_FIELD_SOURCE_LAYER
                    )
                )
                if not output_lineage:
                    raise ValueError(
                        "MERGE feature is missing immutable source lineage."
                    )
                feature[LINEAGE_FIELD_SOURCE_LAYER] = output_lineage
                feature[LINEAGE_FIELD_SOURCE_KEY] = source_key
                feature[LINEAGE_FIELD_ANCESTORS] = encode_feature_ancestors(
                    self._freeform_feature_ancestors(
                        source_feature,
                        output_lineage,
                        source_key,
                    )
                )
                if source_feature.hasGeometry():
                    geometry = QgsGeometry(source_feature.geometry())
                    if QgsWkbTypes.isMultiType(output_wkb) and not geometry.isMultipart():
                        if not geometry.convertToMultiType():
                            raise RuntimeError(
                                f"Could not promote source feature {source_feature.id()} "
                                "to the compatible MERGE geometry type."
                            )
                    feature.setGeometry(geometry)
                batch.append(feature)
                if len(batch) >= 1000:
                    result = provider.addFeatures(batch)
                    if not (result[0] if isinstance(result, tuple) else bool(result)):
                        raise RuntimeError("Could not add a feature batch to the explicit MERGE candidate.")
                    total += len(batch)
                    batch = []

        if batch:
            result = provider.addFeatures(batch)
            if not (result[0] if isinstance(result, tuple) else bool(result)):
                raise RuntimeError("Could not add the final feature batch to the explicit MERGE candidate.")
            total += len(batch)
        memory.updateExtents()
        return memory, total

    @staticmethod
    def _compatible_output_wkb(*layers):
        if len(layers) < 2:
            raise ValueError("At least two layers are required for compatibility checks.")
        first_wkb = layers[0].wkbType()
        for layer in layers[1:]:
            wkb = layer.wkbType()
            if QgsWkbTypes.geometryType(first_wkb) != QgsWkbTypes.geometryType(wkb):
                raise ValueError(
                    "Source geometry families are incompatible: "
                    f"{QgsWkbTypes.displayString(first_wkb)} vs {QgsWkbTypes.displayString(wkb)}."
                )
            if QgsWkbTypes.singleType(first_wkb) != QgsWkbTypes.singleType(wkb):
                raise ValueError(
                    "Source geometry types are incompatible: "
                    f"{QgsWkbTypes.displayString(first_wkb)} vs {QgsWkbTypes.displayString(wkb)}."
                )
            if QgsWkbTypes.hasZ(first_wkb) != QgsWkbTypes.hasZ(wkb):
                raise ValueError("Source geometry Z dimensions do not match.")
            if QgsWkbTypes.hasM(first_wkb) != QgsWkbTypes.hasM(wkb):
                raise ValueError("Source geometry M dimensions do not match.")

        if any(QgsWkbTypes.isMultiType(layer.wkbType()) for layer in layers):
            output_wkb = QgsWkbTypes.multiType(QgsWkbTypes.singleType(first_wkb))
            if QgsWkbTypes.hasZ(first_wkb):
                output_wkb = QgsWkbTypes.addZ(output_wkb)
            if QgsWkbTypes.hasM(first_wkb):
                output_wkb = QgsWkbTypes.addM(output_wkb)
            return output_wkb
        return first_wkb

    def preflight_derived_schema(self, left_layer, right_layer, mapping) -> DerivedSchemaPlan:
        self._compatible_output_wkb(left_layer, right_layer)
        if not left_layer.crs().isValid() or not right_layer.crs().isValid():
            raise ValueError("Both source layers need a valid CRS before propagation.")
        return build_derived_schema_plan(
            self.field_specs(left_layer),
            self.field_specs(right_layer),
            mapping.left_field,
            mapping.right_field,
        )

    @staticmethod
    def _source_qgs_field(layer, field_name):
        index = layer.fields().indexOf(field_name)
        if index < 0:
            return None
        return layer.fields().at(index)

    def _derived_qgs_fields(self, left_layer, right_layer, plan: DerivedSchemaPlan):
        fields = []
        for derived in plan.fields:
            source_field = None
            if derived.left_name:
                source_field = self._source_qgs_field(left_layer, derived.left_name)
            if source_field is None and derived.right_name:
                source_field = self._source_qgs_field(right_layer, derived.right_name)
            if source_field is None:
                raise RuntimeError(f"Could not resolve source definition for field '{derived.name}'.")
            copied = QgsField(source_field)
            copied.setName(derived.name)
            if derived.length:
                copied.setLength(derived.length)
            if derived.precision:
                copied.setPrecision(derived.precision)
            fields.append(copied)

        fields.extend(
            [
                QgsField(LINEAGE_FIELD_WORKFLOW, QVariant.String),
                QgsField(LINEAGE_FIELD_SOURCE_LAYER, QVariant.String),
                QgsField(LINEAGE_FIELD_SOURCE_KEY, QVariant.String, len=36),
            ]
        )
        return fields

    def build_derived_memory_layer(
        self,
        workflow_id: str,
        name: str,
        left_layer,
        right_layer,
        schema_plan: DerivedSchemaPlan,
        source_key_plans: Dict[str, SourceKeyPlan],
    ):
        """Build a fresh in-memory Layer C from preflighted source-key plans."""
        output_wkb = self._compatible_output_wkb(left_layer, right_layer)
        memory = QgsVectorLayer(QgsWkbTypes.displayString(output_wkb), name, "memory")
        if not memory.isValid():
            raise RuntimeError("QGIS could not create the derived in-memory Layer C.")
        memory.setCrs(left_layer.crs())

        provider = memory.dataProvider()
        if not provider.addAttributes(self._derived_qgs_fields(left_layer, right_layer, schema_plan)):
            raise RuntimeError("Could not create the derived Layer C schema.")
        memory.updateFields()

        transform = None
        if right_layer.crs() != left_layer.crs():
            transform = QgsCoordinateTransform(right_layer.crs(), left_layer.crs(), self.project)

        total = 0
        batch = []
        for side, layer in (("left", left_layer), ("right", right_layer)):
            key_plan = source_key_plans.get(layer.id())
            if key_plan is None:
                raise RuntimeError(
                    f"No preflighted source-key plan is available for '{layer.name()}'."
                )
            source_keys = key_plan.all_keys
            source_names = [field.name() for field in layer.fields()]
            for source_feature in layer.getFeatures():
                source_key = source_keys.get(source_feature.id())
                if not source_key:
                    raise RuntimeError(
                        f"No stable source UUID was planned for feature {source_feature.id()} "
                        f"in '{layer.name()}'."
                    )
                attributes = {
                    field_name: source_feature[field_name]
                    for field_name in source_names
                }
                projected = project_attributes(side, attributes, schema_plan)

                feature = QgsFeature(memory.fields())
                for field_name, value in projected.items():
                    feature[field_name] = value
                feature[LINEAGE_FIELD_WORKFLOW] = workflow_id
                feature[LINEAGE_FIELD_SOURCE_LAYER] = layer.id()
                feature[LINEAGE_FIELD_SOURCE_KEY] = source_key

                if source_feature.hasGeometry():
                    geometry = QgsGeometry(source_feature.geometry())
                    if transform is not None and side == "right":
                        status = geometry.transform(transform)
                        if status != 0:
                            raise RuntimeError(
                                f"Could not transform geometry for source feature {source_feature.id()}."
                            )
                    if QgsWkbTypes.isMultiType(output_wkb) and not geometry.isMultipart():
                        if not geometry.convertToMultiType():
                            raise RuntimeError(
                                f"Could not promote geometry for source feature {source_feature.id()} to multipart."
                            )
                    feature.setGeometry(geometry)

                batch.append(feature)
                if len(batch) >= 1000:
                    result = provider.addFeatures(batch)
                    ok = result[0] if isinstance(result, tuple) else bool(result)
                    if not ok:
                        raise RuntimeError("Could not add a feature batch to derived Layer C.")
                    total += len(batch)
                    batch = []

        if batch:
            result = provider.addFeatures(batch)
            ok = result[0] if isinstance(result, tuple) else bool(result)
            if not ok:
                raise RuntimeError("Could not add the final feature batch to derived Layer C.")
            total += len(batch)

        memory.updateExtents()
        return memory, total

    def replace_project_derived_layer(self, old_layer_id: Optional[str], new_layer):
        """Swap Layer C in the project only; never delete an external data source."""
        added = self.project.addMapLayer(new_layer)
        if added is None:
            raise RuntimeError("Could not add the rebuilt Layer C to the QGIS project.")
        if old_layer_id and old_layer_id != new_layer.id() and self.project.mapLayer(old_layer_id):
            self.project.removeMapLayer(old_layer_id)
        self.project.setDirty(True)
        return new_layer

    def replace_graph_node_layer(self, old_layer_id: Optional[str], new_layer):
        """Install one explicit node binding without touching workflow Layer C."""

        layer_id = new_layer.id() if hasattr(new_layer, "id") else None
        if layer_id and self.project.mapLayer(layer_id) is not None:
            # Already present (e.g. legacy path); still swap out the previous binding.
            if old_layer_id and old_layer_id != layer_id and self.project.mapLayer(old_layer_id):
                self.project.removeMapLayer(old_layer_id)
            self.project.setDirty(True)
            return new_layer
        added = self.project.addMapLayer(new_layer)
        if added is None:
            raise RuntimeError("Could not add the explicit derived layer to the QGIS project.")
        if old_layer_id and old_layer_id != new_layer.id() and self.project.mapLayer(old_layer_id):
            self.project.removeMapLayer(old_layer_id)
        self.project.setDirty(True)
        return new_layer

    def restore_graph_node_layer(self, old_layer, new_layer_id: Optional[str] = None):
        """Restore a previously bound explicit layer after a failed run."""

        if new_layer_id and self.project.mapLayer(new_layer_id):
            self.project.removeMapLayer(new_layer_id)
        if old_layer is not None and self.project.mapLayer(old_layer.id()) is None:
            self.project.addMapLayer(old_layer)
        self.project.setDirty(True)
