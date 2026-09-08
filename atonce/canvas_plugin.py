"""Production adapter for the compact drag-first AtOnce canvas."""

import json

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from .core.change_session import ChangeSessionState
from .core.field_relationships import source_field_relationships
from .core.freeform_graph import registered_selected_edges
from .infrastructure.project_store import (
    ACTIVE_WORKFLOW_ID_KEY,
    DERIVED_STATES_KEY,
    EXPORT_REVISIONS_KEY,
    GRAPH_NODE_MATERIALIZATIONS_KEY,
    GRAPH_RUN_STATES_KEY,
    PROJECT_SCOPE,
    SCHEMA_VERSION_KEY,
    STORE_SCHEMA_VERSION,
    SYNC_AUDITS_KEY,
    WORKFLOWS_KEY,
)
from .models.dependency_graph import NodeKind
from .models.workflow import EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN
from .recovery_plugin import RecoveryAtOncePlugin
from .services.graph_execution_service import GraphExecutionServiceError
from .services.recovery_service import RecoveryError
from .ui.compact_dock import CompactAtOnceDockWidget
from .ui.recovery_dialog import SourceRecoveryDialog


class CanvasRecoveryAtOncePlugin(RecoveryAtOncePlugin):
    """Keep execution/lineage services while replacing the authoring/change UX."""

    def open_dock(self):
        if self.dock is None:
            self.dock = CompactAtOnceDockWidget(self.iface.mainWindow(), self.qgis)
            self.dock.refresh_requested.connect(self._on_refresh_requested)
            self.dock.plan_requested.connect(self._on_canvas_plan_requested)
            self.dock.register_requested.connect(self._on_canvas_clear_requested)
            self.dock.authoring_requested.connect(self._on_authoring_requested)
            self.dock.builder.configuration_requested.connect(
                self._on_canvas_configuration_requested
            )
            self.dock.builder.plan_requested.connect(self._on_canvas_plan_requested)
            self.dock.builder.message_requested.connect(self._on_canvas_message)
            self.dock.builder.result_requested.connect(self._on_canvas_result_requested)
            self.dock.builder.source_relink_index_requested.connect(
                self._on_canvas_source_relink_requested
            )
            self.dock.workflow_selected.connect(self._on_workflow_selected)
            self.dock.validate_requested.connect(self._on_validate_requested)
            self.dock.trace_requested.connect(self._on_trace_requested)
            self.dock.source_edit_requested.connect(self._on_source_edit_requested)
            self.dock.update_changes_requested.connect(self._on_update_changes_requested)
            self.dock.session_closed.connect(self._reset_change_session)

            # Compact Changes/History are no longer the legacy XLSX reverse-sync
            # controllers. History is deliberately session-only.
            self.change_scan_panel = None
            self.history_panel = None
            self._change_session = ChangeSessionState()
            self._session_changed_source_ids = self._change_session.changed_source_ids
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        # A close event resets the session; tab switches and ordinary rerenders do not.
        self._show_empty_canvas_with_selector()
        self.dock.show()
        self.dock.raise_()

    def _reset_change_session(self):
        state = getattr(self, "_change_session", None)
        if state is not None:
            state.clear()
        else:
            self._session_changed_source_ids = set()
        if self.dock is not None:
            self.dock.clear_session_history()

    def _show_empty_canvas_with_selector(self):
        if self.dock is None:
            return
        workflows = self.workflow_service.load_all()
        self.dock.workflow_combo.blockSignals(True)
        self.dock.set_workflows(
            [
                (
                    item.workflow_id,
                    f"{item.name} · {self.workflow_service.type_label(item)}",
                )
                for item in workflows
            ],
            None,
        )
        self.dock.workflow_combo.setCurrentIndex(-1)
        self.dock.workflow_combo.blockSignals(False)
        self.dock.set_workflow(None)
        self.dock.workflow_combo.setEnabled(self.dock.workflow_combo.count() > 0)
        self._update_downstream_actions(None, None)

    def _on_canvas_clear_requested(self):
        """Clear the current canvas and delete its persisted AtOnce workflow state."""

        if self.dock is None:
            return
        workflow = getattr(self.dock, "_workflow", None)
        if workflow is not None:
            self._delete_workflow_state(workflow.workflow_id)
        self._reset_change_session()
        self.dock.builder.clear_canvas()
        self._show_empty_canvas_with_selector()
        self.iface.messageBar().pushInfo(
            "AtOnce",
            "Canvas cleared. Drag SOURCE, OPERATION and OUTPUT blocks to start again.",
        )

    def _delete_workflow_state(self, workflow_id):
        """Delete one workflow and its persisted AtOnce evidence, not QGIS source data."""

        wanted = str(workflow_id or "").strip()
        if not wanted:
            return
        project = self.store.project
        workflows = [
            item for item in self.store.load_workflows() if item.workflow_id != wanted
        ]
        export_revisions = [
            item for item in self.store.load_export_revisions() if item.workflow_id != wanted
        ]
        derived_states = [
            item for item in self.store.load_derived_states() if item.workflow_id != wanted
        ]
        sync_audits = [
            item for item in self.store.load_sync_audits() if item.workflow_id != wanted
        ]
        graph_runs = [
            item for item in self.store.load_graph_run_states() if item.workflow_id != wanted
        ]
        materializations = [
            item
            for item in self.store.load_graph_node_materializations()
            if item.workflow_id != wanted
        ]

        project.writeEntry(
            PROJECT_SCOPE,
            WORKFLOWS_KEY,
            json.dumps([item.to_dict() for item in workflows], ensure_ascii=False),
        )
        project.writeEntry(
            PROJECT_SCOPE,
            EXPORT_REVISIONS_KEY,
            json.dumps([item.to_dict() for item in export_revisions], ensure_ascii=False),
        )
        project.writeEntry(
            PROJECT_SCOPE,
            DERIVED_STATES_KEY,
            json.dumps([item.to_dict() for item in derived_states], ensure_ascii=False),
        )
        project.writeEntry(
            PROJECT_SCOPE,
            SYNC_AUDITS_KEY,
            json.dumps([item.to_dict() for item in sync_audits], ensure_ascii=False),
        )
        project.writeEntry(
            PROJECT_SCOPE,
            GRAPH_RUN_STATES_KEY,
            json.dumps([item.to_dict() for item in graph_runs], ensure_ascii=False),
        )
        project.writeEntry(
            PROJECT_SCOPE,
            GRAPH_NODE_MATERIALIZATIONS_KEY,
            json.dumps([item.to_dict() for item in materializations], ensure_ascii=False),
        )
        project.writeEntry(PROJECT_SCOPE, ACTIVE_WORKFLOW_ID_KEY, "")
        project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        project.setDirty(True)

    def _on_canvas_plan_requested(self):
        """Register the lineage and immediately create/update included outputs."""

        if self.dock is None:
            return
        definition = self.dock.builder.definition()
        if definition is None:
            self.iface.messageBar().pushWarning(
                "AtOnce", "Complete the source, operation and output blocks before Register."
            )
            return
        validation = self.workflow_service.validate(definition)
        if not validation.is_valid:
            self.dock.set_validation(validation)
            self._show_validation_dialog(validation, title="Lineage cannot be registered")
            return
        result = self.workflow_service.register(definition)
        if not result.is_valid:
            self._show_validation_dialog(result, title="Lineage cannot be registered")
            return
        self._reload_workflow()
        workflow = self._active_workflow() or definition
        if not self._prepare_explicit_source_identity(workflow):
            return
        changed_source_ids = {
            source.stable_id for source in workflow.source_layers if source.stable_id
        }
        if not self._execute_registered_outputs(workflow, changed_source_ids):
            return
        self._session_changed_source_ids.clear()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Lineage registered. Connected outputs were created or refreshed.",
        )

    def _execute_registered_outputs(self, workflow, changed_source_ids):
        from .models.dependency_graph import NodeKind

        graph = workflow.effective_dependency_graph()
        selected_edge_ids = registered_selected_edges(graph)
        try:
            result = self.graph_execution_service.execute(
                workflow.workflow_id,
                changed_source_ids,
                selected_edge_ids=selected_edge_ids,
            )
        except GraphExecutionServiceError as exc:
            self._reload_workflow()
            self.iface.messageBar().pushCritical("AtOnce", str(exc))
            return False
        self._reload_workflow()
        revision = result.get("export_revision_number") if isinstance(result, dict) else None
        node_map = graph.node_map()
        selected_deliveries = [
            edge.to_node
            for edge in graph.edges
            if edge.edge_id in selected_edge_ids
            and node_map.get(edge.to_node) is not None
            and node_map[edge.to_node].kind == NodeKind.DELIVERY
        ]
        if selected_deliveries and revision is None:
            self.iface.messageBar().pushCritical(
                "AtOnce",
                "Register completed but no output file was written. "
                "Check that SOURCE → OPERATION → OUTPUT is connected and the OUTPUT path is valid.",
            )
            return False
        return True

    def _on_source_edit_requested(self, source_lineage_id):
        """Use native QGIS layer edit mode with explicit Save/Discard/Cancel on stop."""

        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "Select a registered workflow first.")
            return
        source = workflow.source_by_lineage_id(str(source_lineage_id or ""))
        layer = self.qgis.resolve_layer(source.current_layer_id) if source is not None else None
        if layer is None:
            self.iface.messageBar().pushCritical("AtOnce", "The selected source layer is not available.")
            return
        self.iface.setActiveLayer(layer)

        if not layer.isEditable():
            if not layer.startEditing():
                self.iface.messageBar().pushCritical(
                    "AtOnce", "QGIS could not start editing the selected source layer."
                )
                return
            self.dock.refresh_source_edit_button()
            return

        changed_fields = self._changed_field_names(layer)
        modified = bool(layer.isModified())
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("Stop editing source layer")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Stop editing '{layer.name()}'?")
        save = box.addButton("Save Changes", QMessageBox.AcceptRole)
        discard = box.addButton("Discard Changes", QMessageBox.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is cancel:
            return
        if clicked is discard:
            layer.rollBack()
            self.dock.refresh_source_edit_button()
            return
        if clicked is not save:
            return
        if not layer.commitChanges():
            self.iface.messageBar().pushCritical(
                "AtOnce", "QGIS could not save the source-layer edits. Editing remains unresolved."
            )
            self.dock.refresh_source_edit_button()
            return

        if modified:
            rows = self._session_history_rows(workflow, source.stable_id, changed_fields)
            self._change_session.record(source.stable_id, rows)
            self.dock.append_session_history(rows)
        self.dock.refresh_source_edit_button()

    @staticmethod
    def _changed_field_names(layer):
        names = set()
        edit_buffer = layer.editBuffer() if hasattr(layer, "editBuffer") else None
        if edit_buffer is None:
            return names
        try:
            for values in edit_buffer.changedAttributeValues().values():
                for index in values:
                    names.add(layer.fields().at(int(index)).name())
        except Exception:
            pass
        try:
            if edit_buffer.changedGeometries():
                names.add("(geometry)")
        except Exception:
            pass
        try:
            if edit_buffer.addedFeatures() or edit_buffer.deletedFeatureIds():
                names.add("(feature)")
        except Exception:
            pass
        return names or {"(feature)"}

    def _session_history_rows(self, workflow, changed_lineage_id, changed_fields):
        return source_field_relationships(
            workflow, changed_lineage_id, changed_fields
        )

    def _on_update_changes_requested(self):
        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "Select a registered workflow first.")
            return
        if not self._session_changed_source_ids:
            self.iface.messageBar().pushInfo(
                "AtOnce", "No saved source-layer edits are waiting to update outputs."
            )
            return
        if not self._prepare_explicit_source_identity(workflow):
            return
        changed = set(self._session_changed_source_ids)
        succeeded = self._execute_registered_outputs(workflow, changed)
        self._change_session.complete_update(changed, succeeded)
        if succeeded:
            self.iface.messageBar().pushSuccess(
                "AtOnce",
                "Changes updated for registered outputs that are included in Changes.",
            )

    def _update_downstream_actions(self, workflow, validation):
        """Preserve shared enablement logic but keep the product action named Register."""

        super()._update_downstream_actions(workflow, validation)
        if self.dock is None:
            return
        button = self.dock.refresh_button
        button.setText("Register")
        button.setToolTip(
            "Register the lineage and create/update outputs included in Changes"
        )
        if workflow is None:
            button.setEnabled(bool(self.dock.builder.can_plan()))

    def _on_canvas_source_relink_requested(self, source_index):
        """Relink the exact clicked source block through the proven recovery service."""

        workflow = self._active_workflow()
        if workflow is None:
            self.iface.messageBar().pushWarning("AtOnce", "No workflow is registered yet.")
            return
        index = int(source_index)
        if not 0 <= index < len(workflow.source_layers):
            self.iface.messageBar().pushWarning("AtOnce", "That source block is no longer available.")
            return

        dialog = SourceRecoveryDialog(
            workflow,
            self.qgis.vector_layers(),
            mode="relink",
            parent=self.iface.mainWindow(),
        )
        dialog.source_combo.setCurrentIndex(index)
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

    def _on_canvas_result_requested(self):
        workflow = self._active_workflow()
        if (
            workflow is None
            or workflow.effective_dependency_graph_origin()
            not in {EXPLICIT_GRAPH_ORIGIN, FREEFORM_GRAPH_ORIGIN}
            or self.dock is None
        ):
            self.iface.messageBar().pushInfo(
                "AtOnce", "Register an explicit workflow to create its result layer."
            )
            return
        node_id = self.dock.builder.final_result_node_id()
        if not node_id:
            self.iface.messageBar().pushInfo("AtOnce", "No final result block is configured.")
            return
        state = self.store.load_graph_node_materialization(workflow.workflow_id, node_id)
        layer = self.qgis.resolve_layer(state.layer_id) if state is not None else None
        if layer is None:
            self.iface.messageBar().pushInfo(
                "AtOnce", "The final result is not currently materialized in QGIS."
            )
            return
        self.iface.setActiveLayer(layer)
