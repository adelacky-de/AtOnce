"""SOURCE/OPERATION/OUTPUT configuration dialogs for the AtOnce canvas."""

from dataclasses import replace

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.freeform_graph import set_output_inclusion
from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY
from ..models.dependency_graph import NodeKind
from .freeform_workflow_canvas import _value
from .operation_parameter_editor import OperationParameterEditor
from .structured_filter_editor import StructuredFilterEditor
from .styles import DIALOG_STYLESHEET


class CanvasBlockDialogMixin:
    """Focused block-configuration UI mixed into the workflow canvas."""

    def _show_source_dialog(self, node):
        if self.gateway is None:
            self.message_requested.emit("No loaded layer gateway is available.")
            return
        layers = list(self.gateway.source_candidate_layers())
        if not layers:
            self.message_requested.emit(
                "No loaded vector or raster layers are available in this QGIS project."
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Source")
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        title = QLabel("Select source layer")
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Vector layers (including GeoPackage) support full operations. "
            "Raster layers (for example GeoTIFF) support CRS conversion and GeoTIFF/KMZ output only."
        )
        hint.setObjectName("AtOnceMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        combo = QComboBox(dialog)
        current_lineage = str(node.metadata.get("source_lineage_id") or "")
        current_ref = self._source_refs.get(current_lineage)
        current_layer_id = current_ref.current_layer_id if current_ref else ""
        current_index = 0
        for index, layer in enumerate(layers):
            kind = self.gateway.layer_data_type(layer)
            label = str(_value(getattr(layer, "name", ""), "Unnamed layer"))
            combo.addItem(f"{label} ({kind})", layer)
            if str(_value(getattr(layer, "id", "")) or "") == current_layer_id:
                current_index = index
        combo.setCurrentIndex(current_index)
        layout.addWidget(combo)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")
        cancel.clicked.connect(dialog.reject)
        apply.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        if dialog.exec_() == QDialog.Accepted:
            self._bind_source(node.node_id, combo.currentData())

    def _upstream_data_type(self, node_id):
        """Resolve raster/vector from the nearest bound SOURCE ancestor."""

        node_map = self._graph.node_map()
        seen = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            node = node_map.get(current)
            if node is None:
                continue
            if node.kind == NodeKind.SOURCE:
                return str(node.metadata.get("data_type") or "vector")
            for edge in self._graph.edges:
                if edge.to_node == current:
                    frontier.append(edge.from_node)
        return "vector"

    def _operations_for_data_type(self, data_type):
        wanted = str(data_type or "vector")
        return sorted(
            (
                item
                for item in DEFAULT_OPERATION_REGISTRY.all()
                if wanted in tuple(getattr(item, "accepted_data_types", ("vector",)) or ("vector",))
            ),
            key=lambda item: (item.category, item.title),
        )

    def _show_operation_dialog(self, node):
        dialog = QDialog(self)
        dialog.setWindowTitle("Operation")
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        title = QLabel("Configure operation")
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        function_combo = QComboBox(dialog)
        function_combo.addItem("Select function…", "")
        data_type = self._upstream_data_type(node.node_id)
        definitions = self._operations_for_data_type(data_type)
        for definition in definitions:
            function_combo.addItem(
                f"{definition.category} — {definition.title}", definition.kind.value
            )
        current_kind = str(node.metadata.get("operation_kind") or "")
        current_index = function_combo.findData(current_kind)
        function_combo.setCurrentIndex(current_index if current_index >= 0 else 0)
        form.addRow("Function", function_combo)
        layout.addLayout(form)

        filter_editor = StructuredFilterEditor(dialog)
        generic_editor = OperationParameterEditor(dialog)
        filter_editor.setVisible(False)
        generic_editor.setVisible(False)
        layout.addWidget(filter_editor)
        layout.addWidget(generic_editor)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        generic_values = {"value": {}}

        def generic_changed(parameters):
            generic_values["value"] = dict(parameters or {})

        generic_editor.parameters_changed.connect(generic_changed)

        def configure_editor():
            kind = str(function_combo.currentData() or "")
            definition = DEFAULT_OPERATION_REGISTRY.get(kind)
            parameters = (
                node.metadata.get("parameters") or {} if kind == current_kind else {}
            )
            is_filter = kind == "filter"
            filter_editor.setVisible(is_filter)
            generic_editor.setVisible(bool(definition) and not is_filter)
            if is_filter:
                filter_editor.set_layer(self._filter_context_layer(node.node_id))
                filter_editor.set_expression(str(parameters.get("expression") or ""))
            elif definition is not None:
                generic_values["value"] = definition.parameter_defaults()
                generic_values["value"].update(dict(parameters or {}))
                generic_editor.set_definition(
                    definition,
                    parameters,
                    self._operation_context_layers(node.node_id),
                )

        def apply_operation():
            kind = str(function_combo.currentData() or "")
            definition = DEFAULT_OPERATION_REGISTRY.get(kind)
            if definition is None:
                self.message_requested.emit(
                    "Select a function before applying this operation."
                )
                return
            if kind == "filter":
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
            metadata = dict(node.metadata)
            metadata.update(
                {
                    "workflow_role": "operation",
                    "operation_kind": definition.kind.value,
                    "operation_version": definition.version,
                    "parameters": parameters,
                }
            )
            self._replace_node(
                replace(node, name=definition.title, metadata=metadata)
            )
            self.graph_changed.emit()
            dialog.accept()

        function_combo.currentIndexChanged.connect(configure_editor)
        apply.clicked.connect(apply_operation)
        configure_editor()
        dialog.adjustSize()
        dialog.exec_()

    def _registered_output_node(self, node_id):
        """Return the persisted version of an output already in this workflow."""

        workflow = getattr(self, "_workflow", None)
        if workflow is None:
            return None
        try:
            persisted = workflow.effective_dependency_graph().node_map().get(str(node_id))
        except Exception:
            return None
        if persisted is None or persisted.kind != NodeKind.DELIVERY:
            return None
        return persisted

    def _show_output_dialog(self, node):
        dialog = QDialog(self)
        dialog.setWindowTitle("Output")
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        title = QLabel("Configure output")
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        data_type = self._upstream_data_type(node.node_id)
        is_raster = data_type == "raster"

        name_edit = QLineEdit(dialog)
        name_edit.setText("" if node.name in {"", "Output"} else node.name)
        name_edit.setPlaceholderText("Output name")
        format_combo = QComboBox(dialog)
        format_choices = (
            (("GeoTIFF", "geotiff"), ("KMZ", "kmz"))
            if is_raster
            else (
                ("GeoJSON", "geojson"),
                ("Shapefile", "shapefile"),
                ("KML", "kml"),
                ("KMZ", "kmz"),
                ("GeoPackage", "gpkg"),
                ("GeoTIFF", "geotiff"),
            )
        )
        for label, value in format_choices:
            format_combo.addItem(label, value)
        index = format_combo.findData(node.format)
        format_combo.setCurrentIndex(index if index >= 0 else 0)

        path_widget = QWidget(dialog)
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit(path_widget)
        path_edit.setText(str(node.metadata.get("path") or ""))
        path_edit.setPlaceholderText("Choose output path…")
        browse = QPushButton("Browse…", path_widget)
        path_layout.addWidget(path_edit, 1)
        path_layout.addWidget(browse)

        include_changes = None
        if not is_raster:
            include_changes = QCheckBox("Include in Changes", dialog)
            include_changes.setChecked(
                bool(node.metadata.get("include_in_changes", True))
            )
        form.addRow("Name", name_edit)
        form.addRow("Format", format_combo)
        form.addRow("Path", path_widget)
        if include_changes is not None:
            form.addRow("", include_changes)
        layout.addLayout(form)

        def browse_path():
            fmt = str(format_combo.currentData() or "geojson")
            suffix = {
                "geojson": "GeoJSON (*.geojson)",
                "shapefile": "Shapefile (*.shp)",
                "kml": "KML (*.kml)",
                "kmz": "KMZ (*.kmz)",
                "gpkg": "GeoPackage (*.gpkg)",
                "geotiff": "GeoTIFF (*.tif *.tiff)",
            }[fmt]
            path, _ = QFileDialog.getSaveFileName(
                dialog, "Choose output path", path_edit.text(), suffix
            )
            if path:
                path_edit.setText(path)

        browse.clicked.connect(browse_path)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")
        cancel.clicked.connect(dialog.reject)

        def apply_output():
            path = path_edit.text().strip()
            if not path:
                self.message_requested.emit(
                    "Choose an output path before applying this output."
                )
                return
            fmt = str(format_combo.currentData() or "geojson")
            included = False if is_raster else include_changes.isChecked()
            proposed_name = name_edit.text().strip() or fmt.upper()

            # A registered OUTPUT is one task endpoint. Its identity must stay
            # immutable after registration: renaming/repointing it would make an
            # old materialized result appear to be a new output. Users who need
            # another path/name/format add another OUTPUT block instead.
            persisted = self._registered_output_node(node.node_id)
            if persisted is not None:
                old_name = str(persisted.name or "")
                old_path = str((persisted.metadata or {}).get("path") or "")
                old_format = str(persisted.format or (persisted.metadata or {}).get("format") or "")
                if (
                    proposed_name != old_name
                    or path != old_path
                    or fmt != old_format
                ):
                    self.message_requested.emit(
                        "This output is already registered and cannot be renamed, "
                        "repointed, or changed to another format. Add a new OUTPUT "
                        "block if you need another output. Only the workflow name "
                        "may be renamed while keeping the same registered task/output."
                    )
                    return

            metadata = dict(node.metadata)
            metadata.update(
                {
                    "path": path,
                    "format": fmt,
                    "include_in_changes": included,
                    "data_type": data_type,
                }
            )
            updated = replace(
                node,
                name=proposed_name,
                format=fmt,
                metadata=metadata,
            )
            self._graph.nodes = [
                updated if item.node_id == node.node_id else item
                for item in self._graph.nodes
            ]
            self._graph = set_output_inclusion(self._graph, node.node_id, included)
            self._render_graph()
            self.graph_changed.emit()
            dialog.accept()

        apply.clicked.connect(apply_output)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        dialog.adjustSize()
        dialog.exec_()