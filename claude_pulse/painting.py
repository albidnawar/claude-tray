"""
Painting Primitives
===================

Reusable drawing helpers shared by the tray icon, the hover card and the
island: soft shadows, progress rings, progress bars with a time-elapsed
marker, an iOS-style switch, and text.  Everything is plain QPainter work so
the result is crisp and antialiased on every display scale.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPainterPathStroker, QPen

from . import theme

__all__ = ['soft_shadow', 'draw_ring', 'draw_bar', 'draw_switch', 'draw_refresh_icon', 'draw_text', 'text_width']


def soft_shadow(painter: QPainter, body: QRectF, radius: float, spread: int = 20, offset_y: float = 8.0, strength: float = 0.5) -> None:
    """A soft drop shadow built from stacked translucent rounded rectangles."""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    for step in range(spread, 0, -1):
        falloff = (1.0 - step / (spread + 1)) ** 2
        colour = QColor(0, 0, 0, max(1, round(255 * strength * falloff / 5.5)))
        painter.setBrush(colour)
        grown = body.adjusted(-step, -step, step, step).translated(0, offset_y * (1 - step / (spread * 1.6)))
        painter.drawRoundedRect(grown, radius + step, radius + step)
    painter.restore()


def draw_ring(painter: QPainter, centre: QPointF, radius: float, width: float, percent: float | None, colour: QColor) -> None:
    """A progress ring that starts at 12 o'clock and runs clockwise."""
    painter.save()
    box = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)

    track_pen = QPen(theme.TRACK, width)
    painter.setPen(track_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(box)

    if percent is not None and percent > 0:
        span = min(100.0, percent) / 100.0 * 360.0
        pen = QPen(colour, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(box, 90 * 16, -round(span * 16))

    painter.restore()


def draw_bar(painter: QPainter, rect: QRectF, percent: float | None, colour: QColor, marker: float | None = None) -> None:
    """A rounded progress bar; ``marker`` (0..1) is where the clock stands within the window."""
    painter.save()
    radius = rect.height() / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.TRACK)
    painter.drawRoundedRect(rect, radius, radius)

    if percent is not None and percent > 0:
        width = max(rect.height(), rect.width() * min(100.0, percent) / 100.0)
        fill = QRectF(rect.left(), rect.top(), width, rect.height())
        gradient = QLinearGradient(fill.topLeft(), fill.bottomLeft())
        gradient.setColorAt(0.0, colour.lighter(118))
        gradient.setColorAt(1.0, colour)
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, radius, radius)

    if marker is not None:
        x = rect.left() + rect.width() * min(1.0, max(0.0, marker))
        overhang = max(1.5, rect.height() * 0.45)
        pen = QPen(QColor(255, 255, 255, 215), max(1.4, rect.height() * 0.28))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, rect.top() - overhang), QPointF(x, rect.bottom() + overhang))

    painter.restore()


def draw_switch(painter: QPainter, rect: QRectF, enabled: bool) -> None:
    """An iOS-style on/off switch filling ``rect``."""
    painter.save()
    radius = rect.height() / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.GREEN if enabled else QColor(255, 255, 255, 52))
    painter.drawRoundedRect(rect, radius, radius)

    inset = 2.0
    knob = rect.height() - inset * 2
    left = rect.right() - inset - knob if enabled else rect.left() + inset
    painter.setBrush(QColor(255, 255, 255, 250))
    painter.drawEllipse(QRectF(left, rect.top() + inset, knob, knob))
    painter.restore()


def draw_refresh_icon(painter: QPainter, rect: QRectF, colour: QColor, spin_degrees: float = 0.0) -> None:
    """A clockwise-arrow refresh glyph centred in ``rect``: an open ring with a solid arrowhead on its end."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.center())
    painter.rotate(spin_degrees)

    radius = min(rect.width(), rect.height()) * 0.3
    stroke = max(1.5, radius * 0.26)
    end_angle = 78.0
    sweep = 292.0
    box = QRectF(-radius, -radius, radius * 2, radius * 2)

    ring = QPainterPath()
    ring.arcMoveTo(box, end_angle + sweep)
    ring.arcTo(box, end_angle + sweep, -sweep)
    stroker = QPainterPathStroker()
    stroker.setWidth(stroke)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline = stroker.createStroke(ring)

    # Arrowhead at the end of the ring, pointing along the clockwise tangent.
    theta = math.radians(end_angle)
    point = QPointF(radius * math.cos(theta), -radius * math.sin(theta))
    tangent = QPointF(math.sin(theta), math.cos(theta))
    normal = QPointF(-tangent.y(), tangent.x())
    length = stroke * 2.1
    half_width = stroke * 1.35
    tip = point + tangent * (length * 0.55)
    base = point - tangent * (length * 0.45)
    head = QPainterPath()
    head.moveTo(tip)
    head.lineTo(base + normal * half_width)
    head.lineTo(base - normal * half_width)
    head.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    painter.drawPath(outline.united(head))  # one shape, so a translucent colour never double-paints where they overlap
    painter.restore()


def draw_text(painter: QPainter, rect: QRectF, text: str, font: QFont, colour: QColor, align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> None:
    """Draw single-line ``text`` vertically centred in ``rect``."""
    painter.save()
    painter.setFont(font)
    painter.setPen(colour)
    painter.drawText(rect, int(align | Qt.AlignmentFlag.AlignVCenter), text)
    painter.restore()


def text_width(text: str, font: QFont) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)
