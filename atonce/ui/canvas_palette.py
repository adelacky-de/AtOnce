"""Drag-only palette widgets for the AtOnce workflow canvas."""

from qgis.PyQt.QtCore import QMimeData, Qt
from qgis.PyQt.QtGui import QBrush, QDrag, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QWidget

from .canvas_theme import (
    BLOCK_BORDER,
    BLOCK_TEMPLATE_MIME,
    BLOCK_TEXT,
    PASTEL_GROUP,
    PASTEL_OPERATION,
    PASTEL_OUTPUT,
    PASTEL_SOURCE,
    PASTEL_TEXT,
)


class DraggableBlockTemplate(QWidget):
    """Compact template that creates content only by drag-and-drop."""

    WIDTHS = {"source": 68, "operation": 86, "output": 68, "text": 54, "group": 58}

    def __init__(self, block_kind, label, parent=None):
        super().__init__(parent)
        self.block_kind = str(block_kind)
        self.label = str(label)
        self._press_pos = None
        self.setFixedSize(self.WIDTHS.get(self.block_kind, 68), 38)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(f"Drag {self.label} into the canvas")

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if not (event.buttons() & Qt.LeftButton) or self._press_pos is None:
            super().mouseMoveEvent(event)
            return
        if (event.pos() - self._press_pos).manhattanLength() < 5:
            return
        mime = QMimeData()
        mime.setData(BLOCK_TEMPLATE_MIME, self.block_kind.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)
        self._press_pos = None
        self.setCursor(Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._press_pos = None
        self.setCursor(Qt.OpenHandCursor)
        event.accept()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(BLOCK_BORDER, 1.1))
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
            pen = QPen(BLOCK_BORDER, 1.1)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(BLOCK_TEXT)
        painter.drawText(self.rect(), Qt.AlignCenter, self.label)
