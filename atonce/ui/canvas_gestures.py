"""Small interaction state helpers for the workflow canvas."""

import time

from qgis.PyQt.QtCore import QPointF


class HoldToMoveGesture:
    """Require a deliberate hold before a canvas node starts moving.

    Quick press/release remains a configuration click.  Once the pointer has
    stayed down for ``delay_ms`` the gesture becomes a move gesture and all
    following pointer movement is applied relative to the original node
    position.  The helper is deliberately independent of graph persistence.
    """

    def __init__(self, delay_ms=1500, clock=None):
        self.delay_ms = int(delay_ms)
        self._clock = clock or time.monotonic
        self._started_at = None
        self._origin = None
        self._press_scene_pos = None
        self._move_mode = False

    @property
    def active(self):
        return self._started_at is not None

    def begin(self, origin, scene_pos):
        self._started_at = self._clock()
        self._origin = QPointF(origin)
        self._press_scene_pos = QPointF(scene_pos)
        self._move_mode = False

    def _elapsed_ms(self):
        if self._started_at is None:
            return 0.0
        return (self._clock() - self._started_at) * 1000.0

    def position_for(self, scene_pos):
        if not self.active:
            return None
        if not self._move_mode and self._elapsed_ms() >= self.delay_ms:
            self._move_mode = True
        if not self._move_mode:
            return None
        delta = QPointF(scene_pos) - self._press_scene_pos
        return self._origin + delta

    def finish(self):
        if not self.active:
            return False, None
        if not self._move_mode and self._elapsed_ms() >= self.delay_ms:
            self._move_mode = True
        moving = self._move_mode
        origin = QPointF(self._origin)
        self.cancel()
        return moving, origin

    def cancel(self):
        self._started_at = None
        self._origin = None
        self._press_scene_pos = None
        self._move_mode = False
