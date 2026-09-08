"""Material-style operation popup for the simplified AtOnce canvas.

Only presentation is specialized here. Operation values still come from the
registry and are persisted through the existing graph metadata contract.
"""

from dataclasses import replace

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..core.graph_editing import GraphEditError
from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY
from ..core.provisional_connections import (
    bind_provisional_inputs,
    provisional_port_assignment,
)
from ..models.dependency_graph import NodeKind, OperationKind
from .canvas_block_dialogs import CanvasBlockDialogMixin
from .canvas_icons import material_icon, operation_symbol
from .operation_parameter_editor import OperationParameterEditor
from .structured_filter_editor import StructuredFilterEditor
from .styles import DIALOG_STYLESHEET

MATERIAL_DIALOG_QSS = DIALOG_STYLESHEET + """
QFrame#AtOnceDivider { color: #E5E7EB; background: #E5E7EB; max-height: 1px; }
QListWidget#AtOnceMaterialFieldList,
QTableWidget#AtOnceMaterialTable {
    background: #FFFFFF;
    border: 1px solid #D8DEE3;
    border-radius: 7px;
    color: #23323A;
    selection-background-color: #EEF3F6;
    selection-color: #23323A;
}
QTableWidget#AtOnceMaterialTable QHeaderView::section {
    background: #F7F8FA;
    color: #61727C;
    border: none;
    border-bottom: 1px solid #E5E7EB;
    padding: 5px 7px;
    font-weight: 600;
}
QDialog QComboBox, QDialog QLineEdit,
QDialog QDoubleSpinBox, QDialog QSpinBox {
    min-height: 24px;
}
"""


