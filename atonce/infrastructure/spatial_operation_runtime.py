"""Spatial free-form operation adapters."""

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


class SpatialOperationRuntime(FreeformOperationRuntime):
    def build_buffer_memory_layer(
        self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters
    ):
        unit = str(parameters.get("unit") or "layer").strip().lower()
        if unit != "layer":
            raise ValueError(
                "BUFFER currently accepts distance in the input layer CRS units only; "
                "automatic metre conversion is not enabled."
            )
        try:
            distance = float(parameters.get("distance"))
        except (TypeError, ValueError) as exc:
            raise ValueError("BUFFER requires a numeric distance.") from exc
        segments = int(parameters.get("segments") or 8)
        memory = self._freeform_memory_layer(parent_layer, display_name, wkb_type=QgsWkbTypes.Polygon)
        records = self._freeform_feature_records(parent_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in records}
        features = []
        dissolve = parameters.get("dissolve", False)
        if type(dissolve) is not bool:
            raise ValueError("BUFFER dissolve must be a boolean.")
        buffered = []
        for source_feature, _source_key, _identity in records:
            if not source_feature.hasGeometry():
                continue
            geometry = source_feature.geometry().buffer(distance, segments)
            if dissolve:
                buffered.append((source_feature, geometry, keys.get(source_feature.id())))
                continue
            features.append(
                self._freeform_copy_feature(
                    memory, source_feature, workflow_id, source_lineage_id,
                    keys.get(source_feature.id()), geometry=geometry,
                )
            )
        if dissolve and buffered:
            geometry = None
            ancestors = ()
            for source_feature, buffered_geometry, source_key in buffered:
                geometry = (
                    QgsGeometry(buffered_geometry)
                    if geometry is None
                    else geometry.combine(buffered_geometry)
                )
                ancestors += self._freeform_feature_ancestors(
                    source_feature, source_lineage_id, source_key
                )
            first_feature, _first_geometry, first_key = buffered[0]
            features.append(
                self._freeform_copy_feature(
                    memory,
                    first_feature,
                    workflow_id,
                    source_lineage_id,
                    first_key,
                    geometry=geometry,
                    ancestors=ancestors,
                )
            )
        return memory, self._freeform_add_features(memory, features)
    def build_reproject_memory_layer(
        self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters
    ):
        target_authid = str(parameters.get("target_crs") or "").strip()
        target_crs = QgsCoordinateReferenceSystem(target_authid)
        if not target_crs.isValid():
            raise ValueError("REPROJECT requires a valid target CRS auth ID.")
        transform = QgsCoordinateTransform(parent_layer.crs(), target_crs, self.project)
        memory = self._freeform_memory_layer(parent_layer, display_name)
        memory.setCrs(target_crs)
        records = self._freeform_feature_records(parent_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in records}
        features = []
        for source_feature, _source_key, _identity in records:
            geometry = QgsGeometry(source_feature.geometry()) if source_feature.hasGeometry() else None
            if geometry is not None:
                geometry.transform(transform)
            features.append(
                self._freeform_copy_feature(
                    memory, source_feature, workflow_id, source_lineage_id,
                    keys.get(source_feature.id()), geometry=geometry,
                )
            )
        return memory, self._freeform_add_features(memory, features)

    @staticmethod
    def _freeform_predicate(left_geometry, right_geometry, predicate):
        predicate = str(predicate or "intersects").lower()
        if predicate == "within":
            return left_geometry.within(right_geometry)
        if predicate == "contains":
            return left_geometry.contains(right_geometry)
        if predicate == "intersects":
            return left_geometry.intersects(right_geometry)
        raise ValueError("Supported spatial predicates are intersects, within and contains.")

    def build_select_by_location_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layers,
        parent_lineage_ids,
        parameters,
    ):
        if len(parent_layers) != 2:
            raise ValueError("SELECT BY LOCATION requires Target and Predicate Layer inputs.")
        self.require_equivalent_crs(parent_layers, "SELECT BY LOCATION")
        target, predicate_layer = parent_layers
        memory = self._freeform_memory_layer(target, display_name)
        target_records = self._freeform_feature_records(target)
        keys = {feature.id(): source_key for feature, source_key, _identity in target_records}
        predicate = str(parameters.get("predicate") or "intersects")
        predicates = [feature.geometry() for feature in predicate_layer.getFeatures() if feature.hasGeometry()]
        features = []
        source_lineage_id = str(parent_lineage_ids[0] or "") if parent_lineage_ids else ""
        for source_feature, _source_key, _identity in target_records:
            if not source_feature.hasGeometry():
                continue
            if any(
                self._freeform_predicate(source_feature.geometry(), geometry, predicate)
                for geometry in predicates
            ):
                features.append(
                    self._freeform_copy_feature(
                        memory, source_feature, workflow_id, source_lineage_id,
                        keys.get(source_feature.id()),
                    )
                )
        return memory, self._freeform_add_features(memory, features)

    def build_spatial_join_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layers,
        parent_lineage_ids,
        parameters,
    ):
        if len(parent_layers) != 2:
            raise ValueError("SPATIAL JOIN requires Target and Join Layer inputs.")
        self.require_equivalent_crs(parent_layers, "SPATIAL JOIN")
        target, join_layer = parent_layers
        selected = parameters.get("fields") or [field.name() for field in join_layer.fields()]
        selected = set(str(item) for item in selected)
        internal = RESERVED_LINEAGE_FIELDS
        extra = []
        used = {field.name() for field in target.fields()}
        join_names = {}
        suffix = str(parameters.get("collision_suffix") or "_spatial")
        if not suffix or not (suffix[0].isalpha() or suffix[0] == "_") or not all(
            character.isalnum() or character == "_" for character in suffix
        ):
            raise ValueError("SPATIAL JOIN collision_suffix must be a safe field-name suffix.")
        for field in join_layer.fields():
            if field.name() not in selected or field.name() in internal:
                continue
            name = field.name()
            if name in used:
                name += suffix
            join_names[field.name()] = name
            used.add(name)
            extra.append((name, field.type(), field.length()))
        memory = self._freeform_memory_layer(target, display_name, extra_fields=extra)
        target_records = self._freeform_feature_records(target)
        target_keys = {feature.id(): source_key for feature, source_key, _identity in target_records}
        join_records = [
            item for item in self._freeform_feature_records(join_layer)
            if item[0].hasGeometry()
        ]
        join_features = [feature for feature, _source_key, _identity in join_records]
        predicate = str(parameters.get("predicate") or "intersects")
        features = []
        source_lineage_id = str(parent_lineage_ids[0] or "") if parent_lineage_ids else ""
        for target_feature, _target_key, _target_identity in target_records:
            if not target_feature.hasGeometry():
                continue
            matches = [
                feature for feature in join_features
                if self._freeform_predicate(target_feature.geometry(), feature.geometry(), predicate)
            ]
            matches.sort(key=lambda feature: self._freeform_feature_order_key(feature))
            if not matches:
                matches = [None]
            if str(parameters.get("multiple_match_policy") or "all").lower() == "first":
                matches = matches[:1]
            for match in matches:
                values = {
                    output_name: match[field_name]
                    for field_name, output_name in join_names.items()
                    if match is not None
                }
                ancestors = self._freeform_feature_ancestors(
                    target_feature,
                    parent_lineage_ids[0] if parent_lineage_ids else "",
                    target_keys.get(target_feature.id()),
                )
                if match is not None:
                    ancestors = ancestors + self._freeform_feature_ancestors(
                        match,
                        parent_lineage_ids[1] if len(parent_lineage_ids) > 1 else "",
                        "",
                    )
                features.append(
                    self._freeform_copy_feature(
                        memory, target_feature, workflow_id, source_lineage_id,
                        target_keys.get(target_feature.id()), values=values,
                        ancestors=ancestors,
                    )
                )
        return memory, self._freeform_add_features(memory, features)

    def build_clip_memory_layer(
        self,
        workflow_id,
        node_id,
        display_name,
        parent_layers,
        parent_lineage_ids,
        parameters,
    ):
        if len(parent_layers) != 2:
            raise ValueError("CLIP requires Input Layer and Overlay / Mask inputs.")
        self.require_equivalent_crs(parent_layers, "CLIP")
        input_layer, overlay_layer = parent_layers
        overlay_geometry = None
        for feature in overlay_layer.getFeatures():
            if not feature.hasGeometry():
                continue
            overlay_geometry = (
                QgsGeometry(feature.geometry())
                if overlay_geometry is None
                else overlay_geometry.combine(feature.geometry())
            )
        if overlay_geometry is None:
            raise ValueError("CLIP overlay layer contains no geometry.")
        memory = self._freeform_memory_layer(input_layer, display_name)
        input_records = self._freeform_feature_records(input_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in input_records}
        source_lineage_id = str(parent_lineage_ids[0] or "") if parent_lineage_ids else ""
        features = []
        for source_feature, _source_key, _identity in input_records:
            if not source_feature.hasGeometry():
                continue
            geometry = source_feature.geometry().intersection(overlay_geometry)
            if geometry.isEmpty():
                continue
            features.append(
                self._freeform_copy_feature(
                    memory, source_feature, workflow_id, source_lineage_id,
                    keys.get(source_feature.id()), geometry=geometry,
                )
            )
        return memory, self._freeform_add_features(memory, features)

    def build_dissolve_memory_layer(
        self, workflow_id, node_id, display_name, parent_layer, source_lineage_id, parameters
    ):
        fields = [str(item) for item in (parameters.get("fields") or []) if str(item)]
        if any(name in RESERVED_LINEAGE_FIELDS for name in fields):
            raise ValueError("DISSOLVE cannot use reserved lineage fields.")
        dissolve_all = parameters.get("dissolve_all", True)
        if type(dissolve_all) is not bool:
            raise ValueError("DISSOLVE dissolve_all must be a boolean.")
        if not dissolve_all and not fields:
            raise ValueError("DISSOLVE requires fields unless dissolve_all is enabled.")
        if dissolve_all:
            fields = []
        missing = [name for name in fields if parent_layer.fields().indexOf(name) < 0]
        if missing:
            raise ValueError("DISSOLVE field(s) missing: " + ", ".join(missing))
        memory = self._freeform_memory_layer(
            parent_layer,
            display_name,
            extra_fields=(("_atonce_ancestors", QVariant.String, 0),),
        )
        parent_records = self._freeform_feature_records(parent_layer)
        keys = {feature.id(): source_key for feature, source_key, _identity in parent_records}
        groups = {}
        for feature, _source_key, _identity in parent_records:
            group_key = tuple(str(feature[name]) for name in fields) if fields else ("__all__",)
            groups.setdefault(group_key, []).append(feature)
        features = []
        for group in groups.values():
            first = group[0]
            geometry = None
            for source_feature in group:
                if not source_feature.hasGeometry():
                    continue
                geometry = (
                    QgsGeometry(source_feature.geometry())
                    if geometry is None
                    else geometry.combine(source_feature.geometry())
                )
            group_ancestors = tuple(
                ancestor
                for source_feature in group
                for ancestor in self._freeform_feature_ancestors(
                    source_feature,
                    source_lineage_id,
                    keys.get(source_feature.id()),
                )
            )
            features.append(
                self._freeform_copy_feature(
                    memory, first, workflow_id, source_lineage_id,
                    keys.get(first.id()), geometry=geometry,
                    ancestors=group_ancestors,
                )
            )
        return memory, self._freeform_add_features(memory, features)
