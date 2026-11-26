"""
Value Tweener
=============

Eases displayed numbers toward their real value so rings and bars glide
instead of jumping when new usage arrives.  The timer only runs while some
value is still in motion, so an idle app does no work.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

__all__ = ['Tweener']

_FRAME_MS = 16
_EASE = 0.2
_SNAP_DISTANCE = 0.05


class Tweener(QObject):
    """Maps a key to a value that chases its target a little every frame."""

    moved = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current: dict[str, float] = {}
        self._target: dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_MS)
        self._timer.timeout.connect(self._step)

    def set_target(self, key: str, value: float | None) -> None:
        if value is None:
            self._target.pop(key, None)
            self._current.pop(key, None)
            return

        self._target[key] = value
        if key not in self._current:
            self._current[key] = value
        elif abs(self._current[key] - value) > _SNAP_DISTANCE and not self._timer.isActive():
            self._timer.start()

    def get(self, key: str, fallback: float | None = None) -> float | None:
        return self._current.get(key, fallback)

    def _step(self) -> None:
        moving = False
        for key, target in self._target.items():
            current = self._current.get(key, target)
            if abs(target - current) <= _SNAP_DISTANCE:
                self._current[key] = target
                continue

            self._current[key] = current + (target - current) * _EASE
            moving = True

        self.moved.emit()
        if not moving:
            self._timer.stop()
