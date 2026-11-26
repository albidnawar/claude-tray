"""
Dynamic Island
==============

A small pill floating at the top-centre of the primary screen.  Collapsed it
shows the selected account's session and weekly usage as two rings with
numbers, and the Claude mascot on the right showing whether a Claude Code task
is running or finished.  While collapsed the window is completely click-through,
so it can never block a title bar or a tab underneath it.  Rest the pointer on
it and it springs open to show every account side by side plus what each recent
Claude Code session is doing; click a panel to select that account.

Three modes: ``always`` keeps the pill on screen, ``hover`` hides it until the
pointer touches the top edge of the screen in the middle, then drops it down
(and lets it back up when the pointer leaves), ``off`` removes it.  It also
slides away while a full-screen app is active.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen

from . import painting, theme, winapi
from .activity import DONE, IDLE, MAX_SESSIONS, NEEDS_INPUT, RUNNING, STOPPED, SessionActivity, SessionMonitor
from .floating import FloatingWindow
from .formatting import clock_text, countdown, percent_text
from .mascot import draw_mascot
from .quotas import Quota
from .settings import ISLAND_MODES, ISLAND_STYLES
from .store import AccountState, UsageStore
from .tween import Tweener

__all__ = ['Island']

MARGIN_SIDE = 36
MARGIN_TOP = 8
MARGIN_BOTTOM = 42
TOP_OFFSET = 8
COLLAPSED_WIDTH = 240
MASCOT_SLOT = 40
COLLAPSED_HEIGHT = 36
PANEL_WIDTH = 224
PANEL_HEIGHT = 96
PANEL_GAP = 6
EXPANDED_PADDING = 10
ROW_HEIGHT = 46
ROW_GAP = 6
COLLAPSED_RADIUS = COLLAPSED_HEIGHT / 2
EXPANDED_RADIUS = 30.0
_DWELL_SECONDS = 0.20
_LEAVE_SECONDS = 0.40
_ZONE_DWELL_SECONDS = 0.18
_ZONE_HALF_WIDTH = 120
_RETRACT_SECONDS = 0.90
_PEEK_SECONDS = 5.0
_OPEN_MS = 560
_CLOSE_MS = 340
_MIN_MOTION_MS = 180
_FADE_MS = 220
_DROP_MS = 340
_SPRING_DECAY = 8.5
_SPRING_FREQUENCY = 8.0
_SPRING_VELOCITY = 2.5
_ANIMATION_MS = 83
_HOVER_SLIDE = 64.0
_SHOW_SLIDE = 10.0
_CORAL = QColor('#E8906F')
_NOTCH_RADIUS = 16.0
_NOTCH_RADIUS_OPEN = 30.0
_NOTCH_FLARE = 10.0
_NOTCH_FLARE_OPEN = 16.0
_SHADOW_SPREAD = 8
_SHADOW_OFFSET = 2.0
_SHADOW_STRENGTH = 0.14
_SHADOW_GROWTH = 0.06


class Island(FloatingWindow):
    """The always-on-top pill."""

    def __init__(self, store: UsageStore, tweener: Tweener, use_24h: bool = True, activity: SessionMonitor | None = None) -> None:
        super().__init__()
        self._store = store
        self._tween = tweener
        self._use_24h = use_24h
        self._activity = activity
        self._show_activity = activity is not None
        self._mode = 'off'
        self._style = 'floating'
        self._dropped = False
        self._suppressed = False
        self._expand = 0.0
        self._presence = 0.0
        self._target_open = False
        self._rows = 0
        self._peek_until = 0.0
        self._known_states: dict[str, str] = {}
        self._hover_since: float | None = None
        self._leave_since: float | None = None

        self._motion_from = 0.0
        self._motion_to = 0.0
        self._motion_curve: Callable[[float], float] = _spring
        self._motion = QVariantAnimation(self)
        self._motion.setStartValue(0.0)
        self._motion.setEndValue(1.0)
        self._motion.setEasingCurve(QEasingCurve.Type.Linear)
        self._motion.valueChanged.connect(self._on_motion)
        self._motion.finished.connect(self._on_motion_finished)

        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(self._on_fade_finished)

        self._clock = QTimer(self)
        self._clock.setInterval(_ANIMATION_MS)
        self._clock.timeout.connect(self._animate)

        store.changed.connect(self._on_data_changed)
        tweener.moved.connect(self.update)
        if activity is not None:
            activity.changed.connect(self._on_activity_changed)

    # -- public API ---------------------------------------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def enabled(self) -> bool:
        return self._mode != 'off'

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ISLAND_MODES else 'off'
        self._dropped = False
        self._peek_until = 0.0
        self._hover_since = None
        self._leave_since = None
        self._sync_visibility()

    @property
    def style(self) -> str:
        return self._style

    def set_style(self, style: str) -> None:
        """``'attached'`` grows out of the top edge of the screen with curved ears; ``'floating'`` leaves a gap above the pill."""
        self._style = style if style in ISLAND_STYLES else 'floating'
        self._apply_geometry()
        self.update()

    def is_busy(self) -> bool:
        """True while the pill is open, opening, closing or fading.

        The mascot's animation does not count: it repaints a tiny rectangle and can run for hours.
        """
        return self._expand > 0.0 or self._motion.state() == QVariantAnimation.State.Running or self._fade.state() == QVariantAnimation.State.Running

    def set_enabled(self, enabled: bool) -> None:
        self.set_mode('always' if enabled else 'off')

    def set_show_activity(self, show: bool) -> None:
        self._show_activity = show and self._activity is not None
        self._refresh_rows()
        self._sync_visibility()
        self.update()

    def set_suppressed(self, suppressed: bool) -> None:
        if suppressed != self._suppressed:
            self._suppressed = suppressed
            self._sync_visibility()

    def tick(self, pos: QPoint) -> None:
        """Called ~20 times a second with the pointer position."""
        if not self._is_live():
            return

        now = time.monotonic()
        hover_mode = self._mode == 'hover'

        if hover_mode and not self._dropped:
            if self._hot_zone().contains(pos):
                self._hover_since = self._hover_since or now
                if now - self._hover_since >= _ZONE_DWELL_SECONDS:
                    self._drop()
            else:
                self._hover_since = None
            return

        if not self._target_open:
            if self._collapsed_target().contains(pos):
                self._leave_since = None
                self._hover_since = self._hover_since or now
                if now - self._hover_since >= _DWELL_SECONDS:
                    self._open()
                return

            self._hover_since = None
            if hover_mode:
                self._maybe_retract(pos, now)
            return

        if self._pill_rect(expanded=True).adjusted(-10, -10, 10, 14).contains(pos):
            self._leave_since = None
        else:
            self._leave_since = self._leave_since or now
            if now - self._leave_since >= _LEAVE_SECONDS:
                self._close()

    # -- state --------------------------------------------------------------------------------------------------------

    def _is_live(self) -> bool:
        return self._mode != 'off' and not self._suppressed and bool(self._store.accounts)

    def _wants_visible(self) -> bool:
        return self._is_live() and (self._mode == 'always' or self._dropped)

    def _sync_visibility(self) -> None:
        wanted = self._wants_visible()
        if wanted:
            self._apply_geometry()
            if not self.isVisible():
                self._presence = 0.0
                self._refresh_rows()
                self.show()
                winapi.set_click_through(self.native_id, True)
            self._run_fade(1.0)
        elif self.isVisible():
            self._target_open = False
            self._motion.stop()
            self._expand = 0.0
            winapi.set_click_through(self.native_id, True)
            self._run_fade(0.0)

        self._store.watching = wanted
        if self._activity is not None:
            self._activity.set_active(wanted and self._show_activity)

        self._sync_clock()

    def _sync_clock(self) -> None:
        """Run the mascot's animation clock only while the pill is on screen and something is running."""
        animated = self.isVisible() and self._show_activity and any(session.state == RUNNING for session in self._sessions())
        if animated and not self._clock.isActive():
            self._clock.start()
        elif not animated and self._clock.isActive():
            self._clock.stop()
            self.update()

    def _animate(self) -> None:
        """Advance the mascot; while the pill is collapsed only its small rectangle needs repainting."""
        if self._expand > 0.001 or self._target_open:
            self.update()
        else:
            self.update(self._mascot_area())

    def _slide_distance(self) -> float:
        return _HOVER_SLIDE if self._mode == 'hover' else _SHOW_SLIDE

    def _mascot_area(self) -> QRect:
        """Where the collapsed pill's mascot (with its badge and bounce) is, in window coordinates."""
        width = self._collapsed_width()
        left = (self.width() - width) / 2
        box = QRectF(left + width - 14 - 31, self._top_margin() + COLLAPSED_HEIGHT / 2 - 12, 31, 24).adjusted(-8, -10, 8, 8)
        return box.translated(0, -(1.0 - self._presence) * self._slide_distance()).toAlignedRect()

    def _hot_zone(self) -> QRect:
        """The strip of screen edge, centred at the top, that makes a hover-mode island drop down."""
        screen = self._screen_geometry()
        return QRect(screen.center().x() - _ZONE_HALF_WIDTH, screen.top(), _ZONE_HALF_WIDTH * 2, 3)

    def _drop(self) -> None:
        self._dropped = True
        self._hover_since = None
        self._leave_since = None
        self._sync_visibility()

    def _retract(self) -> None:
        self._dropped = False
        self._hover_since = None
        self._leave_since = None
        self._sync_visibility()

    def _maybe_retract(self, pos: QPoint, now: float) -> None:
        if now < self._peek_until or self._hot_zone().contains(pos):
            self._leave_since = None
            return

        self._leave_since = self._leave_since or now
        if now - self._leave_since >= _RETRACT_SECONDS:
            self._retract()

    def _open(self) -> None:
        self._target_open = True
        self._leave_since = None
        self._peek_until = 0.0
        self._refresh_rows()
        self._store.ensure_fresh()
        winapi.set_click_through(self.native_id, False)
        self._run_motion(1.0, _OPEN_MS, _spring)

    def _close(self) -> None:
        self._target_open = False
        self._hover_since = None
        self._run_motion(0.0, _CLOSE_MS, _ease_out)

    def _run_motion(self, target: float, full_duration: int, curve: Callable[[float], float]) -> None:
        """Animate from wherever the pill is now; a reversal part-way takes proportionally less time."""
        self._motion.stop()
        distance = abs(target - self._expand)
        self._motion_from = self._expand
        self._motion_to = target
        self._motion_curve = curve
        if distance < 0.002:
            self._finish_motion()
            return

        self._motion.setDuration(max(_MIN_MOTION_MS, round(full_duration * min(1.0, distance))))
        self._motion.start()

    def _run_fade(self, target: float) -> None:
        self._fade.stop()
        dropping = self._mode == 'hover'
        self._fade.setDuration(_DROP_MS if dropping else _FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic if target > self._presence else QEasingCurve.Type.InCubic)
        self._fade.setStartValue(self._presence)
        self._fade.setEndValue(target)
        self._fade.start()

    def _on_motion(self, value: object) -> None:
        progress = float(value)  # type: ignore[arg-type]
        self._expand = self._motion_from + (self._motion_to - self._motion_from) * self._motion_curve(progress)
        self.update()

    def _on_motion_finished(self) -> None:
        self._finish_motion()

    def _finish_motion(self) -> None:
        self._expand = self._motion_to
        if self._motion_to == 0.0:
            winapi.set_click_through(self.native_id, True)
            self._refresh_rows()

        self.update()

    def _on_fade(self, value: object) -> None:
        self._presence = float(value)  # type: ignore[arg-type]
        self.update()

    def _on_fade_finished(self) -> None:
        if self._fade.endValue() == 0.0:
            self.hide()
            self._sync_clock()

    def _on_data_changed(self) -> None:
        if self._wants_visible():
            self._apply_geometry()
            self.update()

    def _on_activity_changed(self) -> None:
        self._notice_transitions()
        if not self._target_open and self._expand == 0.0:
            self._refresh_rows()

        self._sync_clock()
        self.update()

    def _notice_transitions(self) -> None:
        """In hover mode, let the island drop down briefly when a task finishes or needs you."""
        peek = False
        current: dict[str, str] = {}
        for session in self._sessions():
            key = f'{session.account_id}:{session.session_id}'
            current[key] = session.state
            before = self._known_states.get(key)
            if session.state == NEEDS_INPUT and before != NEEDS_INPUT:
                peek = True
            elif session.state == DONE and before == RUNNING:
                peek = True

        self._known_states = current
        if peek and self._mode == 'hover' and self._is_live():
            self._peek_until = time.monotonic() + _PEEK_SECONDS
            if not self._dropped:
                self._drop()

    # -- activity -----------------------------------------------------------------------------------------------------

    def _sessions(self) -> list[SessionActivity]:
        return self._activity.sessions if self._activity is not None and self._show_activity else []

    def _refresh_rows(self) -> None:
        """The number of activity rows is fixed while the island is open so the pill does not change height under the pointer."""
        self._rows = min(MAX_SESSIONS, len(self._sessions()))

    def _primary_state(self) -> str:
        sessions = self._sessions()
        return sessions[0].state if sessions else IDLE

    # -- geometry -----------------------------------------------------------------------------------------------------

    def _collapsed_width(self) -> int:
        return COLLAPSED_WIDTH + (MASCOT_SLOT if self._show_activity else 0)

    def _expanded_size(self, rows: int | None = None) -> tuple[int, int]:
        count = max(1, len(self._store.accounts))
        rows = self._rows if rows is None else rows
        width = count * PANEL_WIDTH + (count - 1) * PANEL_GAP + EXPANDED_PADDING * 2
        height = PANEL_HEIGHT + EXPANDED_PADDING * 2
        if rows > 0:
            height += ROW_GAP + rows * ROW_HEIGHT + (rows - 1) * ROW_GAP
        return width, height

    def _top_margin(self) -> int:
        """Window space above the pill: none when the pill is attached to the top edge."""
        return 0 if self._style == 'attached' else MARGIN_TOP

    def _collapsed_target(self) -> QRect:
        """Where resting the pointer opens the collapsed pill.

        Attached, the top rows of the pill belong to the edge zone that drops a hidden island down, so the pointer
        has to move into the pill, not just touch the edge, before the pill opens.
        """
        pill = self._pill_rect(expanded=False)
        return pill.adjusted(-4, 4, 4, 4) if self._style == 'attached' else pill.adjusted(-4, -4, 4, 4)

    def _screen_geometry(self) -> QRect:
        screen = QGuiApplication.primaryScreen()
        return screen.geometry() if screen is not None else QRect(0, 0, 1920, 1080)

    def _pill_rect(self, expanded: bool, rows: int | None = None) -> QRect:
        """Global rectangle of the pill in its collapsed or fully expanded form."""
        screen = self._screen_geometry()
        width, height = self._expanded_size(rows) if expanded else (self._collapsed_width(), COLLAPSED_HEIGHT)
        return QRect(screen.center().x() - width // 2 + 1, screen.top() + (0 if self._style == 'attached' else TOP_OFFSET), width, height)

    def _apply_geometry(self) -> None:
        """Fit the window around the largest the pill can become and never resize it for the animation, which would flash.

        While collapsed the whole window is click-through, so the extra transparent area blocks nothing.
        """
        rows = MAX_SESSIONS if self._show_activity else 0
        pill = self._pill_rect(expanded=True, rows=rows)
        geometry = QRect(
            pill.left() - MARGIN_SIDE, pill.top() - self._top_margin(),
            pill.width() + MARGIN_SIDE * 2, pill.height() + self._top_margin() + MARGIN_BOTTOM,
        )
        if self.geometry() != geometry:
            self.setGeometry(geometry)

    # -- painting -----------------------------------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if not self._store.accounts:
            return

        seconds = time.monotonic()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.setOpacity(self._presence)
        painter.translate(0, -(1.0 - self._presence) * self._slide_distance())

        progress = self._expand
        expanded_width, expanded_height = self._expanded_size()
        collapsed_width = self._collapsed_width()
        width = _lerp(collapsed_width, expanded_width, progress)
        height = _lerp(COLLAPSED_HEIGHT, expanded_height, progress)
        pill = QRectF((self.width() - width) / 2, self._top_margin(), width, height)
        opened = min(1.0, progress)

        self._hits = []
        if self._style == 'attached':
            radius = _lerp(_NOTCH_RADIUS, _NOTCH_RADIUS_OPEN, opened)
            flare = _lerp(_NOTCH_FLARE, _NOTCH_FLARE_OPEN, opened)
            shape = notch_path(pill, radius, flare)
            outline = notch_path(pill, radius, flare, outline_only=True)
        else:
            radius = _lerp(COLLAPSED_RADIUS, EXPANDED_RADIUS, opened)
            painting.soft_shadow(painter, pill, radius, _SHADOW_SPREAD, _SHADOW_OFFSET, _SHADOW_STRENGTH + _SHADOW_GROWTH * progress)
            shape = QPainterPath()
            shape.addRoundedRect(pill, radius, radius)
            outline = QPainterPath()
            outline.addRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 252))
        painter.drawPath(shape)

        accent = self._alert_colour()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(accent if accent is not None else QColor(255, 255, 255, 28))
        painter.drawPath(outline)

        painter.setClipPath(shape)

        settled = min(1.0, max(0.0, progress))
        leaving = min(1.0, settled * 2.6)
        if leaving < 1.0:
            # Anchored to the pill's centre, so the content stays put while the pill grows around it.
            home = QRectF(pill.center().x() - collapsed_width / 2, pill.top(), collapsed_width, COLLAPSED_HEIGHT)
            painter.save()
            painter.setOpacity(painter.opacity() * (1.0 - leaving))
            painter.translate(home.center())
            shrink = 1.0 - 0.08 * leaving
            painter.scale(shrink, shrink)
            painter.translate(-home.center())
            self._paint_collapsed(painter, home, seconds)
            painter.restore()

        arriving = _smoothstep((settled - 0.36) / 0.64)
        if arriving > 0.0:
            full = QRectF(pill.center().x() - expanded_width / 2, pill.top(), expanded_width, expanded_height)
            grow = 0.86 + 0.14 * settled
            painter.save()
            painter.setOpacity(painter.opacity() * arriving)
            painter.translate(full.center())
            painter.scale(grow, grow)
            painter.translate(-full.center())
            self._paint_expanded(painter, full, seconds, record_hits=settled > 0.95)
            painter.restore()

    def _alert_colour(self) -> QColor | None:
        """A coloured rim so trouble is noticeable even while collapsed: red for nearly-empty quota, amber when Claude needs you."""
        worst = max((quota.percent for state in self._store.accounts for quota in (state.session, state.weekly) if quota is not None), default=0.0)
        if worst >= 90:
            colour = QColor(theme.RED)
        elif any(session.state == NEEDS_INPUT for session in self._sessions()):
            colour = QColor(theme.ORANGE)
        else:
            return None

        colour.setAlpha(150)
        return colour

    def _paint_collapsed(self, painter: QPainter, pill: QRectF, seconds: float) -> None:
        state = self._store.active
        if state is None:
            return

        index = self._store.accounts.index(state)
        mid = pill.center().y()
        x = pill.left() + 15

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.account_color(index))
        painter.drawEllipse(QPointF(x + 4, mid), 4, 4)
        x += 8 + 12

        number_font = theme.ui_font(14, QFont.Weight.DemiBold)
        label_font = theme.ui_font(10, QFont.Weight.Medium)
        number_width = painting.text_width('100%', number_font)
        for quota, caption in self._reset_captions(state):
            label_width = painting.text_width(caption, label_font)
            percent = None if quota is None else self._tween.get(f'{state.id}:{quota.key}', quota.percent)
            colour = theme.severity_color(percent)
            painting.draw_ring(painter, QPointF(x + 8, mid), 6.5, 3.0, percent, colour)

            number_left = x + 16 + 7
            painting.draw_text(
                painter, QRectF(number_left, pill.top(), number_width, pill.height()), percent_text(percent), number_font,
                theme.TEXT if percent is None or percent < 70 else colour,
            )
            caption_left = number_left + painting.text_width(percent_text(percent), number_font) + 4
            painting.draw_text(painter, QRectF(caption_left, pill.top() + 1.5, label_width + 2, pill.height()), caption, label_font, theme.TEXT_FAINT)
            x = number_left + number_width + 4 + label_width + 12

        if self._show_activity:
            mascot = QRectF(pill.right() - 14 - 31, mid - 12, 31, 24)
            draw_mascot(painter, mascot, self._primary_state(), seconds)

    def _paint_expanded(self, painter: QPainter, area: QRectF, seconds: float, record_hits: bool) -> None:
        for index, state in enumerate(self._store.accounts):
            panel = QRectF(
                area.left() + EXPANDED_PADDING + index * (PANEL_WIDTH + PANEL_GAP), area.top() + EXPANDED_PADDING,
                PANEL_WIDTH, PANEL_HEIGHT,
            )
            self._paint_panel(painter, panel, state, index, state.id == self._store.active_id)
            if record_hits:
                self._hits.append((panel, 'account', state.id))

        y = area.top() + EXPANDED_PADDING + PANEL_HEIGHT + ROW_GAP
        for session in self._sessions()[: self._rows]:
            row = QRectF(area.left() + EXPANDED_PADDING, y, area.width() - EXPANDED_PADDING * 2, ROW_HEIGHT)
            self._paint_session_row(painter, row, session, seconds)
            y += ROW_HEIGHT + ROW_GAP

    def _paint_session_row(self, painter: QPainter, row: QRectF, session: SessionActivity, seconds: float) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 15))
        painter.drawRoundedRect(row, 18, 18)

        draw_mascot(painter, QRectF(row.left() + 14, row.center().y() - 14, 40, 28), session.state, seconds)

        right_edge = row.right() - 16
        if session.state == RUNNING:
            self._paint_spinner(painter, QPointF(right_edge - 6, row.center().y()), seconds)
            right_edge -= 26

        project_font = theme.ui_font(11, QFont.Weight.Medium)
        project = QFontMetricsF(project_font).elidedText(session.project, Qt.TextElideMode.ElideRight, 120)
        project_width = painting.text_width(project, project_font)
        account_index = next((i for i, state in enumerate(self._store.accounts) if state.id == session.account_id), 0)
        painter.setBrush(theme.account_color(account_index))
        painter.drawEllipse(QPointF(right_edge - project_width - 10, row.center().y()), 3.5, 3.5)
        painting.draw_text(painter, QRectF(right_edge - project_width - 2, row.top(), project_width + 4, row.height()), project, project_font, theme.TEXT_DIM, Qt.AlignmentFlag.AlignRight)

        text_left = row.left() + 68
        text_width = max(40.0, right_edge - project_width - 26 - text_left)
        title_font = theme.ui_font(13, QFont.Weight.DemiBold)
        title = QFontMetricsF(title_font).elidedText(session.title, Qt.TextElideMode.ElideRight, text_width)
        painting.draw_text(painter, QRectF(text_left, row.top() + 6, text_width, 18), title, title_font, theme.TEXT)

        detail_font = theme.ui_font(11.5, QFont.Weight.Normal)
        text, colour = self._session_status(session)
        detail = QFontMetricsF(detail_font).elidedText(text, Qt.TextElideMode.ElideRight, text_width)
        painting.draw_text(painter, QRectF(text_left, row.top() + 24, text_width, 16), detail, detail_font, colour)
        painter.restore()

    @staticmethod
    def _session_status(session: SessionActivity) -> tuple[str, QColor]:
        """The status line of a session row and its colour."""
        age = session.age_seconds()
        ago = f'{countdown(age)} ago' if age is not None else ''
        if session.state == RUNNING:
            progress = f'  ·  {session.progress[0]}/{session.progress[1]}' if session.progress else ''
            return f'{session.detail}{progress}', _CORAL
        if session.state == NEEDS_INPUT:
            return session.detail, theme.ORANGE
        if session.state == DONE:
            return f'Finished  ·  {ago}', theme.GREEN
        if session.state == STOPPED:
            return f'Stopped  ·  {ago}', theme.TEXT_DIM
        return (f'Idle  ·  {ago}' if ago else 'No activity'), theme.TEXT_FAINT

    @staticmethod
    def _paint_spinner(painter: QPainter, centre: QPointF, seconds: float) -> None:
        radius = 6.0
        pen = QPen(_CORAL, 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(theme.TRACK, 2.2))
        painter.drawEllipse(centre, radius, radius)
        painter.setPen(pen)
        box = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)
        painter.drawArc(box, -round((seconds * 280.0 % 360.0) * 16), -95 * 16)
        painter.restore()

    def _paint_panel(self, painter: QPainter, panel: QRectF, state: AccountState, index: int, selected: bool) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QColor(255, 255, 255, 22))
            painter.drawRoundedRect(panel, 20, 20)
            outline = theme.account_color(index)
            outline.setAlpha(120)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(outline)
            painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), 20, 20)
        else:
            painter.setOpacity(painter.opacity() * 0.72)

        left = panel.left() + 14
        inner = panel.width() - 28
        y = panel.top() + 12

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.account_color(index))
        painter.drawEllipse(QPointF(left + 4, y + 9), 4, 4)
        painting.draw_text(painter, QRectF(left + 14, y, inner - 14, 18), state.label, theme.ui_font(13, QFont.Weight.DemiBold), theme.TEXT)
        y += 26

        if not state.has_data:
            message = {'no_token': 'Not signed in', 'auth': 'Sign-in expired'}.get(state.error or '', "Can't reach Claude" if state.error else 'Loading')
            painting.draw_text(painter, QRectF(left, y, inner, 40), message, theme.ui_font(12, QFont.Weight.Medium), theme.TEXT_DIM)
            painter.restore()
            return

        if state.is_stale():
            painter.setOpacity(painter.opacity() * 0.6)

        for quota, caption in self._reset_captions(state):
            self._paint_compact_row(painter, left, y, inner, state, quota, caption)
            y += 22
        painter.restore()

    def _reset_captions(self, state: AccountState) -> list[tuple[Quota | None, str]]:
        """The text beside each ring or bar: the session's reset time of day and the days until the week resets."""
        session, weekly = state.session, state.weekly
        session_text = clock_text(session.resets_at, self._use_24h) if session is not None else '--'
        weekly_text = countdown(weekly.seconds_until_reset()) if weekly is not None else '--'
        return [(session, session_text), (weekly, weekly_text)]

    def _paint_compact_row(self, painter: QPainter, left: float, y: float, inner: float, state: AccountState, quota: Quota | None, caption: str) -> None:
        percent = None if quota is None else self._tween.get(f'{state.id}:{quota.key}', quota.percent)
        colour = theme.severity_color(percent)
        painting.draw_text(painter, QRectF(left, y, 52, 18), caption, theme.ui_font(10.5, QFont.Weight.Medium), theme.TEXT_FAINT)
        painting.draw_bar(painter, QRectF(left + 58, y + 6, inner - 58 - 46, 6), percent, colour, None if quota is None else quota.elapsed_fraction())
        painting.draw_text(
            painter, QRectF(left + inner - 42, y - 1, 42, 20), percent_text(percent), theme.ui_font(13.5, QFont.Weight.DemiBold),
            theme.TEXT if percent is None or percent < 70 else colour, Qt.AlignmentFlag.AlignRight,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            return

        hit = self.hit_at(event.position().x(), event.position().y())
        if hit is not None and hit[0] == 'account':
            self._store.select(str(hit[1]))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        hit = self.hit_at(event.position().x(), event.position().y())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)


