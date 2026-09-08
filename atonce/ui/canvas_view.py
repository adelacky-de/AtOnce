"""Canvas view and lineage connector graphics for AtOnce."""

import re

from qgis.PyQt.QtCore import QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath, QPen

from .canvas_theme import BLOCK_TEMPLATE_MIME, LINEAGE_EXCLUDED, LINEAGE_INCLUDED
from .freeform_workflow_canvas import (
    CanvasConnectionItem,
    FreeformGraphicsView,
    LAYER_TREE_MIME,
    LAYER_URI_MIME,
    _value,
)


class DragFirstCanvasView(FreeformGraphicsView):
    template_dropped = pyqtSignal(str, QPointF)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_TEMPLATE_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_TEMPLATE_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        mime = event.mimeData()
        if mime.hasFormat(BLOCK_TEMPLATE_MIME):
            kind = bytes(mime.data(BLOCK_TEMPLATE_MIME)).decode("utf-8", "ignore").strip()
            if kind in {"source", "operation", "output", "text", "group"}:
                self.template_dropped.emit(kind, self.mapToScene(event.pos()))
                event.acceptProposedAction()
                return
            event.ignore()
            return
        if mime.hasFormat(LAYER_URI_MIME) or mime.hasFormat(LAYER_TREE_MIME):
            layers = self._layers_from_qgis_mime(mime)
            if layers:
                layer = layers[0]
                if not self.gateway.is_vector_layer(layer) and not self.gateway.is_raster_layer(
                    layer
                ):
                    self.invalid_drop.emit(
                        "Only loaded vector or raster layers can be used in an AtOnce workflow."
                    )
                    event.ignore()
                    return
                self.layer_dropped.emit(layer, self.mapToScene(event.pos()))
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def _layers_from_qgis_mime(self, mime):
        layers = []
        try:
            from qgis.core import QgsMimeDataUtils

            for uri in QgsMimeDataUtils.decodeUriList(mime):
                layer_id = str(_value(getattr(uri, "layerId", "")) or "")
                layer = self.gateway.resolve_layer(layer_id) if layer_id else None
                if layer is not None and layer not in layers:
                    layers.append(layer)
        except Exception:
            pass
        if layers or not mime.hasFormat(LAYER_TREE_MIME) or self.gateway is None:
            return layers
        payload = bytes(mime.data(LAYER_TREE_MIME)).decode("utf-8", "ignore")
        for candidate in self.gateway.source_candidate_layers():
            candidate_id = str(_value(getattr(candidate, "id", "")) or "")
            candidate_name = str(_value(getattr(candidate, "name", "")) or "")
            if candidate_id and candidate_id in payload:
                layers.append(candidate)
            elif candidate_name and candidate_name in payload:
                layers.append(candidate)
        if layers:
            return layers
        for layer_id in re.findall(r"(?:id|layerid)=[\"']([^\"']+)[\"']", payload):
            layer = self.gateway.resolve_layer(layer_id)
            if layer is not None and layer not in layers:
                layers.append(layer)
        return layers


class LineageConnectionItem(CanvasConnectionItem):
    def __init__(
        self,
        source_item,
        target_item,
        target_port="input",
        edge_id="",
        included=True,
        remove_callback=None,
    ):
        self.included = bool(included)
        super().__init__(
            source_item,
            target_item,
            target_port,
            edge_id,
            remove_callback=remove_callback,
        )
        pen = QPen(LINEAGE_INCLUDED if self.included else LINEAGE_EXCLUDED, 2.0)
        pen.setStyle(Qt.SolidLine if self.included else Qt.DashLine)
        self.setPen(pen)

    def update_path(self):
        path = QPainterPath(self.source_item.output_scene_pos())
        path.lineTo(self.target_item.input_scene_pos(self.target_port))
        self.setPath(path)
