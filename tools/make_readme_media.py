"""
README Media
============

Renders the pictures and animated GIFs used by README.md from the real widgets (demo data, a fake clock so the
animation is deterministic and no real account or login is involved).

    QT_QPA_PLATFORM=offscreen python tools/make_readme_media.py assets
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_pulse import island as island_module  # noqa: E402
from claude_pulse import tray_icon  # noqa: E402
from claude_pulse.activity import DONE, IDLE, NEEDS_INPUT, RUNNING, STOPPED  # noqa: E402
from claude_pulse.card import MARGIN, HoverCard  # noqa: E402
from claude_pulse.demo import apply_demo_data  # noqa: E402
from claude_pulse.mascot import draw_mascot  # noqa: E402
from claude_pulse.tween import Tweener  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preview import make_island, make_store  # noqa: E402

WIDTH, HEIGHT, TASKBAR = 1120, 630, 56
FPS = 20


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def to_pil(image: QImage) -> Image.Image:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombuffer('RGBA', (converted.width(), converted.height()), bytes(converted.constBits()), 'raw', 'RGBA', converted.bytesPerLine(), 1)


def wallpaper() -> QImage:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QLinearGradient(0, 0, WIDTH, HEIGHT)
    base.setColorAt(0, QColor('#1b1f3a'))
    base.setColorAt(1, QColor('#0f1224'))
    painter.fillRect(image.rect(), base)
    for x, y, radius, colour in ((0.22, 0.30, 520, '#5b4bd6'), (0.80, 0.25, 460, '#d97757'), (0.55, 0.95, 560, '#2f7fe8')):
        glow = QRadialGradient(WIDTH * x, HEIGHT * y, radius)
        tint = QColor(colour)
        tint.setAlpha(120)
        glow.setColorAt(0, tint)
        tint.setAlpha(0)
        glow.setColorAt(1, tint)
        painter.fillRect(image.rect(), glow)
    painter.end()
    return image


def editor_window(painter: QPainter) -> None:
    """A fake code editor in the middle of the screen: the thing the island must never cover."""
    box = QRectF(150, 78, WIDTH - 300, HEIGHT - TASKBAR - 78 - 40)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 90))
    painter.drawRoundedRect(box.translated(0, 10).adjusted(-6, -2, 6, 6), 14, 14)
    painter.setBrush(QColor('#1e1f26'))
    painter.drawRoundedRect(box, 12, 12)
    painter.setBrush(QColor('#2a2c36'))
    bar = QPainterPath()
    bar.addRoundedRect(QRectF(box.left(), box.top(), box.width(), 34), 12, 12)
    bar.addRect(QRectF(box.left(), box.top() + 20, box.width(), 14))
    painter.drawPath(bar.simplified())
    for index, colour in enumerate(('#ff5f57', '#febc2e', '#28c840')):
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QPointF(box.left() + 22 + index * 20, box.top() + 17), 6, 6)
    palette = ('#7aa2f7', '#bb9af7', '#9ece6a', '#e0af68', '#c0caf5', '#f7768e')
    y = box.top() + 56
    for row in range(14):
        x = box.left() + 28 + (row % 3) * 26 * (1 if row % 5 else 0)
        for piece in range(3 + (row * 7) % 4):
            length = 40 + (row * 37 + piece * 53) % 110
            painter.setBrush(QColor(palette[(row + piece * 2) % len(palette)]))
            painter.setOpacity(0.55)
            painter.drawRoundedRect(QRectF(x, y, length, 9), 4, 4)
            x += length + 12
        y += 22
    painter.setOpacity(1.0)


def taskbar(painter: QPainter, icon_pixmap) -> QRectF:
    """Draws a Windows-11-style taskbar and returns the rectangle of our tray icon."""
    top = HEIGHT - TASKBAR
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(28, 28, 32, 238))
    painter.drawRect(0, top, WIDTH, TASKBAR)
    painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
    painter.drawLine(0, top, WIDTH, top)

    # centred app icons
    painter.setPen(Qt.PenStyle.NoPen)
    for index, colour in enumerate(('#4cc2ff', '#9ece6a', '#d97757', '#bb9af7', '#e0af68')):
        centre = WIDTH / 2 + (index - 2) * 52
        painter.setBrush(QColor(255, 255, 255, 14) if index == 2 else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(centre - 22, top + 6, 44, 44), 8, 8)
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(QRectF(centre - 11, top + 17, 22, 22), 6, 6)

    centre_y = top + TASKBAR / 2
    white = QColor(255, 255, 255, 230)
    painter.setPen(QPen(white, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # chevron
    x = WIDTH - 268
    painter.drawPolyline(QPolygonF([QPointF(x - 4, centre_y + 2), QPointF(x, centre_y - 2), QPointF(x + 4, centre_y + 2)]))

    # our icon
    icon_size = 28
    icon_rect = QRectF(WIDTH - 238, centre_y - icon_size / 2, icon_size, icon_size)
    painter.drawPixmap(icon_rect.toRect(), icon_pixmap)
    painter.setPen(QPen(white, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

    # wifi
    x = WIDTH - 184
    for radius in (10, 6.5, 3):
        painter.drawArc(QRectF(x - radius, centre_y + 5 - radius, radius * 2, radius * 2), 45 * 16, 90 * 16)
    painter.setBrush(white)
    painter.drawEllipse(QPointF(x, centre_y + 5), 1.4, 1.4)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # volume
    x = WIDTH - 150
    painter.drawPolyline(QPolygonF([QPointF(x - 8, centre_y - 3), QPointF(x - 4, centre_y - 3), QPointF(x + 1, centre_y - 8),
                                    QPointF(x + 1, centre_y + 8), QPointF(x - 4, centre_y + 3), QPointF(x - 8, centre_y + 3), QPointF(x - 8, centre_y - 3)]))
    painter.drawArc(QRectF(x - 2, centre_y - 6, 12, 12), -50 * 16, 100 * 16)

    # battery
    x = WIDTH - 116
    painter.drawRoundedRect(QRectF(x - 11, centre_y - 6, 21, 12), 3, 3)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(white)
    painter.drawRoundedRect(QRectF(x - 9, centre_y - 4, 14, 8), 1.5, 1.5)
    painter.drawRect(QRectF(x + 11, centre_y - 2, 2, 4))

    # clock
    painter.setPen(white)
    font = QFont('Segoe UI', 9)
    painter.setFont(font)
    painter.drawText(QRectF(WIDTH - 86, top + 6, 76, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, '9:41 AM')
    painter.setPen(QColor(255, 255, 255, 190))
    painter.drawText(QRectF(WIDTH - 86, top + 27, 76, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, '11/26/2026')
    return icon_rect


def cursor(painter: QPainter, x: float, y: float) -> None:
    arrow = QPolygonF([QPointF(0, 0), QPointF(0, 17), QPointF(4.4, 13.2), QPointF(7.4, 20), QPointF(10, 18.9), QPointF(7.1, 12.3), QPointF(12.6, 12.3)])
    painter.save()
    painter.translate(x, y)
    painter.setPen(QPen(QColor(0, 0, 0, 230), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(QColor('white'))
    painter.drawPolygon(arrow)
    painter.restore()


def ease(fraction: float) -> float:
    fraction = min(1.0, max(0.0, fraction))
    return fraction * fraction * (3 - 2 * fraction)


def ease_out(fraction: float) -> float:
    return 1.0 - (1.0 - min(1.0, max(0.0, fraction))) ** 3


def segment(time: float, start: float, end: float) -> float:
    return min(1.0, max(0.0, (time - start) / (end - start)))


def render_hero(out: Path, clock: Clock) -> None:
    store = make_store(out)
    apply_demo_data(store, session=(42, 18), weekly=(34, 12))
    island = make_island(store, (RUNNING, DONE, IDLE), 'attached')
    card = HoverCard(store, Tweener(), use_24h=True)
    card.set_island_mode('hover')
    store.select('team')

    light_icon = tray_icon.render_icon(64, 42, 34, 0, 2, False)
    icon_pro = tray_icon.render_icon(64, 18, 12, 1, 2, False)
    background = wallpaper()
    painter = QPainter(background)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    editor_window(painter)
    painter.end()

    duration = 11.0
    frames = []
    selected = 'team'
    for index in range(int(duration * FPS)):
        time = index / FPS
        clock.now = 1000.0 + time

        # island: open 1.0-1.9 (spring), hold, close 3.7-4.3
        opening = segment(time, 1.0, 1.9)
        closing = segment(time, 3.8, 4.4)
        if time < 3.8:
            expand = island_module._spring(opening)
        else:
            expand = max(0.0, 1.0 - ease(closing))
        island._expand = expand
        island._target_open = expand > 0.001
        island._refresh_rows()
        island._apply_geometry()
        island_image = island.grab()

        # card: cursor travels to the tray icon 4.6-5.4, card reveals, toggles account at 7.4, hides 9.6
        reveal = ease_out(segment(time, 5.3, 5.75)) * (1.0 - ease(segment(time, 9.7, 10.15)))
        if time >= 7.6 and selected == 'team':
            selected = 'pro'
            store.select('pro')
        card._reveal = reveal
        card._remeasure()
        card_image = card.grab() if reveal > 0.001 else None

        canvas = QImage(background)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap((WIDTH - island_image.width()) // 2, 0, island_image)
        icon_rect = taskbar(painter, icon_pro if selected == 'pro' else light_icon)
        if card_image is not None:
            left = icon_rect.center().x() - card_image.width() / 2
            left = min(left, WIDTH - card_image.width() - 6 + MARGIN)
            painter.drawPixmap(round(left), HEIGHT - TASKBAR - 10 + MARGIN - card_image.height(), card_image)

        # pointer path: rest -> top edge (0.2-0.9) -> back down (3.9-4.6) -> tray icon (4.6-5.4) -> stay -> away (9.4)
        px, py = WIDTH * 0.66, HEIGHT * 0.62
        if time < 0.2:
            pass
        elif time < 3.9:
            fraction = ease(segment(time, 0.2, 0.9))
            px, py = px + (WIDTH / 2 - px) * fraction, py + (6 - py) * fraction
            if time >= 3.0:
                away = ease(segment(time, 3.0, 3.9))
                px, py = WIDTH / 2 + (WIDTH * 0.66 - WIDTH / 2) * away, 6 + (HEIGHT * 0.62 - 6) * away
        else:
            fraction = ease(segment(time, 4.5, 5.35))
            px, py = px + (icon_rect.center().x() - px) * fraction, py + (icon_rect.center().y() - py) * fraction
            if time >= 6.9:
                # glide up onto the toggle for the account switch, then settle back
                fraction = ease(segment(time, 6.9, 7.5))
                toggle_x, toggle_y = icon_rect.center().x() + 50, HEIGHT - TASKBAR - 10 - 330
                px, py = px + (toggle_x - px) * fraction, py + (toggle_y - py) * fraction
        cursor(painter, px, py)
        painter.end()
        frames.append(to_pil(canvas).convert('RGB'))

    save_gif(frames, out / 'hero.gif', colours=256, scale=0.86)
    frames[int(8.4 * FPS)].save(out / 'hero.png', optimize=True)
    frames[int(2.9 * FPS)].save(out / 'hero-island.png', optimize=True)


def save_gif(frames: list[Image.Image], path: Path, colours: int = 128, scale: float = 1.0) -> None:
    """Shared palette and no dithering: frames then differ only where something moved, which keeps the file small."""
    if scale != 1.0:
        size = (round(frames[0].width * scale), round(frames[0].height * scale))
        frames = [frame.resize(size, Image.Resampling.LANCZOS) for frame in frames]
    sample = frames[:: max(1, len(frames) // 24)]
    sheet = Image.new('RGB', (sample[0].width, sample[0].height * len(sample)))
    for index, frame in enumerate(sample):
        sheet.paste(frame, (0, index * frame.height))
    palette = sheet.quantize(colors=colours, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    quantised = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    quantised[0].save(path, save_all=True, append_images=quantised[1:], duration=int(1000 / FPS), loop=0, optimize=False, disposal=1)
    print(f'{path.name}: {len(frames)} frames, {path.stat().st_size / 1e6:.2f} MB')


def render_mascot(out: Path, clock: Clock) -> None:
    states = [(RUNNING, 'running'), (DONE, 'finished'), (NEEDS_INPUT, 'needs you'), (IDLE, 'idle'), (STOPPED, 'stopped')]
    cell, rows_height = 170, 150
    width, height = cell * len(states) + 20, rows_height
    frames = []
    for index in range(int(2.4 * FPS)):
        seconds = 5.0 + index / FPS
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor('#14151c'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for column, (state, label) in enumerate(states):
            left = 10 + column * cell
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#000000'))
            painter.drawRoundedRect(QRectF(left + 8, 14, cell - 16, 96), 22, 22)
            draw_mascot(painter, QRectF(left + 30, 30, 110, 64), state, seconds)
            painter.setPen(QColor(255, 255, 255, 170))
            painter.setFont(QFont('Segoe UI', 10))
            painter.drawText(QRectF(left, 116, cell, 26), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()
        frames.append(to_pil(image).convert('RGB'))
    save_gif(frames, out / 'mascot.gif', colours=48)


def render_stills(out: Path) -> None:
    store = make_store(out)
    apply_demo_data(store, session=(42, 18), weekly=(34, 12))
    card = HoverCard(store, Tweener(), use_24h=True)
    card.set_island_mode('hover')
    card._reveal = 1.0
    card._remeasure()
    pixmap = card.grab()
    canvas = wallpaper().copy(0, 0, pixmap.width() + 80, pixmap.height() + 80)
    painter = QPainter(canvas)
    painter.drawPixmap(40, 40, pixmap)
    painter.end()
    canvas.save(str(out / 'card.png'))

    island = make_island(store, (RUNNING, DONE, IDLE), 'attached')
    shots = []
    for progress in (0.0, 1.0):
        island._expand = progress
        island._target_open = progress > 0
        island._refresh_rows()
        island._apply_geometry()
        shots.append(island.grab())
    width = max(shot.width() for shot in shots) + 60
    height = sum(shot.height() for shot in shots) + 30
    canvas = wallpaper().copy(0, 0, width, height)
    painter = QPainter(canvas)
    y = 0
    for shot in shots:
        painter.drawPixmap((width - shot.width()) // 2, y, shot)
        y += shot.height() - 10
    painter.end()
    canvas.save(str(out / 'island.png'))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / 'assets'
    out.mkdir(parents=True, exist_ok=True)
    application = QApplication(sys.argv)
    clock = Clock()
    with mock.patch('time.monotonic', clock):
        render_hero(out, clock)
        render_mascot(out, clock)
        render_stills(out)
    del application
    return 0


if __name__ == '__main__':
    sys.exit(main())
