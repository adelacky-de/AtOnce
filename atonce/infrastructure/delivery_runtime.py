"""External delivery staging and QGIS writer runtime."""

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
class XlsxStageResult:
    row_count: int
    rows: List[Dict[str, object]]


@dataclass(frozen=True)
class ShapefileStageResult:
    feature_count: int
    artifacts: Tuple[str, ...]


@dataclass(frozen=True)
class KmlStageResult:
    feature_count: int
    geometry_type: int
    crs_authid: str
    lineage_fields: Tuple[str, ...]


class DeliveryRuntime:
    def __init__(self, project, layer_runtime):
        self.project = project
        self.layer_runtime = layer_runtime

    def field_names(self, layer):
        return self.layer_runtime.field_names(layer)

    def has_field(self, layer, field_name):
        return self.layer_runtime.has_field(layer, field_name)

    def validate_expression(self, expression_text, layer=None):
        return self.layer_runtime.validate_expression(expression_text, layer)

    def require_exportable_lineage(self, layer) -> None:
        required = (
            LINEAGE_FIELD_WORKFLOW,
            LINEAGE_FIELD_SOURCE_LAYER,
            LINEAGE_FIELD_SOURCE_KEY,
        )
        missing = [name for name in required if not self.has_field(layer, name)]
        if missing:
            raise ValueError(
                "Layer C is missing required AtOnce lineage field(s): " + ", ".join(missing)
            )

    def _write_vector_layer(
        self,
        layer,
        path: str,
        driver_name: str,
        layer_name: str,
        layer_options: Optional[List[str]] = None,
    ) -> None:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver_name
        options.layerName = layer_name
        options.fileEncoding = "UTF-8"
        options.layerOptions = list(layer_options or [])
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            path,
            self.project.transformContext(),
            options,
        )
        error = result[0]
        message = result[1] if len(result) > 1 else ""
        if error != QgsVectorFileWriter.NoError:
            raise RuntimeError(
                f"QGIS {driver_name} writer failed for '{path}': {message or error}"
            )

    def stage_geopackage(self, layer, staged_path: str, layer_name: str = "layer_c") -> int:
        """Write the current derived layer to a staging GeoPackage.

        GeoPackage defaults its OGR primary-key column to ``fid``. Layer C may also
        contain an ordinary source attribute called ``fid`` whose A/B values repeat,
        so AtOnce allocates a separate collision-free primary-key column instead of
        letting GDAL reinterpret that source attribute as the table primary key.
        """
        self.require_exportable_lineage(layer)
        gpkg_fid = choose_gpkg_fid_column(self.field_names(layer))
        self._write_vector_layer(
            layer,
            staged_path,
            "GPKG",
            layer_name,
            layer_options=[f"FID={gpkg_fid}"],
        )
        return int(layer.featureCount())

    def stage_geojson(self, layer, staged_path: str) -> int:
        """Write the current derived layer to a staged UTF-8 GeoJSON file."""
        self.require_exportable_lineage(layer)
        self._write_vector_layer(layer, staged_path, "GeoJSON", "AtOnce")
        return int(layer.featureCount())

    def validate_shapefile_schema(self, layer) -> None:
        required = (
            LINEAGE_FIELD_WORKFLOW,
            LINEAGE_FIELD_SOURCE_LAYER,
            LINEAGE_FIELD_SOURCE_KEY,
        )
        issues = validate_shapefile_field_names(self.field_names(layer), required)
        if issues:
            raise ValueError(
                "Layer C cannot be represented safely as an ESRI Shapefile: "
                + " | ".join(issues)
            )

    def stage_shapefile(self, layer, staged_shp_path: str) -> ShapefileStageResult:
        """Write an owned Shapefile bundle to a staged basename only."""
        self.require_exportable_lineage(layer)
        self.validate_shapefile_schema(layer)
        self._write_vector_layer(layer, staged_shp_path, "ESRI Shapefile", "AtOnce")
        produced = existing_shapefile_artifacts(staged_shp_path)
        produced_suffixes = {path.suffix.lower() for path in produced}
        missing = sorted(SHAPEFILE_CORE_SUFFIXES - produced_suffixes)
        if missing:
            raise RuntimeError(
                "ESRI Shapefile writer did not produce required artifact(s): "
                + ", ".join(missing)
            )
        return ShapefileStageResult(
            feature_count=int(layer.featureCount()),
            artifacts=tuple(str(path) for path in produced),
        )

    @staticmethod
    def _is_wgs84_crs(crs, target_crs) -> bool:
        return not plan_kml_crs(crs, target_crs).transform_required

    @staticmethod
    def _validate_kml_geometry(layer) -> int:
        geometry_type = QgsWkbTypes.geometryType(layer.wkbType())
        supported = {
            QgsWkbTypes.PointGeometry,
            QgsWkbTypes.LineGeometry,
            QgsWkbTypes.PolygonGeometry,
        }
        if geometry_type not in supported:
            raise ValueError(
                "KML export supports point, line, and polygon geometry families only."
            )
        if QgsWkbTypes.hasZ(layer.wkbType()) or QgsWkbTypes.hasM(layer.wkbType()):
            raise ValueError("KML export blocks Z/M geometry dimensions until they have a lossless writer contract.")
        for feature in layer.getFeatures():
            if not feature.hasGeometry() or feature.geometry().isNull():
                raise ValueError(f"KML export cannot represent feature {feature.id()} without geometry.")
            if feature.geometry().type() != geometry_type:
                raise ValueError(
                    f"KML export found a geometry family mismatch on feature {feature.id()}."
                )
        return int(geometry_type)

    def _build_kml_wgs84_layer(self, layer, target_crs, transform):
        export_layer = QgsVectorLayer(
            QgsWkbTypes.displayString(layer.wkbType()),
            "AtOnce KML export",
            "memory",
        )
        if not export_layer.isValid():
            raise RuntimeError("Could not create the temporary WGS84 KML export layer.")
        export_layer.setCrs(target_crs)
        provider = export_layer.dataProvider()
        if not provider.addAttributes([QgsField(field) for field in layer.fields()]):
            raise RuntimeError("Could not copy Layer C fields to the temporary KML export layer.")
        export_layer.updateFields()

        features = []
        for source_feature in layer.getFeatures():
            if not source_feature.hasGeometry() or source_feature.geometry().isNull():
                raise ValueError(f"KML export cannot represent feature {source_feature.id()} without geometry.")
            geometry = QgsGeometry(source_feature.geometry())
            if geometry.transform(transform) != 0:
                raise RuntimeError(
                    f"Could not transform geometry for KML feature {source_feature.id()} to EPSG:4326."
                )
            feature = QgsFeature(export_layer.fields())
            feature.setAttributes(source_feature.attributes())
            feature.setGeometry(geometry)
            features.append(feature)

        result = provider.addFeatures(features)
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if not ok:
            raise RuntimeError("Could not add transformed features to the temporary KML export layer.")
        export_layer.updateExtents()
        return export_layer

    def _validate_staged_kml(self, staged_path: str, expected_count: int, expected_geometry_type: int) -> KmlStageResult:
        if not Path(staged_path).is_file():
            raise RuntimeError(f"KML writer did not create staged output: {staged_path}")
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        validation_layer = QgsVectorLayer(staged_path, "AtOnce KML validation", "ogr")
        if not validation_layer.isValid():
            raise RuntimeError("Staged KML is not readable as a vector dataset.")
        if int(validation_layer.featureCount()) != int(expected_count):
            raise RuntimeError(
                f"Staged KML feature count {validation_layer.featureCount()} does not match expected {expected_count}."
            )
        if not self._is_wgs84_crs(validation_layer.crs(), target_crs):
            raise RuntimeError("Staged KML is not represented in WGS84/EPSG:4326 semantics.")
        if QgsWkbTypes.geometryType(validation_layer.wkbType()) != expected_geometry_type:
            raise RuntimeError("Staged KML geometry family does not match Layer C.")
        required = (
            LINEAGE_FIELD_WORKFLOW,
            LINEAGE_FIELD_SOURCE_LAYER,
            LINEAGE_FIELD_SOURCE_KEY,
        )
        fields = tuple(self.field_names(validation_layer))
        missing = [name for name in required if name not in fields]
        if missing:
            raise RuntimeError(
                "Staged KML is missing distinguishable AtOnce lineage field(s): "
                + ", ".join(missing)
            )
        return KmlStageResult(
            feature_count=int(validation_layer.featureCount()),
            geometry_type=int(expected_geometry_type),
            crs_authid=str(validation_layer.crs().authid() or "EPSG:4326"),
            lineage_fields=tuple(name for name in required if name in fields),
        )

    def stage_kml(self, layer, staged_path: str) -> KmlStageResult:
        """Stage a validated WGS84 KML export without mutating Layer C."""
        self.require_exportable_lineage(layer)
        source_crs = layer.crs()
        if source_crs is None or not source_crs.isValid():
            raise ValueError("KML export requires a valid Layer C source CRS.")
        geometry_type = self._validate_kml_geometry(layer)
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if not target_crs.isValid():
            raise RuntimeError("QGIS could not construct the required KML target CRS EPSG:4326.")
        crs_plan = plan_kml_crs(source_crs, target_crs)

        export_layer = layer
        temporary_layer = None
        try:
            if crs_plan.transform_required:
                try:
                    transform = QgsCoordinateTransform(source_crs, target_crs, self.project)
                except Exception as exc:
                    raise RuntimeError(
                        f"Could not construct the WGS84 transform for KML export: {exc}"
                    ) from exc
                temporary_layer = self._build_kml_wgs84_layer(layer, target_crs, transform)
                export_layer = temporary_layer
            self._write_vector_layer(export_layer, staged_path, "KML", "AtOnce")
            return self._validate_staged_kml(staged_path, int(layer.featureCount()), geometry_type)
        finally:
            # The temporary memory layer is never added to the project and is
            # released here on both successful and failed staging.
            temporary_layer = None

    def stage_kmz(self, layer, staged_kmz_path: str):
        """Stage a deterministic KMZ by reusing the validated KML pipeline."""
        inner_kml_path = Path(staged_kmz_path).with_name(
            f".{Path(staged_kmz_path).stem}.atonce-kml-{uuid4().hex}.kml"
        )
        return stage_kmz_from_kml(
            lambda path: self.stage_kml(layer, path),
            staged_kmz_path,
            str(inner_kml_path),
        )

    def stage_xlsx(
        self,
        layer,
        staged_path: str,
        export_id: str,
        revision_number: int,
        filter_expression: str = "",
    ) -> XlsxStageResult:
        """Build a filtered, geometry-free XLSX staging layer with durable metadata."""
        self.require_exportable_lineage(layer)
        for reserved in (LINEAGE_FIELD_EXPORT, LINEAGE_FIELD_REVISION):
            if self.has_field(layer, reserved):
                raise ValueError(
                    f"Layer C already contains reserved export metadata field {reserved!r}."
                )

        ok, error = self.validate_expression(filter_expression)
        if not ok:
            raise ValueError(f"Invalid XLSX filter expression: {error}")

        table = QgsVectorLayer("None", f"AtOnce {export_id}", "memory")
        if not table.isValid():
            raise RuntimeError("QGIS could not create the temporary XLSX staging table.")

        fields = [QgsField(field) for field in layer.fields()]
        fields.extend(
            [
                QgsField(LINEAGE_FIELD_EXPORT, QVariant.String),
                QgsField(LINEAGE_FIELD_REVISION, QVariant.Int),
            ]
        )
        provider = table.dataProvider()
        if not provider.addAttributes(fields):
            raise RuntimeError("Could not create the XLSX staging schema.")
        table.updateFields()

        request = QgsFeatureRequest()
        if filter_expression.strip():
            request.setFilterExpression(filter_expression)

        source_names = [field.name() for field in layer.fields()]
        rows: List[Dict[str, object]] = []
        features = []
        for source_feature in layer.getFeatures(request):
            row = {
                name: source_feature[name]
                for name in source_names
            }
            row[LINEAGE_FIELD_EXPORT] = export_id
            row[LINEAGE_FIELD_REVISION] = revision_number
            rows.append(row)

            feature = QgsFeature(table.fields())
            for name, value in row.items():
                feature[name] = value
            features.append(feature)

        if features:
            result = provider.addFeatures(features)
            added = result[0] if isinstance(result, tuple) else bool(result)
            if not added:
                raise RuntimeError("Could not populate the XLSX staging table.")

        self._write_vector_layer(table, staged_path, "XLSX", "AtOnce")
        return XlsxStageResult(row_count=len(rows), rows=rows)
