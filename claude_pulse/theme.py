"""
Theme
=====

Colours and fonts.  The palette follows Apple's dark system colours: calm
green while there is plenty of quota, orange when it is getting used up,
red when it is nearly gone.  Accounts get a fixed accent so they are easy to
tell apart.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QFontDatabase

from .formatting import severity

__all__ = [
    'BODY', 'STROKE', 'TEXT', 'TEXT_DIM', 'TEXT_FAINT', 'TRACK', 'SELECTED', 'GREEN', 'ORANGE', 'RED',
    'account_color', 'severity_color', 'ui_font',
]

BODY = QColor(20, 20, 23, 251)
STROKE = QColor(255, 255, 255, 26)
TEXT = QColor(255, 255, 255, 245)
TEXT_DIM = QColor(255, 255, 255, 150)
TEXT_FAINT = QColor(255, 255, 255, 112)
TRACK = QColor(255, 255, 255, 30)
SELECTED = QColor(255, 255, 255, 30)

GREEN = QColor('#30D158')
ORANGE = QColor('#FF9F0A')
RED = QColor('#FF453A')

_ACCOUNT_COLORS = (QColor('#0A84FF'), QColor('#BF5AF2'), QColor('#64D2FF'))
_PREFERRED_FAMILIES = ('Segoe UI Variable Display', 'Segoe UI Variable Text', 'Segoe UI')


def account_color(index: int) -> QColor:
    return QColor(_ACCOUNT_COLORS[index % len(_ACCOUNT_COLORS)])


def severity_color(percent: float | None) -> QColor:
    return QColor({'ok': GREEN, 'warn': ORANGE, 'critical': RED}[severity(percent)])


@lru_cache(maxsize=1)
def _family() -> str:
    installed = set(QFontDatabase.families())
    for family in _PREFERRED_FAMILIES:
        if family in installed:
            return family

    # Constructing the default font initialises Qt's whole system-font machinery (about 33 MB), so only do it as a last resort.
    return QFont().defaultFamily()


def ui_font(pixel_size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(_family())
    font.setPixelSize(max(1, round(pixel_size)))
    font.setWeight(weight)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font
