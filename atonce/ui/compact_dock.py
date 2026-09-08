"""Compact AtOnce dock whose Workflow tab is canvas-first.

The workflow canvas is intentionally minimal. Changes is a source/output workspace,
and History is a session-only field-impact table. Persisted workflow state is still
owned by the existing service layer.
"""

from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.dependency_graph import NodeKind
from .canvas_icons import material_icon
from .dock import AtOnceDockWidget
from .drag_node_workflow_canvas import DragNodeWorkflowCanvas
from .styles import DOCK_STYLESHEET
from .widgets.dependency_graph import DependencyGraphView


class SafeDragNodeWorkflowCanvas(DragNodeWorkflowCanvas):
    """Keep source binding safe even when a provider cannot expose fields."""

    def __init__(self, *args, **kwargs):
        self._safe_field_render = False
        super().__init__(*args, **kwargs)

    def _show_source_dialog(self, node):
        if (
            self._workflow is not None
            and not getattr(self, "_imported_draft", False)
            and node.kind == NodeKind.SOURCE
            and node.metadata.get("source_lineage_id")
        ):
            source_ids = [
                item.node_id for item in self._graph.nodes if item.kind == NodeKind.SOURCE
            ]
            if node.node_id in source_ids:
                self.source_relink_index_requested.emit(source_ids.index(node.node_id))
                return
        super()._show_source_dialog(node)

    def _available_source_fields(self, node):
        if self._safe_field_render:
            return ()
        try:
            return super()._available_source_fields(node)
        except Exception as exc:
            self.message_requested.emit(
                f"The source layer was selected, but its fields could not be displayed: {exc}"
            )
            return ()

    def _render_graph(self):
        """Never leave the canvas blank because one provider failed to render fields."""

        center = None
        view = getattr(self, "view", None)
        if view is not None and view.viewport() is not None:
            try:
                center = view.mapToScene(view.viewport().rect().center())
            except Exception:
                center = None
        try:
            super()._render_graph()
        except Exception as exc:
            self._safe_field_render = True
            try:
                super()._render_graph()
                self.message_requested.emit(
                    "The workflow was preserved, but optional source-field display "
                    f"was skipped because QGIS reported: {exc}"
                )
            finally:
                self._safe_field_render = False
        if center is not None:
            try:
                view.centerOn(center)
            except Exception:
                pass


