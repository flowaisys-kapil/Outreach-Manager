# openoutreach/core/windows_scheduler.py
"""Windows Task Scheduler Manager for OpenOutreach.

Provides self-healing scheduled task management on Windows platforms.
Registers or updates a single named scheduled task (default: 'OpenOutreachManager')
to trigger the next automatic execution at a calculated local start time.
"""
from __future__ import annotations

import datetime
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TASK_NAME = "OpenOutreachManager"


def get_task_command() -> str:
    """Build the executable command line for the scheduled task."""
    root_dir = Path(__file__).parent.parent.parent
    manage_py = root_dir / "manage.py"
    python_exe = sys.executable

    return f'"{python_exe}" "{manage_py}" rundaemon --mode automatic --exit-on-empty'


def task_exists(task_name: str = DEFAULT_TASK_NAME) -> bool:
    """Return True if the named scheduled task exists in Windows Task Scheduler."""
    if not sys.platform.startswith("win"):
        return False

    cmd = ["schtasks", "/Query", "/TN", task_name]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0
    except Exception as exc:
        logger.debug("Failed to query schtasks: %s", exc)
        return False


def update_windows_scheduled_task(
    next_run_time: datetime.datetime,
    task_name: str = DEFAULT_TASK_NAME,
) -> bool:
    """Create or update the single named Windows scheduled task for next_run_time.

    Self-healing: Creates task if missing; updates trigger if existing.
    Uses local system time and configures task to run as soon as possible if missed.
    """
    cmd_str = get_task_command()
    local_dt_str = next_run_time.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info(
        "[SCHEDULER] Setting Windows Task '%s' for next run at %s",
        task_name, local_dt_str,
    )

    if not sys.platform.startswith("win"):
        logger.info("[SCHEDULER] Non-Windows OS detected (%s) — skipping Task Scheduler update.", sys.platform)
        return True

    # Use PowerShell Register-ScheduledTask / Set-ScheduledTask for robust ISO datetime & StartWhenAvailable support
    ps_script = f"""
$taskName = "{task_name}"
$action = New-ScheduledTaskAction -Execute "{sys.executable}" -Argument '"{Path(__file__).parent.parent.parent / "manage.py"}" rundaemon --mode automatic --exit-on-empty'
$trigger = New-ScheduledTaskTrigger -Once -At "{local_dt_str}"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {{
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -ErrorAction Stop | Out-Null
"""

    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, check=False,
        )
        if res.returncode == 0:
            logger.info("[SCHEDULER] Successfully updated Windows Task '%s' to %s", task_name, local_dt_str)
            return True
        else:
            logger.warning("[SCHEDULER] PowerShell scheduled task update notice: %s", res.stderr.strip())
            # Fallback to schtasks CLI if PowerShell execution policy restricts script
            return _fallback_schtasks(next_run_time, task_name)
    except Exception as exc:
        logger.warning("[SCHEDULER] Failed to update Windows scheduled task: %s", exc)
        return False


def _fallback_schtasks(next_run_time: datetime.datetime, task_name: str) -> bool:
    """Fallback task creation using schtasks.exe CLI."""
    date_str = next_run_time.strftime("%m/%d/%Y")
    time_str = next_run_time.strftime("%H:%M:%S")
    cmd_str = get_task_command()

    cmd = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", cmd_str,
        "/SC", "ONCE",
        "/SD", date_str,
        "/ST", time_str,
        "/F",  # Overwrite existing
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            logger.info("[SCHEDULER] schtasks fallback successfully updated '%s' to %s %s", task_name, date_str, time_str)
            return True
        else:
            logger.error("[SCHEDULER] schtasks fallback failed: %s", res.stderr)
            return False
    except Exception as exc:
        logger.error("[SCHEDULER] schtasks fallback exception: %s", exc)
        return False


def remove_windows_scheduled_task(task_name: str = DEFAULT_TASK_NAME) -> bool:
    """Unregister/delete the named Windows scheduled task if it exists.

    Self-healing: Removes existing task when operating in Manual mode.
    Returns True if task was deleted or did not exist.
    """
    if not sys.platform.startswith("win"):
        logger.info("[SCHEDULER] Non-Windows OS detected (%s) — skipping Task Scheduler removal.", sys.platform)
        return True

    logger.info("[SCHEDULER] Removing Windows Scheduled Task '%s' if present...", task_name)

    ps_script = f"""
$taskName = "{task_name}"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {{
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
}}
"""

    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, check=False,
        )
        if res.returncode == 0:
            logger.info("[SCHEDULER] Successfully removed Windows Scheduled Task '%s'", task_name)
            return True
        else:
            logger.warning("[SCHEDULER] PowerShell scheduled task removal notice: %s", res.stderr.strip())
            return _fallback_delete_schtasks(task_name)
    except Exception as exc:
        logger.warning("[SCHEDULER] Failed to remove Windows scheduled task via PowerShell: %s", exc)
        return _fallback_delete_schtasks(task_name)


def _fallback_delete_schtasks(task_name: str) -> bool:
    """Fallback task deletion using schtasks.exe CLI."""
    cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 or "ERROR: The specified task name" in res.stderr or "The specified task name" in res.stdout:
            logger.info("[SCHEDULER] schtasks fallback successfully deleted task '%s'", task_name)
            return True
        else:
            logger.error("[SCHEDULER] schtasks delete fallback failed: %s", res.stderr)
            return False
    except Exception as exc:
        logger.error("[SCHEDULER] schtasks delete fallback exception: %s", exc)
        return False
