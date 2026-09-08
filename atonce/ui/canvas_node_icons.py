"""Small vector pictograms used by executable canvas nodes.

Icons are drawn with QPainter so they stay sharp at different QGIS/Qt scale
factors and do not require additional bitmap assets.
"""

from qgis.PyQt.QtCore import QPointF, QRectF
from qgis.PyQt.QtGui import QBrush, QPainterPath, QPen

from .canvas_theme import BLOCK_BORDER


def _pen():
    return QPen(BLOCK_BORDER, 1.25)


def draw_source_icon(painter, rect):
    """Draw a compact database/layer-stack source pictogram."""

    painter.save()
    painter.setPen(_pen())
    painter.setBrush(QBrush())
    top = QRectF(rect.left(), rect.top(), rect.width(), rect.height() * 0.34)
    painter.drawEllipse(top)
    left = rect.left()
    right = rect.right()
    y1 = top.center().y()
    y2 = rect.bottom() - top.height() / 2
    painter.drawLine(QPointF(left, y1), QPointF(left, y2))
    painter.drawLine(QPointF(right, y1), QPointF(right, y2))
    painter.drawArc(
        QRectF(left, y2 - top.height() / 2, rect.width(), top.height()),
        180 * 16,
        180 * 16,
    )
    mid = rect.top() + rect.height() * 0.58
    painter.drawArc(
        QRectF(left, mid - top.height() / 2, rect.width(), top.height()),
        180 * 16,
        180 * 16,
    )
    painter.restore()


def draw_operation_icon(painter, rect):
    """Draw a small diamond transformer pictogram."""

    painter.save()
    painter.setPen(_pen())
    painter.setBrush(QBrush())
    path = QPainterPath()
    path.moveTo(rect.center().x(), rect.top())
    path.lineTo(rect.right(), rect.center().y())
    path.lineTo(rect.center().x(), rect.bottom())
    path.lineTo(rect.left(), rect.center().y())
    path.closeSubpath()
    painter.drawPath(path)
    painter.restore()


def draw_output_icon(painter, rect):
    """Draw stacked output sheets/layers."""

    painter.save()
    painter.setPen(_pen())
    painter.setBrush(QBrush())
    inset = rect.width() * 0.18
    back = QRectF(rect.left() + inset, rect.top(), rect.width() - inset, rect.height() - inset)
    front = QRectF(rect.left(), rect.top() + inset, rect.width() - inset, rect.height() - inset)
    painter.drawRoundedRect(back, 1.5, 1.5)
    painter.drawRoundedRect(front, 1.5, 1.5)
    painter.restore()


def draw_node_icon(painter, node_kind, rect):
    """Dispatch to the pictogram for one executable node kind."""

    value = str(getattr(node_kind, "value", node_kind))
    if value == "source":
        draw_source_icon(painter, rect)
    elif value == "delivery":
        draw_output_icon(painter, rect)
    else:
        draw_operation_icon(painter, rect)
