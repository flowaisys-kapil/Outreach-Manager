# outreach_manager/linkedin/browser/launch.py
"""Persist + orchestrate the daemon's LinkedIn browser session.

Cookie persistence (to the Django DB) and the launch/login orchestration are
Outreach Manager concerns, so they live here. The reusable *mechanics* — launching a
stealthed browser, driving the login form, clearing checkpoints — stay in the
Django-free ``linkedin_cli.browser`` library and are called from here.

Stealth strategy (CDP mode):
  - ``apply_full_stealth(page, context)`` injects 13 anti-detection JS scripts
    into every page before any JS runs (via ``add_init_script``).
  - ``context.on('page', apply_stealth_to_new_page)`` ensures every new tab
    opened by LinkedIn (pop-ups, modals, etc.) also gets the full stealth suite.
  - Extra HTTP headers (User-Agent, sec-ch-ua) are set on the context so all
    outgoing requests appear as a real Chrome 125 on Windows 10.
"""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin_cli.auth import authenticate
from linkedin_cli.browser.login import dismiss_comply_gate, launch_browser
from linkedin_cli.browser.nav import goto_page
from outreach_manager.linkedin.browser.stealth_profile import (
    apply_full_stealth,
    apply_stealth_to_new_page,
)

logger = logging.getLogger(__name__)

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB."""
    state = session.context.storage_state()
    session.linkedin_profile.cookie_data = state
    session.linkedin_profile.save(update_fields=["cookie_data"])


def purge_chrome_cache():
    """Purge expired temporary session cookies and DOM cached files from chrome_profile directory."""
    import os
    import shutil
    from django.conf import settings

    profile_dir = os.path.join(settings.BASE_DIR, "data", "chrome_profile")
    if not os.path.exists(profile_dir):
        return

    logger.info("Purging Chrome temporary cache and session files inside %s...", profile_dir)
    
    # Safe subdirectories to clear without deleting credentials/persistent cookies
    targets = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/Service Worker/CacheStorage",
        "Default/Service Worker/ScriptCache",
        "Default/Storage/ext",
        "ShaderCache",
        "GrShaderCache",
    ]

    cleared_dirs = 0
    cleared_files = 0

    for target in targets:
        path = os.path.join(profile_dir, target)
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                cleared_dirs += 1
            except Exception as e:
                logger.debug("Failed to delete cache path %s: %s", path, e)

    # Delete any temporary files (.tmp or similar temporary patterns)
    for root, dirs, files in os.walk(profile_dir):
        for file in files:
            if file.endswith(".tmp") or "tmp" in file.lower() or file.startswith("temp"):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    cleared_files += 1
                except Exception:
                    pass

    logger.info("Purge completed: cleaned up %d directories and %d temp files.", cleared_dirs, cleared_files)


def start_browser_session(session):
    import os
    from playwright.sync_api import sync_playwright
    
    try:
        purge_chrome_cache()
    except Exception as e:
        logger.warning("Cache purge failed: %s", e)

    logger.debug("Configuring browser for %s", session)

    use_cdp = os.environ.get("USE_CDP", "False").lower() in ("true", "1", "yes")
    cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222")

    if use_cdp:
        logger.info("Connecting to active browser via CDP at %s", cdp_url)
        session.playwright = sync_playwright().start()
        try:
            session.browser = session.playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            logger.info("CDP connection failed. Attempting to automatically launch Chrome in remote-debugging mode...")
            import subprocess
            import time
            from django.conf import settings
            from outreach_manager.linkedin.browser.stealth_profile import get_chrome_launch_args

            def find_chrome_path():
                paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                    "chrome.exe"
                ]
                for p in paths:
                    if os.path.exists(p):
                        return p
                return "chrome.exe"

            import urllib.parse
            try:
                parsed = urllib.parse.urlparse(cdp_url)
                debug_port = parsed.port or 9222
            except Exception:
                debug_port = 9222

            chrome_path = find_chrome_path()
            profile_dir = os.path.join(settings.BASE_DIR, "data", "chrome_profile")
            os.makedirs(profile_dir, exist_ok=True)
            args = get_chrome_launch_args(debug_port=debug_port, profile_dir=profile_dir)
            cmd = [chrome_path] + args
            
            try:
                # Launch Chrome as a detached background process
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(cmd, creationflags=DETACHED_PROCESS, close_fds=True)
                logger.info("Spawning Chrome in background: %s", " ".join(cmd))
                # Wait for the WebSocket debugging server to initialize
                time.sleep(4)
                
                # Retry connection
                session.browser = session.playwright.chromium.connect_over_cdp(cdp_url)
                logger.info("Successfully connected to the auto-spawned Chrome instance!")
            except Exception as retry_err:
                logger.error(colored(
                    f"Auto-spawn failed or failed to connect on retry: {retry_err}",
                    "red", attrs=["bold"]
                ))
                logger.error(colored(
                    f"Failed to connect to browser over CDP at {cdp_url}. "
                    "Please ensure Google Chrome is running with remote debugging enabled.",
                    "red", attrs=["bold"]
                ))
                logger.error(colored(
                    f'"{chrome_path}" --remote-debugging-port=9222 --user-data-dir="{profile_dir}"',
                    "yellow"
                ))
                raise e

        # Get existing context or create one
        if session.browser.contexts:
            session.context = session.browser.contexts[0]
        else:
            session.context = session.browser.new_context()

        # ----------------------------------------------------------------
        # Apply full stealth to ALL pages in this context — now and future.
        # This is the critical missing piece in CDP mode: stealth scripts
        # must be injected here because connect_over_cdp() doesn't go
        # through playwright-stealth's launch hook.
        # ----------------------------------------------------------------
        logger.info("Applying stealth profile to CDP context...")

        # Stealth all already-open pages (e.g. new tab, about:blank)
        for existing_page in session.context.pages:
            apply_full_stealth(existing_page, session.context)

        # Stealth every page opened from here forward (new tabs, pop-ups)
        session.context.on("page", apply_stealth_to_new_page)

        # Find if a LinkedIn page is already open in the browser
        linkedin_page = None
        for p in session.context.pages:
            if "linkedin.com" in p.url:
                linkedin_page = p
                logger.info("Found existing LinkedIn tab: %s", p.url)
                break

        if linkedin_page:
            session.page = linkedin_page
        else:
            session.page = session.context.new_page()
            # Stealth is already wired via the context 'page' event above;
            # new_page() triggers apply_stealth_to_new_page automatically.
            session.page.goto(LINKEDIN_FEED_URL)

        # Ensure we are on LinkedIn feed or logged in
        if "linkedin.com" not in session.page.url:
            session.page.goto(LINKEDIN_FEED_URL)

        # Wait for user to log in if they aren't
        if any(k in session.page.url for k in ["/login", "/signup", "authwall", "checkpoint"]):
            logger.warning(colored(
                "Not logged in to LinkedIn. Please log in manually in the visible Chrome window.",
                "yellow", attrs=["bold"]
            ))
            import time
            while not any(k in session.page.url for k in ["/feed", "/in/", "/messaging"]):
                time.sleep(2)
            logger.info(colored("Logged in successfully in active browser!", "green", attrs=["bold"]))
            try:
                save_storage_state(session)
            except Exception:
                pass

        dismiss_comply_gate(session.page)
    else:
        session.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        cookie_data = session.linkedin_profile.cookie_data

        storage_state = cookie_data if cookie_data else None
        if storage_state:
            logger.info("Loading saved session for %s", session)

        session.page, session.context, session.browser, session.playwright = launch_browser(storage_state=storage_state)

        # Stealth non-CDP path: launch_browser uses playwright-stealth's
        # Stealth().apply_stealth_sync(context) which covers the basics.
        # We add our extended scripts on top for full coverage.
        logger.info("Applying extended stealth profile to launched browser...")
        apply_full_stealth(session.page, session.context)
        session.context.on("page", apply_stealth_to_new_page)

        if not storage_state:
            lp = session.linkedin_profile
            authenticate(session, username=lp.linkedin_username, password=lp.linkedin_password)
            _save_cookies(session)
            logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))
        else:
            session.page.goto(LINKEDIN_FEED_URL)
            dismiss_comply_gate(session.page)
            goto_page(
                session,
                action=lambda: None,
                expected_url_pattern="/feed",
                error_message="Saved session invalid",
            )

    # "domcontentloaded" — "load" waits for every subresource (analytics
    # beacons, lazy media) and on LinkedIn that event may never fire,
    # hanging the daemon for the duration of the browser timeout.
    session.page.wait_for_load_state("domcontentloaded")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))

