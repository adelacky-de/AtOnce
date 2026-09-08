"""Read semantic Layer C / GeoPackage state for divergence detection.

These helpers are read-only. They never start edit sessions or mutate project,
source, derived or output data.
"""

import sqlite3
from pathlib import Path
from typing import List

from ..core.export_snapshot import build_baseline_rows
from ..core.lineage import LINEAGE_FIELD_SOURCE_KEY, LINEAGE_FIELD_SOURCE_LAYER
from ..models.export_revision import BaselineRow

GEOMETRY_FIELD = "__geometry_wkb"


def layer_baseline_rows(layer) -> List[BaselineRow]:
    """Snapshot a loaded QGIS vector layer by immutable AtOnce lineage."""

    field_names = [field.name() for field in layer.fields()]
    rows = []
    for feature in layer.getFeatures():
        row = {name: feature[name] for name in field_names}
        if feature.hasGeometry():
            row[GEOMETRY_FIELD] = bytes(feature.geometry().asWkb()).hex()
        else:
            row[GEOMETRY_FIELD] = ""
        rows.append(row)
    return build_baseline_rows(rows)


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def geopackage_baseline_rows(
    path: str,
    layer_name: str,
    fid_column: str,
) -> List[BaselineRow]:
    """Read one GeoPackage layer semantically with an explicit sqlite lifetime."""

    gpkg = Path(path).expanduser().resolve()
    if not gpkg.is_file():
        raise RuntimeError(f"GeoPackage does not exist: {gpkg}")

    connection = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        geometry_row = connection.execute(
            "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?",
            (layer_name,),
        ).fetchone()
        geometry_column = geometry_row[0] if geometry_row else None

        columns = [
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(layer_name)})"
            ).fetchall()
        ]
        if not columns:
            raise RuntimeError(f"GeoPackage layer {layer_name!r} does not exist in {gpkg}.")

        field_names = [name for name in columns if name != fid_column]
        required = {LINEAGE_FIELD_SOURCE_LAYER, LINEAGE_FIELD_SOURCE_KEY}
        if not required.issubset(field_names):
            missing = sorted(required.difference(field_names))
            raise RuntimeError(
                "GeoPackage is missing required AtOnce lineage field(s): " + ", ".join(missing)
            )

        select_columns = ", ".join(_quote_identifier(name) for name in field_names)
        query = f"SELECT {select_columns} FROM {_quote_identifier(layer_name)}"
        rows = []
        for values in connection.execute(query):
            row = dict(zip(field_names, values))
            if geometry_column and geometry_column in row:
                geometry = row.pop(geometry_column)
                row[GEOMETRY_FIELD] = bytes(geometry).hex() if geometry is not None else ""
            else:
                row[GEOMETRY_FIELD] = ""
            rows.append(row)
        return build_baseline_rows(rows)
    finally:
        connection.close()
