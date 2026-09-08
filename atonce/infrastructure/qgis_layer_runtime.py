"""QGIS layer, schema, field and CRS runtime."""

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


class QgisLayerRuntime:
    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    def resolve_layer(self, layer_id):
        if not layer_id:
            return None
        return self.project.mapLayer(layer_id)

    @staticmethod
    def _map_layer_type_value(layer) -> int:
        """Normalize QgsMapLayerType / int so QGIS version quirks do not hide rasters."""

        value = layer.type()
        try:
            return int(value)
        except Exception:
            return value

    def vector_layers(self) -> List[object]:
        vector_type = int(QgsMapLayerType.VectorLayer)
        return [
            layer
            for layer in self.project.mapLayers().values()
            if self._map_layer_type_value(layer) == vector_type
        ]

    def raster_layers(self) -> List[object]:
        raster_type = int(QgsMapLayerType.RasterLayer)
        return [
            layer
            for layer in self.project.mapLayers().values()
            if self._map_layer_type_value(layer) == raster_type
        ]

    def source_candidate_layers(self) -> List[object]:
        """Vector and raster layers that may bind to a free-form SOURCE block."""

        return list(self.vector_layers()) + list(self.raster_layers())

    @staticmethod
    def make_layer_ref(layer, role: str) -> LayerRef:
        if layer is None:
            raise ValueError("A QGIS layer is required")
        return LayerRef(
            layer_id=layer.id(),
            name=layer.name(),
            role=role,
            source_uri=layer.source(),
            provider=layer.providerType(),
        )

    @staticmethod
    def make_source_layer_ref(layer) -> LayerRef:
        """Create fresh logical source identity separate from the QGIS binding."""

        if layer is None:
            raise ValueError("A QGIS source layer is required")
        return LayerRef(
            layer_id=str(uuid4()),
            name=layer.name(),
            role=SOURCE_ROLE,
            source_uri=layer.source(),
            provider=layer.providerType(),
            binding_id=layer.id(),
        )

    @staticmethod
    def is_vector_layer(layer) -> bool:
        if layer is None:
            return False
        try:
            return int(layer.type()) == int(QgsMapLayerType.VectorLayer)
        except Exception:
            return layer.type() == QgsMapLayerType.VectorLayer

    @staticmethod
    def is_raster_layer(layer) -> bool:
        if layer is None:
            return False
        try:
            return int(layer.type()) == int(QgsMapLayerType.RasterLayer)
        except Exception:
            return layer.type() == QgsMapLayerType.RasterLayer

    @classmethod
    def layer_data_type(cls, layer) -> str:
        if cls.is_raster_layer(layer):
            return "raster"
        if cls.is_vector_layer(layer):
            return "vector"
        raise ValueError("AtOnce SOURCE blocks accept loaded vector or raster layers only.")

    @staticmethod
    def field_names(layer) -> List[str]:
        """Return the exact field names exposed by a loaded vector layer."""
        if layer is None or not hasattr(layer, "fields"):
            return []
        return [field.name() for field in layer.fields()]

    @classmethod
    def has_field(cls, layer, field_name: str) -> bool:
        return bool(field_name) and field_name in cls.field_names(layer)

    @staticmethod
    def field_specs(layer) -> List[FieldSpec]:
        if layer is None or not hasattr(layer, "fields"):
            return []
        return [
            FieldSpec(
                name=field.name(),
                type_name=str(field.type()),
                length=max(0, field.length()),
                precision=max(0, field.precision()),
            )
            for field in layer.fields()
        ]

    @staticmethod
    def validate_expression(expression_text: str, layer=None) -> Tuple[bool, str]:
        if not expression_text.strip():
            return True, ""
        expression = QgsExpression(expression_text)
        if expression.hasParserError():
            return False, expression.parserErrorString()
        if layer is not None:
            referenced = set(expression.referencedColumns())
            missing = sorted(referenced.difference(QgisLayerRuntime.field_names(layer)))
            if missing:
                return False, "Unknown field(s): " + ", ".join(missing)
            context = QgsExpressionContext()
            context.setFields(layer.fields())
            if not expression.prepare(context):
                return False, expression.evalErrorString() or "Expression could not be prepared."
            for feature in layer.getFeatures():
                context.setFeature(feature)
                expression.evaluate(context)
                if expression.hasEvalError():
                    return False, expression.evalErrorString()
        return True, ""

    @staticmethod
    def validate_crs(authid: str) -> Tuple[bool, str]:
        crs = QgsCoordinateReferenceSystem(str(authid or "").strip())
        return (True, "") if crs.isValid() else (False, "CRS auth ID is not valid.")

    @staticmethod
    def require_equivalent_crs(layers, operation: str) -> None:
        if len(layers) != 2:
            raise ValueError(f"{operation} requires exactly two spatial inputs.")
        crs_values = [layer.crs() for layer in layers]
        if any(crs is None or not crs.isValid() for crs in crs_values):
            raise ValueError(f"{operation} inputs must use valid CRS definitions.")
        if hasattr(crs_values[0], "isEquivalentTo"):
            equivalent = crs_values[0].isEquivalentTo(crs_values[1])
        else:
            left_authid = crs_values[0].authid()
            right_authid = crs_values[1].authid()
            equivalent = (
                bool(left_authid and right_authid and left_authid == right_authid)
                or crs_values[0].toWkt() == crs_values[1].toWkt()
            )
        if not equivalent:
            raise ValueError(
                f"{operation} inputs use different CRS. Add a REPROJECT operation "
                "before this spatial operation."
            )
