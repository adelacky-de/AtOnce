"""Material icon dispatch for AtOnce canvas UI.

Reference-visible icons use the exact @mui/icons-material Outlined SVG path
geometry.  Less prominent operation symbols retain compact Qt fallbacks until
they are assigned an explicit MUI asset.
"""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from .mui_exact_icons import draw_exact_mui_icon, exact_mui_icon, has_exact_mui_icon

ICON_SIZE = 20


def _norm(rect):
    return QRectF(rect)


def _line_pen(color, width=1.35):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw_merge(p, r):
    p.drawLine(QPointF(r.left()+3, r.top()+3), QPointF(r.center().x(), r.center().y()))
    p.drawLine(QPointF(r.left()+3, r.bottom()-3), QPointF(r.center().x(), r.center().y()))
    p.drawLine(QPointF(r.center().x(), r.center().y()), QPointF(r.right()-3, r.center().y()))


def _draw_compare(p, r):
    y1 = r.top()+5; y2 = r.bottom()-5
    p.drawLine(QPointF(r.left()+3,y1), QPointF(r.right()-3,y1))
    p.drawLine(QPointF(r.right()-6,y1-3), QPointF(r.right()-3,y1))
    p.drawLine(QPointF(r.right()-6,y1+3), QPointF(r.right()-3,y1))
    p.drawLine(QPointF(r.right()-3,y2), QPointF(r.left()+3,y2))
    p.drawLine(QPointF(r.left()+6,y2-3), QPointF(r.left()+3,y2))
    p.drawLine(QPointF(r.left()+6,y2+3), QPointF(r.left()+3,y2))


def _draw_location(p, r):
    p.drawEllipse(QRectF(r.center().x()-2, r.top()+3, 4, 4))
    path = QPainterPath(); path.moveTo(r.center().x(), r.bottom()-2)
    path.cubicTo(r.left()+1, r.center().y(), r.left()+3, r.top()+1, r.center().x(), r.top()+1)
    path.cubicTo(r.right()-3, r.top()+1, r.right()-1, r.center().y(), r.center().x(), r.bottom()-2)
    p.drawPath(path)


def _draw_buffer(p, r):
    p.drawEllipse(r.adjusted(2,2,-2,-2)); p.drawEllipse(r.adjusted(5,5,-5,-5))


def _draw_reproject(p, r):
    p.drawEllipse(r.adjusted(2,2,-2,-2))
    p.drawArc(QRectF(r.left()+5,r.top()+2,r.width()-10,r.height()-4), 90*16, 180*16)
    p.drawLine(QPointF(r.left()+2,r.center().y()), QPointF(r.right()-2,r.center().y()))


def _draw_table(p, r):
    p.drawRoundedRect(r.adjusted(2,2,-2,-2), 1, 1)
    p.drawLine(QPointF(r.left()+2,r.top()+7), QPointF(r.right()-2,r.top()+7))
    p.drawLine(QPointF(r.center().x(),r.top()+2), QPointF(r.center().x(),r.bottom()-2))


def _draw_source_database(p, r):
    """Draw one database cylinder without repeated server-row glyphs."""

    body = r.adjusted(2, 2, -2, -2)
    cap_height = max(4.0, body.height() * 0.28)
    p.drawEllipse(QRectF(body.left(), body.top(), body.width(), cap_height))
    p.drawLine(
        QPointF(body.left(), body.top() + cap_height / 2),
        QPointF(body.left(), body.bottom() - cap_height / 2),
    )
    p.drawLine(
        QPointF(body.right(), body.top() + cap_height / 2),
        QPointF(body.right(), body.bottom() - cap_height / 2),
    )
    p.drawArc(
        QRectF(body.left(), body.bottom() - cap_height, body.width(), cap_height),
        180 * 16,
        180 * 16,
    )


def draw_material_symbol(painter, symbol, rect, color=QColor("#23323A")):
    """Render an exact MUI icon when available; otherwise use a Qt fallback."""

    symbol = str(symbol or "").lower()
    exact_alias = {
        "source": "storage",
        "operation": "account_tree",
        "delivery": "output",
        "text": "notes",
        "group": "dashboard_customize",
        "import": "file_download",
        "export": "file_upload",
        "person_add": "how_to_reg",
        "register": "how_to_reg",
        "filter": "filter_alt",
        "functions": "calculate",
        "calculate_field": "calculate",
        "join": "link",
        "spatial_join": "link",
    }.get(symbol, symbol)
    if has_exact_mui_icon(exact_alias):
        draw_exact_mui_icon(painter, exact_alias, QRectF(rect), color)
        return

    r = _norm(rect)
    painter.save(); painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(_line_pen(color)); painter.setBrush(Qt.NoBrush)
    if symbol in {"call_merge", "merge"}: _draw_merge(painter, r)
    elif symbol in {"compare_arrows", "compare_changes"}: _draw_compare(painter, r)
    elif symbol in {"location_on", "select_by_location", "clip"}: _draw_location(painter, r)
    elif symbol in {"radio_button_unchecked", "buffer", "dissolve"}: _draw_buffer(painter, r)
    elif symbol in {"public", "reproject"}: _draw_reproject(painter, r)
    elif symbol in {"table_view", "field_mapping", "keep_fields", "rename_field", "change_field_type", "sort", "remove_duplicates"}: _draw_table(painter, r)
    else: draw_exact_mui_icon(painter, "account_tree", r, color)
    painter.restore()


def operation_symbol(operation_kind):
    kind = str(operation_kind or "").lower()
    return {
        "filter": "filter_alt", "join": "link", "merge": "call_merge",
        "calculate_field": "calculate", "aggregate": "calculate",
        "compare_changes": "compare_arrows", "select_by_location": "location_on",
        "spatial_join": "link", "clip": "location_on", "buffer": "radio_button_unchecked",
        "dissolve": "radio_button_unchecked", "reproject": "public",
        "raster_reproject": "public", "raster_convert": "file_upload",
        "field_mapping": "table_view", "keep_fields": "table_view",
        "rename_field": "table_view", "change_field_type": "table_view",
        "sort": "table_view", "remove_duplicates": "table_view",
    }.get(kind, "account_tree")


def draw_block_icon(painter, kind, rect, color, operation_kind=""):
    kind = str(kind or "").lower()
    if kind == "source":
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(_line_pen(color))
        painter.setBrush(Qt.NoBrush)
        _draw_source_database(painter, QRectF(rect))
        painter.restore()
        return
    symbol = operation_symbol(operation_kind) if kind == "operation" else {
        "output": "output", "text": "notes", "group": "dashboard_customize"
    }.get(kind, "account_tree")
    draw_material_symbol(painter, symbol, rect, color)


def material_icon(symbol, color=QColor("#23323A"), size=20):
    """Return the same exact MUI icon used by nodes whenever available."""

    symbol = str(symbol or "").lower()
    alias = {
        "person_add": "how_to_reg", "register": "how_to_reg",
        "functions": "calculate", "calculate_field": "calculate",
    }.get(symbol, symbol)
    if has_exact_mui_icon(alias):
        return exact_mui_icon(alias, color, size)
    pixmap = QPixmap(size, size); pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    draw_material_symbol(painter, symbol, QRectF(1, 1, size-2, size-2), color)
    painter.end()
    return QIcon(pixmap)
