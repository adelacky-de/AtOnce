"""Workflow registration dialog for the first AtOnce A/B -> C workflow."""

from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsMapLayerType, QgsProject

from ..models.workflow import (
    DERIVED_ROLE,
    SAME_BUSINESS_KEY,
    SOURCE_ROLE,
    ExportRef,
    DeliveryRef,
    FieldMapping,
    LayerRef,
    WorkflowDefinition,
)
from .styles import DIALOG_STYLESHEET


class ExportEditorRow(QFrame):
    remove_requested = pyqtSignal(object)

    def __init__(self, export: Optional[ExportRef] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("AtOnceExportCard")
        self.export_id = export.export_id if export else str(uuid4())
        self._original = export

        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 10, 11, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("Linked XLSX output")
        title.setObjectName("AtOnceSectionTitle")
        self.name_edit = QLineEdit(export.name if export else "")
        self.name_edit.setPlaceholderText("e.g. XLSX 1")
        remove_button = QPushButton("Remove")
        remove_button.setObjectName("AtOnceDangerQuiet")
        remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(remove_button)
        layout.addLayout(top)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)
        form.addRow("Name", self.name_edit)

        path_widget = QWidget()
        path_row = QHBoxLayout(path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        self.path_edit = QLineEdit(export.path if export else "")
        self.path_edit.setPlaceholderText("/path/to/output.xlsx")
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_button)
        form.addRow("File", path_widget)

        self.filter_edit = QLineEdit(export.filter_expression if export else "")
        self.filter_edit.setPlaceholderText('Optional QGIS expression, e.g. "status" = \'Open\'')
        form.addRow("Filter", self.filter_edit)
        layout.addLayout(form)

    def _browse(self):
        initial = self.path_edit.text().strip() or str(Path.home() / "output.xlsx")
        path, _ = QFileDialog.getSaveFileName(
            self, "Linked XLSX output", initial, "Excel workbook (*.xlsx)"
        )
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self.path_edit.setText(path)

    def export_ref(self, source_layer_id: Optional[str]) -> ExportRef:
        path = self.path_edit.text().strip()
        same_path = bool(self._original and path == self._original.path)
        return ExportRef(
            export_id=self.export_id,
            name=self.name_edit.text().strip(),
            path=path,
            filter_expression=self.filter_edit.text().strip(),
            source_layer_id=source_layer_id,
            relink_revision_number=(
                self._original.relink_revision_number if same_path else None
            ),
            relink_sha256=self._original.relink_sha256 if same_path else "",
            relink_modified=self._original.relink_modified if same_path else False,
        )


