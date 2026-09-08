"""Focused runtime hardening for the canvas desktop acceptance cases.

The canvas is intentionally vector/attribute-first. These compatibility hooks
keep that product contract explicit while preserving the existing execution and
lineage services:

* expose the DependencyGraph downstream-edge API under the older canvas name;
* rebind stale incoming edge operation metadata when an OPERATION is changed;
* reject same-physical-source MERGE/JOIN inputs at connection time;
* draw invalid attempted connectors red and completed refreshed connectors green;
* make the vector-only SOURCE selector explicit about raster layers;
* keep AtOnce lineage fields in the data while hiding them from normal QGIS UI;
* name materialized result layers from OUTPUT/workflow context instead of the
  operation label such as FILTER; and
* treat Register after changing a registered workflow name as a new workflow,
  so a distinct workflow never replaces another workflow's materialization.

The hooks are installed once from ``classFactory`` after the production canvas
modules have loaded. This keeps the change isolated to the acceptance branch
without changing the persisted graph schema.
"""

from dataclasses import replace

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QPen

from .core.lineage import RESERVED_LINEAGE_FIELDS
from .models.dependency_graph import DependencyGraph, NodeKind, OperationKind


_APPLIED = False
_INVALID_RED = QColor("#D92D20")
_SUCCESS_GREEN = QColor("#2E7D32")
_ACTIVE_DISPLAY_NAMES = {}


def _source_binding_ids(canvas, node_id, seen=None):
    """Return physical QGIS bindings contributing to one canvas node."""

    visited = set(seen or ())
    wanted = str(node_id or "")
    if not wanted or wanted in visited:
        return set()
    visited.add(wanted)
    node = canvas._graph.node_map().get(wanted)
    if node is None:
        return set()
    if node.kind == NodeKind.SOURCE:
        lineage_id = str(node.metadata.get("source_lineage_id") or "")
        ref = canvas._source_refs.get(lineage_id)
        binding_id = str(getattr(ref, "current_layer_id", "") or "") if ref else ""
        return {binding_id} if binding_id else set()
    if node.kind != NodeKind.DERIVED:
        return set()
    result = set()
    for edge in canvas._graph.incoming_edges(node.node_id):
        result.update(_source_binding_ids(canvas, edge.from_node, visited))
    return result


def _duplicate_source_input_error(canvas, from_node_id, to_node_id):
    """Reject self MERGE/JOIN while still allowing a source to branch elsewhere."""

    target = canvas._graph.node_map().get(str(to_node_id or ""))
    if target is None or target.kind != NodeKind.DERIVED:
        return ""
    operation_kind = str(target.metadata.get("operation_kind") or "")
    if operation_kind not in {OperationKind.MERGE.value, OperationKind.JOIN.value}:
        return ""

    proposed = _source_binding_ids(canvas, from_node_id)
    if not proposed:
        return ""
    for edge in canvas._graph.incoming_edges(target.node_id):
        if proposed.intersection(_source_binding_ids(canvas, edge.from_node)):
            title = operation_kind.upper()
            return (
                f"{title} cannot use the same loaded source layer more than once. "
                "The source may still be reused on separate workflow branches."
            )
    return ""


def _draw_invalid_connection(canvas, from_node_id, to_node_id, port_id, message):
    """Show one rejected connector in red without adding it to the graph."""

    from .ui.canvas_view import LineageConnectionItem

    source_item = canvas._items.get(str(from_node_id or ""))
    target_item = canvas._items.get(str(to_node_id or ""))
    if source_item is None or target_item is None:
        return
    connection = LineageConnectionItem(
        source_item,
        target_item,
        str(port_id or "input"),
        edge_id="",
        included=True,
    )
    connection.setPen(QPen(_INVALID_RED, 2.4))
    connection.setToolTip(str(message or "Invalid workflow connection."))
    canvas.view.scene().addItem(connection)
    canvas._connections.append(connection)