class CompactAtOnceDockWidget(AtOnceDockWidget):
    """Drag-first workflow authoring plus source/output change controls."""

    source_edit_requested = pyqtSignal(str)
    update_changes_requested = pyqtSignal()
    session_closed = pyqtSignal()

    def __init__(self, parent=None, qgis_gateway=None):
        QDockWidget.__init__(self, "AtOnce", parent)
        self.setObjectName("AtOnceDockWidget")
        self.setMinimumWidth(560)
        self._workflow = None
        self.qgis_gateway = qgis_gateway
        self.builder = SafeDragNodeWorkflowCanvas(qgis_gateway, parent=self)
        self._coalesce_builder_toolbar()

        root = QWidget(self)
        root.setObjectName("AtOnceRoot")
        root.setStyleSheet(DOCK_STYLESHEET)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._header())

        self.tabs = QTabWidget(root)
        self.tabs.setObjectName("AtOnceTabs")
        self.tabs.addTab(self._workflow_tab(), "Workflow")
        self.tabs.addTab(self._changes_tab(), "Changes")
        self.tabs.addTab(self._history_tab(), "History")
        layout.addWidget(self.tabs, 1)
        self.setWidget(root)

        self.builder.readiness_changed.connect(self._canvas_readiness_changed)
        self.set_workflow(None)
        self._apply_responsive_toolbar()

    def _coalesce_builder_toolbar(self):
        for button in (
            self.builder.import_button,
            self.builder.export_button,
            self.builder.run_button,
        ):
            button.setMinimumHeight(38)

    def _apply_responsive_toolbar(self):
        width = max(1, self.width())
        if width < 700:
            sizes = (64, 64, 82)
        elif width < 860:
            sizes = (72, 72, 92)
        else:
            sizes = (80, 80, 102)
        for button, target in zip(
            (self.builder.import_button, self.builder.export_button, self.builder.run_button),
            sizes,
        ):
            button.setMinimumWidth(target)
            button.setMaximumWidth(target)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "builder"):
            self._apply_responsive_toolbar()

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self.session_closed.emit()
        super().closeEvent(event)

    def _clear_canvas_only(self):
        self.builder.clear_canvas()
        self.workflow_name_label.setText(
            "Drag blocks into the empty canvas, configure them, then Register."
        )
        self.validation_title.setText("Not configured")
        self.validation_detail.setText("")
        self.validation_icon.setText("·")

    def _show_usage_help(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        QMessageBox.information(
            self,
            "How to use AtOnce",
            "1. Drag SOURCE, OPERATION and OUTPUT blocks onto the canvas.\n\n"
            "2. Click a SOURCE to choose a loaded QGIS vector layer.\n\n"
            "3. Click an OPERATION to choose and configure the function.\n\n"
            "4. Drag from node ports to connect the lineage.\n\n"
            "5. Configure OUTPUT blocks and keep Include in Changes checked for "
            "outputs that should refresh after source edits.\n\n"
            "6. Click Register to save the lineage and create/update included outputs.\n\n"
            "Tip: quick-click configures a block; hold it for 1.5 seconds before "
            "dragging to move its position.",
        )

    def _header(self):
        frame = QFrame()
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 2)
        row.setSpacing(9)

        logo = QLabel(frame)
        logo.setObjectName("AtOnceHeaderLogo")
        logo.setFixedSize(38, 38)
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet("background:#111111; border-radius:8px; padding:3px;")
        icon_path = Path(__file__).resolve().parents[1] / "resources" / "icon.png"
        pixmap = QPixmap(str(icon_path))
        if not pixmap.isNull():
            logo.setPixmap(
                pixmap.scaled(
                    30,
                    30,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        row.addWidget(logo, 0, Qt.AlignTop)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        title = QLabel("AtOnce")
        title.setObjectName("AtOnceTitle")
        subtitle = QLabel("Trace lineage. Review safely. Refresh downstream.")
        subtitle.setObjectName("AtOnceSubtitle")
        subtitle.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(subtitle)
        row.addLayout(copy, 1)

        self.status_chip = QLabel("Not configured")
        self.status_chip.setVisible(False)
        return frame

    def _workflow_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(7)

        top = QFrame()
        top.setObjectName("AtOnceCard")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(9, 7, 9, 7)
        top_layout.setSpacing(6)
        self.workflow_combo = QComboBox()
        self.workflow_combo.setObjectName("AtOnceWorkflowSelector")
        self.workflow_combo.setPlaceholderText("Choose a saved workflow…")
        self.workflow_combo.currentIndexChanged.connect(self._workflow_changed)
        self.register_button = QPushButton("Clear")
        self.register_button.setToolTip("Clear only the current canvas; saved workflows are not deleted")
        self.register_button.clicked.connect(self._clear_canvas_only)
        self.info_button = QPushButton("")
        self.info_button.setIcon(material_icon("info", size=20))
        self.info_button.setObjectName("AtOnceQuiet")
        self.info_button.setFixedWidth(38)
        self.info_button.setToolTip("How to use AtOnce")
        self.info_button.clicked.connect(self._show_usage_help)
        top_layout.addWidget(self.workflow_combo, 1)
        top_layout.addWidget(self.register_button)
        top_layout.addWidget(self.info_button)
        layout.addWidget(top)

        self.workflow_name_label = QLabel(
            "Drag blocks into the empty canvas, configure them, then Register."
        )
        self.workflow_name_label.setObjectName("AtOnceMuted")
        self.workflow_name_label.setWordWrap(True)
        layout.addWidget(self.workflow_name_label)

        canvas = QScrollArea()
        canvas.setObjectName("AtOnceWorkflowCanvasScroll")
        canvas.setWidgetResizable(True)
        canvas.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        canvas.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        canvas.setWidget(self.builder)
        self.canvas_scroll = canvas
        layout.addWidget(canvas, 1)

        self.validation_banner = QFrame()
        self.validation_banner.setObjectName("AtOnceValidationNeutral")
        status_layout = QHBoxLayout(self.validation_banner)
        status_layout.setContentsMargins(9, 6, 9, 6)
        status_layout.setSpacing(6)
        self.validation_icon = QLabel("·")
        self.validation_title = QLabel("Not configured")
        self.validation_title.setObjectName("AtOnceSectionTitle")
        self.validation_detail = QLabel("")
        self.validation_detail.setObjectName("AtOnceMuted")
        self.validation_detail.setWordWrap(True)
        status_copy = QVBoxLayout()
        status_copy.setContentsMargins(0, 0, 0, 0)
        status_copy.setSpacing(1)
        status_copy.addWidget(self.validation_title)
        status_copy.addWidget(self.validation_detail)
        status_layout.addWidget(self.validation_icon, 0, Qt.AlignTop)
        status_layout.addLayout(status_copy, 1)
        layout.addWidget(self.validation_banner)

        self.plan_button = self.builder.run_button
        self.refresh_button = self.plan_button

        self.sources_value = QLabel("—")
        self.sources_meta = QLabel("No sources")
        self.derived_value = QLabel("—")
        self.derived_meta = QLabel("Not configured")
        self.outputs_value = QLabel("—")
        self.outputs_meta = QLabel("No outputs")
        self.validation_value = QLabel("Not validated")
        self.validation_meta = QLabel("Run validation")
        self.author_button = QPushButton("Create derived workflow")
        self.author_button.clicked.connect(self.authoring_requested.emit)
        self.validate_button = QPushButton("Validate")
        self.validate_button.clicked.connect(self.validate_requested.emit)
        self.graph = DependencyGraphView(page)
        self.graph.setVisible(False)
        return page

    def _changes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        source_card = QFrame(page)
        source_card.setObjectName("AtOnceCard")
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(10, 10, 10, 10)
        source_layout.setSpacing(7)
        source_title = QLabel("Source Layers")
        source_title.setObjectName("AtOnceSectionTitle")
        source_layout.addWidget(source_title)
        source_row = QHBoxLayout()
        self.source_tabs = QTabBar(source_card)
        self.source_tabs.setExpanding(False)
        self.source_tabs.currentChanged.connect(self._source_tab_changed)
        self.source_edit_button = QPushButton("Start Editing", source_card)
        self.source_edit_button.setEnabled(False)
        self.source_edit_button.clicked.connect(self._emit_source_edit)
        source_row.addWidget(self.source_tabs, 1)
        source_row.addWidget(self.source_edit_button)
        source_layout.addLayout(source_row)
        self.source_stack = QStackedWidget(source_card)
        source_layout.addWidget(self.source_stack, 1)
        layout.addWidget(source_card, 1)

        output_card = QFrame(page)
        output_card.setObjectName("AtOnceCard")
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(10, 10, 10, 10)
        output_layout.setSpacing(7)
        output_title = QLabel("Output Layers")
        output_title.setObjectName("AtOnceSectionTitle")
        output_layout.addWidget(output_title)
        output_row = QHBoxLayout()
        self.output_tabs = QTabBar(output_card)
        self.output_tabs.setExpanding(False)
        self.output_tabs.currentChanged.connect(self._output_tab_changed)
        self.update_changes_button = QPushButton("Update Changes", output_card)
        self.update_changes_button.setObjectName("AtOncePrimary")
        self.update_changes_button.setEnabled(False)
        self.update_changes_button.clicked.connect(self.update_changes_requested.emit)
        output_row.addWidget(self.output_tabs, 1)
        output_row.addWidget(self.update_changes_button)
        output_layout.addLayout(output_row)
        self.output_stack = QStackedWidget(output_card)
        output_layout.addWidget(self.output_stack, 1)
        layout.addWidget(output_card, 1)
        return page

    def _history_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        helper = QLabel("Current session only — this table clears when AtOnce/QGIS is reopened.")
        helper.setObjectName("AtOnceMuted")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        self.history_table = QTableWidget(0, 4, page)
        self.history_table.setObjectName("AtOnceTable")
        self.history_table.setHorizontalHeaderLabels(
            ["Changed layer", "Changed field", "Affected source", "Corresponding field"]
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history_table, 1)
        return page

    @staticmethod
    def _clear_stack(tab_bar, stack):
        while tab_bar.count():
            tab_bar.removeTab(0)
        while stack.count():
            widget = stack.widget(0)
            stack.removeWidget(widget)
            widget.deleteLater()

    def _populate_change_layers(self, workflow):
        self._clear_stack(self.source_tabs, self.source_stack)
        self._clear_stack(self.output_tabs, self.output_stack)
        if workflow is None:
            self.source_edit_button.setEnabled(False)
            self.update_changes_button.setEnabled(False)
            return

        for source in workflow.source_layers:
            page = QWidget(self.source_stack)
            page.setProperty("source_lineage_id", source.stable_id)
            page.setProperty("layer_id", source.current_layer_id)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(10, 14, 10, 14)
            label = QLabel(source.name or source.stable_id, page)
            label.setObjectName("AtOnceSectionTitle")
            note = QLabel(
                "This is the registered QGIS source layer. Use Start Editing to enter native layer edit mode.",
                page,
            )
            note.setObjectName("AtOnceMuted")
            note.setWordWrap(True)
            page_layout.addWidget(label)
            page_layout.addWidget(note)
            page_layout.addStretch(1)
            self.source_tabs.addTab(source.name or "Source")
            self.source_stack.addWidget(page)

        graph = workflow.effective_dependency_graph()
        for node in graph.nodes:
            if node.kind != NodeKind.DELIVERY:
                continue
            is_raster = str(node.metadata.get("data_type") or "") == "raster"
            included = bool(node.metadata.get("include_in_changes", True))
            page = QWidget(self.output_stack)
            page.setProperty("output_node_id", node.node_id)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(10, 14, 10, 14)
            label = QLabel(node.name or "Output", page)
            label.setObjectName("AtOnceSectionTitle")
            status = QLabel(
                (
                    "Raster output — not part of Changes"
                    if is_raster
                    else (
                        "Included in Changes"
                        if included
                        else "Excluded from Changes"
                    )
                ),
                page,
            )
            status.setObjectName("AtOnceMuted")
            page_layout.addWidget(label)
            page_layout.addWidget(status)
            page_layout.addStretch(1)
            self.output_tabs.addTab(node.name or "Output")
            self.output_stack.addWidget(page)

        if self.source_tabs.count():
            self.source_tabs.setCurrentIndex(0)
            self.source_stack.setCurrentIndex(0)
            self.source_edit_button.setEnabled(True)
            self._source_tab_changed(0)
        else:
            self.source_edit_button.setEnabled(False)
        if self.output_tabs.count():
            self.output_tabs.setCurrentIndex(0)
            self.output_stack.setCurrentIndex(0)
        self.update_changes_button.setEnabled(
            any(
                node.kind == NodeKind.DELIVERY
                and str(node.metadata.get("data_type") or "") != "raster"
                and bool(node.metadata.get("include_in_changes", True))
                for node in graph.nodes
            )
        )

    def _source_tab_changed(self, index):
        if 0 <= index < self.source_stack.count():
            self.source_stack.setCurrentIndex(index)
        self.refresh_source_edit_button()

    def _output_tab_changed(self, index):
        if 0 <= index < self.output_stack.count():
            self.output_stack.setCurrentIndex(index)

    def selected_source_lineage_id(self):
        page = self.source_stack.currentWidget()
        return str(page.property("source_lineage_id") or "") if page is not None else ""

    def selected_source_layer_id(self):
        page = self.source_stack.currentWidget()
        return str(page.property("layer_id") or "") if page is not None else ""

    def selected_output_node_id(self):
        page = self.output_stack.currentWidget()
        return str(page.property("output_node_id") or "") if page is not None else ""

    def _emit_source_edit(self):
        lineage_id = self.selected_source_lineage_id()
        if lineage_id:
            self.source_edit_requested.emit(lineage_id)

    def refresh_source_edit_button(self):
        layer_id = self.selected_source_layer_id()
        layer = self.qgis_gateway.resolve_layer(layer_id) if self.qgis_gateway and layer_id else None
        if layer is None:
            self.source_edit_button.setText("Start Editing")
            self.source_edit_button.setEnabled(False)
            return
        editable = bool(layer.isEditable()) if hasattr(layer, "isEditable") else False
        self.source_edit_button.setText("Stop Editing" if editable else "Start Editing")
        self.source_edit_button.setEnabled(True)

    def append_session_history(self, rows):
        for changed_layer, changed_field, affected_source, corresponding_field in rows or ():
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for column, value in enumerate(
                (changed_layer, changed_field, affected_source, corresponding_field)
            ):
                self.history_table.setItem(row, column, QTableWidgetItem(str(value or "—")))
        self.history_table.resizeColumnsToContents()
        self.history_table.horizontalHeader().setStretchLastSection(True)

    def clear_session_history(self):
        self.history_table.setRowCount(0)

    def set_workflow(self, workflow):
        super().set_workflow(workflow)
        self.register_button.setText("Clear")
        self.workflow_combo.setEnabled(self.workflow_combo.count() > 0)
        if workflow is None:
            self.workflow_name_label.setText(
                "Drag blocks into the empty canvas, configure them, then Register."
            )
        else:
            self.workflow_name_label.setText(workflow.name)
        self.author_button.setVisible(False)
        self.validate_button.setVisible(False)
        self._populate_change_layers(workflow)

    def set_run_evidence(self, workflow, revision=None, graph_state=None, freshness=None):
        super().set_run_evidence(workflow, revision, graph_state, freshness)
        if workflow is not None and workflow.effective_dependency_graph_origin() in {
            "explicit",
            "freeform_explicit",
        }:
            self.builder.set_result_evidence(graph_state)