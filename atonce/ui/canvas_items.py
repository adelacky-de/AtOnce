"""Compatibility exports for the modular AtOnce canvas graphics.

Keep imports stable for existing callers while implementation lives in focused
modules instead of one monolithic graphics file.
"""

from .canvas_annotations import CanvasTextItem, GroupBoardItem
from .canvas_icon_node_item import DragCanvasBlockItem
from .canvas_icon_palette import DraggableBlockTemplate
from .canvas_theme import (
    BLOCK_BORDER,
    BLOCK_MUTED,
    BLOCK_TEMPLATE_MIME,
    BLOCK_TEXT,
    FIELD_BG,
    FIELD_BORDER,
    LINEAGE_EXCLUDED,
    LINEAGE_INCLUDED,
    PASTEL_GROUP,
    PASTEL_OPERATION,
    PASTEL_OUTPUT,
    PASTEL_SOURCE,
    PASTEL_TEXT,
)
from .canvas_view import DragFirstCanvasView, LineageConnectionItem

__all__ = [
    "BLOCK_BORDER",
    "BLOCK_MUTED",
    "BLOCK_TEMPLATE_MIME",
    "BLOCK_TEXT",
    "FIELD_BG",
    "FIELD_BORDER",
    "LINEAGE_EXCLUDED",
    "LINEAGE_INCLUDED",
    "PASTEL_GROUP",
    "PASTEL_OPERATION",
    "PASTEL_OUTPUT",
    "PASTEL_SOURCE",
    "PASTEL_TEXT",
    "CanvasTextItem",
    "DragCanvasBlockItem",
    "DragFirstCanvasView",
    "DraggableBlockTemplate",
    "GroupBoardItem",
    "LineageConnectionItem",
]
