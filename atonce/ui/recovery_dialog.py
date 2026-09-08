"""Small explicit recovery dialogs for source/workbook rebinding."""

from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .styles import DIALOG_STYLESHEET


class SourceRecoveryDialog(QDialog):
    def __init__(self, workflow, vector_layers, mode="relink", parent=None):
        super().__init__(parent)
        self.workflow = workflow
        self.mode = mode
        self.setWindowTitle("Relink Source" if mode == "relink" else "Replace Source")
        self.resize(560, 300)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 15, 16, 15)
        root.setSpacing(10)

        title = QLabel("Relink an existing source" if mode == "relink" else "Replace authoritative source")
        title.setObjectName("AtOnceDialogTitle")
        root.addWidget(title)

        note = QLabel(
            "Relink keeps the existing immutable AtOnce source identity. The selected candidate must prove continuity through the historical _atonce_source_key UUIDs."
            if mode == "relink"
            else "Replace Source starts a new source-lineage generation. Older export revisions remain history but cannot be written back to the new authority."
        )
        note.setWordWrap(True)
        note.setObjectName("AtOnceDialogSubtitle")
        root.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.source_combo = QComboBox()
        for index, source in enumerate(workflow.source_layers, 1):
            suffix = source.stable_id[:8] if source.stable_id else "missing"
            self.source_combo.addItem(f"Source {index}: {source.name} · {suffix}", source.stable_id)
        form.addRow("Registered source", self.source_combo)

        self.candidate_combo = QComboBox()
        for layer in vector_layers:
            self.candidate_combo.addItem(
                f"{layer.name()} · {layer.providerType()}",
                layer.id(),
            )
        form.addRow("Loaded candidate", self.candidate_combo)
        root.addLayout(form)

        warning = QLabel(
            "AtOnce never accepts layer name, filename, geometry, grave_id/burial_ref, FID or row order as proof of source identity."
        )
        warning.setWordWrap(True)
        warning.setObjectName("AtOnceMuted")
        root.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        if ok is not None:
            ok.setText("Relink source" if mode == "relink" else "Review replacement")
            ok.setObjectName("AtOncePrimary" if mode == "relink" else "AtOnceDangerQuiet")
            ok.setEnabled(self.source_combo.count() > 0 and self.candidate_combo.count() > 0)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def source_lineage_id(self):
        return str(self.source_combo.currentData() or "")

    @property
    def candidate_layer_id(self):
        return str(self.candidate_combo.currentData() or "")


class WorkbookRecoveryDialog(QDialog):
    def __init__(self, workflow, parent=None):
        super().__init__(parent)
        self.workflow = workflow
        self.setWindowTitle("Relink workbook")
        self.resize(600, 260)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 15, 16, 15)
        root.setSpacing(10)

        title = QLabel("Relink moved XLSX")
        title.setObjectName("AtOnceDialogTitle")
        root.addWidget(title)
        note = QLabel(
            "Select the configured export and the moved workbook. AtOnce reads it read-only and accepts it only when workflow/export/revision metadata and immutable row lineage match a completed current-generation revision."
        )
        note.setWordWrap(True)
        note.setObjectName("AtOnceDialogSubtitle")
        root.addWidget(note)

        form = QFormLayout()
        self.export_combo = QComboBox()
        for export in workflow.exports:
            self.export_combo.addItem(export.name, export.export_id)
        form.addRow("Linked export", self.export_combo)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/path/to/moved-workbook.xlsx")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        form.addRow("Workbook", self.path_edit)
        form.addRow("", browse)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        if ok is not None:
            ok.setText("Validate & relink")
            ok.setObjectName("AtOncePrimary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self):
        initial = self.path_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select moved AtOnce workbook",
            initial,
            "Excel workbook (*.xlsx)",
        )
        if path:
            self.path_edit.setText(path)

    @property
    def export_id(self):
        return str(self.export_combo.currentData() or "")

    @property
    def workbook_path(self):
        return self.path_edit.text().strip()