def notch_path(rect: QRectF, radius: float, flare: float, outline_only: bool = False) -> QPainterPath:
    """A shape that hangs from the top edge of the screen.

    Flat on top, rounded at the bottom corners, with a concave "ear" at each top corner that sweeps outward until it
    meets the screen edge, so the shape looks grown out of the edge rather than stuck onto it.  With ``outline_only``
    the flat top edge is left out, for stroking just the visible rim.
    """
    left, right, top, bottom = rect.left(), rect.right(), rect.top(), rect.bottom()
    radius = min(radius, rect.height() / 2, rect.width() / 2)
    path = QPainterPath()
    path.moveTo(left - flare, top)
    path.arcTo(QRectF(left - 2 * flare, top, 2 * flare, 2 * flare), 90, -90)
    path.lineTo(left, bottom - radius)
    path.arcTo(QRectF(left, bottom - 2 * radius, 2 * radius, 2 * radius), 180, 90)
    path.lineTo(right - radius, bottom)
    path.arcTo(QRectF(right - 2 * radius, bottom - 2 * radius, 2 * radius, 2 * radius), 270, 90)
    path.lineTo(right, top + flare)
    path.arcTo(QRectF(right, top, 2 * flare, 2 * flare), 180, -90)
    if not outline_only:
        path.closeSubpath()

    return path


def _lerp(start: float, end: float, amount: float) -> float:
    return start + (end - start) * amount


def _spring(fraction: float) -> float:
    """Damped spring from 0 to 1 with a little initial speed: it leaves quickly, overshoots a few percent, then settles."""
    if fraction >= 1.0:
        return 1.0

    coupling = (_SPRING_DECAY - _SPRING_VELOCITY) / _SPRING_FREQUENCY
    decay = math.exp(-_SPRING_DECAY * fraction)
    return 1.0 - decay * (math.cos(_SPRING_FREQUENCY * fraction) + coupling * math.sin(_SPRING_FREQUENCY * fraction))


def _ease_out(fraction: float) -> float:
    return 1.0 - (1.0 - min(1.0, fraction)) ** 3


def _smoothstep(amount: float) -> float:
    clamped = min(1.0, max(0.0, amount))
    return clamped * clamped * (3.0 - 2.0 * clamped)