class MaterialCanvasBlockDialogMixin(CanvasBlockDialogMixin):
    """Use a compact Material/FME-like operation editor popup."""

    def _effective_input_edges(self, node_id, definition):
        assignment = provisional_port_assignment(self._graph, node_id, definition)
        rows = []
        for edge in self._graph.incoming_edges(node_id):
            port_id = assignment.get(edge.edge_id) or str(
                edge.parameters.get("target_port") or "input"
            )
            rows.append((edge, port_id))
        return tuple(rows)

    def _connected_input_rows(self, node_id, definition):
        if definition is None:
            return ()
        node_map = self._graph.node_map()
        by_port = {}
        for edge, port_id in self._effective_input_edges(node_id, definition):
            source = node_map.get(edge.from_node)
            if source is not None:
                by_port.setdefault(port_id, []).append(source.name or source.node_id)
        rows = []
        for port in definition.input_ports:
            names = by_port.get(port.port_id, [])
            rows.append((port.label, ", ".join(names) if names else "Not connected"))
        return tuple(rows)

    def _operation_context_layers_for_definition(self, node_id, definition):
        """Resolve upstream layers using preview semantic ports before Apply."""

        if self.gateway is None or definition is None:
            return {}
        layers = {}
        for edge, port_id in self._effective_input_edges(node_id, definition):
            layer = self._context_layer_for_node(edge.from_node)
            if layer is None:
                continue
            if port_id in layers:
                current = layers[port_id]
                layers[port_id] = (
                    (*current, layer)
                    if isinstance(current, tuple)
                    else (current, layer)
                )
            else:
                layers[port_id] = layer
        return layers

    @staticmethod
    def _section(text, parent):
        label = QLabel(text, parent)
        label.setObjectName("AtOnceSectionTitle")
        return label

    def _operation_changed_from_registered(self, node_id, operation_kind, parameters):
        """Return True when a persisted operation node is edited in place."""

        workflow = getattr(self, "_workflow", None)
        if workflow is None:
            return False
        try:
            old_node = workflow.effective_dependency_graph().node_map().get(str(node_id))
        except Exception:
            return False
        if old_node is None or old_node.kind != NodeKind.DERIVED:
            return False
        old_kind = str((old_node.metadata or {}).get("operation_kind") or "")
        old_parameters = dict((old_node.metadata or {}).get("parameters") or {})
        return old_kind != str(operation_kind or "") or old_parameters != dict(parameters or {})

    def _mark_operation_branch_stale(self, node_id):
        """Mark a changed operation and every downstream node as stale in-canvas."""

        queue = [str(node_id)]
        stale = set()
        while queue:
            current = queue.pop(0)
            if current in stale:
                continue
            stale.add(current)
            for edge in self._graph.downstream_edges(current):
                queue.append(str(edge.to_node))
        self._stale_node_ids.update(stale)

    def _show_operation_dialog(self, node):
        dialog = QDialog(self)
        dialog.setWindowTitle("Operation")
        dialog.setStyleSheet(MATERIAL_DIALOG_QSS)
        dialog.setMinimumWidth(520)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("Configure operation", dialog)
        heading.setObjectName("AtOnceDialogTitle")
        layout.addWidget(heading)

        function_form = QFormLayout()
        function_form.setHorizontalSpacing(16)
        function_form.setVerticalSpacing(8)
        function_combo = QComboBox(dialog)
        function_combo.addItem("Select function…", "")
        data_type = self._upstream_data_type(node.node_id)
        definitions = self._operations_for_data_type(data_type)
        for definition in definitions:
            function_combo.addItem(
                material_icon(operation_symbol(definition.kind.value)),
                f"{definition.category} — {definition.title}",
                definition.kind.value,
            )
        current_kind = str(node.metadata.get("operation_kind") or "")
        current_index = function_combo.findData(current_kind)
        function_combo.setCurrentIndex(current_index if current_index >= 0 else 0)
        function_form.addRow("Function", function_combo)
        layout.addLayout(function_form)

        divider = QFrame(dialog)
        divider.setFrameShape(QFrame.HLine)
        divider.setObjectName("AtOnceDivider")
        layout.addWidget(divider)

        inputs_title = self._section("Connected inputs", dialog)
        layout.addWidget(inputs_title)
        inputs_form = QFormLayout()
        inputs_form.setHorizontalSpacing(16)
        inputs_form.setVerticalSpacing(5)
        layout.addLayout(inputs_form)

        parameters_title = self._section("Parameters", dialog)
        layout.addWidget(parameters_title)

        filter_editor = StructuredFilterEditor(dialog)
        generic_editor = OperationParameterEditor(dialog)
        filter_editor.setVisible(False)
        generic_editor.setVisible(False)
        layout.addWidget(filter_editor)
        layout.addWidget(generic_editor)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        generic_values = {"value": {}}
        generic_editor.parameters_changed.connect(
            lambda parameters: generic_values.__setitem__("value", dict(parameters or {}))
        )

        def clear_input_rows():
            while inputs_form.rowCount():
                inputs_form.removeRow(0)

        def configure_editor():
            kind = str(function_combo.currentData() or "")
            definition = DEFAULT_OPERATION_REGISTRY.get(kind)
            parameters = (
                node.metadata.get("parameters") or {} if kind == current_kind else {}
            )
            clear_input_rows()
            if definition is not None:
                for label, value in self._connected_input_rows(node.node_id, definition):
                    value_label = QLabel(value, dialog)
                    value_label.setObjectName("AtOnceMuted")
                    value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    inputs_form.addRow(label, value_label)

            is_filter = bool(definition and definition.kind == OperationKind.FILTER)
            filter_editor.setVisible(is_filter)
            generic_editor.setVisible(bool(definition) and not is_filter)
            parameters_title.setVisible(bool(definition))
            inputs_title.setVisible(bool(definition))
            dialog.setWindowIcon(material_icon(operation_symbol(kind)))

            if is_filter:
                filter_editor.set_layer(self._filter_context_layer(node.node_id))
                filter_editor.set_expression(str(parameters.get("expression") or ""))
            elif definition is not None:
                generic_values["value"] = definition.parameter_defaults()
                generic_values["value"].update(dict(parameters or {}))
                generic_editor.set_definition(
                    definition,
                    parameters,
                    self._operation_context_layers_for_definition(
                        node.node_id,
                        definition,
                    ),
                )
            dialog.adjustSize()

        def apply_operation():
            kind = str(function_combo.currentData() or "")
            definition = DEFAULT_OPERATION_REGISTRY.get(kind)
            if definition is None:
                self.message_requested.emit("Select a function before applying this operation.")
                return
            if definition.kind == OperationKind.FILTER:
                expression = filter_editor.expression()
                if not expression:
                    self.message_requested.emit(
                        "Choose a FILTER field/condition or enter an advanced expression."
                    )
                    return
                parameters = {"expression": expression}
            else:
                generic_editor._emit_parameters()
                parameters = dict(generic_values["value"])

            changed_from_registered = self._operation_changed_from_registered(
                node.node_id,
                definition.kind.value,
                parameters,
            )
            metadata = dict(node.metadata)
            metadata.update(
                {
                    "workflow_role": "operation",
                    "operation_kind": definition.kind.value,
                    "operation_version": definition.version,
                    "parameters": parameters,
                }
            )
            updated_node = replace(node, name=definition.title, metadata=metadata)
            try:
                self._graph = bind_provisional_inputs(
                    self._graph,
                    node.node_id,
                    definition.kind,
                )
            except GraphEditError as exc:
                self.message_requested.emit(str(exc))
                return
            if changed_from_registered:
                self._mark_operation_branch_stale(node.node_id)
            self._replace_node(updated_node)
            if changed_from_registered:
                self.message_requested.emit(
                    "This registered task has changed. Its existing result is now stale "
                    "and will not be overwritten. Create or duplicate a new operation "
                    "task and output for the new condition."
                )
            self.graph_changed.emit()
            dialog.accept()

        function_combo.currentIndexChanged.connect(configure_editor)
        apply.clicked.connect(apply_operation)
        configure_editor()
        dialog.exec_()