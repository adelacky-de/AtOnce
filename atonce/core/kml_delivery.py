"""QGIS-free KML CRS planning for the format-mandated WGS84 export rule."""

from dataclasses import dataclass


@dataclass(frozen=True)
class KmlCrsPlan:
    target_authid: str
    transform_required: bool


def plan_kml_crs(source_crs, target_crs) -> KmlCrsPlan:
    if source_crs is None or not source_crs.isValid():
        raise ValueError("KML export requires a valid Layer C source CRS.")
    authid = str(source_crs.authid() or "").upper()
    equivalent = getattr(source_crs, "isEquivalentTo", None)
    is_wgs84 = authid in {"EPSG:4326", "OGC:CRS84"} or (
        callable(equivalent) and bool(equivalent(target_crs))
    )
    return KmlCrsPlan("EPSG:4326", not is_wgs84)
