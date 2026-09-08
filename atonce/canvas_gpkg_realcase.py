"""Real-case GeoPackage and branding support for the canvas beta.

This module keeps the accepted canvas/lineage architecture intact while adding
production-safe GeoPackage behavior discovered during real-data testing:

* a GeoPackage is treated as a container whose vector or raster sublayer can be
  the SOURCE;
* SOURCE configuration can browse a .gpkg or raster file directly and lists
  loaded project rasters for the limited raster lane;
* GPKG OUTPUT preserves unrelated layers already present in the container;
* GPKG OUTPUT can never replace the physical GeoPackage used by a registered
  SOURCE in the same workflow;
* ownership/divergence is checked per AtOnce output layer semantically rather
  than by whole-container bytes; and
* Plugin Manager, toolbar, and dock use the high-contrast black/white mark.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

_APPLIED = False


def gpkg_layer_name(value):
    """Return a conservative GeoPackage table/layer name from user-facing text."""

    text = str(value or "").strip()
    if not text:
        return "AtOnce"
    text = re.sub(r"[^0-9A-Za-z_\- ]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("_-")
    return text[:63] or "AtOnce"


def _normalized_path(path):
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve()).casefold()
    except (OSError, RuntimeError, ValueError):
        return str(Path(text).expanduser().absolute()).casefold()


def _source_container_path(source_uri):
    """Return the physical .gpkg path from a normal QGIS OGR source URI."""

    text = str(source_uri or "").strip()
    if not text:
        return ""
    physical = text.split("|", 1)[0].strip()
    return physical if physical.lower().endswith(".gpkg") else ""


def _quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def _gpkg_tables(path):
    """Return GeoPackage content table names without loading a QGIS provider."""

    gpkg = Path(path).expanduser()
    if not gpkg.exists():
        return set()
    connection = sqlite3.connect(f"file:{gpkg.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='gpkg_contents'"
        ).fetchone()
        if row is None:
            raise ValueError(f"Existing output is not a valid GeoPackage: {gpkg}")
        return {
            str(item[0])
            for item in connection.execute(
                "SELECT table_name FROM gpkg_contents"
            ).fetchall()
        }
    finally:
        connection.close()


def _gpkg_fid_column(path, layer_name):
    gpkg = Path(path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            f"PRAGMA table_info({_quote_identifier(layer_name)})"
        ).fetchall()
        primary = [row for row in rows if int(row[5] or 0) > 0]
        if not primary:
            raise ValueError(
                f"GeoPackage layer {layer_name!r} has no primary-key column."
            )
        primary.sort(key=lambda row: int(row[5] or 0))
        return str(primary[0][1])
    finally:
        connection.close()


def _sqlite_backup(source_path, target_path):
    """Copy a GeoPackage safely, including committed SQLite WAL content."""

    source = Path(source_path).expanduser().resolve()
    target = Path(target_path).expanduser()
    target.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _recorded_gpkg_delivery_revision(store, workflow_id, delivery_id):
    loader = getattr(store, "load_export_revisions", None)
    if callable(loader):
        for revision in reversed(loader(workflow_id)):
            for output in revision.delivery_outputs:
                if (
                    output.delivery_id == delivery_id
                    and output.format == "gpkg"
                ):
                    return output
    return None


def _install_gpkg_source_drag_support():
    """Prefer vector sublayers when a QGIS GeoPackage/container drag has many URIs."""

    from .ui.canvas_view import DragFirstCanvasView
    from .ui.freeform_workflow_canvas import LAYER_TREE_MIME, _value

    original = DragFirstCanvasView._layers_from_qgis_mime

    def layers_from_qgis_mime(self, mime):
        layers = list(original(self, mime) or ())
        gateway = getattr(self, "gateway", None)
        if gateway is None:
            return layers

        if mime.hasFormat(LAYER_TREE_MIME):
            payload = bytes(mime.data(LAYER_TREE_MIME)).decode(
                "utf-8", "ignore"
            )
            candidates = []
            if hasattr(gateway, "source_candidate_layers"):
                candidates = list(gateway.source_candidate_layers())
            else:
                candidates = list(gateway.vector_layers())
                raster_layers = getattr(gateway, "raster_layers", None)
                if callable(raster_layers):
                    candidates.extend(list(raster_layers()))
            for candidate in candidates:
                candidate_id = str(
                    _value(getattr(candidate, "id", "")) or ""
                )
                candidate_name = str(
                    _value(getattr(candidate, "name", "")) or ""
                )
                if (
                    (candidate_id and candidate_id in payload)
                    or (candidate_name and candidate_name in payload)
                ) and candidate not in layers:
                    layers.append(candidate)

        vectors = [
            layer for layer in layers if gateway.is_vector_layer(layer)
        ]
        rasters = [
            layer
            for layer in layers
            if getattr(gateway, "is_raster_layer", lambda _layer: False)(layer)
            and layer not in vectors
        ]
        other = [
            layer
            for layer in layers
            if layer not in vectors and layer not in rasters
        ]
        return vectors + rasters + other

    DragFirstCanvasView._layers_from_qgis_mime = layers_from_qgis_mime


def _install_gpkg_source_dialog():
    """SOURCE dialog: pick a loaded vector or raster from the project dropdown."""

    from qgis.PyQt.QtWidgets import (
        QComboBox,
        QDialog,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
    )

    from .ui.canvas_block_dialogs import CanvasBlockDialogMixin
    from .ui.freeform_workflow_canvas import _value
    from .ui.styles import DIALOG_STYLESHEET

    def show_source_dialog(self, node):
        if self.gateway is None:
            self.message_requested.emit(
                "No QGIS layer gateway is available."
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Source")
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        title = QLabel("Select source layer", dialog)
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "Choose a loaded vector or raster layer from the current QGIS "
            "project (or drag one from the Layers panel). Raster sources "
            "support CRS conversion and GeoTIFF/KMZ output only.",
            dialog,
        )
        helper.setObjectName("AtOnceMuted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        combo = QComboBox(dialog)
        current_lineage = str(
            node.metadata.get("source_lineage_id") or ""
        )
        current_ref = self._source_refs.get(current_lineage)
        current_layer_id = (
            current_ref.current_layer_id if current_ref else ""
        )
        current_index = -1
        candidates = []
        if hasattr(self.gateway, "source_candidate_layers"):
            candidates = list(self.gateway.source_candidate_layers())
        else:
            candidates = list(self.gateway.vector_layers())
        for layer in candidates:
            layer_id = str(_value(getattr(layer, "id", "")) or "")
            label = str(
                _value(getattr(layer, "name", ""), "Unnamed layer")
            )
            try:
                kind = self.gateway.layer_data_type(layer)
            except Exception:
                kind = "vector" if self.gateway.is_vector_layer(layer) else "raster"
            combo.addItem(f"{label} ({kind})", layer)
            if layer_id == current_layer_id:
                current_index = combo.count() - 1
        if combo.count():
            combo.setCurrentIndex(
                current_index if current_index >= 0 else 0
            )
        else:
            combo.addItem("No loaded vector or raster layers", None)
            item = combo.model().item(0)
            if item is not None:
                item.setEnabled(False)
        layout.addWidget(combo)

        selected = {"layer": None}

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")

        def apply_loaded():
            layer = combo.currentData()
            if layer is None:
                self.message_requested.emit(
                    "Choose a loaded vector or raster layer from the list."
                )
                return
            selected["layer"] = layer
            dialog.accept()

        cancel.clicked.connect(dialog.reject)
        apply.clicked.connect(apply_loaded)
        row.addWidget(cancel)
        row.addWidget(apply)
        layout.addLayout(row)

        if (
            dialog.exec_() == QDialog.Accepted
            and selected["layer"] is not None
        ):
            self._bind_source(node.node_id, selected["layer"])

    # Loaded project rasters and vectors share the same SOURCE dropdown.
    # File-browse import buttons were removed; use Layers panel load + select.
    CanvasBlockDialogMixin._show_source_dialog = show_source_dialog


def _install_gpkg_output_dialog():
    """Expose GeoPackage as a normal OUTPUT format in the production canvas."""

    from dataclasses import replace

    from qgis.PyQt.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    from .core.freeform_graph import set_output_inclusion
    from .ui.canvas_block_dialogs import CanvasBlockDialogMixin
    from .ui.styles import DIALOG_STYLESHEET

    def show_output_dialog(self, node):
        dialog = QDialog(self)
        dialog.setWindowTitle("Output")
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        title = QLabel("Configure output")
        title.setObjectName("AtOnceDialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        data_type = self._upstream_data_type(node.node_id)
        is_raster = data_type == "raster"

        name_edit = QLineEdit(dialog)
        name_edit.setText(
            "" if node.name in {"", "Output"} else node.name
        )
        name_edit.setPlaceholderText("Output name")
        format_combo = QComboBox(dialog)
        format_choices = (
            (("GeoTIFF", "geotiff"), ("KMZ", "kmz"))
            if is_raster
            else (
                ("GeoPackage", "gpkg"),
                ("GeoJSON", "geojson"),
                ("Shapefile", "shapefile"),
                ("KML", "kml"),
                ("KMZ", "kmz"),
            )
        )
        for label, value in format_choices:
            format_combo.addItem(label, value)
        index = format_combo.findData(node.format)
        format_combo.setCurrentIndex(index if index >= 0 else 0)

        path_widget = QWidget(dialog)
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit(path_widget)
        path_edit.setText(str(node.metadata.get("path") or ""))
        path_edit.setPlaceholderText("Choose output path…")
        browse = QPushButton("Browse…", path_widget)
        path_layout.addWidget(path_edit, 1)
        path_layout.addWidget(browse)

        include_changes = None
        if not is_raster:
            include_changes = QCheckBox("Include in Changes", dialog)
            include_changes.setChecked(
                bool(node.metadata.get("include_in_changes", True))
            )
        gpkg_hint = QLabel(
            "For GeoPackage output, the Output name is used as the "
            "GeoPackage layer name. Other layers in an existing GeoPackage "
            "are preserved."
        )
        gpkg_hint.setObjectName("AtOnceMuted")
        gpkg_hint.setWordWrap(True)
        raster_hint = QLabel(
            "Raster outputs are written on Register/Run and are not part of Changes.",
            dialog,
        )
        raster_hint.setObjectName("AtOnceMuted")
        raster_hint.setWordWrap(True)
        raster_hint.setVisible(is_raster)

        form.addRow("Name", name_edit)
        form.addRow("Format", format_combo)
        form.addRow("Path", path_widget)
        if include_changes is not None:
            form.addRow("", include_changes)
        layout.addLayout(form)
        layout.addWidget(gpkg_hint)
        layout.addWidget(raster_hint)

        def update_hint():
            gpkg_hint.setVisible(
                (not is_raster)
                and str(format_combo.currentData() or "") == "gpkg"
            )

        def browse_path():
            fmt = str(format_combo.currentData() or "geojson")
            filters = {
                "gpkg": "GeoPackage (*.gpkg)",
                "geojson": "GeoJSON (*.geojson)",
                "shapefile": "Shapefile (*.shp)",
                "kml": "KML (*.kml)",
                "kmz": "KMZ (*.kmz)",
                "geotiff": "GeoTIFF (*.tif *.tiff)",
            }
            path, _ = QFileDialog.getSaveFileName(
                dialog,
                "Choose output path",
                path_edit.text(),
                filters[fmt],
            )
            if path:
                path_edit.setText(path)

        browse.clicked.connect(browse_path)
        format_combo.currentIndexChanged.connect(update_hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", dialog)
        apply = QPushButton("Apply", dialog)
        apply.setObjectName("AtOncePrimary")
        cancel.clicked.connect(dialog.reject)

        def apply_output():
            path = path_edit.text().strip()
            if not path:
                self.message_requested.emit(
                    "Choose an output path before applying this output."
                )
                return
            fmt = str(format_combo.currentData() or "geojson")
            included = False if is_raster else include_changes.isChecked()
            proposed_name = name_edit.text().strip() or (
                "AtOnce" if fmt == "gpkg" else fmt.upper()
            )
            if fmt == "gpkg" and Path(path).suffix.lower() != ".gpkg":
                path += ".gpkg"

            persisted = self._registered_output_node(node.node_id)
            if persisted is not None:
                old_name = str(persisted.name or "")
                old_path = str(
                    (persisted.metadata or {}).get("path") or ""
                )
                old_format = str(
                    persisted.format
                    or (persisted.metadata or {}).get("format")
                    or ""
                )
                if (
                    proposed_name != old_name
                    or path != old_path
                    or fmt != old_format
                ):
                    self.message_requested.emit(
                        "This output is already registered and cannot be "
                        "renamed, repointed, or changed to another format. "
                        "Add a new OUTPUT block if you need another output."
                    )
                    return

            metadata = dict(node.metadata)
            metadata.update(
                {
                    "path": path,
                    "format": fmt,
                    "include_in_changes": included,
                    "data_type": data_type,
                    "gpkg_layer_name": (
                        gpkg_layer_name(proposed_name)
                        if fmt == "gpkg"
                        else ""
                    ),
                }
            )
            updated = replace(
                node,
                name=proposed_name,
                format=fmt,
                metadata=metadata,
            )
            self._graph.nodes = [
                updated if item.node_id == node.node_id else item
                for item in self._graph.nodes
            ]
            self._graph = set_output_inclusion(
                self._graph, node.node_id, included
            )
            self._render_graph()
            self.graph_changed.emit()
            dialog.accept()

        apply.clicked.connect(apply_output)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        update_hint()
        dialog.adjustSize()
        dialog.exec_()

    CanvasBlockDialogMixin._show_output_dialog = show_output_dialog


def _install_gpkg_delivery_runtime():
    """Add forward GPKG delivery while preserving unrelated container layers."""

    from qgis.core import QgsVectorFileWriter

    from .core.export_snapshot import choose_gpkg_fid_column
    from .infrastructure.gpkg_runtime import geopackage_semantic_fingerprint
    from .infrastructure.qgis_gateway import QgisGateway
    from .models.export_revision import DeliveryOutputRevision
    from .services.export_service import ExportError, ExportService
    from .infrastructure.output_transaction import StagedOutput, staging_path_for

    def stage_geopackage_delivery(
        self,
        layer,
        staged_path,
        *,
        layer_name="AtOnce",
        base_path="",
    ):
        self.require_exportable_lineage(layer)
        staged = Path(staged_path)
        staged.unlink(missing_ok=True)
        base = Path(base_path).expanduser() if base_path else None
        if base is not None and base.is_file():
            _sqlite_backup(str(base), str(staged))

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = gpkg_layer_name(layer_name)
        options.fileEncoding = "UTF-8"
        options.layerOptions = [
            f"FID={choose_gpkg_fid_column(self.field_names(layer))}"
        ]
        options.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteLayer
            if staged.exists()
            else QgsVectorFileWriter.CreateOrOverwriteFile
        )
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            str(staged),
            self.project.transformContext(),
            options,
        )
        error = result[0]
        message = result[1] if len(result) > 1 else ""
        if error != QgsVectorFileWriter.NoError:
            raise RuntimeError(
                f"QGIS GPKG writer failed for '{staged}': {message or error}"
            )
        return int(layer.featureCount())

    QgisGateway.stage_geopackage_delivery = stage_geopackage_delivery

    @staticmethod
    def selected_deliveries(workflow, delivery_ids):
        if delivery_ids is None:
            selected = list(workflow.forward_deliveries)
        else:
            requested = {str(item) for item in delivery_ids}
            known = {item.delivery_id for item in workflow.forward_deliveries}
            unknown = requested - known
            if unknown:
                raise ExportError(
                    "Unknown forward delivery id(s): " + ", ".join(sorted(unknown))
                )
            selected = [
                item for item in workflow.forward_deliveries
                if item.delivery_id in requested
            ]
        supported = {"gpkg", "geojson", "shapefile", "kml", "kmz", "geotiff"}
        unsupported = [
            item.format for item in selected if item.format not in supported
        ]
        if unsupported:
            raise ExportError(
                "Unsupported forward delivery format(s): "
                + ", ".join(sorted(set(unsupported)))
            )
        return selected

    ExportService._selected_deliveries = selected_deliveries
    original_stage = ExportService._stage_forward_deliveries

    def stage_forward_deliveries(
        self,
        layer,
        deliveries,
        staged,
        delivery_revisions,
        *,
        layers_by_delivery_id=None,
    ):
        gpkg_deliveries = [item for item in deliveries if item.format == "gpkg"]
        other_deliveries = [
            item for item in deliveries if item.format != "gpkg"
        ]
        if other_deliveries:
            original_stage(
                self,
                layer,
                other_deliveries,
                staged,
                delivery_revisions,
                layers_by_delivery_id=layers_by_delivery_id,
            )

        for delivery in gpkg_deliveries:
            current_layer = (
                layers_by_delivery_id.get(delivery.delivery_id)
                if layers_by_delivery_id is not None
                else layer
            )
            if current_layer is None:
                raise ValueError(
                    f"GeoPackage output {delivery.name!r} has no "
                    "materialized upstream layer."
                )
            stage_path = staging_path_for(delivery.path)
            staged.append(StagedOutput(stage_path, delivery.path))
            layer_name = gpkg_layer_name(delivery.name)
            count = self.qgis.stage_geopackage_delivery(
                current_layer,
                stage_path,
                layer_name=layer_name,
                base_path=delivery.path,
            )
            fid = _gpkg_fid_column(stage_path, layer_name)
            fingerprint = geopackage_semantic_fingerprint(
                self.qgis.project,
                stage_path,
                layer_name,
                fid,
            )
            delivery_revisions.append(
                DeliveryOutputRevision(
                    delivery_id=delivery.delivery_id,
                    name=delivery.name,
                    format=delivery.format,
                    path=delivery.path,
                    feature_count=count,
                    # GPKG stores a per-layer semantic fingerprint here.
                    # Other tables in the same container are intentionally
                    # outside this output's ownership.
                    sha256=fingerprint,
                )
            )

    ExportService._stage_forward_deliveries = stage_forward_deliveries
    original_preflight = ExportService._preflight_targets

    def preflight_targets(
        self,
        workflow,
        layer_name,
        gpkg_fid_column,
        *,
        refresh_gpkg=True,
        exports=None,
        deliveries=None,
    ):
        selected_deliveries = list(
            workflow.forward_deliveries if deliveries is None else deliveries
        )
        gpkg_deliveries = [
            item for item in selected_deliveries if item.format == "gpkg"
        ]
        other_deliveries = [
            item for item in selected_deliveries if item.format != "gpkg"
        ]
        obsolete = original_preflight(
            self,
            workflow,
            layer_name,
            gpkg_fid_column,
            refresh_gpkg=refresh_gpkg,
            exports=exports,
            deliveries=other_deliveries,
        )

        # Keep one physical transaction per OUTPUT for this beta. This still
        # allows an AtOnce layer to coexist with unrelated user layers.
        paths = [_normalized_path(item.path) for item in gpkg_deliveries]
        if len(paths) != len(set(paths)):
            raise ValueError(
                "GeoPackage OUTPUT blocks in one run must use distinct file paths."
            )

        source_containers = {
            _normalized_path(_source_container_path(source.source_uri))
            for source in workflow.source_layers
            if _source_container_path(source.source_uri)
        }

        for delivery in gpkg_deliveries:
            path = Path(delivery.path).expanduser()
            key = _normalized_path(path)
            if not str(delivery.path).strip():
                raise ValueError(
                    "Every selected downstream output needs a configured path."
                )
            if path.suffix.lower() != ".gpkg":
                raise ValueError(f"GeoPackage output must use a .gpkg path: {path}")
            if not path.parent.exists() or not path.parent.is_dir():
                raise ValueError(
                    f"Output parent directory does not exist: {path.parent}"
                )
            if key in source_containers:
                raise ValueError(
                    f"GeoPackage output '{path}' is also a registered SOURCE "
                    "container. Choose a different output GeoPackage; AtOnce "
                    "will never replace a source database."
                )
            if not path.exists():
                continue

            tables = _gpkg_tables(path)
            target_layer = gpkg_layer_name(delivery.name)
            recorded = _recorded_gpkg_delivery_revision(
                self.store,
                workflow.workflow_id,
                delivery.delivery_id,
            )
            if recorded is None:
                if target_layer in tables:
                    raise ValueError(
                        f"GeoPackage layer '{target_layer}' already exists in {path} "
                        "and is not recorded as an AtOnce output. Choose another "
                        "Output name/path or remove that layer explicitly."
                    )
                continue

            if target_layer not in tables:
                raise ValueError(
                    f"AtOnce-owned GeoPackage layer '{target_layer}' is missing "
                    f"from {path}. Review the file before refreshing."
                )
            expected = str(recorded.sha256 or "")
            if not expected:
                raise ValueError(
                    f"AtOnce has no semantic fingerprint for GeoPackage layer "
                    f"'{target_layer}'. Review or move the existing output before "
                    "refreshing."
                )
            fid = _gpkg_fid_column(str(path), target_layer)
            current = geopackage_semantic_fingerprint(
                self.qgis.project,
                str(path),
                target_layer,
                fid,
            )
            if current != expected:
                raise ValueError(
                    "Refusing to replace modified AtOnce GeoPackage layer "
                    f"'{target_layer}' in {path}. That layer changed since the "
                    "recorded revision."
                )
        return obsolete

    ExportService._preflight_targets = preflight_targets


def _install_black_branding():
    from qgis.PyQt.QtGui import QIcon
    from qgis.PyQt.QtWidgets import QLabel

    from .plugin import AtOncePlugin
    from .ui.compact_dock import CompactAtOnceDockWidget

    icon_path = Path(__file__).resolve().parent / "resources" / "icon_black.svg"
    original_init = AtOncePlugin.initGui

    def init_gui(self):
        result = original_init(self)
        if self.action is not None and icon_path.is_file():
            self.action.setIcon(QIcon(str(icon_path)))
        return result

    AtOncePlugin.initGui = init_gui
    original_header = CompactAtOnceDockWidget._header

    def header(self):
        frame = original_header(self)
        logo = frame.findChild(QLabel, "AtOnceHeaderLogo")
        if logo is not None and icon_path.is_file():
            logo.setStyleSheet(
                "background:#000000; border-radius:8px; padding:3px;"
            )
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                logo.setPixmap(icon.pixmap(30, 30))
        return frame

    CompactAtOnceDockWidget._header = header


def apply_gpkg_realcase_support():
    """Install the real-case beta compatibility hooks exactly once."""

    global _APPLIED
    if _APPLIED:
        return
    _install_gpkg_source_drag_support()
    _install_gpkg_source_dialog()
    _install_gpkg_output_dialog()
    _install_gpkg_delivery_runtime()
    _install_black_branding()
    _APPLIED = True
