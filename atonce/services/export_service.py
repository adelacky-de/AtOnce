"""Layer C -> GeoPackage -> linked XLSX materialization and baseline snapshots."""

import gc
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from uuid import uuid4

from ..core.derived_diff import compare_derived_snapshots, format_derived_diff
from ..core.export_snapshot import build_baseline_rows, choose_gpkg_fid_column
from ..core.delivery_artifacts import (
    artifact_role,
    bundle_sha256,
    existing_shapefile_artifacts,
)
from ..infrastructure.derived_runtime import geopackage_baseline_rows, layer_baseline_rows
from ..infrastructure.gpkg_runtime import (
    add_durable_geopackage_layer,
    geopackage_semantic_fingerprint,
    layer_uses_path,
    remove_project_layer,
)
from ..infrastructure.output_transaction import (
    OutputTransaction,
    StagedOutput,
    cleanup_staging_artifacts,
    staging_path_for,
)
from ..models.derived_state import DerivedState
from ..models.export_revision import (
    DeliveryArtifactRevision,
    DeliveryOutputRevision,
    ExportOutputRevision,
    ExportRevision,
)
from ..models.workflow import DERIVED_ROLE, WorkflowDefinition


class ExportError(RuntimeError):
    """Raised when a downstream revision cannot complete safely."""


class DerivedDivergenceError(ExportError):
    """Raised when Layer C / its GeoPackage differs from recorded AtOnce state."""

    def __init__(self, message, diff):
        self.diff = diff
        super().__init__(f"{message} {diff.summary()}")

    @property
    def details(self) -> str:
        return format_derived_diff(self.diff)


@dataclass(frozen=True)
class ExportRunResult:
    revision: ExportRevision

    @property
    def xlsx_row_count(self) -> int:
        return sum(output.row_count for output in self.revision.outputs)


