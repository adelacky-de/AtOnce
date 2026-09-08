"""G5 selective source-sync orchestration for the proven v0.1 graph adapter."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from ..core.export_snapshot import normalize_snapshot_value
from ..core.legacy_graph_execution import (
    SelectiveExecutionError,
    plan_legacy_execution,
)
from ..core.sync_planning import plan_approved_changes, scan_changes_signature
from ..infrastructure.workbook_archive import plan_workbook_archive
from ..models.change import ChangeScanResult
from ..models.graph_run_state import GraphRunState
from ..models.sync_audit import SyncAuditChange, SyncAuditRecord
from .sync_service import SyncApplyResult, SyncError, SyncService


# Compatibility for integrations that imported the short-lived G5 result name.
SelectiveSyncApplyResult = SyncApplyResult


class SelectiveSyncService(SyncService):
    """Preserve the full-refresh SyncService default and add explicit G5 selection."""

    def apply(self, scan_result, approved_changes, selected_edge_ids=None):
        # Existing callers/tests retain the accepted all-output v0.1 behavior.
        if selected_edge_ids is None:
            return super().apply(scan_result, approved_changes)
        return self._apply_selected(scan_result, approved_changes, selected_edge_ids)

    def _apply_selected(self, scan_result, approved_changes, selected_edge_ids):
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

        changed_source_ids = {item.source_layer_id for item in proposals}
        try:
            execution = plan_legacy_execution(
                workflow,
                changed_source_ids,
                selected_edge_ids,
            )
        except SelectiveExecutionError as exc:
            raise SyncError(f"Selected propagation cannot run safely: {exc}") from exc

        # Manual Layer C divergence matters only when this run intends to replace
        # Layer C. If the user explicitly keeps the change upstream, Layer C stays
        # untouched and is recorded stale by choice instead.
        if execution.refresh_layer_c:
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
                        f"this selected propagation; no source data changed. {divergence.summary()}"
                    )

        try:
            write_plan = self.writer.preflight(proposals)
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

        audit = SelectiveSyncService._pending_selective_audit(
            sync_id,
            fresh_scan,
            write_plan.changes,
            archive.archive_path,
            execution,
        )
        try:
            # Persist exact selected/skipped/stale IDs before mutation so a later
            # crash/finalization failure still leaves enough evidence to explain
            # what the user approved for this run.
            self.store.save_sync_audit(audit)
        except Exception as exc:
            raise SyncError(f"Could not persist pending sync audit; no source data changed: {exc}") from exc

        receipt = None
        archive_staged = False
        source_applied = False
        output_revision_completed = False
        execution_recorded = False
        refresh_revision_number = None
        try:
            archive.stage()
            archive_staged = True
            receipt = self.writer.apply(write_plan)
            source_applied = True

            # Full-sync historically moves the returned workbook into an audit
            # archive and immediately recreates the registered path by re-exporting
            # it. Selective propagation may intentionally skip that XLSX edge. In
            # that case the user's edited workbook is itself the chosen stale
            # delivery and must remain byte-for-byte at its original path while an
            # independent archive copy remains as audit evidence.
            if fresh_scan.export_id not in execution.export_ids:
                archive.preserve_original_copy()

            if execution.refresh_layer_c:
                self.propagation_service.rebuild_layer_c(workflow.workflow_id)

            if execution.refresh_gpkg or execution.export_ids or execution.delivery_ids:
                refresh = self.export_service.export_downstream(
                    workflow.workflow_id,
                    refresh_gpkg=execution.refresh_gpkg,
                    export_ids=execution.export_ids,
                    delivery_ids=execution.delivery_ids,
                )
                output_revision_completed = True
                refresh_revision_number = refresh.revision.revision_number

            graph_state = GraphRunState(
                workflow_id=workflow.workflow_id,
                run_id=sync_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                reason="source-sync",
                changed_node_ids=list(execution.propagation.changed_node_ids),
                selected_edge_ids=list(execution.propagation.selected_edge_ids),
                skipped_edge_ids=list(execution.propagation.skipped_edge_ids),
                refreshed_node_ids=list(execution.refreshed_node_ids),
                stale_node_ids=list(execution.propagation.stale_by_choice_node_ids),
                export_revision_number=refresh_revision_number,
                source_sync_id=sync_id,
            )
            self.store.save_graph_run_state(graph_state)
            execution_recorded = True
            archive.keep()

            audit.status = "complete"
            audit.completed_at = datetime.now(timezone.utc).isoformat()
            audit.refresh_revision_number = refresh_revision_number
            stale_count = len(execution.propagation.stale_by_choice_node_ids)
            refreshed_count = len(execution.refreshed_node_ids - execution.propagation.changed_node_ids)
            audit.message = (
                f"Applied {len(write_plan.changes)} approved source change(s); "
                f"refreshed {refreshed_count} downstream node(s); "
                f"{stale_count} node(s) remain stale by choice."
            )
            for item in audit.changes:
                item.result = "applied"
                item.message = audit.message
            self.store.save_sync_audit(audit)
            return SyncApplyResult(
                sync_id=sync_id,
                applied_changes=len(write_plan.changes),
                refresh_revision_number=refresh_revision_number,
                returned_workbook_archive=archive.archive_path,
                stale_node_ids=tuple(sorted(execution.propagation.stale_by_choice_node_ids)),
            )
        except Exception as exc:
            # A completed output revision cannot be made consistent by rolling only
            # sources back. Likewise, once graph-run evidence is persisted the run
            # is durably recorded as executed. Preserve that consistent state and
            # surface finalization failure instead of repeating mutation blindly.
            if output_revision_completed or execution_recorded:
                audit.status = "audit_finalize_failed"
                audit.completed_at = datetime.now(timezone.utc).isoformat()
                audit.refresh_revision_number = refresh_revision_number
                audit.message = (
                    "Selected source/downstream execution completed, but final evidence/audit "
                    f"persistence failed: {exc}. Do not repeat this sync blindly."
                )
                for item in audit.changes:
                    item.result = "applied_unfinalized"
                    item.message = audit.message
                try:
                    self.store.save_sync_audit(audit)
                except Exception:
                    pass
                raise SyncError(
                    "Selected propagation completed, but audit finalization failed. "
                    "Inspect graph-run/audit evidence before retrying."
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

                # Only rebuild a derived layer that this failed selected run had
                # actually replaced. A skipped Layer C must remain untouched.
                if recovery_ok and execution.refresh_layer_c:
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
                    recovery.append("returned workbook + audit archive retained")

            audit.status = "failed" if recovery_ok else "recovery_failed"
            audit.completed_at = datetime.now(timezone.utc).isoformat()
            audit.message = f"Selective sync failed: {exc}. Recovery: {'; '.join(recovery) or 'not required'}"
            for item in audit.changes:
                item.result = audit.status
                item.message = audit.message
            try:
                self.store.save_sync_audit(audit)
            except Exception:
                pass

            if recovery_ok:
                raise SyncError(
                    f"Selected propagation failed and prior source/workbook state was restored: {exc}"
                ) from exc
            raise SyncError(
                f"Selected propagation failed and recovery needs manual attention: {exc}. "
                f"{' | '.join(recovery)}"
            ) from exc

    @staticmethod
    def _pending_selective_audit(sync_id, scan_result, prepared_changes, archive_path, execution):
        return SyncAuditRecord(
            sync_id=sync_id,
            workflow_id=scan_result.workflow_id,
            export_id=scan_result.export_id,
            source_revision_number=scan_result.revision_number,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
            returned_workbook_archive=archive_path,
            message=(
                "Approved sync preflight passed; source mutation not yet complete. "
                f"Selected {len(execution.propagation.selected_edge_ids)} graph edge(s); "
                f"{len(execution.propagation.skipped_edge_ids)} reached edge(s) are skipped."
            ),
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
            selected_edge_ids=sorted(execution.propagation.selected_edge_ids),
            skipped_edge_ids=sorted(execution.propagation.skipped_edge_ids),
            refreshed_node_ids=sorted(execution.refreshed_node_ids),
            stale_node_ids=sorted(execution.propagation.stale_by_choice_node_ids),
        )
