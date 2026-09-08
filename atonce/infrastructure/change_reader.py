"""Read-only QGIS boundary for returned-XLSX scans and source resolution.

Historical spreadsheet/derived lineage carries an immutable AtOnce source lineage
ID. Source lookup may therefore use a different current QGIS binding ID after an
explicit Relink Source operation.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsMapLayerType,
    QgsProject,
    QgsVectorLayer,
)

from ..core.lineage import LINEAGE_FIELD_SOURCE_KEY


@dataclass(frozen=True)
class XlsxReadResult:
    fields: List[str]
    rows: List[Dict[str, object]]


@dataclass(frozen=True)
class SourceFeatureState:
    # Immutable lineage source identity from Layer C/XLSX.
    layer_id: str
    # Current QGIS project layer binding used for the actual lookup.
    bound_layer_id: str
    layer_name: str
    source_key: str
    feature_id: int
    values: Dict[str, object]


class ChangeReader:
    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    def read_xlsx_rows(self, path: str, sheet_name: str = "AtOnce") -> XlsxReadResult:
        workbook = Path(path).expanduser()
        if not workbook.is_file():
            raise ValueError(f"Linked XLSX file does not exist: {workbook}")

        uri = f"{workbook}|layername={sheet_name}"
        layer = QgsVectorLayer(uri, f"AtOnce scan: {workbook.name}", "ogr")
        if not layer.isValid():
            layer = QgsVectorLayer(str(workbook), f"AtOnce scan: {workbook.name}", "ogr")
        if not layer.isValid():
            raise ValueError(
                f"QGIS/GDAL could not open linked XLSX {workbook}. "
                "AtOnce supports its own generated XLSX files through the OGR XLSX driver."
            )

        fields = [field.name() for field in layer.fields()]
        rows = [{name: feature[name] for name in fields} for feature in layer.getFeatures()]
        return XlsxReadResult(fields=fields, rows=rows)

    def source_feature_state(
        self,
        source_layer_id: str,
        source_key: str,
        bound_layer_id: Optional[str] = None,
    ) -> Optional[SourceFeatureState]:
        """Resolve immutable lineage against an explicit current QGIS binding."""

        current_binding = str(bound_layer_id or source_layer_id or "")
        layer = self.project.mapLayer(current_binding)
        if layer is None or layer.type() != QgsMapLayerType.VectorLayer:
            return None

        key_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
        if key_index < 0:
            return None

        quoted = QgsExpression.quotedString(str(source_key))
        request = QgsFeatureRequest().setFilterExpression(
            f'"{LINEAGE_FIELD_SOURCE_KEY}" = {quoted}'
        )
        matches = list(layer.getFeatures(request))
        if len(matches) != 1:
            return None

        feature = matches[0]
        names = [field.name() for field in layer.fields()]
        return SourceFeatureState(
            layer_id=str(source_layer_id),
            bound_layer_id=layer.id(),
            layer_name=layer.name(),
            source_key=str(source_key),
            feature_id=int(feature.id()),
            values={name: feature[name] for name in names},
        )
