# openoutreach/linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time
from functools import cached_property

from outreach_manager.core.conf import MIN_DELAY, MAX_DELAY
from outreach_manager.linkedin.browser.exceptions import BrowserRecoveryFailed

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


class AccountSession:
    def __init__(self, linkedin_profile):
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

        # Counters for session summary accounting
        self.browser_recoveries: int = 0
        self.diagnostics_generated: int = 0

        # Guard: prevents re-entrant recovery loops.
        # Set to True for the duration of a recovery attempt.
        self._recovery_in_progress: bool = False
        # True after a recovery was already attempted this session.
        # A second failure in the same workflow escalates to BrowserRecoveryFailed.
        self._recovery_attempted: bool = False

    @cached_property
    def campaigns(self):
        """All campaigns this user belongs to (cached)."""
        from outreach_manager.core.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user))

    def is_browser_healthy(self) -> bool:
        """Return True only when ALL Playwright objects are live.

        A partial-closed state (e.g. page open but context closed) is treated
        as unhealthy — any subsequent Playwright call would raise
        "Target page, context or browser has been closed".
        """
        try:
            if not self.page or self.page.is_closed():
                return False
            if not self.context or self.context.is_closed():
                return False
            # browser.is_connected() available in Playwright >=1.14
            if self.browser and not self.browser.is_connected():
                return False
            return True
        except Exception:
            # Any Playwright internal error means the session is dead.
            return False

    def ensure_browser(self) -> None:
        """Guarantee a healthy Playwright page before any browser operation.

        Call this as the first line of any method that touches ``session.page``.

        Recovery flow:
          1. Check all Playwright objects via ``is_browser_healthy()``.
          2. If healthy, optionally refresh cookies and return.
          3. If unhealthy and not already recovering:
             a. Dispose all stale objects via ``close()``.
             b. Call ``start_browser_session()`` to rebuild from scratch.
             c. On success: clear recovery flags and continue.
             d. On failure: raise ``BrowserRecoveryFailed`` so the
                workflow-level handler in ``run_session()`` can record
                the error and skip to the next workflow.
          4. If recovery is already in progress (re-entrant call), raise
             ``BrowserRecoveryFailed`` immediately to break the loop.
        """
        from outreach_manager.linkedin.browser.launch import start_browser_session

        if self.is_browser_healthy():
            self._maybe_refresh_cookies()
            return

        # --- Browser is dead or partially closed ---

        if self._recovery_in_progress:
            # Re-entrant: start_browser_session() itself called ensure_browser().
            # Do NOT recurse — surface the failure immediately.
            raise BrowserRecoveryFailed(
                f"Re-entrant browser recovery detected for {self}. "
                "Aborting to prevent an infinite restart loop."
            )

        if self._recovery_attempted:
            # A second failure in the same workflow call-chain.
            # Mark as definitely broken; caller should skip remaining work.
            raise BrowserRecoveryFailed(
                f"Browser recovery already failed once for {self}. "
                "Not retrying — skipping remainder of workflow."
            )

        is_initial_launch = (self.page is None and self.browser is None)
        if is_initial_launch:
            logger.info("[INFO] Initializing Chrome browser session for %s...", self)
        else:
            logger.warning(
                "[WARN] Browser session disconnected.\n  Account: %s\n  Initiating recovery...",
                self,
            )
            self._recovery_attempted = True

        self._recovery_in_progress = True
        try:
            # Step 1: tear down all stale Playwright objects completely.
            self.close()
            # Step 2: full rebuild — launch browser, create context, restore auth.
            start_browser_session(session=self)
            # Step 3: confirm the rebuilt session is healthy.
            if not self.is_browser_healthy():
                raise RuntimeError("post-launch health check failed")
            if not is_initial_launch:
                self.browser_recoveries += 1
                logger.info("[INFO] Browser recovered successfully for %s.", self)
            else:
                logger.info("[INFO] Chrome browser session established for %s.", self)
        except Exception as exc:

            logger.warning(
                "[WARN] Browser recovery failed.\n  Account: %s\n  Error: %s\n  Workflow skipped.",
                self, exc,
            )
            # Ensure all objects are cleaned up regardless.
            try:
                self.close()
            except Exception:
                pass
            raise BrowserRecoveryFailed(
                f"Browser recovery failed for {self}: {exc}"
            ) from exc
        finally:
            self._recovery_in_progress = False

    def reset_recovery_state(self) -> None:
        """Call once per workflow entry to allow a fresh recovery attempt.

        The Session Executor calls this before each workflow so that a
        browser failure in workflow A does not permanently prevent recovery
        in workflow B (which starts with a fresh context).
        """
        self._recovery_attempted = False
        self._recovery_in_progress = False

    @cached_property
    def self_profile(self) -> dict:
        """Authenticated user's profile dict, fetched once per session.

        The dict isn't persisted to DB (we dropped ``Lead.profile_data``),
        so the first access per session triggers a Voyager call via the
        ``linkedin_cli`` self-discovery primitive; the ``cached_property``
        keeps it warm for the rest of the session. CRM-side persistence
        (the disqualified ``self_lead``) is layered on in ``register_self_lead``.
        """
        from linkedin_cli.setup.self_profile import discover_self_profile
        from outreach_manager.linkedin.db.leads import register_self_lead

        profile = discover_self_profile(self)
        register_self_lead(self, profile)
        return profile

    @cached_property
    def active_timezone(self) -> str | None:
        """IANA zone for the active-hours window, resolved once per session.

        An explicit ``ACTIVE_TIMEZONE`` in conf wins (operator override);
        otherwise the zone is inferred from the LinkedIn profile country.
        None when neither yields a zone — the scheduler/daemon treat None as
        "no active-hours gating" rather than guessing UTC. Resolving via
        ``self_profile`` means this fires only after login.
        """
        from outreach_manager.core.conf import ACTIVE_TIMEZONE
        from outreach_manager.core.tz_country import timezone_for_country

        if ACTIVE_TIMEZONE:
            return ACTIVE_TIMEZONE
        return timezone_for_country(self.self_profile.get("country_code"))

    def active_timezone_provenance(self) -> str:
        """Human-readable note on where ``active_timezone`` came from — used in
        the daemon's active-hours log so an inferred (and possibly wrong) zone
        is visible and overridable."""
        from outreach_manager.core.conf import ACTIVE_TIMEZONE

        if ACTIVE_TIMEZONE:
            return f"{ACTIVE_TIMEZONE} (configured via ACTIVE_TIMEZONE)"
        tz = self.active_timezone
        country = (self.self_profile.get("country_code") or "?").upper()
        if tz:
            return (
                f"{tz} (inferred from LinkedIn profile country {country}; "
                "override with ACTIVE_TIMEZONE)"
            )
        return "unknown (no profile country and no ACTIVE_TIMEZONE) — not gating"

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("domcontentloaded")

    def reauthenticate(self):
        """Force a fresh login: close browser, clear saved cookies, re-launch."""
        from outreach_manager.linkedin.browser.launch import start_browser_session

        logger.warning("Re-authenticating %s — clearing saved session", self)
        self.close()
        self.linkedin_profile.cookie_data = None
        self.linkedin_profile.save(update_fields=["cookie_data"])
        start_browser_session(session=self)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired."""
        from outreach_manager.linkedin.browser.launch import start_browser_session

        self.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        cookie_data = self.linkedin_profile.cookie_data
        if not cookie_data:
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    self.close()
                    start_browser_session(session=self)
                return

    def close(self):
        """Release all Playwright objects and close browser resources.

        Guarantees clean release of page, context, browser, and playwright instance.
        Suppresses stack traces on expected cleanup failures.
        """
        has_resources = any(
            obj is not None
            for obj in (self.page, self.context, self.browser, self.playwright)
        )
        if not has_resources:
            logger.debug("AccountSession close called; no active browser resources for %s", self)
            return

        logger.info("[INFO] Closing browser session...")
        cleanup_error = False

        try:
            import os
            use_cdp = os.environ.get("USE_CDP", "False").lower() in ("true", "1", "yes")

            if self.page:
                try:
                    self.page.close()
                except Exception as e:
                    logger.debug("Error closing page: %s", e)
                    cleanup_error = True

            if not use_cdp:
                if self.context:
                    try:
                        self.context.close()
                    except Exception as e:
                        logger.debug("Error closing context: %s", e)
                        cleanup_error = True

                if self.browser:
                    try:
                        self.browser.close()
                    except Exception as e:
                        logger.debug("Error closing browser: %s", e)
                        cleanup_error = True

            if self.playwright:
                try:
                    if hasattr(self.playwright, "stop"):
                        self.playwright.stop()
                except Exception as e:
                    logger.debug("Error stopping playwright: %s", e)
                    cleanup_error = True

            if cleanup_error:
                logger.warning("[WARN] Browser cleanup encountered an error. Resources released where possible.")
            else:
                logger.info("[INFO] Browser closed successfully.")
        except Exception as e:
            logger.warning("[WARN] Browser cleanup encountered an error. Resources released where possible.")
            logger.debug("Unexpected error during browser cleanup: %s", e)
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return self.linkedin_profile.linkedin_username
