"""Generation/binding-aware extension of the approved XLSX sync service."""

from datetime import datetime, timezone

from ..core.change_detection import workbook_revision_candidates
from ..core.export_snapshot import normalize_snapshot_value
from ..models.sync_audit import SyncAuditChange, SyncAuditRecord
from .sync_service import SyncService


class RecoveryAwareSyncService(SyncService):
    """Keep old revisions readable while preventing cross-generation write-back."""

    def _select_baseline(self, workflow_id, export_id, workbook_rows):
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            return None, None
        revisions = [
            item
            for item in self.store.load_export_revisions(workflow_id)
            if item.lineage_generation == workflow.lineage_generation
        ]
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

        # Preserve the existing safety behavior for mixed/tampered metadata, but
        # never cross back into an older source-authority generation.
        for revision in reversed(revisions):
            output = next((item for item in revision.outputs if item.export_id == export_id), None)
            if output is not None:
                return revision, output
        return None, None

    @staticmethod
    def _pending_audit(sync_id, scan_result, prepared_changes, archive_path):
        """Audit immutable source lineage even when the QGIS binding was relinked."""

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
                    source_layer_id=item.source_lineage_id or item.layer_id,
                    source_layer_name=item.layer_name,
                    source_feature_key=item.source_key,
                    source_field_name=item.field_name,
                    before_value=normalize_snapshot_value(item.before_value),
                    after_value=normalize_snapshot_value(item.after_value),
                )
                for item in prepared_changes
            ],
        )
