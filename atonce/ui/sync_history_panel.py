"""Small History-tab renderer for persisted sync audit records."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.history_activity import sync_activity_counts


class SyncHistoryPanel:
    def __init__(self, dock):
        self.dock = dock
        self.page = dock.tabs.widget(2)
        self.layout = self.page.layout()

        self.card = QFrame()
        self.card.setObjectName("AtOnceCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(12, 11, 12, 11)
        card_layout.setSpacing(7)
        title = QLabel("Sync audit")
        title.setObjectName("AtOnceSectionTitle")
        self.meta = QLabel(
            "No approved source sync has been recorded yet. Returned workbook archives are retained when available."
        )
        self.meta.setObjectName("AtOnceMuted")
        self.meta.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("AtOnceTable")
        self.table.setHorizontalHeaderLabels(
            ["Started", "Export / rev", "Changes", "Refresh rev", "Status"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setVisible(False)
        card_layout.addWidget(title)
        card_layout.addWidget(self.meta)
        card_layout.addWidget(self.table)
        self.layout.insertWidget(1, self.card)

        self._legacy_empty = None
        for label in self.page.findChildren(QLabel):
            if label.text() == "No workflow evidence yet":
                self._legacy_empty = label.parentWidget()

        # The original Porcelain mockup shipped static Recent activity counters for
        # Exports / Validations / Refreshes. Once persisted sync audits became real,
        # leaving those labels at zero was misleading. Only show activity that can be
        # reconstructed honestly from the durable audit store: sync operations, the
        # number of reviewed/applied change records, and downstream refreshes.
        self._activity_metrics = {}
        labels = self.page.findChildren(QLabel)
        metric_captions = {
            "Exports": "syncs",
            "Validations": "changes",
            "Refreshes": "refreshes",
        }
        display_names = {
            "syncs": "Syncs",
            "changes": "Changes",
            "refreshes": "Refreshes",
        }
        for index, caption in enumerate(labels):
            metric_name = metric_captions.get(caption.text())
            if metric_name is None:
                continue
            value_label = None
            for candidate in reversed(labels[:index]):
                if (
                    candidate.parentWidget() is caption.parentWidget()
                    and candidate.objectName() == "AtOnceMetric"
                ):
                    value_label = candidate
                    break
            caption.setText(display_names[metric_name])
            if value_label is not None:
                self._activity_metrics[metric_name] = value_label

    def render(self, audits):
        """Render legacy XLSX sync evidence."""

        audits = list(audits or [])
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Started", "Export / rev", "Changes", "Refresh rev", "Status"]
        )
        self.table.setRowCount(len(audits))
        for row, audit in enumerate(reversed(audits)):
            values = (
                self._short_time(audit.started_at),
                f"{audit.export_id} / {audit.source_revision_number}",
                str(len(audit.changes)),
                str(audit.refresh_revision_number or "—"),
                audit.status.replace("_", " ").title(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                if column == 4 and audit.message:
                    item.setToolTip(audit.message)
                self.table.setItem(row, column, item)

        activity_counts = sync_activity_counts(audits)
        for metric_name, value_label in self._activity_metrics.items():
            value_label.setText(str(activity_counts[metric_name]))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setVisible(bool(audits))
        self.meta.setText(
            f"{len(audits)} persisted sync operation(s). Records survive project reopen."
            if audits
            else "No approved source sync has been recorded yet. Returned workbook archives are retained when available."
        )
        if self._legacy_empty is not None:
            self._legacy_empty.setVisible(not audits)

    def render_workflow(self, workflow, audits, graph_states, revisions):
        """Render active-workflow evidence without inventing audit records."""

        if workflow.effective_dependency_graph_origin() not in {
            "explicit",
            "freeform_explicit",
        }:
            self.render(audits)
            return

        states = list(graph_states or [])
        revisions = list(revisions or [])
        rows = []
        for state in states:
            revision = (
                f"revision {state.export_revision_number}"
                if state.export_revision_number is not None
                else "no delivery revision"
            )
            rows.append(
                (
                    state.created_at,
                    f"Graph run · {state.reason}",
                    str(len(state.refreshed_node_ids)),
                    str(len(state.stale_node_ids)),
                    revision,
                )
            )
        for revision in revisions:
            delivery_count = len(revision.delivery_outputs) + len(revision.outputs)
            rows.append(
                (
                    revision.created_at,
                    f"Export revision {revision.revision_number}",
                    str(delivery_count),
                    "—",
                    revision.status.replace("_", " ").title(),
                )
            )
        rows.sort(key=lambda item: str(item[0] or ""), reverse=True)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Recorded", "Evidence", "Refreshed", "Stale", "Status"]
        )
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(row, column, item)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setVisible(bool(rows))
        self.meta.setText(
            f"{len(states)} graph run(s) and {len(revisions)} delivery revision(s) recorded for this workflow."
            if rows
            else "No completed explicit graph run or delivery revision has been recorded."
        )
        if self._legacy_empty is not None:
            self._legacy_empty.setVisible(not rows)
        if "syncs" in self._activity_metrics:
            self._activity_metrics["syncs"].setText(str(len(revisions)))
        if "changes" in self._activity_metrics:
            self._activity_metrics["changes"].setText(str(len(states)))
        if "refreshes" in self._activity_metrics:
            self._activity_metrics["refreshes"].setText(
                str(sum(len(state.refreshed_node_ids) for state in states))
            )

    @staticmethod
    def _short_time(value):
        text = str(value or "")
        return text.replace("T", " ")[:19] if text else "—"
