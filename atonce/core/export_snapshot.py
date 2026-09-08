"""Pure baseline-snapshot rules for linked XLSX exports.

Snapshots are keyed by immutable AtOnce source lineage. Spreadsheet row order is
never part of identity.
"""

import base64
import hashlib
import json
from datetime import date, datetime, time
from typing import Dict, Iterable, List, Mapping

from .lineage import (
    LINEAGE_FIELD_ANCESTORS,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_SOURCE_LAYER,
    decode_feature_ancestors,
)
from ..models.export_revision import BaselineRow


class ExportSnapshotError(ValueError):
    """Raised when an exported row cannot be safely identified."""


def choose_gpkg_fid_column(field_names: Iterable[str]) -> str:
    """Choose a GeoPackage primary-key column that cannot collide with attributes.

    GDAL's GeoPackage driver defaults its primary-key/FID column to ``fid``. Layer C
    legitimately may contain an ordinary source attribute also called ``fid`` and,
    after appending Source A + B, those values are not guaranteed globally unique.
    AtOnce therefore gives the GeoPackage its own internal primary-key column while
    preserving any source ``fid`` attribute unchanged.
    """

    names = {str(name) for name in field_names}
    base = "_atonce_gpkg_fid"
    if base not in names:
        return base

    suffix = 2
    while f"{base}_{suffix}" in names:
        suffix += 1
    return f"{base}_{suffix}"


def normalize_snapshot_value(value):
    """Convert common Python/Qt-ish values to durable JSON-safe typed values."""

    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat()}

    # PyQt date/time values expose conversion helpers without needing a Qt import.
    for method_name, kind in (
        ("toPyDateTime", "datetime"),
        ("toPyDate", "date"),
        ("toPyTime", "time"),
    ):
        method = getattr(value, method_name, None)
        if callable(method):
            converted = method()
            if hasattr(converted, "isoformat"):
                return {"type": kind, "value": converted.isoformat()}

    return {
        "type": f"{value.__class__.__module__}.{value.__class__.__name__}",
        "value": str(value),
    }


def build_baseline_rows(rows: Iterable[Mapping[str, object]]) -> List[BaselineRow]:
    """Create a deterministic lineage-keyed baseline from exported row mappings."""

    result: List[BaselineRow] = []
    seen = set()

    for row in rows:
        source_layer_id = str(row.get(LINEAGE_FIELD_SOURCE_LAYER) or "").strip()
        source_key = str(row.get(LINEAGE_FIELD_SOURCE_KEY) or "").strip()
        if not source_layer_id or not source_key:
            raise ExportSnapshotError(
                "Every exported row needs _atonce_source_layer and _atonce_source_key."
            )

        identity = f"{source_layer_id}:{source_key}"
        ancestors = list(decode_feature_ancestors(row.get(LINEAGE_FIELD_ANCESTORS)))
        baseline = BaselineRow(source_layer_id, source_key, {}, ancestors)
        identity = baseline.identity
        if identity in seen:
            raise ExportSnapshotError(
                f"Duplicate exported lineage identity {identity!r}; row order cannot disambiguate it."
            )
        seen.add(identity)

        values: Dict[str, object] = {
            name: normalize_snapshot_value(value)
            for name, value in row.items()
        }
        result.append(
            BaselineRow(source_layer_id, source_key, values, ancestors)
        )

    result.sort(key=lambda item: item.identity)
    return result


def semantic_vector_fingerprint(
    rows: Iterable[Mapping[str, object]],
    crs_text: str,
) -> str:
    """Hash vector content rather than mutable container bytes.

    SQLite/GeoPackage metadata can change when a file is merely opened by QGIS.
    The export safety check therefore fingerprints feature attributes, geometry and
    CRS in a deterministic lineage order instead of hashing the raw `.gpkg` bytes.
    Rows must contain the normal AtOnce source lineage fields and may additionally
    contain ``__geometry_wkb`` with a hexadecimal WKB representation.
    """

    baselines = build_baseline_rows(rows)
    payload = {
        "version": "atonce-semantic-vector-v1",
        "crs": str(crs_text or ""),
        "rows": [row.to_dict() for row in baselines],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
