"""AtOnce Porcelain Minimal design tokens and QSS.

The palette is intentionally low-saturation and close to native QGIS controls. It
adds hierarchy without turning the dock into a web dashboard.
"""

COLORS = {
    "background": "#F6F7F4",
    "surface": "#FCFCFA",
    "surface_alt": "#F8F9F7",
    "border": "#D9E1E5",
    "divider": "#E7ECEF",
    "text": "#23323A",
    "text_muted": "#61727C",
    "text_disabled": "#96A4AC",
    "primary": "#6E8F9F",
    "primary_hover": "#5F8192",
    "primary_wash": "#E8EFF2",
    "source_bg": "#F1F5EC",
    "source_border": "#B9C9A5",
    "derived_bg": "#EDF4F7",
    "derived_border": "#AFC8D4",
    "output_bg": "#FAF5EA",
    "output_border": "#D8BF8E",
    "success": "#6F956F",
    "success_bg": "#EDF5EE",
    "warning": "#A37C3D",
    "warning_bg": "#F8F2E6",
    "danger": "#A85E55",
    "danger_bg": "#FAEEEC",
    "info": "#6D91A5",
    "info_bg": "#EDF4F8",
}

DOCK_STYLESHEET = f"""
QWidget#AtOnceRoot {{
    background: {COLORS['background']};
    color: {COLORS['text']};
}}

QLabel#AtOnceTitle {{
    color: {COLORS['text']};
    font-size: 19px;
    font-weight: 700;
}}
QLabel#AtOnceSubtitle,
QLabel#AtOnceMuted,
QLabel#AtOnceMeta {{
    color: {COLORS['text_muted']};
}}
QLabel#AtOnceSectionTitle {{
    color: {COLORS['text']};
    font-size: 13px;
    font-weight: 650;
}}
QLabel#AtOnceMetric {{
    color: {COLORS['text']};
    font-size: 17px;
    font-weight: 700;
}}

QFrame#AtOnceCard,
QFrame#AtOnceSummaryCard,
QFrame#AtOnceEmptyCard {{
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
    background: {COLORS['surface']};
}}
QFrame#AtOnceSummaryCard {{
    background: {COLORS['surface_alt']};
}}
/*
 * Summary value labels intentionally have no object name in dock.py. Qt style
 * sheet foregrounds do not reliably inherit through a host application's
 * palette, so explicitly pin every summary-card label to the Porcelain text
 * color first, then restore the muted metadata hierarchy below. This prevents
 * low-contrast host-palette text on AtOnce's fixed light card background.
 */
QFrame#AtOnceSummaryCard QLabel {{
    color: {COLORS['text']};
}}
QFrame#AtOnceSummaryCard QLabel#AtOnceMeta {{
    color: {COLORS['text_muted']};
}}
QFrame#AtOnceEmptyCard {{
    background: {COLORS['surface']};
}}
QFrame#AtOnceValidationSuccess {{
    border: 1px solid #CDDCCB;
    border-radius: 9px;
    background: {COLORS['success_bg']};
}}
QFrame#AtOnceValidationWarning {{
    border: 1px solid #E4D4B4;
    border-radius: 9px;
    background: {COLORS['warning_bg']};
}}
QFrame#AtOnceValidationError {{
    border: 1px solid #E4C1BC;
    border-radius: 9px;
    background: {COLORS['danger_bg']};
}}
QFrame#AtOnceValidationNeutral {{
    border: 1px solid {COLORS['border']};
    border-radius: 9px;
    background: {COLORS['surface_alt']};
}}

QLabel#AtOnceStatusValid {{
    color: #456D48;
    background: {COLORS['success_bg']};
    border: 1px solid #D5E3D3;
    border-radius: 11px;
    padding: 3px 9px;
    font-weight: 600;
}}
QLabel#AtOnceStatusWarning {{
    color: #805F2B;
    background: {COLORS['warning_bg']};
    border: 1px solid #E7D7BA;
    border-radius: 11px;
    padding: 3px 9px;
    font-weight: 600;
}}
QLabel#AtOnceStatusError {{
    color: #8D4D45;
    background: {COLORS['danger_bg']};
    border: 1px solid #E7C8C4;
    border-radius: 11px;
    padding: 3px 9px;
    font-weight: 600;
}}
QLabel#AtOnceStatusNeutral {{
    color: {COLORS['text_muted']};
    background: #F0F3F3;
    border: 1px solid {COLORS['border']};
    border-radius: 11px;
    padding: 3px 9px;
    font-weight: 600;
}}
QLabel#AtOnceTag {{
    color: #557282;
    background: {COLORS['primary_wash']};
    border-radius: 8px;
    padding: 2px 6px;
}}

QTabWidget#AtOnceTabs::pane {{
    border: none;
    background: transparent;
    top: -1px;
}}
QTabWidget#AtOnceTabs QTabBar::tab {{
    color: {COLORS['text_muted']};
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 12px 7px 12px;
    min-width: 62px;
}}
QTabWidget#AtOnceTabs QTabBar::tab:selected {{
    color: {COLORS['text']};
    border-bottom: 2px solid {COLORS['primary']};
    font-weight: 650;
}}
QTabWidget#AtOnceTabs QTabBar::tab:hover:!selected {{
    color: {COLORS['text']};
    background: #F0F3F3;
}}

QPushButton {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 7px;
    padding: 7px 11px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: #F1F4F4;
    border-color: #C5D1D7;
}}
QPushButton:pressed {{
    background: #E9EEEF;
}}
QPushButton:disabled {{
    color: {COLORS['text_disabled']};
    background: #F1F2F0;
    border-color: #E1E5E5;
}}
QPushButton#AtOncePrimary {{
    color: white;
    background: {COLORS['primary']};
    border-color: {COLORS['primary']};
    font-weight: 650;
}}
QPushButton#AtOncePrimary:hover {{
    background: {COLORS['primary_hover']};
    border-color: {COLORS['primary_hover']};
}}
/* Keep disabled primary actions visibly disabled; the ID selector above is
 * more specific than the generic QPushButton:disabled rule. */
QPushButton#AtOncePrimary:disabled {{
    color: {COLORS['text_disabled']};
    background: #E9ECEA;
    border-color: #D8DEDE;
}}
QPushButton#AtOnceQuiet {{
    color: {COLORS['text_muted']};
    background: transparent;
    border-color: transparent;
}}
QPushButton#AtOnceQuiet:hover {{
    color: {COLORS['text']};
    background: #EEF2F2;
}}

QTableWidget#AtOnceTable {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    alternate-background-color: {COLORS['surface_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    gridline-color: {COLORS['divider']};
    selection-background-color: {COLORS['primary_wash']};
    selection-color: {COLORS['text']};
}}
QTableWidget#AtOnceTable QHeaderView::section {{
    color: {COLORS['text_muted']};
    background: #F5F7F5;
    border: none;
    border-bottom: 1px solid {COLORS['divider']};
    padding: 6px 7px;
    font-weight: 600;
}}

QLineEdit,
QComboBox {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 7px;
    padding: 6px 8px;
    min-height: 18px;
}}
QLineEdit:focus,
QComboBox:focus {{
    border: 1px solid {COLORS['primary']};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QFrame#AtOnceSourceDropSlot {{
    background: {COLORS['source_bg']};
    border: 1px dashed {COLORS['source_border']};
    border-radius: 9px;
}}
QLabel#AtOnceCanvasHint,
QLabel#AtOnceCanvasMuted {{
    color: {COLORS['text_muted']};
}}
QLabel#AtOnceCanvasCaption {{
    color: {COLORS['text_muted']};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QFrame#AtOnceCanvasEditor {{
    background: {COLORS['surface_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
}}
QPushButton#AtOnceCanvasOperationBlock {{
    color: {COLORS['text']};
    background: {COLORS['derived_bg']};
    border: 1px solid {COLORS['derived_border']};
    border-radius: 9px;
    padding: 10px;
    font-weight: 700;
}}
QPushButton#AtOnceCanvasResultPlaceholder,
QPushButton#AtOnceCanvasAddOutput {{
    color: {COLORS['text_muted']};
    background: {COLORS['surface_alt']};
    border: 1px dashed {COLORS['border']};
    border-radius: 9px;
    padding: 10px;
    font-weight: 650;
}}
QPushButton#AtOnceCanvasResultBlock {{
    color: {COLORS['text']};
    background: {COLORS['derived_bg']};
    border: 1px solid {COLORS['derived_border']};
    border-radius: 9px;
    padding: 10px;
    font-weight: 700;
}}
QPushButton#AtOnceCanvasOutputBlock {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 8px;
    font-weight: 650;
}}
QLabel#AtOnceCanvasArrow {{
    color: {COLORS['text_muted']};
    font-size: 18px;
    font-weight: 700;
}}
QLabel#AtOnceBuilderRole {{
    color: {COLORS['text_muted']};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#AtOnceBuilderSlotText,
QLabel#AtOnceBuilderEmpty {{
    color: {COLORS['text']};
    font-weight: 600;
}}
QLabel#AtOnceBuilderArrow {{
    color: {COLORS['primary']};
    font-size: 22px;
    font-weight: 700;
}}
QPushButton#AtOnceBuilderTransform,
QPushButton#AtOnceBuilderResult {{
    color: {COLORS['text']};
    background: {COLORS['derived_bg']};
    border: 1px solid {COLORS['derived_border']};
    border-radius: 9px;
    padding: 9px;
    font-weight: 650;
}}
QPushButton#AtOnceBuilderDelivery {{
    color: {COLORS['text']};
    background: {COLORS['output_bg']};
    border: 1px solid {COLORS['output_border']};
    border-radius: 9px;
    padding: 9px;
    font-weight: 650;
}}
QPushButton#AtOnceBuilderChoose,
QPushButton#AtOnceBuilderSecondary {{
    padding: 5px 8px;
}}
QLabel#AtOnceBuilderError {{
    color: {COLORS['danger']};
}}
QFrame#AtOnceChooserCard {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 9px;
}}
QPushButton#AtOnceChooserChoice {{
    text-align: left;
    font-weight: 650;
    border: none;
    background: transparent;
}}
QPushButton#AtOnceBuilderCancel {{
    color: {COLORS['text_muted']};
}}
"""

