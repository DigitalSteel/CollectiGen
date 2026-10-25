from __future__ import annotations

import ctypes
import os

from PySide6.QtGui import QColor,QPalette
from .confidence import COLOR_BLIND_STATUS_COLORS,STANDARD_STATUS_COLORS,set_color_blind_mode


THEMES={
    "system":"Follow Windows",
    "light":"Light",
    "dark":"Dark",
    "midnight":"Midnight Blue",
}

_system_palette=None


def system_high_contrast_enabled() -> bool:
    if os.name!="nt":return False
    class HIGHCONTRASTW(ctypes.Structure):
        _fields_=[("cbSize",ctypes.c_uint),("dwFlags",ctypes.c_uint),("lpszDefaultScheme",ctypes.c_wchar_p)]
    value=HIGHCONTRASTW(); value.cbSize=ctypes.sizeof(value)
    try:available=bool(ctypes.windll.user32.SystemParametersInfoW(0x0042,value.cbSize,ctypes.byref(value),0))
    except (AttributeError,OSError):return False
    return bool(available and value.dwFlags&0x00000001)


def _dark_palette(*,midnight: bool=False) -> QPalette:
    palette=QPalette()
    window=QColor("#111827" if midnight else "#1d1f22")
    base=QColor("#172033" if midnight else "#25272b")
    palette.setColor(QPalette.Window,window); palette.setColor(QPalette.WindowText,QColor("#f2f4f8"))
    palette.setColor(QPalette.Base,base); palette.setColor(QPalette.AlternateBase,QColor("#1d2940" if midnight else "#2d3035"))
    palette.setColor(QPalette.Text,QColor("#f2f4f8")); palette.setColor(QPalette.Button,QColor("#28364f" if midnight else "#34373c"))
    palette.setColor(QPalette.PlaceholderText,QColor("#aeb6c2"))
    palette.setColor(QPalette.ButtonText,QColor("#f2f4f8")); palette.setColor(QPalette.Highlight,QColor("#1686d9"))
    palette.setColor(QPalette.HighlightedText,QColor("#ffffff")); palette.setColor(QPalette.Mid,QColor("#687386"))
    palette.setColor(QPalette.Midlight,QColor("#35425a" if midnight else "#45494f"))
    palette.setColor(QPalette.ToolTipBase,QColor("#f8f1d4")); palette.setColor(QPalette.ToolTipText,QColor("#202124"))
    return palette


def _light_palette() -> QPalette:
    palette=QPalette()
    palette.setColor(QPalette.Window,QColor("#f5f6f8")); palette.setColor(QPalette.WindowText,QColor("#20242a"))
    palette.setColor(QPalette.Base,QColor("#ffffff")); palette.setColor(QPalette.AlternateBase,QColor("#edf1f5"))
    palette.setColor(QPalette.Text,QColor("#20242a")); palette.setColor(QPalette.Button,QColor("#edf1f5"))
    palette.setColor(QPalette.PlaceholderText,QColor("#6b7280"))
    palette.setColor(QPalette.ButtonText,QColor("#20242a")); palette.setColor(QPalette.Highlight,QColor("#147cc1"))
    palette.setColor(QPalette.HighlightedText,QColor("#ffffff")); palette.setColor(QPalette.Mid,QColor("#7b8490"))
    palette.setColor(QPalette.Midlight,QColor("#d8dde4"))
    return palette


