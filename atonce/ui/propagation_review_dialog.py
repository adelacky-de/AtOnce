"""Native review dialog for one run-scoped selective propagation choice."""

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from ..core.legacy_graph_execution import SelectiveExecutionError, plan_legacy_execution
from ..core.graph_execution import GraphExecutionError, plan_explicit_graph_execution
from ..core.freeform_graph import FreeformGraphError, plan_freeform_graph_execution
from .styles import DIALOG_STYLESHEET
from .widgets.dependency_graph import DependencyGraphView


class PropagationReviewDialog(QDialog):
    """Let the user tick dependency edges for this run without changing defaults."""

    def __init__(self, workflow, changed_source_lineage_ids, parent=None):
        super().__init__(parent)
        self.workflow = workflow
        self.changed_source_lineage_ids = {
            str(item) for item in changed_source_lineage_ids if str(item)
        }
        self.graph_model = workflow.effective_dependency_graph()

        self.setWindowTitle("Choose downstream propagation")
        self.resize(700, 560)
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 15, 16, 15)
        layout.setSpacing(10)

        title = QLabel("Choose what should follow this source change")
        title.setObjectName("AtOnceDialogTitle")
        note = QLabel(
            "Tick dependency edges to include them in this run. Unticked branches are not "
            "overwritten; AtOnce records them as stale by choice. This does not change the "
            "workflow's saved default edge settings."
        )
        note.setWordWrap(True)
        note.setObjectName("AtOnceMuted")
        layout.addWidget(title)
        layout.addWidget(note)

        self.graph = DependencyGraphView(self)
        self.graph.setMinimumHeight(340)
        layout.addWidget(self.graph, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("AtOnceMuted")
        layout.addWidget(self.summary)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.Ok)
        if self.apply_button is not None:
            self.apply_button.setText("Apply selected propagation")
            self.apply_button.setObjectName("AtOncePrimary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        changed_node_ids = {
            node.node_id
            for node in self.graph_model.nodes
            if str(node.metadata.get("source_lineage_id") or "")
            in self.changed_source_lineage_ids
        }
        self.graph.render_dependency_graph(
            self.graph_model,
            changed_node_ids=changed_node_ids,
        )
        self.graph.edge_selection_changed.connect(
            lambda _edge_id, _checked: self._refresh_summary()
        )
        self._refresh_summary()

    def selected_edge_ids(self):
        return self.graph.selected_edge_ids()

    def _refresh_summary(self):
        try:
            if self.workflow.effective_dependency_graph_origin() == "explicit":
                execution = plan_explicit_graph_execution(
                    self.workflow,
                    self.changed_source_lineage_ids,
                    self.selected_edge_ids(),
                )
                stale = len(execution.propagation.stale_by_choice_node_ids)
                selected = len(execution.propagation.selected_edge_ids)
                outputs = len(execution.delivery_ids)
                derived_ids = {
                    node.node_id
                    for node in self.graph_model.nodes
                    if node.kind.value == "derived"
                }
                derived_count = len(derived_ids & set(execution.refreshed_node_ids))
                derived = f"{derived_count} derived step(s) will refresh"
            elif self.workflow.effective_dependency_graph_origin() == "freeform_explicit":
                changed_node_ids = {
                    node.node_id
                    for node in self.graph_model.nodes
                    if str(node.metadata.get("source_lineage_id") or "")
                    in self.changed_source_lineage_ids
                }
                execution = plan_freeform_graph_execution(
                    self.graph_model,
                    changed_node_ids,
                    self.selected_edge_ids(),
                )
                stale = len(execution.propagation.stale_by_choice_node_ids)
                selected = len(execution.propagation.selected_edge_ids)
                outputs = len(
                    [
                        edge
                        for edge in self.graph_model.edges
                        if edge.edge_id in execution.propagation.selected_edge_ids
                        and self.graph_model.node_map()[edge.to_node].kind.value == "delivery"
                    ]
                )
                derived_count = len(execution.transform_steps)
                derived = f"{derived_count} derived step(s) will refresh"
            else:
                execution = plan_legacy_execution(
                    self.workflow,
                    self.changed_source_lineage_ids,
                    self.selected_edge_ids(),
                )
                stale = len(execution.propagation.stale_by_choice_node_ids)
                selected = len(execution.propagation.selected_edge_ids)
                outputs = int(execution.refresh_gpkg) + len(execution.export_ids)
                derived = "Layer C will refresh" if execution.refresh_layer_c else "Layer C stays unchanged"
        except (SelectiveExecutionError, GraphExecutionError, FreeformGraphError) as exc:
            self.summary.setText(f"Cannot apply this selection safely: {exc}")
            if self.apply_button is not None:
                self.apply_button.setEnabled(False)
            return

        self.summary.setText(
            f"{selected} edge(s) selected · {outputs} delivery output(s) will refresh · "
            f"{stale} node(s) remain stale by choice · {derived}."
        )
        if self.apply_button is not None:
            self.apply_button.setEnabled(True)
