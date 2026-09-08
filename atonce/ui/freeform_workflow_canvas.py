"""Lightweight free-form DAG canvas.

This widget is a configuration surface only.  It owns a scene and graph-edit
intent, while the plugin/service layer owns source preparation and execution.
"""

from dataclasses import replace

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGraphicsObject,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsMimeDataUtils

from ..core.freeform_graph import (
    connect_graph_nodes,
    operation_node,
    output_node,
    source_node,
)
from ..core.graph_editing import GraphEditError
from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY, InferredSchema, OperationDefinition
from ..models.dependency_graph import DependencyGraph, NodeKind
from ..models.workflow import LayerRef, SOURCE_ROLE
from ..models.workflow import LEGACY_GRAPH_ORIGIN
from .guided_workflow_builder import GuidedFilterBuilder
from .operation_parameter_editor import OperationParameterEditor


LAYER_URI_MIME = "application/x-vnd.qgis.qgis.uri"
LAYER_TREE_MIME = "application/qgis.layertreemodeldata"


def _value(value, fallback=""):
    return value() if callable(value) else value or fallback


class CanvasPortItem:
    """Small semantic port descriptor rendered by its owning block item."""

    def __init__(self, node_id, port_id, direction):
        self.node_id = str(node_id)
        self.port_id = str(port_id)
        self.direction = direction


class CanvasConnectionItem(QGraphicsPathItem):
    """A selectable connector with an explicit midpoint remove control."""

    def __init__(
        self,
        source_item,
        target_item,
        target_port="input",
        edge_id="",
        parent=None,
        remove_callback=None,
    ):
        super().__init__(parent)
        self.source_item = source_item
        self.target_item = target_item
        self.target_port = str(target_port or "input")
        self.edge_id = str(edge_id or "")
        self.remove_callback = remove_callback
        self.setPen(QPen(QColor("#7B8D96"), 2.0))
        self.setZValue(-1)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.update_path()

    def update_path(self):
        start = self.source_item.output_scene_pos()
        end = self.target_item.input_scene_pos(self.target_port)
        distance = max(35.0, (end.x() - start.x()) * 0.45)
        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x() + distance, start.y()),
            QPointF(end.x() - distance, end.y()),
            end,
        )
        self.setPath(path)

    def _remove_center(self):
        return self.path().pointAtPercent(0.5)

    def boundingRect(self):  # noqa: N802 - Qt API
        return super().boundingRect().adjusted(-9, -9, 9, 9)

    def shape(self):
        shape = super().shape()
        center = self._remove_center()
        shape.addEllipse(center, 10, 10)
        return shape

    def paint(self, painter, option, widget=None):  # noqa: N802 - Qt API
        super().paint(painter, option, widget)
        center = self._remove_center()
        painter.setPen(QPen(QColor("#A33A32"), 1.0))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        painter.drawEllipse(center, 7, 7)
        painter.drawLine(center + QPointF(-2.5, -2.5), center + QPointF(2.5, 2.5))
        painter.drawLine(center + QPointF(-2.5, 2.5), center + QPointF(2.5, -2.5))

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if (
            event.button() == Qt.LeftButton
            and (event.pos() - self._remove_center()).manhattanLength() <= 13
            and callable(self.remove_callback)
        ):
            self.remove_callback(self.edge_id)
            event.accept()
            return
        super().mousePressEvent(event)