def build_stylesheet(theme: str="system",color_blind_mode: bool=False) -> str:
    navigation="#082b73" if theme=="midnight" else "palette(window)"
    navigation_text="#f4f8ff" if theme=="midnight" else "palette(text)"
    disabled_text=("#aeb6c2" if theme in {"dark","midnight"}
                   else "#6b7280" if theme=="light" else "palette(button-text)")
    colors=COLOR_BLIND_STATUS_COLORS if color_blind_mode else STANDARD_STATUS_COLORS
    styles={"red":"double","orange":"dashed","yellow":"dotted","green":"solid"} if color_blind_mode else {key:"solid" for key in ("red","orange","yellow","green")}
    return f"""
        QMainWindow, QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
        QListWidget#navigation {{ background:{navigation}; color:{navigation_text}; border:0; border-right:1px solid palette(mid); padding:10px 6px; }}
        QListWidget#navigation::item {{ min-height:38px; padding:4px 10px; border-radius:5px; }}
        QListWidget#navigation[compactNavigation="true"] {{ padding:10px 4px; }}
        QListWidget#navigation[compactNavigation="true"]::item {{ padding:4px 0px; }}
        QListWidget#navigation::item:hover {{ background:palette(midlight); }}
        QListWidget#navigation::item:selected {{ background:palette(highlight); color:palette(highlighted-text); font-weight:600; }}
        QSplitter#shellSplitter::handle {{ background:palette(midlight); }}
        QSplitter#shellSplitter::handle:hover {{ background:palette(highlight); }}
        QWidget#commandBar {{ background:palette(window); border-bottom:1px solid palette(midlight); }}
        QWidget#commandRow {{ background:palette(base); border:1px solid palette(midlight); border-radius:7px; }}
        QLabel#commandContext {{ color:palette(text); font-weight:700; }}
        QFrame#commandContextSeparator, QFrame#commandSectionSeparator {{ background:palette(mid); border:0; }}
        QWidget#commandBar QTabBar::tab {{ background:transparent; border:0; border-bottom:3px solid transparent; padding:5px 12px; margin:0; }}
        QWidget#commandBar QTabBar::tab:selected {{ border-bottom-color:palette(highlight); color:palette(text); }}
        QWidget#commandBar QTabBar::tab:hover {{ background:palette(midlight); }}
        QWidget#commandBar QToolButton {{ background:transparent; border:1px solid transparent; border-radius:5px; padding:3px 8px; }}
        QWidget#commandBar QToolButton:hover {{ background:palette(midlight); }}
        QWidget#commandBar QToolButton:checked {{ background:palette(midlight); border-color:palette(highlight); }}
        QWidget#commandBar QToolButton:focus {{ border-color:palette(highlight); }}
        QTreeView {{ alternate-background-color:palette(base); }}
        QTreeView, QListView, QTextEdit {{ border:1px solid palette(midlight); border-radius:6px; background:palette(base); }}
        QTreeView::item:selected {{ background:palette(highlight); color:palette(highlighted-text); }}
        QLabel#pageTitle {{ font-size:23pt; font-weight:700; }}
        QPushButton#pageTitleButton {{ font-size:23pt; font-weight:700; text-align:left; background:transparent; border:0; padding:0 4px; }}
        QPushButton#pageTitleButton:hover {{ color:palette(highlight); }}
        QLabel#sectionTitle {{ font-size:16pt; font-weight:650; }}
        QLabel#mutedLabel {{ color:palette(text); }}
        QLabel#metricValue {{ font-size:12pt; font-weight:650; }}
        QFrame#metricCard {{ background:palette(base); border:1px solid palette(midlight); border-radius:6px; }}
        QFrame#modelCard {{ background:palette(base); border:1px solid palette(midlight); border-radius:9px; }}
        QFrame#capabilityRow {{ background:palette(base); border:1px solid palette(midlight); border-radius:7px; }}
        QFrame#capabilityRow[capabilityKind="optional"] {{ border-left:3px solid {colors['blue']}; }}
        QLabel#capabilityStatus {{ font-weight:650; }}
        QFrame#statusActionCard, QFrame#emptyStatePanel {{ background:palette(base); border:1px solid palette(midlight); border-radius:7px; }}
        QLabel#statusCardTitle, QLabel#statusCardStatus, QLabel#emptyStateTitle {{ font-weight:650; }}
        QFrame#galleryToolbar {{ background:palette(base); border:1px solid palette(midlight); border-radius:10px; }}
        QFrame#galleryStateFilter {{ background:palette(button); border:1px solid palette(mid); border-radius:8px; padding:2px 5px; }}
        QFrame#galleryStateFilter[active="true"] {{ border:2px solid palette(highlight); padding:1px 4px; }}
        QFrame#galleryStateFilter QLabel {{ font-weight:650; }}
        QFrame#ingestCompletionPanel {{ background:palette(base); border:1px solid palette(highlight); border-radius:9px; }}
        QFrame#imageCard {{ background:transparent; border:3px solid transparent; border-radius:9px; }}
        QFrame#imageCard[marked="true"] {{ border:2px solid {colors['blue']}; background:palette(midlight); }}
        QFrame#imageCard[selected="true"] {{ border:3px solid {colors['blue']}; }}
        QFrame#imageCardStatus {{ background:palette(base); border:1px solid palette(midlight); border-radius:6px; }}
        QFrame#imageCardStatus[visualStatus="red"] {{ border:3px {styles['red']} {colors['red']}; }}
        QFrame#imageCardStatus[visualStatus="orange"] {{ border:3px {styles['orange']} {colors['orange']}; }}
        QFrame#imageCardStatus[visualStatus="yellow"] {{ border:3px {styles['yellow']} {colors['yellow']}; }}
        QFrame#imageCardStatus[visualStatus="green"] {{ border:3px {styles['green']} {colors['green']}; }}
        QLabel#imageLibraryBadge {{ background:palette(midlight); color:palette(text); border:1px solid palette(mid); border-radius:7px; padding:1px 5px; font-size:8pt; font-weight:700; }}
        QLabel#activeLabel {{ color:{colors['blue']}; font-weight:700; }}
        QLabel#confidenceLegend {{ font-weight:600; }}
        QLabel#confidenceLegend[visualStatus="red"] {{ color:{colors['red']}; }}
        QLabel#confidenceLegend[visualStatus="orange"] {{ color:{colors['orange']}; }}
        QLabel#confidenceLegend[visualStatus="yellow"] {{ color:{colors['yellow']}; }}
        QLabel#confidenceLegend[visualStatus="green"] {{ color:{colors['green']}; }}
        QLabel#confidenceLegend[visualStatus="blue"] {{ color:{colors['blue']}; }}
        QGroupBox {{ font-weight:600; margin-top:9px; padding-top:10px; }}
        QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 4px; }}
        QPushButton, QToolButton[compact="true"] {{ background:palette(button); color:palette(button-text); border:1px solid palette(mid); border-radius:9px; min-height:30px; padding:3px 12px; }}
        QPushButton:hover {{ background:palette(midlight); border-color:palette(highlight); }}
        QPushButton:pressed {{ background:palette(mid); border-color:palette(highlight); }}
        QPushButton:checked, QToolButton[compact="true"]:checked {{ background:palette(midlight); border-color:palette(highlight); }}
        QPushButton:focus, QToolButton[compact="true"]:focus {{ border:2px solid palette(highlight); padding:2px 11px; }}
        QToolButton[compact="true"]:hover {{ background:palette(midlight); border-color:palette(highlight); }}
        QToolButton[compact="true"]:pressed {{ background:palette(mid); border-color:palette(highlight); }}
        QPushButton:disabled {{ color:{disabled_text}; border-color:palette(midlight); }}
        QPushButton:flat {{ background:transparent; border-color:transparent; }}
        QPushButton:flat:hover {{ background:palette(midlight); border-color:transparent; }}
        QPushButton[primary="true"] {{ background:palette(highlight); color:palette(highlighted-text); border-color:palette(highlight); font-weight:650; }}
        QPushButton[primary="true"]:hover {{ border-color:palette(text); }}
        QPushButton[primary="true"]:disabled {{ background:palette(button); color:palette(button-text); border-color:palette(midlight); }}
        QPushButton[activeFilter="true"] {{ border-color:palette(highlight); font-weight:650; }}
        QPushButton[featured="true"] {{ min-height:34px; padding:4px 14px; }}
        QToolButton[preparationLauncher="true"] {{ background:palette(highlight); color:palette(highlighted-text); border:1px solid palette(highlight); border-radius:9px; min-height:30px; padding:3px 12px; font-weight:650; }}
        QToolButton[preparationLauncher="true"]:hover {{ border-color:palette(text); }}
        QToolButton[preparationLauncher="true"]:disabled {{ background:palette(button); color:palette(button-text); border-color:palette(midlight); }}
        QToolButton[preparationLauncher="true"]::menu-button {{ border-left:1px solid palette(midlight); width:22px; }}
        QToolButton[preparationLauncher="true"][secondaryLauncher="true"] {{ background:transparent; color:palette(button-text); border-color:palette(midlight); font-weight:400; }}
        QToolButton[preparationLauncher="true"][secondaryLauncher="true"]:hover {{ background:palette(midlight); border-color:palette(highlight); }}
        QToolButton[preparationLauncher="true"][secondaryLauncher="true"]:disabled {{ background:transparent; color:{disabled_text}; border-color:palette(midlight); }}
        QToolButton[completionLauncher="true"] {{ background:palette(highlight); color:palette(highlighted-text); border:1px solid palette(highlight); border-radius:9px; min-height:34px; padding:4px 14px; font-weight:650; }}
        QToolButton[completionLauncher="true"]:hover {{ border-color:palette(text); }}
        QToolButton[completionLauncher="true"]:disabled {{ background:palette(button); color:{disabled_text}; border-color:palette(midlight); }}
        QToolButton[completionLauncher="true"]::menu-button {{ border-left:1px solid palette(midlight); width:22px; }}
        QPushButton[compact="true"], QToolButton[compact="true"] {{ min-height:24px; padding:1px 9px; border-radius:6px; }}
        QPushButton[compact="true"]:focus, QToolButton[compact="true"]:focus {{ padding:0px 8px; }}
        QToolButton[compact="true"]::menu-indicator {{ subcontrol-origin:padding; subcontrol-position:right center; right:3px; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ min-height:30px; border:1px solid palette(mid); border-radius:7px; padding:2px 8px; background:palette(base); }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color:palette(highlight); }}
        QTabWidget::pane {{ border:1px solid palette(midlight); border-radius:7px; top:-1px; }}
        QTabBar::tab {{ background:palette(button); border:1px solid palette(midlight); border-bottom:0; border-top-left-radius:7px; border-top-right-radius:7px; padding:8px 12px; margin-right:2px; }}
        QTabBar::tab:selected {{ background:palette(base); color:palette(text); font-weight:650; }}
        QTabBar::tab:hover:!selected {{ background:palette(midlight); }}
        QSlider#galleryScale::groove:horizontal {{ height:4px; background:palette(midlight); border-radius:2px; }}
        QSlider#galleryScale::sub-page:horizontal {{ background:palette(highlight); border-radius:2px; }}
        QSlider#galleryScale::handle:horizontal {{ background:palette(highlight); border:2px solid palette(base); width:16px; margin:-7px 0; border-radius:9px; }}
        QCheckBox#cardCheck {{ padding:4px 6px; border-radius:5px; }}
        QCheckBox#cardCheck:hover {{ background:palette(midlight); }}
        QCheckBox#cardCheck::indicator {{ width:18px; height:18px; }}
        QRadioButton::indicator, QCheckBox::indicator {{ width:16px; height:16px; }}
        QProgressBar {{ min-height:18px; border:1px solid palette(midlight); border-radius:7px; background:palette(base); color:palette(text); text-align:center; }}
        QProgressBar::chunk {{ background:palette(highlight); border-radius:6px; }}
        QStatusBar {{ background:palette(base); border:0; }}
        QStatusBar::item {{ border:none; }}
        QFrame#statusFooter {{ background:palette(base); border-top:1px solid palette(mid); border-radius:0; }}
        QFrame#statusFooter QPushButton {{ min-height:24px; max-height:24px; padding:1px 9px; border-radius:6px; }}
        QFrame#statusFooter QPushButton:focus {{ padding:0px 8px; }}
        QFrame#statusFooter QLabel {{ font-size:10pt; }}
        QFrame#footerActivityRow, QFrame#globalStatusStrip {{ background:transparent; border:none; }}
        QLabel#footerActivityTitle, QLabel#footerNotification {{ font-weight:600; }}
    """


APP_STYLESHEET=build_stylesheet()


def apply_theme(app,theme: str,color_blind_mode: bool=False) -> str:
    global _system_palette
    if _system_palette is None: _system_palette=QPalette(app.palette())
    theme=theme if theme in THEMES else "system"
    if theme=="light": app.setPalette(_light_palette())
    elif theme=="dark": app.setPalette(_dark_palette())
    elif theme=="midnight": app.setPalette(_dark_palette(midnight=True))
    else: app.setPalette(_system_palette)
    app.setProperty("collectigenHighContrast",bool(theme=="system" and system_high_contrast_enabled()))
    set_color_blind_mode(color_blind_mode); app.setStyleSheet(build_stylesheet(theme,color_blind_mode))
    return theme
