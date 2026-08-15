# openoutreach/core/logging.py
"""Centralized logging configuration with colored output and startup banner."""
from __future__ import annotations

import logging
import os
import sys

from termcolor import colored

# ── Banner ──────────────────────────────────────────────────────────

BANNER = r"""
  ___  _   _ _____ ____  _____    _    ____ _   _ 
 / _ \| | | |_   _|  _ \| ____|  / \  / ___| | | |
| | | | | | | | | | |_) |  _|   / _ \| |   | |_| |
| |_| | |_| | | | |  _ <| |___ / ___ \ |___|  _  |
 \___/ \___/  |_| |_| \_\_____/_/   \_\____|_| |_|

 __  __    _    _   _    _    ____ _____ ____  
|  \/  |  / \  | \ | |  / \  / ___| ____|  _ \ 
| |\/| | / _ \ |  \| | / _ \| |  _|  _| | |_) |
| |  | |/ ___ \| |\  |/ ___ \ |_| | |___|  _ < 
|_|  |_/_/   \_\_| \_/_/   \_\____|_____|_| \_\
"""


def print_banner():
    """Print the Outreach Manager startup banner in bold cyan."""
    sys.stdout.write(colored(BANNER, "cyan", attrs=["bold"]))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ── Colored formatter ───────────────────────────────────────────────

_LEVEL_COLORS = {
    logging.DEBUG: ("dark_grey", []),
    logging.INFO: (None, []),
    logging.WARNING: ("yellow", ["bold"]),
    logging.ERROR: ("red", ["bold"]),
    logging.CRITICAL: ("red", ["bold", "underline"]),
}

_LEVEL_LABELS = {
    logging.DEBUG: "DBG",
    logging.INFO: "INF",
    logging.WARNING: "WRN",
    logging.ERROR: "ERR",
    logging.CRITICAL: "CRT",
}


class ColoredFormatter(logging.Formatter):
    """Compact colored formatter: ``[LVL] message``."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        color, attrs = _LEVEL_COLORS.get(record.levelno, (None, []))
        label = _LEVEL_LABELS.get(record.levelno, "???")
        prefix = colored(f"[{label}]", color, attrs=attrs) if color else f"[{label}]"
        return f"{prefix} {msg}"


# ── Brand palette (third-party services) ────────────────────────────
# 24-bit accent colours lifted from each vendor's own site, so a service
# name prints in its real palette colour. termcolor only knows the 16
# named colours, so these go out as raw truecolor SGR escapes.

_BRANDS = {
    "bettercontact": ("BetterContact", (155, 81, 224)),  # bettercontact.rocks #9b51e0
    "icemail": ("IceMail", (34, 197, 94)),               # icemail.ai --brand #22c55e
}


def _color_enabled() -> bool:
    """Mirror termcolor's gating: NO_COLOR off, FORCE_COLOR on, else TTY-only, checked via get_config()."""
    from outreach_manager.core.config import get_config
    if not get_config().diagnostics.color_enabled:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def brand(service: str, text: str | None = None) -> str:
    """Render a service name (or `text`) in that vendor's brand colour."""
    label, (r, g, b) = _BRANDS[service]
    label = text if text is not None else label
    if not _color_enabled():
        return label
    return f"\033[38;2;{r};{g};{b}m{label}\033[0m"


# ── Public API ──────────────────────────────────────────────────────

SILENCED_LOGGERS = (
    "urllib3", "httpx", "pydantic_ai", "openai", "playwright",
    "httpcore", "fastembed", "huggingface_hub", "filelock", "asyncio",
)


def configure_logging(level: int = logging.DEBUG):
    """Configure root logger with colored output and silence noisy libraries."""
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter("%(message)s"))
    handler.setLevel(level)

    root.addHandler(handler)
    root.setLevel(level)

    for name in SILENCED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