def _hide_internal_lineage_fields(layer):
    """Hide AtOnce-owned fields in QGIS while retaining their stored values."""

    if layer is None or not hasattr(layer, "fields"):
        return
    reserved = set(RESERVED_LINEAGE_FIELDS)

    try:
        config = layer.attributeTableConfig()
        columns = list(config.columns())
        changed = False
        for column in columns:
            if str(getattr(column, "name", "") or "") in reserved:
                column.hidden = True
                changed = True
        if changed:
            config.setColumns(columns)
            layer.setAttributeTableConfig(config)
    except Exception:
        pass

    try:
        from qgis.core import QgsEditorWidgetSetup

        for field_name in reserved:
            index = layer.fields().indexOf(field_name)
            if index >= 0:
                layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
    except Exception:
        pass


def _materialized_display_names(workflow):
    """Return user-facing QGIS layer names for one workflow's operation nodes.

    A terminal operation with exactly one OUTPUT inherits the OUTPUT block name.
    Intermediate or multi-output operations use workflow + operation context, so
    two workflows never look like the same anonymous ``FILTER`` result.
    """

    if workflow is None:
        return {}
    graph = workflow.effective_dependency_graph()
    node_map = graph.node_map()
    result = {}
    workflow_name = str(getattr(workflow, "name", "") or "Workflow").strip() or "Workflow"

    for node in graph.nodes:
        if node.kind != NodeKind.DERIVED:
            continue
        outputs = []
        for edge in graph.downstream_edges(node.node_id):
            target = node_map.get(edge.to_node)
            if target is None or target.kind != NodeKind.DELIVERY:
                continue
            name = str(target.name or "").strip()
            if name and name.lower() != "output":
                outputs.append(name)
        outputs = list(dict.fromkeys(outputs))
        if len(outputs) == 1:
            display_name = outputs[0]
        else:
            operation_name = str(node.name or "Result").strip() or "Result"
            display_name = f"{workflow_name} · {operation_name}"
        result[(str(workflow.workflow_id), str(node.node_id))] = display_name
    return result


def _install_graph_alias():
    if not hasattr(DependencyGraph, "outgoing_edges"):
        DependencyGraph.outgoing_edges = DependencyGraph.downstream_edges


def _install_operation_rebinding():
    """Make configured OPERATION changes update incoming edge semantics too."""

    from .core import provisional_connections as provisional
    from .core.graph_editing import GraphEditError
    from .ui import material_block_dialogs

    original = provisional.bind_provisional_inputs

    def bind_inputs(graph, operation_node_id, operation_kind):
        definition = provisional.DEFAULT_OPERATION_REGISTRY.get(operation_kind)
        if definition is None:
            raise GraphEditError(f"Unsupported operation {operation_kind!r}.")

        incoming = list(graph.incoming_edges(str(operation_node_id)))
        desired = OperationKind(definition.kind)
        mismatched = [edge for edge in incoming if edge.operation != desired]
        if not mismatched:
            return original(graph, operation_node_id, operation_kind)

        ordered = sorted(incoming, key=lambda edge: str(edge.edge_id))
        ports = provisional.provisional_port_ids(definition, len(ordered))
        if len(ports) < len(ordered):
            raise GraphEditError(
                f"{definition.title} accepts fewer inputs than are already connected. "
                "Disconnect extra inputs before applying this function."
            )

        port_by_edge = {
            edge.edge_id: ports[index] for index, edge in enumerate(ordered)
        }
        rebound = []
        for edge in graph.edges:
            port_id = port_by_edge.get(edge.edge_id)
            if port_id is None:
                rebound.append(edge)
                continue
            parameters = dict(edge.parameters or {})
            parameters.pop(provisional.PROVISIONAL_EDGE_FLAG, None)
            parameters["target_port"] = port_id
            rebound.append(
                replace(edge, operation=desired, parameters=parameters)
            )
        return DependencyGraph(list(graph.nodes), rebound)

    provisional.bind_provisional_inputs = bind_inputs
    material_block_dialogs.bind_provisional_inputs = bind_inputs


