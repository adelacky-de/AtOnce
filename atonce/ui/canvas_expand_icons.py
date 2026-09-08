"""Exact MUI expand/collapse symbols for SOURCE field controls."""

from html import escape

from qgis.PyQt.QtCore import QByteArray, QRectF
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtSvg import QSvgRenderer


_PATHS = {
    "more": "M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z",
    "less": "m12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z",
}


def draw_expand_icon(painter, expanded, rect, color=QColor("#23323A")):
    """Render the exact MUI ExpandMore/ExpandLess 24x24 SVG path."""

    path = _PATHS["less" if expanded else "more"]
    fill = escape(QColor(color).name())
    svg = QByteArray(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'width="24" height="24">'
            f'<path d="{escape(path)}" fill="{fill}"/>'
            "</svg>"
        ).encode("utf-8")
    )
    renderer = QSvgRenderer(svg)
    if renderer.isValid():
        renderer.render(painter, QRectF(rect))
