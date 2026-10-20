"""TEMPORARY (Claude Code bridge, see claudecode.py): the screen, mouse and keyboard of this computer, as MCP tools
for Claude Code's operate_computer tasks.

    python -m labharness.screen      # an MCP server on stdio (Kelvin.exe --screen-mcp in the private build)

Coordinates are in the screenshot Claude was shown (scaled down to at most 1366 px wide); Kelvin converts them to
the screen's own pixels. Windows uses the system's own input calls; macOS needs pyobjc's Quartz for the mouse and
keyboard (screenshots work without it)."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

MAX_WIDTH = 1366
WIN = sys.platform.startswith("win")
_scale = {"f": 1.0}   # screen pixels per screenshot pixel, from the last screenshot

KEYS = {   # names Claude may use -> Windows virtual-key codes
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "ctrl": 0x11, "control": 0x11, "shift": 0x10,
    "alt": 0x12, "win": 0x5B, "cmd": 0x5B, "super": 0x5B, "capslock": 0x14, "printscreen": 0x2C,
    **{"f%d" % i: 0x6F + i for i in range(1, 13)},
}
MODIFIERS = {"ctrl", "control", "shift", "alt", "win", "cmd", "super"}


def parse_keys(combo: str) -> List[int]:
    """'ctrl+shift+s' -> virtual-key codes, modifiers first."""
    codes = []
    for part in [p.strip().lower() for p in str(combo).replace(" ", "").split("+") if p.strip()]:
        if part in KEYS:
            codes.append(KEYS[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError("unknown key %r (use names like enter, tab, esc, ctrl+s, alt+f4, f5)" % part)
    if not codes:
        raise ValueError("no key given")
    return codes


def to_screen(x: float, y: float) -> Tuple[int, int]:
    return int(round(float(x) * _scale["f"])), int(round(float(y) * _scale["f"]))


def fit(size: Tuple[int, int], max_width: int = MAX_WIDTH) -> Tuple[Tuple[int, int], float]:
    """The size a screenshot is shown at, and the screen pixels per shown pixel."""
    w, h = size
    if w <= max_width:
        return (w, h), 1.0
    f = w / float(max_width)
    return (max_width, int(round(h / f))), f


# ------------------------------------------------------------------ the operating system

def _dpi_aware() -> None:
    if WIN:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def grab() -> Any:
    """A picture of the main screen (PIL image)."""
    from PIL import Image, ImageGrab
    if sys.platform == "darwin":
        path = Path(tempfile.mkdtemp()) / "screen.png"
        subprocess.run(["screencapture", "-x", str(path)], check=True)
        return Image.open(path)
    return ImageGrab.grab()


def screenshot_png() -> Tuple[bytes, str]:
    image = grab()
    shown, f = fit(image.size)
    _scale["f"] = f
    if f != 1.0:
        image = image.resize(shown)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue(), "Screen %dx%d, shown at %dx%d; give coordinates in this picture." % (
        int(shown[0] * f), int(shown[1] * f), shown[0], shown[1])


def _win_mouse(flags: int, data: int = 0) -> None:
    import ctypes
    ctypes.windll.user32.mouse_event(flags, 0, 0, data, 0)


def _win_key(code: int, up: bool) -> None:
    import ctypes
    ctypes.windll.user32.keybd_event(code, 0, 0x0002 if up else 0, 0)


def _quartz() -> Any:
    try:
        import Quartz   # type: ignore
        return Quartz
    except ImportError:
        raise RuntimeError("the mouse and keyboard on macOS need pyobjc (pip install pyobjc-framework-Quartz)")


def move(x: int, y: int) -> None:
    if WIN:
        import ctypes
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
    else:
        q = _quartz()
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, (x, y), 0))


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    move(x, y)
    time.sleep(0.05)
    for _ in range(max(1, min(int(clicks), 3))):
        if WIN:
            down, up = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40)}[button]
            _win_mouse(down)
            _win_mouse(up)
        else:
            q = _quartz()
            kinds = {"left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGMouseButtonLeft),
                     "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGMouseButtonRight)}
            down, up, b = kinds.get(button, kinds["left"])
            for kind in (down, up):
                q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(None, kind, (x, y), b))
        time.sleep(0.08)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    move(x1, y1)
    if not WIN:
        raise RuntimeError("drag is only on Windows for now")
    _win_mouse(0x02)
    for i in range(1, 11):
        move(x1 + (x2 - x1) * i // 10, y1 + (y2 - y1) * i // 10)
        time.sleep(0.02)
    _win_mouse(0x04)


def scroll(x: int, y: int, amount: int) -> None:
    move(x, y)
    if WIN:
        _win_mouse(0x0800, int(-amount) * 120)   # positive amount scrolls down
    else:
        q = _quartz()
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateScrollWheelEvent(None, 0, 1, -int(amount)))


def press(combo: str) -> None:
    codes = parse_keys(combo)
    if not WIN:
        raise RuntimeError("keys are only on Windows for now")
    for c in codes:
        _win_key(c, False)
    for c in reversed(codes):
        _win_key(c, True)


def type_text(text: str) -> None:
    if not WIN:
        raise RuntimeError("typing is only on Windows for now")
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    for ch in str(text):
        if ch == "\n":
            press("enter")
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            for flags in (0x0004, 0x0004 | 0x0002):   # KEYEVENTF_UNICODE, then key up
                inp = INPUT(type=1, ki=KEYBDINPUT(0, code, flags, 0, None))
                ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        time.sleep(0.005)



def describe(name: str, args: Dict[str, Any]) -> str:
    """One line for the chat and the record: what Claude did on the screen."""
    a = args or {}
    short = name.split("__")[-1]
    if short == "screenshot":
        return "looked at the screen"
    if short == "click_at":
        return "%sclicked at (%s, %s)" % ({2: "double-", 3: "triple-"}.get(int(a.get("clicks") or 1), "")
                                         + ("" if a.get("button", "left") == "left" else a.get("button") + " "),
                                         a.get("x"), a.get("y"))
    if short == "type_into":
        text = str(a.get("text") or "")
        return "typed %r" % (text if len(text) <= 60 else text[:57] + "...")
    if short == "press_keys":
        return "pressed %s" % a.get("keys")
    if short == "scroll_at":
        return "scrolled %s at (%s, %s)" % (a.get("amount"), a.get("x"), a.get("y"))
    if short == "drag_from_to":
        return "dragged (%s, %s) to (%s, %s)" % (a.get("x1"), a.get("y1"), a.get("x2"), a.get("y2"))
    if short == "wait_seconds":
        return "waited %s s" % a.get("seconds")
    return short


# ------------------------------------------------------------------ the MCP server

def build() -> Any:
    from mcp.server.mcpserver import Image, MCPServer
    server = MCPServer("screen", instructions="The screen, mouse and keyboard of the lab computer. Take a screenshot "
                       "first and after each step that changes what is shown; coordinates are in the last screenshot.")

    @server.tool(structured_output=False)
    def screenshot() -> List[Any]:
        """A picture of the screen now."""
        png, note = screenshot_png()
        return [Image(data=png, format="png"), note]

    @server.tool(structured_output=False)
    def click_at(x: float, y: float, button: str = "left", clicks: int = 1) -> str:
        """Click at (x, y) in the last screenshot. button: left, right or middle; clicks: 1, 2 (double) or 3."""
        if button not in ("left", "right", "middle"):
            raise ValueError("button must be left, right or middle")
        click(*to_screen(x, y), button=button, clicks=clicks)
        return "clicked %s at (%s, %s)" % (button, x, y)

    @server.tool(structured_output=False)
    def type_into(text: str) -> str:
        """Type text where the keyboard focus is (a new line presses Enter)."""
        type_text(text[:5000])
        return "typed %d characters" % len(text[:5000])

    @server.tool(structured_output=False)
    def press_keys(keys: str) -> str:
        """Press a key or a combination, e.g. enter, tab, esc, ctrl+s, alt+f4, ctrl+shift+t, f5."""
        press(keys)
        return "pressed %s" % keys

    @server.tool(structured_output=False)
    def scroll_at(x: float, y: float, amount: int) -> str:
        """Scroll at (x, y): positive amount scrolls down, negative up, in mouse-wheel notches."""
        scroll(*to_screen(x, y), amount=max(-30, min(30, int(amount))))
        return "scrolled %d at (%s, %s)" % (amount, x, y)

    @server.tool(structured_output=False)
    def drag_from_to(x1: float, y1: float, x2: float, y2: float) -> str:
        """Drag with the left button held from (x1, y1) to (x2, y2)."""
        drag(*to_screen(x1, y1), *to_screen(x2, y2))
        return "dragged from (%s, %s) to (%s, %s)" % (x1, y1, x2, y2)

    @server.tool(structured_output=False)
    def wait_seconds(seconds: float) -> str:
        """Wait for a program to respond (at most 10 s)."""
        time.sleep(max(0.0, min(10.0, float(seconds))))
        return "waited %s s" % seconds

    return server


def main() -> int:
    _dpi_aware()
    build().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
