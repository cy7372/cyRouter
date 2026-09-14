"""System tray integration for the frozen (exe) build.

Runs on a dedicated daemon thread and exposes a threading.Event that the
main thread polls to know when the user clicked "Quit". Non-frozen
development runs skip the tray entirely.
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from typing import Callable

logger = logging.getLogger("cyRouter.tray")

# Set by the tray thread when the user chooses Quit.
_quit_event = threading.Event()

# ── Windows boot-autostart via HKCU\...\Run (no admin needed) ──
_AUTOSTART_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Run"
)
_AUTOSTART_NAME = "cyRouter"


def _is_autostart_enabled() -> bool:
    """True if the HKCU Run entry for cyRouter exists."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(f"Failed to read autostart status: {e}")
        return False


def set_autostart(enabled: bool) -> bool:
    """Enable/disable boot autostart. Returns the resulting state."""
    if sys.platform != "win32":
        logger.warning("Autostart only supported on Windows")
        return False
    exe = sys.executable if getattr(sys, "frozen", False) else None
    if not exe:
        logger.warning("Autostart only available in frozen (exe) builds")
        return False
    # Quote the path to survive spaces in the install location.
    cmd = f'"{exe}"'
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _AUTOSTART_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as k:
            if enabled:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        logger.info(f"Autostart {'enabled' if enabled else 'disabled'}")
        return enabled
    except OSError as e:
        logger.error(f"Failed to toggle autostart: {e}")
        return _is_autostart_enabled()


def _make_icon_image():
    """Draw the cyRouter logo (2x2 squares) matching the web favicon.

    Kept in-process so the tray icon works even if the bundled .png is
    unavailable; the exe itself uses packaging/cyrouter.ico.
    """
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    BG = (13, 15, 22, 255)
    FG = (0, 229, 160, 255)
    FG_DIM = (0, 229, 160, 128)
    FG_FAINT = (0, 229, 160, 77)

    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 8 / 32, fill=BG)

    def fill(ix, iy, color):
        x0, y0 = size * ix / 32, size * iy / 32
        w = size * 10 / 32
        d.rounded_rectangle((x0, y0, x0 + w, y0 + w), radius=size * 2.5 / 32, fill=color)

    def stroke(ix, iy, color):
        x0, y0 = size * ix / 32, size * iy / 32
        w = size * 10 / 32
        d.rounded_rectangle(
            (x0, y0, x0 + w, y0 + w),
            radius=size * 2.5 / 32,
            outline=color,
            width=max(1, int(size * 1.5 / 32)),
        )

    fill(4, 4, FG)
    fill(18, 4, FG_DIM)
    fill(4, 18, FG_FAINT)
    stroke(18, 18, FG)
    return img


def _open_browser(url: str):
    try:
        webbrowser.open(url)
    except Exception as e:
        logger.warning(f"Failed to open browser: {e}")


def start_tray(
    *,
    url: str,
    on_quit: Callable[[], None] | None = None,
) -> threading.Event:
    """Start the tray icon on a daemon thread.

    Returns the quit-event the caller should poll. When set, the server
    should shut down.
    """
    try:
        import pystray
    except ImportError:
        logger.warning("pystray not available; running without tray icon")
        return _quit_event

    def _on_quit(icon, item):
        _quit_event.set()
        if on_quit:
            try:
                on_quit()
            except Exception:
                pass
        icon.stop()

    def _on_open(icon, item):
        _open_browser(url)

    def _on_autostart(icon, item):
        # Toggle the HKCU Run entry; icon auto-refreshes its check state via
        # the `checked` callable below on next menu open.
        set_autostart(not _is_autostart_enabled())
        icon.update_menu()

    def _autostart_checked(item):
        return _is_autostart_enabled()

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", _on_open, default=True),
        pystray.MenuItem(
            "开机自启",
            _on_autostart,
            checked=_autostart_checked,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit cyRouter", _on_quit),
    )

    icon = pystray.Icon("cyRouter", _make_icon_image(), "cyRouter", menu)

    def _run():
        try:
            icon.run()
        except Exception as e:
            logger.warning(f"Tray icon stopped: {e}")

    t = threading.Thread(target=_run, name="cyRouter-tray", daemon=True)
    t.start()
    logger.info(f"Tray icon started; dashboard URL: {url}")
    return _quit_event
