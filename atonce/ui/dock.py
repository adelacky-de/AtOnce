"""Primary AtOnce dock widget.

The dock presents persisted workflow state and emits user intent only. Source mutation,
export execution and audit persistence remain outside the UI layer.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .styles import DOCK_STYLESHEET
from .guided_workflow_builder import GuidedWorkflowCanvas
from .widgets.dependency_graph import DependencyGraphView


class AtOnceDockWidget(QDockWidget):
    register_requested = pyqtSignal()
    validate_requested = pyqtSignal()
    sync_requested = pyqtSignal()
    refresh_requested = pyqtSignal()
    trace_requested = pyqtSignal()
    plan_requested = pyqtSignal()
    workflow_selected = pyqtSignal(str)
    authoring_requested = pyqtSignal()

    def __init__(self, parent=None, qgis_gateway=None):
        super().__init__("AtOnce", parent)
        self.setObjectName("AtOnceDockWidget")
        self.setMinimumWidth(440)
        self._workflow = None
        self.builder = GuidedWorkflowCanvas(
            qgis_gateway,
            parent=self,
        )

        root = QWidget(self)
        root.setObjectName("AtOnceRoot")
        root.setStyleSheet(DOCK_STYLESHEET)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

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

    def _header(self):
        frame = QFrame()
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 2)
        row.setSpacing(10)

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
        self.status_chip.setObjectName("AtOnceStatusNeutral")
        self.status_chip.setAlignment(Qt.AlignCenter)
        row.addWidget(self.status_chip, 0, Qt.AlignTop)
        return frame

    def _workflow_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        overview = self._card()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(12, 11, 12, 10)
        overview_layout.setSpacing(5)
        overview_layout.addWidget(self._section_title("Workflow canvas"))
        self.workflow_name_label = QLabel(
            "Drop sources into the blocks, click a block to configure it, then use > Plan."
        )
        self.workflow_name_label.setObjectName("AtOnceMuted")
        self.workflow_name_label.setWordWrap(True)
        overview_layout.addWidget(self.workflow_name_label)
        canvas_scroll = QScrollArea()
        canvas_scroll.setObjectName("AtOnceWorkflowCanvasScroll")
        canvas_scroll.setWidgetResizable(True)
        canvas_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        canvas_scroll.setWidget(self.builder)
        overview_layout.addWidget(canvas_scroll, 1)
        self.graph = DependencyGraphView(page)
        self.graph.setVisible(False)
        layout.addWidget(overview)

        selector = self._card()
        selector_layout = QHBoxLayout(selector)
        selector_layout.setContentsMargins(12, 9, 12, 9)
        selector_layout.setSpacing(8)
        selector_label = QLabel("Workflow")
        selector_label.setObjectName("AtOnceSectionTitle")
        self.workflow_combo = QComboBox()
        self.workflow_combo.setObjectName("AtOnceWorkflowSelector")
        self.workflow_combo.currentIndexChanged.connect(self._workflow_changed)
        selector_layout.addWidget(selector_label)
        selector_layout.addWidget(self.workflow_combo, 1)
        layout.insertWidget(0, selector)

        layout.addWidget(self._section_title("Configuration summary"))
        summary_grid = QGridLayout()
        summary_grid.setContentsMargins(0, 0, 0, 0)
        summary_grid.setHorizontalSpacing(8)
        summary_grid.setVerticalSpacing(8)

        sources_card, self.sources_value, self.sources_meta = self._summary_card(
            "Sources", "—", "No sources"
        )
        derived_card, self.derived_value, self.derived_meta = self._summary_card(
            "Derived steps", "—", "Not configured"
        )
        outputs_card, self.outputs_value, self.outputs_meta = self._summary_card(
            "Deliveries", "—", "No outputs"
        )
        validation_card, self.validation_value, self.validation_meta = self._summary_card(
            "Validation", "Not validated", "Run validation after setup"
        )
        summary_grid.addWidget(sources_card, 0, 0)
        summary_grid.addWidget(derived_card, 0, 1)
        summary_grid.addWidget(outputs_card, 1, 0)
        summary_grid.addWidget(validation_card, 1, 1)
        layout.addLayout(summary_grid)

        self.validation_banner = QFrame()
        self.validation_banner.setObjectName("AtOnceValidationNeutral")
        banner_layout = QHBoxLayout(self.validation_banner)
        banner_layout.setContentsMargins(11, 9, 11, 9)
        banner_layout.setSpacing(9)
        self.validation_icon = QLabel("·")
        self.validation_icon.setObjectName("AtOnceMetric")
        banner_copy = QVBoxLayout()
        banner_copy.setContentsMargins(0, 0, 0, 0)
        banner_copy.setSpacing(1)
        self.validation_title = QLabel("Workflow not configured")
        self.validation_title.setObjectName("AtOnceSectionTitle")
        self.validation_detail = QLabel("Register the dependency chain to begin.")
        self.validation_detail.setObjectName("AtOnceMuted")
        self.validation_detail.setWordWrap(True)
        banner_copy.addWidget(self.validation_title)
        banner_copy.addWidget(self.validation_detail)
        banner_layout.addWidget(self.validation_icon, 0, Qt.AlignTop)
        banner_layout.addLayout(banner_copy, 1)
        layout.addWidget(self.validation_banner)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.register_button = QPushButton("Create workflow")
        self.register_button.setObjectName("AtOncePrimary")
        self.register_button.clicked.connect(self.register_requested.emit)
        self.validate_button = QPushButton("Validate")
        self.validate_button.clicked.connect(self.validate_requested.emit)
        self.validate_button.setEnabled(False)
        self.plan_button = QPushButton("> Plan")
        self.plan_button.setObjectName("AtOncePrimary")
        self.plan_button.clicked.connect(self.plan_requested.emit)
        self.refresh_button = self.plan_button
        self.refresh_button.setEnabled(False)
        self.refresh_button.setToolTip("Validate, prepare source identity, review edges and run this workflow.")
        self.author_button = QPushButton("Create derived workflow")
        self.author_button.clicked.connect(self.authoring_requested.emit)
        actions.addWidget(self.register_button, 1)
        actions.addWidget(self.author_button)
        actions.addWidget(self.validate_button)
        actions.addWidget(self.refresh_button)
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    def _changes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        card = self._card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 11, 12, 11)
        card_layout.setSpacing(8)
        card_layout.addWidget(self._section_title("Detected changes"))
        helper = QLabel("Spreadsheet edits will be compared against linked export snapshots.")
        helper.setObjectName("AtOnceMuted")
        helper.setWordWrap(True)
        card_layout.addWidget(helper)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        for label in ("All 0", "Writable 0", "Conflicts 0", "Blocked 0"):
            chip = QPushButton(label)
            chip.setObjectName("AtOnceQuiet")
            chip.setEnabled(False)
            chips.addWidget(chip)
        chips.addStretch(1)
        card_layout.addLayout(chips)

        self.changes_table = QTableWidget(0, 6)
        self.changes_table.setObjectName("AtOnceTable")
        self.changes_table.setHorizontalHeaderLabels(
            ["Source", "Feature", "Field", "Before", "After", "Status"]
        )
        self.changes_table.verticalHeader().setVisible(False)
        self.changes_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.changes_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.changes_table.horizontalHeader().setStretchLastSection(True)
        self.changes_table.setVisible(False)
        card_layout.addWidget(self.changes_table)

        empty = self._empty_card(
            "No changes to review yet",
            "Choose a linked XLSX export and scan its returned workbook to review changes.",
            "Scanning is read-only until you explicitly review selected Writable rows.",
        )
        card_layout.addWidget(empty)
        layout.addWidget(card, 1)

        review = QFrame()
        review.setObjectName("AtOnceValidationNeutral")
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(11, 9, 11, 9)
        review_layout.setSpacing(2)
        review_title = QLabel("Nothing will be written upstream without review")
        review_title.setObjectName("AtOnceSectionTitle")
        review_note = QLabel(
            "Scanning and tracing are read-only. Source writes require selected Writable rows and a separate final confirmation."
        )
        review_note.setObjectName("AtOnceMuted")
        review_note.setWordWrap(True)
        review_layout.addWidget(review_title)
        review_layout.addWidget(review_note)
        layout.addWidget(review)

        actions = QHBoxLayout()
        trace_button = QPushButton("Trace source")
        trace_button.clicked.connect(self.trace_requested.emit)
        trace_button.setEnabled(False)
        review_button = QPushButton("Review changes")
        review_button.setObjectName("AtOncePrimary")
        review_button.setEnabled(False)
        sync_button = QPushButton("Sync downstream")
        sync_button.clicked.connect(self.sync_requested.emit)
        sync_button.setEnabled(False)
        actions.addWidget(trace_button)
        actions.addWidget(review_button, 1)
        actions.addWidget(sync_button)
        layout.addLayout(actions)
        return page

    def _history_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        top_grid = QGridLayout()
        top_grid.setContentsMargins(0, 0, 0, 0)
        top_grid.setSpacing(8)
        activity = self._card()
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(12, 11, 12, 11)
        activity_layout.setSpacing(7)
        activity_layout.addWidget(self._section_title("Recent activity"))
        metrics = QHBoxLayout()
        for number, label in (("0", "Exports"), ("0", "Validations"), ("0", "Refreshes")):
            metric = QVBoxLayout()
            value = QLabel(number)
            value.setObjectName("AtOnceMetric")
            value.setAlignment(Qt.AlignCenter)
            caption = QLabel(label)
            caption.setObjectName("AtOnceMuted")
            caption.setAlignment(Qt.AlignCenter)
            metric.addWidget(value)
            metric.addWidget(caption)
            metrics.addLayout(metric, 1)
        activity_layout.addLayout(metrics)

        audit = self._card()
        audit_layout = QVBoxLayout(audit)
        audit_layout.setContentsMargins(12, 11, 12, 11)
        audit_layout.setSpacing(4)
        audit_layout.addWidget(self._section_title("Audit trail"))
        audit_note = QLabel("Completed legacy syncs and explicit graph runs are shown from project evidence.")
        audit_note.setObjectName("AtOnceMuted")
        audit_note.setWordWrap(True)
        audit_layout.addWidget(audit_note)
        top_grid.addWidget(activity, 0, 0)
        top_grid.addWidget(audit, 0, 1)
        layout.addLayout(top_grid)

        history_card = self._card()
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(12, 11, 12, 11)
        history_layout.setSpacing(8)
        history_layout.addWidget(self._section_title("Workflow history"))
        history_layout.addWidget(
            self._empty_card(
                "No workflow evidence yet",
                "Completed graph runs, delivery revisions and approved sync operations appear here.",
                "No entries are shown until durable project evidence exists.",
            )
        )
        layout.addWidget(history_card, 1)

        rollback = QFrame()
        rollback.setObjectName("AtOnceValidationWarning")
        rollback_layout = QVBoxLayout(rollback)
        rollback_layout.setContentsMargins(11, 9, 11, 9)
        rollback_layout.setSpacing(2)
        rollback_title = QLabel("Rollback reference")
        rollback_title.setObjectName("AtOnceSectionTitle")
        rollback_note = QLabel(
            "Completed export and sync records retain recoverable references for safe review."
        )
        rollback_note.setObjectName("AtOnceMuted")
        rollback_note.setWordWrap(True)
        rollback_layout.addWidget(rollback_title)
        rollback_layout.addWidget(rollback_note)
        layout.addWidget(rollback)

        actions = QHBoxLayout()
        export_log = QPushButton("Export log")
        export_log.setEnabled(False)
        details = QPushButton("View details")
        details.setEnabled(False)
        actions.addWidget(export_log, 1)
        actions.addWidget(details, 1)
        layout.addLayout(actions)
        return page

    def set_workflow(self, workflow):
        self._workflow = workflow
        # The active canvas is also the read-only visual adapter for legacy
        # workflows.  Each builder decides how its origin is rendered; the dock
        # must not silently hide a persisted graph just because it is legacy.
        self.builder.set_workflow(workflow)
        self.graph.render_workflow(workflow)

        if workflow is None:
            self.workflow_combo.setEnabled(False)
            self.workflow_name_label.setText("Register a workflow to map dependencies from source to outputs.")
            self.sources_value.setText("—")
            self.sources_meta.setText("No sources")
            self.derived_value.setText("—")
            self.derived_meta.setText("Not configured")
            self.outputs_value.setText("—")
            self.outputs_meta.setText("No outputs")
            self.validation_value.setText("Not validated")
            self.validation_meta.setText("Run validation after setup")
            self.register_button.setText("Create workflow")
            self.author_button.setText("Create derived workflow")
            self.author_button.setEnabled(False)
            self.validate_button.setEnabled(False)
            self._set_status("Not configured", "neutral")
            self._set_validation_banner(
                "neutral", "·", "Workflow not configured", "Register the dependency chain to begin."
            )
            return

        self.workflow_name_label.setText(workflow.name)
        sources = workflow.source_layers
        self.sources_value.setText("\n".join(layer.name for layer in sources) or "—")
        self.sources_meta.setText(f"{len(sources)} source layer(s)")

        graph = workflow.effective_dependency_graph()
        explicit = workflow.effective_dependency_graph_origin() in {
            "explicit",
            "freeform_explicit",
        }
        derived_nodes = [node for node in graph.nodes if node.kind.value == "derived"]
        if explicit:
            self.derived_value.setText(str(len(derived_nodes)))
            self.derived_meta.setText("derived step(s)" if derived_nodes else "No derived steps")
        else:
            derived = workflow.derived_layer
            if derived is None:
                self.derived_value.setText("—")
                self.derived_meta.setText("Not configured")
            else:
                self.derived_value.setText(derived.name or "Layer C")
                self.derived_meta.setText("Layer C")

        output_lines = []
        if explicit:
            output_lines.extend(
                delivery.name or delivery.format
                for delivery in workflow.forward_deliveries
            )
        else:
            if workflow.gpkg_path:
                output_lines.append("GeoPackage")
            output_lines.extend(export.name for export in workflow.exports)
        self.outputs_value.setText("\n".join(output_lines) or "—")
        self.outputs_meta.setText(f"{len(output_lines)} configured delivery/output(s)")

        self.validation_value.setText("Not validated")
        self.validation_meta.setText("Run validation")
        self.register_button.setText("Edit workflow")
        self.author_button.setEnabled(False)
        if explicit:
            is_single_filter = len(derived_nodes) == 1 and any(
                edge.operation.value == "filter" and edge.to_node == derived_nodes[0].node_id
                for edge in graph.edges
            )
            if is_single_filter:
                self.author_button.setText("Add second filter")
                self.author_button.setEnabled(True)
            else:
                self.author_button.setText("No further transform")
        else:
            self.author_button.setText("Create derived workflow")
            self.author_button.setEnabled(True)
        self.validate_button.setEnabled(True)
        self._set_status("Not validated", "neutral")
        self._set_validation_banner(
            "neutral", "·", "Ready to validate", "Check layer references, output paths and filter expressions."
        )

    def _canvas_readiness_changed(self, ready):
        """Enable Plan for an unsaved canvas draft without bypassing validation."""

        if self._workflow is None:
            self.refresh_button.setEnabled(bool(ready))

    def set_workflows(self, workflows, active_workflow_id=None):
        """Render all persisted workflow choices without exposing internal IDs."""

        self.workflow_combo.blockSignals(True)
        self.workflow_combo.clear()
        for workflow_id, label in workflows or []:
            self.workflow_combo.addItem(label, workflow_id)
        index = self.workflow_combo.findData(active_workflow_id)
        if index >= 0:
            self.workflow_combo.setCurrentIndex(index)
        self.workflow_combo.blockSignals(False)
        self.workflow_combo.setEnabled(self.workflow_combo.count() > 0)

    def _workflow_changed(self, index):
        workflow_id = self.workflow_combo.itemData(index)
        if workflow_id:
            self.workflow_selected.emit(str(workflow_id))

    def set_run_evidence(self, workflow, revision=None, graph_state=None, freshness=None):
        if workflow is None:
            return
        if workflow.effective_dependency_graph_origin() in {"explicit", "freeform_explicit"}:
            refreshed = bool(graph_state and graph_state.refreshed_node_ids)
            self.builder.set_materialized(refreshed)
        stale = len(graph_state.stale_node_ids) if graph_state is not None else 0
        if workflow.effective_dependency_graph_origin() in {"explicit", "freeform_explicit"}:
            if freshness is not None and freshness.state == "not_run":
                text = "Not run yet · run required"
            elif freshness is not None and freshness.state == "pre_g8b":
                text = "Run required to confirm current configuration"
            elif freshness is not None and freshness.state == "changed":
                if revision is not None:
                    text = (
                        f"Last completed revision {revision.revision_number} · "
                        "configuration changed since this run"
                    )
                else:
                    text = "Configuration changed · run required"
            elif revision is not None:
                delivery_count = len(revision.delivery_outputs)
                text = f"Revision {revision.revision_number} current · {delivery_count} delivery output(s)"
            elif graph_state is not None:
                refreshed = len(graph_state.refreshed_node_ids)
                text = f"Last run current · refreshed {refreshed} node(s) · no delivery revision"
            else:
                text = "No explicit run recorded"
            if stale:
                text += f" · {stale} stale by choice"
            self.outputs_meta.setText(text)
            if (
                freshness is not None
                and not freshness.is_current
                and "blocking issue" not in self.validation_value.text()
            ):
                self._set_status("Run required", "warning")
                self._set_validation_banner(
                    "warning",
                    "!",
                    freshness.label,
                    "Review and run this explicit workflow to apply the saved configuration.",
                )
        elif revision is not None:
            rows = sum(output.row_count for output in revision.outputs)
            suffix = f" · {stale} stale node(s)" if stale else ""
            self.outputs_meta.setText(
                f"Revision {revision.revision_number} complete · {rows} refreshed XLSX row(s){suffix}"
            )
        elif stale:
            self.outputs_meta.setText(f"{stale} downstream node(s) stale by choice")

    def set_validation(self, result):
        if result is None:
            label = "Not validated" if self._workflow else "Not configured"
            self.validation_value.setText(label)
            self.validation_meta.setText("Run validation" if self._workflow else "Register workflow first")
            self._set_status(label, "neutral")
            return

        errors = sum(1 for issue in result.issues if issue.severity == "error")
        warnings = sum(1 for issue in result.issues if issue.severity != "error")

        if result.is_valid and not result.issues:
            self.validation_value.setText("No blocking issues")
            self.validation_meta.setText("Validation passed")
            self._set_status("Valid", "valid")
            self._set_validation_banner(
                "success", "✓", "Workflow is valid", "No blocking issues detected."
            )
        elif result.is_valid:
            self.validation_value.setText(f"{warnings} warning(s)")
            self.validation_meta.setText("Valid with attention")
            self._set_status("Needs review", "warning")
            self._set_validation_banner(
                "warning",
                "!",
                "Workflow is valid with warnings",
                f"{warnings} warning(s) should be reviewed before downstream execution.",
            )
        else:
            self.validation_value.setText(f"{errors} blocking issue(s)")
            self.validation_meta.setText("Fix before continuing")
            self._set_status("Invalid", "error")
            self._set_validation_banner(
                "error",
                "!",
                "Workflow needs attention",
                f"{errors} blocking issue(s) must be resolved before downstream execution.",
            )

    def _set_status(self, text, state):
        object_names = {
            "valid": "AtOnceStatusValid",
            "warning": "AtOnceStatusWarning",
            "error": "AtOnceStatusError",
            "neutral": "AtOnceStatusNeutral",
        }
        self.status_chip.setText(text)
        self.status_chip.setObjectName(object_names[state])
        self._repolish(self.status_chip)

    def _set_validation_banner(self, state, icon, title, detail):
        object_names = {
            "success": "AtOnceValidationSuccess",
            "warning": "AtOnceValidationWarning",
            "error": "AtOnceValidationError",
            "neutral": "AtOnceValidationNeutral",
        }
        self.validation_banner.setObjectName(object_names[state])
        self.validation_icon.setText(icon)
        self.validation_title.setText(title)
        self.validation_detail.setText(detail)
        self._repolish(self.validation_banner)

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    @staticmethod
    def _section_title(text):
        label = QLabel(text)
        label.setObjectName("AtOnceSectionTitle")
        return label

    @staticmethod
    def _card():
        frame = QFrame()
        frame.setObjectName("AtOnceCard")
        return frame

    @staticmethod
    def _summary_card(title, value, meta):
        frame = QFrame()
        frame.setObjectName("AtOnceSummaryCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("AtOnceSectionTitle")
        value_label = QLabel(value)
        value_label.setWordWrap(True)
        meta_label = QLabel(meta)
        meta_label.setObjectName("AtOnceMeta")
        meta_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addStretch(1)
        layout.addWidget(meta_label)
        return frame, value_label, meta_label

    @staticmethod
    def _empty_card(title, body, meta):
        frame = QFrame()
        frame.setObjectName("AtOnceEmptyCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 18, 16, 18)
        layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("AtOnceSectionTitle")
        title_label.setAlignment(Qt.AlignCenter)
        body_label = QLabel(body)
        body_label.setObjectName("AtOnceMuted")
        body_label.setWordWrap(True)
        body_label.setAlignment(Qt.AlignCenter)
        meta_label = QLabel(meta)
        meta_label.setObjectName("AtOnceMeta")
        meta_label.setWordWrap(True)
        meta_label.setAlignment(Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(title_label)
        layout.addWidget(body_label)
        layout.addWidget(meta_label)
        layout.addStretch(1)
        return frame
