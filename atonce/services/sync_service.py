"""Returned-XLSX detection/classification and approved source write-back orchestration."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from ..core.change_detection import (
    ROW_FIELD,
    compare_workbook_rows,
    snapshot_value_equal,
    source_field_for,
    workbook_revision_candidates,
)
from ..core.export_snapshot import normalize_snapshot_value
from ..core.sync_planning import plan_approved_changes, scan_changes_signature
from ..infrastructure.workbook_archive import plan_workbook_archive
from ..models.change import ChangeDisposition, ChangeScanResult, DetectedChange
from ..models.sync_audit import SyncAuditChange, SyncAuditRecord


class SyncError(RuntimeError):
    """Raised when a linked XLSX scan or approved sync cannot complete safely."""


@dataclass(frozen=True)
class SyncApplyResult:
    sync_id: str
    applied_changes: int
    refresh_revision_number: Optional[int]
    returned_workbook_archive: str
    stale_node_ids: tuple = ()


class SyncService:
    """Detect returned-XLSX edits and apply only explicit Writable approvals."""

    def __init__(
        self,
        store,
        workflow_service=None,
        reader=None,
        writer=None,
        propagation_service=None,
        export_service=None,
    ):
        self.store = store
        self.workflow_service = workflow_service
        if reader is None:
            from ..infrastructure.change_reader import ChangeReader

            reader = ChangeReader()
        self.reader = reader
        # Keep the QGIS-mutating writer lazy so pure detection/unit tests never need
        # a QGIS import merely to construct SyncService.
        self.writer = writer
        self.propagation_service = propagation_service
        self.export_service = export_service

    def detect_changes(
        self,
        export_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> ChangeScanResult:
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            raise SyncError("No registered AtOnce workflow is available for XLSX scanning.")
        if self.workflow_service is not None:
            validation = self.workflow_service.validate(workflow)
            if not validation.is_valid:
                raise SyncError("Workflow validation must pass before scanning linked XLSX files.")

        export_ref = self._export_ref(workflow, export_id)
        workbook = Path(export_ref.path).expanduser()
        if not workbook.is_file():
            raise SyncError(f"Linked XLSX file does not exist: {workbook}")

        try:
            read = self.reader.read_xlsx_rows(str(workbook))
        except Exception as exc:
            raise SyncError(f"Could not read linked XLSX: {exc}") from exc

        revision, output_revision = self._select_baseline(
            workflow.workflow_id,
            export_ref.export_id,
            read.rows,
        )
        if revision is None or output_revision is None:
            raise SyncError(
                "No completed AtOnce export revision matches this linked XLSX. "
                "Export the workbook from AtOnce before scanning edits."
            )

        comparison = compare_workbook_rows(
            output_revision.baseline_rows,
            read.rows,
            read.fields,
            workflow.workflow_id,
            export_ref.export_id,
            revision.revision_number,
        )

        source_refs = {item.layer_id: item for item in workflow.source_layers if item.layer_id}
        source_cache = {}
        changes = []
        for raw in comparison.changes:
            if raw.unresolved or raw.field_name == ROW_FIELD:
                source_ref = source_refs.get(raw.source_layer_id)
                changes.append(
                    DetectedChange(
                        source_layer_id=raw.source_layer_id,
                        source_feature_key=raw.source_key,
                        field_name=raw.field_name,
                        old_value=raw.baseline_value,
                        new_value=raw.workbook_value,
                        disposition=ChangeDisposition.UNRESOLVED,
                        reason=raw.reason or "Spreadsheet row cannot be resolved safely.",
                        source_layer_name=source_ref.name if source_ref else None,
                        export_id=export_ref.export_id,
                        revision_number=revision.revision_number,
                    )
                )
                continue

            source_ref = source_refs.get(raw.source_layer_id)
            if source_ref is None:
                changes.append(
                    self._change(
                        raw,
                        export_ref.export_id,
                        revision.revision_number,
                        ChangeDisposition.UNRESOLVED,
                        "The originating source layer is no longer registered in this workflow.",
                    )
                )
                continue

            cache_key = (raw.source_layer_id, raw.source_key)
            if cache_key not in source_cache:
                source_cache[cache_key] = self.reader.source_feature_state(*cache_key)
            source_state = source_cache[cache_key]
            if source_state is None:
                changes.append(
                    self._change(
                        raw,
                        export_ref.export_id,
                        revision.revision_number,
                        ChangeDisposition.UNRESOLVED,
                        "The exact source feature could not be resolved from immutable AtOnce lineage.",
                        source_layer_name=source_ref.name,
                    )
                )
                continue

            source_field = source_field_for(
                workflow,
                raw.source_layer_id,
                raw.field_name,
                source_state.values.keys(),
            )
            if source_field is None:
                changes.append(
                    self._change(
                        raw,
                        export_ref.export_id,
                        revision.revision_number,
                        ChangeDisposition.DERIVED,
                        "This exported field is protected, derived, or not a direct attribute of the originating source.",
                        source_layer_name=source_state.layer_name,
                    )
                )
                continue

            current_source = source_state.values.get(source_field)
            if not snapshot_value_equal(raw.baseline_typed, current_source):
                changes.append(
                    self._change(
                        raw,
                        export_ref.export_id,
                        revision.revision_number,
                        ChangeDisposition.CONFLICT,
                        "Source and spreadsheet both changed since the export baseline; AtOnce will not choose a winner.",
                        source_field_name=source_field,
                        current_source_value=current_source,
                        source_layer_name=source_state.layer_name,
                    )
                )
                continue

            changes.append(
                self._change(
                    raw,
                    export_ref.export_id,
                    revision.revision_number,
                    ChangeDisposition.WRITABLE,
                    "Direct source-backed attribute; source still matches the export baseline.",
                    source_field_name=source_field,
                    current_source_value=current_source,
                    source_layer_name=source_state.layer_name,
                )
            )

        changes.sort(
            key=lambda item: (
                item.source_layer_name or item.source_layer_id,
                item.source_feature_key,
                item.field_name,
            )
        )
        return ChangeScanResult(
            workflow_id=workflow.workflow_id,
            export_id=export_ref.export_id,
            export_name=export_ref.name,
            path=str(workbook),
            revision_number=revision.revision_number,
            scanned_rows=comparison.scanned_rows,
            baseline_rows=comparison.baseline_rows,
            changes=changes,
        )

    def preview(self, changes):
        """Return classified changes for review rendering without mutation."""
        if isinstance(changes, ChangeScanResult):
            return list(changes.changes)
        return list(changes)

    def apply(
        self,
        scan_result,
        approved_changes,
        *,
        selected_edge_ids=None,
    ) -> SyncApplyResult:
        """Apply explicit Writable approvals, refresh downstream and persist audit."""

        if selected_edge_ids is not None:
            # Keep the fully accepted v0.1 transaction below unchanged for the
            # default path. The selective adapter shares this service's exact
            # validation, writer, archive, rollback, and audit collaborators.
            from .selective_sync_service import SelectiveSyncService

            return SelectiveSyncService._apply_selected(
                self,
                scan_result,
                approved_changes,
                selected_edge_ids,
            )

        if not isinstance(scan_result, ChangeScanResult):
            raise SyncError("Approved sync requires the authoritative ChangeScanResult.")
        if self.propagation_service is None or self.export_service is None:
            raise SyncError("Source sync services are not fully configured.")
        if self.writer is None:
            from ..infrastructure.source_writer import SourceWriter

            self.writer = SourceWriter()

        workflow = self.store.load_workflow(scan_result.workflow_id)
        if workflow is None:
            raise SyncError("The scanned workflow is no longer registered.")
        if self.workflow_service is not None:
            validation = self.workflow_service.validate(workflow)
            if not validation.is_valid:
                raise SyncError("Workflow validation must pass immediately before source sync.")

        # Re-read the workbook and source immediately before planning the write. Both
        # the complete reviewed change set and the selected approvals must still be
        # authoritative; otherwise a newer, unreviewed edit could be archived/lost.
        try:
            fresh_scan = self.detect_changes(scan_result.export_id, scan_result.workflow_id)
        except Exception as exc:
            raise SyncError(f"Approval revalidation failed; rescan before applying: {exc}") from exc
        if fresh_scan.revision_number != scan_result.revision_number:
            raise SyncError("The linked workbook revision changed after scan; rescan before applying.")
        if scan_changes_signature(fresh_scan.changes) != scan_changes_signature(scan_result.changes):
            raise SyncError(
                "The workbook/source change set changed after scan; rescan before applying."
            )

        try:
            proposals = plan_approved_changes(fresh_scan.changes, approved_changes)
        except Exception as exc:
            raise SyncError(
                f"Approval is stale or no longer Writable; rescan before applying: {exc}"
            ) from exc

        # Layer C is a derived product but may contain manual user edits. Those edits
        # must be reviewed/rebuilt before source mutation so a later refresh cannot
        # silently destroy them or force an avoidable source rollback.
        check_divergence = getattr(self.propagation_service, "check_derived_divergence", None)
        if callable(check_divergence):
            try:
                divergence = check_divergence(workflow.workflow_id)
            except Exception as exc:
                raise SyncError(
                    f"Could not verify Layer C before source sync; no source data changed: {exc}"
                ) from exc
            if divergence is not None:
                raise SyncError(
                    "Layer C has unreviewed manual differences. Review or rebuild Layer C before "
                    f"source sync; no source data changed. {divergence.summary()}"
                )

        try:
            plan = self.writer.preflight(proposals)
        except Exception as exc:
            raise SyncError(f"Source sync preflight failed: {exc}") from exc

        if any(item.export_id != fresh_scan.export_id for item in proposals):
            raise SyncError("Approved changes do not belong to the scanned linked export.")
        if any(item.revision_number != fresh_scan.revision_number for item in proposals):
            raise SyncError("Approved changes do not belong to the scanned export revision.")

        sync_id = str(uuid4())
        try:
            archive = plan_workbook_archive(
                fresh_scan.path,
                sync_id,
                fresh_scan.revision_number,
            )
        except Exception as exc:
            raise SyncError(f"Returned workbook cannot be preserved safely: {exc}") from exc

        audit = self._pending_audit(sync_id, fresh_scan, plan.changes, archive.archive_path)
        try:
            self.store.save_sync_audit(audit)
        except Exception as exc:
            raise SyncError(f"Could not persist pending sync audit; no source data changed: {exc}") from exc

        receipt = None
        archive_staged = False
        source_applied = False
        refresh_completed = False
        refresh_revision_number = None
        try:
            archive.stage()
            archive_staged = True
            receipt = self.writer.apply(plan)
            source_applied = True

            self.propagation_service.rebuild_layer_c(workflow.workflow_id)
            refresh = self.export_service.export_downstream(workflow.workflow_id)
            refresh_completed = True
            refresh_revision_number = refresh.revision.revision_number
            archive.keep()

            audit.status = "complete"
            audit.completed_at = datetime.now(timezone.utc).isoformat()
            audit.refresh_revision_number = refresh_revision_number
            audit.message = (
                f"Applied {len(plan.changes)} approved source change(s) and refreshed "
                f"downstream revision {refresh_revision_number}."
            )
            for item in audit.changes:
                item.result = "applied"
                item.message = "Source write and downstream refresh completed."
            self.store.save_sync_audit(audit)
            return SyncApplyResult(
                sync_id=sync_id,
                applied_changes=len(plan.changes),
                refresh_revision_number=refresh_revision_number,
                returned_workbook_archive=archive.archive_path,
            )
        except Exception as exc:
            # Once the source write AND downstream export revision have both completed,
            # rolling only the sources back would make the already-persisted export
            # revision inconsistent. Preserve the consistent source/output state and
            # leave the existing pending audit as the durable warning instead.
            if refresh_completed:
                audit.status = "audit_finalize_failed"
                audit.completed_at = datetime.now(timezone.utc).isoformat()
                audit.refresh_revision_number = refresh_revision_number
                audit.message = (
                    f"Source write and downstream revision {refresh_revision_number} completed, "
                    f"but sync audit finalization failed: {exc}. Do not repeat this sync blindly."
                )
                for item in audit.changes:
                    item.result = "applied_unfinalized"
                    item.message = audit.message
                try:
                    self.store.save_sync_audit(audit)
                except Exception:
                    pass
                raise SyncError(
                    "Source changes and downstream refresh completed, but audit finalization failed. "
                    "The source/output state is consistent; inspect the pending audit before retrying."
                ) from exc

            recovery = []
            recovery_ok = not bool(getattr(exc, "recovery_incomplete", False))
            if not recovery_ok:
                recovery.append("source writer reported incomplete cross-layer recovery")

            if source_applied and receipt is not None:
                try:
                    self.writer.restore(receipt)
                    recovery.append("source before-images restored")
                except Exception as recovery_exc:
                    recovery_ok = False
                    recovery.append(f"source recovery failed: {recovery_exc}")

                if recovery_ok:
                    try:
                        self.propagation_service.rebuild_layer_c(workflow.workflow_id)
                        recovery.append("Layer C rebuilt from restored sources")
                    except Exception as recovery_exc:
                        recovery_ok = False
                        recovery.append(f"Layer C recovery failed: {recovery_exc}")

            if archive_staged:
                original = Path(archive.original_path)
                if not original.exists():
                    try:
                        archive.restore()
                        recovery.append("returned workbook restored")
                    except Exception as recovery_exc:
                        recovery_ok = False
                        recovery.append(f"workbook recovery failed: {recovery_exc}")
                else:
                    recovery.append("returned workbook archive retained")

            audit.status = "failed" if recovery_ok else "recovery_failed"
            audit.completed_at = datetime.now(timezone.utc).isoformat()
            audit.message = f"Sync failed: {exc}. Recovery: {'; '.join(recovery) or 'not required'}"
            for item in audit.changes:
                item.result = audit.status
                item.message = audit.message
            try:
                self.store.save_sync_audit(audit)
            except Exception:
                # The pending audit already exists. Never hide the original failure
                # merely because final audit annotation also failed.
                pass

            if recovery_ok:
                raise SyncError(
                    f"Approved sync failed and prior source/workbook state was restored: {exc}"
                ) from exc
            raise SyncError(
                f"Approved sync failed and recovery needs manual attention: {exc}. "
                f"{' | '.join(recovery)}"
            ) from exc

    @staticmethod
    def _pending_audit(sync_id, scan_result, prepared_changes, archive_path):
        return SyncAuditRecord(
            sync_id=sync_id,
            workflow_id=scan_result.workflow_id,
            export_id=scan_result.export_id,
            source_revision_number=scan_result.revision_number,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
            returned_workbook_archive=archive_path,
            message="Approved sync preflight passed; source mutation not yet complete.",
            changes=[
                SyncAuditChange(
                    source_layer_id=item.layer_id,
                    source_layer_name=item.layer_name,
                    source_feature_key=item.source_key,
                    source_field_name=item.field_name,
                    before_value=normalize_snapshot_value(item.before_value),
                    after_value=normalize_snapshot_value(item.after_value),
                )
                for item in prepared_changes
            ],
        )

    @staticmethod
    def _change(
        raw,
        export_id,
        revision_number,
        disposition,
        reason,
        source_field_name=None,
        current_source_value=None,
        source_layer_name=None,
    ):
        return DetectedChange(
            source_layer_id=raw.source_layer_id,
            source_feature_key=raw.source_key,
            field_name=raw.field_name,
            old_value=raw.baseline_value,
            new_value=raw.workbook_value,
            disposition=disposition,
            reason=reason,
            source_field_name=source_field_name,
            current_source_value=current_source_value,
            source_layer_name=source_layer_name,
            export_id=export_id,
            revision_number=revision_number,
        )

    @staticmethod
    def _export_ref(workflow, export_id):
        if export_id:
            match = next((item for item in workflow.exports if item.export_id == export_id), None)
            if match is None:
                raise SyncError(f"Unknown linked export id: {export_id}")
            return match
        if len(workflow.exports) == 1:
            return workflow.exports[0]
        raise SyncError("Choose which linked XLSX export to scan.")

    def _select_baseline(self, workflow_id, export_id, workbook_rows):
        revisions = self.store.load_export_revisions(workflow_id)
        if not revisions:
            return None, None

        candidates = workbook_revision_candidates(workbook_rows)
        if len(candidates) == 1:
            wanted = candidates[0]
            for revision in reversed(revisions):
                if revision.revision_number != wanted:
                    continue
                output = next((item for item in revision.outputs if item.export_id == export_id), None)
                if output is not None:
                    return revision, output

        # Mixed/missing/tampered revision metadata cannot choose an authoritative row
        # baseline. Use the newest completed baseline only to classify those rows as
        # unresolved metadata mismatches; never map them by position.
        for revision in reversed(revisions):
            output = next((item for item in revision.outputs if item.export_id == export_id), None)
            if output is not None:
                return revision, output
        return None, None
