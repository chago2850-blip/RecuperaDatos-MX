"""Temas oscuro y claro."""

DARK = {
    "bg": "#0B1523", "panel": "#111E30", "panel2": "#172840", "border": "#22385A", "text": "#E6F0FA",
    "muted": "#8CA3BD", "accent": "#21D6B0", "accent2": "#18A0FB", "warn": "#F5A524", "bad": "#F0525A",
    "good": "#2ED47A", "sel": "#1C4F7A", "input": "#0E1A2A", "icon": "#CFE3F5",
}
LIGHT = {
    "bg": "#F3F6FA", "panel": "#FFFFFF", "panel2": "#EAF0F7", "border": "#D3DEEA", "text": "#13243A",
    "muted": "#5A6F88", "accent": "#0FA88A", "accent2": "#1479D6", "warn": "#C77D00", "bad": "#D23A42",
    "good": "#1E9E5A", "sel": "#CFE4FA", "input": "#FFFFFF", "icon": "#2A4566",
}


def qss(c: dict) -> str:
    return f"""
* {{ font-size: 13px; color: {c['text']}; }}
QMainWindow, QWidget#Root {{ background: {c['bg']}; }}
QWidget#Sidebar {{ background: {c['panel']}; border-right: 1px solid {c['border']}; }}
QLabel#AppName {{ font-size: 15px; font-weight: 700; }}
QLabel#AppSub {{ color: {c['muted']}; font-size: 11px; }}
QLabel#H1 {{ font-size: 22px; font-weight: 700; }}
QLabel#H2 {{ font-size: 15px; font-weight: 650; }}
QLabel#Muted, QLabel[muted="true"] {{ color: {c['muted']}; }}
QLabel#Banner {{ background: {c['warn']}22; border: 1px solid {c['warn']}; border-radius: 10px; padding: 10px 14px; }}
QFrame#Card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 14px; }}
QToolButton#Nav {{ background: transparent; border: none; border-radius: 10px; padding: 9px 12px;
    text-align: left; color: {c['muted']}; font-weight: 550; }}
QToolButton#Nav:hover {{ background: {c['panel2']}; color: {c['text']}; }}
QToolButton#Nav:checked {{ background: {c['sel']}; color: {c['text']}; }}
QPushButton {{ background: {c['panel2']}; border: 1px solid {c['border']}; border-radius: 9px; padding: 8px 14px; }}
QPushButton:hover {{ border-color: {c['accent2']}; }}
QPushButton:pressed {{ background: {c['sel']}; }}
QPushButton:disabled {{ color: {c['muted']}; background: {c['panel']}; }}
QPushButton#Primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {c['accent']}, stop:1 {c['accent2']});
    border: none; color: #04121F; font-weight: 700; }}
QPushButton#Primary:disabled {{ background: {c['panel2']}; color: {c['muted']}; }}
QPushButton#Danger {{ background: {c['bad']}; border: none; color: white; font-weight: 700; }}
QPushButton#Big {{ text-align: left; padding: 14px 16px; font-size: 14px; font-weight: 600; border-radius: 12px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {c['input']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {c['accent2']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; background: {c['panel2']}; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['border']}; selection-background-color: {c['sel']}; }}
QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{ background: {c['panel']}; border: 1px solid {c['border']};
    border-radius: 10px; alternate-background-color: {c['panel2']}; gridline-color: {c['border']};
    selection-background-color: {c['sel']}; selection-color: {c['text']}; }}
QHeaderView::section {{ background: {c['panel2']}; color: {c['muted']}; border: none; border-right: 1px solid {c['border']};
    padding: 6px 8px; font-weight: 600; }}
QProgressBar {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 8px; height: 16px; text-align: center; }}
QProgressBar::chunk {{ border-radius: 7px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c['accent']}, stop:1 {c['accent2']}); }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 8px 14px; color: {c['muted']}; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QToolTip {{ background: {c['panel2']}; color: {c['text']}; border: 1px solid {c['accent2']}; border-radius: 6px; padding: 6px 8px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QSplitter::handle {{ background: {c['border']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 10px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {c['muted']}; }}
"""
