"""Changes-tab controller for XLSX scans, approvals and trace selection."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidgetItem,
)

from ..models.change import ChangeDisposition


class ChangeScanPanel:
    def __init__(self, dock, scan_callback, review_callback=None):
        self.dock = dock
        self.scan_callback = scan_callback
        self.review_callback = review_callback
        self.last_result = None
        self.row_changes = []
        self.card = dock.changes_table.parentWidget()
        self.layout = self.card.layout()

        table_index = self.layout.indexOf(dock.changes_table)
        empty_item = self.layout.itemAt(table_index + 1) if table_index >= 0 else None
        self.empty_widget = empty_item.widget() if empty_item is not None else None

        # Replace foundation-era copy without coupling the dock layout to sync logic.
        for label in dock.findChildren(QLabel):
            if label.text() == "Spreadsheet edits will be compared against linked export snapshots.":
                label.setText(
                    "Scan linked XLSX values against the recorded baseline, review Writable rows, or trace any resolved change to its source."
                )
            elif label.text() == "Change detection will appear here after linked XLSX snapshots and comparison are implemented.":
                label.setText("Choose a linked export and scan its returned XLSX to review real changes.")

        self.count_buttons = {}
        for button in self.card.findChildren(QPushButton):
            text = button.text()
            for key in ("All", "Writable", "Conflicts", "Blocked"):
                if text.startswith(key):
                    self.count_buttons[key] = button

        self.trace_button = next(
            (button for button in dock.findChildren(QPushButton) if button.text() == "Trace source"),
            None,
        )
        if self.trace_button is not None:
            self.trace_button.setEnabled(False)
            self.trace_button.setToolTip(
                "Select exactly one resolved change row to navigate to its immutable source UUID."
            )

        self.review_button = next(
            (button for button in dock.findChildren(QPushButton) if button.text() == "Review changes"),
            None,
        )
        if self.review_button is not None:
            self.review_button.setEnabled(False)
            self.review_button.setObjectName("AtOnceQuiet")
            if review_callback is not None:
                self.review_button.clicked.connect(self._review)

        self.sync_button = next(
            (button for button in dock.findChildren(QPushButton) if button.text() == "Sync downstream"),
            None,
        )

        self.toolbar = QHBoxLayout()
        self.toolbar.setSpacing(7)
        label = QLabel("Linked export")
        label.setObjectName("AtOnceMuted")
        self.export_combo = QComboBox()
        self.export_combo.setMinimumWidth(150)
        self.scan_button = QPushButton("Scan edited XLSX")
        self.scan_button.setObjectName("AtOncePrimary")
        self.scan_button.setEnabled(False)
        self.scan_button.clicked.connect(self._scan)
        self.scan_meta = QLabel("Export a baseline before scanning.")
        self.scan_meta.setObjectName("AtOnceMuted")
        self.scan_meta.setWordWrap(True)

        self.toolbar.addWidget(label)
        self.toolbar.addWidget(self.export_combo, 1)
        self.toolbar.addWidget(self.scan_button)
        insertion = max(0, table_index)
        self.layout.insertLayout(insertion, self.toolbar)
        self.layout.insertWidget(insertion + 1, self.scan_meta)

        table = self.dock.changes_table
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels(
            ["Approve", "Source", "Feature", "Field", "Baseline", "Source now", "XLSX", "Status"]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setSortingEnabled(True)
        table.itemChanged.connect(self._approval_changed)
        table.itemSelectionChanged.connect(self._trace_selection_changed)

    def set_exports(
        self,
        exports,
        has_completed_revision=False,
        *,
        enabled=True,
        disabled_message=None,
    ):
        current = self.export_combo.currentData()
        self.export_combo.blockSignals(True)
        self.export_combo.clear()
        for export in exports or []:
            self.export_combo.addItem(export.name or export.export_id, export.export_id)
        if current:
            index = self.export_combo.findData(current)
            if index >= 0:
                self.export_combo.setCurrentIndex(index)
        self.export_combo.blockSignals(False)
        can_scan = bool(enabled) and bool(exports) and bool(has_completed_revision)
        self.scan_button.setEnabled(can_scan)
        if self.sync_button is not None and not enabled:
            self.sync_button.setEnabled(False)
        if not enabled:
            self._set_review_enabled(False)
            self._set_trace_enabled(False)
        self.scan_meta.setText(
            "Compare workbook cell values with the recorded export baseline. Scanning and trace navigation never write source data."
            if can_scan
            else disabled_message
            if disabled_message
            else "Complete an AtOnce XLSX export revision before scanning edits."
        )

    def render(self, result):
        self.last_result = result
        self.row_changes = list(result.changes)
        table = self.dock.changes_table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.clearSelection()
        table.setRowCount(len(result.changes))
        counts = result.counts
        blocked = counts[ChangeDisposition.DERIVED] + counts[ChangeDisposition.UNRESOLVED]
        labels = {
            "All": len(result.changes),
            "Writable": counts[ChangeDisposition.WRITABLE],
            "Conflicts": counts[ChangeDisposition.CONFLICT],
            "Blocked": blocked,
        }
        for key, value in labels.items():
            button = self.count_buttons.get(key)
            if button is not None:
                button.setText(f"{key} {value}")

        for change_index, change in enumerate(result.changes):
            approval = QTableWidgetItem("")
            approval.setData(Qt.UserRole, change_index)
            if change.disposition == ChangeDisposition.WRITABLE:
                approval.setFlags(
                    Qt.ItemIsEnabled
                    | Qt.ItemIsSelectable
                    | Qt.ItemIsUserCheckable
                )
                approval.setCheckState(Qt.Unchecked)
                approval.setToolTip("Explicitly select this Writable change for final review.")
            else:
                approval.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                approval.setText("—")
                approval.setToolTip(f"{change.disposition.value.title()} changes cannot be approved.")
            table.setItem(change_index, 0, approval)

            field_label = change.field_name
            if change.source_field_name and change.source_field_name != change.field_name:
                field_label = f"{change.field_name} → {change.source_field_name}"
            cells = (
                change.source_layer_name or change.source_layer_id or "—",
                self._feature_label(change.source_feature_key),
                field_label,
                self._display(change.old_value),
                self._display(change.current_source_value),
                self._display(change.new_value),
                change.disposition.value.title(),
            )
            for offset, value in enumerate(cells, start=1):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, change_index)
                if offset == 7 and change.reason:
                    item.setToolTip(change.reason)
                if offset in (1, 2, 3, 7):
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                table.setItem(change_index, offset, item)

        table.setSortingEnabled(True)
        table.blockSignals(False)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        table.setVisible(bool(result.changes))
        if self.empty_widget is not None:
            self.empty_widget.setVisible(not result.changes)
        self.scan_meta.setText(
            f"Scanned {result.scanned_rows} row(s) against revision {result.revision_number} · "
            f"{len(result.changes)} change(s) classified. Select one resolved row to trace, or check Writable rows for review."
        )
        self._approval_changed()
        self._trace_selection_changed()

    def selected_writable_changes(self):
        selected = []
        table = self.dock.changes_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            change = self._change_from_item(item)
            if change is None or change.disposition != ChangeDisposition.WRITABLE:
                continue
            if item.checkState() == Qt.Checked:
                selected.append(change)
        return selected

    def selected_trace_change(self):
        """Return the authoritative selected change, independent of visible table order."""

        table = self.dock.changes_table
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if len(rows) != 1:
            return None
        item = table.item(rows[0].row(), 0)
        change = self._change_from_item(item)
        if change is None or change.disposition == ChangeDisposition.UNRESOLVED:
            return None
        if not str(change.source_layer_id or "").strip():
            return None
        if not str(change.source_feature_key or "").strip():
            return None
        return change

    def clear(self):
        self.last_result = None
        self.row_changes = []
        table = self.dock.changes_table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.clearSelection()
        table.setRowCount(0)
        table.setSortingEnabled(True)
        table.blockSignals(False)
        table.setVisible(False)
        if self.empty_widget is not None:
            self.empty_widget.setVisible(True)
        for key, button in self.count_buttons.items():
            button.setText(f"{key} 0")
        self._set_review_enabled(False)
        self._set_trace_enabled(False)

    def _scan(self):
        export_id = self.export_combo.currentData()
        if export_id:
            self.scan_callback(export_id)

    def _review(self):
        selected = self.selected_writable_changes()
        if self.last_result is not None and selected and self.review_callback is not None:
            self.review_callback(self.last_result, selected)

    def _approval_changed(self, _item=None):
        self._set_review_enabled(bool(self.selected_writable_changes()))

    def _trace_selection_changed(self):
        self._set_trace_enabled(self.selected_trace_change() is not None)

    def _set_review_enabled(self, enabled):
        if self.review_button is None:
            return
        self.review_button.setEnabled(bool(enabled))
        # A disabled review action must not visually look like the primary action.
        self.review_button.setObjectName("AtOncePrimary" if enabled else "AtOnceQuiet")
        style = self.review_button.style()
        style.unpolish(self.review_button)
        style.polish(self.review_button)

    def _set_trace_enabled(self, enabled):
        if self.trace_button is None:
            return
        self.trace_button.setEnabled(bool(enabled))
        self.trace_button.setToolTip(
            "Navigate to the selected change's exact source feature by immutable UUID."
            if enabled
            else "Select exactly one resolved change row to trace its source."
        )

    def _change_from_item(self, item):
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if not isinstance(index, int) or index < 0 or index >= len(self.row_changes):
            return None
        return self.row_changes[index]

    @staticmethod
    def _feature_label(source_key):
        value = str(source_key or "")
        if not value:
            return "—"
        return value if len(value) <= 14 else value[:12] + "…"

    @staticmethod
    def _display(value):
        if value is None:
            return "—"
        if isinstance(value, dict):
            return "{…}"
        text = str(value)
        return text if len(text) <= 80 else text[:77] + "…"
