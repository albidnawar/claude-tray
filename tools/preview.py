"""
Preview Renderer
================

Renders the tray icon, hover card and island (collapsed, mid-animation and
expanded) to PNG files using demo data, composited on a wallpaper-like
background.  Used to check the visuals without touching the real tray.

    python tools/preview.py <output_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_pulse import tray_icon  # noqa: E402
from claude_pulse.activity import DONE, IDLE, NEEDS_INPUT, RUNNING, STOPPED, SessionMonitor  # noqa: E402
from claude_pulse.mascot import draw_mascot  # noqa: E402
from claude_pulse.card import HoverCard  # noqa: E402
from claude_pulse.demo import apply_demo_data, demo_sessions  # noqa: E402
from claude_pulse.island import Island  # noqa: E402
from claude_pulse.settings import AccountConfig, Settings  # noqa: E402
from claude_pulse.store import UsageStore  # noqa: E402
from claude_pulse.tween import Tweener  # noqa: E402


def wallpaper(width: int, height: int, light: bool = False) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, width, height)
    if light:
        gradient.setColorAt(0, QColor('#dfe6f2'))
        gradient.setColorAt(1, QColor('#b9c7e0'))
    else:
        gradient.setColorAt(0, QColor('#3a4a6b'))
        gradient.setColorAt(1, QColor('#1c2236'))
    painter.fillRect(image.rect(), gradient)
    painter.end()
    return image


def compose(window_pixmap: QPixmap, canvas: QImage, position: QPoint) -> None:
    painter = QPainter(canvas)
    painter.drawPixmap(position, window_pixmap)
    painter.end()


def make_store(tmp: Path) -> UsageStore:
    store = UsageStore([AccountConfig('team', Path('a')), AccountConfig('pro', Path('b'))], Settings(tmp / 'settings.json'))
    apply_demo_data(store)
    return store


def render_icons(out: Path) -> None:
    cases = [(42, 18, 0), (9, 34, 1), (78, 91, 0), (100, 100, 1), (None, None, 0)]
    cell = 205
    sheet = QImage(len(cases) * cell + 20, 2 * 230, QImage.Format.Format_ARGB32)
    painter = QPainter(sheet)
    for row, light in enumerate((False, True)):
        top = row * 230
        painter.fillRect(0, top, sheet.width(), 230, QColor('#f3f3f3') if light else QColor('#1f1f1f'))
        for column, (session, weekly, account) in enumerate(cases):
            left = 20 + column * cell
            large = tray_icon.render_icon(64, session, weekly, account, 2, light)
            painter.drawPixmap(left, top + 10, large.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            for index, size in enumerate((16, 24, 32)):
                pixmap = tray_icon.render_icon(size, session, weekly, account, 2, light)
                painter.drawPixmap(left + index * 40, top + 160, pixmap)
            small = tray_icon.render_icon(16, session, weekly, account, 2, light)
            painter.drawPixmap(left + 135, top + 150, small.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
    painter.end()
    sheet.save(str(out / 'icons.png'))


def render_card(out: Path, store: UsageStore) -> None:
    card = HoverCard(store, Tweener(), use_24h=True)
    card._reveal = 1.0
    card._remeasure()
    pixmap = card.grab()
    canvas = wallpaper(pixmap.width() + 40, pixmap.height() + 40)
    compose(pixmap, canvas, QPoint(20, 20))
    canvas.save(str(out / 'card.png'))

    store.select('pro')
    card._remeasure()
    pixmap = card.grab()
    canvas = wallpaper(pixmap.width() + 40, pixmap.height() + 40)
    compose(pixmap, canvas, QPoint(20, 20))
    canvas.save(str(out / 'card_pro.png'))
    store.select('team')


def make_island(store: UsageStore, states: tuple[str, ...] = (RUNNING, DONE, IDLE), style: str = 'floating') -> Island:
    monitor = SessionMonitor([])
    monitor.set_fixed(demo_sessions([state.id for state in store.accounts], states))
    island = Island(store, Tweener(), True, monitor)
    island.set_show_activity(True)
    island.set_style(style)
    island._presence = 1.0
    return island


def render_island(out: Path, store: UsageStore) -> None:
    island = make_island(store)
    frames = []
    for progress in (0.0, 0.35, 0.7, 1.0):
        island._expand = progress
        island._target_open = progress > 0
        island._apply_geometry()
        island.resize(island.width(), island.height())
        frames.append(island.grab())

    width = max(frame.width() for frame in frames) + 40
    height = sum(frame.height() for frame in frames) + 40
    canvas = wallpaper(width, height)
    y = 10
    for frame in frames:
        compose(frame, canvas, QPoint((width - frame.width()) // 2, y))
        y += frame.height() + 6
    canvas.save(str(out / 'island.png'))

    # Hot state: nearly out of quota.
    apply_demo_data(store, session=(96, 71), weekly=(88, 93))
    island._expand = 1.0
    island._target_open = True
    island._apply_geometry()
    hot = island.grab()
    collapsed = QImage(1, 1, QImage.Format.Format_ARGB32)
    island._expand = 0.0
    island._target_open = False
    island._apply_geometry()
    cold = island.grab()
    canvas = wallpaper(hot.width() + 40, hot.height() + cold.height() + 50, light=True)
    compose(cold, canvas, QPoint((canvas.width() - cold.width()) // 2, 10))
    compose(hot, canvas, QPoint(20, cold.height() + 20))
    canvas.save(str(out / 'island_hot_light.png'))
    del collapsed


def render_mascot(out: Path) -> None:
    states = [RUNNING, DONE, NEEDS_INPUT, STOPPED, IDLE]
    canvas = wallpaper(len(states) * 150 + 20, 2 * 120 + 20)
    painter = QPainter(canvas)
    for row, seconds in enumerate((0.05, 0.2)):
        for column, state in enumerate(states):
            painter.fillRect(20 + column * 150 - 6, 14 + row * 120, 130, 100, QColor(0, 0, 0, 230))
            draw_mascot(painter, QRectF(20 + column * 150, 24 + row * 120, 120, 74), state, seconds)
    painter.end()
    canvas.save(str(out / 'mascot.png'))

    big = wallpaper(7 * 40 + 20, 90)
    painter = QPainter(big)
    painter.fillRect(10, 10, big.width() - 20, 70, QColor(0, 0, 0, 240))
    for column, state in enumerate((RUNNING, DONE, NEEDS_INPUT, IDLE, RUNNING, DONE, IDLE)):
        draw_mascot(painter, QRectF(20 + column * 40, 28, 31, 24), state, 0.1 + column * 0.07)
    painter.end()
    big.save(str(out / 'mascot_small.png'))


def render_activity(out: Path, store: UsageStore) -> None:
    island = make_island(store, (NEEDS_INPUT, RUNNING, DONE))
    shots = []
    for progress in (0.0, 1.0):
        island._expand = progress
        island._target_open = progress > 0
        island._refresh_rows()
        island._apply_geometry()
        shots.append(island.grab())

    canvas = wallpaper(shots[1].width() + 40, shots[0].height() + shots[1].height() + 30)
    compose(shots[0], canvas, QPoint((canvas.width() - shots[0].width()) // 2, 6))
    compose(shots[1], canvas, QPoint(20, shots[0].height() + 16))
    canvas.save(str(out / 'island_activity.png'))


def render_attached(out: Path, store: UsageStore) -> None:
    island = make_island(store, (NEEDS_INPUT, RUNNING, DONE), 'attached')
    frames = []
    for progress in (0.0, 0.35, 0.7, 1.0):
        island._expand = progress
        island._target_open = progress > 0
        island._refresh_rows()
        island._apply_geometry()
        frames.append(island.grab())

    backgrounds = [('light', QColor('#f4f5f7')), ('dark', QColor('#1d1d21'))]
    width = max(frame.width() for frame in frames) + 40
    height = sum(frame.height() - 20 for frame in frames) + 30
    sheet = QImage(width * len(backgrounds), height, QImage.Format.Format_ARGB32)
    painter = QPainter(sheet)
    for column, (_name, colour) in enumerate(backgrounds):
        painter.fillRect(column * width, 0, width, height, colour)
        painter.fillRect(column * width, 0, width, 34, colour.darker(108) if colour.lightness() > 128 else colour.lighter(130))
        y = 0
        for frame in frames:
            painter.drawPixmap(QPoint(column * width + (width - frame.width()) // 2, y), frame)
            y += frame.height() - 20
    painter.end()
    sheet.save(str(out / 'island_attached.png'))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / 'preview'
    out.mkdir(parents=True, exist_ok=True)
    application = QApplication(sys.argv)
    store = make_store(out)
    render_icons(out)
    render_card(out, store)
    render_island(out, store)
    render_mascot(out)
    render_activity(out, store)
    render_attached(out, store)
    print('written to', out)
    del application
    return 0


if __name__ == '__main__':
    sys.exit(main())
