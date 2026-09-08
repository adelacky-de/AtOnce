"""AtOnce plugin entrypoint extended with explicit recovery workflows."""

from qgis.PyQt.QtWidgets import QAction, QDialog, QMessageBox

from .infrastructure.binding_project import SourceBindingProject
from .infrastructure.change_reader import ChangeReader
from .infrastructure.source_writer import SourceWriter
from .infrastructure.trace_navigator import QgisTraceNavigator
from .plugin import AtOncePlugin
from .services.export_service import ExportService
from .services.propagation_service import PropagationService
from .services.recovery_service import RecoveryError, RecoveryService
from .services.recovery_sync_service import RecoveryAwareSyncService
from .services.recovery_workflow_service import RecoveryWorkflowService
from .models.workflow import EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN
from .ui.recovery_dialog import SourceRecoveryDialog, WorkbookRecoveryDialog


class RecoveryAtOncePlugin(AtOncePlugin):
    """Production v0.1 plugin with lineage-preserving recovery bindings."""

    def __init__(self, iface):
        super().__init__(iface)

        self.binding_project = SourceBindingProject(self.qgis.project, self.store)
        self.change_reader = ChangeReader(self.binding_project)
        self.source_writer = SourceWriter(self.binding_project)
        self.trace_navigator = QgisTraceNavigator(self.iface, self.binding_project)
        self.workflow_service = RecoveryWorkflowService(self.store, self.qgis)
        self.propagation_service = PropagationService(
            self.store,
            self.qgis,
            self.workflow_service,
        )
        self.export_service = ExportService(
            self.store,
            self.qgis,
            self.workflow_service,
        )
        self.sync_service = RecoveryAwareSyncService(
            self.store,
            self.workflow_service,
            reader=self.change_reader,
            writer=self.source_writer,
            propagation_service=self.propagation_service,
            export_service=self.export_service,
        )
        self.recovery_service = RecoveryService(
            self.store,
            self.qgis,
            self.change_reader,
            propagation_service=self.propagation_service,
        )

        self.relink_source_action = None
        self.replace_source_action = None
        self.relink_workbook_action = None

    def initGui(self):  # noqa: N802 - QGIS plugin API
        super().initGui()

        self.relink_source_action = QAction("Relink Source…", self.iface.mainWindow())
        self.relink_source_action.setToolTip(
            "Reconnect the same authoritative source after QGIS/path rebinding while preserving immutable lineage."
        )
        self.relink_source_action.triggered.connect(self._on_relink_source_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.relink_source_action)

        self.replace_source_action = QAction("Replace Source…", self.iface.mainWindow())
        self.replace_source_action.setToolTip(
            "Explicitly replace an authoritative source and start a new lineage generation."
        )
        self.replace_source_action.triggered.connect(self._on_replace_source_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.replace_source_action)

        self.relink_workbook_action = QAction("Relink Workbook…", self.iface.mainWindow())
        self.relink_workbook_action.setToolTip(
            "Reconnect a moved AtOnce XLSX only after its embedded metadata and row lineage validate."
        )
        self.relink_workbook_action.triggered.connect(self._on_relink_workbook_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.relink_workbook_action)

    def unload(self):
        for name in (
            "relink_source_action",
            "replace_source_action",
            "relink_workbook_action",
        ):
            action = getattr(self, name, None)
            if action is not None:
                self.iface.removePluginVectorMenu(self.MENU_NAME, action)
                action.deleteLater()
                setattr(self, name, None)
        super().unload()

    def _reload_workflow(self):
        """Render only current-generation revisions as active sync baselines."""

        super()._reload_workflow()
        workflow = self.workflow_service.load_active()
        if workflow is None:
            return
        if workflow.effective_dependency_graph_origin() in {
            EXPLICIT_GRAPH_ORIGIN,
            FREEFORM_GRAPH_ORIGIN,
        }:
            return

        current_revisions = [
            item
            for item in self.store.load_export_revisions(workflow.workflow_id)
            if item.lineage_generation == workflow.lineage_generation
        ]
        latest = current_revisions[-1] if current_revisions else None

        if self.change_scan_panel is not None:
            self.change_scan_panel.set_exports(
                workflow.exports,
                has_completed_revision=latest is not None,
            )
        if self.dock is not None:
            if latest is None:
                historical = self.store.latest_export_revision(workflow.workflow_id)
                if historical is None:
                    self.dock.outputs_meta.setText("No completed downstream revision")
                else:
                    self.dock.outputs_meta.setText(
                        f"Source generation {workflow.lineage_generation} needs rebuild/export · "
                        f"older revision {historical.revision_number} kept as history"
                    )
            else:
                rows = sum(output.row_count for output in latest.outputs)
                self.dock.outputs_meta.setText(
                    f"Revision {latest.revision_number} complete · generation {workflow.lineage_generation} · "
                    f"{rows} XLSX row(s)"
                )

    def _on_relink_source_requested(self):
        workflow = self.workflow_service.load_active()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        dialog = SourceRecoveryDialog(
            workflow,
            self.qgis.vector_layers(),
            mode="relink",
            parent=self.iface.mainWindow(),
        )
        if dialog.exec_() != QDialog.Accepted:
            return

        try:
            result = self.recovery_service.relink_source(
                dialog.source_lineage_id,
                dialog.candidate_layer_id,
                workflow.workflow_id,
            )
        except RecoveryError as exc:
            self.iface.messageBar().pushCritical("AtOnce", f"Relink Source failed: {exc}")
            return

        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            f"Source relinked without changing historical identity. Verified "
            f"{result.historical_uuid_count} UUID(s) from the latest authoritative state; "
            f"candidate has {result.candidate_uuid_count} UUID(s).",
        )

    def _on_replace_source_requested(self):
        workflow = self.workflow_service.load_active()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        dialog = SourceRecoveryDialog(
            workflow,
            self.qgis.vector_layers(),
            mode="replace",
            parent=self.iface.mainWindow(),
        )
        if dialog.exec_() != QDialog.Accepted:
            return

        source = workflow.source_by_lineage_id(dialog.source_lineage_id)
        candidate = self.qgis.resolve_layer(dialog.candidate_layer_id)
        if source is None or candidate is None:
            self.iface.messageBar().pushCritical(
                "AtOnce", "Replacement selection is no longer available."
            )
            return

        confirm = QMessageBox(self.iface.mainWindow())
        confirm.setWindowTitle("Replace authoritative source")
        confirm.setIcon(QMessageBox.Warning)
        confirm.setText(
            f"Replace '{source.name}' with '{candidate.name()}' as a new source authority?"
        )
        confirm.setInformativeText(
            "This is NOT a relink. AtOnce will start a new source-lineage generation. "
            "Older exports/audits remain readable history but cannot be used for write-back. "
            "The current unchanged Layer C will be detached and must be rebuilt before export. "
            "No source attributes are changed by this confirmation itself."
        )
        confirm.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        confirm.setDefaultButton(QMessageBox.Cancel)
        if confirm.exec_() != QMessageBox.Yes:
            return

        try:
            result = self.recovery_service.replace_source(
                dialog.source_lineage_id,
                dialog.candidate_layer_id,
                workflow.workflow_id,
            )
        except RecoveryError as exc:
            self.iface.messageBar().pushCritical("AtOnce", f"Replace Source failed: {exc}")
            return

        if self.change_scan_panel is not None:
            self.change_scan_panel.clear()
        self._reload_workflow()

        message = (
            f"Source replaced. Lineage generation is now {result.lineage_generation}. "
            "Rebuild Layer C before creating new downstream outputs."
        )
        if not result.detached_old_derived:
            message += (
                " The replacement is already saved, but QGIS could not detach the previous Layer C. "
                "Remove that stale derived layer manually before rebuilding. Do not repeat Replace Source."
            )
        self.iface.messageBar().pushWarning("AtOnce", message)

    def _on_relink_workbook_requested(self):
        workflow = self.workflow_service.load_active()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        dialog = WorkbookRecoveryDialog(workflow, self.iface.mainWindow())
        if dialog.exec_() != QDialog.Accepted:
            return
        if not dialog.workbook_path:
            self.iface.messageBar().pushWarning("AtOnce", "Choose a workbook to relink.")
            return

        try:
            result = self.recovery_service.relink_workbook(
                dialog.export_id,
                dialog.workbook_path,
                workflow.workflow_id,
            )
        except RecoveryError as exc:
            self.iface.messageBar().pushCritical("AtOnce", f"Relink workbook failed: {exc}")
            return

        self._reload_workflow()
        if result.contains_changes:
            self.iface.messageBar().pushWarning(
                "AtOnce",
                f"Workbook relinked to revision {result.revision_number}, but it contains data differences. "
                "Scan/review those edits before any downstream refresh.",
            )
        else:
            self.iface.messageBar().pushSuccess(
                "AtOnce",
                f"Workbook relinked to completed revision {result.revision_number}. No data edits detected.",
            )