DIALOG_STYLESHEET = f"""
QDialog {{
    background: {COLORS['background']};
    color: {COLORS['text']};
}}
QLabel#AtOnceDialogTitle {{
    font-size: 18px;
    font-weight: 700;
    color: {COLORS['text']};
}}
QLabel#AtOnceDialogSubtitle,
QLabel#AtOnceMuted {{
    color: {COLORS['text_muted']};
}}
QLabel#AtOnceSectionTitle {{
    font-size: 13px;
    font-weight: 650;
    color: {COLORS['text']};
}}
QFrame#AtOnceDialogCard,
QFrame#AtOnceExportCard {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
}}
QFrame#AtOnceSafetyNote {{
    background: {COLORS['info_bg']};
    border: 1px solid #CEDDE5;
    border-radius: 8px;
}}
QLineEdit,
QComboBox {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 7px;
    padding: 6px 8px;
    min-height: 18px;
}}
QLineEdit:focus,
QComboBox:focus {{
    border: 1px solid {COLORS['primary']};
}}
QPushButton {{
    color: {COLORS['text']};
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 7px;
    padding: 7px 11px;
}}
QPushButton:hover {{
    background: #F1F4F4;
}}
QPushButton:disabled {{
    color: {COLORS['text_disabled']};
    background: #F1F2F0;
    border-color: #E1E5E5;
}}
QPushButton#AtOncePrimary {{
    color: white;
    background: {COLORS['primary']};
    border-color: {COLORS['primary']};
    font-weight: 650;
}}
QPushButton#AtOncePrimary:disabled {{
    color: {COLORS['text_disabled']};
    background: #E9ECEA;
    border-color: #D8DEDE;
}}
QPushButton#AtOnceDangerQuiet {{
    color: {COLORS['danger']};
    background: transparent;
    border-color: transparent;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QFrame#AtOnceSourceDropSlot {{
    background: {COLORS['source_bg']};
    border: 1px dashed {COLORS['source_border']};
    border-radius: 9px;
}}
QLabel#AtOnceBuilderRole {{
    color: {COLORS['text_muted']};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#AtOnceBuilderSlotText,
QLabel#AtOnceBuilderEmpty {{
    color: {COLORS['text']};
    font-weight: 600;
}}
QLabel#AtOnceBuilderArrow {{
    color: {COLORS['primary']};
    font-size: 22px;
    font-weight: 700;
}}
QPushButton#AtOnceBuilderTransform,
QPushButton#AtOnceBuilderResult {{
    color: {COLORS['text']};
    background: {COLORS['derived_bg']};
    border: 1px solid {COLORS['derived_border']};
    border-radius: 9px;
    padding: 9px;
    font-weight: 650;
}}
QPushButton#AtOnceBuilderDelivery {{
    color: {COLORS['text']};
    background: {COLORS['output_bg']};
    border: 1px solid {COLORS['output_border']};
    border-radius: 9px;
    padding: 9px;
    font-weight: 650;
}}
QPushButton#AtOnceBuilderChoose,
QPushButton#AtOnceBuilderSecondary {{
    padding: 5px 8px;
}}
QLabel#AtOnceBuilderError {{
    color: {COLORS['danger']};
}}
QFrame#AtOnceChooserCard {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 9px;
}}
QPushButton#AtOnceChooserChoice {{
    text-align: left;
    font-weight: 650;
    border: none;
    background: transparent;
}}
QPushButton#AtOnceBuilderCancel {{
    color: {COLORS['text_muted']};
}}
"""
