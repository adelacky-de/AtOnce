"""Executable SOURCE/OPERATION/OUTPUT graphics for the AtOnce canvas."""

from qgis.PyQt.QtCore import QElapsedTimer, QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QGraphicsItem, QGraphicsObject

from ..models.dependency_graph import NodeKind
from .canvas_node_icons import draw_node_icon
from .canvas_theme import (
    BLOCK_BORDER,
    BLOCK_MUTED,
    BLOCK_TEXT,
    FIELD_BG,
    FIELD_BORDER,
    PASTEL_OPERATION,
    PASTEL_OUTPUT,
    PASTEL_SOURCE,
)
from .freeform_workflow_canvas import CanvasBlockItem


class DragCanvasBlockItem(CanvasBlockItem):
    """Fixed main node plus attached FME-style field rows.

    Field rows never enlarge the executable node shape itself. SOURCE/OUTPUT use
    compact rectangles; OPERATION uses a taller diamond with a dedicated central
    safe text band so labels never cross the sloped polygon edges. A quick click
    opens configuration; a 1.5-second hold unlocks block movement.
    """

    position_committed = pyqtSignal(str, QPointF)
    port_dragged = pyqtSignal(str, QPointF)
    source_field_toggled = pyqtSignal(str, str, bool)

    WIDTH = 202
    SOURCE_HEIGHT = 86
    OPERATION_HEIGHT = 106
    OUTPUT_HEIGHT = 102
    FIELD_ROW = 22
    FIELD_GAP = 2
    FIELD_PADDING = 9
    MOVE_HOLD_MS = 1500

    def __init__(
        self,
        node,
        definition=None,
        *,
        available_fields=(),
        selected_fields=(),
        operation_fields=(),
        fields_expanded=False,
        parent=None,
    ):
        super().__init__(node, definition, parent)
        self.available_fields = tuple(str(item) for item in available_fields)
        self.selected_fields = list(
            dict.fromkeys(str(item) for item in selected_fields if item)
        )
        self.operation_fields = tuple(str(item) for item in operation_fields if item)
        self._fields_expanded = bool(fields_expanded)
        self._normal_press_pos = None
        self._hold_timer = None
        self._move_unlocked = False

    def _is_unconfigured_operation(self):
        return self.node.kind == NodeKind.DERIVED and not str(
            self.node.metadata.get("operation_kind") or ""
        )

    def _field_lines(self):
        if self.node.kind == NodeKind.SOURCE:
            return (
                self.available_fields
                if self._fields_expanded
                else tuple(self.selected_fields)
            )
        if self.node.kind == NodeKind.DERIVED:
            return self.operation_fields
        return ()

    def _main_height(self):
        if self.node.kind == NodeKind.DELIVERY:
            return self.OUTPUT_HEIGHT
        if self.node.kind == NodeKind.DERIVED:
            return self.OPERATION_HEIGHT
        return self.SOURCE_HEIGHT

    def _field_top(self):
        return self._main_height() + self.FIELD_GAP

    def _field_rect(self, index):
        return QRectF(
            0,
            self._field_top() + index * (self.FIELD_ROW + 1),
            self.WIDTH,
            self.FIELD_ROW,
        )

    def _fill(self):
        if self.node.kind == NodeKind.SOURCE:
            return PASTEL_SOURCE
        if self.node.kind == NodeKind.DELIVERY:
            return PASTEL_OUTPUT
        return PASTEL_OPERATION

    def _type_text(self):
        if self.node.kind == NodeKind.SOURCE:
            return "SOURCE"
        if self.node.kind == NodeKind.DELIVERY:
            return "OUTPUT"
        return "OPERATION"

    def _name_text(self):
        if self.node.kind == NodeKind.SOURCE:
            return (
                self.node.name
                if self.node.metadata.get("source_lineage_id")
                else "Select layer"
            )
        if self.node.kind == NodeKind.DELIVERY:
            return self.node.name or "Output"
        return self.definition.title if self.definition is not None else "Select function"

    def boundingRect(self):
        field_count = len(self._field_lines())
        height = self._main_height()
        if field_count:
            height += self.FIELD_GAP + field_count * (self.FIELD_ROW + 1) - 1
        return QRectF(0, 0, self.WIDTH, height)

    def _input_y(self, index=0):
        ports = len(self.definition.input_ports) if self.definition else 0
        main_height = self._main_height()
        if ports <= 1:
            return main_height / 2
        usable = main_height - 32
        return 16 + usable * (index + 1) / (ports + 1)

    def input_scene_pos(self, port_id="input"):
        if self.node.kind == NodeKind.SOURCE or self._is_unconfigured_operation():
            return self.mapToScene(QPointF(0, self._main_height() / 2))
        index = 0
        if self.definition:
            index = next(
                (
                    i
                    for i, port in enumerate(self.definition.input_ports)
                    if port.port_id == port_id
                ),
                0,
            )
        return self.mapToScene(QPointF(4, self._input_y(index)))

    def output_scene_pos(self):
        return self.mapToScene(QPointF(self.WIDTH - 4, self._main_height() / 2))

    @staticmethod
    def _elided(painter, text, width):
        return painter.fontMetrics().elidedText(
            str(text), Qt.ElideRight, max(1, int(width))
        )

    def _paint_main_shape(self, painter, main_rect):
        painter.setPen(QPen(BLOCK_BORDER, 1.35))
        painter.setBrush(QBrush(self._fill()))
        if self.node.kind == NodeKind.DERIVED:
            path = QPainterPath()
            path.moveTo(main_rect.center().x(), main_rect.top())
            path.lineTo(main_rect.right(), main_rect.center().y())
            path.lineTo(main_rect.center().x(), main_rect.bottom())
            path.lineTo(main_rect.left(), main_rect.center().y())
            path.closeSubpath()
            painter.drawPath(path)
            return
        painter.drawRoundedRect(main_rect, 9, 9)

    def _paint_type_icon(self, painter):
        """Draw a small node-type pictogram without competing with field rows."""

        if self.node.kind == NodeKind.DERIVED:
            rect = QRectF(56, 31, 15, 15)
        else:
            rect = QRectF(20, 11, 18, 18)
        draw_node_icon(painter, self.node.kind, rect)

    def _paint_main_text(self, painter):
        if self.node.kind == NodeKind.DERIVED:
            # Keep icon + label inside the diamond's wide central band.
            type_rect = QRectF(77, 30, 70, 18)
            name_rect = QRectF(54, 52, self.WIDTH - 108, 22)
            painter.setPen(BLOCK_TEXT)
            painter.drawText(
                type_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                self._elided(painter, self._type_text(), type_rect.width()),
            )
            painter.setPen(BLOCK_MUTED)
            painter.drawText(
                name_rect,
                Qt.AlignCenter | Qt.AlignVCenter,
                self._elided(painter, self._name_text(), name_rect.width()),
            )
            return

        painter.setPen(BLOCK_TEXT)
        type_rect = QRectF(46, 10, self.WIDTH - 66, 20)
        painter.drawText(
            type_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            self._elided(painter, self._type_text(), type_rect.width()),
        )
        painter.setPen(BLOCK_MUTED)
        name_rect = QRectF(22, 34, self.WIDTH - 44, 24)
        if self.node.kind == NodeKind.SOURCE:
            name_rect.setWidth(self.WIDTH - 68)
        name = self._elided(painter, self._name_text(), name_rect.width())
        painter.drawText(name_rect, Qt.AlignCenter | Qt.AlignVCenter, name)
        if self.node.kind == NodeKind.SOURCE:
            painter.setPen(BLOCK_TEXT)
            painter.drawText(
                QRectF(self.WIDTH - 40, 34, 22, 22),
                Qt.AlignCenter,
                "▴" if self._fields_expanded else "▾",
            )

        if (
            self.node.kind == NodeKind.DELIVERY
            and str(self.node.metadata.get("data_type") or "") != "raster"
        ):
            included = bool(self.node.metadata.get("include_in_changes", True))
            status_rect = QRectF(18, 66, self.WIDTH - 36, 20)
            status = "Included in Changes" if included else "Excluded from Changes"
            painter.drawText(
                status_rect,
                Qt.AlignCenter | Qt.AlignVCenter,
                self._elided(painter, status, status_rect.width()),
            )

    def _paint_field_rows(self, painter):
        original_font = painter.font()
        field_font = painter.font()
        field_font.setPixelSize(11)
        painter.setFont(field_font)
        for index, field in enumerate(self._field_lines()):
            rect = self._field_rect(index).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(FIELD_BORDER, 1.0))
            painter.setBrush(QBrush(FIELD_BG))
            painter.drawRoundedRect(rect, 3, 3)
            text = str(field)
            if self.node.kind == NodeKind.SOURCE and self._fields_expanded:
                text = ("☑ " if field in self.selected_fields else "☐ ") + text
            text_rect = rect.adjusted(self.FIELD_PADDING, 0, -self.FIELD_PADDING, 0)
            painter.setPen(BLOCK_MUTED)
            painter.drawText(
                text_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                self._elided(painter, text, text_rect.width()),
            )
        painter.setFont(original_font)

    def _paint_ports(self, painter):
        if self.node.kind == NodeKind.DELIVERY:
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.setPen(QPen(BLOCK_BORDER, 1.2))
            painter.drawEllipse(QPointF(4, self._input_y(0)), 5, 5)
        elif self.node.kind == NodeKind.DERIVED and self.definition is not None:
            for index, _port in enumerate(self.definition.input_ports):
                painter.setBrush(QBrush(QColor("#FFFFFF")))
                painter.setPen(QPen(BLOCK_BORDER, 1.2))
                painter.drawEllipse(QPointF(4, self._input_y(index)), 5, 5)

        if self.node.kind == NodeKind.SOURCE or (
            self.node.kind == NodeKind.DERIVED and self.definition is not None
        ):
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.setPen(QPen(BLOCK_BORDER, 1.2))
            painter.drawEllipse(self.output_scene_pos() - self.scenePos(), 5, 5)

    def paint(self, painter, option, widget=None):  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing)
        main_rect = QRectF(0, 0, self.WIDTH, self._main_height()).adjusted(
            1, 1, -1, -1
        )
        self._paint_main_shape(painter, main_rect)
        self._paint_type_icon(painter)
        self._paint_main_text(painter)
        self._paint_field_rows(painter)
        self._paint_ports(painter)

        if self._stale:
            painter.setPen(QPen(QColor("#B42318"), 1.0))
            painter.setBrush(QBrush(QColor("#D92D20")))
            painter.drawEllipse(QPointF(17, 17), 8, 8)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(QRectF(12, 9, 10, 16), Qt.AlignCenter, "!")
        self._paint_delete_control(painter)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.moved.emit(self.node.node_id)
        return QGraphicsObject.itemChange(self, change, value)

    def _field_index_at(self, point):
        if point.y() < self._field_top():
            return -1
        relative = point.y() - self._field_top()
        row_span = self.FIELD_ROW + 1
        index = int(relative // row_span)
        if relative - index * row_span > self.FIELD_ROW:
            return -1
        return index

    def _start_hold_timer(self):
        self._hold_timer = QElapsedTimer()
        self._hold_timer.start()
        self._move_unlocked = False

    def _hold_elapsed(self):
        return self._hold_timer is not None and self._hold_timer.elapsed() >= self.MOVE_HOLD_MS

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self._delete_hit(event.pos()):
            self.delete_requested.emit(self.node.node_id)
            event.accept()
            return

        if event.button() == Qt.LeftButton and self.node.kind == NodeKind.SOURCE:
            point = event.pos()
            if self.WIDTH - 48 <= point.x() <= self.WIDTH - 10 and 30 <= point.y() <= 61:
                self.prepareGeometryChange()
                self._fields_expanded = not self._fields_expanded
                self.update()
                self.moved.emit(self.node.node_id)
                event.accept()
                return
            if self._fields_expanded:
                index = self._field_index_at(point)
                if 0 <= index < len(self.available_fields):
                    field = self.available_fields[index]
                    checked = field not in self.selected_fields
                    if checked:
                        self.selected_fields.append(field)
                    else:
                        self.selected_fields = [
                            item for item in self.selected_fields if item != field
                        ]
                    self.source_field_toggled.emit(self.node.node_id, field, checked)
                    self.update()
                    event.accept()
                    return

        if event.button() == Qt.LeftButton:
            point = event.pos()
            if self.node.kind != NodeKind.SOURCE and not self._is_unconfigured_operation():
                port_count = len(self.definition.input_ports) if self.definition else 1
                if point.x() <= 14 and point.y() <= self._main_height():
                    for index in range(port_count):
                        if abs(point.y() - self._input_y(index)) <= 10:
                            port_id = (
                                self.definition.input_ports[index].port_id
                                if self.definition
                                else "input"
                            )
                            self.port_pressed.emit(self.node.node_id, "in", port_id)
                            event.accept()
                            return
            if (
                point.x() >= self.WIDTH - 16
                and point.y() <= self._main_height()
                and self.node.kind != NodeKind.DELIVERY
                and not self._is_unconfigured_operation()
            ):
                self._port_drag = ("out", "result")
                self.port_pressed.emit(self.node.node_id, "out", "result")
                event.accept()
                return
            self._normal_press_pos = QPointF(self.pos())
            self._start_hold_timer()
        QGraphicsObject.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._port_drag is not None:
            self.port_dragged.emit(self.node.node_id, self.mapToScene(event.pos()))
            event.accept()
            return
        if self._normal_press_pos is not None and not self._move_unlocked:
            if not self._hold_elapsed():
                event.accept()
                return
            self._move_unlocked = True
        QGraphicsObject.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._port_drag is not None:
            direction, port_id = self._port_drag
            self._port_drag = None
            self.port_released.emit(
                self.node.node_id,
                direction,
                port_id,
                self.mapToScene(event.pos()),
            )
            event.accept()
            return

        start = self._normal_press_pos
        move_unlocked = self._move_unlocked
        QGraphicsObject.mouseReleaseEvent(self, event)
        moved = (
            move_unlocked
            and start is not None
            and (self.pos() - start).manhattanLength() > 2
        )
        self._normal_press_pos = None
        self._hold_timer = None
        self._move_unlocked = False
        if moved:
            self.position_committed.emit(self.node.node_id, self.pos())
        else:
            self.clicked.emit(self.node.node_id)
