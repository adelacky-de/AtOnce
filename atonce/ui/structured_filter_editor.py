"""Compact FME-like FILTER editor that emits the existing QGIS expression contract."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.lineage import RESERVED_LINEAGE_FIELDS


class StructuredFilterEditor(QWidget):
    """Build a common FILTER expression without exposing raw syntax by default."""

    OPERATORS = (
        ("Equals", "="),
        ("Not equal", "!="),
        ("Greater than", ">"),
        ("Greater than or equal", ">="),
        ("Less than", "<"),
        ("Less than or equal", "<="),
        ("Contains", "contains"),
        ("Is null", "is_null"),
        ("Is not null", "is_not_null"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._numeric_fields = set()
        self._boolean_fields = set()
        self.field_combo = QComboBox(self)
        self.field_combo.setEditable(True)
        self.field_combo.lineEdit().setPlaceholderText("Choose a field…")
        self.operator_combo = QComboBox(self)
        for label, value in self.OPERATORS:
            self.operator_combo.addItem(label, value)
        self.value_edit = QLineEdit(self)
        self.value_edit.setPlaceholderText("Enter a value…")
        self.advanced_check = QCheckBox("Advanced expression", self)
        self.advanced_edit = QLineEdit(self)
        self.advanced_edit.setPlaceholderText("Enter a QGIS expression…")
        self.advanced_edit.setVisible(False)
        self.advanced_check.toggled.connect(self._advanced_toggled)
        self.operator_combo.currentIndexChanged.connect(self._operator_changed)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Field", self.field_combo)
        form.addRow("Condition", self.operator_combo)
        form.addRow("Value", self.value_edit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(form)
        layout.addWidget(self.advanced_check)
        layout.addWidget(self.advanced_edit)

    @staticmethod
    def _field_type_name(field):
        type_name = getattr(field, "typeName", None)
        if callable(type_name):
            return str(type_name() or "").strip().lower()
        value = getattr(field, "type", "")
        value = value() if callable(value) else value
        return str(value or "").strip().lower()

    def set_layer(self, layer):
        current = self.field_combo.currentText()
        self.field_combo.clear()
        self.field_combo.addItem("")
        self._numeric_fields.clear()
        self._boolean_fields.clear()
        if layer is not None and hasattr(layer, "fields"):
            for field in layer.fields():
                name = str(field.name())
                if name in RESERVED_LINEAGE_FIELDS:
                    continue
                self.field_combo.addItem(name)
                type_name = self._field_type_name(field)
                if any(
                    token in type_name
                    for token in ("int", "real", "double", "float", "decimal", "numeric")
                ):
                    self._numeric_fields.add(name)
                if any(token in type_name for token in ("bool", "boolean")):
                    self._boolean_fields.add(name)
        self.field_combo.setCurrentText(current)

    def set_expression(self, expression):
        expression = str(expression or "").strip()
        self.advanced_edit.setText(expression)
        self.advanced_check.setChecked(bool(expression))
        self._advanced_toggled(bool(expression))

    def _advanced_toggled(self, enabled):
        self.advanced_edit.setVisible(bool(enabled))
        self.field_combo.setEnabled(not enabled)
        self.operator_combo.setEnabled(not enabled)
        self.value_edit.setEnabled(not enabled)
        self._operator_changed()

    def _operator_changed(self):
        operator = str(self.operator_combo.currentData() or "=")
        self.value_edit.setEnabled(
            not self.advanced_check.isChecked()
            and operator not in {"is_null", "is_not_null"}
        )

    @staticmethod
    def _quoted_field(name):
        return '"' + str(name).replace('"', '""') + '"'

    def _literal(self, field_name, value):
        text = str(value or "").strip()
        if field_name in self._numeric_fields:
            try:
                float(text)
                return text
            except ValueError:
                pass
        if field_name in self._boolean_fields and text.lower() in {"true", "false"}:
            return text.upper()
        return "'" + text.replace("'", "''") + "'"

    def expression(self):
        if self.advanced_check.isChecked():
            return self.advanced_edit.text().strip()
        field = self.field_combo.currentText().strip()
        if not field:
            return ""
        field_expr = self._quoted_field(field)
        operator = str(self.operator_combo.currentData() or "=")
        if operator == "is_null":
            return f"{field_expr} IS NULL"
        if operator == "is_not_null":
            return f"{field_expr} IS NOT NULL"
        value = self._literal(field, self.value_edit.text())
        if operator == "contains":
            return f"contains(to_string({field_expr}), {value})"
        return f"{field_expr} {operator} {value}"
