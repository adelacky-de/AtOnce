"""QGIS plugin lifecycle and UI wiring."""

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDialog, QMessageBox

from .core.derived_diff import format_derived_diff
from .core.trace_planning import TracePlanningError, plan_trace_target
from .infrastructure.project_store import ProjectStore
from .infrastructure.qgis_gateway import QgisGateway
from .infrastructure.trace_navigator import QgisTraceNavigator, TraceNavigationError
from .models.change import ChangeDisposition
from .models.workflow import EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN
from .services.export_service import DerivedDivergenceError, ExportError, ExportService
from .services.graph_execution_service import GraphExecutionService, GraphExecutionServiceError
from .services.propagation_service import (
    PropagationDivergenceError,
    PropagationError,
    PropagationService,
)
from .services.source_identity_service import SourceIdentityError, SourceIdentityService
from .services.sync_service import SyncError, SyncService
from .services.workflow_service import WorkflowService
from .ui.change_scan_panel import ChangeScanPanel
from .ui.dock import AtOnceDockWidget
from .ui.explicit_filter_dialog import ExplicitFilterDialog
from .ui.explicit_chain_filter_dialog import ExplicitChainFilterDialog
from .ui.explicit_merge_dialog import ExplicitMergeDialog
from .ui.explicit_workflow_editor_dialog import ExplicitWorkflowEditorDialog
from .ui.guided_workflow_builder import WorkflowTypeChooserDialog
from .ui.standalone_explicit_workflow_dialog import StandaloneExplicitWorkflowDialog
from .ui.propagation_review_dialog import PropagationReviewDialog
from .ui.sync_history_panel import SyncHistoryPanel
from .ui.workflow_dialog import WorkflowRegistrationDialog

# Existing persisted workflow labels remain readable in the selector; the
# first-run chooser uses the user-facing name "Spreadsheet sync" instead.
LEGACY_WORKFLOW_COMPATIBILITY_LABEL = "Legacy A/B workflow"
EXPLICIT_WORKFLOW_COMPATIBILITY_LABELS = ("Filter workflow", "Merge workflow")
EXPLICIT_EXECUTION_ORIGINS = {EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN}


