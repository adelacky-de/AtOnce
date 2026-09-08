"""Small production editor for one explicit two-parent MERGE graph slice."""

from uuid import uuid4

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QFormLayout,
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


class ExplicitMergeDialog(QDialog):
    """Define one AtOnce-owned derived node by appending two sources."""

    def __init__(self, source_workflow, parent=None):
        super().__init__(parent)
        self.source_workflow = source_workflow
        self.setWindowTitle("Add merged derived layer")

        layout = QVBoxLayout(self)
        note = QLabel(
            "AtOnce will append the two registered sources and manage the derived layer "
            "when this explicit MERGE graph runs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.source_a_combo = QComboBox()
        self.source_b_combo = QComboBox()
        for source in source_workflow.source_layers:
            self.source_a_combo.addItem(source.name, source.stable_id)
            self.source_b_combo.addItem(source.name, source.stable_id)
        if self.source_b_combo.count() > 1:
            self.source_b_combo.setCurrentIndex(1)
        self.name_edit = QLineEdit("Merged layer")
        form.addRow("Source A", self.source_a_combo)
        form.addRow("Source B", self.source_b_combo)
        form.addRow("Derived name", self.name_edit)
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

    def _accept(self):
        if self.source_a_combo.currentData() == self.source_b_combo.currentData():
            QMessageBox.warning(self, "Cannot merge source", "Source A and Source B must be different.")
            return
        super().accept()

    def definition(self) -> WorkflowDefinition:
        source_a_id = str(self.source_a_combo.currentData() or "")
        source_b_id = str(self.source_b_combo.currentData() or "")
        source_a = self.source_workflow.source_by_lineage_id(source_a_id)
        source_b = self.source_workflow.source_by_lineage_id(source_b_id)
        derived_node_id = f"derived:merge:{uuid4()}"
        derived_name = self.name_edit.text().strip() or "Merged layer"
        graph_nodes = [
            DependencyNode(
                f"source:{source_a_id}",
                source_a.name,
                NodeKind.SOURCE,
                metadata={"source_lineage_id": source_a_id},
            ),
            DependencyNode(
                f"source:{source_b_id}",
                source_b.name,
                NodeKind.SOURCE,
                metadata={"source_lineage_id": source_b_id},
            ),
            DependencyNode(derived_node_id, derived_name, NodeKind.DERIVED),
        ]
        graph_edges = [
            DependencyEdge(
                f"edge:source:{source_a_id}->{derived_node_id}",
                f"source:{source_a_id}",
                derived_node_id,
                OperationKind.MERGE,
            ),
            DependencyEdge(
                f"edge:source:{source_b_id}->{derived_node_id}",
                f"source:{source_b_id}",
                derived_node_id,
                OperationKind.MERGE,
            ),
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
            layers=[
                type(source_a).from_dict(source_a.to_dict()),
                type(source_b).from_dict(source_b.to_dict()),
            ],
            forward_deliveries=deliveries,
            dependency_graph=DependencyGraph(graph_nodes, graph_edges),
            dependency_graph_origin=EXPLICIT_GRAPH_ORIGIN,
        )
