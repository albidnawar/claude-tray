"""
Mascot
======

A small pixel-art Claude drawn entirely in code, in Claude's coral.  It shows a
session's state at a glance: it bobs and shuffles its feet while a task is
running, wears a green check when the task is finished, an amber mark when it
needs you, and dims when nothing is going on.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from . import theme
from .activity import DONE, IDLE, NEEDS_INPUT, RUNNING

__all__ = ['draw_mascot', 'ASPECT']

BODY = QColor('#D97757')
BODY_LIGHT = QColor('#E8906F')
EYE = QColor(38, 24, 20)

_COLUMNS = 13
_ROWS = 8
ASPECT = _COLUMNS / _ROWS

_HEAD = (
    '...XXXXXXX...',
    '...XEXXXEX...',
    '.XXXEXXXEXXX.',
    '.XXXXXXXXXXX.',
    '...XXXXXXX...',
)
_LEGS = (
    ('...X.X.X.X...', '...X.X.X.X...'),
    ('..X.X...X.X..', '..X.X...X.X..'),
)
_BLINK_PERIOD = 3.4
_BLINK_LENGTH = 0.13


def draw_mascot(painter: QPainter, rect: QRectF, state: str, seconds: float, badge: bool = True) -> None:
    """Draw the mascot centred in ``rect``; ``seconds`` is any steadily increasing clock that drives the animation."""
    unit = min(rect.width() / _COLUMNS, rect.height() / _ROWS)
    width, height = unit * _COLUMNS, unit * _ROWS
    left = rect.center().x() - width / 2
    top = rect.center().y() - height / 2

    running = state == RUNNING
    bob = -abs(math.sin(seconds * 7.0)) * unit * 0.55 if running else 0.0
    legs = _LEGS[int(seconds * 6.0) % 2] if running else _LEGS[0]
    blinking = (seconds % _BLINK_PERIOD) < _BLINK_LENGTH

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if state == IDLE:
        painter.setOpacity(painter.opacity() * 0.5)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.translate(left, top + bob)
    for row_index, row in enumerate(_HEAD):
        _draw_row(painter, row, row_index, unit, blinking)
    for offset, row in enumerate(legs):
        _draw_row(painter, row, len(_HEAD) + offset, unit, False)
    painter.restore()

    if badge and state in (DONE, NEEDS_INPUT):
        _draw_badge(painter, rect, state, unit)


def _draw_row(painter: QPainter, row: str, row_index: int, unit: float, blinking: bool) -> None:
    for column, cell in enumerate(row):
        if cell == '.':
            continue

        if cell == 'E':
            closed_top = blinking and row_index == 1
            painter.setBrush(BODY_LIGHT if closed_top else EYE)
        else:
            painter.setBrush(BODY_LIGHT if row_index == 0 else BODY)

        painter.drawRect(QRectF(column * unit, row_index * unit, unit + 0.4, unit + 0.4))


def _draw_badge(painter: QPainter, rect: QRectF, state: str, unit: float) -> None:
    diameter = max(7.0, unit * 4.6)
    centre = QPointF(rect.right() - diameter * 0.2, rect.bottom() - diameter * 0.35)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(0, 0, 0, 230), max(1.0, diameter * 0.16)))
    painter.setBrush(theme.GREEN if state == DONE else theme.ORANGE)
    painter.drawEllipse(centre, diameter / 2, diameter / 2)

    glyph = QPen(QColor(255, 255, 255), max(1.2, diameter * 0.17))
    glyph.setCapStyle(Qt.PenCapStyle.RoundCap)
    glyph.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(glyph)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if state == DONE:
        path.moveTo(centre.x() - diameter * 0.24, centre.y() + diameter * 0.02)
        path.lineTo(centre.x() - diameter * 0.06, centre.y() + diameter * 0.2)
        path.lineTo(centre.x() + diameter * 0.26, centre.y() - diameter * 0.18)
    else:
        path.moveTo(centre.x(), centre.y() - diameter * 0.24)
        path.lineTo(centre.x(), centre.y() + diameter * 0.04)
        path.moveTo(centre.x(), centre.y() + diameter * 0.22)
        path.lineTo(centre.x(), centre.y() + diameter * 0.23)
    painter.drawPath(path)
    painter.restore()
