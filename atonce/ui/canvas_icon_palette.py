"""Pixel-locked MUI drag templates with responsive toolbar sizing."""

from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QSizePolicy

from .canvas_icons import draw_block_icon
from .canvas_palette import DraggableBlockTemplate as BaseDraggableBlockTemplate
from .canvas_theme import (
    BLOCK_BORDER,
    BLOCK_TEXT,
    PASTEL_GROUP,
    PASTEL_OPERATION,
    PASTEL_OUTPUT,
    PASTEL_SOURCE,
    PASTEL_TEXT,
)


class DraggableBlockTemplate(BaseDraggableBlockTemplate):
    """Keep full labels visible while the toolbar follows the QGIS dock width."""

    WIDTHS = {
        "source": 102,
        "operation": 126,
        "output": 102,
        "text": 78,
        "group": 90,
    }
    MIN_WIDTHS = {
        "source": 82,
        "operation": 98,
        "output": 82,
        "text": 60,
        "group": 68,
    }
    HEIGHT = 40
    ICON_SIZE = 20

    def __init__(self, block_kind, label, parent=None):
        super().__init__(block_kind, label, parent)
        preferred = self.WIDTHS.get(self.block_kind, 102)
        minimum = self.MIN_WIDTHS.get(self.block_kind, 72)
        self.setMinimumSize(minimum, self.HEIGHT)
        self.setMaximumHeight(self.HEIGHT)
        self.setMaximumWidth(preferred)
        self.resize(preferred, self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(BLOCK_BORDER, 1.0))
        rect = self.rect().adjusted(1, 1, -1, -1)
        if self.block_kind == "source":
            painter.setBrush(QBrush(PASTEL_SOURCE))
            painter.drawRoundedRect(rect, 7, 7)
        elif self.block_kind == "output":
            painter.setBrush(QBrush(PASTEL_OUTPUT))
            painter.drawRoundedRect(rect, 7, 7)
        elif self.block_kind == "operation":
            painter.setBrush(QBrush(PASTEL_OPERATION))
            path = QPainterPath()
            path.moveTo(rect.center().x(), rect.top())
            path.lineTo(rect.right(), rect.center().y())
            path.lineTo(rect.center().x(), rect.bottom())
            path.lineTo(rect.left(), rect.center().y())
            path.closeSubpath()
            painter.drawPath(path)
        elif self.block_kind == "text":
            painter.setBrush(QBrush(PASTEL_TEXT))
            painter.drawRoundedRect(rect, 5, 5)
        else:
            painter.setBrush(QBrush(PASTEL_GROUP))
            pen = QPen(BLOCK_BORDER, 1.0)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 5, 5)

        original_font = painter.font()
        label_font = painter.font()
        label_font.setPixelSize(9 if self.width() >= 92 else 8)
        label_font.setBold(False)
        painter.setFont(label_font)
        painter.setPen(BLOCK_TEXT)

        if self.block_kind == "operation":
            # Diamond labels get a dedicated central safe band rather than the
            # rectangular template layout used by other block types.
            icon_size = 14 if self.width() < 112 else 16
            icon_x = max(18.0, self.width() * 0.24)
            draw_block_icon(
                painter,
                self.block_kind,
                QRectF(icon_x, (self.height() - icon_size) / 2, icon_size, icon_size),
                BLOCK_TEXT,
            )
            text_left = icon_x + icon_size + 4
            text_right = self.width() - max(13.0, self.width() * 0.14)
            painter.drawText(
                QRectF(text_left, 0, max(1, text_right - text_left), self.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                self.label,
            )
        else:
            icon_size = 16 if self.width() < 78 else 18
            icon_rect = QRectF(7, (self.height() - icon_size) / 2, icon_size, icon_size)
            draw_block_icon(painter, self.block_kind, icon_rect, BLOCK_TEXT)
            text_left = 28 if self.width() < 78 else 31
            painter.drawText(
                QRectF(text_left, 0, max(1, self.width() - text_left - 4), self.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                self.label,
            )
        painter.setFont(original_font)
