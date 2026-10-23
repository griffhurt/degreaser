"""App-wide look: design tokens → palette + stylesheet, fonts and recolourable icons."""

from degreaser.ui.theme.icons import icon, pixmap
from degreaser.ui.theme.manager import ThemeManager, theme, theme_override
from degreaser.ui.theme.tokens import DARK, LIGHT, Tokens

__all__ = ["DARK", "LIGHT", "ThemeManager", "Tokens", "icon", "pixmap", "theme", "theme_override"]