class CanvasBlockItem(QGraphicsObject):
    clicked = pyqtSignal(str)
    port_pressed = pyqtSignal(str, str, str)
    port_released = pyqtSignal(str, str, str, QPointF)
    moved = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    WIDTH = 220
    HEIGHT = 92

    def __init__(self, node, definition: OperationDefinition = None, parent=None):
        super().__init__(parent)
        self.node = node
        self.definition = definition
        self.setFlag(QGraphicsObject.ItemIsMovable, True)
        self.setFlag(QGraphicsObject.ItemIsSelectable, True)
        self.setFlag(QGraphicsObject.ItemSendsGeometryChanges, True)
        canvas = node.metadata.get("canvas") or {}
        self.setPos(float(canvas.get("x", 80)), float(canvas.get("y", 80)))
        self.setAcceptHoverEvents(True)
        self._port_drag = None
        self._stale = False
        self._stale_tooltip = ""

    def set_stale(self, stale, tooltip=""):
        self._stale = bool(stale)
        self._stale_tooltip = str(tooltip or "")
        self.setToolTip(self._stale_tooltip if self._stale else "")
        self.update()

    def boundingRect(self):
        input_count = len(self.definition.input_ports) if self.definition else (0 if self.node.kind == NodeKind.SOURCE else 1)
        output_count = 0 if self.node.kind == NodeKind.DELIVERY else 1
        height = max(self.HEIGHT, 44 + 22 * max(input_count, output_count))
        return QRectF(0, 0, self.WIDTH, height)

    def _fill(self):
        if self.node.kind == NodeKind.SOURCE:
            return QColor("#E8F2F5")
        if self.node.kind == NodeKind.DELIVERY:
            return QColor("#F6F7F7")
        category = self.definition.category if self.definition else "Data"
        return {
            "Data": QColor("#EDF4F7"),
            "Spatial": QColor("#FAF5EA"),
            "Utility": QColor("#EEF5EA"),
        }.get(category, QColor("#EDF4F7"))

    def _title(self):
        if self.node.kind == NodeKind.SOURCE:
            return "SOURCE"
        if self.node.kind == NodeKind.DELIVERY:
            return "OUTPUT"
        return self.definition.title if self.definition else self.node.name

    def paint(self, painter, option, widget=None):  # noqa: N802 - Qt API
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        pen = QPen(QColor("#6D7C83"), 1.4)
        if self.node.kind == NodeKind.DELIVERY:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(self._fill()))
        painter.drawRoundedRect(rect, 9, 9)
        painter.setPen(QColor("#223139"))
        painter.drawText(QRectF(14, 11, self.WIDTH - 28, 22), Qt.AlignLeft, self._title())
        painter.setPen(QColor("#5D6C73"))
        detail = self.node.name
        if self.node.kind == NodeKind.DELIVERY:
            detail = f"{self.node.format.upper()} · {self.node.metadata.get('path') or 'Choose output path'}"
        elif self.node.kind == NodeKind.SOURCE:
            detail = self.node.name or "Drop a vector layer here"
        elif self.node.metadata.get("parameters"):
            detail = str(self.node.metadata["parameters"].get("expression") or "Configure this block")
        painter.drawText(QRectF(14, 35, self.WIDTH - 28, 35), Qt.TextWordWrap, detail)

        if self.node.kind != NodeKind.SOURCE:
            ports = self.definition.input_ports if self.definition else ()
            for index, port in enumerate(ports or (CanvasPortItem(self.node.node_id, "input", "in"),)):
                y = 49 + index * 22
                painter.setBrush(QBrush(QColor("#FFFFFF")))
                painter.setPen(QPen(QColor("#66777F"), 1.2))
                painter.drawEllipse(QPointF(4, y), 5, 5)
                painter.setPen(QColor("#68777D"))
                painter.drawText(QRectF(14, y - 9, 115, 18), Qt.AlignLeft, getattr(port, "label", "Input"))
        if self.node.kind != NodeKind.DELIVERY:
            y = 49
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.setPen(QPen(QColor("#66777F"), 1.2))
            painter.drawEllipse(QPointF(self.WIDTH - 4, y), 5, 5)
        if self._stale:
            painter.setPen(QPen(QColor("#B42318"), 1.0))
            painter.setBrush(QBrush(QColor("#D92D20")))
            painter.drawEllipse(QPointF(11, 11), 10, 10)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(5, 1, 12, 20), Qt.AlignCenter, "!")
        self._paint_delete_control(painter)

    def _delete_rect(self):
        return QRectF(self.WIDTH - 28, 7, 20, 20)

    def _paint_delete_control(self, painter):
        rect = self._delete_rect()
        painter.setPen(QPen(QColor("#7A3E39"), 1.0))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        painter.drawEllipse(rect)
        center = rect.center()
        painter.drawLine(center + QPointF(-3, -3), center + QPointF(3, 3))
        painter.drawLine(center + QPointF(-3, 3), center + QPointF(3, -3))

    def _delete_hit(self, point):
        return self._delete_rect().adjusted(-3, -3, 3, 3).contains(point)

    def input_scene_pos(self, port_id="input"):
        if self.definition:
            index = next((i for i, item in enumerate(self.definition.input_ports) if item.port_id == port_id), 0)
        else:
            index = 0
        return self.mapToScene(QPointF(4, 49 + index * 22))

    def output_scene_pos(self):
        return self.mapToScene(QPointF(self.WIDTH - 4, 49))

    def itemChange(self, change, value):  # noqa: N802 - Qt API
        if change == QGraphicsObject.ItemPositionHasChanged:
            canvas = self.node.metadata.setdefault("canvas", {})
            canvas["x"] = float(value.x())
            canvas["y"] = float(value.y())
            self.moved.emit(self.node.node_id)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            point = event.pos()
            if self._delete_hit(point):
                self.delete_requested.emit(self.node.node_id)
                event.accept()
                return
            if point.x() <= 12 and self.node.kind != NodeKind.SOURCE:
                port_id = "input"
                if self.definition:
                    index = max(0, int((point.y() - 40) // 22))
                    if index < len(self.definition.input_ports):
                        port_id = self.definition.input_ports[index].port_id
                self.port_pressed.emit(self.node.node_id, "in", port_id)
                event.accept()
                return
            if point.x() >= self.WIDTH - 14 and self.node.kind != NodeKind.DELIVERY:
                self._port_drag = ("out", "result")
                self.port_pressed.emit(self.node.node_id, "out", "result")
                event.accept()
                return
            self.clicked.emit(self.node.node_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API
        if self._port_drag is not None:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API
        if self._port_drag is not None:
            direction, port_id = self._port_drag
            self._port_drag = None
            self.port_released.emit(
                self.node.node_id,
                direction,
                port_id,
                self.mapToScene(event.pos()),
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)


class FreeformGraphicsView(QGraphicsView):
    layer_dropped = pyqtSignal(object, QPointF)
    invalid_drop = pyqtSignal(str)

    def __init__(self, gateway, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self.setAcceptDrops(True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QColor("#FFFFFF"))
        self.setScene(QGraphicsScene(self))
        self.scene().setSceneRect(-1200, -1200, 3600, 3000)
        self.setMinimumHeight(420)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        mime = event.mimeData()
        if mime.hasFormat(LAYER_URI_MIME) or mime.hasFormat(LAYER_TREE_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        try:
            uris = QgsMimeDataUtils.decodeUriList(event.mimeData())
        except Exception as exc:
            self.invalid_drop.emit(f"Could not read the QGIS layer drag data: {exc}")
            event.ignore()
            return
        for uri in uris:
            layer_id = _value(getattr(uri, "layerId", ""))
            layer = self.gateway.resolve_layer(layer_id) if self.gateway else None
            if layer is None:
                continue
            if not self.gateway.is_vector_layer(layer) and not self.gateway.is_raster_layer(layer):
                self.invalid_drop.emit(
                    "Only loaded vector or raster layers can be used in an AtOnce workflow."
                )
                event.ignore()
                return
            self.layer_dropped.emit(layer, self.mapToScene(event.pos()))
            event.acceptProposedAction()
            return
        self.invalid_drop.emit("Drop a vector or raster layer from the current QGIS Layers panel.")
        event.ignore()


class FreeformWorkflowCanvas(QWidget):
    """Main free-form authoring surface with stable graph IDs and ports."""

    configuration_requested = pyqtSignal()
    plan_requested = pyqtSignal()
    readiness_changed = pyqtSignal(bool)
    message_requested = pyqtSignal(str)
    result_requested = pyqtSignal()
    source_relink_index_requested = pyqtSignal(int)
    block_selected = pyqtSignal(str)
    graph_changed = pyqtSignal()

    def __init__(self, gateway=None, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self._workflow = None
        self._graph = DependencyGraph()
        self._source_refs = {}
        self._legacy_display = False
        self._items = {}
        self._connections = []
        self._selected_node_id = None
        self._selected_output_node_id = None
        self._pending_connection = None
        self._stale_node_ids = set()
        self._build_once()

    def _build_once(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(5)
        self.workflow_name_edit = QLineEdit(self)
        self.workflow_name_edit.setObjectName("AtOnceFreeformWorkflowName")
        self.workflow_name_edit.setPlaceholderText("Workflow name…")
        root.addWidget(self.workflow_name_edit)
        self.palette = QHBoxLayout()
        self.add_source_button = QPushButton("+ Source")
        self.add_source_button.clicked.connect(self._add_source)
        self.palette.addWidget(self.add_source_button)
        self.add_operation_button = QToolButton()
        self.add_operation_button.setText("+ Operation")
        self.add_operation_button.setPopupMode(QToolButton.InstantPopup)
        operation_menu = QMenu(self.add_operation_button)
        categories = {}
        for definition in DEFAULT_OPERATION_REGISTRY.all():
            categories.setdefault(definition.category, operation_menu.addMenu(definition.category))
            action = categories[definition.category].addAction(definition.title)
            action.triggered.connect(
                lambda _checked=False, kind=definition.kind: self._add_operation(kind)
            )
        self.add_operation_button.setMenu(operation_menu)
        self.palette.addWidget(self.add_operation_button)
        self.add_output_button = QPushButton("+ Output")
        self.add_output_button.clicked.connect(self._add_output)
        self.palette.addWidget(self.add_output_button)
        root.addLayout(self.palette)

        self.view = FreeformGraphicsView(self.gateway, self)
        self.view.layer_dropped.connect(self._layer_dropped)
        self.view.invalid_drop.connect(self.message_requested.emit)
        root.addWidget(self.view, 1)

        self.editor_stack = QStackedWidget(self)
        self.editor_empty = QLabel("Click a block to configure only that block.")
        self.editor_empty.setObjectName("AtOnceFreeformEditorHint")
        self.editor_empty.setWordWrap(True)
        self.editor_stack.addWidget(self.editor_empty)
        self.filter_editor = GuidedFilterBuilder(parent=self)
        self.filter_editor.expression_changed.connect(self._filter_expression_changed)
        self.editor_stack.addWidget(self.filter_editor)
        self.operation_editor = OperationParameterEditor(parent=self)
        self.operation_editor.parameters_changed.connect(self._operation_parameters_changed)
        self.editor_stack.addWidget(self.operation_editor)
        self.output_editor = QWidget(self)
        output_layout = QHBoxLayout(self.output_editor)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.output_format = QComboBox(self.output_editor)
        for label, value in (
            ("GeoJSON", "geojson"),
            ("Shapefile", "shapefile"),
            ("KML", "kml"),
            ("KMZ", "kmz"),
            ("GeoPackage", "gpkg"),
            ("GeoTIFF", "geotiff"),
        ):
            self.output_format.addItem(label, value)
        self.output_path = QLineEdit(self.output_editor)
        self.output_path.setPlaceholderText("Choose an output path…")
        self.output_enabled = QCheckBox("Enabled by default", self.output_editor)
        self.output_enabled.setChecked(True)
        output_apply = QPushButton("Apply", self.output_editor)
        output_apply.clicked.connect(self._apply_output)
        output_layout.addWidget(self.output_format)
        output_layout.addWidget(self.output_path, 1)
        output_layout.addWidget(self.output_enabled)
        output_layout.addWidget(output_apply)
        self.editor_stack.addWidget(self.output_editor)
        root.addWidget(self.editor_stack)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Graph configuration is saved separately from Plan."))
        actions.addStretch(1)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.configuration_requested.emit)
        self.plan_button = QPushButton("> Plan")
        self.plan_button.clicked.connect(self.plan_requested.emit)
        actions.addWidget(self.save_button)
        actions.addWidget(self.plan_button)
        root.addLayout(actions)

    @property
    def graph(self):
        return self._graph

    def can_plan(self):
        """Return the lightweight draft readiness used by the dock action.

        Complete graph validation remains the plugin/service responsibility;
        this method only prevents an empty canvas from presenting an active
        Plan action.
        """

        return bool(self._graph.nodes) and not self._legacy_display

    def _clear_scene(self):
        self._items.clear()
        self._connections.clear()
        self.view.scene().clear()

    def set_workflow(self, workflow):
        self._workflow = workflow
        self._source_refs = {}
        self._legacy_display = False
        self._stale_node_ids = set()
        if workflow is None:
            self._graph = DependencyGraph()
            self.workflow_name_edit.clear()
        else:
            self.workflow_name_edit.setText(str(workflow.name or ""))
            self._graph = DependencyGraph.from_dict(workflow.effective_dependency_graph().to_dict())
            self._source_refs = {ref.stable_id: ref for ref in workflow.source_layers}
            self._legacy_display = workflow.effective_dependency_graph_origin() == LEGACY_GRAPH_ORIGIN
            if self._legacy_display:
                node_map = self._graph.node_map()
                for node in self._graph.nodes:
                    if node.kind != NodeKind.DERIVED:
                        continue
                    incoming = self._graph.incoming_edges(node.node_id)
                    if not incoming:
                        continue
                    node.metadata["operation_kind"] = incoming[0].operation.value
                    node.metadata["parameters"] = dict(incoming[0].parameters)
        self._render_graph()

    def _render_graph(self):
        self._clear_scene()
        for node in self._graph.nodes:
            definition = None
            if node.kind == NodeKind.DERIVED:
                definition = DEFAULT_OPERATION_REGISTRY.get(node.metadata.get("operation_kind"))
            item = CanvasBlockItem(node, definition)
            item.set_stale(
                node.node_id in self._stale_node_ids,
                "Upstream changed; rerun this branch to update it."
                if node.node_id in self._stale_node_ids
                else "",
            )
            item.clicked.connect(self._block_clicked)
            item.delete_requested.connect(self._remove_node_requested)
            item.port_pressed.connect(self._port_pressed)
            item.port_released.connect(self._port_released)
            item.moved.connect(lambda _node_id: self._refresh_connections())
            self.view.scene().addItem(item)
            self._items[node.node_id] = item
        node_map = self._graph.node_map()
        for edge in self._graph.edges:
            source_item = self._items.get(edge.from_node)
            target_item = self._items.get(edge.to_node)
            if source_item is None or target_item is None:
                continue
            connection = CanvasConnectionItem(
                source_item,
                target_item,
                edge.parameters.get("target_port", "input"),
                edge.edge_id,
                remove_callback=self._remove_edge_requested,
            )
            self.view.scene().addItem(connection)
            self._connections.append(connection)
        self._refresh_connections()
        self.readiness_changed.emit(bool(self._graph.nodes))

    def _refresh_connections(self):
        for connection in self._connections:
            connection.update_path()

    def _add_source(self):
        node = source_node(name="Drop a vector layer here", x=80 + len(self._items) * 22, y=80 + len(self._items) * 22)
        self._graph.nodes.append(node)
        self._render_graph()
        self._choose_source(node.node_id)

    def _add_operation(self, kind):
        node = operation_node(kind, x=320 + len(self._items) * 18, y=100 + len(self._items) * 18)
        self._graph.nodes.append(node)
        self._render_graph()
        self._block_clicked(node.node_id)

    def _add_output(self):
        from qgis.PyQt.QtWidgets import QInputDialog

        labels = ["GeoJSON", "Shapefile", "KML", "KMZ", "GeoPackage", "GeoTIFF"]
        values = ["geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"]
        label, accepted = QInputDialog.getItem(self, "Add output", "Format", labels, 0, False)
        if not accepted:
            return
        format_name = values[labels.index(label)]
        suffix = {
            "geojson": "*.geojson",
            "shapefile": "*.shp",
            "kml": "*.kml",
            "kmz": "*.kmz",
            "gpkg": "*.gpkg",
            "geotiff": "*.tif",
        }[format_name]
        node = output_node(format_name, x=620 + len(self._items) * 18, y=100 + len(self._items) * 18)
        path, _ = QFileDialog.getSaveFileName(self, "Choose output path", "", suffix)
        if not path:
            return
        node.metadata["path"] = path
        self._graph.nodes.append(node)
        self._render_graph()

    def _choose_source(self, node_id):
        if self.gateway is None:
            self.message_requested.emit("No loaded layer gateway is available.")
            return
        layers = list(self.gateway.source_candidate_layers())
        if not layers:
            self.message_requested.emit(
                "No loaded vector or raster layers are available in this QGIS project."
            )
            return
        from qgis.PyQt.QtWidgets import QInputDialog

        names = []
        for layer in layers:
            label = str(_value(getattr(layer, "name", ""), "Unnamed layer"))
            kind = self.gateway.layer_data_type(layer)
            names.append(f"{label} ({kind})")
        choice, accepted = QInputDialog.getItem(
            self, "Choose source layer", "Layer", names, 0, False
        )
        if accepted:
            self._bind_source(node_id, layers[names.index(choice)])

    def _bind_source(self, node_id, layer):
        if self.gateway is None:
            self.message_requested.emit("No loaded layer gateway is available.")
            return
        try:
            data_type = self.gateway.layer_data_type(layer)
        except ValueError:
            self.message_requested.emit(
                "Only loaded vector or raster layers can be used in an AtOnce workflow."
            )
            return
        node = self._graph.node_map().get(node_id)
        if node is None or node.kind != NodeKind.SOURCE:
            return
        ref = self.gateway.make_source_layer_ref(layer)
        # Rebinding an existing visual source keeps its logical identity.
        old_lineage = str(node.metadata.get("source_lineage_id") or "")
        if old_lineage and old_lineage in self._source_refs:
            ref = LayerRef(
                layer_id=old_lineage,
                name=ref.name,
                role=SOURCE_ROLE,
                source_uri=ref.source_uri,
                provider=ref.provider,
                binding_id=ref.current_layer_id,
            )
        metadata = dict(node.metadata)
        metadata["source_lineage_id"] = ref.stable_id
        metadata["data_type"] = data_type
        self._source_refs[ref.stable_id] = ref
        self._replace_node(replace(node, name=ref.name, metadata=metadata))
        self.graph_changed.emit()

    def keyPressEvent(self, event):  # noqa: N802 - QWidget API
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            selected = list(self.view.scene().selectedItems())
            for item in selected:
                if isinstance(item, CanvasConnectionItem):
                    self._remove_edge_requested(item.edge_id)
                    return
                if isinstance(item, CanvasBlockItem):
                    self._remove_node_requested(item.node.node_id)
                    return
        super().keyPressEvent(event)

    def _graph_from_disconnect(self, edge_id):
        from ..core.graph_editing import disconnect_edge

        return disconnect_edge(self._graph, edge_id)

    def _graph_from_remove(self, node_id):
        from ..core.graph_editing import remove_node

        return remove_node(self._graph, node_id)

    def _remove_edge_requested(self, edge_id):
        wanted = str(edge_id)
        if not any(edge.edge_id == wanted for edge in self._graph.edges):
            # A stale graphics item can deliver one final queued click after the
            # first delete. Re-sync the scene instead of surfacing an exception.
            self._render_graph()
            return
        self._graph = self._graph_from_disconnect(wanted)
        self._render_graph()
        self.graph_changed.emit()

    def _remove_node_requested(self, node_id):
        wanted = str(node_id)
        node = self._graph.node_map().get(wanted)
        if node is None:
            # The first click may already have removed the graph node while a
            # now-stale item still has queued mouse events. Deletion is
            # intentionally idempotent.
            self._render_graph()
            return
        lineage = str(node.metadata.get("source_lineage_id") or "")
        self._graph = self._graph_from_remove(wanted)
        if lineage:
            self._source_refs.pop(lineage, None)
        if self._selected_node_id == wanted:
            self._selected_node_id = None
            editor_stack = getattr(self, "editor_stack", None)
            editor_empty = getattr(self, "editor_empty", None)
            if editor_stack is not None and editor_empty is not None:
                editor_stack.setCurrentWidget(editor_empty)
        if self._selected_output_node_id == wanted:
            self._selected_output_node_id = None
        self._render_graph()
        self.graph_changed.emit()

    def _layer_dropped(self, layer, scene_pos):
        item = self.view.scene().itemAt(scene_pos, self.view.transform())
        if isinstance(item, CanvasBlockItem) and item.node.kind == NodeKind.SOURCE:
            self._bind_source(item.node.node_id, layer)
            return
        self._add_source_at(layer, scene_pos)

    def _add_source_at(self, layer, scene_pos):
        node = source_node(name="Source", x=scene_pos.x(), y=scene_pos.y())
        self._graph.nodes.append(node)
        self._render_graph()
        self._bind_source(node.node_id, layer)

    def _replace_node(self, node):
        self._graph.nodes = [node if item.node_id == node.node_id else item for item in self._graph.nodes]
        self._render_graph()

    def _block_clicked(self, node_id):
        self._selected_node_id = str(node_id)
        self.block_selected.emit(self._selected_node_id)
        node = self._graph.node_map().get(self._selected_node_id)
        if node is None:
            return
        if node.kind == NodeKind.SOURCE and node.metadata.get("source_lineage_id"):
            source_ids = [item.node_id for item in self._graph.nodes if item.kind == NodeKind.SOURCE]
            index = source_ids.index(node.node_id)
            if self._workflow is not None:
                self.source_relink_index_requested.emit(index)
            else:
                self._choose_source(node.node_id)
            self.editor_stack.setCurrentWidget(self.editor_empty)
            return
        if node.kind == NodeKind.DERIVED and node.metadata.get("operation_kind") == "filter":
            self.filter_editor.set_layer(self._filter_context_layer(node.node_id))
            self.filter_editor.set_expression(str((node.metadata.get("parameters") or {}).get("expression") or ""))
            self.editor_stack.setCurrentWidget(self.filter_editor)
            return
        if node.kind == NodeKind.DELIVERY:
            self._selected_output_node_id = node.node_id
            index = self.output_format.findData(node.format)
            self.output_format.setCurrentIndex(max(0, index))
            self.output_path.setText(str(node.metadata.get("path") or ""))
            incoming = self._graph.incoming_edges(node.node_id)
            self.output_enabled.setChecked(
                incoming[0].enabled_by_default if incoming else True
            )
            self.editor_stack.setCurrentWidget(self.output_editor)
            return
        if node.kind == NodeKind.DERIVED:
            definition = DEFAULT_OPERATION_REGISTRY.get(node.metadata.get("operation_kind"))
            if definition is not None:
                self.operation_editor.set_definition(
                    definition,
                    node.metadata.get("parameters") or {},
                    self._operation_context_layers(node.node_id),
                )
                self.editor_stack.setCurrentWidget(self.operation_editor)
                return
        self.editor_empty.setText(
            f"{node.name}: configure this operation through its block editor."
        )
        self.editor_stack.setCurrentWidget(self.editor_empty)

    def _filter_context_layer(self, node_id):
        if self.gateway is None:
            return None
        node = self._graph.node_map().get(node_id)
        if node is None:
            return None
        incoming = self._graph.incoming_edges(node_id)
        if len(incoming) != 1:
            return None
        parent = self._graph.node_map().get(incoming[0].from_node)
        if parent is None:
            return None
        return self._context_layer_for_node(parent.node_id)

    def _filter_expression_changed(self, expression):
        node = self._graph.node_map().get(self._selected_node_id)
        if node is None or node.kind != NodeKind.DERIVED:
            return
        parameters = dict(node.metadata.get("parameters") or {})
        parameters["expression"] = str(expression or "")
        metadata = dict(node.metadata)
        metadata["parameters"] = parameters
        self._replace_node(replace(node, metadata=metadata))
        self.graph_changed.emit()

    def _operation_context_layers(self, node_id):
        if self.gateway is None:
            return ()
        layers = {}
        for edge in self._graph.incoming_edges(node_id):
            parent = self._graph.node_map().get(edge.from_node)
            if parent is None:
                continue
            layer = self._context_layer_for_node(parent.node_id)
            if layer is not None:
                port = str(edge.parameters.get("target_port") or "input")
                if port in layers:
                    layers[port] = (*layers[port], layer) if isinstance(layers[port], tuple) else (layers[port], layer)
                else:
                    layers[port] = layer
        return layers

    def _context_layer_for_node(self, node_id, visited=None):
        visited = set(visited or ())
        if node_id in visited:
            return None
        visited.add(node_id)
        node = self._graph.node_map().get(node_id)
        if node is None:
            return None
        if node.kind == NodeKind.SOURCE:
            lineage = str(node.metadata.get("source_lineage_id") or "")
            ref = self._source_refs.get(lineage)
            return self.gateway.resolve_layer(ref.current_layer_id) if ref else None
        if node.kind != NodeKind.DERIVED:
            return None
        definition = DEFAULT_OPERATION_REGISTRY.get(node.metadata.get("operation_kind"))
        if definition is None:
            return None
        schemas = {}
        for edge in self._graph.incoming_edges(node.node_id):
            parent_layer = self._context_layer_for_node(edge.from_node, visited)
            if parent_layer is None:
                continue
            field_specs = getattr(self.gateway, "field_specs", None)
            parent_schema = (
                field_specs(parent_layer)
                if callable(field_specs)
                else tuple(parent_layer.fields())
            )
            port = str(edge.parameters.get("target_port") or "input")
            if port in schemas:
                schemas[port] = (*schemas[port], parent_schema) if isinstance(schemas[port], tuple) else (schemas[port], parent_schema)
            else:
                schemas[port] = parent_schema
        try:
            inferred = definition.infer_output_schema(
                schemas, node.metadata.get("parameters") or {}
            )
            return InferredSchema(inferred)
        except Exception:
            return None

    def _operation_parameters_changed(self, parameters):
        node = self._graph.node_map().get(self._selected_node_id)
        if node is None or node.kind != NodeKind.DERIVED:
            return
        metadata = dict(node.metadata)
        metadata["parameters"] = dict(parameters or {})
        self._replace_node(replace(node, metadata=metadata))
        self.graph_changed.emit()

    def _apply_output(self):
        node = self._graph.node_map().get(self._selected_output_node_id)
        path = self.output_path.text().strip()
        if node is None or node.kind != NodeKind.DELIVERY:
            return
        if not path:
            self.message_requested.emit("Choose an output path before adding this output.")
            return
        from ..models.dependency_graph import DependencyNode

        metadata = dict(node.metadata)
        metadata["path"] = path
        metadata["format"] = str(self.output_format.currentData() or "geojson")
        updated = DependencyNode(
            node_id=node.node_id,
            name=node.name,
            kind=node.kind,
            format=metadata["format"],
            current_revision=node.current_revision,
            metadata=metadata,
        )
        self._replace_node(updated)
        self._graph.edges = [
            replace(
                edge,
                enabled_by_default=self.output_enabled.isChecked(),
            )
            if edge.to_node == node.node_id
            else edge
            for edge in self._graph.edges
        ]
        self._render_graph()
        self.graph_changed.emit()

    def _port_pressed(self, node_id, direction, port_id):
        if direction == "out":
            self._pending_connection = (node_id, port_id)
            return
        if self._pending_connection is None:
            return
        from_node_id, _from_port = self._pending_connection
        self._pending_connection = None
        self._complete_connection(from_node_id, node_id, port_id)

    def _port_released(self, node_id, direction, port_id, scene_pos):
        """Complete a connector dropped on a compatible input port."""

        if direction != "out":
            return
        target = None
        for item in self.view.scene().items(scene_pos):
            if isinstance(item, CanvasBlockItem) and item.node.node_id != node_id:
                target = item
                break
        if target is None or target.node.kind == NodeKind.SOURCE:
            self._pending_connection = None
            return
        target_port = self._port_at_scene_position(target, scene_pos)
        if target_port is None:
            self._pending_connection = None
            return
        self._pending_connection = None
        self._complete_connection(node_id, target.node.node_id, target_port)

    @staticmethod
    def _port_at_scene_position(item, scene_pos):
        if item.node.kind == NodeKind.DELIVERY:
            return "input" if item.input_scene_pos("input").y() - 9 <= scene_pos.y() <= item.input_scene_pos("input").y() + 9 else None
        if item.definition is None:
            return "input" if abs(item.input_scene_pos("input").y() - scene_pos.y()) <= 9 else None
        for index, port in enumerate(item.definition.input_ports):
            y = item.mapToScene(QPointF(4, 49 + index * 22)).y()
            if abs(y - scene_pos.y()) <= 9:
                return port.port_id
        return None

    def _complete_connection(self, from_node_id, node_id, port_id):
        try:
            self._graph = connect_graph_nodes(
                self._graph,
                from_node_id,
                node_id,
                target_port=port_id,
            )
        except GraphEditError as exc:
            self.message_requested.emit(str(exc))
            return
        self._render_graph()
        self.graph_changed.emit()

    def definition(self):
        """Return a new workflow model without running or touching QGIS."""

        if not self._graph.nodes or self._legacy_display:
            return None
        from ..core.freeform_workflow import build_freeform_workflow

        refs = [
            self._source_refs[str(node.metadata.get("source_lineage_id") or "")]
            for node in self._graph.nodes
            if node.kind == NodeKind.SOURCE
            and str(node.metadata.get("source_lineage_id") or "") in self._source_refs
        ]
        workflow_id = self._workflow.workflow_id if self._workflow is not None else None
        name = self.workflow_name_edit.text().strip() or (
            self._workflow.name if self._workflow is not None else "Untitled workflow"
        )
        return build_freeform_workflow(name=name, sources=refs, graph=self._graph, workflow_id=workflow_id)

    def final_result_node_id(self):
        # Free-form graphs have no global result.  This compatibility method is
        # intentionally only useful when there is exactly one terminal operation.
        terminals = [node for node in self._graph.nodes if node.kind == NodeKind.DELIVERY]
        if len(terminals) == 1:
            incoming = self._graph.incoming_edges(terminals[0].node_id)
            return incoming[0].from_node if incoming else None
        return None

    def set_result_evidence(self, _graph_state):
        self._stale_node_ids = set(
            getattr(_graph_state, "stale_node_ids", ()) or ()
        )
        self._stale_node_ids.difference_update(
            getattr(_graph_state, "refreshed_node_ids", ()) or ()
        )
        self._render_graph()

    def set_materialized(self, _materialized):
        """Compatibility hook used by the shared dock evidence renderer."""

        return None
