"""FME-like simplified drag-first workflow authoring surface.

This module owns canvas orchestration only. Block dialogs, presentation-state
management, portable workflow I/O and graphics items live in focused modules.
"""

from uuid import uuid4

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QGraphicsPathItem,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core.canvas_field_display import operation_field_summary, operation_input_fields
from ..core.canvas_presentation import empty_canvas_presentation, extract_canvas_presentation
from ..core.lineage import RESERVED_LINEAGE_FIELDS
from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY
from ..core.provisional_connections import connect_provisional_operation_input
from ..models.dependency_graph import DependencyGraph, DependencyNode, NodeKind
from .canvas_icons import material_icon
from .canvas_items import (
    CanvasTextItem,
    DragCanvasBlockItem,
    DragFirstCanvasView,
    DraggableBlockTemplate,
    GroupBoardItem,
    LineageConnectionItem,
)
from .canvas_presentation_controller import CanvasPresentationMixin
from .canvas_workflow_io import CanvasWorkflowIOMixin
from .freeform_workflow_canvas import FreeformWorkflowCanvas
from .material_block_dialogs import MaterialCanvasBlockDialogMixin


class DragNodeWorkflowCanvas(
    MaterialCanvasBlockDialogMixin,
    CanvasPresentationMixin,
    CanvasWorkflowIOMixin,
    FreeformWorkflowCanvas,
):
    """Production FME-like simplified authoring canvas."""

    def _build_once(self):
        self._presentation = empty_canvas_presentation()
        self._preview_connection = None
        self._imported_draft = False

        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(6)

        self.workflow_name_edit = QLineEdit(self)
        self.workflow_name_edit.setObjectName("AtOnceFreeformWorkflowName")
        self.workflow_name_edit.setPlaceholderText("Workflow name…")
        root.addWidget(self.workflow_name_edit)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        toolbar.setContentsMargins(0, 0, 0, 0)
        self.source_template = DraggableBlockTemplate("source", "SOURCE", self)
        self.operation_template = DraggableBlockTemplate("operation", "OPERATION", self)
        self.output_template = DraggableBlockTemplate("output", "OUTPUT", self)
        self.text_template = DraggableBlockTemplate("text", "TEXT", self)
        self.group_template = DraggableBlockTemplate("group", "GROUP", self)
        for item in (
            self.source_template,
            self.operation_template,
            self.output_template,
            self.text_template,
            self.group_template,
        ):
            toolbar.addWidget(item)
        toolbar.addStretch(1)

        self.import_button = QPushButton("Import", self)
        self.export_button = QPushButton("Export", self)
        self.run_button = QPushButton("Register", self)
        self.import_button.setIcon(material_icon("file_download"))
        self.export_button.setIcon(material_icon("file_upload"))
        self.run_button.setIcon(material_icon("person_add", QColor("#FFFFFF")))
        self.run_button.setObjectName("AtOncePrimary")
        self.import_button.setToolTip(
            "Import and overwrite the workflow draft from an .atonce.json file"
        )
        self.export_button.setToolTip("Export this reusable workflow as .atonce.json")
        self.run_button.setToolTip(
            "Register the lineage and create/update outputs included in Changes"
        )
        self.import_button.clicked.connect(self._import_workflow)
        self.export_button.clicked.connect(self._export_workflow)
        self.run_button.clicked.connect(self.plan_requested.emit)
        toolbar.addWidget(self.import_button)
        toolbar.addWidget(self.export_button)
        toolbar.addWidget(self.run_button)
        root.addLayout(toolbar)

        self.view = DragFirstCanvasView(self.gateway, self)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.template_dropped.connect(self._template_dropped)
        self.view.layer_dropped.connect(self._layer_dropped)
        self.view.invalid_drop.connect(self.message_requested.emit)
        root.addWidget(self.view, 1)

    def set_workflow(self, workflow):
        self._presentation = (
            extract_canvas_presentation(workflow.effective_dependency_graph())
            if workflow is not None
            else empty_canvas_presentation()
        )
        self._imported_draft = False
        super().set_workflow(workflow)

    def clear_canvas(self):
        self._workflow = None
        self._graph = DependencyGraph()
        self._source_refs = {}
        self._presentation = empty_canvas_presentation()
        self._legacy_display = False
        self._selected_node_id = None
        self._selected_output_node_id = None
        self._pending_connection = None
        self._stale_node_ids = set()
        self._imported_draft = False
        self.workflow_name_edit.clear()
        self._render_graph()
        self.graph_changed.emit()

    def _template_dropped(self, kind, scene_pos):
        if kind == "source":
            from ..core.freeform_graph import source_node

            self._graph.nodes.append(
                source_node(name="Select layer", x=scene_pos.x(), y=scene_pos.y())
            )
        elif kind == "operation":
            self._graph.nodes.append(
                DependencyNode(
                    node_id=f"operation:unconfigured:{uuid4()}",
                    name="Select function",
                    kind=NodeKind.DERIVED,
                    metadata={
                        "workflow_role": "operation",
                        "operation_kind": "",
                        "parameters": {},
                        "canvas": {
                            "x": float(scene_pos.x()),
                            "y": float(scene_pos.y()),
                        },
                    },
                )
            )
        elif kind == "output":
            from ..core.freeform_graph import output_node

            self._graph.nodes.append(
                output_node(
                    "",
                    name="Output",
                    path="",
                    x=scene_pos.x(),
                    y=scene_pos.y(),
                )
            )
        elif kind == "text":
            self._presentation["texts"].append(
                {
                    "id": f"text:{uuid4()}",
                    "text": "Text note",
                    "x": float(scene_pos.x()),
                    "y": float(scene_pos.y()),
                }
            )
        elif kind == "group":
            self._presentation["groups"].append(
                {
                    "id": f"group:{uuid4()}",
                    "title": "Group",
                    "x": float(scene_pos.x()),
                    "y": float(scene_pos.y()),
                    "width": 460.0,
                    "height": 260.0,
                    "members": [],
                }
            )
        else:
            return
        self._render_graph()
        self.graph_changed.emit()

    def _available_source_fields(self, node):
        if self.gateway is None:
            return ()
        lineage = str(node.metadata.get("source_lineage_id") or "")
        ref = self._source_refs.get(lineage)
        layer = self.gateway.resolve_layer(ref.current_layer_id) if ref else None
        if layer is None or not hasattr(layer, "fields"):
            return ()
        return tuple(
            str(field.name())
            for field in layer.fields()
            if str(field.name()) not in RESERVED_LINEAGE_FIELDS
        )

    def _auto_fields_for_source(self, node):
        available = set(self._available_source_fields(node))
        if not available:
            return ()
        queue = [node.node_id]
        visited = set()
        found = []
        node_map = self._graph.node_map()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for edge in self._graph.outgoing_edges(current):
                target = node_map.get(edge.to_node)
                if target is None or target.kind != NodeKind.DERIVED:
                    continue
                target_port = str(edge.parameters.get("target_port") or "input")
                for field in operation_input_fields(
                    target.metadata.get("operation_kind"),
                    target.metadata.get("parameters") or {},
                    target_port,
                ):
                    if field in available and field not in found:
                        found.append(field)
                queue.append(target.node_id)
        return tuple(found)

    def _selected_source_fields(self, node):
        if "display_fields" in node.metadata:
            return tuple(str(item) for item in node.metadata.get("display_fields") or ())
        return self._auto_fields_for_source(node)

    @staticmethod
    def _operation_field_summary(node):
        return operation_field_summary(
            node.metadata.get("operation_kind"),
            node.metadata.get("parameters") or {},
        )

    def _render_graph(self):
        self._clear_scene()
        self._preview_connection = None

        for group in self._presentation.get("groups", []):
            item = GroupBoardItem(group)
            item.moved_delta.connect(self._group_moved_delta)
            item.geometry_committed.connect(self._group_geometry_committed)
            item.rename_requested.connect(self._rename_group)
            self.view.scene().addItem(item)

        node_map = self._graph.node_map()
        for node in self._graph.nodes:
            definition = None
            if node.kind == NodeKind.DERIVED:
                definition = DEFAULT_OPERATION_REGISTRY.get(
                    str(node.metadata.get("operation_kind") or "")
                )
            item = DragCanvasBlockItem(
                node,
                definition,
                available_fields=(
                    self._available_source_fields(node)
                    if node.kind == NodeKind.SOURCE
                    else ()
                ),
                selected_fields=(
                    self._selected_source_fields(node)
                    if node.kind == NodeKind.SOURCE
                    else ()
                ),
                operation_fields=(
                    self._operation_field_summary(node)
                    if node.kind == NodeKind.DERIVED
                    else ()
                ),
            )
            item.set_stale(
                node.node_id in self._stale_node_ids,
                "Upstream changed; rerun this branch to update it."
                if node.node_id in self._stale_node_ids
                else "",
            )
            item.clicked.connect(self._block_clicked)
            item.delete_requested.connect(self._remove_node_requested)
            item.port_pressed.connect(self._port_pressed)
            item.port_dragged.connect(self._port_dragged)
            item.port_released.connect(self._port_released)
            item.moved.connect(lambda _node_id: self._refresh_connections())
            item.position_committed.connect(self._node_position_committed)
            item.source_field_toggled.connect(self._source_field_toggled)
            self.view.scene().addItem(item)
            self._items[node.node_id] = item

        for edge in self._graph.edges:
            source_item = self._items.get(edge.from_node)
            target_item = self._items.get(edge.to_node)
            if source_item is None or target_item is None:
                continue
            target = node_map.get(edge.to_node)
            included = True
            if target is not None and target.kind == NodeKind.DELIVERY:
                included = bool(
                    target.metadata.get("include_in_changes", edge.enabled_by_default)
                )
            connection = LineageConnectionItem(
                source_item,
                target_item,
                edge.parameters.get("target_port", "input"),
                edge.edge_id,
                included=included,
                remove_callback=self._remove_edge_requested,
            )
            self.view.scene().addItem(connection)
            self._connections.append(connection)

        for text in self._presentation.get("texts", []):
            item = CanvasTextItem(text)
            item.edit_requested.connect(self._edit_text)
            item.position_committed.connect(self._text_position_committed)
            self.view.scene().addItem(item)

        self._refresh_connections()
        self._update_scene_bounds()
        self.readiness_changed.emit(bool(self._graph.nodes))

    def _update_scene_bounds(self):
        bounds = self.view.scene().itemsBoundingRect()
        if bounds.isNull():
            bounds = QRectF(-300, -250, 1200, 900)
        else:
            bounds = bounds.adjusted(-350, -300, 450, 400)
        self.view.scene().setSceneRect(bounds)

    def _block_clicked(self, node_id):
        self._selected_node_id = str(node_id)
        self.block_selected.emit(self._selected_node_id)
        node = self._graph.node_map().get(self._selected_node_id)
        if node is None:
            return
        if node.kind == NodeKind.SOURCE:
            self._show_source_dialog(node)
        elif node.kind == NodeKind.DERIVED:
            self._show_operation_dialog(node)
        elif node.kind == NodeKind.DELIVERY:
            self._show_output_dialog(node)

    def _port_pressed(self, node_id, direction, port_id):
        if direction == "out":
            self._pending_connection = (node_id, port_id)
            self._remove_preview_connection()
            item = self._items.get(str(node_id))
            if item is not None:
                preview = QGraphicsPathItem()
                preview.setZValue(-0.5)
                pen = QPen(QColor("#8C999F"), 1.6)
                pen.setStyle(Qt.DashLine)
                preview.setPen(pen)
                start = item.output_scene_pos()
                path = QPainterPath(start)
                path.lineTo(start)
                preview.setPath(path)
                self.view.scene().addItem(preview)
                self._preview_connection = preview
            return
        super()._port_pressed(node_id, direction, port_id)

    def _port_dragged(self, node_id, scene_pos):
        if self._preview_connection is None:
            return
        item = self._items.get(str(node_id))
        if item is None:
            return
        path = QPainterPath(item.output_scene_pos())
        path.lineTo(scene_pos)
        self._preview_connection.setPath(path)

    def _remove_preview_connection(self):
        if self._preview_connection is not None:
            try:
                self.view.scene().removeItem(self._preview_connection)
            except RuntimeError:
                pass
        self._preview_connection = None

    def _port_released(self, node_id, direction, port_id, scene_pos):
        self._remove_preview_connection()
        super()._port_released(node_id, direction, port_id, scene_pos)

    @staticmethod
    def _port_at_scene_position(item, scene_pos):
        # Unconfigured OPERATION blocks deliberately expose one generic input
        # port. This lets the user wire context before choosing the function.
        if item.node.kind == NodeKind.DERIVED and not str(
            item.node.metadata.get("operation_kind") or ""
        ):
            return (
                "input"
                if abs(item.input_scene_pos("input").y() - scene_pos.y()) <= 16
                else None
            )
        if item.node.kind == NodeKind.DELIVERY:
            return (
                "input"
                if abs(item.input_scene_pos("input").y() - scene_pos.y()) <= 14
                else None
            )
        if item.definition is None:
            return None
        for index, port in enumerate(item.definition.input_ports):
            y = item.mapToScene(QPointF(4, item._input_y(index))).y()
            if abs(y - scene_pos.y()) <= 14:
                return port.port_id
        return None

    def _complete_connection(self, from_node_id, node_id, port_id):
        target = self._graph.node_map().get(node_id)
        if target is not None and target.kind == NodeKind.DERIVED and not str(
            target.metadata.get("operation_kind") or ""
        ):
            try:
                self._graph = connect_provisional_operation_input(
                    self._graph,
                    from_node_id,
                    node_id,
                )
            except Exception as exc:
                self.message_requested.emit(str(exc))
                return
            self._render_graph()
            self.graph_changed.emit()
            return
        super()._complete_connection(from_node_id, node_id, port_id)
