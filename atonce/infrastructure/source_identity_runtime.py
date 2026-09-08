"""Stable source-key preflight and persistence runtime."""

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


@dataclass(frozen=True)
class SourceKeyPreflight:
    layer_id: str
    field_exists: bool
    plan: SourceKeyPlan

    @property
    def writes_required(self) -> bool:
        return (not self.field_exists) or bool(self.plan.assignments)


class SourceIdentityRuntime:
    def __init__(self, layer_runtime):
        self.layer_runtime = layer_runtime

    def preflight_source_keys(self, layer) -> SourceKeyPreflight:
        """Inspect one source without mutating it and plan missing UUIDs."""
        if not self.layer_runtime.is_vector_layer(layer):
            raise ValueError("Stable source keys require a vector layer.")
        if layer.isEditable():
            raise ValueError(
                f"Source layer '{layer.name()}' is currently in an edit session. "
                "Save or roll back those edits before AtOnce propagation."
            )

        field_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
        field_exists = field_index >= 0
        provider = layer.dataProvider()
        capabilities = provider.capabilities()

        if field_exists:
            field = layer.fields().at(field_index)
            if field.type() != QVariant.String:
                raise ValueError(
                    f"Existing {LINEAGE_FIELD_SOURCE_KEY} on '{layer.name()}' must be a text field."
                )
            if 0 < field.length() < 36:
                raise ValueError(
                    f"Existing {LINEAGE_FIELD_SOURCE_KEY} on '{layer.name()}' is too short for UUIDs."
                )
            request = QgsFeatureRequest()
            request.setSubsetOfAttributes([LINEAGE_FIELD_SOURCE_KEY], layer.fields())
            request.setFlags(QgsFeatureRequest.NoGeometry)
            values = [
                (feature.id(), feature[LINEAGE_FIELD_SOURCE_KEY])
                for feature in layer.getFeatures(request)
            ]
        else:
            request = QgsFeatureRequest()
            request.setNoAttributes()
            request.setFlags(QgsFeatureRequest.NoGeometry)
            values = [(feature.id(), None) for feature in layer.getFeatures(request)]

        plan = plan_source_keys(values)

        if not field_exists and not (capabilities & QgsVectorDataProvider.AddAttributes):
            raise ValueError(
                f"Source layer '{layer.name()}' cannot add the stable AtOnce UUID field. "
                "External source-key mapping is not implemented in v0.1."
            )
        if plan.assignments and not (capabilities & QgsVectorDataProvider.ChangeAttributeValues):
            raise ValueError(
                f"Source layer '{layer.name()}' cannot persist missing AtOnce UUID values."
            )

        return SourceKeyPreflight(layer.id(), field_exists, plan)

    def preflight_existing_source_keys(self, layer) -> Dict[int, str]:
        """Read stable source keys without introducing source-layer mutation."""

        preflight = self.preflight_source_keys(layer)
        if preflight.plan.assignments:
            raise ValueError(
                f"Source layer '{layer.name()}' is missing stable {LINEAGE_FIELD_SOURCE_KEY} values. "
                "Establish source keys before executing an explicit graph."
            )
        return dict(preflight.plan.existing)

    def apply_source_key_plan(self, layer, preflight: SourceKeyPreflight) -> int:
        """Persist a preflighted UUID plan in one source layer."""
        if layer.id() != preflight.layer_id:
            raise ValueError("Source-key preflight belongs to a different layer.")
        if not preflight.writes_required:
            return 0
        if layer.isEditable():
            raise ValueError(
                f"Source layer '{layer.name()}' entered edit mode after preflight; propagation aborted."
            )
        if not layer.startEditing():
            raise RuntimeError(f"Could not start an edit session for source layer '{layer.name()}'.")

        try:
            field_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
            if field_index < 0:
                if not layer.addAttribute(QgsField(LINEAGE_FIELD_SOURCE_KEY, QVariant.String, len=36)):
                    raise RuntimeError(
                        f"Could not add {LINEAGE_FIELD_SOURCE_KEY} to source layer '{layer.name()}'."
                    )
                layer.updateFields()
                field_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
                if field_index < 0:
                    raise RuntimeError(
                        f"Added {LINEAGE_FIELD_SOURCE_KEY} but QGIS did not expose the new field."
                    )

            for feature_id, key in preflight.plan.assignments.items():
                if not layer.changeAttributeValue(feature_id, field_index, key):
                    raise RuntimeError(
                        f"Could not assign an AtOnce UUID to feature {feature_id} in '{layer.name()}'."
                    )

            if not layer.commitChanges():
                details = "; ".join(layer.commitErrors()) or "unknown provider error"
                raise RuntimeError(
                    f"Could not persist AtOnce source UUIDs for '{layer.name()}': {details}"
                )
        except Exception:
            if layer.isEditable():
                layer.rollBack()
            raise

        return len(preflight.plan.assignments)
