"""Small editor for extending one explicit FILTER graph by one stage."""

from uuid import uuid4

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..core.graph_execution import GraphExecutionError, plan_explicit_graph_execution
from ..models.dependency_graph import DependencyEdge, DependencyGraph, DependencyNode, NodeKind, OperationKind
from ..models.workflow import EXPLICIT_GRAPH_ORIGIN, WorkflowDefinition
from .guided_workflow_builder import GuidedFilterBuilder


class ExplicitChainFilterDialog(QDialog):
    """Add exactly one FILTER stage to an eligible explicit G7a workflow."""

    def __init__(self, source_workflow, parent=None, qgis_gateway=None):
        super().__init__(parent)
        self.source_workflow = source_workflow
        self.qgis = qgis_gateway
        self.setWindowTitle("Add second filtered derived layer")

        layout = QVBoxLayout(self)
        note = QLabel(
            "The selected AtOnce-managed FILTER result becomes the parent. "
            "AtOnce will create and manage the second derived layer when the chain runs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.parent_combo = QComboBox()
        graph = source_workflow.effective_dependency_graph()
        for node in graph.nodes:
            if node.kind == NodeKind.DERIVED:
                self.parent_combo.addItem(node.name, node.node_id)
        self.name_edit = QLineEdit("Second filtered layer")
        source_layer = None
        if self.qgis is not None and source_workflow.source_layers:
            source_layer = self.qgis.resolve_layer(source_workflow.source_layers[0].current_layer_id)
        self.filter_builder = GuidedFilterBuilder(source_layer, parent=self)
        form.addRow("Parent", self.parent_combo)
        form.addRow("Derived name", self.name_edit)
        form.addRow("Filter", self.filter_builder)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        valid, error = self.filter_builder.validate()
        if not valid:
            from qgis.PyQt.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Filter needs attention", error)
            return
        self.accept()

    @staticmethod
    def eligible(workflow):
        if workflow.effective_dependency_graph_origin() != EXPLICIT_GRAPH_ORIGIN:
            return False
        source_ids = [source.stable_id for source in workflow.source_layers if source.stable_id]
        if len(source_ids) != 1:
            return False
        try:
            plan = plan_explicit_graph_execution(workflow, {source_ids[0]})
        except GraphExecutionError:
            return False
        return len(plan.transform_steps) == 1 and plan.operation == OperationKind.FILTER

    def definition(self) -> WorkflowDefinition:
        definition = WorkflowDefinition.from_dict(self.source_workflow.to_dict())
        graph = definition.effective_dependency_graph()
        parent_node_id = str(self.parent_combo.currentData() or "")
        derived_node_id = f"derived:filter:{uuid4()}"
        derived_name = self.name_edit.text().strip() or "Second filtered layer"

        nodes = list(graph.nodes)
        nodes.append(DependencyNode(derived_node_id, derived_name, NodeKind.DERIVED))
        edges = []
        for edge in graph.edges:
            if edge.from_node == parent_node_id:
                target = graph.node_map()[edge.to_node]
                if target.kind == NodeKind.DELIVERY and edge.operation == OperationKind.EXPORT:
                    edges.append(
                        DependencyEdge(
                            edge.edge_id,
                            derived_node_id,
                            edge.to_node,
                            edge.operation,
                            edge.enabled_by_default,
                            dict(edge.parameters),
                        )
                    )
                    continue
            edges.append(edge)
        edges.append(
            DependencyEdge(
                f"edge:{parent_node_id}->{derived_node_id}",
                parent_node_id,
                derived_node_id,
                OperationKind.FILTER,
                parameters={"expression": self.filter_builder.expression()},
            )
        )
        definition.dependency_graph = DependencyGraph(nodes, edges)
        definition.name = f"{definition.name} · {derived_name}"
        return definition
