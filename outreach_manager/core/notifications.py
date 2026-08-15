# openoutreach/core/notifications.py
"""Session completion notification service for OpenOutreach.

Provides lightweight notifications (native Windows toast via winotify with console
fallback) when an outreach session completes, without changing execution architecture,
scheduling, or browser automation.

Native Windows notifications use winotify — pure Python, no shell execution.
If winotify is unavailable or raises, the notifier falls back to a formatted
console notification block silently without interrupting shutdown.

Future extensibility: _show_windows_notification() is intentionally separated so
notification actions (e.g. "Open Dashboard") can be attached to the Notification
object without redesigning the public API.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from outreach_manager.core.config import get_config

if TYPE_CHECKING:
    from outreach_manager.core.session_executor import SessionSummary

logger = logging.getLogger(__name__)


# ── Notification Levels ─────────────────────────────────────────────

class NotificationLevel:
    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"
    INFO    = "info"


# ── Notification Data Class ─────────────────────────────────────────

@dataclass
class Notification:
    """Structured notification payload.

    Keeps notification data separate from delivery so future interactive
    actions (e.g. "Open Dashboard") can be attached without redesigning
    the public SessionNotifier API.
    """
    level: str
    title: str
    body: str
    actions: list[dict] = field(default_factory=list)  # reserved for future use


# ── Helpers ─────────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _build_notification(summary: "SessionSummary") -> Notification:
    """Derive notification level and content from SessionSummary."""
    dur_str = _format_duration(summary.duration_seconds)

    if summary.fatal_errors > 0 or (summary.workflow_errors > 0 and summary.actions_performed == 0):
        return Notification(
            level=NotificationLevel.FAILURE,
            title="✖ Session Failed",
            body=(
                f"Duration: {dur_str}\n"
                f"Errors: {summary.total_errors}\n"
                "See Session History for details."
            ),
        )

    if summary.total_errors > 0 or summary.browser_recoveries > 0 or getattr(summary, "fallback_used", False):
        return Notification(
            level=NotificationLevel.WARNING,
            title="⚠ Session Completed with Issues",
            body=(
                f"Duration: {dur_str}\n"
                f"Actions Completed: {summary.actions_performed}\n"
                f"Errors: {summary.total_errors}\n"
                "See Session History for details."
            ),
        )

    if summary.actions_performed == 0:
        return Notification(
            level=NotificationLevel.INFO,
            title="ℹ Session Finished",
            body=f"No eligible outreach work was available.\nDuration: {dur_str}",
        )

    # Success — extended content including primary provider
    wf_str = ", ".join(summary.workflows_executed) if summary.workflows_executed else "None"
    provider_str = getattr(summary, "primary_provider", "") or "N/A"
    return Notification(
        level=NotificationLevel.SUCCESS,
        title="✔ Outreach Session Complete",
        body=(
            f"Duration: {dur_str}\n"
            f"Actions Completed: {summary.actions_performed}\n"
            f"Workflows Executed: {wf_str}\n"
            f"Primary Provider: {provider_str}\n"
            "Errors: 0"
        ),
    )


# ── Notifier ────────────────────────────────────────────────────────

class SessionNotifier:
    """Delivers lightweight session completion notifications.

    Public API:
        SessionNotifier.notify_session_complete(summary)

    The Session Executor remains unaware of notification internals.
    Notification failures never interrupt outreach execution or shutdown.
    """

    @classmethod
    def notify_session_complete(cls, summary: "SessionSummary") -> None:
        """Display session completion notification if enabled in configuration."""
        try:
            diag_cfg = get_config().diagnostics

            # Master switch check
            if not getattr(diag_cfg, "notifications_enabled", True) or getattr(diag_cfg, "notification_delivery_mode", "toast") == "disabled":
                logger.info("[INFO] Notifications disabled by configuration.")
                return

            notification = _build_notification(summary)

            # Granular category filtering
            lvl = notification.level
            if lvl == NotificationLevel.SUCCESS and not getattr(diag_cfg, "notify_on_success", True):
                logger.info("[INFO] Notification skipped (success notifications disabled).")
                return
            elif lvl == NotificationLevel.WARNING and not getattr(diag_cfg, "notify_on_warning", True):
                logger.info("[INFO] Notification skipped (warning notifications disabled).")
                return
            elif lvl == NotificationLevel.FAILURE and not getattr(diag_cfg, "notify_on_failure", True):
                logger.info("[INFO] Notification skipped (failure notifications disabled).")
                return
            elif lvl == NotificationLevel.INFO and not getattr(diag_cfg, "notify_on_info", False):
                logger.info("[INFO] Notification skipped (idle/no-work notifications disabled).")
                return

            # Delivery Mode check
            delivery_mode = getattr(diag_cfg, "notification_delivery_mode", "toast").lower()
            if delivery_mode == "console_only":
                cls._display_console_notification(notification)
                return

            delivered = False
            if sys.platform == "win32":
                delivered = cls._show_windows_notification(notification)

            if not delivered:
                cls._display_console_notification(notification)

        except Exception as exc:
            # Notification failures must never affect outreach execution or shutdown
            logger.debug("[SessionNotifier] Exception during notification: %s", exc)

    @classmethod
    def _show_windows_notification(cls, notification: Notification) -> bool:
        """Deliver a native Windows toast notification via winotify (pure Python).

        Returns True on success, False if unavailable or exception.
        Structured so future notification actions (e.g. Open Dashboard) can be
        attached to the winotify Notification object without changing the API.
        """
        try:
            from winotify import Notification as WinNotification, audio  # type: ignore

            toast = WinNotification(
                app_id="Outreach Manager",
                title=notification.title,
                msg=notification.body,
                duration="short",
            )
            toast.set_audio(audio.Default, loop=False)

            # Future: attach actions here before show()
            # e.g. toast.add_actions(label="Open Dashboard", launch="openoutreach://dashboard")

            toast.show()
            logger.info("[INFO] Native Windows notification displayed.")
            return True

        except Exception as exc:
            logger.debug("[Notification] Windows notification unavailable. Falling back to console. (%s)", exc)
            return False

    @classmethod
    def _display_console_notification(cls, notification: Notification) -> None:
        """Display notification as a formatted console block (fallback)."""
        try:
            from termcolor import colored
            color_map = {
                NotificationLevel.SUCCESS: "green",
                NotificationLevel.WARNING: "yellow",
                NotificationLevel.FAILURE: "red",
                NotificationLevel.INFO:    "cyan",
            }
            theme = color_map.get(notification.level, "cyan")
            box = f"=================== [ {notification.title} ] ==================="
            lines = [colored(box, theme, attrs=["bold"])]
            for line in notification.body.splitlines():
                lines.append(f"  {line}")
            lines.append(colored("=" * len(box), theme, attrs=["bold"]))
            print("\n" + "\n".join(lines) + "\n", flush=True)
        except Exception:
            print(f"\n*** {notification.title} ***\n{notification.body}\n", flush=True)
        logger.info("[INFO] Console notification displayed.")
