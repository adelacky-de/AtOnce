"""Presentation-only text and group graphics for the AtOnce canvas."""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QGraphicsItem, QGraphicsObject

from .canvas_theme import BLOCK_MUTED, BLOCK_TEXT, PASTEL_TEXT


class CanvasTextItem(QGraphicsObject):
    edit_requested = pyqtSignal(str)
    position_committed = pyqtSignal(str, QPointF)

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self.text_id = str(payload.get("id") or "")
        self.text = str(payload.get("text") or "Text note")
        self.setPos(float(payload.get("x", 80)), float(payload.get("y", 80)))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setZValue(5)

    def boundingRect(self):
        return QRectF(0, 0, 220, 72)

    def paint(self, painter, option, widget=None):  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#C5B770"), 1.0))
        painter.setBrush(QBrush(PASTEL_TEXT))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 6, 6)
        painter.setPen(BLOCK_TEXT)
        painter.drawText(
            QRectF(10, 8, 200, 56),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            self.text,
        )

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.edit_requested.emit(self.text_id)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        QGraphicsObject.mouseReleaseEvent(self, event)
        self.position_committed.emit(self.text_id, self.pos())


class GroupBoardItem(QGraphicsObject):
    moved_delta = pyqtSignal(str, QPointF)
    geometry_committed = pyqtSignal(str, float, float, float, float)
    rename_requested = pyqtSignal(str)

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self.group_id = str(payload.get("id") or "")
        self.title = str(payload.get("title") or "Group")
        self.width = max(220.0, float(payload.get("width", 460)))
        self.height = max(150.0, float(payload.get("height", 260)))
        self._last_pos = QPointF(float(payload.get("x", 60)), float(payload.get("y", 60)))
        self.setPos(self._last_pos)
        self._resizing = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(-20)

    def boundingRect(self):
        return QRectF(0, 0, self.width, self.height)

    def paint(self, painter, option, widget=None):  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#A5ADB2"), 1.2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(242, 243, 245, 90)))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 9, 9)
        painter.setPen(BLOCK_MUTED)
        painter.drawText(QRectF(12, 8, self.width - 24, 22), Qt.AlignLeft, self.title)
        painter.setPen(QPen(QColor("#A5ADB2"), 1.0))
        painter.drawLine(self.width - 13, self.height - 4, self.width - 4, self.height - 13)
        painter.drawLine(self.width - 9, self.height - 4, self.width - 4, self.height - 9)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.ItemPositionHasChanged:
            new_pos = QPointF(value)
            delta = new_pos - self._last_pos
            self._last_pos = new_pos
            if delta.x() or delta.y():
                self.moved_delta.emit(self.group_id, delta)
        return QGraphicsObject.itemChange(self, change, value)

    def mousePressEvent(self, event):  # noqa: N802
        point = event.pos()
        if (
            event.button() == Qt.LeftButton
            and point.x() >= self.width - 20
            and point.y() >= self.height - 20
        ):
            self._resizing = True
            event.accept()
            return
        QGraphicsObject.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._resizing:
            self.prepareGeometryChange()
            self.width = max(220.0, float(event.pos().x()))
            self.height = max(150.0, float(event.pos().y()))
            self.update()
            event.accept()
            return
        QGraphicsObject.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._resizing:
            self._resizing = False
            event.accept()
        else:
            QGraphicsObject.mouseReleaseEvent(self, event)
        self.geometry_committed.emit(
            self.group_id,
            float(self.pos().x()),
            float(self.pos().y()),
            self.width,
            self.height,
        )

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.rename_requested.emit(self.group_id)
        event.accept()
