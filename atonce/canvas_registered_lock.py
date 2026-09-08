"""Grey and lock registered canvas task structure.

Product contract:

* SOURCE blocks stay available for the controlled source/relink workflow.
* Once a workflow has been registered, its existing OPERATION and OUTPUT blocks
  are visually grey and read-only.
* Existing registered connectors are grey and cannot be deleted or rewired.
* A registered OPERATION may still start a new outgoing connection so users can
  extend the lineage with a new downstream task without mutating the old task.
* Newly added OPERATION/OUTPUT blocks and connectors remain normal/editable until
  the next successful Register reloads them as part of the persisted workflow.
* Workflow display-name editing remains separate and is governed by the rename
  confirmation policy.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QPen

from .models.dependency_graph import NodeKind


_APPLIED = False
_REGISTERED_FILL = QColor("#E3E6E8")
_REGISTERED_LINE = QColor("#B6BDC1")


def _registered_message(node):
    if node is None:
        return "This registered block is read-only."
    if node.kind == NodeKind.DELIVERY:
        return (
            "This OUTPUT is registered and locked. Add a new OUTPUT block if you "
            "need another file/name/path."
        )
    return (
        "This OPERATION is registered and locked. Create or duplicate a new task "
        "to use different operation settings. You can still connect its result "
        "to a new downstream OPERATION."
    )


def apply_registered_canvas_lock():
    """Install registered/read-only visuals and graph-edit guards exactly once."""

    global _APPLIED
    if _APPLIED:
        return

    from .ui.canvas_icon_node_item import DragCanvasBlockItem
    from .ui.canvas_workflow_io import CanvasWorkflowIOMixin
    from .ui.drag_node_workflow_canvas import DragNodeWorkflowCanvas

    # --- Node visual -------------------------------------------------------
    original_fill = DragCanvasBlockItem._fill

    def fill(self):
        if bool(getattr(self, "_registered", False)):
            return _REGISTERED_FILL
        return original_fill(self)

    def set_registered(self, registered, tooltip=""):
        self._registered = bool(registered)
        if self._registered:
            self.setToolTip(
                str(tooltip or _registered_message(getattr(self, "node", None)))
            )
        elif not bool(getattr(self, "_stale", False)):
            self.setToolTip("")
        self.update()

    DragCanvasBlockItem._fill = fill
    DragCanvasBlockItem.set_registered = set_registered

    # --- Canvas persisted-state tracking ---------------------------------
    original_set_workflow = DragNodeWorkflowCanvas.set_workflow

    def set_workflow(self, workflow):
        original_set_workflow(self, workflow)
        if workflow is None:
            self._registered_node_ids = set()
            self._registered_edge_ids = set()
        else:
            graph = workflow.effective_dependency_graph()
            self._registered_node_ids = {
                node.node_id
                for node in graph.nodes
                if node.kind in {NodeKind.DERIVED, NodeKind.DELIVERY}
            }
            self._registered_edge_ids = {edge.edge_id for edge in graph.edges}
        self._render_graph()

    DragNodeWorkflowCanvas.set_workflow = set_workflow

    original_clear_canvas = DragNodeWorkflowCanvas.clear_canvas

    def clear_canvas(self):
        self._registered_node_ids = set()
        self._registered_edge_ids = set()
        return original_clear_canvas(self)

    DragNodeWorkflowCanvas.clear_canvas = clear_canvas

    def apply_registered_visuals(self):
        registered_nodes = set(getattr(self, "_registered_node_ids", set()) or set())
        registered_edges = set(getattr(self, "_registered_edge_ids", set()) or set())
        node_map = self._graph.node_map()

        for node_id, item in getattr(self, "_items", {}).items():
            node = node_map.get(str(node_id))
            locked = bool(
                node is not None
                and node.kind in {NodeKind.DERIVED, NodeKind.DELIVERY}
                and str(node_id) in registered_nodes
            )
            setter = getattr(item, "set_registered", None)
            if callable(setter):
                setter(locked, _registered_message(node) if locked else "")

        for connection in getattr(self, "_connections", ()):
            edge_id = str(getattr(connection, "edge_id", "") or "")
            if edge_id not in registered_edges:
                continue
            pen = QPen(_REGISTERED_LINE, 1.8)
            pen.setStyle(Qt.SolidLine)
            connection.setPen(pen)
            connection.setToolTip(
                "Registered lineage connection — locked. Add a new task/branch "
                "instead of rewiring this connection."
            )

    DragNodeWorkflowCanvas._apply_registered_visuals = apply_registered_visuals

    original_render_graph = DragNodeWorkflowCanvas._render_graph

    def render_graph(self):
        original_render_graph(self)
        apply_registered_visuals(self)

    DragNodeWorkflowCanvas._render_graph = render_graph

    # The acceptance evidence hook may colour a successful edge green after its
    # rerender. Registered structure wins: after execution it should settle back
    # to the grey/read-only state.
    original_set_result_evidence = DragNodeWorkflowCanvas.set_result_evidence

    def set_result_evidence(self, graph_state):
        result = original_set_result_evidence(self, graph_state)
        apply_registered_visuals(self)
        return result

    DragNodeWorkflowCanvas.set_result_evidence = set_result_evidence

    # --- Read-only interaction guards ------------------------------------
    original_block_clicked = DragNodeWorkflowCanvas._block_clicked

    def block_clicked(self, node_id):
        wanted = str(node_id or "")
        node = self._graph.node_map().get(wanted)
        if wanted in set(getattr(self, "_registered_node_ids", set()) or set()) and (
            node is not None and node.kind in {NodeKind.DERIVED, NodeKind.DELIVERY}
        ):
            self.message_requested.emit(_registered_message(node))
            return
        return original_block_clicked(self, node_id)

    DragNodeWorkflowCanvas._block_clicked = block_clicked

    original_port_pressed = DragNodeWorkflowCanvas._port_pressed

    def port_pressed(self, node_id, direction, port_id):
        wanted = str(node_id or "")
        node = self._graph.node_map().get(wanted)
        registered = wanted in set(
            getattr(self, "_registered_node_ids", set()) or set()
        )
        # Existing registered inputs are immutable. A registered OPERATION's
        # outgoing result remains usable to start a new downstream task.
        if registered and direction == "in":
            self._pending_connection = None
            self.message_requested.emit(_registered_message(node))
            return
        return original_port_pressed(self, node_id, direction, port_id)

    DragNodeWorkflowCanvas._port_pressed = port_pressed

    original_complete_connection = DragNodeWorkflowCanvas._complete_connection

    def complete_connection(self, from_node_id, node_id, port_id):
        target_id = str(node_id or "")
        if target_id in set(getattr(self, "_registered_node_ids", set()) or set()):
            target = self._graph.node_map().get(target_id)
            self._pending_connection = None
            self.message_requested.emit(_registered_message(target))
            return
        return original_complete_connection(self, from_node_id, node_id, port_id)

    DragNodeWorkflowCanvas._complete_connection = complete_connection

    original_key_press = DragNodeWorkflowCanvas.keyPressEvent

    def key_press(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            registered_nodes = set(
                getattr(self, "_registered_node_ids", set()) or set()
            )
            registered_edges = set(
                getattr(self, "_registered_edge_ids", set()) or set()
            )
            for item in list(self.view.scene().selectedItems()):
                node = getattr(item, "node", None)
                node_id = str(getattr(node, "node_id", "") or "")
                edge_id = str(getattr(item, "edge_id", "") or "")
                if node_id in registered_nodes or edge_id in registered_edges:
                    self.message_requested.emit(
                        "Registered task structure is locked. Add a new task/branch "
                        "instead of deleting or rewiring the registered lineage."
                    )
                    event.accept()
                    return
        return original_key_press(self, event)

    DragNodeWorkflowCanvas.keyPressEvent = key_press

    # Imported .atonce.json is a draft, not persisted registration evidence.
    original_import = CanvasWorkflowIOMixin._import_workflow

    def import_workflow(self):
        result = original_import(self)
        if bool(getattr(self, "_imported_draft", False)):
            self._registered_node_ids = set()
            self._registered_edge_ids = set()
            self._render_graph()
        return result

    CanvasWorkflowIOMixin._import_workflow = import_workflow

    _APPLIED = True