class DeliveryEditorRow(QFrame):
    remove_requested = pyqtSignal(object)

    def __init__(self, delivery: Optional[DeliveryRef] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("AtOnceExportCard")
        self.delivery_id = delivery.delivery_id if delivery else str(uuid4())
        layout = QFormLayout(self)
        self.name_edit = QLineEdit(delivery.name if delivery else "GeoJSON")
        self.format_combo = QComboBox()
        self.format_combo.addItem("GeoJSON", "geojson")
        self.format_combo.addItem("ESRI Shapefile", "shapefile")
        self.format_combo.addItem("KML", "kml")
        self.format_combo.addItem("KMZ", "kmz")
        self.format_combo.addItem("GeoPackage", "gpkg")
        if delivery:
            index = self.format_combo.findData(delivery.format)
            if index >= 0:
                self.format_combo.setCurrentIndex(index)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        path_widget = QWidget()
        path_row = QHBoxLayout(path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        self.path_edit = QLineEdit(delivery.path if delivery else "")
        self.path_edit.setPlaceholderText(self._path_placeholder())
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_button)
        self.enabled = QCheckBox("Include by default")
        self.enabled.setChecked(delivery.enabled_by_default if delivery else True)
        remove = QPushButton("Remove")
        remove.setObjectName("AtOnceDangerQuiet")
        remove.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addRow("Name", self.name_edit)
        layout.addRow("Format", self.format_combo)
        layout.addRow("File", path_widget)
        layout.addRow("", self.enabled)
        layout.addRow("", remove)

    def _path_placeholder(self):
        return {
            "shapefile": "/path/to/output.shp",
            "kml": "/path/to/output.kml",
            "kmz": "/path/to/output.kmz",
            "gpkg": "/path/to/output.gpkg",
        }.get(self.format_combo.currentData(), "/path/to/output.geojson")

    def _format_changed(self):
        self.path_edit.setPlaceholderText(self._path_placeholder())

    def _browse(self):
        format_name = self.format_combo.currentData()
        suffix = {
            "shapefile": ".shp",
            "kml": ".kml",
            "kmz": ".kmz",
            "gpkg": ".gpkg",
        }.get(format_name, ".geojson")
        initial = self.path_edit.text().strip() or str(Path.home() / f"output{suffix}")
        title = {
            "shapefile": "Shapefile output",
            "kml": "KML output",
            "kmz": "KMZ output",
            "gpkg": "GeoPackage output",
        }.get(format_name, "GeoJSON output")
        file_filter = {
            "shapefile": "ESRI Shapefile (*.shp)",
            "kml": "KML (*.kml)",
            "kmz": "KMZ (*.kmz)",
            "gpkg": "GeoPackage (*.gpkg)",
        }.get(format_name, "GeoJSON (*.geojson)")
        path, _ = QFileDialog.getSaveFileName(
            self, title, initial, file_filter
        )
        if path:
            if not path.lower().endswith(suffix):
                path += suffix
            self.path_edit.setText(path)

    def delivery_ref(self) -> DeliveryRef:
        return DeliveryRef(
            self.delivery_id, self.name_edit.text().strip(), self.format_combo.currentData(),
            self.path_edit.text().strip(), self.enabled.isChecked(),
        )


class WorkflowRegistrationDialog(QDialog):
    """Collect a serializable workflow definition without changing source data.

    Once a workflow exists, source identity/binding controls are intentionally
    locked here. Source continuity must go through Relink Source and a genuinely
    new authority must go through Replace Source so generic configuration edits
    can never silently rewrite historical lineage identity.
    """

    def __init__(self, workflow: Optional[WorkflowDefinition] = None, parent=None):
        super().__init__(parent)
        self.workflow = workflow
        self.project = QgsProject.instance()
        self._export_rows: List[ExportEditorRow] = []
        self._delivery_rows: List[DeliveryEditorRow] = []
        self._existing_refs = (
            {layer.layer_id: layer for layer in workflow.layers if layer.layer_id} if workflow else {}
        )
        self._existing_mapping = workflow.primary_field_mapping if workflow else None

        self.setWindowTitle("Register AtOnce workflow")
        self.resize(700, 820)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 15, 16, 15)
        root.setSpacing(10)

        title = QLabel("Register workflow")
        title.setObjectName("AtOnceDialogTitle")
        subtitle = QLabel(
            "Define the dependency chain AtOnce should track. This step stores configuration only."
        )
        subtitle.setObjectName("AtOnceDialogSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 2, 0, 2)
        content_layout.setSpacing(10)

        basics = self._section_card("Basics")
        basics_form = QFormLayout()
        basics_form.setHorizontalSpacing(12)
        basics_form.setVerticalSpacing(8)
        self.name_edit = QLineEdit(workflow.name if workflow else "AtOnce workflow")
        basics_form.addRow("Workflow name", self.name_edit)
        basics.layout().addLayout(basics_form)
        content_layout.addWidget(basics)

        sources = workflow.source_layers if workflow else []
        source_card = self._section_card("Sources")
        source_help = QLabel(
            "Choose the loaded vector layers that remain the source of truth."
            if workflow is None
            else "Source identity is protected after registration. Use Relink Source for the same lineage or Replace Source for a new authoritative dataset."
        )
        source_help.setObjectName("AtOnceMuted")
        source_help.setWordWrap(True)
        source_card.layout().addWidget(source_help)
        source_form = QFormLayout()
        source_form.setHorizontalSpacing(12)
        source_form.setVerticalSpacing(8)
        self.source_a_combo = self._make_layer_combo(sources[0] if len(sources) > 0 else None)
        self.source_b_combo = self._make_layer_combo(sources[1] if len(sources) > 1 else None)
        if not workflow and self.source_b_combo.count() > 1:
            self.source_b_combo.setCurrentIndex(1)
        if workflow:
            self.source_a_combo.setEnabled(False)
            self.source_b_combo.setEnabled(False)
        source_form.addRow("Source Layer A", self.source_a_combo)
        source_form.addRow("Source Layer B", self.source_b_combo)
        source_card.layout().addLayout(source_form)
        content_layout.addWidget(source_card)

        relationship_card = self._section_card("Record relationship")
        relationship_help = QLabel(
            "The two source layers may use different field names for the same business key. "
            "Choose the matching fields and confirm their meaning once. AtOnce will not infer an authoritative relationship silently."
        )
        relationship_help.setObjectName("AtOnceMuted")
        relationship_help.setWordWrap(True)
        relationship_card.layout().addWidget(relationship_help)

        relationship_form = QFormLayout()
        relationship_form.setHorizontalSpacing(12)
        relationship_form.setVerticalSpacing(8)
        self.source_a_field_combo = QComboBox()
        self.source_b_field_combo = QComboBox()
        relationship_form.addRow("Layer A field", self.source_a_field_combo)
        relationship_form.addRow("Layer B field", self.source_b_field_combo)
        relationship_card.layout().addLayout(relationship_form)

        relationship_type = QLabel("Same business key")
        relationship_type.setObjectName("AtOnceMuted")
        relationship_card.layout().addWidget(relationship_type)
        self.mapping_confirm = QCheckBox(
            "I confirm these two fields represent the same business key for this workflow."
        )
        self.mapping_confirm.setChecked(bool(self._existing_mapping and self._existing_mapping.confirmed))
        relationship_card.layout().addWidget(self.mapping_confirm)

        lineage_note = QLabel(
            "This mapping is for cross-layer matching only. AtOnce uses immutable source lineage + feature UUID for tracking."
        )
        lineage_note.setObjectName("AtOnceMuted")
        lineage_note.setWordWrap(True)
        relationship_card.layout().addWidget(lineage_note)
        content_layout.addWidget(relationship_card)

        self._refresh_field_mapping_controls()
        self._mapping_source_pair = (
            self.source_a_combo.currentData(),
            self.source_b_combo.currentData(),
        )
        self.source_a_combo.currentIndexChanged.connect(self._source_selection_changed)
        self.source_b_combo.currentIndexChanged.connect(self._source_selection_changed)
        self.source_a_field_combo.currentIndexChanged.connect(self._mapping_field_changed)
        self.source_b_field_combo.currentIndexChanged.connect(self._mapping_field_changed)

        derived = workflow.derived_layer if workflow else None
        derived_card = self._section_card("Derived target")
        derived_help = QLabel(
            "Use an existing Layer C or keep it planned until AtOnce creates/rebuilds it."
        )
        derived_help.setObjectName("AtOnceMuted")
        derived_help.setWordWrap(True)
        derived_card.layout().addWidget(derived_help)
        derived_form = QFormLayout()
        derived_form.setHorizontalSpacing(12)
        derived_form.setVerticalSpacing(8)
        self.derived_combo = self._make_layer_combo(derived, allow_empty=True)
        self.derived_name_edit = QLineEdit(derived.name if derived else "Layer C")
        self.derived_name_edit.setPlaceholderText("Layer C")
        self.derived_combo.currentIndexChanged.connect(self._sync_derived_name)
        derived_form.addRow("Existing Layer C", self.derived_combo)
        derived_form.addRow("Layer C name", self.derived_name_edit)
        derived_card.layout().addLayout(derived_form)
        content_layout.addWidget(derived_card)

        outputs_card = self._section_card("Outputs")
        outputs_help = QLabel(
            "Register the GeoPackage target and every linked XLSX export. Filters are stored as QGIS expressions. Use Relink workbook when an existing linked XLSX was moved."
        )
        outputs_help.setObjectName("AtOnceMuted")
        outputs_help.setWordWrap(True)
        outputs_card.layout().addWidget(outputs_help)

        gpkg_widget = QWidget()
        gpkg_row = QHBoxLayout(gpkg_widget)
        gpkg_row.setContentsMargins(0, 0, 0, 0)
        gpkg_row.setSpacing(6)
        self.gpkg_edit = QLineEdit(workflow.gpkg_path if workflow else "")
        self.gpkg_edit.setPlaceholderText("/path/to/derived_output.gpkg")
        gpkg_browse = QPushButton("Browse…")
        gpkg_browse.clicked.connect(self._browse_gpkg)
        gpkg_row.addWidget(self.gpkg_edit, 1)
        gpkg_row.addWidget(gpkg_browse)
        outputs_form = QFormLayout()
        outputs_form.setHorizontalSpacing(12)
        outputs_form.setVerticalSpacing(8)
        outputs_form.addRow("GeoPackage", gpkg_widget)
        outputs_card.layout().addLayout(outputs_form)

        self.exports_container = QWidget()
        self.exports_layout = QVBoxLayout(self.exports_container)
        self.exports_layout.setContentsMargins(0, 2, 0, 0)
        self.exports_layout.setSpacing(8)
        outputs_card.layout().addWidget(self.exports_container)

        exports = workflow.exports if workflow else []
        if exports:
            for export in exports:
                self._add_export_row(export)
        else:
            self._add_export_row(ExportRef(str(uuid4()), "XLSX 1", ""))
            self._add_export_row(ExportRef(str(uuid4()), "XLSX 2", ""))

        add_export = QPushButton("+ Add XLSX output")
        add_export.clicked.connect(lambda: self._add_export_row())
        outputs_card.layout().addWidget(add_export)

        deliveries_label = QLabel("Forward deliveries")
        deliveries_label.setObjectName("AtOnceSectionTitle")
        outputs_card.layout().addWidget(deliveries_label)
        self.deliveries_container = QWidget()
        self.deliveries_layout = QVBoxLayout(self.deliveries_container)
        self.deliveries_layout.setContentsMargins(0, 2, 0, 0)
        outputs_card.layout().addWidget(self.deliveries_container)
        for delivery in (workflow.forward_deliveries if workflow else []):
            self._add_delivery_row(delivery)
        add_delivery = QPushButton("+ Add GeoJSON delivery")
        add_delivery.clicked.connect(lambda: self._add_delivery_row())
        outputs_card.layout().addWidget(add_delivery)
        add_shapefile = QPushButton("+ Add Shapefile delivery")
        add_shapefile.clicked.connect(
            lambda: self._add_delivery_row(DeliveryRef(str(uuid4()), "Shapefile", "shapefile", ""))
        )
        outputs_card.layout().addWidget(add_shapefile)
        add_kml = QPushButton("+ Add KML delivery")
        add_kml.clicked.connect(
            lambda: self._add_delivery_row(DeliveryRef(str(uuid4()), "KML", "kml", ""))
        )
        outputs_card.layout().addWidget(add_kml)
        add_kmz = QPushButton("+ Add KMZ delivery")
        add_kmz.clicked.connect(
            lambda: self._add_delivery_row(DeliveryRef(str(uuid4()), "KMZ", "kmz", ""))
        )
        outputs_card.layout().addWidget(add_kmz)
        add_gpkg = QPushButton("+ Add GeoPackage delivery")
        add_gpkg.clicked.connect(
            lambda: self._add_delivery_row(DeliveryRef(str(uuid4()), "GeoPackage", "gpkg", ""))
        )
        outputs_card.layout().addWidget(add_gpkg)
        content_layout.addWidget(outputs_card)

        safety = QFrame()
        safety.setObjectName("AtOnceSafetyNote")
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(11, 9, 11, 9)
        safety_title = QLabel("Configuration only")
        safety_title.setObjectName("AtOnceSectionTitle")
        safety_note = QLabel(
            "Saving this dialog does not modify source features, add UUID fields, rebuild Layer C, or generate exports."
        )
        safety_note.setObjectName("AtOnceMuted")
        safety_note.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_note)
        content_layout.addWidget(safety)
        content_layout.addStretch(1)

        content_scroll.setWidget(content)
        root.addWidget(content_scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Save)
        if save_button is not None:
            save_button.setObjectName("AtOncePrimary")
            save_button.setText("Save workflow")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _section_card(title):
        card = QFrame()
        card.setObjectName("AtOnceDialogCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("AtOnceSectionTitle")
        layout.addWidget(title_label)
        return card

    def _vector_layers(self):
        return [
            layer
            for layer in self.project.mapLayers().values()
            if layer.type() == QgsMapLayerType.VectorLayer
        ]

    def _make_layer_combo(self, current: Optional[LayerRef], allow_empty: bool = False) -> QComboBox:
        combo = QComboBox()
        if allow_empty:
            combo.addItem("Create / rebuild later", None)

        current_binding = current.current_layer_id if current else ""
        found_current = False
        for layer in self._vector_layers():
            combo.addItem(layer.name(), layer.id())
            if current_binding and layer.id() == current_binding:
                combo.setCurrentIndex(combo.count() - 1)
                found_current = True

        if current and current_binding and not found_current:
            combo.insertItem(0, f"[Missing] {current.name}", current_binding)
            combo.setCurrentIndex(0)
            combo.setProperty("atonce_missing_layer_id", current_binding)

        return combo

    def _field_names_for_layer_id(self, layer_id: Optional[str]) -> List[str]:
        layer = self.project.mapLayer(layer_id) if layer_id else None
        if layer is None or not hasattr(layer, "fields"):
            return []
        return [field.name() for field in layer.fields()]

    def _populate_field_combo(self, combo: QComboBox, layer_id: Optional[str], selected: str = ""):
        combo.blockSignals(True)
        combo.clear()
        names = self._field_names_for_layer_id(layer_id)
        for name in names:
            combo.addItem(name, name)

        if selected:
            index = combo.findData(selected)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.insertItem(0, f"[Missing] {selected}", selected)
                combo.setCurrentIndex(0)

        if combo.count() == 0:
            combo.addItem("No fields available", "")
            combo.setEnabled(False)
        else:
            combo.setEnabled(True)
        combo.blockSignals(False)

    def _source_selection_changed(self):
        source_pair = (
            self.source_a_combo.currentData(),
            self.source_b_combo.currentData(),
        )
        if source_pair != self._mapping_source_pair:
            self.mapping_confirm.setChecked(False)
        self._mapping_source_pair = source_pair
        self._refresh_field_mapping_controls()

    def _mapping_field_changed(self):
        self.mapping_confirm.setChecked(False)

    def _refresh_field_mapping_controls(self):
        left_binding = self.source_a_combo.currentData()
        right_binding = self.source_b_combo.currentData()

        selected_left = self._existing_mapping.left_field if self._existing_mapping else ""
        selected_right = self._existing_mapping.right_field if self._existing_mapping else ""
        self._populate_field_combo(self.source_a_field_combo, left_binding, selected_left)
        self._populate_field_combo(self.source_b_field_combo, right_binding, selected_right)

    def _sync_derived_name(self):
        layer_id = self.derived_combo.currentData()
        layer = self.project.mapLayer(layer_id) if layer_id else None
        if layer is not None:
            self.derived_name_edit.setText(layer.name())

    def _browse_gpkg(self):
        initial = self.gpkg_edit.text().strip() or str(Path.home() / "atonce_output.gpkg")
        path, _ = QFileDialog.getSaveFileName(
            self, "GeoPackage output", initial, "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.gpkg_edit.setText(path)

    def _add_export_row(self, export: Optional[ExportRef] = None):
        row = ExportEditorRow(export, self.exports_container)
        row.remove_requested.connect(self._remove_export_row)
        self._export_rows.append(row)
        self.exports_layout.addWidget(row)

    def _remove_export_row(self, row):
        if row not in self._export_rows:
            return
        self._export_rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _add_delivery_row(self, delivery: Optional[DeliveryRef] = None):
        row = DeliveryEditorRow(delivery, self.deliveries_container)
        row.remove_requested.connect(self._remove_delivery_row)
        self._delivery_rows.append(row)
        self.deliveries_layout.addWidget(row)

    def _remove_delivery_row(self, row):
        if row in self._delivery_rows:
            self._delivery_rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def _layer_ref(
        self,
        combo: QComboBox,
        role: str,
        fallback_name: str = "",
        existing: Optional[LayerRef] = None,
    ) -> LayerRef:
        current_qgis_id = combo.currentData()
        layer = self.project.mapLayer(current_qgis_id) if current_qgis_id else None

        if role == SOURCE_ROLE and existing is not None:
            # Generic edit preserves immutable identity and the already confirmed
            # binding. Source combos are disabled, so recovery cannot be bypassed.
            return LayerRef(
                layer_id=existing.stable_id,
                name=layer.name() if layer is not None else existing.name,
                role=role,
                source_uri=layer.source() if layer is not None else existing.source_uri,
                provider=layer.providerType() if layer is not None else existing.provider,
                binding_id=existing.current_layer_id or None,
            )

        if layer is not None:
            return LayerRef(
                layer_id=layer.id(),
                name=layer.name(),
                role=role,
                source_uri=layer.source(),
                provider=layer.providerType(),
                binding_id=layer.id() if role == SOURCE_ROLE else None,
            )

        previous = self._existing_refs.get(current_qgis_id)
        if previous is not None:
            return LayerRef(
                layer_id=previous.layer_id,
                name=previous.name,
                role=role,
                source_uri=previous.source_uri,
                provider=previous.provider,
                binding_id=previous.binding_id,
            )

        return LayerRef(layer_id=None, name=fallback_name.strip(), role=role)

    def definition(self) -> WorkflowDefinition:
        existing_sources = self.workflow.source_layers if self.workflow else []
        source_a = self._layer_ref(
            self.source_a_combo,
            SOURCE_ROLE,
            existing=existing_sources[0] if len(existing_sources) > 0 else None,
        )
        source_b = self._layer_ref(
            self.source_b_combo,
            SOURCE_ROLE,
            existing=existing_sources[1] if len(existing_sources) > 1 else None,
        )
        derived = self._layer_ref(
            self.derived_combo,
            DERIVED_ROLE,
            self.derived_name_edit.text(),
        )
        if derived.layer_id is None:
            derived.name = self.derived_name_edit.text().strip()

        workflow_id = self.workflow.workflow_id if self.workflow else str(uuid4())
        exports = [row.export_ref(derived.layer_id) for row in self._export_rows]
        deliveries = [row.delivery_ref() for row in self._delivery_rows]
        mapping = FieldMapping(
            left_layer_id=source_a.stable_id,
            left_field=str(self.source_a_field_combo.currentData() or "").strip(),
            right_layer_id=source_b.stable_id,
            right_field=str(self.source_b_field_combo.currentData() or "").strip(),
            relationship_type=SAME_BUSINESS_KEY,
            confirmed=self.mapping_confirm.isChecked(),
        )

        return WorkflowDefinition(
            workflow_id=workflow_id,
            name=self.name_edit.text().strip(),
            schema_version=max(self.workflow.schema_version if self.workflow else 1, 5),
            layers=[source_a, source_b, derived],
            gpkg_path=self.gpkg_edit.text().strip(),
            exports=exports,
            forward_deliveries=deliveries,
            field_mappings=[mapping],
        )
