"""Final desktop-QGIS ordering fixes for the 0.1.3 real-case beta.

Two UI behaviors depend on patch order rather than persisted workflow state:

* the GeoPackage-aware SOURCE dialog must be the final SOURCE dialog installed,
  otherwise the older acceptance wrapper can emit its obsolete generic raster
  warning; and
* QGIS may keep the toolbar pixmap that existed when the QAction was first added.
  Setting a new icon on the QAction afterwards is not sufficient on every QGIS
  desktop build, so the toolbar action is removed and re-added after the final
  black icon is assigned.
"""

from pathlib import Path

_APPLIED = False


def _reinstall_gpkg_source_dialog_last():
    """Guarantee that the direct GeoPackage vector-sublayer chooser wins."""

    from .canvas_gpkg_realcase import _install_gpkg_source_dialog

    _install_gpkg_source_dialog()


def _install_toolbar_refresh_after_black_icon():
    """Re-add the QAction after assigning the final black-background icon."""

    from qgis.PyQt.QtGui import QIcon

    from .plugin import AtOncePlugin

    previous_init_gui = AtOncePlugin.initGui
    icon_path = Path(__file__).resolve().parent / "resources" / "icon_black.svg"

    def init_gui(self):
        result = previous_init_gui(self)
        action = getattr(self, "action", None)
        if action is None or not icon_path.is_file():
            return result

        icon = QIcon(str(icon_path))
        if icon.isNull():
            return result

        # Assign first, then physically reinsert the QAction.  This avoids QGIS
        # retaining the pixmap from the earlier transparent/white-line icon.
        action.setIcon(icon)
        try:
            self.iface.removeToolBarIcon(action)
        except Exception:
            pass
        self.iface.addToolBarIcon(action)
        return result

    AtOncePlugin.initGui = init_gui


def apply_realcase_finalizer():
    """Install final UI ordering fixes exactly once, after all other patches."""

    global _APPLIED
    if _APPLIED:
        return
    _reinstall_gpkg_source_dialog_last()
    _install_toolbar_refresh_after_black_icon()
    _APPLIED = True
