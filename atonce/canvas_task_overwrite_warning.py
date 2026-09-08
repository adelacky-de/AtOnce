"""Protect registered task/output identity from misleading overwrite.

Product rule:

* A workflow name is presentation only. Renaming keeps the same workflow_id.
* An already-run task is identified by its operation node ID and execution
  signature. Changing its operation, parameters, or upstream wiring is a new
  task intent and must not overwrite the old materialized result.
* A registered OUTPUT node also keeps its name/path/format identity. Changing
  any of those values would make an old result look like a new output, so users
  must add another OUTPUT block instead.
* If the user wants both variants, they create/duplicate another operation node
  and/or output branch.
* The only authoring-time case that may intentionally overwrite/refresh an
  existing output is a pure workflow rename with unchanged task/output logic,
  and that requires explicit confirmation first.
* Source-data propagation through Update Changes is separate from authoring-time
  task adjustment and remains allowed.
"""

import json

from qgis.PyQt.QtWidgets import QMessageBox

from .models.dependency_graph import NodeKind


_APPLIED = False


def _stable_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _task_signature(graph, node):
    """Return the execution-relevant signature of one operation node."""

    incoming = []
    for edge in graph.incoming_edges(node.node_id):
        incoming.append(
            (
                str(edge.from_node),
                str((edge.parameters or {}).get("target_port") or "input"),
                str(getattr(edge.operation, "value", edge.operation)),
            )
        )
    return (
        str((node.metadata or {}).get("operation_kind") or ""),
        _stable_json((node.metadata or {}).get("parameters") or {}),
        tuple(sorted(incoming)),
    )


def _materialized_task_change_rows(existing, proposed, store):
    """Describe changed task definitions that already own a result."""

    if existing is None or proposed is None:
        return []
    old_graph = existing.effective_dependency_graph()
    new_graph = proposed.effective_dependency_graph()
    old_nodes = old_graph.node_map()
    new_nodes = new_graph.node_map()
    rows = []

    for node_id, new_node in new_nodes.items():
        if new_node.kind != NodeKind.DERIVED:
            continue
        old_node = old_nodes.get(node_id)
        if old_node is None or old_node.kind != NodeKind.DERIVED:
            # New node = new task. It does not overwrite an existing task ID.
            continue
        state = store.load_graph_node_materialization(existing.workflow_id, node_id)
        if state is None or not str(getattr(state, "layer_id", "") or ""):
            continue
        if _task_signature(old_graph, old_node) == _task_signature(new_graph, new_node):
            continue

        old_kind = str(
            (old_node.metadata or {}).get("operation_kind")
            or old_node.name
            or "Operation"
        )
        new_kind = str(
            (new_node.metadata or {}).get("operation_kind")
            or new_node.name
            or "Operation"
        )
        old_params = (old_node.metadata or {}).get("parameters") or {}
        new_params = (new_node.metadata or {}).get("parameters") or {}
        old_expr = str(old_params.get("expression") or "").strip()
        new_expr = str(new_params.get("expression") or "").strip()

        label = str(new_node.name or new_kind or "Operation")
        if old_expr or new_expr:
            detail = f"{label}: {old_expr or '(none)'}  →  {new_expr or '(none)'}"
        elif old_kind != new_kind:
            detail = f"{label}: {old_kind.upper()}  →  {new_kind.upper()}"
        else:
            detail = f"{label}: parameters or upstream inputs changed"
        rows.append(detail)

    return rows


