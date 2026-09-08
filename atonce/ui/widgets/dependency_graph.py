"""Porcelain-style dependency graph used in the Workflow overview.

The proven v0.1 ``render_workflow`` path remains intentionally unchanged in
meaning.  ``render_dependency_graph`` is the additive Issue #22 G3 surface for
arbitrary dependency DAGs: native Qt checkboxes live on edges, emit immutable
``edge_id`` selection events, and preview selective-propagation impact without
executing any refresh or source mutation.
"""

from qgis.PyQt.QtCore import QRectF, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QCheckBox, QGraphicsScene, QGraphicsView

from ...models.dependency_graph import (
    NodeKind,
    OperationKind,
    default_selected_edges,
    plan_propagation,
)
from ..styles import COLORS


class DependencyGraphView(QGraphicsView):
    """Render legacy v0.1 workflow graphs and selectable generic DAGs."""

    edge_selection_changed = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(205)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setStyleSheet("background: transparent; border: none;")

        self._edge_controls = {}
        self._selected_edge_ids = set()
        self._native_graph = None
        self._native_changed_node_ids = set()
        self._native_unsupported_edge_ids = set()
        self.render_empty()

    def _clear_scene(self):
        self._edge_controls.clear()
        self.scene().clear()

    def _clear_native_state(self):
        self._native_graph = None
        self._native_changed_node_ids = set()
        self._native_unsupported_edge_ids = set()
        self._selected_edge_ids = set()
        self._edge_controls.clear()

    def render_empty(self):
        self._clear_native_state()
        scene = self.scene()
        scene.clear()
        scene.setSceneRect(QRectF(0, 0, 620, 205))
        text = scene.addText("No workflow registered yet")
        text.setDefaultTextColor(QColor(COLORS["text_muted"]))
        rect = text.boundingRect()
        text.setPos((620 - rect.width()) / 2, (205 - rect.height()) / 2)
        self._fit()

    def render_workflow(self, workflow):
        """Render the proven v0.1 source -> derived -> GeoPackage/XLSX graph."""
        if workflow is None:
            self.render_empty()
            return
        if workflow.effective_dependency_graph_origin() in {
            "explicit",
            "freeform_explicit",
        }:
            self.render_dependency_graph(workflow.effective_dependency_graph())
            return

        # Legacy v0.1 display mode is deliberately non-interactive.  Generic
        # edge selection lives only in render_dependency_graph until a later
        # persistence/execution milestone explicitly adapts the v0.1 workflow.
        self._clear_native_state()
        scene = self.scene()
        scene.clear()
        scene.setSceneRect(QRectF(0, 0, 620, 245))

        sources = workflow.source_layers[:2]
        derived = workflow.derived_layer
        outputs = [("GeoPackage", "OUTPUT")]
        outputs.extend((export.name or "XLSX", "EXPORT") for export in workflow.exports)

        source_positions = self._spread_positions(len(sources), 620, 132, top=52)
        source_nodes = []
        for source, x in zip(sources, source_positions):
            source_nodes.append(
                self._node(
                    x,
                    16,
                    132,
                    48,
                    source.name,
                    "Source",
                    COLORS["source_bg"],
                    COLORS["source_border"],
                )
            )

        derived_name = derived.name if derived and derived.name else "Layer C"
        derived_node = self._node(
            244,
            98,
            132,
            48,
            derived_name,
            "Result",
            COLORS["derived_bg"],
            COLORS["derived_border"],
        )

        output_count = max(len(outputs), 1)
        output_width = 122 if output_count <= 4 else 104
        output_positions = self._spread_positions(output_count, 620, output_width, top=20)
        output_nodes = []
        for (label, role), x in zip(outputs, output_positions):
            output_nodes.append(
                self._node(
                    x,
                    180,
                    output_width,
                    46,
                    label,
                    "Delivery",
                    COLORS["output_bg"],
                    COLORS["output_border"],
                )
            )

        edge_pen = QPen(QColor("#99A7AD"), 1.2)
        edge_pen.setCapStyle(Qt.RoundCap)
        edge_pen.setJoinStyle(Qt.RoundJoin)
        for source_node in source_nodes:
            self._edge(source_node, derived_node, edge_pen)
        for output_node in output_nodes:
            self._edge(derived_node, output_node, edge_pen)

        self._fit()

    def render_dependency_graph(
        self,
        graph,
        changed_node_ids=(),
        selected_edge_ids=None,
        unsupported_edge_ids=(),
    ):
        """Render an arbitrary dependency DAG with native edge checkboxes.

        This method is presentation-only.  Toggling an edge recomputes the pure
        ``PropagationPlan`` preview and emits ``edge_selection_changed``.  It
        does not call refresh, export, sync, source-write, or project-persistence
        services.
        """

        errors = graph.validate()
        if errors:
            self._render_graph_error(errors)
            return

        unsupported = {str(edge_id) for edge_id in unsupported_edge_ids}
        known_edges = {edge.edge_id for edge in graph.edges}
        unsupported &= known_edges
        unsupported_targets = {
            edge.to_node for edge in graph.edges if edge.edge_id in unsupported
        }

        if selected_edge_ids is None:
            selected = default_selected_edges(graph)
        else:
            selected = {str(edge_id) for edge_id in selected_edge_ids}
        selected &= known_edges
        selected -= unsupported

        changed = {str(node_id) for node_id in changed_node_ids}
        plan = plan_propagation(graph, changed, selected) if changed else None

        self._native_graph = graph
        self._native_changed_node_ids = changed
        self._native_unsupported_edge_ids = unsupported
        self._selected_edge_ids = set(selected)

        self._clear_scene()
        positions, scene_height = self._layered_positions(graph)
        self.scene().setSceneRect(QRectF(0, 0, 620, scene_height))
        node_map = graph.node_map()

        node_items = {}
        for node in graph.nodes:
            x, y, width, height = positions[node.node_id]
            kind = node.kind if isinstance(node.kind, NodeKind) else NodeKind(str(node.kind))
            if kind == NodeKind.SOURCE:
                fill = COLORS["source_bg"]
                border = COLORS["source_border"]
            elif kind == NodeKind.DELIVERY:
                fill = COLORS["output_bg"]
                border = COLORS["output_border"]
            else:
                fill = COLORS["derived_bg"]
                border = COLORS["derived_border"]

            incoming_operations = {
                edge.operation
                for edge in graph.edges
                if edge.to_node == node.node_id
            }
            if kind == NodeKind.SOURCE:
                role = "Source"
            elif kind == NodeKind.DELIVERY:
                role = "Delivery"
            elif OperationKind.MERGE in incoming_operations:
                role = "Merge result"
            elif OperationKind.FILTER in incoming_operations:
                role = "Filter result"
            else:
                role = "Result"

            status = ""
            stale = bool(plan and node.node_id in plan.stale_by_choice_node_ids)
            affected = bool(plan and node.node_id in plan.affected_node_ids)
            changed_here = bool(plan and node.node_id in plan.changed_node_ids)
            unsupported_here = node.node_id in unsupported_targets
            if changed_here:
                status = "CHANGED"
            elif unsupported_here and affected:
                status = "REFRESH + UNSUPPORTED"
            elif unsupported_here:
                status = "UNSUPPORTED"
            elif stale and affected:
                status = "REFRESH + STALE"
            elif stale:
                status = "STALE BY CHOICE"
            elif affected:
                status = "WILL REFRESH"

            if unsupported_here:
                border = COLORS["text_disabled"]
            elif stale:
                border = COLORS["warning"]
            elif affected:
                border = COLORS["primary"]

            node_items[node.node_id] = self._node(
                x,
                y,
                width,
                height,
                node.name,
                role,
                fill,
                border,
                status=status,
                dashed=stale,
            )

        for edge in graph.edges:
            enabled = edge.edge_id not in unsupported
            checked = edge.edge_id in selected and enabled
            self._interactive_edge(
                node_items[edge.from_node],
                node_items[edge.to_node],
                edge,
                checked=checked,
                enabled=enabled,
                from_label=node_map[edge.from_node].name,
                to_label=node_map[edge.to_node].name,
            )

        self._fit()

    def selected_edge_ids(self):
        """Return a copy of the current run-scoped native graph selection."""

        return set(self._selected_edge_ids)

    def _render_graph_error(self, errors):
        self._clear_native_state()
        scene = self.scene()
        scene.clear()
        scene.setSceneRect(QRectF(0, 0, 620, 205))
        text = scene.addText("Invalid dependency graph\n" + "\n".join(errors))
        text.setDefaultTextColor(QColor(COLORS["danger"]))
        text.setTextWidth(560)
        rect = text.boundingRect()
        text.setPos((620 - rect.width()) / 2, max(18, (205 - rect.height()) / 2))
        self._fit()

    def _layered_positions(self, graph):
        """Return deterministic topological layer positions for a small DAG."""

        depth = {node.node_id: 0 for node in graph.nodes}
        # Graph validation guarantees acyclicity.  Repeated relaxation therefore
        # converges quickly and keeps the layout independent of input edge order.
        for _ in range(max(1, len(graph.nodes))):
            changed = False
            for edge in graph.edges:
                candidate = depth[edge.from_node] + 1
                if candidate > depth[edge.to_node]:
                    depth[edge.to_node] = candidate
                    changed = True
            if not changed:
                break

        levels = {}
        for node in graph.nodes:
            levels.setdefault(depth[node.node_id], []).append(node)

        max_depth = max(levels, default=0)
        scene_height = max(270, 110 + max_depth * 126)
        positions = {}
        for level in sorted(levels):
            nodes = sorted(levels[level], key=lambda item: (item.name.lower(), item.node_id))
            count = len(nodes)
            width = 148 if count <= 4 else 124
            xs = self._spread_positions(count, 620, width, top=24)
            y = 18 + level * 126
            for node, x in zip(nodes, xs):
                positions[node.node_id] = (x, y, width, 72)
        return positions, scene_height

    @staticmethod
    def _spread_positions(count, total_width, node_width, top=20):
        if count <= 0:
            return []
        if count == 1:
            return [(total_width - node_width) / 2]
        usable = total_width - 2 * top - node_width
        step = usable / (count - 1)
        return [top + index * step for index in range(count)]

    def _edge(self, upper, lower, pen):
        path, start_x, start_y, end_x, end_y, _, _ = self._edge_path(upper, lower)
        self.scene().addPath(path, pen)

        dot_pen = QPen(QColor("#99A7AD"), 0.8)
        dot_brush = QBrush(QColor("#99A7AD"))
        self.scene().addEllipse(start_x - 2, start_y - 2, 4, 4, dot_pen, dot_brush)
        self.scene().addEllipse(end_x - 2, end_y - 2, 4, 4, dot_pen, dot_brush)

    def _interactive_edge(self, upper, lower, edge, checked, enabled, from_label, to_label):
        path, start_x, start_y, end_x, end_y, middle_x, middle_y = self._edge_path(upper, lower)

        if not enabled:
            edge_color = COLORS["text_disabled"]
        elif checked:
            edge_color = COLORS["primary"]
        else:
            edge_color = COLORS["warning"]

        pen = QPen(QColor(edge_color), 1.25)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if not checked:
            pen.setStyle(Qt.DashLine)
        self.scene().addPath(path, pen)

        dot_pen = QPen(QColor(edge_color), 0.8)
        dot_brush = QBrush(QColor(edge_color))
        self.scene().addEllipse(start_x - 2, start_y - 2, 4, 4, dot_pen, dot_brush)
        self.scene().addEllipse(end_x - 2, end_y - 2, 4, 4, dot_pen, dot_brush)

        checkbox = QCheckBox()
        checkbox.setObjectName("AtOnceGraphEdgeToggle")
        checkbox.setProperty("edge_id", edge.edge_id)
        checkbox.setChecked(bool(checked))
        checkbox.setEnabled(bool(enabled))
        checkbox.setToolTip(
            f"{from_label} → {to_label}\n"
            + (
                "Executor is not available for this dependency edge yet."
                if not enabled
                else "Include this dependency edge in the current propagation run."
            )
        )
        checkbox.setStyleSheet(
            "QCheckBox {"
            f"background: {COLORS['surface']};"
            f"border: 1px solid {COLORS['border']};"
            "border-radius: 6px; padding: 3px 4px;"
            "}"
        )
        checkbox.toggled.connect(
            lambda state, edge_id=edge.edge_id: self._on_edge_toggled(edge_id, state)
        )
        proxy = self.scene().addWidget(checkbox)
        proxy.setZValue(20)
        bounds = proxy.boundingRect()
        proxy.setPos(middle_x - bounds.width() / 2, middle_y - bounds.height() / 2)
        self._edge_controls[edge.edge_id] = checkbox

    @staticmethod
    def _edge_path(upper, lower):
        a = upper.sceneBoundingRect()
        b = lower.sceneBoundingRect()
        start_x = a.center().x()
        start_y = a.bottom()
        end_x = b.center().x()
        end_y = b.top()
        middle_y = start_y + (end_y - start_y) / 2
        middle_x = start_x + (end_x - start_x) / 2

        path = QPainterPath()
        path.moveTo(start_x, start_y)
        path.lineTo(start_x, middle_y)
        path.lineTo(end_x, middle_y)
        path.lineTo(end_x, end_y)
        return path, start_x, start_y, end_x, end_y, middle_x, middle_y

    def _on_edge_toggled(self, edge_id, checked):
        """Update run-scoped selection and emit intent; never execute the graph."""

        if checked:
            self._selected_edge_ids.add(edge_id)
        else:
            self._selected_edge_ids.discard(edge_id)
        self.edge_selection_changed.emit(edge_id, bool(checked))

        # Defer redraw until the checkbox's own Qt event has completed.  The
        # redraw recomputes only the pure impact preview; it performs no I/O.
        QTimer.singleShot(0, self._rerender_native_graph)

    def _rerender_native_graph(self):
        graph = self._native_graph
        if graph is None:
            return
        self.render_dependency_graph(
            graph,
            changed_node_ids=set(self._native_changed_node_ids),
            selected_edge_ids=set(self._selected_edge_ids),
            unsupported_edge_ids=set(self._native_unsupported_edge_ids),
        )

    def _node(self, x, y, width, height, label, role, fill, border, status="", dashed=False):
        scene = self.scene()
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, width, height), 8, 8)
        pen = QPen(QColor(border), 1.0)
        if dashed:
            pen.setStyle(Qt.DashLine)
        item = scene.addPath(path, pen, QBrush(QColor(fill)))

        clipped = label if len(label) <= 19 else label[:16] + "…"
        text = scene.addText(clipped, QFont("", 10, QFont.DemiBold))
        text.setDefaultTextColor(QColor(COLORS["text"]))
        text.setToolTip(label)
        text_rect = text.boundingRect()
        text.setPos(x + (width - text_rect.width()) / 2, y + 6)

        role_text = scene.addText(role, QFont("", 8, QFont.Medium))
        role_text.setDefaultTextColor(QColor(COLORS["text_muted"]))
        role_rect = role_text.boundingRect()
        role_y = y + 25
        role_text.setPos(x + (width - role_rect.width()) / 2, role_y)

        if status:
            status_text = scene.addText(status, QFont("", 7, QFont.DemiBold))
            if "STALE" in status:
                status_color = COLORS["warning"]
            elif "UNSUPPORTED" in status:
                status_color = COLORS["text_disabled"]
            else:
                status_color = COLORS["primary"]
            status_text.setDefaultTextColor(QColor(status_color))
            status_rect = status_text.boundingRect()
            status_text.setPos(x + (width - status_rect.width()) / 2, y + 42)
        return item

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        if self.scene() is not None and not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
