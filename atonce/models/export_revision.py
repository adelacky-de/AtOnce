"""Serializable downstream export revision and baseline models.

These models deliberately contain no QGIS objects. They are persisted with the
QGIS project and later become comparison baselines for returned XLSX files and
for semantic derived/GeoPackage divergence reporting.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..core.lineage import FeatureAncestor, canonical_feature_ancestors


@dataclass
class BaselineRow:
    """One exported row keyed by immutable AtOnce lineage, never spreadsheet row number."""

    source_layer_id: str
    source_key: str
    values: Dict[str, Any] = field(default_factory=dict)
    ancestors: List[FeatureAncestor] = field(default_factory=list)

    @property
    def identity(self) -> str:
        ancestors = canonical_feature_ancestors(self.ancestors)
        if ancestors:
            return "ancestors:" + "|".join(item.identity for item in ancestors)
        return f"{self.source_layer_id}:{self.source_key}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_layer_id": self.source_layer_id,
            "source_key": self.source_key,
            "values": self.values,
            "ancestors": [
                {
                    "source_layer_id": item.source_layer_id,
                    "source_feature_key": item.source_feature_key,
                }
                for item in canonical_feature_ancestors(self.ancestors)
            ],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BaselineRow":
        return cls(
            source_layer_id=payload.get("source_layer_id", ""),
            source_key=payload.get("source_key", ""),
            values=dict(payload.get("values", {})),
            ancestors=list(canonical_feature_ancestors(payload.get("ancestors", ()))),
        )


@dataclass
class ExportOutputRevision:
    """Completed metadata for one linked XLSX output in a workflow revision."""

    export_id: str
    name: str
    path: str
    filter_expression: str
    row_count: int
    sha256: str = ""
    baseline_rows: List[BaselineRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "export_id": self.export_id,
            "name": self.name,
            "path": self.path,
            "filter_expression": self.filter_expression,
            "row_count": self.row_count,
            "sha256": self.sha256,
            "baseline_rows": [row.to_dict() for row in self.baseline_rows],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ExportOutputRevision":
        return cls(
            export_id=payload.get("export_id", ""),
            name=payload.get("name", ""),
            path=payload.get("path", ""),
            filter_expression=payload.get("filter_expression", ""),
            row_count=int(payload.get("row_count", 0)),
            sha256=payload.get("sha256", ""),
            baseline_rows=[
                BaselineRow.from_dict(item)
                for item in payload.get("baseline_rows", [])
                if isinstance(item, dict)
            ],
        )


@dataclass
class DeliveryArtifactRevision:
    """One physical artifact belonging to a forward delivery bundle."""

    path: str
    role: str
    sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "role": self.role, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DeliveryArtifactRevision":
        return cls(
            path=str(payload.get("path", "")),
            role=str(payload.get("role", "")),
            sha256=str(payload.get("sha256", "")),
        )


@dataclass
class DeliveryOutputRevision:
    """Forward-only materialization evidence; not an XLSX reverse-sync baseline."""

    delivery_id: str
    name: str
    format: str
    path: str
    feature_count: int
    sha256: str = ""
    artifacts: List[DeliveryArtifactRevision] = field(default_factory=list)
    bundle_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "name": self.name,
            "format": self.format,
            "path": self.path,
            "feature_count": self.feature_count,
            "sha256": self.sha256,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "bundle_sha256": self.bundle_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DeliveryOutputRevision":
        return cls(
            delivery_id=str(payload.get("delivery_id", "")),
            name=str(payload.get("name", "")),
            format=str(payload.get("format", "")),
            path=str(payload.get("path", "")),
            feature_count=int(payload.get("feature_count", 0)),
            sha256=str(payload.get("sha256", "")),
            artifacts=[
                DeliveryArtifactRevision.from_dict(item)
                for item in payload.get("artifacts", [])
                if isinstance(item, dict)
            ],
            bundle_sha256=str(payload.get("bundle_sha256", "")),
        )


@dataclass
class ExportRevision:
    """One completed downstream materialization run.

    Historical revisions refreshed GeoPackage plus every XLSX atomically. G5
    permits a completed run to refresh only the selected downstream branches.
    ``gpkg_refreshed`` therefore states whether this revision owns a new
    GeoPackage fingerprint; ``outputs`` contains only XLSX files refreshed by
    this run. Older persisted revisions default to ``gpkg_refreshed=True``.
    """

    revision_id: str
    workflow_id: str
    revision_number: int
    created_at: str
    gpkg_path: str
    gpkg_feature_count: int
    gpkg_sha256: str = ""
    gpkg_fingerprint: str = ""
    gpkg_fingerprint_kind: str = "semantic-v1"
    gpkg_layer_name: str = ""
    gpkg_fid_column: str = ""
    gpkg_baseline_rows: List[BaselineRow] = field(default_factory=list)
    outputs: List[ExportOutputRevision] = field(default_factory=list)
    delivery_outputs: List[DeliveryOutputRevision] = field(default_factory=list)
    status: str = "complete"
    lineage_generation: int = 1
    gpkg_refreshed: bool = True

    @property
    def owned_paths(self) -> List[str]:
        paths = [self.gpkg_path] if self.gpkg_refreshed and self.gpkg_path else []
        paths += [output.path for output in self.outputs]
        for output in self.delivery_outputs:
            paths.append(output.path)
            paths.extend(artifact.path for artifact in output.artifacts)
        return list(dict.fromkeys(paths))

    @property
    def effective_gpkg_fingerprint(self) -> str:
        if not self.gpkg_refreshed:
            return ""
        return self.gpkg_fingerprint or self.gpkg_sha256

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "workflow_id": self.workflow_id,
            "revision_number": self.revision_number,
            "created_at": self.created_at,
            "gpkg_path": self.gpkg_path,
            "gpkg_feature_count": self.gpkg_feature_count,
            "gpkg_sha256": self.gpkg_sha256,
            "gpkg_fingerprint": self.gpkg_fingerprint,
            "gpkg_fingerprint_kind": self.gpkg_fingerprint_kind,
            "gpkg_layer_name": self.gpkg_layer_name,
            "gpkg_fid_column": self.gpkg_fid_column,
            "gpkg_baseline_rows": [row.to_dict() for row in self.gpkg_baseline_rows],
            "outputs": [output.to_dict() for output in self.outputs],
            "delivery_outputs": [output.to_dict() for output in self.delivery_outputs],
            "status": self.status,
            "lineage_generation": self.lineage_generation,
            "gpkg_refreshed": self.gpkg_refreshed,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ExportRevision":
        has_semantic = bool(payload.get("gpkg_fingerprint"))
        return cls(
            revision_id=payload.get("revision_id", ""),
            workflow_id=payload.get("workflow_id", ""),
            revision_number=int(payload.get("revision_number", 0)),
            created_at=payload.get("created_at", ""),
            gpkg_path=payload.get("gpkg_path", ""),
            gpkg_feature_count=int(payload.get("gpkg_feature_count", 0)),
            gpkg_sha256=payload.get("gpkg_sha256", ""),
            gpkg_fingerprint=payload.get("gpkg_fingerprint", ""),
            gpkg_fingerprint_kind=payload.get(
                "gpkg_fingerprint_kind", "semantic-v1" if has_semantic else "sha256"
            ),
            gpkg_layer_name=payload.get("gpkg_layer_name", ""),
            gpkg_fid_column=payload.get("gpkg_fid_column", ""),
            gpkg_baseline_rows=[
                BaselineRow.from_dict(item)
                for item in payload.get("gpkg_baseline_rows", [])
                if isinstance(item, dict)
            ],
            outputs=[
                ExportOutputRevision.from_dict(item)
                for item in payload.get("outputs", [])
                if isinstance(item, dict)
            ],
            delivery_outputs=[
                DeliveryOutputRevision.from_dict(item)
                for item in payload.get("delivery_outputs", [])
                if isinstance(item, dict)
            ],
            status=payload.get("status", "complete"),
            lineage_generation=max(1, int(payload.get("lineage_generation", 1))),
            gpkg_refreshed=bool(payload.get("gpkg_refreshed", True)),
        )
