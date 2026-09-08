"""Configuration-only editor for existing explicit workflows."""

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.explicit_workflow_editing import apply_explicit_workflow_edits
from ..models.dependency_graph import NodeKind, OperationKind
from .styles import DIALOG_STYLESHEET
from .guided_workflow_builder import GuidedFilterBuilder
from .workflow_dialog import DeliveryEditorRow


class ExplicitWorkflowEditorDialog(QDialog):
    """Edit existing explicit configuration without changing graph topology."""

    def __init__(self, workflow, type_label=None, parent=None, qgis_gateway=None):
        super().__init__(parent)
        self.workflow = workflow
        self.qgis = qgis_gateway
        self.filter_edits = {}
        self.filter_builders = {}
        self.delivery_rows = []
        self.setWindowTitle("Edit explicit workflow")
        self.resize(680, 760)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 15, 16, 15)
        root.setSpacing(10)
        title = QLabel("Edit workflow")
        title.setObjectName("AtOnceDialogTitle")
        root.addWidget(title)
        note = QLabel(
            "Saving changes configuration only. Run the active workflow explicitly to rebuild derived layers or deliveries."
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
        self.name_edit = QLineEdit(workflow.name)
        basics_form.addRow("Workflow name", self.name_edit)
        type_value = QLabel(type_label or self._type_label(workflow))
        type_value.setObjectName("AtOnceMuted")
        basics_form.addRow("Workflow type", type_value)
        basics.layout().addLayout(basics_form)
        content_layout.addWidget(basics)

        sources_card = self._section_card("Sources")
        source_note = QLabel("Source identity and current bindings are read-only here.")
        source_note.setObjectName("AtOnceMuted")
        source_note.setWordWrap(True)
        sources_card.layout().addWidget(source_note)
        source_form = QFormLayout()
        for index, source in enumerate(workflow.source_layers, start=1):
            binding = "bound" if source.current_layer_id else "binding missing"
            value = QLineEdit(f"{source.name} ({binding})")
            value.setReadOnly(True)
            source_form.addRow(f"Source {index}", value)
        sources_card.layout().addLayout(source_form)
        content_layout.addWidget(sources_card)

        transform_card = self._section_card("Transform steps")
        transform_note = QLabel(
            "Existing topology is fixed. Edit FILTER expressions with the guided field/condition/value builder or advanced QGIS mode."
        )
        transform_note.setObjectName("AtOnceMuted")
        transform_note.setWordWrap(True)
        transform_card.layout().addWidget(transform_note)
        graph = workflow.effective_dependency_graph()
        node_map = graph.node_map()
        transform_form = QFormLayout()
        merge_targets = set()
        for edge in graph.edges:
            target = node_map.get(edge.to_node)
            if target is None or target.kind != NodeKind.DERIVED:
                continue
            parent = node_map.get(edge.from_node)
            if edge.operation == OperationKind.MERGE:
                merge_targets.add(edge.to_node)
                continue
            if edge.operation != OperationKind.FILTER:
                continue
            label = f"{parent.name if parent else edge.from_node} → {target.name}"
            layer = self._filter_context_layer(edge.from_node, node_map)
            expression = GuidedFilterBuilder(
                layer,
                str(edge.parameters.get("expression") or ""),
                self,
            )
            self.filter_builders[edge.edge_id] = expression
            transform_form.addRow(label, expression)
        for target_id in sorted(merge_targets):
            target = node_map[target_id]
            parents = [
                node_map[edge.from_node].name
                for edge in graph.edges
                if edge.to_node == target_id and edge.operation == OperationKind.MERGE
            ]
            transform_form.addRow(
                "MERGE",
                QLabel(f"{' + '.join(parents)} → {target.name} · append"),
            )
        transform_card.layout().addLayout(transform_form)
        content_layout.addWidget(transform_card)

        deliveries_card = self._section_card("Forward deliveries")
        deliveries_note = QLabel(
            "Edit existing delivery settings. Delivery topology and identities are preserved."
        )
        deliveries_note.setObjectName("AtOnceMuted")
        deliveries_note.setWordWrap(True)
        deliveries_card.layout().addWidget(deliveries_note)
        for delivery in workflow.forward_deliveries:
            row = DeliveryEditorRow(delivery, deliveries_card)
            for button in row.findChildren(QPushButton):
                if button.text() == "Remove":
                    button.hide()
            self.delivery_rows.append(row)
            deliveries_card.layout().addWidget(row)
        content_layout.addWidget(deliveries_card)

        safety = QFrame()
        safety.setObjectName("AtOnceSafetyNote")
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(11, 9, 11, 9)
        safety_title = QLabel("Configuration only")
        safety_title.setObjectName("AtOnceSectionTitle")
        safety_note = QLabel(
            "Save does not build candidates, replace layers, touch delivery files, or create run evidence."
        )
        safety_note.setObjectName("AtOnceMuted")
        safety_note.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_note)
        content_layout.addWidget(safety)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save = buttons.button(QDialogButtonBox.Save)
        if save is not None:
            save.setObjectName("AtOncePrimary")
            save.setText("Save workflow")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def definition(self):
        return apply_explicit_workflow_edits(
            self.workflow,
            name=self.name_edit.text(),
            filter_expressions={
                edge_id: editor.expression()
                for edge_id, editor in self.filter_builders.items()
            },
            deliveries=[row.delivery_ref() for row in self.delivery_rows],
        )

    def _filter_context_layer(self, parent_node_id, node_map):
        if self.qgis is None:
            return None
        parent = node_map.get(parent_node_id)
        if parent is not None and parent.kind == NodeKind.SOURCE:
            lineage = parent.metadata.get("source_lineage_id", "")
            source = self.workflow.source_by_lineage_id(lineage)
            if source is not None:
                return self.qgis.resolve_layer(source.current_layer_id)
        # FILTER-chain candidates preserve the source attribute schema.  The
        # registered source is therefore a safe field-context fallback while
        # the editor remains configuration-only and does not materialize data.
        if self.workflow.source_layers:
            return self.qgis.resolve_layer(self.workflow.source_layers[0].current_layer_id)
        return None

    def _accept(self):
        for builder in self.filter_builders.values():
            valid, error = builder.validate()
            if not valid:
                QMessageBox.warning(self, "Filter needs attention", error)
                return
        self.accept()

    @staticmethod
    def _type_label(workflow):
        graph = workflow.effective_dependency_graph()
        transforms = [
            edge
            for edge in graph.edges
            if graph.node_map().get(edge.to_node)
            and graph.node_map()[edge.to_node].kind == NodeKind.DERIVED
        ]
        if any(edge.operation == OperationKind.MERGE for edge in transforms):
            return "MERGE"
        if len({edge.to_node for edge in transforms}) == 2:
            return "FILTER chain"
        return "FILTER"

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
