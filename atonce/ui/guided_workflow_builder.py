"""Bounded visual authoring surfaces for G10a.

These widgets only collect configuration.  They never register workflows,
write source layers, build candidates, export files, or create run evidence.
"""

from uuid import uuid4

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsExpression, QgsMimeDataUtils

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
from ..models.workflow import DeliveryRef, EXPLICIT_GRAPH_ORIGIN
from .styles import DIALOG_STYLESHEET


try:
    from qgis.gui import QgsFieldComboBox, QgsFieldExpressionWidget
except ImportError:  # pragma: no cover - only used by non-QGIS static tooling
    QgsFieldComboBox = None
    QgsFieldExpressionWidget = None

from ..core.filter_builder import (
    FILTER_OPERATORS,
    build_filter_expression,
    normalize_field_type,
    parse_simple_filter_expression,
)
from qgis.PyQt.QtWidgets import QComboBox, QLineEdit, QFormLayout


def _layer_name(layer):
    value = getattr(layer, "name", "")
    return str(value() if callable(value) else value or "Unnamed layer")


def _layer_id(layer):
    value = getattr(layer, "id", "")
    return str(value() if callable(value) else value or "")


class SourceDropSlot(QFrame):
    """One current-project vector drop target with a click fallback."""

    layer_changed = pyqtSignal(object)
    invalid_drop = pyqtSignal(str)
    clicked = pyqtSignal()

    URI_MIME = "application/x-vnd.qgis.qgis.uri"
    LAYER_TREE_MIME = "application/qgis.layertreemodeldata"

    def __init__(self, gateway, title="Source", parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self._layer = None
        self.setObjectName("AtOnceSourceDropSlot")
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        heading = QLabel(title.upper())
        heading.setObjectName("AtOnceBuilderRole")
        self.layer_label = QLabel("Drop a QGIS vector layer here\nor click to choose")
        self.layer_label.setObjectName("AtOnceBuilderSlotText")
        self.layer_label.setWordWrap(True)
        self.choose_button = QPushButton("Choose loaded layer…")
        self.choose_button.setObjectName("AtOnceBuilderChoose")
        self.choose_button.clicked.connect(self._choose_layer)
        layout.addWidget(heading)
        layout.addWidget(self.layer_label)
        layout.addWidget(self.choose_button)
        self.heading = heading

    @property
    def layer(self):
        return self._layer

    def set_layer(self, layer, emit=True):
        self._layer = layer
        if layer is None:
            self.layer_label.setText("Drop a QGIS vector layer here\nor click to choose")
        else:
            self.layer_label.setText(_layer_name(layer))
        if emit:
            self.layer_changed.emit(layer)

    def set_display(self, name, detail=""):
        """Render a persisted source without treating it as a mutable QGIS layer."""

        self._layer = None
        self.layer_label.setText(str(name or "Source"))
        self.layer_label.setToolTip(str(detail or ""))

    def set_title(self, title):
        self.heading.setText(str(title).upper())

    def set_editable(self, editable):
        self.setAcceptDrops(bool(editable))
        self.choose_button.setEnabled(bool(editable))
        self.setProperty("readOnly", not editable)
        self.style().unpolish(self)
        self.style().polish(self)

    def _choose_layer(self):
        if self.gateway is None:
            return
        if hasattr(self.gateway, "source_candidate_layers"):
            layers = list(self.gateway.source_candidate_layers())
        else:
            layers = list(self.gateway.vector_layers())
        if not layers:
            self.invalid_drop.emit(
                "No loaded vector or raster layers are available in this QGIS project."
            )
            return
        names = []
        for layer in layers:
            label = _layer_name(layer)
            try:
                kind = self.gateway.layer_data_type(layer)
            except Exception:
                kind = (
                    "raster"
                    if getattr(self.gateway, "is_raster_layer", lambda _layer: False)(layer)
                    else "vector"
                )
            names.append(f"{label} ({kind})")
        value, accepted = QInputDialog.getItem(
            self,
            "Choose source layer",
            "Loaded layer",
            names,
            0,
            False,
        )
        if accepted:
            self.set_layer(layers[names.index(value)])

    def choose_loaded_layer(self):
        """Expose the click fallback to the non-modal canvas."""

        self._choose_layer()

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def _decode_layer(self, mime_data):
        if mime_data is None:
            return None, "No QGIS layer data was provided."
        try:
            uris = QgsMimeDataUtils.decodeUriList(mime_data)
        except Exception as exc:
            return None, f"Could not read the QGIS layer drag data: {exc}"
        for uri in uris:
            layer_id = getattr(uri, "layerId", "")
            if callable(layer_id):
                layer_id = layer_id()
            layer = self.gateway.resolve_layer(layer_id) if self.gateway else None
            if layer is None:
                continue
            if not self.gateway.is_vector_layer(layer) and not self.gateway.is_raster_layer(
                layer
            ):
                return None, "Only loaded vector or raster layers can be used in an AtOnce workflow."
            return layer, ""
        return None, "Drop a vector or raster layer from the current QGIS Layers panel."

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        mime = event.mimeData()
        if mime.hasFormat(self.URI_MIME) or mime.hasFormat(self.LAYER_TREE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        layer, error = self._decode_layer(event.mimeData())
        if layer is None:
            self.invalid_drop.emit(error)
            event.ignore()
            return
        self.set_layer(layer)
        event.acceptProposedAction()


class GuidedFilterBuilder(QWidget):
    """Simple field/operator/value FILTER builder with advanced QGIS mode."""

    expression_changed = pyqtSignal(str)

    def __init__(self, layer=None, expression="", parent=None):
        super().__init__(parent)
        self.layer = None
        self._advanced = False
        self.setObjectName("AtOnceFilterBuilder")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        simple = QFormLayout()
        self.field_combo = self._field_widget()
        self.field_combo.setObjectName("AtOnceFilterField")
        self.field_combo.setToolTip("Choose a field…")
        self.operator_combo = QComboBox()
        self.operator_combo.setObjectName("AtOnceFilterOperator")
        for key, label in FILTER_OPERATORS:
            self.operator_combo.addItem(label, key)
        self.value_edit = QLineEdit()
        self.value_edit.setObjectName("AtOnceFilterValue")
        self.value_edit.setPlaceholderText("Enter a value…")
        simple.addRow("Field", self.field_combo)
        simple.addRow("Condition", self.operator_combo)
        simple.addRow("Value", self.value_edit)
        layout.addLayout(simple)

        self.advanced_button = QPushButton("Advanced expression…")
        self.advanced_button.setObjectName("AtOnceBuilderSecondary")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button, 0, Qt.AlignLeft)

        self.advanced_widget = None
        self.error_label = QLabel()
        self.error_label.setObjectName("AtOnceBuilderError")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        if hasattr(self.field_combo, "fieldChanged"):
            self.field_combo.fieldChanged.connect(lambda _value: self._changed())
        else:
            self.field_combo.currentIndexChanged.connect(lambda _index: self._changed())
        self.operator_combo.currentIndexChanged.connect(lambda _index: self._changed())
        self.value_edit.textChanged.connect(lambda _value: self._changed())
        self.set_layer(layer)
        if expression:
            self.set_expression(expression)

    def _field_widget(self):
        if QgsFieldComboBox is not None:
            return QgsFieldComboBox()
        return QComboBox()

    def set_layer(self, layer):
        self.layer = layer
        if hasattr(self.field_combo, "setLayer"):
            self.field_combo.setLayer(layer)
        else:
            self.field_combo.blockSignals(True)
            self.field_combo.clear()
            if layer is not None and hasattr(layer, "fields"):
                for field in layer.fields():
                    self.field_combo.addItem(field.name(), field.name())
            self.field_combo.blockSignals(False)
        self._changed()

    def _field_name(self):
        if hasattr(self.field_combo, "currentField"):
            return str(self.field_combo.currentField() or "")
        return str(self.field_combo.currentData() or self.field_combo.currentText() or "")

    def _field_type(self):
        if self.layer is None or not hasattr(self.layer, "fields"):
            return "string"
        fields = self.layer.fields()
        field_name = self._field_name()
        if not field_name:
            return "string"
        try:
            field = fields.field(field_name) if hasattr(fields, "field") else None
        except (KeyError, IndexError):
            field = None
        if field is None:
            return "string"
        type_name = field.typeName() if hasattr(field, "typeName") else "string"
        return normalize_field_type(type_name)

    def _toggle_advanced(self, enabled):
        self._advanced = bool(enabled)
        if self._advanced and self.advanced_widget is None:
            self.advanced_widget = self._make_advanced_widget()
            self.layout().insertWidget(2, self.advanced_widget)
        if self.advanced_widget is not None:
            self.advanced_widget.setVisible(self._advanced)
        self._changed()

    def _make_advanced_widget(self):
        if QgsFieldExpressionWidget is not None:
            widget = QgsFieldExpressionWidget(self)
            if self.layer is not None and hasattr(widget, "setLayer"):
                widget.setLayer(self.layer)
            return widget
        widget = QLineEdit(self)
        widget.setPlaceholderText("Enter a QGIS expression")
        return widget

    def _advanced_expression(self):
        if self.advanced_widget is None:
            return ""
        if hasattr(self.advanced_widget, "expression"):
            value = self.advanced_widget.expression()
        else:
            value = self.advanced_widget.text()
        return str(value or "").strip()

    def _set_advanced_expression(self, expression):
        if self.advanced_widget is None:
            self._toggle_advanced(True)
        if hasattr(self.advanced_widget, "setExpression"):
            self.advanced_widget.setExpression(str(expression or ""))
        else:
            self.advanced_widget.setText(str(expression or ""))

    def set_expression(self, expression):
        """Decompose generated expressions and preserve everything else advanced."""

        parsed = parse_simple_filter_expression(expression)
        if parsed is not None and self.layer is not None:
            field, operator, value, _field_type = parsed
            if hasattr(self.field_combo, "setField"):
                self.field_combo.setField(field)
            else:
                index = self.field_combo.findData(field)
                if index >= 0:
                    self.field_combo.setCurrentIndex(index)
            if self._field_name() != field:
                self._set_advanced_expression(str(expression or "").strip())
                self.advanced_button.setChecked(True)
                return
            index = self.operator_combo.findData(operator)
            if index >= 0:
                self.operator_combo.setCurrentIndex(index)
            self.value_edit.setText(value)
            self.advanced_button.setChecked(False)
            return
        self._set_advanced_expression(str(expression or "").strip())
        self.advanced_button.setChecked(True)

    def expression(self):
        if self._advanced:
            return self._advanced_expression()
        field = self._field_name()
        operator = self.operator_combo.currentData()
        try:
            return build_filter_expression(field, operator, self.value_edit.text(), self._field_type())
        except ValueError:
            return ""

    def validate(self):
        if self._advanced:
            expression = self._advanced_expression()
            if not expression:
                return False, "Enter a QGIS expression."
            parsed = QgsExpression(expression)
            if parsed.hasParserError():
                return False, parsed.parserErrorString()
            return True, ""
        field_name = self._field_name()
        if not field_name:
            return False, "Choose a field."
        try:
            build_filter_expression(
                field_name,
                self.operator_combo.currentData(),
                self.value_edit.text(),
                self._field_type(),
            )
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def _changed(self):
        valid, error = self.validate()
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        if valid:
            self.expression_changed.emit(self.expression())


class GuidedWorkflowBuilder(QWidget):
    """Fixed visual pipeline for one bounded FILTER or MERGE workflow."""

    source_changed = pyqtSignal(int, object)
    message_requested = pyqtSignal(str)
    configure_requested = pyqtSignal()
    source_details_requested = pyqtSignal(int)

    def __init__(self, gateway=None, operation="filter", editable=True, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self.operation = str(operation or "").lower()
        self.editable = bool(editable)
        self._workflow = None
        self._slots = []
        self.setObjectName("AtOnceGuidedWorkflowBuilder")
        self._build_once()
        self._apply_state(self.operation)

    def _build_once(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        source_column = QVBoxLayout()
        source_column.setSpacing(6)
        self._slots = []
        for index in range(2):
            slot = SourceDropSlot(
                self.gateway,
                "Source A" if index == 0 else "Source B",
                self,
            )
            slot.set_editable(self.editable)
            slot.layer_changed.connect(lambda layer, i=index: self._source_changed(i, layer))
            slot.invalid_drop.connect(self.message_requested.emit)
            slot.mousePressEvent = lambda event, i=index: self.source_details_requested.emit(i)
            self._slots.append(slot)
            source_column.addWidget(slot)
        self._empty_state = QLabel("Choose Filter or Merge to begin a guided workflow.")
        self._empty_state.setObjectName("AtOnceBuilderEmpty")
        self._empty_state.setWordWrap(True)
        source_column.addWidget(self._empty_state)
        source_column.addStretch(1)
        layout.addLayout(source_column, 2)

        self._arrow_one = self._arrow()
        layout.addWidget(self._arrow_one)
        self.transform_block = self._block(
            "Transform",
            "Choose a workflow type",
            "AtOnceBuilderTransform",
        )
        self.transform_block.clicked.connect(self.configure_requested.emit)
        layout.addWidget(self.transform_block, 2)

        self._arrow_two = self._arrow()
        layout.addWidget(self._arrow_two)
        self.result_block = self._block("Result", "Derived layer", "AtOnceBuilderResult")
        layout.addWidget(self.result_block, 2)

        self._arrow_three = self._arrow()
        layout.addWidget(self._arrow_three)
        self.delivery_block = self._block("Deliveries", "None configured", "AtOnceBuilderDelivery")
        layout.addWidget(self.delivery_block, 2)

    @staticmethod
    def _arrow():
        label = QLabel("→")
        label.setObjectName("AtOnceBuilderArrow")
        label.setAlignment(Qt.AlignCenter)
        return label

    @staticmethod
    def _block(title, body, object_name):
        block = QPushButton()
        block.setObjectName(object_name)
        block.setText(f"{title}\n{body}")
        block.setToolTip(body)
        block.setMinimumHeight(76)
        block.setCursor(Qt.PointingHandCursor)
        return block

    def _source_changed(self, index, layer):
        if self.operation == "merge" and layer is not None:
            other = self.source_layer(1 - index)
            if other is not None and _layer_id(other) == _layer_id(layer):
                self._slots[index].set_layer(None, emit=False)
                self.message_requested.emit("MERGE requires two different loaded vector layers.")
                return
        self.source_changed.emit(index, layer)

    def source_layer(self, index=0):
        return self._slots[index].layer if 0 <= index < len(self._slots) else None

    def set_source_layer(self, index, layer):
        if 0 <= index < len(self._slots):
            self._slots[index].set_layer(layer)

    @staticmethod
    def _normalized_operation(operation):
        value = str(operation or "").lower()
        return value if value in {"filter", "merge"} else ""

    def _apply_state(self, operation):
        """Update the persistent widget tree without replacing any layout."""

        self.operation = self._normalized_operation(operation)
        has_operation = bool(self.operation)
        merge = self.operation == "merge"
        slot_count = 2 if merge else 1 if self.operation == "filter" else 0

        for index, slot in enumerate(self._slots):
            slot.setVisible(index < slot_count)
            slot.set_title(
                "Source A" if merge and index == 0 else "Source B" if merge else "Source"
            )
            slot.set_editable(self.editable and self._workflow is None)
            if self._workflow is None:
                slot.set_layer(None, emit=False)

        self._empty_state.setVisible(not has_operation)
        for arrow in (self._arrow_one, self._arrow_two, self._arrow_three):
            arrow.setVisible(has_operation)

        if self.operation == "filter":
            title, body = "FILTER", "Configure filter"
        elif self.operation == "merge":
            title, body = "MERGE / append", "Append compatible features"
        else:
            title, body = "Transform", "Choose a workflow type"
        self.transform_block.setText(f"{title}\n{body}")
        self.transform_block.setToolTip(body)
        self.transform_block.setEnabled(self.editable or self._workflow is not None)
        self.result_block.setText("Result\nDerived layer" if has_operation else "Result\nChoose a workflow")
        self.delivery_block.setText("Deliveries\nNone configured")

    def set_workflow(self, workflow):
        self._workflow = workflow
        if workflow is None:
            self._apply_state("")
            return
        graph = workflow.effective_dependency_graph()
        derived = [node for node in graph.nodes if node.kind == NodeKind.DERIVED]
        target_id = derived[-1].node_id if derived else ""
        incoming = [edge for edge in graph.edges if edge.to_node == target_id]
        operation = "merge" if any(edge.operation == OperationKind.MERGE for edge in incoming) else "filter"
        self._apply_state(operation)
        sources = workflow.source_layers
        for index, slot in enumerate(self._slots):
            if index < len(sources):
                source = sources[index]
                slot.set_display(source.name, source.current_layer_id)
            else:
                slot.set_layer(None, emit=False)
        if derived:
            self.result_block.setText(f"Result\n{derived[-1].name}")
        if workflow.forward_deliveries:
            labels = [delivery.name or delivery.format for delivery in workflow.forward_deliveries]
            self.delivery_block.setText("Deliveries\n" + ", ".join(labels))
        else:
            self.delivery_block.setText("Deliveries\nNone configured")
        self.transform_block.setEnabled(True)


class GuidedWorkflowCanvas(QWidget):
    """Compact, non-modal block-first authoring surface for the AtOnce dock.

    The canvas owns configuration state only.  It emits ``configuration_requested``
    and ``plan_requested`` so the plugin can persist or execute through the
    existing workflow and graph-execution services.
    """

    configuration_requested = pyqtSignal()
    plan_requested = pyqtSignal()
    readiness_changed = pyqtSignal(bool)
    message_requested = pyqtSignal(str)
    source_changed = pyqtSignal(int, object)
    result_requested = pyqtSignal()

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
        self._operation = ""
        self._filter_edge_id = ""
        self._result_node_id = ""
        self._result_materialized = False
        self._source_refs = [None, None]
        self._deliveries = []
        self._draft_workflow_id = str(uuid4())
        self._draft_derived_node_id = f"derived:canvas:{uuid4()}"
        self._editing_delivery_index = None
        self.setObjectName("AtOnceWorkflowCanvas")
        self._build_once()
        self._reset_canvas()

    def _build_once(self):
        """Create the canvas widget tree once; transitions only update state."""

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        self.canvas_hint = QLabel(
            "Drop loaded vector layers into Source, choose an operation, then Plan."
        )
        self.canvas_hint.setObjectName("AtOnceCanvasHint")
        self.canvas_hint.setWordWrap(True)
        root.addWidget(self.canvas_hint)

        source_title = QLabel("SOURCE")
        source_title.setObjectName("AtOnceCanvasCaption")
        root.addWidget(source_title)
        source_column = QVBoxLayout()
        source_column.setSpacing(6)
        self._slots = []
        for index, title in enumerate(("Source", "Source B")):
            slot = SourceDropSlot(self.gateway, title, self)
            slot.clicked.connect(lambda i=index: self._source_clicked(i))
            slot.layer_changed.connect(lambda layer, i=index: self._source_changed(i, layer))
            slot.invalid_drop.connect(self.message_requested.emit)
            self._slots.append(slot)
            source_column.addWidget(slot)
        root.addLayout(source_column)
        self._source_column = source_column

        root.addWidget(self._vertical_arrow())
        self.operation_block = self._canvas_block(
            "Choose an operation",
            "Click to configure FILTER or MERGE",
            "AtOnceCanvasOperationBlock",
        )
        self.operation_block.clicked.connect(self._toggle_operation_editor)
        root.addWidget(self.operation_block)

        self.operation_editor = self._editor_frame()
        operation_layout = self.operation_editor.layout()
        self.operation_choice = QComboBox()
        self.operation_choice.setObjectName("AtOnceCanvasOperationChoice")
        self.operation_choice.addItem("Choose an operation…", "")
        self.operation_choice.addItem("Filter", "filter")
        self.operation_choice.addItem("Merge / append", "merge")
        self.operation_choice.currentIndexChanged.connect(self._operation_changed)
        operation_layout.addWidget(self.operation_choice)

        self.filter_builder = GuidedFilterBuilder(parent=self.operation_editor)
        self.filter_builder.expression_changed.connect(lambda _expression: self._emit_readiness())
        operation_layout.addWidget(self.filter_builder)
        self.merge_note = QLabel(
            "MERGE appends all features from two compatible vector sources."
        )
        self.merge_note.setObjectName("AtOnceCanvasMuted")
        self.merge_note.setWordWrap(True)
        operation_layout.addWidget(self.merge_note)
        self.operation_apply = QPushButton("Apply")
        self.operation_apply.setObjectName("AtOncePrimary")
        self.operation_apply.clicked.connect(self.plan_requested.emit)
        operation_layout.addWidget(self.operation_apply, 0, Qt.AlignRight)
        root.addWidget(self.operation_editor)

        root.addWidget(self._vertical_arrow())
        self.result_block = self._canvas_block(
            "Output will appear here",
            "Apply an operation to create the result",
            "AtOnceCanvasResultPlaceholder",
        )
        self.result_block.clicked.connect(self._toggle_result_editor)
        root.addWidget(self.result_block)

        self.result_editor = self._editor_frame()
        result_layout = self.result_editor.layout()
        self.result_name_edit = QLineEdit()
        self.result_name_edit.setPlaceholderText("Result layer name")
        result_layout.addWidget(self.result_name_edit)
        self.result_state = QLabel("The result is created when you Plan.")
        self.result_state.setObjectName("AtOnceCanvasMuted")
        self.result_state.setWordWrap(True)
        result_layout.addWidget(self.result_state)
        self.result_open = QPushButton("Select result layer")
        self.result_open.clicked.connect(self.result_requested.emit)
        result_layout.addWidget(self.result_open, 0, Qt.AlignLeft)
        self.result_apply = QPushButton("Apply result settings")
        self.result_apply.clicked.connect(self._apply_result_settings)
        result_layout.addWidget(self.result_apply, 0, Qt.AlignRight)
        root.addWidget(self.result_editor)

        root.addWidget(self._vertical_arrow())
        deliveries_title = QLabel("OUTPUTS")
        deliveries_title.setObjectName("AtOnceCanvasCaption")
        root.addWidget(deliveries_title)
        self.deliveries_layout = QVBoxLayout()
        self.deliveries_layout.setSpacing(5)
        root.addLayout(self.deliveries_layout)

        self.output_editor = self._editor_frame()
        output_layout = self.output_editor.layout()
        self.output_format = QComboBox()
        for title, format_name, _suffix in self._FORMATS:
            self.output_format.addItem(title, format_name)
        output_layout.addWidget(self.output_format)
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("Output name")
        output_layout.addWidget(self.output_name)
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("Output path")
        output_layout.addWidget(self.output_path)
        self.output_default = QCheckBox("Include by default when planning")
        self.output_default.setChecked(True)
        output_layout.addWidget(self.output_default)
        output_actions = QHBoxLayout()
        output_apply = QPushButton("Apply output")
        output_apply.clicked.connect(self._apply_output_settings)
        output_cancel = QPushButton("Cancel")
        output_cancel.clicked.connect(self.output_editor.hide)
        output_actions.addWidget(output_apply)
        output_actions.addWidget(output_cancel)
        output_layout.addLayout(output_actions)
        root.addWidget(self.output_editor)

        self._add_output_button = QPushButton("+ Add output")
        self._add_output_button.setObjectName("AtOnceCanvasAddOutput")
        self._add_output_button.clicked.connect(self._add_output)
        root.addWidget(self._add_output_button)
        root.addStretch(1)

    @staticmethod
    def _vertical_arrow():
        arrow = QLabel("↓")
        arrow.setObjectName("AtOnceCanvasArrow")
        arrow.setAlignment(Qt.AlignCenter)
        return arrow

    @staticmethod
    def _canvas_block(title, body, object_name):
        block = QPushButton(f"{title}\n{body}")
        block.setObjectName(object_name)
        block.setMinimumHeight(60)
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

    def _reset_canvas(self):
        self._workflow = None
        self._operation = ""
        self._filter_edge_id = ""
        self._result_node_id = ""
        self._result_materialized = False
        self._source_refs = [None, None]
        self._deliveries = []
        self._draft_workflow_id = str(uuid4())
        self._draft_derived_node_id = f"derived:canvas:{uuid4()}"
        for index, slot in enumerate(self._slots):
            slot.set_editable(True)
            slot.setVisible(index == 0)
            slot.set_title("Source" if index == 0 else "Source B")
            slot.set_layer(None, emit=False)
        self._set_operation("")
        self.result_name_edit.setText("")
        self.result_state.setText("The result is created when you Plan.")
        self.result_open.setEnabled(False)
        self.result_editor.hide()
        self.output_editor.hide()
        self._render_deliveries()
        self._emit_readiness()

    def set_workflow(self, workflow):
        """Display an explicit workflow without mutating QGIS or executing it."""

        if workflow is None or workflow.effective_dependency_graph_origin() != EXPLICIT_GRAPH_ORIGIN:
            self._reset_canvas()
            return

        self._workflow = workflow
        self._draft_workflow_id = workflow.workflow_id
        self._result_materialized = False
        graph = workflow.effective_dependency_graph()
        derived = [node for node in graph.nodes if node.kind == NodeKind.DERIVED]
        target = derived[-1] if derived else None
        self._result_node_id = target.node_id if target else ""
        incoming = [edge for edge in graph.edges if edge.to_node == self._result_node_id]
        operation = "merge" if any(edge.operation == OperationKind.MERGE for edge in incoming) else "filter"
        filter_edges = [edge for edge in incoming if edge.operation == OperationKind.FILTER]
        self._filter_edge_id = filter_edges[-1].edge_id if filter_edges else ""
        self._draft_derived_node_id = self._result_node_id
        for index, slot in enumerate(self._slots):
            slot.set_editable(False)
            if index < len(workflow.source_layers):
                source = workflow.source_layers[index]
                slot.set_display(source.name, source.current_layer_id)
                slot.setVisible(index < (2 if operation == "merge" else 1))
            else:
                slot.set_layer(None, emit=False)
                slot.setVisible(False)
        self._source_refs = [None, None]
        self._deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in workflow.forward_deliveries]
        self._set_operation(operation)
        if self._filter_edge_id:
            edge = next(edge for edge in graph.edges if edge.edge_id == self._filter_edge_id)
            self.filter_builder.set_layer(self._filter_context_layer())
            self.filter_builder.set_expression(str(edge.parameters.get("expression") or ""))
        if target is not None:
            self.result_name_edit.setText(target.name)
            self.result_block.setText("Output will appear here\nPlan to create the result")
            self.result_state.setText("The result is not materialized yet. Plan to create it.")
            self.result_open.setEnabled(False)
        self._render_deliveries()
        self.output_editor.hide()
        self.operation_editor.hide()
        self.result_editor.hide()
        self._emit_readiness()

    def set_materialized(self, materialized):
        self._result_materialized = bool(materialized)
        if self._workflow is None:
            return
        graph = self._workflow.effective_dependency_graph()
        target = next((node for node in graph.nodes if node.node_id == self._result_node_id), None)
        if target is None:
            return
        if self._result_materialized:
            self.result_block.setObjectName("AtOnceCanvasResultBlock")
            self.result_block.setText(f"{target.name}\nResult ready · click for details")
            self.result_state.setText("Result is materialized in the current QGIS project.")
            self.result_open.setEnabled(True)
        else:
            self.result_block.setObjectName("AtOnceCanvasResultPlaceholder")
            self.result_block.setText("Output will appear here\nPlan to create the result")
            self.result_state.setText("The result is not materialized yet. Plan to create it.")
            self.result_open.setEnabled(False)
        self.result_block.style().unpolish(self.result_block)
        self.result_block.style().polish(self.result_block)

    def can_plan(self):
        if self._workflow is not None:
            return bool(self._operation)
        if self._operation not in {"filter", "merge"}:
            return False
        if self.source_layer(0) is None:
            return False
        if self._operation == "merge" and self.source_layer(1) is None:
            return False
        valid, _error = self.filter_builder.validate()
        return valid if self._operation == "filter" else True

    def source_layer(self, index=0):
        return self._slots[index].layer if 0 <= index < len(self._slots) else None

    def _source_clicked(self, index):
        if self._workflow is None and self._slots[index].isEnabled():
            self._slots[index].choose_loaded_layer()
        elif self._workflow is not None:
            self.message_requested.emit(
                "Source bindings are fixed for this workflow; use Relink Source to change one safely."
            )

    def _source_changed(self, index, layer):
        if self._operation == "merge" and layer is not None:
            other = self.source_layer(1 - index)
            if other is not None and _layer_id(other) == _layer_id(layer):
                self._slots[index].set_layer(None, emit=False)
                self.message_requested.emit("MERGE requires two different loaded vector layers.")
                return
        if self._workflow is None and layer is not None and self.gateway is not None:
            self._source_refs[index] = self.gateway.make_source_layer_ref(layer)
        elif layer is None:
            self._source_refs[index] = None
        if self._operation == "filter":
            self.filter_builder.set_layer(self.source_layer(0))
        self.source_changed.emit(index, layer)
        self._emit_readiness()

    def _toggle_operation_editor(self):
        self.operation_editor.setVisible(not self.operation_editor.isVisible())
        if self.operation_editor.isVisible() and self._operation == "filter":
            self.filter_builder.set_layer(self._filter_context_layer())

    def _operation_changed(self, _index):
        self._set_operation(str(self.operation_choice.currentData() or ""))
        self._emit_readiness()

    def _set_operation(self, operation):
        self._operation = operation if operation in {"filter", "merge"} else ""
        self.operation_choice.blockSignals(True)
        choice = self.operation_choice.findData(self._operation)
        self.operation_choice.setCurrentIndex(max(0, choice))
        self.operation_choice.blockSignals(False)
        merge = self._operation == "merge"
        self._slots[0].setVisible(True)
        self._slots[1].setVisible(merge)
        self._slots[0].set_title("Source A" if merge else "Source")
        self._slots[1].set_title("Source B")
        self.filter_builder.setVisible(self._operation == "filter")
        if self._operation == "filter":
            self.filter_builder.set_layer(self._filter_context_layer())
        self.merge_note.setVisible(merge)
        self.operation_block.setText(
            "FILTER\nClick to configure fields and expression"
            if self._operation == "filter"
            else "MERGE / append\nClick to review source compatibility"
            if merge
            else "Choose an operation\nClick to configure FILTER or MERGE"
        )
        self.result_block.setText(
            "Output will appear here\nPlan to create the result"
            if self._operation
            else "Output will appear here\nChoose an operation first"
        )

    def _filter_context_layer(self):
        layer = self.source_layer(0)
        if layer is not None:
            return layer
        if self._workflow is not None and self.gateway is not None and self._workflow.source_layers:
            return self.gateway.resolve_layer(self._workflow.source_layers[0].current_layer_id)
        return None

    def _toggle_result_editor(self):
        self.result_editor.setVisible(not self.result_editor.isVisible())

    def _apply_result_settings(self):
        self.result_editor.hide()
        self._configuration_changed()

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
            block = self._canvas_block(
                delivery.name or delivery.format,
                f"{delivery.format.upper()} · click to configure",
                "AtOnceCanvasOutputBlock",
            )
            block.clicked.connect(lambda _checked=False, i=index: self._open_delivery_editor(i))
            self.deliveries_layout.addWidget(block)
        self._add_output_button.setVisible(True)

    def _open_delivery_editor(self, index):
        if not 0 <= index < len(self._deliveries):
            return
        delivery = self._deliveries[index]
        self._editing_delivery_index = index
        format_index = self.output_format.findData(delivery.format)
        self.output_format.setCurrentIndex(max(0, format_index))
        self.output_name.setText(delivery.name)
        self.output_path.setText(delivery.path)
        self.output_default.setChecked(delivery.enabled_by_default)
        self.output_editor.setVisible(True)

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
        self._configuration_changed()

    def _configuration_changed(self):
        self._emit_readiness()
        self.configuration_requested.emit()

    def _emit_readiness(self):
        self.readiness_changed.emit(self.can_plan())

    def definition(self):
        """Return the current explicit configuration while preserving identities."""

        if self._operation not in {"filter", "merge"} or self.gateway is None:
            return None
        deliveries = [DeliveryRef.from_dict(item.to_dict()) for item in self._deliveries]
        result_name = self.result_name_edit.text().strip()
        if not result_name:
            result_name = "Filtered layer" if self._operation == "filter" else "Merged layer"

        if self._workflow is None:
            source_a_layer = self.source_layer(0)
            if source_a_layer is None:
                return None
            source_a = self._source_refs[0] or self.gateway.make_source_layer_ref(source_a_layer)
            if self._operation == "filter":
                return build_standalone_filter_workflow(
                    name="Filtered workflow",
                    source=source_a,
                    derived_name=result_name,
                    expression=self.filter_builder.expression(),
                    deliveries=deliveries,
                    workflow_id=self._draft_workflow_id,
                    derived_node_id=self._draft_derived_node_id or None,
                )
            source_b_layer = self.source_layer(1)
            if source_b_layer is None:
                return None
            source_b = self._source_refs[1] or self.gateway.make_source_layer_ref(source_b_layer)
            return build_standalone_merge_workflow(
                name="Merged workflow",
                sources=(source_a, source_b),
                derived_name=result_name,
                deliveries=deliveries,
                workflow_id=self._draft_workflow_id,
                derived_node_id=self._draft_derived_node_id or None,
            )

        updates = {}
        if self._filter_edge_id:
            updates[self._filter_edge_id] = self.filter_builder.expression()
        candidate = apply_explicit_workflow_edits(
            self._workflow,
            filter_expressions=updates,
            deliveries=deliveries,
        )
        graph = candidate.effective_dependency_graph()
        nodes = list(graph.nodes)
        edges = list(graph.edges)
        known_delivery_ids = {
            str(node.metadata.get("delivery_id") or "")
            for node in nodes
            if node.kind == NodeKind.DELIVERY
        }
        for delivery in deliveries:
            if delivery.delivery_id in known_delivery_ids:
                continue
            delivery_node_id = f"delivery:forward:{delivery.delivery_id}"
            nodes.append(
                DependencyNode(
                    delivery_node_id,
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
                    f"edge:{self._result_node_id}->{delivery_node_id}",
                    self._result_node_id,
                    delivery_node_id,
                    OperationKind.EXPORT,
                    enabled_by_default=delivery.enabled_by_default,
                )
            )
        candidate.dependency_graph = DependencyGraph(
            [
                DependencyNode(
                    node.node_id,
                    result_name if node.node_id == self._result_node_id else node.name,
                    node.kind,
                    node.format,
                    node.current_revision,
                    dict(node.metadata),
                )
                for node in nodes
            ],
            edges,
        )
        candidate.dependency_graph_origin = EXPLICIT_GRAPH_ORIGIN
        return candidate


class WorkflowTypeChooserDialog(QDialog):
    """Controlled first-run chooser whose final action is always Cancel."""

    FILTER = "filter"
    MERGE = "merge"
    SPREADSHEET = "spreadsheet"

    def __init__(self, include_spreadsheet=True, parent=None):
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle("Create workflow")
        self.setStyleSheet(DIALOG_STYLESHEET)
        self.resize(460, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 15, 16, 15)
        layout.setSpacing(9)
        title = QLabel("Choose a workflow")
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        note = QLabel("AtOnce saves this as configuration. Run it explicitly when you are ready.")
        note.setObjectName("AtOnceDialogSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.choice_buttons = []
        self._add_choice(layout, self.FILTER, "Filter", "One source → keep matching features.")
        self._add_choice(layout, self.MERGE, "Merge", "Two compatible sources → append features.")
        if include_spreadsheet:
            self._add_choice(
                layout,
                self.SPREADSHEET,
                "Spreadsheet sync",
                "Two related source layers → managed combined layer with reviewed spreadsheet write-back.",
            )
        layout.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("AtOnceBuilderCancel")
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.cancel_button)

    def _add_choice(self, layout, key, title, description):
        card = QFrame()
        card.setObjectName("AtOnceChooserCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        button = QPushButton(title)
        button.setObjectName("AtOnceChooserChoice")
        button.clicked.connect(lambda _checked=False, value=key: self._choose(value))
        body = QLabel(description)
        body.setObjectName("AtOnceMuted")
        body.setWordWrap(True)
        card_layout.addWidget(button)
        card_layout.addWidget(body)
        layout.addWidget(card)
        self.choice_buttons.append(button)

    def _choose(self, value):
        self.choice = value
        self.accept()


# Short aliases keep the reusable controls discoverable to older UI callers.
FilterBuilder = GuidedFilterBuilder
WorkflowBuilder = GuidedWorkflowBuilder
WorkflowCanvas = GuidedWorkflowCanvas
