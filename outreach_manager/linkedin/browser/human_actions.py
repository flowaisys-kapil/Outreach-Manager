# outreach_manager/linkedin/browser/human_actions.py
"""
Human behavior simulation for the Outreach Manager browser automation.

Ported and significantly enhanced from Outreach Manager's:
  - HumanBehaviorHelper  (human_type, scroll_like_human)
  - OSMouseHelper        (OS-level real mouse movement via Win32 API)

Key additions over Outreach Manager:
  - Bezier-curve mouse path generation (not straight lines)
  - Overshoot-and-correct on clicks (micro-correction after landing)
  - Pre-click hover dwell (human hesitation before clicking)
  - Page-reading simulation (partial scrolls + random element hovering)
  - Burst-pause typing rhythm (humans type in bursts, not uniform cadence)
  - Per-call random_pause replaces all bare time.sleep() calls

All functions accept a Playwright sync Page object and/or a Locator.
No external dependencies beyond playwright and standard-library modules.
"""
from __future__ import annotations

import logging
import math
import random
import subprocess
import time
from typing import Optional

from playwright.sync_api import Locator, Page

logger = logging.getLogger(__name__)

# Ensure Win32 DPI awareness is initialized so physical mouse cursor coordinates
# match true 1-to-1 screen pixels without Windows OS virtualization scaling distortion.
import sys
if sys.platform.startswith("win"):
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Timing constants — tuned to feel natural without being excessively slow
# ---------------------------------------------------------------------------
_TYPE_MIN_MS   = 45    # minimum per-keystroke delay (ms)
_TYPE_MAX_MS   = 130   # maximum per-keystroke delay (ms)
_BURST_CHARS   = (3, 8)   # burst size range (chars per burst)
_BURST_PAUSE   = (0.08, 0.25)  # pause between bursts (s)
_TYPO_RATE     = 0.035  # probability of adjacent-key typo + backspace
_HOVER_DWELL   = (0.18, 0.55)  # hover dwell before clicking (s)
_OVERSHOOT     = (1, 4)  # overshoot px before correcting (pixels)

