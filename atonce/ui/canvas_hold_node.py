"""Long-press movement policy for executable canvas nodes."""

from qgis.PyQt.QtCore import Qt

from ..models.dependency_graph import NodeKind
from .canvas_gestures import HoldToMoveGesture
from .canvas_node_item import DragCanvasBlockItem as _BaseDragCanvasBlockItem


class DragCanvasBlockItem(_BaseDragCanvasBlockItem):
    """Quick click configures; holding 1.5 seconds arms node movement."""

    HOLD_TO_MOVE_MS = 1500

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hold_move = HoldToMoveGesture(self.HOLD_TO_MOVE_MS)

    def _is_immediate_control(self, point):
        """Ports and SOURCE field controls must react without the hold delay."""

        if self._delete_hit(point):
            return True

        if self.node.kind == NodeKind.SOURCE:
            # Match the expand control slot painted by the icon node item.
            if self.WIDTH - 52 <= point.x() <= self.WIDTH - 18 and 36 <= point.y() <= 70:
                return True
            if self._fields_expanded and self._field_index_at(point) >= 0:
                return True

        # Every OPERATION has a usable generic input/output before its function
        # is selected.  Once configured, the same hit area resolves to the
        # registry-declared named ports.
        if self.node.kind == NodeKind.DERIVED:
            port_count = len(self.definition.input_ports) if self.definition else 1
            if point.x() <= 18 and point.y() <= self._main_height():
                for index in range(port_count):
                    if abs(point.y() - self._input_y(index)) <= 12:
                        return True

        if (
            point.x() >= self.WIDTH - 20
            and point.y() <= self._main_height()
            and self.node.kind != NodeKind.DELIVERY
        ):
            return True
        return False

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        if self._is_immediate_control(event.pos()):
            self._hold_move.cancel()
            super().mousePressEvent(event)
            return

        self._hold_move.begin(self.pos(), event.scenePos())
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API
        if self._port_drag is not None:
            super().mouseMoveEvent(event)
            return
        if self._hold_move.active and (event.buttons() & Qt.LeftButton):
            target = self._hold_move.position_for(event.scenePos())
            if target is not None:
                self.setPos(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API
        if self._port_drag is not None:
            super().mouseReleaseEvent(event)
            return
        if self._hold_move.active:
            moving, origin = self._hold_move.finish()
            if moving:
                if origin is not None and (self.pos() - origin).manhattanLength() > 2:
                    self.position_committed.emit(self.node.node_id, self.pos())
            else:
                self.clicked.emit(self.node.node_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)
