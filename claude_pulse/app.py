"""
Application Controller
======================

Owns the tray icon, the hover card and the island, and drives them from one
cheap 50 ms pointer poll.  Qt gives a tray icon no hover events, so the poll
compares the cursor with the icon's on-screen rectangle instead.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QLocale, QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCursor, QGuiApplication
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import winapi
from .activity import SessionMonitor
from .card import HoverCard
from .formatting import percent_text
from .island import Island
from .settings import Settings
from .store import AccountState, UsageStore
from .tray_icon import build_icon
from .tween import Tweener

__all__ = ['Controller']

_POINTER_POLL_MS = 50
_HOUSEKEEPING_MS = 1500
_TRIM_AFTER_IDLE_SECONDS = 20.0


class Controller(QObject):
    """Wires the store to the tray, the card and the island."""

    def __init__(self, store: UsageStore, settings: Settings, launcher: Path, activity: SessionMonitor | None = None) -> None:
        super().__init__()
        self._store = store
        self._settings = settings
        self._launcher = launcher
        self._tween = Tweener(self)
        self._light_taskbar = winapi.taskbar_is_light()
        self._icon_key: tuple | None = None
        self._idle_since: float | None = None
        self._trimmed = False

        use_24h = 'AP' not in QLocale.system().timeFormat(QLocale.FormatType.ShortFormat).upper()
        self.card = HoverCard(store, self._tween, use_24h)
        self.island = Island(store, self._tween, use_24h, activity)
        self.tray = QSystemTrayIcon(self)

        self._build_menu()
        self.tray.activated.connect(self._on_tray_activated)
        self.card.island_mode_selected.connect(self.set_island_mode)
        self.card.refresh_requested.connect(store.refresh_now)

        store.changed.connect(self._on_store_changed)

        self._pointer_timer = QTimer(self)
        self._pointer_timer.setInterval(_POINTER_POLL_MS)
        self._pointer_timer.timeout.connect(self._poll_pointer)

        self._housekeeping_timer = QTimer(self)
        self._housekeeping_timer.setInterval(_HOUSEKEEPING_MS)
        self._housekeeping_timer.timeout.connect(self._housekeeping)

    def start(self) -> None:
        self._sync_tweens()
        self._refresh_icon()
        self.tray.show()
        self.island.set_style(self._settings.island_style)
        self.island.set_show_activity(self._settings.show_activity)
        self.set_island_mode(self._settings.island_mode, persist=False)
        self._pointer_timer.start()
        self._housekeeping_timer.start()

    def set_island_mode(self, mode: str, persist: bool = True) -> None:
        """Set the island to ``'off'``, ``'hover'`` (drops down from the top edge) or ``'always'``."""
        if persist:
            self._settings.island_mode = mode

        self.island.set_mode(mode)
        self.card.set_island_mode(mode)
        action = self._island_actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def set_island_style(self, style: str, persist: bool = True) -> None:
        """``'attached'`` merges the island into the top edge of the screen; ``'floating'`` leaves a gap above it."""
        if persist:
            self._settings.island_style = style

        self.island.set_style(style)
        attached = style == 'attached'
        if self._attached_action.isChecked() != attached:
            self._attached_action.setChecked(attached)

    def set_show_activity(self, show: bool, persist: bool = True) -> None:
        if persist:
            self._settings.show_activity = show

        self.island.set_show_activity(show)
        if self._activity_action.isChecked() != show:
            self._activity_action.setChecked(show)

    # -- tray ---------------------------------------------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = QMenu()
        menu.aboutToShow.connect(self.card.hide_for_menu)
        self._account_group = QActionGroup(menu)
        self._account_group.setExclusive(True)
        self._account_actions: dict[str, QAction] = {}
        for state in self._store.accounts:
            action = menu.addAction(state.label)
            action.setCheckable(True)
            action.setChecked(state.id == self._store.active_id)
            action.triggered.connect(lambda _checked=False, account_id=state.id: self._store.select(account_id))
            self._account_group.addAction(action)
            self._account_actions[state.id] = action

        menu.addSeparator()
        island_menu = menu.addMenu('Dynamic Island')
        island_group = QActionGroup(island_menu)
        island_group.setExclusive(True)
        self._island_actions: dict[str, QAction] = {}
        for mode, label in (('always', 'Always visible'), ('hover', 'Drop down from the top edge'), ('off', 'Off')):
            island_action = island_menu.addAction(label)
            island_action.setCheckable(True)
            island_action.triggered.connect(lambda _checked=False, chosen=mode: self.set_island_mode(chosen))
            island_group.addAction(island_action)
            self._island_actions[mode] = island_action

        island_menu.addSeparator()
        self._attached_action = island_menu.addAction('Attached to the top edge')
        self._attached_action.setCheckable(True)
        self._attached_action.setChecked(self._settings.island_style == 'attached')
        self._attached_action.triggered.connect(lambda checked: self.set_island_style('attached' if checked else 'floating'))

        self._activity_action = menu.addAction('Show Claude activity')
        self._activity_action.setCheckable(True)
        self._activity_action.setChecked(self._settings.show_activity)
        self._activity_action.triggered.connect(self.set_show_activity)

        menu.addAction('Refresh now').triggered.connect(self._store.refresh_now)

        autostart = menu.addAction('Start with Windows')
        autostart.setCheckable(True)
        autostart.setChecked(winapi.autostart_enabled())
        autostart.toggled.connect(lambda checked: winapi.set_autostart(checked, self._launcher))
        autostart.setEnabled(winapi.IS_WINDOWS)

        menu.addSeparator()
        menu.addAction('Quit').triggered.connect(QGuiApplication.quit)
        self._menu = menu
        self.tray.setContextMenu(menu)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.card.toggle_pinned(self._icon_rect(), QCursor.pos())
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self._store.cycle()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            self.card.hide_for_menu()

    def _icon_rect(self) -> QRect:
        rect = self.tray.geometry()
        return rect if rect.isValid() and rect.width() > 0 else QRect()

    # -- polling ------------------------------------------------------------------------------------------------------

    def _poll_pointer(self) -> None:
        pos = QCursor.pos()
        icon_rect = QRect()
        if self.card.isVisible() or self._near_taskbar(pos):
            icon_rect = self._icon_rect()

        self.card.tick(pos, icon_rect, winapi.left_button_down())
        self.island.tick(pos)

    @staticmethod
    def _near_taskbar(pos: QPoint) -> bool:
        screen = QGuiApplication.screenAt(pos)
        return screen is not None and not screen.availableGeometry().contains(pos)

    def _housekeeping(self) -> None:
        self.island.set_suppressed(winapi.fullscreen_app_active())
        if '--debug' in sys.argv:
            screen = QGuiApplication.primaryScreen()
            print('tray', self.tray.geometry(), 'screen', screen.geometry(), 'work', screen.availableGeometry(), 'dpr', screen.devicePixelRatio(), flush=True)

        self._trim_when_idle()

        light = winapi.taskbar_is_light()
        if light != self._light_taskbar:
            self._light_taskbar = light
            self._refresh_icon()

    def _trim_when_idle(self) -> None:
        """After 20 quiet seconds hand resident-but-unused memory back to Windows, once per quiet spell."""
        busy = self.card.is_busy() or self.island.is_busy() or self._store.refreshing
        if busy:
            self._idle_since = None
            self._trimmed = False
            return

        now = time.monotonic()
        self._idle_since = self._idle_since if self._idle_since is not None else now
        if not self._trimmed and now - self._idle_since >= _TRIM_AFTER_IDLE_SECONDS:
            winapi.trim_working_set()
            self._trimmed = True

    # -- data ---------------------------------------------------------------------------------------------------------

    def _on_store_changed(self) -> None:
        self._sync_tweens()
        self._refresh_icon()
        for state in self._store.accounts:
            action = self._account_actions.get(state.id)
            if action is not None:
                action.setText(state.label)
                action.setChecked(state.id == self._store.active_id)

    def _sync_tweens(self) -> None:
        for state in self._store.accounts:
            for quota in state.quotas:
                self._tween.set_target(f'{state.id}:{quota.key}', quota.percent)

    def _refresh_icon(self) -> None:
        state = self._store.active
        session = state.session.percent if state is not None and state.session else None
        weekly = state.weekly.percent if state is not None and state.weekly else None
        index = self._store.accounts.index(state) if state is not None else 0
        key = (round(session) if session is not None else None, round(weekly) if weekly is not None else None, index, self._light_taskbar)
        if key == self._icon_key:
            return

        self._icon_key = key
        self.tray.setIcon(build_icon(session, weekly, index, len(self._store.accounts), self._light_taskbar))
        self.tray.setToolTip('' if not self._icon_rect().isEmpty() else self._fallback_tooltip(state))

    @staticmethod
    def _fallback_tooltip(state: AccountState | None) -> str:
        if state is None:
            return 'Claude Pulse'

        session = percent_text(state.session.percent) if state.session else '--'
        weekly = percent_text(state.weekly.percent) if state.weekly else '--'
        return f'{state.label}: session {session}, weekly {weekly}'