def _registered_output_change_rows(existing, proposed):
    """Describe edits to an OUTPUT node that already belongs to the workflow."""

    if existing is None or proposed is None:
        return []
    old_nodes = existing.effective_dependency_graph().node_map()
    new_nodes = proposed.effective_dependency_graph().node_map()
    rows = []
    for node_id, new_node in new_nodes.items():
        if new_node.kind != NodeKind.DELIVERY:
            continue
        old_node = old_nodes.get(node_id)
        if old_node is None or old_node.kind != NodeKind.DELIVERY:
            # New OUTPUT block is the supported way to add another endpoint.
            continue
        old_name = str(old_node.name or "")
        new_name = str(new_node.name or "")
        old_path = str((old_node.metadata or {}).get("path") or "")
        new_path = str((new_node.metadata or {}).get("path") or "")
        old_format = str(
            old_node.format or (old_node.metadata or {}).get("format") or ""
        )
        new_format = str(
            new_node.format or (new_node.metadata or {}).get("format") or ""
        )
        if (old_name, old_path, old_format) == (new_name, new_path, new_format):
            continue
        rows.append(
            f"{old_name or 'OUTPUT'} → {new_name or 'OUTPUT'}; "
            f"{old_path or '(no path)'} → {new_path or '(no path)'}"
        )
    return rows


def _has_materialized_task(existing, store):
    if existing is None:
        return False
    graph = existing.effective_dependency_graph()
    for node in graph.nodes:
        if node.kind != NodeKind.DERIVED:
            continue
        state = store.load_graph_node_materialization(existing.workflow_id, node.node_id)
        if state is not None and str(getattr(state, "layer_id", "") or ""):
            return True
    return False


def _show_task_change_block(plugin, rows):
    """Explain why changing an already-run task cannot replace its result."""

    box = QMessageBox(plugin.iface.mainWindow())
    box.setWindowTitle("Create a new task instead")
    box.setIcon(QMessageBox.Warning)
    box.setText(
        "This task has already been run. Its operation or condition cannot be "
        "changed in place because that would overwrite the existing result."
    )

    shown = rows[:5]
    detail = "\n".join(f"• {row}" for row in shown)
    if len(rows) > len(shown):
        detail += f"\n• …and {len(rows) - len(shown)} more changed task(s)"
    box.setInformativeText(
        detail
        + "\n\nThe existing output and lineage condition will be kept unchanged. "
        "Create or duplicate a second operation task and connect it to a new "
        "output if you need the new condition as well."
        "\n\nRenaming the workflow is the only authoring change that may reuse and "
        "overwrite the existing output, because the task logic itself is unchanged."
    )
    box.addButton("OK", QMessageBox.AcceptRole)
    box.exec_()


def _show_output_change_block(plugin, rows):
    """Explain why a registered output endpoint cannot be repurposed."""

    box = QMessageBox(plugin.iface.mainWindow())
    box.setWindowTitle("Add a new output instead")
    box.setIcon(QMessageBox.Warning)
    box.setText(
        "This OUTPUT is already registered. Its name, path, or format cannot be "
        "changed in place because that would make the previous result appear to "
        "be a different output."
    )
    shown = rows[:5]
    detail = "\n".join(f"• {row}" for row in shown)
    if len(rows) > len(shown):
        detail += f"\n• …and {len(rows) - len(shown)} more changed output(s)"
    box.setInformativeText(
        detail
        + "\n\nThe existing output will be kept unchanged. Add another OUTPUT block "
        "for a new file name/path/format. A workflow-name rename is separate and "
        "may keep the same output after confirmation."
    )
    box.addButton("OK", QMessageBox.AcceptRole)
    box.exec_()


def _confirm_rename_overwrite(plugin, existing, proposed):
    """Confirm a pure workflow rename before refreshing the same outputs."""

    old_name = str(getattr(existing, "name", "") or "").strip()
    new_name = str(getattr(proposed, "name", "") or "").strip()
    if not old_name or not new_name or old_name == new_name:
        return True
    if not _has_materialized_task(existing, plugin.store):
        return True

    box = QMessageBox(plugin.iface.mainWindow())
    box.setWindowTitle("Rename workflow and refresh output?")
    box.setIcon(QMessageBox.Warning)
    box.setText(f"Rename workflow '{old_name}' to '{new_name}'?")
    box.setInformativeText(
        "This keeps the same workflow ID and the same task/output lineage. "
        "Continuing will refresh/overwrite the existing materialized output(s); "
        "it will not create a second workflow or a second copy of the task result."
    )
    confirm = box.addButton("Rename & Overwrite", QMessageBox.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)
    box.exec_()
    return box.clickedButton() is confirm


