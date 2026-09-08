"""Compact Material-style, registry-driven parameter editor."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.lineage import RESERVED_LINEAGE_FIELDS


class _StructuredRowsEditor(QWidget):
    """Typed row editor for mapping/sort/aggregate registry parameters."""

    def __init__(self, columns, rows=(), parent=None):
        super().__init__(parent)
        self.columns = tuple(columns)
        self.choices = {
            "output_type": ("string", "integer", "double", "boolean", "date"),
            "direction": ("asc", "desc"),
            "function": ("count", "sum", "min", "max", "mean"),
        }
        self.table = QTableWidget(0, len(self.columns), self)
        self.table.setObjectName("AtOnceMaterialTable")
        self.table.setHorizontalHeaderLabels(
            [label.replace("_", " ").title() for label in self.columns]
        )
        self.table.setMinimumHeight(92)
        self.add_button = QPushButton("+ Add row", self)
        self.remove_button = QPushButton("Remove", self)
        self.add_button.clicked.connect(self._add_row)
        self.remove_button.clicked.connect(self._remove_row)
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.set_rows(rows)

    def _add_row(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column in range(self.table.columnCount()):
            name = self.columns[column]
            if name in self.choices:
                combo = QComboBox(self.table)
                for choice in self.choices[name]:
                    combo.addItem(str(choice).replace("_", " ").title(), choice)
                self.table.setCellWidget(row, column, combo)
            else:
                self.table.setItem(row, column, QTableWidgetItem(""))

    def _remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def set_rows(self, rows):
        self.table.setRowCount(0)
        for value in rows or ():
            self._add_row()
            row = self.table.rowCount() - 1
            for column, name in enumerate(self.columns):
                cell = self.table.cellWidget(row, column)
                if isinstance(cell, QComboBox):
                    index = cell.findData(value.get(name, ""))
                    if index < 0:
                        index = cell.findText(str(value.get(name, "")))
                    cell.setCurrentIndex(max(0, index))
                else:
                    self.table.item(row, column).setText(str(value.get(name, "")))

    def value(self):
        values = []
        for row in range(self.table.rowCount()):
            value = {}
            for column, name in enumerate(self.columns):
                cell = self.table.cellWidget(row, column)
                value[name] = (
                    cell.currentData() or cell.currentText().strip()
                    if isinstance(cell, QComboBox)
                    else self.table.item(row, column).text().strip()
                )
            if any(value.values()):
                values.append(value)
        return values


class _FieldListEditor(QWidget):
    def __init__(self, fields=(), selected=(), parent=None):
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.setObjectName("AtOnceMaterialFieldList")
        self.list.setMinimumHeight(86)
        selected = {str(item) for item in selected or ()}
        for field in fields or ():
            item = QListWidgetItem(str(field), self.list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if str(field) in selected else Qt.Unchecked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)

    def value(self):
        return [
            self.list.item(index).text()
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.Checked
        ]


class OperationParameterEditor(QWidget):
    """Render registry parameters as compact typed Material-style controls."""

    parameters_changed = pyqtSignal(dict)

    _LABELS = {
        "left_field": "Left field",
        "right_field": "Right field",
        "join_type": "Join type",
        "previous_key_field": "Previous key field",
        "current_key_field": "Current key field",
        "source_field": "Source field",
        "target_name": "New field name",
        "field": "Field",
        "field_name": "New field name",
        "target_type": "Target field type",
        "field_type": "Result field type",
        "target_crs": "Target CRS",
        "collision_suffix": "Copied-field suffix",
        "distance": "Distance",
        "unit": "Units",
        "segments": "Segments",
        "predicate": "Spatial relationship",
        "match_policy": "Match policy",
        "expression": "Expression",
        "fields": "Fields",
        "right_fields": "Fields to copy",
        "group_fields": "Group by fields",
        "compare_fields": "Compare fields",
        "mappings": "Field mappings",
        "sort_fields": "Sort fields",
        "aggregations": "Aggregations",
        "create_field": "Create field",
        "dissolve": "Dissolve result",
        "dissolve_all": "Dissolve all",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.definition = None
        self._widgets = {}
        self._field_widgets = {}
        self._context_layers = ()
        self.title = QLabel("Choose an operation")
        self.title.setObjectName("AtOnceSectionTitle")
        self.hint = QLabel("Configure this block, then Apply.")
        self.hint.setObjectName("AtOnceMuted")
        self.hint.setWordWrap(True)
        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(18)
        self.form.setVerticalSpacing(9)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.apply_button = QPushButton("Apply block settings")
        self.apply_button.setObjectName("AtOnceBuilderSecondary")
        self.apply_button.clicked.connect(self._emit_parameters)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addLayout(self.form)
        layout.addWidget(self.apply_button, 0, Qt.AlignLeft)

        # Popups own the heading and Cancel/Apply row; never duplicate them.
        if isinstance(parent, QDialog):
            self.title.setVisible(False)
            self.hint.setVisible(False)
            self.apply_button.setVisible(False)

    def set_definition(self, definition, parameters=None, layers=()):
        self.definition = definition
        self._context_layers = layers or ()
        self._clear_form()
        if definition is None:
            self.title.setText("Choose an operation")
            self.hint.setText("Click a block to configure only that block.")
            self.apply_button.setEnabled(False)
            return

        self.title.setText(definition.title)
        self.apply_button.setEnabled(True)
        values = definition.parameter_defaults()
        values.update(dict(parameters or {}))
        definitions = {item.parameter_id: item for item in definition.parameter_definitions}
        for key in definition.parameter_schema:
            parameter = definitions.get(key)
            if parameter is None:
                continue
            widget = self._make_widget(parameter, values.get(key))
            self._widgets[key] = widget
            self.form.addRow(
                self._LABELS.get(key, key.replace("_", " ").title()), widget
            )

    def set_parameters(self, parameters):
        if self.definition is not None:
            self.set_definition(self.definition, parameters, self._context_layers)

    def _clear_form(self):
        self._widgets.clear()
        self._field_widgets.clear()
        while self.form.rowCount():
            self.form.removeRow(0)

    def _make_widget(self, parameter, value):
        key = parameter.parameter_id
        value_type = parameter.value_type
        if parameter.choices:
            widget = QComboBox(self)
            for data in parameter.choices:
                widget.addItem(str(data).replace("_", " ").title(), data)
            index = widget.findData(value)
            widget.setCurrentIndex(index if index >= 0 else 0)
            return widget
        if value_type == "bool":
            widget = QCheckBox(self)
            widget.setChecked(value is True)
            return widget
        if value_type == "float":
            widget = QDoubleSpinBox(self)
            widget.setRange(0.0, 1_000_000_000.0)
            widget.setDecimals(6)
            widget.setValue(float(value) if value not in (None, "") else 0.0)
            return widget
        if value_type == "int":
            widget = QSpinBox(self)
            widget.setRange(1, 10_000)
            widget.setValue(int(value) if value not in (None, "") else 8)
            return widget
        if parameter.ui_kind == "field":
            widget = QComboBox(self)
            widget.setEditable(True)
            widget.lineEdit().setPlaceholderText("Choose a field…")
            widget.addItem("", "")
            names = []
            for layer in self._context_for(parameter):
                if not hasattr(layer, "fields"):
                    continue
                names.extend(
                    field.name()
                    for field in layer.fields()
                    if field.name() not in names
                    and field.name() not in RESERVED_LINEAGE_FIELDS
                )
            for name in names:
                widget.addItem(name, name)
            widget.setCurrentText(str(value or ""))
            self._field_widgets[key] = widget
            return widget
        if parameter.ui_kind == "field-list":
            names = []
            for layer in self._context_for(parameter):
                if not hasattr(layer, "fields"):
                    continue
                names.extend(
                    field.name()
                    for field in layer.fields()
                    if field.name() not in names
                    and field.name() not in RESERVED_LINEAGE_FIELDS
                )
            return _FieldListEditor(names, value, self)
        if parameter.ui_kind == "structured":
            return _StructuredRowsEditor(parameter.row_fields, value or (), self)
        widget = QLineEdit(self)
        placeholder = {
            "expression": "Enter an expression…",
            "target_crs": "EPSG:4326",
            "distance": "Enter a distance…",
            "collision_suffix": "_join",
        }.get(key, "Enter a value…")
        widget.setPlaceholderText(placeholder)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        widget.setText(str(value or ""))
        return widget

    def _context_for(self, parameter):
        if not isinstance(self._context_layers, dict):
            return tuple(self._context_layers or ())
        if parameter.context_port == "both":
            values = []
            for port in ("previous", "current"):
                value = self._context_layers.get(port)
                if isinstance(value, (tuple, list)):
                    values.extend(value)
                elif value is not None:
                    values.append(value)
            return tuple(values)
        value = self._context_layers.get(parameter.context_port)
        if value is None:
            value = self._context_layers.get("input", ())
        if isinstance(value, (tuple, list)):
            return tuple(value)
        return (value,) if value is not None else ()

    def _emit_parameters(self):
        values = {}
        for key, widget in self._widgets.items():
            if isinstance(widget, QComboBox):
                values[key] = widget.currentData() or widget.currentText().strip()
            elif isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                values[key] = widget.value()
            elif hasattr(widget, "value") and not isinstance(widget, QLineEdit):
                values[key] = widget.value()
            else:
                values[key] = widget.text().strip()
        self.parameters_changed.emit(values)
