"""Exact MUI Outlined SVG path assets used by the canvas.

The path data is copied from @mui/icons-material so the QGIS renderer uses the
same 24x24 geometry instead of hand-drawn approximations. Keep this module
small and presentation-only.
"""

from html import escape

from qgis.PyQt.QtCore import QByteArray, QRectF
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer


MUI_OUTLINED_PATHS = {
    "storage": (
        "M2 20h20v-4H2zm2-3h2v2H4zM2 4v4h20V4zm4 3H4V5h2zm-4 7h20v-4H2zm2-3h2v2H4z",
    ),
    "account_tree": (
        "M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3zM7 9H4V5h3zm10 6h3v4h-3zm0-10h3v4h-3z",
    ),
    "output": (
        "m17 17 5-5-5-5-1.41 1.41L18.17 11H9v2h9.17l-2.58 2.59z",
        "M19 19H5V5h14v2h2V5c0-1.1-.89-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.11 0 2-.9 2-2v-2h-2z",
    ),
    "notes": (
        "M21 11.01 3 11v2h18zM3 16h12v2H3zM21 6H3v2.01L21 8z",
    ),
    "dashboard_customize": (
        "M3 11h8V3H3zm2-6h4v4H5zm8-2v8h8V3zm6 6h-4V5h4zM3 21h8v-8H3zm2-6h4v4H5zm13-2h-2v3h-3v2h3v3h2v-3h3v-2h-3z",
    ),
    "file_download": (
        "M18 15v3H6v-3H4v3c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-3zm-1-4-1.41-1.41L13 12.17V4h-2v8.17L8.41 9.59 7 11l5 5z",
    ),
    "file_upload": (
        "M18 15v3H6v-3H4v3c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-3zM7 9l1.41 1.41L11 7.83V16h2V7.83l2.59 2.58L17 9l-5-5z",
    ),
    "how_to_reg": (
        "M11 12c2.21 0 4-1.79 4-4s-1.79-4-4-4 1.79-4 4 1.79 4 4 4m0-6c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2M5 18c.2-.63 2.57-1.68 4.96-1.94l2.04-2c-.39-.04-.68-.06-1-.06-2.67 0-8 1.34-8 4v2h9l-2-2zm15.6-5.5-5.13 5.17-2.07-2.08L12 17l3.47 3.5L22 13.91z",
    ),
    "filter_alt": (
        "M7 6h10l-5.01 6.3zm-2.75-.39C6.27 8.2 10 13 10 13v6c0 .55.45 1 1 1h2c.55 0 1-.45 1-1v-6s3.72-4.8 5.74-7.39c.51-.66.04-1.61-.79-1.61H5.04c-.83 0-1.3.95-.79 1.61",
    ),
    "calculate": (
        "M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2m0 16H5V5h14z",
        "M6.25 7.72h5v1.5h-5zM13 15.75h5v1.5h-5zm0-2.5h5v1.5h-5zM8 18h1.5v-2h2v-1.5h-2v-2H8v2H6V16h2zm6.09-7.05 1.41-1.41 1.41 1.41 1.06-1.06-1.41-1.42 1.41-1.41L16.91 6 15.5 7.41 14.09 6l-1.06 1.06 1.41 1.41-1.41 1.42z",
    ),
    "link": (
        "M17 7h-4v2h4c1.65 0 3 1.35 3 3s-1.35 3-3 3h-4v2h4c2.76 0 5-2.24 5-5s-2.24-5-5-5m-6 8H7c-1.65 0-3-1.35-3-3s1.35-3 3-3h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4zm-3-4h8v2H8z",
    ),
    "delete_outline": (
        "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6zM8 9h8v10H8zm7.5-5-1-1h-5l-1 1H5v2h14V4z",
    ),
    "edit": (
        "m14.06 9.02.92.92L5.92 19H5v-.92zM17.66 3c-.25 0-.51.1-.7.29l-1.83 1.83 3.75 3.75 1.83-1.83c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.2-.2-.45-.29-.71-.29m-3.6 3.19L3 17.25V21h3.75L17.81 9.94z",
    ),
    "more_vert": (
        "M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2m0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2m0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2",
    ),
    "info": (
        "M11 7h2v2h-2zm0 4h2v6h-2zm1-9C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8",
    ),
}


def has_exact_mui_icon(symbol):
    return str(symbol or "").lower() in MUI_OUTLINED_PATHS


def _svg_bytes(symbol, color):
    name = str(symbol or "").lower()
    paths = MUI_OUTLINED_PATHS[name]
    fill = escape(QColor(color).name())
    body = "".join(f'<path d="{escape(path)}" fill="{fill}"/>' for path in paths)
    return QByteArray(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 24 24" width="24" height="24">'
            f"{body}</svg>"
        ).encode("utf-8")
    )


def draw_exact_mui_icon(painter, symbol, rect, color=QColor("#23323A")):
    """Render the exact MUI 24x24 SVG geometry into ``rect``."""

    if not has_exact_mui_icon(symbol):
        return False
    renderer = QSvgRenderer(_svg_bytes(symbol, color))
    if not renderer.isValid():
        return False
    renderer.render(painter, QRectF(rect))
    return True


def exact_mui_icon(symbol, color=QColor("#23323A"), size=18):
    """Return a QIcon generated from the exact MUI Outlined path data."""

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    draw_exact_mui_icon(painter, symbol, QRectF(0, 0, size, size), color)
    painter.end()
    return QIcon(pixmap)