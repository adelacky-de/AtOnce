"""Configuration-only creation dialog for standalone explicit workflows."""

from uuid import uuid4

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.explicit_workflow_creation import (
    build_standalone_filter_workflow,
    build_standalone_merge_workflow,
)
from ..models.workflow import DeliveryRef
from .styles import DIALOG_STYLESHEET
from .guided_workflow_builder import GuidedFilterBuilder, GuidedWorkflowBuilder
from .workflow_dialog import DeliveryEditorRow


class StandaloneExplicitWorkflowDialog(QDialog):
    """Create a FILTER or MERGE workflow without a parent workflow."""

    def __init__(self, qgis_gateway, operation, parent=None):
        super().__init__(parent)
        self.qgis = qgis_gateway
        self.operation = str(operation).lower()
        self.delivery_rows = []
        self.layers = list(qgis_gateway.vector_layers())
        self.pipeline = GuidedWorkflowBuilder(
            qgis_gateway,
            self.operation,
            editable=True,
            parent=self,
        )
        self.setWindowTitle(
            "Create standalone FILTER workflow"
            if self.operation == "filter"
            else "Create standalone MERGE workflow"
        )
        self.resize(700, 760)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 15, 16, 15)
        root.setSpacing(10)
        title = QLabel(
            "Create FILTER workflow"
            if self.operation == "filter"
            else "Create MERGE workflow"
        )
        title.setObjectName("AtOnceDialogTitle")
        root.addWidget(title)
        note = QLabel(
            "This saves configuration only. Run the active workflow afterward to prepare source identity and execute it."
        )
        note.setObjectName("AtOnceDialogSubtitle")
        note.setWordWrap(True)
        root.addWidget(note)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 2, 0, 2)
        content_layout.setSpacing(10)

        basics = self._section_card("Basics")
        basics_form = QFormLayout()
        self.name_edit = QLineEdit(
            "Filtered workflow" if self.operation == "filter" else "Merged workflow"
        )
        self.derived_name_edit = QLineEdit(
            "Filtered layer" if self.operation == "filter" else "Merged layer"
        )
        basics_form.addRow("Workflow name", self.name_edit)
        basics_form.addRow("Derived name", self.derived_name_edit)
        basics.layout().addLayout(basics_form)
        content_layout.addWidget(basics)

        sources = self._section_card("Guided workflow")
        source_note = QLabel(
            "Drop a loaded vector layer into each source slot, or choose one from the current QGIS project. "
            "Save only records configuration; Run prepares source identity and executes the graph."
        )
        source_note.setObjectName("AtOnceMuted")
        source_note.setWordWrap(True)
        sources.layout().addWidget(source_note)
        sources.layout().addWidget(self.pipeline)
        content_layout.addWidget(sources)

        if self.operation == "filter":
            transform = self._section_card("FILTER")
            transform_note = QLabel(
                "Choose a field, condition and value. Advanced expression mode remains available for supported QGIS expressions."
            )
            transform_note.setObjectName("AtOnceMuted")
            transform_note.setWordWrap(True)
            self.filter_builder = GuidedFilterBuilder(parent=self)
            self.pipeline.source_changed.connect(
                lambda index, layer: self.filter_builder.set_layer(layer)
                if index == 0
                else None
            )
            transform.layout().addWidget(transform_note)
            transform.layout().addWidget(self.filter_builder)
            content_layout.addWidget(transform)

        deliveries = self._section_card("Forward deliveries")
        delivery_note = QLabel(
            "Add zero or more GeoJSON, Shapefile, KML, KMZ or GeoPackage deliveries. Files are untouched until Run."
        )
        delivery_note.setObjectName("AtOnceMuted")
        delivery_note.setWordWrap(True)
        deliveries.layout().addWidget(delivery_note)
        self.deliveries_layout = QVBoxLayout()
        self.deliveries_layout.setContentsMargins(0, 0, 0, 0)
        self.deliveries_layout.setSpacing(8)
        deliveries.layout().addLayout(self.deliveries_layout)
        buttons_row = QHBoxLayout()
        for label, format_name in (
            ("+ GeoJSON", "geojson"),
            ("+ Shapefile", "shapefile"),
            ("+ KML", "kml"),
            ("+ KMZ", "kmz"),
            ("+ GeoPackage", "gpkg"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, value=format_name: self._add_delivery(value)
            )
            buttons_row.addWidget(button)
        deliveries.layout().addLayout(buttons_row)
        content_layout.addWidget(deliveries)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        dialog_buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save = dialog_buttons.button(QDialogButtonBox.Save)
        if save is not None:
            save.setObjectName("AtOncePrimary")
            save.setText("Save workflow")
        dialog_buttons.accepted.connect(self._accept)
        dialog_buttons.rejected.connect(self.reject)
        root.addWidget(dialog_buttons)

    def _add_delivery(self, format_name):
        defaults = {
            "geojson": ("GeoJSON", ".geojson"),
            "shapefile": ("Shapefile", ".shp"),
            "kml": ("KML", ".kml"),
            "kmz": ("KMZ", ".kmz"),
            "gpkg": ("GeoPackage", ".gpkg"),
        }
        name, suffix = defaults[format_name]
        row = DeliveryEditorRow(
            DeliveryRef(str(uuid4()), name, format_name, f"{name.lower()}{suffix}"),
            self,
        )
        self.delivery_rows.append(row)
        self.deliveries_layout.addWidget(row)

    def _accept(self):
        source_a_layer = self.pipeline.source_layer(0)
        if source_a_layer is None:
            QMessageBox.warning(self, "Source required", "Choose a loaded vector source.")
            return
        if self.operation == "filter":
            valid, error = self.filter_builder.validate()
            if not valid:
                QMessageBox.warning(self, "Filter needs attention", error)
                return
        else:
            source_b_layer = self.pipeline.source_layer(1)
            if source_b_layer is None:
                QMessageBox.warning(self, "Source required", "Choose two loaded vector sources for MERGE.")
                return
            if source_a_layer.id() == source_b_layer.id():
                QMessageBox.warning(
                    self,
                    "Distinct sources required",
                    "Choose two different loaded vector layers for MERGE.",
                )
                return
            try:
                self.qgis.preflight_merge_layers([source_a_layer, source_b_layer])
            except ValueError as exc:
                QMessageBox.warning(self, "MERGE sources are not compatible", str(exc))
                return
        super().accept()

    def definition(self):
        source_a_layer = self.pipeline.source_layer(0)
        source_a = self.qgis.make_source_layer_ref(source_a_layer)
        deliveries = [row.delivery_ref() for row in self.delivery_rows]
        if self.operation == "filter":
            return build_standalone_filter_workflow(
                name=self.name_edit.text(),
                source=source_a,
                derived_name=self.derived_name_edit.text(),
                expression=self.filter_builder.expression(),
                deliveries=deliveries,
            )
        source_b_layer = self.pipeline.source_layer(1)
        source_b = self.qgis.make_source_layer_ref(source_b_layer)
        return build_standalone_merge_workflow(
            name=self.name_edit.text(),
            sources=(source_a, source_b),
            derived_name=self.derived_name_edit.text(),
            deliveries=deliveries,
        )

    @staticmethod
    def _section_card(title):
        card = QFrame()
        card.setObjectName("AtOnceDialogCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("AtOnceSectionTitle")
        layout.addWidget(heading)
        return card
