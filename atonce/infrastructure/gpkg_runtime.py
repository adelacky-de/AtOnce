"""Runtime helpers for durable GeoPackage-backed Layer C handling.

QGIS imports stay inside functions so QGIS-free service/unit tests can import the
module safely. GeoPackage ownership is checked semantically because SQLite file
bytes may change merely from opening the dataset.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from ..core.export_snapshot import semantic_vector_fingerprint
from ..core.lineage import LINEAGE_FIELD_SOURCE_KEY, LINEAGE_FIELD_SOURCE_LAYER


def _source_path(layer) -> str:
    source = str(layer.source() or "")
    return source.split("|", 1)[0]


def layer_uses_path(layer, path: str) -> bool:
    if layer is None:
        return False
    try:
        return Path(_source_path(layer)).expanduser().resolve() == Path(path).expanduser().resolve()
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False


def load_geopackage_layer(
    project,
    path: str,
    layer_name: str,
    display_name: Optional[str] = None,
    add_to_project: bool = False,
):
    from qgis.core import QgsVectorLayer

    uri = f"{path}|layername={layer_name}"
    layer = QgsVectorLayer(uri, display_name or layer_name, "ogr")
    if not layer.isValid():
        raise RuntimeError(
            f"Could not open exported GeoPackage layer {layer_name!r} from {path!r}."
        )
    if add_to_project:
        added = project.addMapLayer(layer)
        if added is None:
            raise RuntimeError("Could not add the durable GeoPackage Layer C to the project.")
        project.setDirty(True)
    return layer


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def geopackage_semantic_fingerprint(
    project,
    path: str,
    layer_name: str,
    fid_column: str,
) -> str:
    """Fingerprint GeoPackage feature semantics without opening an OGR/QGIS layer.

    A temporary ``QgsVectorLayer`` can keep an OGR/SQLite provider alive after this
    function returns. During staging that can recreate the moved staging database
    and leave ``-wal`` / ``-shm`` sidecars behind. A read-only sqlite connection is
    sufficient here and has a deterministic lifetime, so no QGIS provider handle is
    retained during transaction install/finalize.

    The AtOnce-generated GeoPackage primary key is excluded. All ordinary fields,
    geometry bytes and the registered CRS definition remain part of the semantic
    fingerprint. Row order is normalized later by immutable AtOnce lineage.
    """

    del project  # retained in the signature for backwards-compatible callers
    gpkg = Path(path).expanduser().resolve()
    if not gpkg.is_file():
        raise RuntimeError(f"GeoPackage does not exist: {gpkg}")

    connection = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        content = connection.execute(
            "SELECT srs_id FROM gpkg_contents WHERE table_name = ?",
            (layer_name,),
        ).fetchone()
        if content is None:
            raise RuntimeError(
                f"GeoPackage layer {layer_name!r} was not found in {str(gpkg)!r}."
            )
        srs_id = content[0]

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
        field_names = [name for name in columns if name != fid_column]

        required = {LINEAGE_FIELD_SOURCE_LAYER, LINEAGE_FIELD_SOURCE_KEY}
        if not required.issubset(field_names):
            missing = sorted(required.difference(field_names))
            raise RuntimeError(
                "Exported GeoPackage is missing required lineage field(s): " + ", ".join(missing)
            )

        select_columns = ", ".join(_quote_identifier(name) for name in field_names)
        query = f"SELECT {select_columns} FROM {_quote_identifier(layer_name)}"
        rows = []
        for values in connection.execute(query):
            row = dict(zip(field_names, values))
            if geometry_column and geometry_column in row:
                geometry = row.pop(geometry_column)
                row["__geometry_wkb"] = bytes(geometry).hex() if geometry is not None else ""
            else:
                row["__geometry_wkb"] = ""
            rows.append(row)

        crs_row = connection.execute(
            "SELECT organization, organization_coordsys_id, definition "
            "FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
            (srs_id,),
        ).fetchone()
        if crs_row is None:
            crs_text = str(srs_id)
        else:
            crs_text = "|".join("" if value is None else str(value) for value in (srs_id,) + crs_row)

        return semantic_vector_fingerprint(rows, crs_text)
    finally:
        connection.close()


def add_durable_geopackage_layer(project, path: str, layer_name: str, display_name: str):
    return load_geopackage_layer(
        project,
        path,
        layer_name,
        display_name=display_name,
        add_to_project=True,
    )


def remove_project_layer(project, layer_id: Optional[str]) -> None:
    if layer_id and project.mapLayer(layer_id):
        project.removeMapLayer(layer_id)
        project.setDirty(True)
