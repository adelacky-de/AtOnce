"""Pure helpers for forward-delivery artifact ownership and Shapefile safety."""

import hashlib
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from ..models.export_revision import DeliveryArtifactRevision


SHAPEFILE_SUFFIXES: Tuple[str, ...] = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj")
SHAPEFILE_CORE_SUFFIXES = frozenset({".shp", ".shx", ".dbf"})
SHAPEFILE_DB_NAME_LIMIT = 10


def shapefile_artifact_path(primary_path: str, suffix: str) -> Path:
    return Path(primary_path).with_suffix(suffix.lower())


def existing_shapefile_artifacts(primary_path: str) -> List[Path]:
    return [
        shapefile_artifact_path(primary_path, suffix)
        for suffix in SHAPEFILE_SUFFIXES
        if shapefile_artifact_path(primary_path, suffix).exists()
    ]


def all_shapefile_artifact_paths(primary_path: str) -> List[Path]:
    return [shapefile_artifact_path(primary_path, suffix) for suffix in SHAPEFILE_SUFFIXES]


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_role(path: str) -> str:
    return Path(path).suffix.lower()


def build_delivery_artifacts(paths: Iterable[str]) -> List[DeliveryArtifactRevision]:
    artifacts = [
        DeliveryArtifactRevision(
            path=str(Path(path)),
            role=artifact_role(path),
            sha256=file_sha256(path),
        )
        for path in paths
    ]
    return sorted(artifacts, key=lambda item: (item.role, item.path))


def canonical_bundle_manifest(artifacts: Iterable[DeliveryArtifactRevision]) -> str:
    return "".join(
        f"{artifact.role}:{artifact.sha256}\n"
        for artifact in sorted(artifacts, key=lambda item: (item.role, item.path))
    )


def bundle_sha256(artifacts: Iterable[DeliveryArtifactRevision]) -> str:
    manifest = canonical_bundle_manifest(artifacts).encode("utf-8")
    return hashlib.sha256(manifest).hexdigest()


def validate_shapefile_field_names(
    field_names: Sequence[str],
    lineage_fields: Sequence[str],
) -> List[str]:
    """Reject DBF truncation/collision before a Shapefile can be replaced."""

    issues: List[str] = []
    seen = {}
    for field_name in field_names:
        effective = field_name[:SHAPEFILE_DB_NAME_LIMIT].lower()
        if len(field_name) > SHAPEFILE_DB_NAME_LIMIT:
            issues.append(
                f"Field '{field_name}' exceeds the {SHAPEFILE_DB_NAME_LIMIT}-character Shapefile DBF limit."
            )
        previous = seen.get(effective)
        if previous and previous != field_name:
            issues.append(
                f"Fields '{previous}' and '{field_name}' collide as Shapefile field '{effective}'."
            )
        seen[effective] = field_name

    for field_name in lineage_fields:
        effective = field_name[:SHAPEFILE_DB_NAME_LIMIT].lower()
        if field_name not in field_names:
            issues.append(f"Required AtOnce lineage field '{field_name}' is missing.")
        elif sum(1 for item in field_names if item[:SHAPEFILE_DB_NAME_LIMIT].lower() == effective) != 1:
            issues.append(f"Required AtOnce lineage field '{field_name}' is not uniquely representable.")
    return sorted(set(issues))
