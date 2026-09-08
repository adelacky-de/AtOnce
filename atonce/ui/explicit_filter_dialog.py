"""Small production editor for one explicit FILTER graph slice."""

from uuid import uuid4

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ..models.dependency_graph import (
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    NodeKind,
    OperationKind,
)
from ..models.workflow import EXPLICIT_GRAPH_ORIGIN, WorkflowDefinition
from .guided_workflow_builder import GuidedFilterBuilder


class ExplicitFilterDialog(QDialog):
    """Define one AtOnce-owned derived node from an existing registered source."""

    def __init__(self, source_workflow, parent=None, qgis_gateway=None):
        super().__init__(parent)
        self.source_workflow = source_workflow
        self.qgis = qgis_gateway
        self.setWindowTitle("Add filtered derived layer")

        layout = QVBoxLayout(self)
        note = QLabel(
            "AtOnce will create and manage the derived layer when this explicit FILTER graph runs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.source_combo = QComboBox()
        for source in source_workflow.source_layers:
            self.source_combo.addItem(source.name, source.stable_id)
        self.name_edit = QLineEdit("Filtered layer")
        self.filter_builder = GuidedFilterBuilder(parent=self)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self._source_changed(self.source_combo.currentIndex())
        form.addRow("Source", self.source_combo)
        form.addRow("Derived name", self.name_edit)
        form.addRow("Filter", self.filter_builder)
        layout.addLayout(form)

        self.delivery_checks = []
        for delivery in source_workflow.forward_deliveries:
            check = QCheckBox(delivery.name or delivery.format)
            check.setChecked(delivery.enabled_by_default)
            self.delivery_checks.append((delivery, check))
            layout.addWidget(check)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _source_changed(self, _index):
        if self.qgis is None:
            return
        source = self.source_workflow.source_by_lineage_id(str(self.source_combo.currentData() or ""))
        layer = self.qgis.resolve_layer(source.current_layer_id) if source is not None else None
        self.filter_builder.set_layer(layer)

    def _accept(self):
        valid, error = self.filter_builder.validate()
        if not valid:
            QMessageBox.warning(self, "Filter needs attention", error)
            return
        self.accept()

    def definition(self) -> WorkflowDefinition:
        source_lineage_id = str(self.source_combo.currentData() or "")
        source = self.source_workflow.source_by_lineage_id(source_lineage_id)
        derived_node_id = f"derived:filter:{uuid4()}"
        source_node_id = f"source:{source_lineage_id}"
        derived_name = self.name_edit.text().strip() or "Filtered layer"
        graph_nodes = [
            DependencyNode(
                source_node_id,
                source.name,
                NodeKind.SOURCE,
                metadata={"source_lineage_id": source_lineage_id},
            ),
            DependencyNode(derived_node_id, derived_name, NodeKind.DERIVED),
        ]
        graph_edges = [
            DependencyEdge(
                f"edge:{source_node_id}->{derived_node_id}",
                source_node_id,
                derived_node_id,
                OperationKind.FILTER,
                parameters={"expression": self.filter_builder.expression()},
            )
        ]
        deliveries = []
        for delivery, check in self.delivery_checks:
            configured = type(delivery).from_dict(delivery.to_dict())
            configured.enabled_by_default = check.isChecked()
            deliveries.append(configured)
            delivery_node_id = f"delivery:forward:{delivery.delivery_id}"
            graph_nodes.append(
                DependencyNode(
                    delivery_node_id,
                    delivery.name,
                    NodeKind.DELIVERY,
                    format=delivery.format,
                    metadata={"delivery_id": delivery.delivery_id},
                )
            )
            graph_edges.append(
                DependencyEdge(
                    f"edge:{derived_node_id}->{delivery_node_id}",
                    derived_node_id,
                    delivery_node_id,
                    OperationKind.EXPORT,
                    enabled_by_default=check.isChecked(),
                )
            )
        return WorkflowDefinition(
            workflow_id=str(uuid4()),
            name=f"{self.source_workflow.name} · {derived_name}",
            layers=[type(source).from_dict(source.to_dict())],
            forward_deliveries=deliveries,
            dependency_graph=DependencyGraph(graph_nodes, graph_edges),
            dependency_graph_origin=EXPLICIT_GRAPH_ORIGIN,
        )