def _stale_result_requested(plugin):
    """Return True when the current result node is a stale draft result."""

    dock = getattr(plugin, "dock", None)
    builder = getattr(dock, "builder", None) if dock is not None else None
    if builder is None:
        return False
    try:
        node_id = builder.final_result_node_id()
    except Exception:
        return False
    stale = set(getattr(builder, "_stale_node_ids", ()) or ())
    return bool(node_id and str(node_id) in stale)


def apply_task_overwrite_confirmation():
    """Install task/output immutability plus rename-only overwrite confirmation."""

    global _APPLIED
    if _APPLIED:
        return

    from .canvas_plugin import CanvasRecoveryAtOncePlugin
    from .services.graph_execution_service import GraphExecutionServiceError

    def register(self):
        if self.dock is None:
            return
        definition = self.dock.builder.definition()
        if definition is None:
            self.iface.messageBar().pushWarning(
                "AtOnce", "Complete the source, operation and output blocks before Register."
            )
            return

        # Editing keeps workflow_id stable. A rename is therefore still the same
        # workflow; task/output identity changes are not allowed to reuse existing
        # registered identities.
        existing = self.store.load_workflow(definition.workflow_id)
        if existing is not None:
            changed_outputs = _registered_output_change_rows(existing, definition)
            if changed_outputs:
                _show_output_change_block(self, changed_outputs)
                self.iface.messageBar().pushWarning(
                    "AtOnce",
                    "Existing output kept. Add a new OUTPUT block for a different name, path, or format.",
                )
                return
            changed_tasks = _materialized_task_change_rows(
                existing, definition, self.store
            )
            if changed_tasks:
                _show_task_change_block(self, changed_tasks)
                self.iface.messageBar().pushWarning(
                    "AtOnce",
                    "Existing task result kept. Create or duplicate a new task for the changed condition.",
                )
                return
            if not _confirm_rename_overwrite(self, existing, definition):
                self.iface.messageBar().pushInfo(
                    "AtOnce", "Rename cancelled. Existing output was not overwritten."
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
        graph = workflow.effective_dependency_graph()
        from .core.freeform_graph import registered_selected_edges

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
            return
        self._reload_workflow()
        revision = result.get("export_revision_number") if isinstance(result, dict) else None
        from .models.dependency_graph import NodeKind

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
            return
        self._session_changed_source_ids.clear()
        self.iface.messageBar().pushSuccess(
            "AtOnce",
            "Lineage registered. Connected outputs were created or refreshed.",
        )

    original_result_requested = CanvasRecoveryAtOncePlugin._on_canvas_result_requested

    def result_requested(self):
        if _stale_result_requested(self):
            box = QMessageBox(self.iface.mainWindow())
            box.setWindowTitle("Result is from the previous task condition")
            box.setIcon(QMessageBox.Warning)
            box.setText(
                "This materialized result is stale because the registered task "
                "condition was changed in the current canvas draft."
            )
            box.setInformativeText(
                "The existing layer still contains data from the previous registered "
                "condition. It has not been recalculated or overwritten. Create or "
                "duplicate a new operation task/output for the new condition, or "
                "revert the draft change."
            )
            box.addButton("OK", QMessageBox.AcceptRole)
            box.exec_()
            self.iface.messageBar().pushWarning(
                "AtOnce", "Stale result not opened as the current result."
            )
            return
        return original_result_requested(self)

    # Installed last from classFactory. This intentionally overrides the older
    # acceptance shim that treated rename as clone and the temporary shim that
    # allowed changed task parameters to overwrite an existing result.
    CanvasRecoveryAtOncePlugin._on_canvas_plan_requested = register
    CanvasRecoveryAtOncePlugin._on_canvas_result_requested = result_requested
    _APPLIED = True
