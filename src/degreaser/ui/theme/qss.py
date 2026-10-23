"""The app stylesheet, generated from tokens.

Variants are opted into with dynamic properties, e.g. button.setProperty("variant", "primary")
or label.setProperty("role", "muted"); see the selectors below.
"""

from degreaser.ui.theme.tokens import Tokens


def stylesheet(
    t: Tokens, arrow_path: str = "", check_path: str = "", mono_family: str = "monospace"
) -> str:
    """arrow_path/check_path: themed images (QSS can only draw these from files).
    mono_family: the platform's fixed-width font, for text previews."""
    r, rl = t.radius, t.radius_lg
    return f"""
* {{
    font-family: "Inter";
    font-size: {t.font_size}px;
    color: {t.text};
    outline: none;
}}
QMainWindow, QDialog {{ background: {t.bg}; }}
QWidget#central {{ background: {t.bg}; }}
/* Scrolling dialog bodies show the dialog's own background. */
QScrollArea#dialogScroll, QWidget#dialogContent {{ background: transparent; }}

/* Labels ---------------------------------------------------------------------------- */
QLabel[role="title"] {{ font-size: {t.font_size_title}px; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {t.text_muted}; }}
QLabel[role="faint"] {{ color: {t.text_faint}; font-size: {t.font_size_small}px; }}
QLabel[role="error"] {{ color: {t.danger}; font-size: {t.font_size_small}px; }}
QLabel[role="success"] {{ color: {t.success}; font-size: {t.font_size_small}px; }}
QLabel[role="warning"] {{ color: {t.warning}; }}

/* Cards and bars ----------------------------------------------------------------------- */
QFrame[role="card"] {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {rl}px;
}}
QFrame[role="card"][selected="true"] {{ border: 2px solid {t.accent}; }}
QFrame[role="toolbar"] {{
    background: {t.surface};
    border: none;
    border-bottom: 1px solid {t.border};
}}
QFrame[role="actionbar"] {{
    background: {t.surface};
    border: none;
    border-top: 1px solid {t.border};
}}
/* Content inside cards is flat: the card draws the border. */
QFrame[role="card"] QAbstractScrollArea {{ border: none; background: transparent; }}
QFrame[role="card"] QGraphicsView, QFrame[role="card"] QPdfView {{
    background: {t.surface_sunken};
    border-radius: {rl}px;
}}
QFrame[role="pill"] {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 13px;
}}

/* Buttons ------------------------------------------------------------------------------ */
QPushButton, QToolButton[variant="button"] {{
    background: {t.surface};
    border: 1px solid {t.border_strong};
    border-radius: {r}px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover, QToolButton[variant="button"]:hover {{ background: {t.surface_alt}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.text_faint}; border-color: {t.border}; }}
QPushButton:default {{ border-color: {t.accent}; }}
QPushButton[variant="primary"] {{
    background: {t.accent};
    border: 1px solid {t.accent};
    color: {t.accent_text};
}}
QPushButton[variant="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.border}; border-color: {t.border}; color: {t.text_faint}; }}
QPushButton[variant="danger"] {{ color: {t.danger}; }}
QPushButton[variant="danger"]:hover {{ background: {t.danger_tint}; border-color: {t.danger}; }}
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 4px 8px;
}}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{ background: {t.surface_alt}; }}
QToolButton[variant="crumb"] {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    color: {t.text_muted};
    font-weight: 500;
}}
QToolButton[variant="crumb"]:hover {{ background: {t.surface_alt}; color: {t.text}; }}

/* Inputs ------------------------------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {t.surface};
    border: 1px solid {t.border_strong};
    border-radius: {r}px;
    padding: 6px 10px;
    selection-background-color: {t.accent};
    selection-color: {t.accent_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {t.accent}; }}
QLineEdit[variant="command"] {{ font-size: 15px; padding: 8px 12px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url("{arrow_path}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {r}px;
    selection-background-color: {t.accent_tint};
    selection-color: {t.text};
    padding: 4px;
}}
QPlainTextEdit[mono="true"], QTextEdit[mono="true"] {{ font-family: "{mono_family}"; font-size: 12px; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 2px solid {t.control_border};
    background: {t.surface};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{
    background: {t.accent};
    border-color: {t.accent};
    image: url("{check_path}");
}}
QRadioButton::indicator:checked {{
    width: 8px;
    height: 8px;
    border: 5px solid {t.accent};
    background: {t.accent_text};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {t.border_strong};
    background: {t.surface_alt};
}}
QCheckBox::indicator:checked:disabled {{ background: {t.border_strong}; border-color: {t.border_strong}; }}
QRadioButton::indicator:checked:disabled {{ border-color: {t.border_strong}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t.text_faint}; }}

/* Group boxes -------------------------------------------------------------------------- */
QGroupBox {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {rl}px;
    margin-top: 22px;
    padding: 6px 12px 10px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 4px;
    padding: 0 2px;
    color: {t.text_muted};
}}

/* Tables, trees, lists ----------------------------------------------------------------- */
QTableView, QTreeView, QListView, QTableWidget, QTreeWidget, QListWidget {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {rl}px;
    gridline-color: {t.border};
    alternate-background-color: {t.surface_alt};
    selection-background-color: {t.accent_tint};
    selection-color: {t.text};
}}
QListWidget[variant="cards"] {{ background: transparent; border: none; }}
QListWidget[variant="cards"]::item {{ border: none; padding: 0; margin: 0 0 8px 0; }}
QListWidget[variant="cards"]::item:selected {{ background: transparent; }}
QTreeView::item, QTableView::item {{ padding: 3px 6px; }}
QHeaderView::section {{
    background: {t.surface};
    color: {t.text_muted};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 6px 8px;
    font-weight: 600;
    font-size: {t.font_size_small}px;
}}
QTableCornerButton::section {{ background: {t.surface}; border: none; }}

/* Scrollbars --------------------------------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {t.border_strong}; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* Menus and tooltips ------------------------------------------------------------------- */
QMenu {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {r}px;
    padding: 6px;
}}
QMenu::item {{ padding: 6px 28px 6px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.surface_alt}; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 6px; }}
QMenuBar {{ background: {t.bg}; }}
QMenuBar::item:selected {{ background: {t.surface_alt}; border-radius: 6px; }}
QToolTip {{
    background: {t.text};
    color: {t.bg};
    border: none;
    border-radius: 6px;
    padding: 5px 8px;
}}

/* Progress, splitters, status bar ------------------------------------------------------ */
QProgressBar {{
    background: {t.surface_alt};
    border: none;
    border-radius: 3px;
    max-height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QStatusBar {{ background: {t.bg}; color: {t.text_muted}; }}
QStatusBar QLabel {{ color: {t.text_muted}; font-size: {t.font_size_small}px; }}
QStatusBar::item {{ border: none; }}
QMessageBox QLabel {{ font-size: {t.font_size}px; }}
"""