def _install_canvas_connection_validation():
    from .core.freeform_graph import connect_graph_nodes
    from .core.graph_editing import GraphEditError
    from .core.provisional_connections import connect_provisional_operation_input
    from .ui.drag_node_workflow_canvas import DragNodeWorkflowCanvas

    def complete_connection(self, from_node_id, node_id, port_id):
        target = self._graph.node_map().get(str(node_id or ""))
        if target is None:
            message = f"Connection target node {node_id!r} does not exist."
            self.message_requested.emit(message)
            return

        message = _duplicate_source_input_error(self, from_node_id, node_id)
        if message:
            _draw_invalid_connection(self, from_node_id, node_id, port_id, message)
            self.message_requested.emit(message)
            return

        try:
            if target.kind == NodeKind.DERIVED and not str(
                target.metadata.get("operation_kind") or ""
            ):
                candidate = connect_provisional_operation_input(
                    self._graph,
                    from_node_id,
                    node_id,
                )
            else:
                candidate = connect_graph_nodes(
                    self._graph,
                    from_node_id,
                    node_id,
                    target_port=port_id,
                )
        except GraphEditError as exc:
            _draw_invalid_connection(
                self, from_node_id, node_id, port_id, str(exc)
            )
            self.message_requested.emit(str(exc))
            return

        self._graph = candidate
        self._render_graph()
        self.graph_changed.emit()

    original_result_evidence = DragNodeWorkflowCanvas.set_result_evidence

    def set_result_evidence(self, graph_state):
        original_result_evidence(self, graph_state)
        selected = set(getattr(graph_state, "selected_edge_ids", ()) or ())
        skipped = set(getattr(graph_state, "skipped_edge_ids", ()) or ())
        refreshed = set(getattr(graph_state, "refreshed_node_ids", ()) or ())
        stale = set(getattr(graph_state, "stale_node_ids", ()) or ())
        for connection in self._connections:
            edge_id = str(getattr(connection, "edge_id", "") or "")
            target_node = getattr(getattr(connection, "target_item", None), "node", None)
            target_id = str(target_node.node_id) if target_node is not None else ""
            if (
                edge_id
                and edge_id in selected
                and edge_id not in skipped
                and target_id in refreshed
                and target_id not in stale
            ):
                pen = QPen(_SUCCESS_GREEN, 2.4)
                pen.setStyle(Qt.SolidLine)
                connection.setPen(pen)
                connection.setToolTip("Successfully refreshed in the last run.")

    DragNodeWorkflowCanvas._complete_connection = complete_connection
    DragNodeWorkflowCanvas.set_result_evidence = set_result_evidence


def _install_vector_only_source_feedback():
    """Kept for plugin install order; raster SOURCE is now first-class."""

    return


def _install_lineage_ui_hiding():
    from .infrastructure.qgis_gateway import QgisGateway
    from .infrastructure.source_identity_runtime import SourceIdentityRuntime

    original_apply = SourceIdentityRuntime.apply_source_key_plan

    def apply_source_key_plan(self, layer, preflight):
        result = original_apply(self, layer, preflight)
        _hide_internal_lineage_fields(layer)
        return result

    SourceIdentityRuntime.apply_source_key_plan = apply_source_key_plan

    for method_name in ("replace_project_derived_layer", "replace_graph_node_layer"):
        original = getattr(QgisGateway, method_name)

        def wrapped(self, old_layer_id, new_layer, _original=original):
            result = _original(self, old_layer_id, new_layer)
            _hide_internal_lineage_fields(new_layer)
            return result

        setattr(QgisGateway, method_name, wrapped)


