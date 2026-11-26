"""
Tray Icon Rendering
===================

Draws the notification-area icon at every size Windows might ask for, so it
stays sharp at 100% to 300% display scaling.  The icon is deliberately
simple: the session percentage as a large number, the weekly usage as a bar
underneath, and a dot in the account's colour in the corner.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

from . import theme
from .formatting import severity

__all__ = ['render_icon', 'build_icon']

_SIZES = (16, 20, 24, 32, 40, 48, 64)


def build_icon(session: float | None, weekly: float | None, account_index: int, account_count: int, light_taskbar: bool) -> QIcon:
    """An icon holding one crisp pixmap per supported size."""
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(render_icon(size, session, weekly, account_index, account_count, light_taskbar))

    return icon


def render_icon(size: int, session: float | None, weekly: float | None, account_index: int, account_count: int, light_taskbar: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)

    neutral = QColor(24, 24, 27) if light_taskbar else QColor(255, 255, 255)
    number_colour = QColor(neutral) if severity(session) == 'ok' else _on_background(theme.severity_color(session), light_taskbar)

    _draw_number(painter, size, session, number_colour)
    _draw_bottom_row(painter, size, weekly, light_taskbar, theme.account_color(account_index) if account_count > 1 else None)

    painter.end()
    return pixmap


def _draw_number(painter: QPainter, size: int, session: float | None, colour: QColor) -> None:
    text = '--' if session is None else str(min(999, round(session)))
    scale = {1: 0.98, 2: 0.9, 3: 0.62}.get(len(text), 0.62)
    area_width = size * 0.96
    area_height = size * 0.78

    font = theme.ui_font(size * scale, QFont.Weight.Bold)
    path = QPainterPath()
    path.addText(0, 0, font, text)
    bounds = path.boundingRect()
    if bounds.width() > area_width or bounds.height() > area_height:
        shrink = min(area_width / bounds.width(), area_height / bounds.height())
        font = theme.ui_font(size * scale * shrink, QFont.Weight.Bold)
        path = QPainterPath()
        path.addText(0, 0, font, text)
        bounds = path.boundingRect()

    left = (size - bounds.width()) / 2 - bounds.left()
    top = (area_height - bounds.height()) / 2 - bounds.top() + size * 0.02
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPath(path.translated(left, top))


def _draw_bottom_row(painter: QPainter, size: int, weekly: float | None, light_taskbar: bool, account_colour: QColor | None) -> None:
    """Weekly usage as a bar; with several accounts a dot in the account's colour sits at its left end."""
    height = max(2.0, size * 0.13)
    margin = size * 0.06
    centre_y = size - size * 0.05 - height / 2
    left = margin

    painter.setPen(Qt.PenStyle.NoPen)
    if account_colour is not None:
        diameter = max(3.0, size * 0.2)
        painter.setBrush(account_colour)
        painter.drawEllipse(QPointF(left + diameter / 2, centre_y), diameter / 2, diameter / 2)
        left += diameter + size * 0.07

    rect = QRectF(left, centre_y - height / 2, size - margin - left, height)
    radius = height / 2
    painter.setBrush(QColor(0, 0, 0, 55) if light_taskbar else QColor(255, 255, 255, 60))
    painter.drawRoundedRect(rect, radius, radius)

    if weekly is not None and weekly > 0:
        width = max(height, rect.width() * min(100.0, weekly) / 100.0)
        painter.setBrush(theme.severity_color(weekly))
        painter.drawRoundedRect(QRectF(rect.left(), rect.top(), width, height), radius, radius)


def _on_background(colour: QColor, light_taskbar: bool) -> QColor:
    """Severity colours darkened a touch so they keep their contrast on a light taskbar."""
    return colour.darker(135) if light_taskbar else colour
