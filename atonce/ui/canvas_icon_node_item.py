"""Pixel-locked MUI-icon executable canvas node presentation."""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPen

from ..models.dependency_graph import NodeKind
from .canvas_expand_icons import draw_expand_icon
from .canvas_hold_node import DragCanvasBlockItem as BaseDragCanvasBlockItem
from .canvas_icons import draw_block_icon
from .canvas_theme import BLOCK_BORDER, BLOCK_MUTED, BLOCK_TEXT


class DragCanvasBlockItem(BaseDragCanvasBlockItem):
    """Render exact-MUI icons, readable labels and provisional operation ports."""

    # Reserved non-overlapping slots: icon gutter, type label, delete control.
    NODE_ICON_SIZE = 22
    ICON_RECT = QRectF(12, 10, NODE_ICON_SIZE, NODE_ICON_SIZE)
    TYPE_LABEL_LEFT = 40
    DELETE_RESERVE = 34
    EXPAND_SIZE = 26

    def _icon_kind(self):
        if self.node.kind == NodeKind.SOURCE:
            return "source"
        if self.node.kind == NodeKind.DELIVERY:
            return "output"
        return "operation"

    def _operation_kind(self):
        return str(self.node.metadata.get("operation_kind") or "")

    def _type_label_rect(self):
        """Type caption sits after the icon and stops before the delete control."""

        width = max(24.0, self.WIDTH - self.TYPE_LABEL_LEFT - self.DELETE_RESERVE)
        return QRectF(self.TYPE_LABEL_LEFT, 10, width, 18)

    def _expand_rect(self):
        return QRectF(self.WIDTH - 48, 40, self.EXPAND_SIZE, self.EXPAND_SIZE)

    def _paint_type_icon(self, painter):
        """Suppress the base icon; this subclass paints it once with its text."""

        return None

    @staticmethod
    def _fit_text_font(painter, pixel_size, bold=False):
        font = painter.font()
        font.setPixelSize(pixel_size)
        font.setBold(bool(bold))
        return font

    def _paint_main_text(self, painter):
        original_font = painter.font()
        if self.node.kind == NodeKind.DERIVED:
            # Diamond safe band: icon | OPERATION label on one row; name below.
            # Delete control owns the top-right corner (WIDTH-28..WIDTH-8).
            icon_rect = QRectF(48, 30, 16, 16)
            draw_block_icon(
                painter,
                "operation",
                icon_rect,
                BLOCK_TEXT,
                operation_kind=self._operation_kind(),
            )

            painter.setFont(self._fit_text_font(painter, 8, True))
            type_rect = QRectF(68, 30, 72, 16)
            painter.setPen(BLOCK_TEXT)
            painter.drawText(type_rect, Qt.AlignLeft | Qt.AlignVCenter, "OPERATION")

            # Name stays below the icon/label row so glyphs never overlap.
            painter.setFont(self._fit_text_font(painter, 9, False))
            name_rect = QRectF(52, 50, self.WIDTH - 104, 28)
            painter.setPen(BLOCK_MUTED)
            painter.drawText(
                name_rect,
                Qt.AlignCenter | Qt.AlignVCenter | Qt.TextWordWrap,
                self._name_text(),
            )
            painter.setFont(original_font)
            return

        draw_block_icon(painter, self._icon_kind(), self.ICON_RECT, BLOCK_TEXT)
        painter.setPen(BLOCK_TEXT)
        painter.setFont(self._fit_text_font(painter, 10, True))
        painter.drawText(
            self._type_label_rect(),
            Qt.AlignLeft | Qt.AlignVCenter,
            self._type_text(),
        )

        painter.setFont(self._fit_text_font(painter, 10, False))
        painter.setPen(BLOCK_MUTED)
        if self.node.kind == NodeKind.SOURCE:
            expand = self._expand_rect()
            # Name stops before the expand control; icon stays in the top gutter.
            name_rect = QRectF(14, 36, expand.left() - 18, 34)
            painter.drawText(
                name_rect,
                Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap,
                self._name_text(),
            )
            draw_expand_icon(
                painter,
                self._fields_expanded,
                expand,
                BLOCK_TEXT,
            )
        else:
            name_rect = QRectF(14, 36, self.WIDTH - 28, 26)
            painter.drawText(
                name_rect,
                Qt.AlignCenter | Qt.AlignVCenter | Qt.TextWordWrap,
                self._name_text(),
            )

        if (
            self.node.kind == NodeKind.DELIVERY
            and str(self.node.metadata.get("data_type") or "") != "raster"
        ):
            included = bool(self.node.metadata.get("include_in_changes", True))
            status_rect = QRectF(14, 68, self.WIDTH - 28, 18)
            status = "Included in Changes" if included else "Excluded from Changes"
            painter.setFont(self._fit_text_font(painter, 9, False))
            painter.drawText(status_rect, Qt.AlignCenter | Qt.AlignVCenter, status)
        painter.setFont(original_font)

    def _paint_ports(self, painter):
        # Before a function is selected, OPERATION still exposes a generic input
        # and output so the user can establish context first.
        if self.node.kind == NodeKind.DERIVED and self.definition is None:
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.setPen(QPen(BLOCK_BORDER, 1.35))
            painter.drawEllipse(QPointF(4, self._input_y(0)), 6, 6)
            painter.drawEllipse(QPointF(self.WIDTH - 4, self._main_height() / 2), 6, 6)
            return
        super()._paint_ports(painter)

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        # The base legacy node intentionally suppresses ports while an operation
        # is unconfigured.  The drag-first canvas instead needs provisional
        # wiring to break the configure-before-connect circular dependency.
        if (
            event.button() == Qt.LeftButton
            and self.node.kind == NodeKind.DERIVED
            and self.definition is None
        ):
            point = event.pos()
            if point.x() <= 18 and abs(point.y() - self._input_y(0)) <= 14:
                self.port_pressed.emit(self.node.node_id, "in", "input")
                event.accept()
                return
            if point.x() >= self.WIDTH - 20 and abs(
                point.y() - self._main_height() / 2
            ) <= 18:
                self._port_drag = ("out", "result")
                self.port_pressed.emit(self.node.node_id, "out", "result")
                event.accept()
                return
        super().mousePressEvent(event)
