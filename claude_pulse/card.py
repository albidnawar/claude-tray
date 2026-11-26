"""
Hover Card
==========

The panel that appears above the tray icon.  Hovering the icon shows it for
as long as the pointer stays on the icon or the card; clicking the icon pins
it open until you click elsewhere.  It shows one account at a time (session
and weekly usage, resets) with a segmented control to switch accounts, plus a
refresh button and a control that sets the Dynamic Island to off, drop-down on hover, or always visible.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QMouseEvent, QPainter, QPaintEvent

from . import painting, theme
from .floating import FloatingWindow
from .formatting import age_text, countdown, percent_text, reset_clock
from .quotas import Quota
from .store import AccountState, UsageStore
from .tween import Tweener

__all__ = ['HoverCard']

MARGIN = 30
BODY_WIDTH = 320
_PADDING = 16
_RADIUS = 22.0
_EDGE_GAP = 10
_HOVER_DWELL_SECONDS = 0.12
_HOVER_LEAVE_SECONDS = 0.32
_FADE_MS = 170
_CLICK_GRACE_SECONDS = 0.4
_SLIDE_PIXELS = 12

_ISLAND_CHOICES = (('off', 'Off'), ('hover', 'Hover'), ('always', 'Always'))
_ERROR_TEXT = {
    'no_token': ('Not signed in', 'Run  claude auth login'),
    'auth': ('Sign-in expired', 'Run  claude auth login'),
    'network': ("Can't reach Claude", 'Check your connection'),
    'rate_limit': ('Too many requests', 'Retrying shortly'),
    'server': ('Claude is having trouble', 'Retrying shortly'),
}


class HoverCard(FloatingWindow):
    """Usage details for the selected account, anchored above the tray icon."""

    island_mode_selected = Signal(str)
    refresh_requested = Signal()

    def __init__(self, store: UsageStore, tweener: Tweener, use_24h: bool) -> None:
        super().__init__()
        self._store = store
        self._tween = tweener
        self._use_24h = use_24h
        self._island_mode = 'off'
        self._spin_angle = 0.0
        self._pinned = False
        self._reveal = 0.0
        self._slide = 1
        self._body_height = 240.0
        self._icon_rect = QRect()
        self._cursor_anchor = QPoint()
        self._hover_since: float | None = None
        self._leave_since: float | None = None
        self._was_down = False
        self._pinned_at = 0.0
        self._hold_hidden = False

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(self._on_fade_finished)

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self.update)

        self._spin = QTimer(self)
        self._spin.setInterval(33)
        self._spin.timeout.connect(self._spin_step)

        store.changed.connect(self._on_data_changed)
        tweener.moved.connect(self.update)

    # -- public API ---------------------------------------------------------------------------------------------------

    @property
    def pinned(self) -> bool:
        return self._pinned

    def is_busy(self) -> bool:
        """True while the card is on screen or still fading, so the app should not be trimmed."""
        return self.isVisible() or self._fade.state() == QVariantAnimation.State.Running

    def set_island_mode(self, mode: str) -> None:
        self._island_mode = mode
        self.update()

    def _start_spinning(self) -> None:
        if self._store.refreshing and not self._spin.isActive():
            self._spin.start()

    def _spin_step(self) -> None:
        self._spin_angle += 14.0
        if not self._store.refreshing and self._spin_angle >= 360.0:
            self._spin_angle = 0.0
            self._spin.stop()
        self.update()

    def present(self, icon_rect: QRect, cursor: QPoint, pinned: bool) -> None:
        """Show the card near the icon (or the cursor when the icon's position is unknown)."""
        self._icon_rect = icon_rect
        self._cursor_anchor = cursor
        if pinned and not self._pinned:
            self._pinned_at = time.monotonic()
        self._pinned = self._pinned or pinned
        self._leave_since = None
        self._remeasure()
        self._place()
        self._clock.start()
        self._store.watching = True
        self._store.ensure_fresh()
        self._start_spinning()

        if not self.isVisible():
            self._reveal = 0.0
            self.show()

        self._run_fade(1.0)

    def dismiss(self, hold_hidden: bool = False) -> None:
        """Close the card; with ``hold_hidden`` it stays closed until the pointer has left the icon once."""
        self._pinned = False
        self._hover_since = None
        self._leave_since = None
        self._hold_hidden = hold_hidden
        if self.isVisible():
            self._run_fade(0.0)

    def hide_for_menu(self) -> None:
        self.dismiss(hold_hidden=True)

    def toggle_pinned(self, icon_rect: QRect, cursor: QPoint) -> None:
        """Left click on the tray icon: pin the card open, or close it when it is already pinned."""
        if self._pinned:
            self.dismiss(hold_hidden=True)
            return

        self._hold_hidden = False
        self.present(icon_rect, cursor, pinned=True)

    def body_global_rect(self) -> QRect:
        return QRect(self.x() + MARGIN, self.y() + MARGIN, BODY_WIDTH, round(self._body_height))

    def tick(self, pos: QPoint, icon_rect: QRect, button_down: bool) -> None:
        """Called ~20 times a second with the cursor position; drives hover show/hide and click-away."""
        over_icon = not icon_rect.isEmpty() and icon_rect.adjusted(-3, -3, 3, 3).contains(pos)
        showing = self.isVisible() and self._fade.endValue() != 0.0
        over_card = showing and self.body_global_rect().adjusted(-8, -8, 8, 8).contains(pos)
        pressed_now = button_down and not self._was_down
        self._was_down = button_down

        if self._pinned:
            if pressed_now and not over_card and not over_icon and time.monotonic() - self._pinned_at > _CLICK_GRACE_SECONDS:
                self.dismiss()
            return

        if self._hold_hidden:
            if over_icon or over_card:
                return
            self._hold_hidden = False

        now = time.monotonic()
        if over_icon or over_card:
            self._leave_since = None
            if not showing and over_icon:
                self._hover_since = self._hover_since or now
                if now - self._hover_since >= _HOVER_DWELL_SECONDS:
                    self.present(icon_rect, pos, pinned=False)
            return

        self._hover_since = None
        if showing:
            self._leave_since = self._leave_since or now
            if now - self._leave_since >= _HOVER_LEAVE_SECONDS:
                self.dismiss()

    # -- animation & placement ----------------------------------------------------------------------------------------

    def _run_fade(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._reveal)
        self._fade.setEndValue(target)
        self._fade.start()

    def _on_fade(self, value: object) -> None:
        self._reveal = float(value)  # type: ignore[arg-type]
        self.update()

    def _on_fade_finished(self) -> None:
        if self._fade.endValue() == 0.0:
            self.hide()
            self._clock.stop()
            self._store.watching = False

    def _on_data_changed(self) -> None:
        if not self.isVisible():
            return

        self._start_spinning()
        self._remeasure()
        self._place()
        self.update()

    def _remeasure(self) -> None:
        scratch = QImage(1, 1, QImage.Format.Format_ARGB32)
        painter = QPainter(scratch)
        self._body_height = self._paint_body(painter, QRectF(0, 0, BODY_WIDTH, 0), record_hits=False)
        painter.end()
        self.resize(BODY_WIDTH + MARGIN * 2, round(self._body_height) + MARGIN * 2)

    def _place(self) -> None:
        anchor = self._icon_rect.center() if not self._icon_rect.isEmpty() else self._cursor_anchor
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        full, work = screen.geometry(), screen.availableGeometry()
        width, height = BODY_WIDTH, round(self._body_height)

        if work.bottom() < full.bottom() - 4:
            top, self._slide = work.bottom() - _EDGE_GAP - height, 1
            left = _clamp(anchor.x() - width // 2, work.left() + _EDGE_GAP, work.right() - _EDGE_GAP - width)
        elif work.top() > full.top() + 4:
            top, self._slide = work.top() + _EDGE_GAP, -1
            left = _clamp(anchor.x() - width // 2, work.left() + _EDGE_GAP, work.right() - _EDGE_GAP - width)
        else:
            on_left = work.left() > full.left() + 4
            left = work.left() + _EDGE_GAP if on_left else work.right() - _EDGE_GAP - width
            top = _clamp(anchor.y() - height // 2, work.top() + _EDGE_GAP, work.bottom() - _EDGE_GAP - height)
            self._slide = 1

        self.move(left - MARGIN, top - MARGIN)

    # -- painting -----------------------------------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.setOpacity(self._reveal)
        painter.translate(0, (1.0 - self._reveal) * _SLIDE_PIXELS * self._slide)

        body = QRectF(MARGIN, MARGIN, BODY_WIDTH, self._body_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.BODY)
        painting.soft_shadow(painter, body, _RADIUS)
        painter.setPen(theme.STROKE)
        painter.setBrush(theme.BODY)
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), _RADIUS, _RADIUS)

        self._hits = []
        self._paint_body(painter, body, record_hits=True)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        hit = self.hit_at(event.position().x(), event.position().y())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            return

        hit = self.hit_at(event.position().x(), event.position().y())
        if hit is None:
            return

        kind, payload = hit
        if kind == 'account':
            self._store.select(str(payload))
        elif kind == 'refresh':
            self.refresh_requested.emit()
            QTimer.singleShot(0, self._start_spinning)
        elif kind == 'island_mode':
            self.island_mode_selected.emit(str(payload))

    def _paint_body(self, painter: QPainter, body: QRectF, record_hits: bool) -> float:
        """Lay out and draw the content; returns the total body height."""
        state = self._store.active
        left = body.left() + _PADDING
        inner = body.width() - _PADDING * 2
        y = body.top() + _PADDING - 2

        if state is None:
            self._text(painter, QRectF(left, y, inner, 60), 'No Claude accounts found', 13, QFont.Weight.Medium, theme.TEXT_DIM)
            return 60 + _PADDING * 2

        y += self._paint_header(painter, left, y, inner, record_hits)
        y += 12
        y += self._paint_identity(painter, left, y, inner, state)
        y += 12

        if state.has_data:
            for quota in (state.session, state.weekly):
                if quota is not None:
                    y += self._paint_metric(painter, left, y, inner, state, quota)
            for quota in state.extras[:3]:
                y += self._paint_extra(painter, left, y, inner, state, quota)
        else:
            y += self._paint_message(painter, left, y, inner, state)

        y += 4
        painter.setPen(QColor(255, 255, 255, 20))
        painter.drawLine(QPointF(left, y), QPointF(left + inner, y))
        y += 8
        y += self._paint_footer(painter, left, y, inner, state, record_hits)
        return y + _PADDING - 6 - body.top()

    def _paint_header(self, painter: QPainter, left: float, y: float, inner: float, record_hits: bool) -> float:
        accounts = self._store.accounts
        height = 32.0
        track = QRectF(left, y, inner, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16))
        painter.drawRoundedRect(track, 11, 11)

        segment_width = (inner - 4) / len(accounts)
        for index, account in enumerate(accounts):
            cell = QRectF(track.left() + 2 + index * segment_width, track.top() + 2, segment_width, height - 4)
            selected = account.id == self._store.active_id
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 38))
                painter.drawRoundedRect(cell, 9, 9)

            font = theme.ui_font(12.5, QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
            label_width = painting.text_width(account.label, font)
            start = cell.center().x() - (7 + 7 + label_width) / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.account_color(index))
            painter.drawEllipse(QPointF(start + 3.5, cell.center().y()), 3.5, 3.5)
            painting.draw_text(
                painter, QRectF(start + 14, cell.top(), label_width + 4, cell.height()), account.label, font,
                theme.TEXT if selected else theme.TEXT_DIM,
            )
            if record_hits:
                self._hits.append((cell, 'account', account.id))

        return height

    def _paint_identity(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState) -> float:
        parts = [part for part in (state.full_name, state.plan) if part]
        text = '  ·  '.join(parts) if parts else state.config.config_dir.name
        self._text(painter, QRectF(left + 2, y, inner, 14), text, 11.5, QFont.Weight.Normal, theme.TEXT_DIM)
        return 14

    def _paint_metric(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState, quota: Quota) -> float:
        percent = self._tween.get(f'{state.id}:{quota.key}', quota.percent)
        colour = theme.severity_color(percent)
        stale = state.is_stale()
        if stale:
            painter.setOpacity(painter.opacity() * 0.6)

        self._text(painter, QRectF(left + 2, y, inner * 0.6, 17), quota.label, 13, QFont.Weight.DemiBold, theme.TEXT)
        self._text(painter, QRectF(left + 2, y + 18, inner * 0.75, 15), self._reset_text(quota), 11.5, QFont.Weight.Normal, theme.TEXT_FAINT)

        big = theme.TEXT if percent < 70 else colour
        painting.draw_text(
            painter, QRectF(left + inner * 0.45, y - 3, inner * 0.55 - 2, 36), percent_text(percent),
            theme.ui_font(30, QFont.Weight.DemiBold), big, Qt.AlignmentFlag.AlignRight,
        )
        painting.draw_bar(painter, QRectF(left + 2, y + 42, inner - 4, 7), percent, colour, quota.elapsed_fraction())

        if stale:
            painter.setOpacity(painter.opacity() / 0.6)
        return 66

    def _paint_extra(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState, quota: Quota) -> float:
        percent = self._tween.get(f'{state.id}:{quota.key}', quota.percent)
        self._text(painter, QRectF(left + 2, y, inner * 0.7, 18), quota.label, 12, QFont.Weight.Medium, theme.TEXT_DIM)
        painting.draw_text(
            painter, QRectF(left + inner * 0.6, y, inner * 0.4 - 2, 18), percent_text(percent),
            theme.ui_font(12.5, QFont.Weight.DemiBold), theme.TEXT if percent < 70 else theme.severity_color(percent),
            Qt.AlignmentFlag.AlignRight,
        )
        return 22

    def _paint_message(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState) -> float:
        title, hint = _ERROR_TEXT.get(state.error or '', ('Loading', 'Fetching your usage'))
        self._text(painter, QRectF(left, y + 6, inner, 22), title, 16, QFont.Weight.DemiBold, theme.TEXT, Qt.AlignmentFlag.AlignHCenter)
        self._text(painter, QRectF(left, y + 30, inner, 18), hint, 12, QFont.Weight.Normal, theme.TEXT_DIM, Qt.AlignmentFlag.AlignHCenter)
        return 70

    def _paint_footer(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState, record_hits: bool) -> float:
        row = 28.0
        self._text(painter, QRectF(left + 2, y, 60, row), 'Island', 12, QFont.Weight.Medium, theme.TEXT_DIM)
        self._paint_island_modes(painter, QRectF(left + inner - 176, y, 176, row), record_hits)
        y += row + 4

        height = 26.0
        if state.error and state.has_data:
            status, colour = f'Offline  \u00b7  {age_text(state.age_seconds())}', theme.ORANGE
        elif state.updated_at is None:
            status, colour = '', theme.TEXT_FAINT
        else:
            status, colour = f'Updated {age_text(state.age_seconds())}', theme.TEXT_FAINT
        self._text(painter, QRectF(left + 2, y, inner * 0.7, height), status, 11, QFont.Weight.Normal, colour)

        refresh = QRectF(left + inner - 26, y, 26, height)
        painting.draw_refresh_icon(painter, refresh, theme.TEXT_DIM, self._spin_angle)
        if record_hits:
            self._hits.append((refresh.adjusted(-4, -2, 4, 2), 'refresh', None))
        return row + 4 + height

    def _paint_island_modes(self, painter: QPainter, area: QRectF, record_hits: bool) -> None:
        """A three-way segmented control: Off, drop down on hover, always visible."""
        track = QRectF(area.left(), area.top() + 1, area.width(), area.height() - 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)

        width = (track.width() - 4) / len(_ISLAND_CHOICES)
        for index, (mode, label) in enumerate(_ISLAND_CHOICES):
            cell = QRectF(track.left() + 2 + index * width, track.top() + 2, width, track.height() - 4)
            selected = mode == self._island_mode
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 40))
                painter.drawRoundedRect(cell, cell.height() / 2, cell.height() / 2)

            weight = QFont.Weight.DemiBold if selected else QFont.Weight.Medium
            self._text(painter, cell, label, 11.5, weight, theme.TEXT if selected else theme.TEXT_DIM, Qt.AlignmentFlag.AlignHCenter)
            if record_hits:
                self._hits.append((cell, 'island_mode', mode))

    def _reset_text(self, quota: Quota) -> str:
        if quota.resets_at is None:
            return 'Starts with your next message'

        remaining = countdown(quota.seconds_until_reset())
        if quota.period_seconds and quota.period_seconds > 86400:
            return f'Resets in {remaining}'

        return f'Resets {reset_clock(quota.resets_at, self._use_24h)}  ·  in {remaining}'

    @staticmethod
    def _text(
        painter: QPainter, rect: QRectF, text: str, pixel_size: float, weight: QFont.Weight, colour: QColor,
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    ) -> None:
        painting.draw_text(painter, rect, text, theme.ui_font(pixel_size, weight), colour, align)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high if high >= low else low))
