"""Block-first workflow canvas for AtOnce.

This module is deliberately UI/configuration only.  The existing plugin/service
runtime path owns source preparation, review, execution, and persistence.
"""

from uuid import uuid4

from qgis.PyQt.QtCore import QMimeData, Qt, pyqtSignal
from qgis.PyQt.QtGui import QDrag
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.explicit_workflow_creation import (
    build_standalone_filter_workflow,
    build_standalone_merge_workflow,
)
from ..core.explicit_workflow_editing import apply_explicit_workflow_edits
from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
)
from ..models.workflow import DeliveryRef, EXPLICIT_GRAPH_ORIGIN, WorkflowDefinition
from .guided_workflow_builder import GuidedFilterBuilder, SourceDropSlot


OPERATION_MIME = "application/x-atonce-operation"


class OperationPaletteButton(QPushButton):
    """A colour operation token that can be clicked or dragged onto the canvas."""

    operation_chosen = pyqtSignal(str)

    def __init__(self, title, operation, parent=None):
        super().__init__(title, parent)
        self.operation = str(operation)
        self.setObjectName(
            "AtOnceCanvasFilterPalette"
            if self.operation == "filter"
            else "AtOnceCanvasMergePalette"
        )
        self.setCursor(Qt.OpenHandCursor)
        self.clicked.connect(lambda: self.operation_chosen.emit(self.operation))

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API
        if event.buttons() & Qt.LeftButton:
            mime = QMimeData()
            mime.setData(OPERATION_MIME, self.operation.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec_(Qt.CopyAction)
            return
        super().mouseMoveEvent(event)


class OperationDropZone(QPushButton):
    """Dotted + Operation block accepting operation drags."""

    operation_dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("+ Operation\nDrag FILTER / MERGE here or click", parent)
        self.setObjectName("AtOnceCanvasOperationDropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(58)
        self.setCursor(Qt.PointingHandCursor)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if event.mimeData().hasFormat(OPERATION_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        if not event.mimeData().hasFormat(OPERATION_MIME):
            event.ignore()
            return
        operation = bytes(event.mimeData().data(OPERATION_MIME)).decode("utf-8").strip()
        if operation in {"filter", "merge"}:
            self.operation_dropped.emit(operation)
            event.acceptProposedAction()
            return
        event.ignore()


class BlockWorkflowCanvas(QWidget):
    """Compact canvas where every source/operation/result/output is a real block."""

    configuration_requested = pyqtSignal()
    plan_requested = pyqtSignal()
    readiness_changed = pyqtSignal(bool)
    message_requested = pyqtSignal(str)
    result_requested = pyqtSignal()
    source_relink_requested = pyqtSignal()

    _FORMATS = (
        ("GeoJSON", "geojson", ".geojson"),
        ("Shapefile", "shapefile", ".shp"),
        ("KML", "kml", ".kml"),
        ("KMZ", "kmz", ".kmz"),
        ("GeoPackage", "gpkg", ".gpkg"),
        ("GeoTIFF", "geotiff", ".tif"),
    )

    def __init__(self, gateway=None, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self._workflow = None
        self._source_refs = [None, None]
        self._operations = []
        self._filter_builders = {}
        self._deliveries = []
        self._editing_delivery_index = None
        self._draft_workflow_id = str(uuid4())
        self._result_state = "empty"
        self.setObjectName("AtOnceBlockWorkflowCanvas")
        self._build_once()
        self._reset_canvas()

    # ------------------------------------------------------------------
    # Persistent widget tree
    # ------------------------------------------------------------------
    def _build_once(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(7)

        hint = QLabel(
            "Build the lineage on the canvas. Click a block to configure it; > Plan runs it."
        )
        hint.setObjectName("AtOnceCanvasHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        caption = QLabel("SOURCES")
        caption.setObjectName("AtOnceCanvasCaption")
        root.addWidget(caption)
        self._slots = []
        for index, title in enumerate(("Source", "Source B")):
            slot = SourceDropSlot(self.gateway, title, self)
            slot.clicked.connect(lambda i=index: self._source_clicked(i))
            slot.layer_changed.connect(lambda layer, i=index: self._source_changed(i, layer))
            slot.invalid_drop.connect(self.message_requested.emit)
            self._slots.append(slot)
            root.addWidget(slot)

        root.addWidget(self._arrow())
        caption = QLabel("OPERATIONS")
        caption.setObjectName("AtOnceCanvasCaption")
        root.addWidget(caption)

        self.operations_host = QFrame(self)
        self.operations_host.setObjectName("AtOnceCanvasOperationsHost")
        self.operations_layout = QVBoxLayout(self.operations_host)
        self.operations_layout.setContentsMargins(0, 0, 0, 0)
        self.operations_layout.setSpacing(6)
        root.addWidget(self.operations_host)

        self.operation_drop = OperationDropZone(self)
        self.operation_drop.clicked.connect(self._toggle_operation_palette)
        self.operation_drop.operation_dropped.connect(self._add_operation)
        root.addWidget(self.operation_drop)

        self.operation_palette = QFrame(self)
        self.operation_palette.setObjectName("AtOnceCanvasOperationPalette")
        palette = QHBoxLayout(self.operation_palette)
        palette.setContentsMargins(6, 6, 6, 6)
        palette.setSpacing(6)
        self.filter_palette_button = OperationPaletteButton("FILTER", "filter", self.operation_palette)
        self.merge_palette_button = OperationPaletteButton("MERGE", "merge", self.operation_palette)
        self.filter_palette_button.operation_chosen.connect(self._add_operation)
        self.merge_palette_button.operation_chosen.connect(self._add_operation)
        palette.addWidget(self.filter_palette_button, 1)
        palette.addWidget(self.merge_palette_button, 1)
        root.addWidget(self.operation_palette)

        root.addWidget(self._arrow())
        self.result_block = self._block(
            "Output will appear here",
            "Plan the workflow to materialize the final result",
            "AtOnceCanvasResultPlaceholder",
        )
        self.result_block.clicked.connect(self._toggle_result_editor)
        root.addWidget(self.result_block)

        self.result_editor = self._editor_frame()
        result_layout = self.result_editor.layout()
        self.result_name_edit = QLineEdit()
        self.result_name_edit.setPlaceholderText("Final result layer name")
        self.result_state_label = QLabel("No materialized result yet.")
        self.result_state_label.setObjectName("AtOnceCanvasMuted")
        self.result_state_label.setWordWrap(True)
        result_layout.addWidget(self.result_name_edit)
        result_layout.addWidget(self.result_state_label)
        result_actions = QHBoxLayout()
        self.result_open = QPushButton("Select result layer")
        self.result_open.clicked.connect(self.result_requested.emit)
        self.result_done = QPushButton("Done")
        self.result_done.clicked.connect(self._result_done)
        result_actions.addWidget(self.result_open)
        result_actions.addStretch(1)
        result_actions.addWidget(self.result_done)
        result_layout.addLayout(result_actions)
        root.addWidget(self.result_editor)

        root.addWidget(self._arrow())
        caption = QLabel("OUTPUTS")
        caption.setObjectName("AtOnceCanvasCaption")
        root.addWidget(caption)
        self.deliveries_host = QFrame(self)
        self.deliveries_layout = QVBoxLayout(self.deliveries_host)
        self.deliveries_layout.setContentsMargins(0, 0, 0, 0)
        self.deliveries_layout.setSpacing(5)
        root.addWidget(self.deliveries_host)

        self.output_editor = self._editor_frame()
        output_layout = self.output_editor.layout()
        self.output_format = QComboBox()
        for title, format_name, _suffix in self._FORMATS:
            self.output_format.addItem(title, format_name)
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("Output name")
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("Choose / enter output path…")
        self.output_default = QCheckBox("Include by default in > Plan")
        self.output_default.setChecked(True)
        output_layout.addWidget(self.output_format)
        output_layout.addWidget(self.output_name)
        output_layout.addWidget(self.output_path)
        output_layout.addWidget(self.output_default)
        output_actions = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.output_editor.hide)
        done = QPushButton("Done")
        done.clicked.connect(self._apply_output_settings)
        output_actions.addWidget(cancel)
        output_actions.addStretch(1)
        output_actions.addWidget(done)
        output_layout.addLayout(output_actions)
        root.addWidget(self.output_editor)

        self.add_output_button = QPushButton("+ Add output")
        self.add_output_button.setObjectName("AtOnceCanvasAddOutput")
        self.add_output_button.clicked.connect(self._add_output)
        root.addWidget(self.add_output_button)
        root.addStretch(1)

    @staticmethod
    def _block(title, body, object_name):
        block = QPushButton(f"{title}\n{body}")
        block.setObjectName(object_name)
        block.setMinimumHeight(58)
        block.setCursor(Qt.PointingHandCursor)
        block.setToolTip(body)
        return block

    @staticmethod
    def _editor_frame():
        frame = QFrame()
        frame.setObjectName("AtOnceCanvasEditor")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)
        return frame

    @staticmethod
    def _arrow():
        label = QLabel("↓")
        label.setObjectName("AtOnceCanvasArrow")
        label.setAlignment(Qt.AlignCenter)
        return label

    # ------------------------------------------------------------------
    # State / topology rendering
    # ------------------------------------------------------------------
    def _reset_canvas(self):
        self._workflow = None
        self._source_refs = [None, None]
        self._operations = []
        self._deliveries = []
        self._draft_workflow_id = str(uuid4())
        for index, slot in enumerate(self._slots):
            slot.set_editable(True)
            slot.setVisible(index == 0)
            slot.set_title("Source" if index == 0 else "Source B")
            slot.set_layer(None, emit=False)
        self.result_name_edit.clear()
        self.result_editor.hide()
        self.output_editor.hide()
        self.operation_palette.hide()
        self._render_operations()
        self._render_deliveries()
        self._set_result_state("empty")
        self._emit_readiness()

    def set_workflow(self, workflow):
        if workflow is None or workflow.effective_dependency_graph_origin() != EXPLICIT_GRAPH_ORIGIN:
            self._reset_canvas()
            return
        self._workflow = workflow
        self._draft_workflow_id = workflow.workflow_id
        self._source_refs = [None, None]
        self._operations = self._operation_records_from_graph(workflow.effective_dependency_graph())
        merge = any(item["kind"] == "merge" for item in self._operations)
        for index, slot in enumerate(self._slots):
            if index < len(workflow.source_layers):
                source = workflow.source_layers[index]
                slot.set_display(source.name, source.current_layer_id)
                slot.setVisible(index == 0 or merge)
                slot.set_editable(False)
            else:
                slot.setVisible(False)
                slot.set_editable(False)
        self._slots[0].set_title("Source A" if merge else "Source")
        self._slots[1].set_title("Source B")
        self._deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in workflow.forward_deliveries]
        final = self._operations[-1] if self._operations else None
        self.result_name_edit.setText(final["result_name"] if final else "")
        self.result_editor.hide()
        self.output_editor.hide()
        self.operation_palette.hide()
        self._render_operations()
        self._render_deliveries()
        self._set_result_state("configured")
        self._emit_readiness()

    def _operation_records_from_graph(self, graph):
        node_map = graph.node_map()
        transforms = [
            edge
            for edge in graph.edges
            if edge.to_node in node_map
            and node_map[edge.to_node].kind == NodeKind.DERIVED
            and edge.operation in {OperationKind.FILTER, OperationKind.MERGE}
        ]
        merges = [edge for edge in transforms if edge.operation == OperationKind.MERGE]
        if merges:
            target_id = merges[0].to_node
            return [{
                "kind": "merge",
                "edge_id": "",
                "result_node_id": target_id,
                "result_name": node_map[target_id].name,
                "expression": "",
                "new": False,
            }]
        filters = [edge for edge in transforms if edge.operation == OperationKind.FILTER]
        if not filters:
            return []
        starts = [
            edge
            for edge in filters
            if node_map.get(edge.from_node) is not None
            and node_map[edge.from_node].kind == NodeKind.SOURCE
        ]
        current = starts[0] if starts else filters[0]
        records = []
        visited = set()
        while current.edge_id not in visited:
            visited.add(current.edge_id)
            target = node_map[current.to_node]
            records.append({
                "kind": "filter",
                "edge_id": current.edge_id,
                "result_node_id": current.to_node,
                "result_name": target.name,
                "expression": str(current.parameters.get("expression") or ""),
                "new": False,
            })
            next_edges = [
                edge for edge in filters
                if edge.from_node == current.to_node and edge.edge_id not in visited
            ]
            if len(next_edges) != 1:
                break
            current = next_edges[0]
        return records

    # ------------------------------------------------------------------
    # Source blocks
    # ------------------------------------------------------------------
    def _source_clicked(self, index):
        if self._workflow is not None:
            self.source_relink_requested.emit()
            return
        self._slots[index].choose_loaded_layer()

    def _source_changed(self, index, layer):
        if self._workflow is not None:
            return
        if layer is None:
            self._source_refs[index] = None
        elif self.gateway is not None:
            self._source_refs[index] = self.gateway.make_source_layer_ref(layer)
        if self._has_merge() and layer is not None:
            other = self.source_layer(1 - index)
            if other is not None and self._layer_id(other) == self._layer_id(layer):
                self._slots[index].set_layer(None, emit=False)
                self._source_refs[index] = None
                self.message_requested.emit("MERGE requires two different loaded vector layers.")
                return
        self._render_operations()
        self._emit_readiness()

    def source_layer(self, index=0):
        return self._slots[index].layer if 0 <= index < len(self._slots) else None

    @staticmethod
    def _layer_id(layer):
        value = getattr(layer, "id", "")
        return str(value() if callable(value) else value or "")

    # ------------------------------------------------------------------
    # Operation blocks / palette
    # ------------------------------------------------------------------
    def _operation_can_be_added(self):
        return not self._operations or (
            len(self._operations) == 1 and self._operations[0]["kind"] == "filter"
        )

    def _toggle_operation_palette(self):
        if not self._operation_can_be_added():
            self.message_requested.emit("This release supports one MERGE or a two-stage FILTER chain.")
            return
        self.operation_palette.setVisible(not self.operation_palette.isVisible())
        self._refresh_palette_availability()

    def _refresh_palette_availability(self):
        self.filter_palette_button.setEnabled(self._operation_can_be_added())
        self.merge_palette_button.setEnabled(not self._operations)

    def _add_operation(self, operation):
        operation = str(operation or "").lower()
        if operation not in {"filter", "merge"}:
            return
        if not self._operation_can_be_added():
            self.message_requested.emit("This release supports one MERGE or a two-stage FILTER chain.")
            return
        if self._operations and operation != "filter":
            self.message_requested.emit("Only a second FILTER can follow a FILTER in this release.")
            return
        index = len(self._operations)
        self._operations.append({
            "kind": operation,
            "edge_id": f"edge:canvas:{uuid4()}" if operation == "filter" else "",
            "result_node_id": f"derived:{operation}:canvas:{uuid4()}",
            "result_name": (
                "Filtered layer"
                if index == 0 and operation == "filter"
                else "Second filtered layer"
                if operation == "filter"
                else "Merged layer"
            ),
            "expression": "",
            "new": True,
        })
        self.operation_palette.hide()
        self._slots[1].setVisible(operation == "merge")
        self._slots[0].set_title("Source A" if operation == "merge" else "Source")
        self._render_operations()
        self._set_result_state("configured")
        self._emit_readiness()

    def _render_operations(self):
        while self.operations_layout.count():
            item = self.operations_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._filter_builders = {}
        for index, operation in enumerate(self._operations):
            container = QFrame(self.operations_host)
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            if operation["kind"] == "filter":
                expression = operation.get("expression") or "Choose field / condition / value"
                summary = expression if len(expression) <= 88 else expression[:85] + "…"
                block = self._block(f"FILTER {index + 1}", summary, "AtOnceCanvasOperationBlock")
            else:
                block = self._block("MERGE", "Append two compatible source layers", "AtOnceCanvasOperationBlock")
                block.setStyleSheet("QPushButton { background:#FAF5EA; border:1px solid #D8BF8E; }")
            layout.addWidget(block)
            editor = self._editor_frame()
            editor_layout = editor.layout()
            if operation["kind"] == "filter":
                builder = GuidedFilterBuilder(
                    self._filter_context_layer(),
                    expression=operation.get("expression") or "",
                    parent=editor,
                )
                builder.expression_changed.connect(
                    lambda expression, i=index: self._filter_expression_changed(i, expression)
                )
                self._filter_builders[index] = builder
                editor_layout.addWidget(builder)
            else:
                note = QLabel(
                    "MERGE uses the existing strict geometry / CRS / schema compatibility gate."
                )
                note.setObjectName("AtOnceCanvasMuted")
                note.setWordWrap(True)
                editor_layout.addWidget(note)
            done = QPushButton("Done")
            done.setObjectName("AtOncePrimary")
            done.clicked.connect(
                lambda _checked=False, i=index, panel=editor: self._operation_done(i, panel)
            )
            editor_layout.addWidget(done, 0, Qt.AlignRight)
            editor.hide()
            block.clicked.connect(
                lambda _checked=False, panel=editor: panel.setVisible(not panel.isVisible())
            )
            layout.addWidget(editor)
            self.operations_layout.addWidget(container)
            if index < len(self._operations) - 1:
                intermediate = self._block(
                    operation["result_name"], "Intermediate result", "AtOnceCanvasResultPlaceholder"
                )
                intermediate.setEnabled(False)
                self.operations_layout.addWidget(intermediate)
                self.operations_layout.addWidget(self._arrow())
        self.operation_drop.setVisible(self._operation_can_be_added())
        self._refresh_palette_availability()

    def _filter_expression_changed(self, index, expression):
        if 0 <= index < len(self._operations):
            self._operations[index]["expression"] = str(expression or "")
            self._emit_readiness()

    def _operation_done(self, index, editor):
        operation = self._operations[index]
        if operation["kind"] == "filter":
            builder = self._filter_builders.get(index)
            if builder is None:
                return
            valid, error = builder.validate()
            if not valid:
                self.message_requested.emit(error)
                return
            operation["expression"] = builder.expression()
        editor.hide()
        self._render_operations()
        self.configuration_requested.emit()
        self._emit_readiness()

    def _has_merge(self):
        return any(item["kind"] == "merge" for item in self._operations)

    def _filter_context_layer(self):
        layer = self.source_layer(0)
        if layer is not None:
            return layer
        if self._workflow is not None and self.gateway is not None and self._workflow.source_layers:
            return self.gateway.resolve_layer(self._workflow.source_layers[0].current_layer_id)
        return None

    # ------------------------------------------------------------------
    # Result / output blocks
    # ------------------------------------------------------------------
    def final_result_node_id(self):
        return self._operations[-1]["result_node_id"] if self._operations else ""

    def _toggle_result_editor(self):
        self.result_editor.setVisible(not self.result_editor.isVisible())

    def _result_done(self):
        text = self.result_name_edit.text().strip()
        if text and self._operations:
            self._operations[-1]["result_name"] = text
        self.result_editor.hide()
        self.configuration_requested.emit()
        self._render_operations()
        self._emit_readiness()

    def set_materialized(self, materialized):
        # Compatibility with old dock callers; CompactAtOnceDockWidget follows this
        # with final-node-specific evidence.
        if materialized:
            self._set_result_state("ready")

    def set_result_evidence(self, graph_state):
        final_id = self.final_result_node_id()
        if not final_id or graph_state is None:
            self._set_result_state("configured" if final_id else "empty")
            return
        stale = set(getattr(graph_state, "stale_node_ids", ()) or ())
        refreshed = set(getattr(graph_state, "refreshed_node_ids", ()) or ())
        if final_id in stale:
            self._set_result_state("stale")
        elif final_id in refreshed:
            self._set_result_state("ready")
        else:
            self._set_result_state("configured")

    def _set_result_state(self, state):
        self._result_state = state
        name = self.result_name_edit.text().strip() or (
            self._operations[-1]["result_name"] if self._operations else "Result"
        )
        if state == "ready":
            self.result_block.setObjectName("AtOnceCanvasResultBlock")
            self.result_block.setText(f"{name}\nResult ready · click for details")
            self.result_state_label.setText("Final result is materialized in the current QGIS project.")
            self.result_open.setEnabled(True)
        elif state == "stale":
            self.result_block.setObjectName("AtOnceCanvasResultPlaceholder")
            self.result_block.setText(f"{name}\nStale by choice · > Plan when ready")
            self.result_state_label.setText(
                "The final result still exists, but the latest plan intentionally left it stale."
            )
            self.result_open.setEnabled(True)
        elif state == "configured":
            self.result_block.setObjectName("AtOnceCanvasResultPlaceholder")
            self.result_block.setText("Output will appear here\n> Plan to materialize the final result")
            self.result_state_label.setText("Configured; final result is not confirmed current.")
            self.result_open.setEnabled(False)
        else:
            self.result_block.setObjectName("AtOnceCanvasResultPlaceholder")
            self.result_block.setText("Output will appear here\nAdd an operation first")
            self.result_state_label.setText("No result configured yet.")
            self.result_open.setEnabled(False)
        self.result_block.style().unpolish(self.result_block)
        self.result_block.style().polish(self.result_block)

    def _add_output(self):
        title, format_name, suffix = self._FORMATS[0]
        self._deliveries.append(
            DeliveryRef(str(uuid4()), title, format_name, f"{format_name}{suffix}", True)
        )
        self._render_deliveries()
        self._open_delivery_editor(len(self._deliveries) - 1)

    def _render_deliveries(self):
        while self.deliveries_layout.count():
            item = self.deliveries_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, delivery in enumerate(self._deliveries):
            block = self._block(
                delivery.name or delivery.format.upper(),
                f"{delivery.format.upper()} · click to configure",
                "AtOnceCanvasOutputBlock",
            )
            block.clicked.connect(lambda _checked=False, i=index: self._open_delivery_editor(i))
            self.deliveries_layout.addWidget(block)

    def _open_delivery_editor(self, index):
        if not 0 <= index < len(self._deliveries):
            return
        delivery = self._deliveries[index]
        self._editing_delivery_index = index
        self.output_format.setCurrentIndex(max(0, self.output_format.findData(delivery.format)))
        self.output_name.setText(delivery.name)
        self.output_path.setText(delivery.path)
        self.output_default.setChecked(delivery.enabled_by_default)
        self.output_editor.show()

    def _apply_output_settings(self):
        index = self._editing_delivery_index
        if index is None or not 0 <= index < len(self._deliveries):
            return
        delivery = self._deliveries[index]
        delivery.format = str(self.output_format.currentData() or "geojson")
        delivery.name = self.output_name.text().strip() or delivery.format.upper()
        delivery.path = self.output_path.text().strip()
        delivery.enabled_by_default = self.output_default.isChecked()
        self.output_editor.hide()
        self._render_deliveries()
        self.configuration_requested.emit()
        self._emit_readiness()

    # ------------------------------------------------------------------
    # Definition / planning
    # ------------------------------------------------------------------
    def can_plan(self):
        if not self._operations:
            return False
        if self._workflow is None:
            if self.source_layer(0) is None:
                return False
            if self._has_merge() and self.source_layer(1) is None:
                return False
        for index, operation in enumerate(self._operations):
            if operation["kind"] != "filter":
                continue
            builder = self._filter_builders.get(index)
            if builder is not None:
                valid, _error = builder.validate()
                if not valid:
                    return False
            elif not str(operation.get("expression") or "").strip():
                return False
        return True

    def _emit_readiness(self):
        self.readiness_changed.emit(self.can_plan())

    def definition(self):
        if not self.can_plan() or self.gateway is None:
            return None
        deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in self._deliveries]
        if self._workflow is None:
            source_a = self._source_refs[0] or self.gateway.make_source_layer_ref(self.source_layer(0))
            first = self._operations[0]
            if first["kind"] == "merge":
                source_b = self._source_refs[1] or self.gateway.make_source_layer_ref(self.source_layer(1))
                return build_standalone_merge_workflow(
                    name="Merged workflow",
                    sources=(source_a, source_b),
                    derived_name=first["result_name"],
                    deliveries=deliveries,
                    workflow_id=self._draft_workflow_id,
                    derived_node_id=first["result_node_id"],
                )
            candidate = build_standalone_filter_workflow(
                name="Filtered workflow",
                source=source_a,
                derived_name=first["result_name"],
                expression=first["expression"],
                deliveries=deliveries,
                workflow_id=self._draft_workflow_id,
                derived_node_id=first["result_node_id"],
            )
            if len(self._operations) == 2:
                candidate = self._append_second_filter(candidate, self._operations[1], deliveries)
            return candidate

        updates = {
            item["edge_id"]: item["expression"]
            for item in self._operations
            if item["kind"] == "filter" and item["edge_id"] and not item.get("new")
        }
        candidate = apply_explicit_workflow_edits(
            self._workflow,
            filter_expressions=updates,
            deliveries=deliveries,
        )
        original_graph = self._workflow.effective_dependency_graph()
        original_filter_count = sum(
            1
            for edge in original_graph.edges
            if edge.operation == OperationKind.FILTER
            and original_graph.node_map().get(edge.to_node)
            and original_graph.node_map()[edge.to_node].kind == NodeKind.DERIVED
        )
        if len(self._operations) == 2 and original_filter_count == 1 and self._operations[1].get("new"):
            candidate = self._append_second_filter(candidate, self._operations[1], deliveries)
        candidate = self._sync_deliveries(candidate, deliveries)
        return self._rename_results(candidate)

    def _append_second_filter(self, workflow, operation, deliveries):
        definition = WorkflowDefinition.from_dict(workflow.to_dict())
        graph = definition.effective_dependency_graph()
        node_map = graph.node_map()
        derived = [node for node in graph.nodes if node.kind == NodeKind.DERIVED]
        parent = derived[-1]
        new_id = operation["result_node_id"]
        nodes = list(graph.nodes)
        if new_id not in node_map:
            nodes.append(DependencyNode(new_id, operation["result_name"], NodeKind.DERIVED))
        edges = []
        for edge in graph.edges:
            target = node_map.get(edge.to_node)
            if (
                edge.from_node == parent.node_id
                and target is not None
                and target.kind == NodeKind.DELIVERY
                and edge.operation == OperationKind.EXPORT
            ):
                edges.append(
                    DependencyEdge(
                        edge.edge_id,
                        new_id,
                        edge.to_node,
                        edge.operation,
                        edge.enabled_by_default,
                        dict(edge.parameters),
                    )
                )
            else:
                edges.append(edge)
        edge_id = operation["edge_id"] or f"edge:{parent.node_id}->{new_id}"
        edges.append(
            DependencyEdge(
                edge_id,
                parent.node_id,
                new_id,
                OperationKind.FILTER,
                parameters={"expression": operation["expression"]},
            )
        )
        operation["edge_id"] = edge_id
        definition.dependency_graph = DependencyGraph(nodes, edges)
        definition.forward_deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in deliveries]
        definition.dependency_graph_origin = EXPLICIT_GRAPH_ORIGIN
        return definition

    def _sync_deliveries(self, workflow, deliveries):
        definition = WorkflowDefinition.from_dict(workflow.to_dict())
        graph = definition.effective_dependency_graph()
        node_map = graph.node_map()
        nodes = [node for node in graph.nodes if node.kind != NodeKind.DELIVERY]
        edges = [
            edge
            for edge in graph.edges
            if node_map.get(edge.to_node) is None or node_map[edge.to_node].kind != NodeKind.DELIVERY
        ]
        final_id = self.final_result_node_id()
        for delivery in deliveries:
            node_id = f"delivery:forward:{delivery.delivery_id}"
            nodes.append(
                DependencyNode(
                    node_id,
                    delivery.name,
                    NodeKind.DELIVERY,
                    format=delivery.format,
                    metadata={
                        "delivery_id": delivery.delivery_id,
                        "path": delivery.path,
                        "format": delivery.format,
                    },
                )
            )
            edges.append(
                DependencyEdge(
                    f"edge:{final_id}->{node_id}",
                    final_id,
                    node_id,
                    OperationKind.EXPORT,
                    enabled_by_default=delivery.enabled_by_default,
                )
            )
        definition.forward_deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in deliveries]
        definition.dependency_graph = DependencyGraph(nodes, edges)
        definition.dependency_graph_origin = EXPLICIT_GRAPH_ORIGIN
        return definition

    def _rename_results(self, workflow):
        names = {item["result_node_id"]: item["result_name"] for item in self._operations}
        final_name = self.result_name_edit.text().strip()
        if final_name and self._operations:
            names[self.final_result_node_id()] = final_name
            self._operations[-1]["result_name"] = final_name
        definition = WorkflowDefinition.from_dict(workflow.to_dict())
        graph = definition.effective_dependency_graph()
        definition.dependency_graph = DependencyGraph(
            [
                DependencyNode(
                    node.node_id,
                    names.get(node.node_id, node.name),
                    node.kind,
                    node.format,
                    node.current_revision,
                    dict(node.metadata),
                )
                for node in graph.nodes
            ],
            list(graph.edges),
        )
        definition.dependency_graph_origin = EXPLICIT_GRAPH_ORIGIN
        return definition