class ExportService:
    """Create a safe completed downstream revision from the current Layer C.

    With no selection arguments this preserves the proven v0.1 all-output
    behavior. G5 may supply ``refresh_gpkg`` and ``export_ids`` so only selected
    downstream branches are staged/installed. Skipped paths are not preflighted,
    opened, staged, replaced, or claimed by the new revision.
    """

    def __init__(self, store, qgis_gateway, workflow_service):
        self.store = store
        self.qgis = qgis_gateway
        self.workflow_service = workflow_service

    def export_downstream(
        self,
        workflow_id: Optional[str] = None,
        *,
        refresh_gpkg: bool = True,
        export_ids: Optional[Iterable[str]] = None,
        delivery_ids: Optional[Iterable[str]] = None,
    ) -> ExportRunResult:
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            raise ExportError("No registered AtOnce workflow is available for export.")

        validation = self.workflow_service.validate(workflow)
        if not validation.is_valid:
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            raise ExportError("Workflow validation failed: " + " | ".join(errors))

        selected_exports = self._selected_exports(workflow, export_ids)
        selected_deliveries = self._selected_deliveries(workflow, delivery_ids)
        if not refresh_gpkg and not selected_exports and not selected_deliveries:
            raise ExportError("No downstream output branch was selected for refresh.")

        derived_ref = workflow.derived_layer
        if derived_ref is None or not derived_ref.layer_id:
            raise ExportError("Build Layer C before exporting downstream outputs.")
        derived = self.qgis.resolve_layer(derived_ref.layer_id)
        if derived is None:
            raise ExportError("The registered Layer C is missing from the QGIS project. Rebuild Layer C first.")
        if not self.qgis.is_vector_layer(derived):
            raise ExportError("The registered Layer C is not a vector layer.")

        layer_name = derived_ref.name or "layer_c"
        field_names = self.qgis.field_names(derived) if hasattr(self.qgis, "field_names") else []
        gpkg_fid_column = choose_gpkg_fid_column(field_names)

        try:
            self.qgis.require_exportable_lineage(derived)
            current_derived_rows = self._layer_baseline_rows(derived)
            load_derived_state = getattr(self.store, "load_derived_state", None)
            recorded_state = (
                load_derived_state(workflow.workflow_id)
                if callable(load_derived_state)
                else None
            )
            if recorded_state is not None:
                diff = compare_derived_snapshots(recorded_state.rows, current_derived_rows)
                if diff.has_changes:
                    raise DerivedDivergenceError(
                        "Layer C was modified outside the recorded AtOnce rebuild/export state. "
                        "Review the differences before rebuilding or replacing downstream outputs.",
                        diff,
                    )
            obsolete_targets = self._preflight_targets(
                workflow,
                layer_name,
                gpkg_fid_column,
                refresh_gpkg=refresh_gpkg,
                exports=selected_exports,
                deliveries=selected_deliveries,
            )
        except DerivedDivergenceError:
            raise
        except Exception as exc:
            raise ExportError(f"Downstream export preflight failed: {exc}") from exc

        revision_number = self.store.next_export_revision_number(workflow.workflow_id)
        staged: List[StagedOutput] = []
        output_revisions: List[ExportOutputRevision] = []
        delivery_revisions: List[DeliveryOutputRevision] = []
        gpkg_count = 0
        gpkg_fingerprint = ""
        gpkg_baseline = []

        try:
            if refresh_gpkg:
                gpkg_stage = staging_path_for(workflow.gpkg_path)
                staged.append(StagedOutput(gpkg_stage, workflow.gpkg_path))
                gpkg_count = self.qgis.stage_geopackage(
                    derived,
                    gpkg_stage,
                    layer_name=layer_name,
                )
                gpkg_fingerprint = self._geopackage_fingerprint(
                    gpkg_stage,
                    layer_name,
                    gpkg_fid_column,
                )
                gpkg_baseline = self._geopackage_baseline_rows(
                    gpkg_stage,
                    layer_name,
                    gpkg_fid_column,
                )

            for export in selected_exports:
                xlsx_stage = staging_path_for(export.path)
                staged.append(StagedOutput(xlsx_stage, export.path))
                result = self.qgis.stage_xlsx(
                    derived,
                    xlsx_stage,
                    export.export_id,
                    revision_number,
                    export.filter_expression,
                )
                baseline = build_baseline_rows(result.rows)
                output_revisions.append(
                    ExportOutputRevision(
                        export_id=export.export_id,
                        name=export.name,
                        path=export.path,
                        filter_expression=export.filter_expression,
                        row_count=result.row_count,
                        sha256=self._file_sha256(xlsx_stage),
                        baseline_rows=baseline,
                    )
                )

            self._stage_forward_deliveries(
                derived,
                selected_deliveries,
                staged,
                delivery_revisions,
            )
        except Exception as exc:
            self._cleanup_staged(staged)
            raise ExportError(f"Downstream staging failed: {exc}") from exc

        revision = ExportRevision(
            revision_id=str(uuid4()),
            workflow_id=workflow.workflow_id,
            revision_number=revision_number,
            created_at=datetime.now(timezone.utc).isoformat(),
            gpkg_path=workflow.gpkg_path,
            gpkg_feature_count=gpkg_count,
            gpkg_fingerprint=gpkg_fingerprint,
            gpkg_fingerprint_kind="semantic-v1",
            gpkg_layer_name=layer_name,
            gpkg_fid_column=gpkg_fid_column,
            gpkg_baseline_rows=gpkg_baseline,
            outputs=output_revisions,
            delivery_outputs=delivery_revisions,
            lineage_generation=workflow.lineage_generation,
            gpkg_refreshed=bool(refresh_gpkg),
        )

        old_workflow = WorkflowDefinition.from_dict(workflow.to_dict())
        old_layer_id = derived_ref.layer_id
        old_display_name = derived_ref.name or "Layer C"
        old_uses_target = bool(
            refresh_gpkg and self._layer_uses_output(derived, workflow.gpkg_path)
        )
        durable_layer = None
        workflow_saved = False
        staged_targets = {str(Path(item.target_path).resolve()) for item in staged}
        obsolete_targets = [
            path for path in obsolete_targets
            if str(Path(path).resolve()) not in staged_targets
        ]
        transaction = OutputTransaction(staged, obsolete_targets=obsolete_targets)

        if old_uses_target:
            try:
                self._remove_project_layer(old_layer_id)
                derived = None
                gc.collect()
            except Exception as exc:
                self._cleanup_staged(staged)
                raise ExportError(
                    f"Could not release the current GeoPackage Layer C before replacement: {exc}"
                ) from exc

        try:
            transaction.install()
            durable_layer_baseline = current_derived_rows
            if refresh_gpkg:
                durable_layer = self._add_durable_geopackage_layer(
                    workflow.gpkg_path,
                    layer_name,
                    old_display_name,
                )
                durable_layer_baseline = self._layer_baseline_rows(durable_layer)
                durable_ref = self.qgis.make_layer_ref(durable_layer, DERIVED_ROLE)
                workflow.layers = [
                    durable_ref if item.role == DERIVED_ROLE else item
                    for item in workflow.layers
                ]

            # A refreshed workbook's completed revision supersedes temporary
            # relink proof. Skipped workbooks retain their prior proof/state.
            selected_export_ids = {item.export_id for item in selected_exports}
            for export in workflow.exports:
                if export.export_id not in selected_export_ids:
                    continue
                export.relink_revision_number = None
                export.relink_sha256 = ""
                export.relink_modified = False

            self.store.save_workflow(workflow)
            workflow_saved = True
            self.store.save_export_revision(revision)

            if refresh_gpkg:
                save_derived_state = getattr(self.store, "save_derived_state", None)
                if callable(save_derived_state):
                    save_derived_state(
                        DerivedState(
                            workflow_id=workflow.workflow_id,
                            created_at=revision.created_at,
                            origin=f"export-revision-{revision.revision_number}",
                            rows=durable_layer_baseline,
                        )
                    )

                if (
                    not old_uses_target
                    and old_layer_id
                    and durable_layer is not None
                    and old_layer_id != durable_layer.id()
                ):
                    self._remove_project_layer(old_layer_id)
        except Exception as exc:
            if durable_layer is not None:
                try:
                    self._remove_project_layer(durable_layer.id())
                    durable_layer = None
                    gc.collect()
                except Exception:
                    pass

            transaction.rollback()

            if refresh_gpkg and old_uses_target and Path(workflow.gpkg_path).exists():
                try:
                    restored = self._add_durable_geopackage_layer(
                        workflow.gpkg_path,
                        layer_name,
                        old_display_name,
                    )
                    restored_ref = self.qgis.make_layer_ref(restored, DERIVED_ROLE)
                    old_workflow.layers = [
                        restored_ref if item.role == DERIVED_ROLE else item
                        for item in old_workflow.layers
                    ]
                    self.store.save_workflow(old_workflow)
                except Exception:
                    pass
            elif workflow_saved:
                try:
                    self.store.save_workflow(old_workflow)
                except Exception:
                    pass

            raise ExportError(
                f"Downstream revision {revision_number} was not completed: {exc}"
            ) from exc

        transaction.finalize()
        return ExportRunResult(revision)

    def export_forward_deliveries_from_layer(
        self,
        workflow,
        layer,
        delivery_ids: Iterable[str],
    ) -> ExportRunResult:
        """Export selected forward deliveries from an explicit materialized layer."""

        selected_deliveries = self._selected_deliveries(workflow, delivery_ids)
        if not selected_deliveries:
            raise ExportError("No forward delivery branch was selected for refresh.")
        is_vector = self.qgis.is_vector_layer(layer)
        is_raster = getattr(self.qgis, "is_raster_layer", lambda _layer: False)(layer)
        if is_raster:
            bad = sorted(
                {
                    item.format
                    for item in selected_deliveries
                    if item.format not in {"geotiff", "kmz"}
                }
            )
            if bad:
                raise ExportError(
                    "Raster deliveries support GeoTIFF and KMZ only; got: "
                    + ", ".join(bad)
                )
        elif not is_vector:
            raise ExportError("The explicit derived node is not a vector layer.")
        try:
            if is_vector:
                self.qgis.require_exportable_lineage(layer)
            name_attr = getattr(layer, "name", None)
            layer_name = name_attr() if callable(name_attr) else name_attr
            obsolete_targets = self._preflight_targets(
                workflow,
                layer_name or "AtOnce",
                "",
                refresh_gpkg=False,
                exports=[],
                deliveries=selected_deliveries,
            )
        except Exception as exc:
            raise ExportError(f"Explicit forward-delivery preflight failed: {exc}") from exc

        revision_number = self.store.next_export_revision_number(workflow.workflow_id)
        staged: List[StagedOutput] = []
        delivery_revisions: List[DeliveryOutputRevision] = []
        try:
            self._stage_forward_deliveries(
                layer,
                selected_deliveries,
                staged,
                delivery_revisions,
            )
        except Exception as exc:
            self._cleanup_staged(staged)
            raise ExportError(f"Explicit forward-delivery staging failed: {exc}") from exc

        revision = ExportRevision(
            revision_id=str(uuid4()),
            workflow_id=workflow.workflow_id,
            revision_number=revision_number,
            created_at=datetime.now(timezone.utc).isoformat(),
            gpkg_path=workflow.gpkg_path,
            gpkg_feature_count=0,
            delivery_outputs=delivery_revisions,
            lineage_generation=workflow.lineage_generation,
            gpkg_refreshed=False,
        )
        transaction = OutputTransaction(staged, obsolete_targets=obsolete_targets)
        try:
            transaction.install()
            self.store.save_export_revision(revision)
        except Exception as exc:
            transaction.rollback()
            raise ExportError(
                f"Explicit forward-delivery revision {revision_number} was not completed: {exc}"
            ) from exc
        transaction.finalize()
        return ExportRunResult(revision)

    def export_forward_deliveries_from_layers(
        self,
        workflow,
        layers_by_delivery_id,
        delivery_ids: Iterable[str],
    ) -> ExportRunResult:
        """Export one durable revision when terminal DAG branches use many layers."""

        selected_deliveries = self._selected_deliveries(workflow, delivery_ids)
        if not selected_deliveries:
            raise ExportError("No forward delivery branch was selected for refresh.")
        for delivery in selected_deliveries:
            layer = layers_by_delivery_id.get(delivery.delivery_id)
            if layer is None:
                raise ExportError(
                    f"Terminal operation for delivery {delivery.delivery_id!r} is not materialized."
                )
            is_vector = self.qgis.is_vector_layer(layer)
            is_raster = getattr(self.qgis, "is_raster_layer", lambda _layer: False)(
                layer
            )
            if is_vector:
                self.qgis.require_exportable_lineage(layer)
            elif is_raster:
                if delivery.format not in {"geotiff", "kmz"}:
                    raise ExportError(
                        f"Raster delivery {delivery.delivery_id!r} supports "
                        "GeoTIFF and KMZ only."
                    )
            else:
                raise ExportError(
                    f"Terminal operation for delivery {delivery.delivery_id!r} is not materialized."
                )
        try:
            obsolete_targets = self._preflight_targets(
                workflow,
                "AtOnce",
                "",
                refresh_gpkg=False,
                exports=[],
                deliveries=selected_deliveries,
            )
        except Exception as exc:
            raise ExportError(f"Explicit forward-delivery preflight failed: {exc}") from exc

        revision_number = self.store.next_export_revision_number(workflow.workflow_id)
        staged: List[StagedOutput] = []
        delivery_revisions: List[DeliveryOutputRevision] = []
        try:
            self._stage_forward_deliveries(
                None,
                selected_deliveries,
                staged,
                delivery_revisions,
                layers_by_delivery_id=layers_by_delivery_id,
            )
        except Exception as exc:
            self._cleanup_staged(staged)
            raise ExportError(f"Explicit forward-delivery staging failed: {exc}") from exc

        revision = ExportRevision(
            revision_id=str(uuid4()),
            workflow_id=workflow.workflow_id,
            revision_number=revision_number,
            created_at=datetime.now(timezone.utc).isoformat(),
            gpkg_path=workflow.gpkg_path,
            gpkg_feature_count=0,
            delivery_outputs=delivery_revisions,
            lineage_generation=workflow.lineage_generation,
            gpkg_refreshed=False,
        )
        transaction = OutputTransaction(staged, obsolete_targets=obsolete_targets)
        try:
            transaction.install()
            self.store.save_export_revision(revision)
        except Exception as exc:
            transaction.rollback()
            raise ExportError(
                f"Explicit forward-delivery revision {revision_number} was not completed: {exc}"
            ) from exc
        transaction.finalize()
        return ExportRunResult(revision)

    def _stage_forward_deliveries(
        self,
        layer,
        deliveries,
        staged: List[StagedOutput],
        delivery_revisions: List[DeliveryOutputRevision],
        *,
        layers_by_delivery_id=None,
    ) -> None:
        for delivery in deliveries:
            current_layer = (
                layers_by_delivery_id.get(delivery.delivery_id)
                if layers_by_delivery_id is not None
                else layer
            )
            if delivery.format == "geojson":
                stage_path = staging_path_for(delivery.path)
                staged.append(StagedOutput(stage_path, delivery.path))
                count = self.qgis.stage_geojson(current_layer, stage_path)
                delivery_revisions.append(
                    DeliveryOutputRevision(
                        delivery_id=delivery.delivery_id,
                        name=delivery.name,
                        format=delivery.format,
                        path=delivery.path,
                        feature_count=count,
                        sha256=self._file_sha256(stage_path),
                    )
                )
                continue

            if delivery.format == "gpkg":
                stage_path = staging_path_for(delivery.path)
                staged.append(StagedOutput(stage_path, delivery.path))
                layer_name = "".join(
                    ch if ch.isalnum() or ch in {"_", "-"} else "_"
                    for ch in str(delivery.name or "atonce")
                ).strip("_") or "atonce"
                count = self.qgis.stage_geopackage(
                    current_layer, stage_path, layer_name=layer_name[:60]
                )
                delivery_revisions.append(
                    DeliveryOutputRevision(
                        delivery_id=delivery.delivery_id,
                        name=delivery.name,
                        format=delivery.format,
                        path=delivery.path,
                        feature_count=count,
                        sha256=self._file_sha256(stage_path),
                    )
                )
                continue

            if delivery.format == "geotiff":
                stage_path = staging_path_for(delivery.path)
                staged.append(StagedOutput(stage_path, delivery.path))
                count = self.qgis.stage_geotiff(current_layer, stage_path)
                delivery_revisions.append(
                    DeliveryOutputRevision(
                        delivery_id=delivery.delivery_id,
                        name=delivery.name,
                        format=delivery.format,
                        path=delivery.path,
                        feature_count=count,
                        sha256=self._file_sha256(stage_path),
                    )
                )
                continue

            if delivery.format in {"kml", "kmz"}:
                stage_path = staging_path_for(delivery.path)
                staged.append(StagedOutput(stage_path, delivery.path))
                if delivery.format == "kmz" and getattr(
                    self.qgis, "is_raster_layer", lambda _layer: False
                )(current_layer):
                    count = self.qgis.stage_raster_kmz(
                        current_layer,
                        stage_path,
                        display_name=str(delivery.name or ""),
                    )
                    result_count = count
                elif getattr(self.qgis, "is_raster_layer", lambda _layer: False)(
                    current_layer
                ):
                    raise ValueError("Raster deliveries support GeoTIFF and KMZ only (not KML).")
                else:
                    stage_method = self.qgis.stage_kml if delivery.format == "kml" else self.qgis.stage_kmz
                    result = stage_method(current_layer, stage_path)
                    result_count = result.feature_count
                delivery_revisions.append(
                    DeliveryOutputRevision(
                        delivery_id=delivery.delivery_id,
                        name=delivery.name,
                        format=delivery.format,
                        path=delivery.path,
                        feature_count=result_count,
                        sha256=self._file_sha256(stage_path),
                    )
                )
                continue

            if delivery.format != "shapefile":
                raise ValueError(f"Unsupported forward delivery format: {delivery.format}")
            stage_path = staging_path_for(delivery.path)
            pending_stage = StagedOutput(stage_path, delivery.path)
            staged.append(pending_stage)
            result = self.qgis.stage_shapefile(current_layer, stage_path)
            target_artifacts = []
            for produced in result.artifacts:
                suffix = Path(produced).suffix.lower()
                target = str(Path(delivery.path).with_suffix(suffix))
                staged.append(StagedOutput(produced, target))
                target_artifacts.append(
                    (target, artifact_role(produced), self._file_sha256(produced))
                )
            if not target_artifacts:
                raise ValueError("Shapefile writer produced no artifacts.")
            staged.remove(pending_stage)
            artifact_revisions = [
                DeliveryArtifactRevision(path=path, role=role, sha256=sha)
                for path, role, sha in target_artifacts
            ]
            artifact_revisions.sort(key=lambda item: (item.role, item.path))
            primary_sha = next(
                item.sha256 for item in artifact_revisions if item.role == ".shp"
            )
            delivery_revisions.append(
                DeliveryOutputRevision(
                    delivery_id=delivery.delivery_id,
                    name=delivery.name,
                    format=delivery.format,
                    path=delivery.path,
                    feature_count=result.feature_count,
                    sha256=primary_sha,
                    artifacts=artifact_revisions,
                    bundle_sha256=bundle_sha256(artifact_revisions),
                )
            )

    @staticmethod
    def _selected_exports(workflow, export_ids: Optional[Iterable[str]]):
        if export_ids is None:
            return list(workflow.exports)
        requested = {str(item) for item in export_ids}
        known = {item.export_id for item in workflow.exports}
        unknown = requested - known
        if unknown:
            raise ExportError(
                "Unknown linked export id(s): " + ", ".join(sorted(unknown))
            )
        return [item for item in workflow.exports if item.export_id in requested]

    @staticmethod
    def _selected_deliveries(workflow, delivery_ids: Optional[Iterable[str]]):
        if delivery_ids is None:
            selected = list(workflow.forward_deliveries)
        else:
            requested = {str(item) for item in delivery_ids}
            known = {item.delivery_id for item in workflow.forward_deliveries}
            unknown = requested - known
            if unknown:
                raise ExportError("Unknown forward delivery id(s): " + ", ".join(sorted(unknown)))
            selected = [item for item in workflow.forward_deliveries if item.delivery_id in requested]
        unsupported = [item.format for item in selected if item.format not in {"geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"}]
        if unsupported:
            raise ExportError("Unsupported forward delivery format(s): " + ", ".join(sorted(set(unsupported))))
        return selected

    def _preflight_targets(
        self,
        workflow,
        layer_name: str,
        gpkg_fid_column: str,
        *,
        refresh_gpkg: bool = True,
        exports=None,
        deliveries=None,
    ) -> List[str]:
        selected_exports = list(workflow.exports if exports is None else exports)
        selected_deliveries = list(workflow.forward_deliveries if deliveries is None else deliveries)
        targets = ([workflow.gpkg_path] if refresh_gpkg else []) + [
            item.path for item in selected_exports
        ] + [item.path for item in selected_deliveries]
        normalized = []
        for raw_path in targets:
            if not str(raw_path).strip():
                raise ValueError("Every selected downstream output needs a configured path.")
            path = Path(raw_path).expanduser()
            parent = path.parent
            if not parent.exists() or not parent.is_dir():
                raise ValueError(f"Output parent directory does not exist: {parent}")
            normalized.append(str(path.resolve()))

        if len(normalized) != len(set(normalized)):
            raise ValueError("Selected downstream outputs must use distinct file paths.")

        if refresh_gpkg:
            gpkg = Path(workflow.gpkg_path).expanduser()
            if gpkg.exists():
                recorded = self._recorded_gpkg_revision(workflow.workflow_id, str(gpkg))
                if recorded is None or not recorded.effective_gpkg_fingerprint:
                    raise ValueError(
                        f"Refusing to replace existing non-AtOnce output: {gpkg}. "
                        "Choose a new path or remove/move that file explicitly."
                    )

                if recorded.gpkg_fingerprint_kind == "semantic-v1":
                    current = self._geopackage_fingerprint(
                        str(gpkg),
                        recorded.gpkg_layer_name or layer_name,
                        recorded.gpkg_fid_column or gpkg_fid_column,
                    )
                else:
                    current = self._file_sha256(str(gpkg))
                if current != recorded.effective_gpkg_fingerprint:
                    if recorded.gpkg_baseline_rows:
                        current_rows = self._geopackage_baseline_rows(
                            str(gpkg),
                            recorded.gpkg_layer_name or layer_name,
                            recorded.gpkg_fid_column or gpkg_fid_column,
                        )
                        diff = compare_derived_snapshots(
                            recorded.gpkg_baseline_rows,
                            current_rows,
                        )
                        raise DerivedDivergenceError(
                            "The AtOnce GeoPackage was modified after its recorded revision. "
                            "AtOnce will not overwrite it silently.",
                            diff,
                        )
                    raise ValueError(
                        f"Refusing to replace modified AtOnce output: {gpkg}. "
                        "Its feature content changed since the recorded revision; this older revision "
                        "does not contain a detailed semantic baseline, so review or move it first."
                    )

        for export in selected_exports:
            path = Path(export.path).expanduser()
            if not path.exists():
                continue
            expected = self.store.output_fingerprint(workflow.workflow_id, str(path))
            if expected is None and export.relink_sha256:
                if export.relink_modified:
                    raise ValueError(
                        f"Relinked workbook contains user edits: {path}. "
                        "Scan/review those edits before refreshing; AtOnce will not overwrite them silently."
                    )
                expected = export.relink_sha256
            if expected is None:
                raise ValueError(
                    f"Refusing to replace existing non-AtOnce output: {path}. "
                    "Choose a new path, explicitly Relink workbook, or remove/move that file."
                )
            current = self._file_sha256(str(path))
            if current != expected:
                raise ValueError(
                    f"Refusing to replace modified AtOnce output: {path}. "
                    "Its contents changed since the recorded/relinked revision; review it first."
                )

        obsolete_targets = []
        for delivery in selected_deliveries:
            path = Path(delivery.path).expanduser()
            if delivery.format in {"geojson", "kml", "kmz", "gpkg", "geotiff"}:
                if not path.exists():
                    continue
                expected = self.store.output_fingerprint(workflow.workflow_id, str(path))
                if expected is None:
                    raise ValueError(
                        f"Refusing to replace existing non-AtOnce output: {path}. "
                        "Choose a new path or remove/move that file explicitly."
                    )
                if self._file_sha256(str(path)) != expected:
                    raise ValueError(
                        f"Refusing to replace modified AtOnce output: {path}. "
                        "Its contents changed since the recorded revision."
                    )
                continue

            recorded = self._recorded_delivery_revision(workflow.workflow_id, delivery.delivery_id)
            existing = existing_shapefile_artifacts(str(path))
            if existing and (recorded is None or not recorded.artifacts):
                raise ValueError(
                    f"Refusing to replace existing non-AtOnce Shapefile bundle: {path}."
                )
            if recorded is not None and recorded.artifacts:
                recorded_paths = {
                    str(Path(item.path).expanduser().resolve())
                    for item in recorded.artifacts
                }
                unowned_current = {
                    str(item.expanduser().resolve()) for item in existing
                } - recorded_paths
                if unowned_current:
                    raise ValueError(
                        f"Refusing to replace existing non-AtOnce Shapefile bundle: {path}."
                    )
                current_artifacts = []
                for artifact in recorded.artifacts:
                    artifact_path = Path(artifact.path)
                    if not artifact_path.exists():
                        raise ValueError(f"AtOnce-owned Shapefile artifact is missing: {artifact_path}")
                    current_artifacts.append(
                        type(artifact)(
                            path=str(artifact_path),
                            role=artifact.role,
                            sha256=self._file_sha256(str(artifact_path)),
                        )
                    )
                if any(item.sha256 != recorded.artifacts[index].sha256 for index, item in enumerate(current_artifacts)):
                    raise ValueError(
                        f"Refusing to replace modified AtOnce Shapefile bundle: {path}."
                    )
                if recorded.bundle_sha256 and bundle_sha256(current_artifacts) != recorded.bundle_sha256:
                    raise ValueError(
                        f"Refusing to replace modified AtOnce Shapefile bundle: {path}."
                    )
                obsolete_targets.extend(str(item.path) for item in recorded.artifacts)

        return obsolete_targets

    def _recorded_delivery_revision(self, workflow_id: str, delivery_id: str):
        loader = getattr(self.store, "load_export_revisions", None)
        if callable(loader):
            for revision in reversed(loader(workflow_id)):
                for output in revision.delivery_outputs:
                    if output.delivery_id == delivery_id and output.format == "shapefile":
                        return output
        latest = getattr(self.store, "latest_export_revision", lambda _workflow_id: None)(workflow_id)
        if latest is not None:
            for output in latest.delivery_outputs:
                if output.delivery_id == delivery_id and output.format == "shapefile":
                    return output
        return None

    def _recorded_gpkg_revision(self, workflow_id: str, path: str):
        candidate = str(Path(path).expanduser().resolve())
        loader = getattr(self.store, "load_export_revisions", None)
        if callable(loader):
            for revision in reversed(loader(workflow_id)):
                if not revision.gpkg_refreshed:
                    continue
                if str(Path(revision.gpkg_path).expanduser().resolve()) == candidate:
                    return revision
        latest = getattr(self.store, "latest_export_revision", lambda _workflow_id: None)(
            workflow_id
        )
        if latest is not None and latest.gpkg_refreshed:
            if str(Path(latest.gpkg_path).expanduser().resolve()) == candidate:
                return latest
        return None

    def _geopackage_fingerprint(self, path: str, layer_name: str, fid_column: str) -> str:
        method = getattr(self.qgis, "geopackage_fingerprint", None)
        if callable(method):
            return method(path, layer_name, fid_column)
        return geopackage_semantic_fingerprint(
            self.qgis.project,
            path,
            layer_name,
            fid_column,
        )

    def _layer_baseline_rows(self, layer):
        method = getattr(self.qgis, "semantic_baseline_rows", None)
        if callable(method):
            return method(layer)
        return layer_baseline_rows(layer)

    def _geopackage_baseline_rows(self, path: str, layer_name: str, fid_column: str):
        method = getattr(self.qgis, "geopackage_baseline_rows", None)
        if callable(method):
            return method(path, layer_name, fid_column)
        return geopackage_baseline_rows(path, layer_name, fid_column)

    def _add_durable_geopackage_layer(self, path: str, layer_name: str, display_name: str):
        method = getattr(self.qgis, "add_durable_geopackage_layer", None)
        if callable(method):
            return method(path, layer_name, display_name)
        return add_durable_geopackage_layer(
            self.qgis.project,
            path,
            layer_name,
            display_name,
        )

    def _remove_project_layer(self, layer_id: str) -> None:
        method = getattr(self.qgis, "remove_project_layer", None)
        if callable(method):
            method(layer_id)
            return
        remove_project_layer(self.qgis.project, layer_id)

    def _layer_uses_output(self, layer, path: str) -> bool:
        method = getattr(self.qgis, "layer_uses_path", None)
        if callable(method):
            return bool(method(layer, path))
        return layer_uses_path(layer, path)

    @staticmethod
    def _file_sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_staged(staged: List[StagedOutput]) -> None:
        for output in staged:
            cleanup_staging_artifacts(output.staged_path)

    def refresh_affected(self, source_changes):
        raise NotImplementedError(
            "Use the G5 PropagationPlan adapter to select downstream branches explicitly."
        )
