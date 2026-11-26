"""
Floating Window
===============

Base class for the two surfaces the app draws itself (hover card and
island): frameless, translucent, always on top, and never stealing focus
from whatever the user is working in.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QWidget

from . import winapi

__all__ = ['FloatingWindow']


class FloatingWindow(QWidget):
    """A borderless, non-activating, per-pixel-alpha window."""

    def __init__(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self._hits: list[tuple[QRectF, str, object]] = []

    @property
    def native_id(self) -> int:
        return int(self.winId())

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        winapi.configure_floating_window(self.native_id)
        super().showEvent(event)

    def hit_at(self, x: float, y: float) -> tuple[str, object] | None:
        """The interactive element under a point, as ``(kind, payload)``."""
        for rect, kind, payload in self._hits:
            if rect.contains(x, y):
                return kind, payload

        return None