# Adjacent-key map for typo simulation (QWERTY layout)
_ADJACENT: dict[str, str] = {
    'a':'sq','b':'vghn','c':'xdfv','d':'esfxc','e':'wrd','f':'dgtre',
    'g':'fhtyr','h':'gjuy','i':'uoj','j':'hkuy','k':'jli','l':'ko',
    'm':'njk','n':'bmhj','o':'ipkl','p':'ol','q':'wa','r':'etf',
    's':'awdze','t':'ryg','u':'yihjk','v':'cfgb','w':'qase',
    'x':'zsdc','y':'tugh','z':'asx',
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rand(min_v: float, max_v: float) -> float:
    return random.uniform(min_v, max_v)


_LAST_VIRTUAL_MOUSE: dict[int, tuple[float, float]] = {}


def _get_start_mouse_pos(page: Page) -> tuple[float, float]:
    page_id = id(page)
    if page_id in _LAST_VIRTUAL_MOUSE:
        return _LAST_VIRTUAL_MOUSE[page_id]
    
    # Default: mouse enters from a random edge of the viewport
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    w = viewport["width"]
    h = viewport["height"]
    if random.random() < 0.5:
        sx = 0.0 if random.random() < 0.5 else float(w)
        sy = _rand(0, h)
    else:
        sx = _rand(0, w)
        sy = 0.0 if random.random() < 0.5 else float(h)
    return sx, sy


def is_chrome_in_foreground(page: Optional[Page] = None) -> bool:
    """Return True if the specific Google Chrome automation window is currently in the active foreground."""
    import sys
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
            
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
            
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h_process:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
                    image_name = buf.value.lower()
                    if "chrome.exe" not in image_name:
                        return False
            finally:
                kernel32.CloseHandle(h_process)
        else:
            return False

        # If page is provided, ensure it actually has active OS/browser focus
        if page:
            try:
                return bool(page.evaluate("() => document.hasFocus()"))
            except Exception:
                return False
        return True
    except Exception as e:
        logger.debug("is_chrome_in_foreground check failed: %s", e)
    return False


def _bezier_path(
    x0: float, y0: float, x1: float, y1: float, steps: int = 20
) -> list[tuple[float, float]]:
    """Generate a Bezier-curved mouse path between two points.

    Two random control points create a natural arc. The path is non-linear
    so velocity varies naturally — humans never move in perfect straight lines.
    """
    mid_x = (x0 + x1) / 2
    mid_y = (y0 + y1) / 2

    cp1x = mid_x + _rand(-80, 80)
    cp1y = mid_y + _rand(-60, 60)
    cp2x = mid_x + _rand(-60, 60)
    cp2y = mid_y + _rand(-40, 40)

    path = []
    for i in range(steps + 1):
        t = i / steps
        # Cubic Bezier formula
        bx = (
            (1 - t) ** 3 * x0
            + 3 * (1 - t) ** 2 * t * cp1x
            + 3 * (1 - t) * t ** 2 * cp2x
            + t ** 3 * x1
        )
        by = (
            (1 - t) ** 3 * y0
            + 3 * (1 - t) ** 2 * t * cp1y
            + 3 * (1 - t) * t ** 2 * cp2y
            + t ** 3 * y1
        )
        path.append((bx, by))
    return path


def _get_element_center(locator: Locator) -> tuple[float, float]:
    """Return the viewport-relative center (x, y) of a Locator's element."""
    box = locator.bounding_box()
    if not box:
        raise RuntimeError("Element has no bounding box — may be hidden or detached")
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _os_mouse_move_and_click(target_screen_x: int, target_screen_y: int) -> None:
    """Move physical mouse cursor to target screen coordinates along a Bezier path, then click."""
    import ctypes
    import sys
    if not sys.platform.startswith("win"):
        return
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            
        user32 = ctypes.windll.user32
        
        # Get current physical cursor position
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        x0, y0 = pt.x, pt.y
        
        # Generate Bezier path
        steps = random.randint(15, 25)
        path = _bezier_path(x0, y0, target_screen_x, target_screen_y, steps=steps)
        
        # Move physical cursor along the path
        for px, py in path:
            user32.SetCursorPos(int(px), int(py))
            time.sleep(random.uniform(0.005, 0.015))
            
        # Micro-overshoot and correction
        overshoot_x = target_screen_x + random.randint(-3, 3)
        overshoot_y = target_screen_y + random.randint(-3, 3)
        user32.SetCursorPos(overshoot_x, overshoot_y)
        time.sleep(random.uniform(0.02, 0.05))
        user32.SetCursorPos(target_screen_x, target_screen_y)
        time.sleep(random.uniform(0.03, 0.07))
        
        # Trigger left mouse button down and up
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(random.uniform(0.05, 0.12))  # hold click naturally
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception as exc:
        logger.debug("OS mouse move and click via ctypes failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def random_pause(min_s: float = 0.8, max_s: float = 2.5, page: Optional[Page] = None) -> None:
    """Sleep for a random duration and optionally wait for page load state.
    
    If page is provided, wiggles the cursor over a random text block to simulate human rest.
    """
    delay = _rand(min_s, max_s)
    logger.debug("Pause %.2fs", delay)
    
    if page:
        start_time = time.time()
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        w = viewport["width"]
        h = viewport["height"]
        
        target_x, target_y = w // 2, h // 2
        try:
            visible_elements = page.locator("p, span, h1, h2, h3, a, li").all()
            if visible_elements:
                for _ in range(5):
                    el = random.choice(visible_elements)
                    box = el.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        bx = int(box["x"] + box["width"] / 2)
                        by = int(box["y"] + box["height"] / 2)
                        if 0 < bx < w and 0 < by < h:
                            target_x, target_y = bx, by
                            break
        except Exception:
            pass

        try:
            page.mouse.move(target_x, target_y, steps=5)
        except Exception:
            pass

        while time.time() - start_time < delay:
            slice_time = _rand(0.2, 0.5)
            time.sleep(slice_time)
            jx = target_x + random.randint(-2, 2)
            jy = target_y + random.randint(-2, 2)
            jx = max(0, min(jx, w - 1))
            jy = max(0, min(jy, h - 1))
            try:
                page.mouse.move(jx, jy)
            except Exception:
                pass

        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
    else:
        time.sleep(delay)


def human_type(page: Page, locator: Locator, text: str) -> None:
    """Type text through the keyboard API with human-realistic cadence.

    Enhancements over Outreach Manager:
    - Types in **bursts** (3–8 chars) separated by micro-pauses, then
      longer inter-burst pauses — humans don't maintain perfectly uniform
      keystroke intervals.
    - 3.5% chance of an adjacent-key typo followed by Backspace.
    - Random initial focus pause.

    Args:
        page:    Playwright Page (used for keyboard API).
        locator: The input field Locator to type into.
        text:    The text to type.
    """
    locator.focus()
    time.sleep(_rand(0.15, 0.4))

    i = 0
    while i < len(text):
        burst = random.randint(*_BURST_CHARS)
        for _ in range(burst):
            if i >= len(text):
                break
            char = text[i]
            char_lower = char.lower()

            # Possibly inject a typo
            if _TYPO_RATE > random.random() and char_lower in _ADJACENT:
                typo = random.choice(_ADJACENT[char_lower])
                page.keyboard.type(typo)
                time.sleep(_rand(0.08, 0.18))
                page.keyboard.press("Backspace")
                time.sleep(_rand(0.06, 0.14))

            page.keyboard.type(char, delay=random.randint(_TYPE_MIN_MS, _TYPE_MAX_MS))
            i += 1

        # Pause between bursts — longer and variable
        time.sleep(_rand(*_BURST_PAUSE))

    time.sleep(_rand(0.15, 0.35))


def human_scroll(page: Page, target_px: int) -> None:
    """Scroll the page by *target_px* pixels in a human-like fashion.

    Enhancements over Outreach Manager:
    - Uses a sigmoid acceleration curve so scrolling starts slow, speeds up,
      then slows near the target (ease-in-out).
    - 8% chance of a micro-reversal (correction).
    - Random inter-step sleep with occasional longer pauses.

    Args:
        page:       Playwright Page.
        target_px:  Number of pixels to scroll (positive = down).
    """
    scrolled = 0
    direction = 1 if target_px >= 0 else -1
    target_abs = abs(target_px)

    while scrolled < target_abs:
        progress = scrolled / max(target_abs, 1)
        # Sigmoid ease-in-out step size
        ease = 1 / (1 + math.exp(-10 * (progress - 0.5)))
        step = max(10, int((30 + ease * 60) + _rand(-10, 10)))
        step = min(step, target_abs - scrolled)

        try:
            page.evaluate(f"window.scrollBy(0, {direction * step})")
        except Exception:
            break

        scrolled += step

        # 8% chance of micro-reversal
        if random.random() < 0.08 and scrolled > 80:
            back = int(_rand(8, 18))
            try:
                page.evaluate(f"window.scrollBy(0, {-direction * back})")
            except Exception:
                pass
            scrolled = max(0, scrolled - back)
            time.sleep(_rand(0.08, 0.2))

        # Occasional longer pause (reading)
        if random.random() < 0.06:
            time.sleep(_rand(0.4, 1.2))
        else:
            time.sleep(_rand(0.025, 0.07))


def human_hover(page: Page, locator: Locator) -> None:
    """Move mouse to element along a curved Bezier path and hover briefly.

    Uses Playwright's native mouse API (not OS-level) for the motion path,
    which is sufficient to fool most behavioural analytics. For the final
    click, :func:`human_click` adds OS-level confirmation.

    Args:
        page:    Playwright Page.
        locator: Element to hover over.
    """
    tx, ty = _get_element_center(locator)

    # Prevent teleportation by starting from the last known coordinates or viewport edge
    sx, sy = _get_start_mouse_pos(page)

    path = _bezier_path(sx, sy, tx, ty, steps=random.randint(18, 32))
    for px, py in path:
        page.mouse.move(px, py)
        time.sleep(_rand(0.005, 0.018))

    _LAST_VIRTUAL_MOUSE[id(page)] = (tx, ty)

    # Hover dwell
    time.sleep(_rand(*_HOVER_DWELL))


def get_os_click_coordinates(
    page: Page, locator: Locator
) -> tuple[tuple[int, int], tuple[int, int, int, int]] | None:
    """Calculate exact physical screen coordinates and safe webpage canvas boundary box.

    Returns ((phys_x, phys_y), (min_safe_x, max_safe_x, min_safe_y, max_safe_y)) if element
    is safely inside the webpage canvas, or None if it touches Chrome toolbar/borders or is offscreen.
    """
    try:
        box = locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None

        # Viewport-relative element center (CSS pixels)
        tx = box["x"] + box["width"] / 2
        ty = box["y"] + box["height"] / 2

        metrics = page.evaluate("""() => {
            const dpr = window.devicePixelRatio || 1.0;
            const topBarHeight = Math.max(window.outerHeight - window.innerHeight, 70);
            const sideBorder = Math.max((window.outerWidth - window.innerWidth) / 2, 0);

            return {
                dpr: dpr,
                screenX: window.screenX,
                screenY: window.screenY,
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
                topBarHeight: topBarHeight,
                sideBorder: sideBorder,
            };
        }""")

        dpr = metrics["dpr"]
        innerWidth = metrics["innerWidth"]
        innerHeight = metrics["innerHeight"]
        screenX = metrics["screenX"]
        screenY = metrics["screenY"]
        topBarHeight = metrics["topBarHeight"]
        sideBorder = metrics["sideBorder"]

        # 1. Element center must be strictly inside the webpage viewport bounds (with 15px margin)
        if not (15 <= tx <= innerWidth - 15 and 15 <= ty <= innerHeight - 15):
            logger.debug("OS Click skipped: element center (%.1f, %.1f) outside viewport margin", tx, ty)
            return None

        # 2. Compute physical webpage canvas boundaries on screen
        min_safe_phys_x = int(round((screenX + sideBorder + 15) * dpr))
        max_safe_phys_x = int(round((screenX + sideBorder + innerWidth - 15) * dpr))
        min_safe_phys_y = int(round((screenY + topBarHeight + 15) * dpr))
        max_safe_phys_y = int(round((screenY + topBarHeight + innerHeight - 15) * dpr))

        # 3. Calculate target physical screen coordinates
        css_screen_x = screenX + sideBorder + tx
        css_screen_y = screenY + topBarHeight + ty

        phys_x = int(round(css_screen_x * dpr))
        phys_y = int(round(css_screen_y * dpr))

        # 4. Strict canvas boundary check
        if not (min_safe_phys_x <= phys_x <= max_safe_phys_x and min_safe_phys_y <= phys_y <= max_safe_phys_y):
            logger.warning(
                "OS Click skipped: physical screen pos (%d, %d) outside safe webpage canvas [%d..%d, %d..%d]",
                phys_x, phys_y, min_safe_phys_x, max_safe_phys_x, min_safe_phys_y, max_safe_phys_y
            )
            return None

        bounds = (min_safe_phys_x, max_safe_phys_x, min_safe_phys_y, max_safe_phys_y)
        return (phys_x, phys_y), bounds
    except Exception as exc:
        logger.debug("get_os_click_coordinates error: %s", exc)
        return None


def human_click(page: Page, locator: Locator, use_os_mouse: bool = True) -> None:
    """Click an element with human-like mouse movement and physical OS-level click when safe.

    Sequence:
      1. Hover over the element along a Bezier-curve path inside the viewport.
      2. Micro-overshoot and correct within the viewport.
      3. If use_os_mouse=True and Chrome is in foreground, compute strict physical
         canvas boundaries and perform OS-level click via Win32 API with path clamping.
      4. If OS click is out of bounds or fails, fall back safely to Playwright's locator.click().
    """
    human_hover(page, locator)

    tx, ty = _get_element_center(locator)

    # Micro-overshoot then correct
    overshoot_x = tx + _rand(*_OVERSHOOT) * random.choice([-1, 1])
    overshoot_y = ty + _rand(*_OVERSHOOT) * random.choice([-1, 1])
    page.mouse.move(overshoot_x, overshoot_y)
    time.sleep(_rand(0.02, 0.06))
    page.mouse.move(tx, ty)
    time.sleep(_rand(0.03, 0.08))

    _LAST_VIRTUAL_MOUSE[id(page)] = (tx, ty)

    if use_os_mouse and is_chrome_in_foreground(page):
        res = get_os_click_coordinates(page, locator)
        if res is not None:
            (phys_x, phys_y), bounds = res
            try:
                _os_mouse_move_and_click(phys_x, phys_y, bounds=bounds)
                return
            except Exception as exc:
                logger.debug("OS physical mouse click failed, falling back to playwright: %s", exc)

    locator.click()


def read_page_naturally(page: Page, sections: int = 3) -> None:
    """Simulate a user scanning a page before interacting with it.

    Performs a series of partial scrolls, random pauses, and element
    hovers to create a human-like interaction signature before automation
    begins performing targeted actions.

    Args:
        page:     Playwright Page.
        sections: Number of page sections to scroll through.
    """
    for _ in range(sections):
        # Scroll a random portion of the viewport height
        scroll_amount = int(_rand(0.3, 0.8) * 700)
        human_scroll(page, scroll_amount)
        time.sleep(_rand(0.6, 2.0))

        # Occasionally hover over a random text element
        if random.random() < 0.4:
            try:
                elements = page.locator("p, span, li, h2, h3").all()
                if elements:
                    el = random.choice(elements[:12])  # top 12 only
                    if el.is_visible():
                        human_hover(page, el)
            except Exception:
                pass

    time.sleep(_rand(0.5, 1.5))
