"""QGIS-free deterministic KMZ packaging and staging orchestration."""

from dataclasses import dataclass
from pathlib import Path
import zipfile


KMZ_ENTRY_NAME = "doc.kml"
KMZ_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class KmzArchiveValidation:
    entry_name: str
    payload_size: int


@dataclass(frozen=True)
class KmzStageResult:
    feature_count: int
    payload_size: int
    entry_name: str = KMZ_ENTRY_NAME


def package_kmz(kml_path: str, kmz_path: str) -> bytes:
    """Package exactly one already-validated KML payload as deterministic KMZ."""

    payload = Path(kml_path).read_bytes()
    if not payload:
        raise ValueError("Cannot package an empty KML payload.")
    info = zipfile.ZipInfo(KMZ_ENTRY_NAME, KMZ_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    with zipfile.ZipFile(
        kmz_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return payload


def validate_kmz(kmz_path: str, expected_payload: bytes = None) -> KmzArchiveValidation:
    with zipfile.ZipFile(kmz_path, "r") as archive:
        names = archive.namelist()
        if names != [KMZ_ENTRY_NAME]:
            raise ValueError("KMZ must contain exactly one entry named doc.kml.")
        if any(
            name.startswith(("/", "\\"))
            or ".." in Path(name).parts
            or name != KMZ_ENTRY_NAME
            for name in names
        ):
            raise ValueError("KMZ contains an unsafe archive member path.")
        payload = archive.read(KMZ_ENTRY_NAME)
    if not payload:
        raise ValueError("KMZ doc.kml payload is empty.")
    if expected_payload is not None and payload != expected_payload:
        raise ValueError("KMZ doc.kml payload differs from the validated staged KML.")
    return KmzArchiveValidation(KMZ_ENTRY_NAME, len(payload))


def stage_kmz_from_kml(stage_kml, staged_kmz_path: str, inner_kml_path: str) -> KmzStageResult:
    """Run the existing KML stage once, package it, validate it, and clean both temps."""

    try:
        kml_result = stage_kml(inner_kml_path)
        payload = package_kmz(inner_kml_path, staged_kmz_path)
        validation = validate_kmz(staged_kmz_path, expected_payload=payload)
        return KmzStageResult(
            feature_count=int(kml_result.feature_count),
            payload_size=validation.payload_size,
        )
    except Exception:
        Path(staged_kmz_path).unlink(missing_ok=True)
        raise
    finally:
        Path(inner_kml_path).unlink(missing_ok=True)