def _install_materialization_naming():
    """Name candidate layers from OUTPUT/workflow context for every operation."""

    from .infrastructure.qgis_gateway import QgisGateway
    from .services.graph_execution_service import GraphExecutionService

    original_execute = GraphExecutionService.execute

    def execute(
        self,
        workflow_id=None,
        changed_source_lineage_ids=(),
        selected_edge_ids=None,
    ):
        workflow = self.store.load_workflow(workflow_id)
        previous = dict(_ACTIVE_DISPLAY_NAMES)
        _ACTIVE_DISPLAY_NAMES.clear()
        _ACTIVE_DISPLAY_NAMES.update(_materialized_display_names(workflow))
        try:
            return original_execute(
                self,
                workflow_id,
                changed_source_lineage_ids,
                selected_edge_ids,
            )
        finally:
            _ACTIVE_DISPLAY_NAMES.clear()
            _ACTIVE_DISPLAY_NAMES.update(previous)

    GraphExecutionService.execute = execute

    method_names = (
        "build_filtered_memory_layer",
        "build_passthrough_memory_layer",
        "build_field_mapping_memory_layer",
        "build_keep_fields_memory_layer",
        "build_rename_field_memory_layer",
        "build_change_field_type_memory_layer",
        "build_sort_memory_layer",
        "build_aggregate_memory_layer",
        "build_compare_changes_memory_layer",
        "build_join_memory_layer",
        "build_calculate_field_memory_layer",
        "build_remove_duplicates_memory_layer",
        "build_buffer_memory_layer",
        "build_reproject_memory_layer",
        "build_select_by_location_memory_layer",
        "build_spatial_join_memory_layer",
        "build_clip_memory_layer",
        "build_dissolve_memory_layer",
        "build_merged_memory_layer",
    )
    for method_name in method_names:
        original = getattr(QgisGateway, method_name, None)
        if not callable(original):
            continue

        def named_builder(
            self,
            workflow_id,
            node_id,
            display_name,
            *args,
            _original=original,
            **kwargs,
        ):
            name = _ACTIVE_DISPLAY_NAMES.get(
                (str(workflow_id), str(node_id)),
                str(display_name or "Result"),
            )
            return _original(
                self,
                workflow_id,
                node_id,
                name,
                *args,
                **kwargs,
            )

        setattr(QgisGateway, method_name, named_builder)


def _install_register_as_new_on_rename():
    """A new workflow name at Register creates a new persisted workflow ID.

    Re-registering without changing the workflow name still refreshes that
    workflow's own materialization. This separates the user's 'new workflow'
    intent from an ordinary rerun/update.
    """

    from .canvas_plugin import CanvasRecoveryAtOncePlugin

    original = CanvasRecoveryAtOncePlugin._on_canvas_plan_requested

    def register(self):
        dock = getattr(self, "dock", None)
        builder = getattr(dock, "builder", None) if dock is not None else None
        current = getattr(builder, "_workflow", None) if builder is not None else None
        entered_name = (
            builder.workflow_name_edit.text().strip()
            if builder is not None and hasattr(builder, "workflow_name_edit")
            else ""
        )
        clone_as_new = bool(
            current is not None
            and entered_name
            and entered_name != str(getattr(current, "name", "") or "").strip()
        )
        if not clone_as_new:
            return original(self)

        # ``definition()`` creates a fresh workflow_id only when the canvas is
        # not editing an existing registered workflow. Keep the current graph
        # and source refs; detach only the persisted workflow identity.
        builder._workflow = None
        try:
            return original(self)
        finally:
            # Successful Register reloads the newly active workflow into the
            # builder. On validation/execution failure keep editing the original
            # workflow instead of leaving the canvas identity detached.
            if getattr(builder, "_workflow", None) is None:
                builder._workflow = current

    CanvasRecoveryAtOncePlugin._on_canvas_plan_requested = register


def apply_canvas_acceptance_fixes():
    """Install the focused acceptance fixes exactly once per plugin process."""

    global _APPLIED
    if _APPLIED:
        return
    _install_graph_alias()
    _install_operation_rebinding()
    _install_canvas_connection_validation()
    _install_vector_only_source_feedback()
    _install_lineage_ui_hiding()
    _install_materialization_naming()
    _install_register_as_new_on_rename()
    _APPLIED = True