class AtOncePlugin:
    """Thin QGIS adapter responsible for UI wiring and service orchestration."""

    MENU_NAME = "AtOnce"

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.rebuild_action = None
        self.add_filtered_action = None
        self.add_chain_filter_action = None
        self.add_merged_action = None
        self.dock = None
        self.change_scan_panel = None
        self.history_panel = None
        self.store = ProjectStore()
        self.qgis = QgisGateway()
        self.trace_navigator = QgisTraceNavigator(self.iface)
        self.workflow_service = WorkflowService(self.store, self.qgis)
        self.propagation_service = PropagationService(
            self.store, self.qgis, self.workflow_service
        )
        self.export_service = ExportService(
            self.store, self.qgis, self.workflow_service
        )
        self.sync_service = SyncService(
            self.store,
            self.workflow_service,
            propagation_service=self.propagation_service,
            export_service=self.export_service,
        )
        self.graph_execution_service = GraphExecutionService(
            self.store,
            self.qgis,
            self.workflow_service,
            self.export_service,
        )
        self.source_identity_service = SourceIdentityService(self.qgis)

    def initGui(self):  # noqa: N802 - QGIS plugin API
        icon_path = Path(__file__).resolve().parent / "resources" / "icon.png"
        self.action = QAction(QIcon(str(icon_path)), "AtOnce", self.iface.mainWindow())
        self.action.setToolTip("Open AtOnce lineage and sync workspace")
        self.action.triggered.connect(self.open_dock)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

        self.rebuild_action = QAction("Rebuild Layer C", self.iface.mainWindow())
        self.rebuild_action.setToolTip("Rebuild the derived Layer C with stable lineage")
        self.rebuild_action.setEnabled(False)
        self.rebuild_action.triggered.connect(self._on_build_layer_c_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.rebuild_action)

        self.add_filtered_action = QAction(
            "Add filtered derived layer",
            self.iface.mainWindow(),
        )
        self.add_filtered_action.setToolTip(
            "Define one standalone explicit source FILTER workflow"
        )
        self.add_filtered_action.triggered.connect(self._on_add_filtered_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.add_filtered_action)

        self.add_chain_filter_action = QAction(
            "Add second filtered derived layer",
            self.iface.mainWindow(),
        )
        self.add_chain_filter_action.setToolTip(
            "Extend an explicit FILTER graph by one AtOnce-managed FILTER stage"
        )
        self.add_chain_filter_action.triggered.connect(self._on_add_chain_filter_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.add_chain_filter_action)

        self.add_merged_action = QAction(
            "Add merged derived layer",
            self.iface.mainWindow(),
        )
        self.add_merged_action.setToolTip(
            "Define one standalone explicit two-source MERGE workflow"
        )
        self.add_merged_action.triggered.connect(self._on_add_merged_requested)
        self.iface.addPluginToVectorMenu(self.MENU_NAME, self.add_merged_action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None

        if self.rebuild_action is not None:
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.rebuild_action)
            self.rebuild_action.deleteLater()
            self.rebuild_action = None

        if self.add_filtered_action is not None:
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.add_filtered_action)
            self.add_filtered_action.deleteLater()
            self.add_filtered_action = None

        if self.add_chain_filter_action is not None:
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.add_chain_filter_action)
            self.add_chain_filter_action.deleteLater()
            self.add_chain_filter_action = None

        if self.add_merged_action is not None:
            self.iface.removePluginVectorMenu(self.MENU_NAME, self.add_merged_action)
            self.add_merged_action.deleteLater()
            self.add_merged_action = None

        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self.change_scan_panel = None
        self.history_panel = None

    def open_dock(self):
        if self.dock is None:
            self.dock = AtOnceDockWidget(self.iface.mainWindow(), self.qgis)
            self.dock.sync_requested.connect(self._on_sync_requested)
            self.dock.refresh_requested.connect(self._on_refresh_requested)
            self.dock.plan_requested.connect(self._on_canvas_plan_requested)
            self.dock.register_requested.connect(self._on_register_requested)
            self.dock.authoring_requested.connect(self._on_authoring_requested)
            self.dock.builder.configuration_requested.connect(
                self._on_canvas_configuration_requested
            )
            self.dock.builder.message_requested.connect(self._on_canvas_message)
            self.dock.builder.result_requested.connect(self._on_canvas_result_requested)
            self.dock.workflow_selected.connect(self._on_workflow_selected)
            self.dock.validate_requested.connect(self._on_validate_requested)
            self.dock.trace_requested.connect(self._on_trace_requested)
            self.change_scan_panel = ChangeScanPanel(
                self.dock,
                self._on_scan_changes_requested,
                self._on_review_changes_requested,
            )
            self.history_panel = SyncHistoryPanel(self.dock)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        self._reload_workflow()
        self.dock.show()
        self.dock.raise_()

    def _reload_workflow(self):
        workflows = self.workflow_service.load_all()
        workflow = self.workflow_service.load_active()
        if self.dock is not None:
            self.dock.set_workflows(
                [
                    (
                        item.workflow_id,
                        f"{item.name} · {self.workflow_service.type_label(item)}",
                    )
                    for item in workflows
                ],
                workflow.workflow_id if workflow is not None else None,
            )
            self.dock.set_workflow(workflow)
        if workflow is None:
            if self.change_scan_panel is not None:
                self.change_scan_panel.set_exports([], False)
                self.change_scan_panel.clear()
            if self.history_panel is not None:
                self.history_panel.render([])
            self._update_downstream_actions(None, None)
            return

        validation = self.workflow_service.validate(workflow)
        latest = self.store.latest_export_revision(workflow.workflow_id)
        latest_graph = self.store.latest_graph_run_state(workflow.workflow_id)
        freshness = self.workflow_service.explicit_freshness(workflow)
        if self.dock is not None:
            self.dock.set_validation(validation)
            self.dock.set_run_evidence(workflow, latest, latest_graph, freshness)
        if self.change_scan_panel is not None:
            if workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
                self.change_scan_panel.clear()
                self.change_scan_panel.set_exports(
                    [],
                    False,
                    enabled=False,
                    disabled_message="Spreadsheet reverse sync is available on linked XLSX workflows.",
                )
            else:
                self.change_scan_panel.set_exports(
                    workflow.exports,
                    has_completed_revision=latest is not None,
                    enabled=True,
                )
        if self.history_panel is not None:
            if workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
                self.history_panel.render_workflow(
                    workflow,
                    [],
                    self.store.load_graph_run_states(workflow.workflow_id),
                    self.store.load_export_revisions(workflow.workflow_id),
                )
            else:
                self.history_panel.render_workflow(
                    workflow,
                    self.store.load_sync_audits(workflow.workflow_id),
                    [],
                    [],
                )
        self._update_downstream_actions(workflow, validation)

    def _active_workflow(self):
        return self.workflow_service.load_active()

    def _on_canvas_message(self, message):
        self.iface.messageBar().pushWarning("AtOnce", str(message))

    def _on_canvas_result_requested(self):
        workflow = self._active_workflow()
        if workflow is None or workflow.effective_dependency_graph_origin() not in EXPLICIT_EXECUTION_ORIGINS:
            self.iface.messageBar().pushInfo("AtOnce", "Plan an explicit workflow to create its result layer.")
            return
        derived = [
            node
            for node in workflow.effective_dependency_graph().nodes
            if node.kind.value == "derived"
        ]
        if not derived:
            self.iface.messageBar().pushInfo("AtOnce", "No explicit result node is configured yet.")
            return
        state = self.store.load_graph_node_materialization(workflow.workflow_id, derived[-1].node_id)
        layer = self.qgis.resolve_layer(state.layer_id) if state is not None else None
        if layer is None:
            self.iface.messageBar().pushInfo("AtOnce", "The result is not currently materialized in QGIS.")
            return
        self.iface.setActiveLayer(layer)

    def _on_canvas_configuration_requested(self):
        """Persist a canvas edit without running or touching QGIS outputs."""

        if self.dock is None:
            return
        definition = self.dock.builder.definition()
        if definition is None:
            self.iface.messageBar().pushWarning(
                "AtOnce", "Choose a source and operation before saving this canvas configuration."
            )
            return
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Canvas configuration not saved")
            return
        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce", "Canvas configuration saved. Use > Plan to create the result."
        )

    def _on_canvas_plan_requested(self):
        """Save the current canvas draft, then use the existing explicit executor."""

        if self.dock is None:
            return
        definition = self.dock.builder.definition()
        if definition is None:
            workflow = self._active_workflow()
            if workflow is not None and workflow.effective_dependency_graph_origin() not in EXPLICIT_EXECUTION_ORIGINS:
                self._on_refresh_requested()
                return
            self.iface.messageBar().pushWarning(
                "AtOnce", "Choose a source and operation before planning this workflow."
            )
            return
        validation = self.workflow_service.validate(definition)
        if not validation.is_valid:
            self.dock.set_validation(validation)
            self._show_validation_dialog(validation, title="Workflow cannot be planned")
            return
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Workflow cannot be planned")
            return
        self._reload_workflow()
        self._on_run_explicit_requested(definition)

    def _on_workflow_selected(self, workflow_id):
        try:
            self.workflow_service.set_active(workflow_id)
        except ValueError as exc:
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return
        self._reload_workflow()

    def _on_authoring_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self._on_register_requested()
            return
        if workflow.effective_dependency_graph_origin() not in EXPLICIT_EXECUTION_ORIGINS:
            chooser = WorkflowTypeChooserDialog(False, self.iface.mainWindow())
            if chooser.exec_() != QDialog.Accepted:
                return
            if chooser.choice == WorkflowTypeChooserDialog.FILTER:
                self._on_add_filtered_requested()
            elif chooser.choice == WorkflowTypeChooserDialog.MERGE:
                self._on_add_merged_requested()
            return
        if ExplicitChainFilterDialog.eligible(workflow):
            self._on_add_chain_filter_requested()
            return
        self.iface.messageBar().pushInfo(
            "AtOnce", "This workflow has no additional G8a authoring step available."
        )

    def _on_edit_workflow_requested(self, workflow=None):
        workflow = workflow or self._active_workflow()
        if workflow is None:
            self._on_register_requested()
            return
        if workflow.effective_dependency_graph_origin() not in EXPLICIT_EXECUTION_ORIGINS:
            self._on_register_requested()
            return

        dialog = ExplicitWorkflowEditorDialog(
            workflow,
            self.workflow_service.type_label(workflow),
            qgis_gateway=self.qgis,
            parent=self.iface.mainWindow(),
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        definition = dialog.definition()
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Workflow changes not saved")
            return
        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Workflow configuration saved. Run the active workflow to apply execution changes.",
        )

    def _on_add_filtered_requested(self):
        dialog = StandaloneExplicitWorkflowDialog(
            self.qgis, "filter", self.iface.mainWindow()
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        definition = dialog.definition()
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Filtered layer not saved")
            return
        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Standalone FILTER workflow saved. Run the active workflow to execute it.",
        )

    def _on_add_merged_requested(self):
        dialog = StandaloneExplicitWorkflowDialog(
            self.qgis, "merge", self.iface.mainWindow()
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        definition = dialog.definition()
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Merged layer not saved")
            return
        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Standalone MERGE workflow saved. Run the active workflow to execute it.",
        )

    def _on_add_chain_filter_requested(self):
        source_workflow = self._active_workflow()
        if source_workflow is None or not ExplicitChainFilterDialog.eligible(source_workflow):
            self.iface.messageBar().pushWarning(
                "AtOnce",
                "Create one valid explicit FILTER derived layer before adding its second FILTER stage.",
            )
            return
        dialog = ExplicitChainFilterDialog(
            source_workflow,
            self.iface.mainWindow(),
            qgis_gateway=self.qgis,
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        definition = dialog.definition()
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Second filtered layer not saved")
            return
        self._reload_workflow()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Second FILTER configuration saved. Run the active workflow to execute it.",
        )

    def _on_register_requested(self):
        existing = self._active_workflow()
        if existing is not None and existing.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            self._on_edit_workflow_requested(existing)
            return
        if existing is None:
            chooser = WorkflowTypeChooserDialog(True, self.iface.mainWindow())
            if chooser.exec_() != QDialog.Accepted:
                return
            if chooser.choice == WorkflowTypeChooserDialog.FILTER:
                self._on_add_filtered_requested()
                return
            if chooser.choice == WorkflowTypeChooserDialog.MERGE:
                self._on_add_merged_requested()
                return
            if chooser.choice != WorkflowTypeChooserDialog.SPREADSHEET:
                return
        dialog = WorkflowRegistrationDialog(existing, self.iface.mainWindow())
        if dialog.exec_() != QDialog.Accepted:
            return

        definition = dialog.definition()
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Workflow not saved")
            if self.dock is not None:
                self.dock.set_validation(result)
            self._update_downstream_actions(definition, result)
            return

        if self.dock is not None:
            self.dock.set_workflow(definition)
            self.dock.set_validation(result)
        if self.change_scan_panel is not None:
            self.change_scan_panel.clear()
            self.change_scan_panel.set_exports(definition.exports, False)
        self._reload_workflow()
        self._update_downstream_actions(definition, result)
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Workflow registered in the QGIS project. Save the project to persist it to disk.",
        )
        if result.issues:
            self._show_validation_dialog(result, title="Workflow saved with warnings")

    def _on_validate_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        result = self.workflow_service.validate(workflow)
        if self.dock is not None:
            self.dock.set_validation(result)
        self._update_downstream_actions(workflow, result)

        if result.is_valid and not result.issues:
            self.iface.messageBar().pushSuccess("AtOnce", "Workflow validation passed.")
        else:
            self._show_validation_dialog(result, title=result.summary)

    def _on_sync_requested(self):
        self._foundation_message(
            "Select Writable rows in Changes and use Review changes. Sync is never automatic."
        )

    def _on_scan_changes_requested(self, export_id):
        workflow = self._active_workflow()
        if workflow is None or workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            self.iface.messageBar().pushInfo(
                "AtOnce", "Spreadsheet reverse sync is available on linked XLSX workflows."
            )
            return
        try:
            result = self.sync_service.detect_changes(export_id, workflow.workflow_id)
        except SyncError as exc:
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        if self.change_scan_panel is not None:
            self.change_scan_panel.render(result)
        if self.dock is not None:
            self.dock.tabs.setCurrentIndex(1)
        counts = result.counts
        self.iface.messageBar().pushInfo(
            "AtOnce",
            f"Scanned revision {result.revision_number}: {len(result.changes)} change(s) · "
            f"{counts[ChangeDisposition.WRITABLE]} writable. No source data was changed.",
        )

    def _on_review_changes_requested(self, scan_result, selected_changes):
        if not selected_changes:
            return

        workflow = self._active_workflow()
        if workflow is None or workflow.workflow_id != scan_result.workflow_id:
            self.iface.messageBar().pushCritical(
                "AtOnce", "The reviewed workflow is no longer the active registered workflow."
            )
            return

        changed_source_ids = {
            change.source_layer_id for change in selected_changes if change.source_layer_id
        }
        propagation = PropagationReviewDialog(
            workflow,
            changed_source_ids,
            self.iface.mainWindow(),
        )
        if propagation.exec_() != QDialog.Accepted:
            return
        selected_edge_ids = propagation.selected_edge_ids()

        lines = []
        for change in selected_changes:
            lines.append(
                f"{change.source_layer_name or change.source_layer_id} | "
                f"{change.source_feature_key} | "
                f"{change.field_name} -> {change.source_field_name} | "
                f"baseline={change.old_value!r} | source now={change.current_source_value!r} | "
                f"proposed={change.new_value!r}"
            )

        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("Review approved source changes")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Apply {len(selected_changes)} approved Writable change(s) upstream?")
        box.setInformativeText(
            "AtOnce will revalidate every source UUID/value before writing and archive the returned "
            "XLSX as recovery evidence. Only the dependency edges you just selected will propagate "
            "downstream; unticked branches remain untouched and are recorded stale by choice. "
            "Derived, Conflict and Unresolved rows are never applied."
        )
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec_() != QMessageBox.Yes:
            return

        try:
            result = self.sync_service.apply(
                scan_result,
                selected_changes,
                selected_edge_ids=selected_edge_ids,
            )
        except SyncError as exc:
            self._reload_workflow()
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        if self.change_scan_panel is not None:
            self.change_scan_panel.clear()
        self._reload_workflow()
        if self.dock is not None:
            self.dock.tabs.setCurrentIndex(2)
        if result.refresh_revision_number is None:
            refresh_text = "No delivery output was refreshed"
        else:
            refresh_text = f"Selected downstream revision {result.refresh_revision_number} completed"
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            f"Applied {result.applied_changes} source change(s). {refresh_text}; "
            f"{len(result.stale_node_ids)} node(s) remain stale by choice. Returned workbook archived for audit.",
        )

    def _on_run_explicit_requested(self, workflow=None):
        workflow = workflow or self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return
        validation = self.workflow_service.validate(workflow)
        if self.dock is not None:
            self.dock.set_validation(validation)
        if not validation.is_valid:
            self._show_validation_dialog(validation, title="Workflow cannot run")
            return

        if not self._prepare_explicit_source_identity(workflow):
            return

        changed_source_ids = {
            source.stable_id for source in workflow.source_layers if source.stable_id
        }
        propagation = PropagationReviewDialog(
            workflow,
            changed_source_ids,
            self.iface.mainWindow(),
        )
        if propagation.exec_() != QDialog.Accepted:
            return
        try:
            result = self.graph_execution_service.execute(
                workflow.workflow_id,
                changed_source_ids,
                selected_edge_ids=propagation.selected_edge_ids(),
            )
        except GraphExecutionServiceError as exc:
            self._reload_workflow()
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        self._reload_workflow()
        revision = result.get("export_revision_number")
        if revision is None:
            self.iface.messageBar().pushSuccess(
                "AtOnce", "Selected explicit workflow run completed with no delivery revision."
            )
        else:
            self.iface.messageBar().pushSuccess(
                "AtOnce", f"Selected explicit workflow revision {revision} completed."
            )

    def _prepare_explicit_source_identity(self, workflow):
        preflight = self.source_identity_service.preflight(workflow)
        if preflight.blocked:
            details = "\n".join(
                f"{item.source_name}: {item.error}" for item in preflight.blocked
            )
            box = QMessageBox(self.iface.mainWindow())
            box.setWindowTitle("Source identity cannot be prepared")
            box.setIcon(QMessageBox.Critical)
            box.setText("The explicit workflow cannot run until every source is ready.")
            box.setDetailedText(details)
            box.exec_()
            return False
        if not preflight.writes_required:
            return True

        affected = "\n".join(
            f"• {item.source_name}: assign {item.assignment_count} missing UUID(s)"
            for item in preflight.needs_initialization
        )
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("Initialize stable source IDs")
        box.setIcon(QMessageBox.Warning)
        box.setText("AtOnce needs stable feature IDs before this workflow can run.")
        box.setInformativeText(
            "AtOnce will add the text field '_atonce_source_key' if needed and assign UUIDs "
            "only to features that do not already have one. Existing valid AtOnce UUIDs are preserved."
        )
        box.setDetailedText(
            f"Affected source layer(s):\n{affected}\n\n"
            "No derived candidate or delivery output will be created if initialization is cancelled."
        )
        initialize = box.addButton("Initialize & continue", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec_()
        if box.clickedButton() is not initialize:
            return False

        try:
            self.source_identity_service.initialize(preflight)
        except SourceIdentityError as exc:
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return False
        return True

    def _on_refresh_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return
        if workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            self._on_run_explicit_requested(workflow)
            return
        derived = workflow.derived_layer
        if derived is not None and derived.layer_id and self.qgis.resolve_layer(derived.layer_id):
            self._on_export_requested()
        else:
            self._on_build_layer_c_requested()

    def _on_build_layer_c_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        if workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            self._on_run_explicit_requested(workflow)
            return
        validation = self.workflow_service.validate(workflow)
        if self.dock is not None:
            self.dock.set_validation(validation)
        self._update_downstream_actions(workflow, validation)
        if not validation.is_valid:
            self._show_validation_dialog(validation, title="Layer C cannot be built")
            return

        try:
            approved_divergence = self.propagation_service.check_derived_divergence(
                workflow.workflow_id
            )
        except PropagationError as exc:
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle(
            "Layer C has manual changes" if approved_divergence is not None else "Build Layer C"
        )
        box.setIcon(QMessageBox.Warning)
        if approved_divergence is not None:
            box.setText("Rebuilding Layer C will discard manual changes in the derived layer.")
            box.setInformativeText(
                "Layer C remains editable, but derived edits are not automatically written back to "
                "Source A/B. Review the differences below. Rebuild only if you intend to replace "
                "those derived-only edits with a fresh source-derived Layer C."
            )
            box.setDetailedText(format_derived_diff(approved_divergence))
        else:
            box.setText("Build a fresh Layer C from the registered source layers?")
            box.setInformativeText(
                "To create stable feature lineage, AtOnce may add the text field "
                "'_atonce_source_key' to Source Layer A / B and assign UUIDs only where "
                "a key is missing. Existing valid UUIDs are preserved. The current project "
                "Layer C is replaced only after the new derived layer builds successfully."
            )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec_() != QMessageBox.Yes:
            return

        try:
            result = self.propagation_service.rebuild_layer_c(
                workflow.workflow_id,
                approved_divergence=approved_divergence,
            )
        except PropagationDivergenceError as exc:
            changed = QMessageBox(self.iface.mainWindow())
            changed.setWindowTitle("Layer C changed again")
            changed.setIcon(QMessageBox.Warning)
            changed.setText("Layer C changed after the differences were reviewed.")
            changed.setInformativeText(
                "Nothing was replaced. Review the updated differences and try again."
            )
            changed.setDetailedText(exc.details)
            changed.exec_()
            return
        except PropagationError as exc:
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        if self.change_scan_panel is not None:
            self.change_scan_panel.clear()
        self._reload_workflow()
        assigned = sum(result.assigned_source_keys.values())
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            f"Layer C rebuilt with {result.derived_feature_count} feature(s). "
            f"Assigned {assigned} new stable source UUID(s).",
        )

    def _on_export_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return

        if workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            self._on_run_explicit_requested(workflow)
            return
        validation = self.workflow_service.validate(workflow)
        if not validation.is_valid:
            if self.dock is not None:
                self.dock.set_validation(validation)
            self._update_downstream_actions(workflow, validation)
            self._show_validation_dialog(validation, title="Outputs cannot be exported")
            return

        next_revision = self.store.next_export_revision_number(workflow.workflow_id)
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("Export downstream outputs")
        box.setIcon(QMessageBox.Information)
        box.setText(f"Create downstream revision {next_revision}?")
        box.setInformativeText(
            "AtOnce stages the GeoPackage and all linked XLSX files before replacing anything. "
            "An existing file is replaced only when a prior completed AtOnce revision proves "
            "that path is AtOnce-owned. Unrelated existing workbooks are blocked."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec_() != QMessageBox.Yes:
            return

        try:
            result = self.export_service.export_downstream(workflow.workflow_id)
        except DerivedDivergenceError as exc:
            if self.dock is not None:
                latest = self.store.latest_export_revision(workflow.workflow_id)
                if latest is not None:
                    self.dock.outputs_meta.setText(
                        f"Export blocked · revision {latest.revision_number} remains current"
                    )
            changed = QMessageBox(self.iface.mainWindow())
            changed.setWindowTitle("Derived output changed")
            changed.setIcon(QMessageBox.Warning)
            changed.setText("Layer C or its GeoPackage differs from the recorded AtOnce state.")
            changed.setInformativeText(
                "AtOnce will not overwrite these changes silently and did not modify Source A/B. "
                "Review the semantic differences below before deciding how to recover."
            )
            changed.setDetailedText(exc.details)
            changed.exec_()
            return
        except ExportError as exc:
            if self.dock is not None:
                latest = self.store.latest_export_revision(workflow.workflow_id)
                if latest is None:
                    self.dock.outputs_meta.setText("Export failed · no completed revision recorded")
                else:
                    self.dock.outputs_meta.setText(
                        f"Export failed · revision {latest.revision_number} remains current"
                    )
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return

        if self.change_scan_panel is not None:
            self.change_scan_panel.clear()
        self._reload_workflow()
        revision = result.revision
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            f"Revision {revision.revision_number} complete: "
            f"{revision.gpkg_feature_count} GeoPackage feature(s), "
            f"{result.xlsx_row_count} linked XLSX row(s).",
        )

    def _update_downstream_actions(self, workflow, validation):
        valid = bool(validation and validation.is_valid)
        if self.rebuild_action is not None:
            self.rebuild_action.setEnabled(
                valid
                and workflow is not None
                and workflow.effective_dependency_graph_origin() not in EXPLICIT_EXECUTION_ORIGINS
            )

        if self.dock is None:
            return
        button = self.dock.refresh_button
        if workflow is not None and workflow.effective_dependency_graph_origin() in EXPLICIT_EXECUTION_ORIGINS:
            button.setText("> Plan")
            button.setEnabled(valid)
            button.setToolTip(
                "Prepare source identity, review propagation edges and run this explicit workflow."
            )
            return
        if workflow is None:
            button.setText("> Plan")
            button.setEnabled(bool(self.dock.builder.can_plan()))
            button.setToolTip("Plan the source and operation currently configured on the canvas.")
            return
        derived = workflow.derived_layer if workflow is not None else None
        has_derived = bool(
            derived
            and derived.layer_id
            and self.qgis.resolve_layer(derived.layer_id) is not None
        )

        if has_derived:
            button.setText("Export outputs")
            button.setEnabled(valid)
            button.setToolTip(
                "Materialize Layer C to the registered GeoPackage and linked XLSX outputs."
            )
        else:
            button.setText("Build Layer C")
            button.setEnabled(valid)
            button.setToolTip(
                "Build a fresh Layer C with stable AtOnce lineage before downstream export."
                if valid
                else "Resolve workflow validation errors before building Layer C."
            )

    def _on_trace_requested(self):
        if self.change_scan_panel is None:
            return
        change = self.change_scan_panel.selected_trace_change()
        if change is None:
            self.iface.messageBar().pushWarning(
                "AtOnce", "Select exactly one resolved change row before tracing its source."
            )
            return

        try:
            target = plan_trace_target(change)
            result = self.trace_navigator.trace(target)
        except (TracePlanningError, TraceNavigationError) as exc:
            self.iface.messageBar().pushCritical("AtOnce", f"Trace source failed: {exc}")
            return

        if result.warning:
            self.iface.messageBar().pushWarning("AtOnce", result.warning)
            return
        location = "selected and zoomed" if result.zoomed else "selected"
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            f"Source feature {result.source_key} on '{result.layer_name}' {location}; attribute table filtered to the exact UUID.",
        )

    def _show_validation_dialog(self, result, title="Workflow validation"):
        lines = []
        for issue in result.issues:
            prefix = "ERROR" if issue.severity == "error" else "WARNING"
            lines.append(f"{prefix}: {issue.message}")
        if not lines:
            lines.append("No validation issues found.")

        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Warning if result.issues else QMessageBox.Information)
        box.setText(result.summary)
        box.setDetailedText("\n".join(lines))
        box.exec_()

    def _foundation_message(self, text):
        self.iface.messageBar().pushInfo("AtOnce", text)
