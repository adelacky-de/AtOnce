"""Final UI polish for the block-first canvas.

Kept separate from the topology implementation so the release-critical executor
and graph construction stay easy to audit.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QLabel, QPushButton

from .block_workflow_canvas import BlockWorkflowCanvas


class FinalBlockWorkflowCanvas(BlockWorkflowCanvas):
    """Add explicit guidance plus durable downstream-stale visual evidence."""

    source_relink_index_requested = pyqtSignal(int)

    _STALE_TOOLTIP = (
        "Upstream changed. Rerun this branch to update the downstream result."
    )
    _STALE_OUTPUT_TOOLTIP = (
        "Upstream changed. Rerun this branch to update the downstream file."
    )

    def __init__(self, gateway=None, parent=None):
        self._pending_output_index = None
        self._stale_node_ids = set()
        self._refreshed_node_ids = set()
        self._delivery_node_ids = {}
        super().__init__(gateway, parent)
        self.filter_palette_button.setStyleSheet(
            "QPushButton { color:#23323A; background:#EDF4F7; "
            "border:1px solid #AFC8D4; border-radius:8px; font-weight:700; padding:8px; }"
        )
        self.merge_palette_button.setStyleSheet(
            "QPushButton { color:#23323A; background:#FAF5EA; "
            "border:1px solid #D8BF8E; border-radius:8px; font-weight:700; padding:8px; }"
        )
        self.operation_drop.setStyleSheet(
            "QPushButton { color:#61727C; background:#F8F9F7; "
            "border:1px dashed #D9E1E5; border-radius:9px; padding:9px; font-weight:650; }"
        )
        for button in self.output_editor.findChildren(QPushButton):
            if button.text() == "Cancel":
                button.clicked.connect(self._cancel_output_settings)
        self._prepare_filter_guidance()
        self._decorate_stale_nodes()

    def _render_operations(self):
        super()._render_operations()
        self._prepare_filter_guidance()
        self._decorate_operation_badges()

    def _render_deliveries(self):
        super()._render_deliveries()
        self._decorate_delivery_badges()

    @staticmethod
    def _stale_badge(parent, tooltip):
        """Create the compact red ! ball shown on a stale downstream node."""

        badge = QLabel("!", parent)
        badge.setObjectName("AtOnceCanvasStaleBadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(20, 20)
        badge.setToolTip(tooltip)
        badge.setAccessibleName("Upstream changed")
        badge.setAccessibleDescription(tooltip)
        badge.setStyleSheet(
            "QLabel#AtOnceCanvasStaleBadge {"
            " color:#FFFFFF; background:#D92D20; border:1px solid #B42318;"
            " border-radius:10px; font-weight:800; font-size:12px; }"
        )
        # Keep the warning visually attached to the top-left corner of the node.
        # It is deliberately an overlay, not part of the button text/layout.
        badge.move(5, 5)
        badge.raise_()
        return badge

    @staticmethod
    def _operation_blocks(host):
        return [
            button
            for button in host.findChildren(QPushButton)
            if button.objectName() == "AtOnceCanvasOperationBlock"
        ]

    @staticmethod
    def _output_blocks(host):
        return [
            button
            for button in host.findChildren(QPushButton)
            if button.objectName() == "AtOnceCanvasOutputBlock"
        ]

    def _clear_stale_badges(self, parent):
        for badge in parent.findChildren(QLabel, "AtOnceCanvasStaleBadge"):
            badge.deleteLater()

    def _decorate_operation_badges(self):
        self._clear_stale_badges(self.operations_host)
        blocks = self._operation_blocks(self.operations_host)
        for operation, block in zip(self._operations, blocks):
            node_id = str(operation.get("result_node_id") or "")
            if node_id not in self._stale_node_ids:
                continue
            self._stale_badge(block, self._STALE_TOOLTIP)
            block.setToolTip(self._STALE_TOOLTIP)

    def _delivery_node_id(self, delivery):
        delivery_id = str(getattr(delivery, "delivery_id", "") or "")
        return self._delivery_node_ids.get(
            delivery_id,
            f"delivery:forward:{delivery_id}" if delivery_id else "",
        )

    def _decorate_delivery_badges(self):
        self._clear_stale_badges(self.deliveries_host)
        blocks = self._output_blocks(self.deliveries_host)
        for delivery, block in zip(self._deliveries, blocks):
            node_id = self._delivery_node_id(delivery)
            if node_id not in self._stale_node_ids:
                continue
            self._stale_badge(block, self._STALE_OUTPUT_TOOLTIP)
            block.setToolTip(self._STALE_OUTPUT_TOOLTIP)

    def _decorate_stale_nodes(self):
        self._decorate_operation_badges()
        self._decorate_delivery_badges()

    def _prepare_filter_guidance(self):
        """Start new FILTER blocks at an explicit light-grey field prompt."""

        for index, builder in self._filter_builders.items():
            combo = builder.field_combo
            if hasattr(combo, "setToolTip"):
                combo.setToolTip("Choose a field…")
            if hasattr(combo, "setPlaceholderText"):
                combo.setPlaceholderText("Choose a field…")
            if hasattr(combo, "setAllowEmptyFieldName"):
                combo.setAllowEmptyFieldName(True)
            operation = self._operations[index] if index < len(self._operations) else {}
            if str(operation.get("expression") or "").strip():
                continue
            if hasattr(combo, "setField"):
                combo.setField("")
            elif hasattr(combo, "setCurrentIndex"):
                combo.setCurrentIndex(-1)
            builder.value_edit.setPlaceholderText("Enter a value…")

    def _add_operation(self, operation):
        before = len(self._operations)
        super()._add_operation(operation)
        if len(self._operations) <= before:
            return
        added = self._operations[-1]
        # New FILTER edges get their canonical edge:<parent>-><derived> ID only
        # when the graph is constructed, matching the existing G7c contract.
        if added.get("kind") == "filter" and added.get("new"):
            added["edge_id"] = ""
        # A newly added operation becomes the final result. Do not accidentally
        # rename FILTER #2 back to FILTER #1's old final name on persistence.
        self.result_name_edit.setText(self._operations[-1]["result_name"])
        self._prepare_filter_guidance()

    def _source_clicked(self, index):
        if self._workflow is not None:
            self.source_relink_index_requested.emit(int(index))
            return
        super()._source_clicked(index)

    def _add_output(self):
        before = len(self._deliveries)
        super()._add_output()
        if len(self._deliveries) <= before:
            return
        self._pending_output_index = len(self._deliveries) - 1
        # A delivery destination is durable data. Never invent a relative working-
        # directory path for the user; require an explicit destination.
        self._deliveries[self._pending_output_index].path = ""
        self.output_path.clear()
        self.output_path.setPlaceholderText("Choose / enter output path…")

    def _apply_output_settings(self):
        if not self.output_path.text().strip():
            self.message_requested.emit("Choose an output path before adding this output.")
            return
        super()._apply_output_settings()
        self._pending_output_index = None

    def _cancel_output_settings(self):
        index = self._pending_output_index
        self._pending_output_index = None
        if index is None or not 0 <= index < len(self._deliveries):
            return
        self._deliveries.pop(index)
        self._render_deliveries()

    def set_workflow(self, workflow):
        self._pending_output_index = None
        self._stale_node_ids = set()
        self._refreshed_node_ids = set()
        self._delivery_node_ids = {}
        super().set_workflow(workflow)
        if workflow is not None and workflow.effective_dependency_graph_origin() == "explicit":
            graph = workflow.effective_dependency_graph()
            self._delivery_node_ids = {
                str(node.metadata.get("delivery_id") or ""): node.node_id
                for node in graph.nodes
                if node.kind.value == "delivery"
                and str(node.metadata.get("delivery_id") or "")
            }
        self._prepare_filter_guidance()
        self._decorate_stale_nodes()

    def set_result_evidence(self, graph_state):
        """Render durable graph-run freshness on every downstream canvas node.

        A node remains visually stale until a later successful graph run records
        it as refreshed. Because graph state is loaded from ProjectStore on dock
        reload, the warning survives closing/reopening the project instead of
        being a transient UI-only flag.
        """

        self._stale_node_ids = set(
            getattr(graph_state, "stale_node_ids", ()) or ()
        )
        self._refreshed_node_ids = set(
            getattr(graph_state, "refreshed_node_ids", ()) or ()
        )
        # Refreshed evidence wins if an older/defensive state contains a node in
        # both sets. The latest successful refresh clears its red warning.
        self._stale_node_ids.difference_update(self._refreshed_node_ids)
        super().set_result_evidence(graph_state)
        self._decorate_stale_nodes()
