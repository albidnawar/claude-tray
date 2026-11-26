"""
Windows Integration
===================

Everything OS-specific lives here, so the rest of the app stays portable
Qt code: making floating windows non-activating and click-through, noticing
a full-screen app, reading the taskbar theme, and the autostart entry.
Every helper degrades to a harmless default when the call is unavailable.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

__all__ = [
    'IS_WINDOWS', 'configure_floating_window', 'set_click_through', 'left_button_down', 'fullscreen_app_active',
    'taskbar_is_light', 'autostart_enabled', 'set_autostart', 'trim_working_set',
]

IS_WINDOWS = sys.platform == 'win32'

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_VK_LBUTTON = 0x01
_FULLSCREEN_STATES = {2, 3, 4}  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
_PERSONALIZE_KEY = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_VALUE = 'ClaudePulse'

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    _kernel32.K32EmptyWorkingSet.restype = wintypes.BOOL


def configure_floating_window(window_id: int) -> None:
    """Keep the window out of Alt+Tab and stop it from ever taking focus."""
    _modify_ex_style(window_id, add=_WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)


def set_click_through(window_id: int, enabled: bool) -> None:
    """While enabled, mouse input passes straight through the window to whatever is beneath."""
    if enabled:
        _modify_ex_style(window_id, add=_WS_EX_TRANSPARENT)
    else:
        _modify_ex_style(window_id, remove=_WS_EX_TRANSPARENT)


def left_button_down() -> bool:
    """True while the left button is held, or if it was pressed at any moment since the previous call.

    The second part catches clicks shorter than the caller's polling interval.
    """
    if not IS_WINDOWS:
        return False

    return bool(_user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8001)


def fullscreen_app_active() -> bool:
    """True while a full-screen game, video or presentation owns the screen."""
    if not IS_WINDOWS:
        return False

    state = ctypes.c_int(0)
    try:
        if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except OSError:
        return False

    return state.value in _FULLSCREEN_STATES


def taskbar_is_light() -> bool:
    """True when the Windows taskbar uses the light theme."""
    if not IS_WINDOWS:
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            return bool(winreg.QueryValueEx(key, 'SystemUsesLightTheme')[0])
    except OSError:
        return False


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool, launcher: Path) -> None:
    """Add or remove the per-user Run entry that starts the app at sign-in."""
    if not IS_WINDOWS:
        return

    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            interpreter = Path(sys.executable).with_name('pythonw.exe')
            python = interpreter if interpreter.is_file() else Path(sys.executable)
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, f'"{python}" "{launcher}"')
            return

        try:
            winreg.DeleteValue(key, _RUN_VALUE)
        except FileNotFoundError:
            pass


def trim_working_set() -> bool:
    """Hand this process's resident pages back to Windows.

    Startup and each burst of activity leave pages in RAM that nothing touches for a long time.  They move to the
    standby list and are faulted back in on demand, so an idle app stops showing as large.  Only the resident
    set shrinks; committed memory is unchanged.
    """
    if not IS_WINDOWS:
        return False

    return bool(_kernel32.K32EmptyWorkingSet(_kernel32.GetCurrentProcess()))


def _modify_ex_style(window_id: int, add: int = 0, remove: int = 0) -> None:
    if not IS_WINDOWS:
        return

    handle = wintypes.HWND(window_id)
    current = _user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)
    updated = (current | add) & ~remove
    if updated != current:
        _user32.SetWindowLongPtrW(handle, _GWL_EXSTYLE, updated)
