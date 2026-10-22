"""Design tokens. Every colour, radius and size the UI uses comes from here."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    dark: bool
    # Surfaces
    bg: str  # window background
    surface: str  # cards, panels, inputs
    surface_alt: str  # hover, subtle fills
    surface_sunken: str  # preview canvas
    border: str
    border_strong: str
    # Text
    text: str
    text_muted: str
    text_faint: str
    # Accent and states (each with a tint for backgrounds)
    accent: str
    accent_hover: str
    accent_text: str  # text on accent
    accent_tint: str
    success: str
    success_tint: str
    danger: str
    danger_tint: str
    warning: str
    warning_tint: str
    info: str
    info_tint: str
    purple: str
    purple_tint: str
    shadow: str
    # Shape and type
    radius: int = 8
    radius_lg: int = 12
    font_size: int = 13
    font_size_small: int = 12
    font_size_title: int = 22


LIGHT = Tokens(
    dark=False,
    bg="#f6f6f7",
    surface="#ffffff",
    surface_alt="#f0f0f2",
    surface_sunken="#ececee",
    border="#e3e3e6",
    border_strong="#cfcfd4",
    text="#18181b",
    text_muted="#6b6b75",
    text_faint="#a1a1aa",
    accent="#5b5bd6",
    accent_hover="#4c4cc7",
    accent_text="#ffffff",
    accent_tint="#ececfb",
    success="#16a34a",
    success_tint="#e7f6ec",
    danger="#dc2626",
    danger_tint="#fdeaea",
    warning="#c2710c",
    warning_tint="#fdf1e2",
    info="#2563eb",
    info_tint="#e6eefd",
    purple="#7c3aed",
    purple_tint="#f0e9fd",
    shadow="#1a000000",
)

DARK = Tokens(
    dark=True,
    bg="#0f0f11",
    surface="#18181b",
    surface_alt="#222226",
    surface_sunken="#0b0b0d",
    border="#2a2a30",
    border_strong="#3b3b42",
    text="#ececef",
    text_muted="#a0a0aa",
    text_faint="#6b6b75",
    accent="#7c7cf0",
    accent_hover="#9090f4",
    accent_text="#ffffff",
    accent_tint="#23233d",
    success="#22c55e",
    success_tint="#13281b",
    danger="#f05252",
    danger_tint="#331618",
    warning="#f59e0b",
    warning_tint="#33240d",
    info="#3b82f6",
    info_tint="#142340",
    purple="#a78bfa",
    purple_tint="#251d3d",
    shadow="#66000000",
)
