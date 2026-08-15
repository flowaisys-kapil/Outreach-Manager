# openoutreach/linkedin/pipeline/acceptances.py
import logging
import random
import time
from typing import Set

from django.utils import timezone
from termcolor import colored

from linkedin_cli.url_utils import url_to_public_id
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.db.deals import set_profile_state

logger = logging.getLogger(__name__)


_LAST_ACCEPTANCE_CHECK_TIME: dict[int, float] = {}
_ACCEPTANCE_CHECK_MIN_INTERVAL_SEC = 900  # 15 minutes


def check_acceptances_page(session) -> Set[str]:
    """Scrapes the LinkedIn connections list page to find all connected public IDs.
    
    If in a test environment (no real browser/page), falls back to checking status of 
    pending deals via get_connection_status so unit tests remain fully functional.
    """
    if not hasattr(session, "page") or session.page is None:
        logger.info("No active browser page on session. Falling back to individual status checks (test/mock mode).")
        from linkedin_cli.actions.status import get_connection_status
        from linkedin_cli.enums import ProfileState
        
        connected_ids = set()
        pending_deals = Deal.objects.filter(
            state=DealState.PENDING,
            lead__disqualified=False,
        ).select_related("lead")
        for deal in pending_deals:
            try:
                status = get_connection_status(session, deal.lead.to_profile_dict())
                if status == ProfileState.CONNECTED or status == "CONNECTED":
                    connected_ids.add(deal.lead.public_identifier)
            except Exception:
                pass
        return connected_ids

    page = session.page
    current_url = getattr(page, "url", "")
    
    if "/mynetwork/invite-connect/connections/" in current_url or "/connections/" in current_url:
        logger.info("Acceptance Check: Already on connections page.")
    else:
        logger.info("Acceptance Check: Navigating to LinkedIn Connections page...")
        try:
            page.goto("https://www.linkedin.com/mynetwork/invite-connect/connections/")
            session.wait()
        except Exception as e:
            logger.warning("Failed direct navigation to connections page: %s. Trying mynetwork fallback.", e)
            try:
                page.goto("https://www.linkedin.com/mynetwork/")
                session.wait()
            except Exception as e2:
                logger.error("Failed to navigate to mynetwork: %s", e2)
                return set()

    session.wait()
    
    # Wait for the connections page list to load
    try:
        page.wait_for_selector('a[href*="/in/"]', timeout=15000)
    except Exception as e:
        logger.warning("No connection links found on connections page: %s", e)
        return set()

    # Scroll down to load full connections list — 3 scrolls at 0.8s each is enough
    # for most lists; longer waits only help when the page is very slow.
    try:
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            session.wait(0.8, 0.8)
    except Exception as scroll_err:
        logger.warning("Scrolling connections list: %s", scroll_err)
        
    hrefs = page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('a[href*="/in/"]'));
        return links.map(a => a.href);
    }""")
    
    connections = set()
    for href in hrefs:
        pid = url_to_public_id(href)
        if pid:
            connections.add(pid)
            
    logger.info("Scraped %d connection profile(s) from page.", len(connections))
    return connections


def is_older_than_7_days(sent_text: str) -> bool:
    """Check if the sent request time text is older than 7 days."""
    text = sent_text.lower().strip()
    if "week" in text or "month" in text or "year" in text:
        return True
    if "day" in text:
        import re
        match = re.search(r'(\d+)\s+day', text)
        if match:
            days = int(match.group(1))
            return days >= 7
    return False


def parse_sent_text_to_datetime(sent_text: str):
    """Parse sent time text (e.g., 'Sent 3 days ago', 'Sent 2 weeks ago') to estimated datetime."""
    text = (sent_text or "").lower().strip()
    now = timezone.now()
    import re
    from datetime import timedelta
    if "week" in text:
        match = re.search(r'(\d+)\s+week', text)
        weeks = int(match.group(1)) if match else 1
        return now - timedelta(days=weeks * 7)
    if "month" in text:
        match = re.search(r'(\d+)\s+month', text)
        months = int(match.group(1)) if match else 1
        return now - timedelta(days=months * 30)
    if "year" in text:
        return now - timedelta(days=365)
    if "day" in text:
        match = re.search(r'(\d+)\s+day', text)
        days = int(match.group(1)) if match else 1
        return now - timedelta(days=days)
    return now - timedelta(days=1)


def _connection_request_age_filter():
    """Return a Q object matching PENDING deals whose connection request is older than 7 days.

    Uses ``connection_requested_at`` exclusively — this is the timestamp
    recorded when the LinkedIn invitation was actually sent (Phase 3).
    """
    from datetime import timedelta
    from django.db.models import Q

    cutoff = timezone.now() - timedelta(days=7)
    return Q(connection_requested_at__isnull=False, connection_requested_at__lte=cutoff)


def withdraw_deal(session, campaign, public_id: str):
    """Withdraw a connection request in the database and handle withdrawal/retry lifecycle."""
    from outreach_manager.crm.models import Deal, Outcome
    from outreach_manager.core.db.deals import set_profile_state
    from outreach_manager.crm.models.event_log import EventLog

    deal = Deal.objects.filter(
        campaign=campaign,
        lead__public_identifier=public_id
    ).select_related("lead").first()
    if not deal:
        logger.warning("[%s] No deal found for withdrawn public_id: %s", campaign, public_id)
        return

    deal.withdraw_count += 1
    deal.last_withdrawn_at = timezone.now()
    deal.save(update_fields=["withdraw_count", "last_withdrawn_at"])

    if deal.withdraw_count >= 3:
        logger.info("[%s] Lead %s reached max withdrawal limit (3). Dropping permanently.", campaign, public_id)
        set_profile_state(session, public_id, DealState.FAILED.value, reason="Withdrawn 3 times without acceptance - dropped permanently.", outcome=Outcome.UNRESPONSIVE)
        try:
            EventLog.objects.create(
                campaign=campaign,
                deal=deal,
                event_type=EventLog.EventType.CONNECT_REQUESTED,
                detail=f"Permanently dropped {public_id} after 3 unanswered connection requests."
            )
        except Exception as e:
            logger.warning("Failed to log withdrawal event: %s", e)
    else:
        logger.info("[%s] Lead %s withdrawn (attempt %d/3). Placing in cooldown.", campaign, public_id, deal.withdraw_count)
        set_profile_state(session, public_id, DealState.QUALIFIED.value, reason=f"Withdrawn (attempt {deal.withdraw_count}/3) - placed in cooldown.")
        try:
            EventLog.objects.create(
                campaign=campaign,
                deal=deal,
                event_type=EventLog.EventType.CONNECT_REQUESTED,
                detail=f"Withdrew connection request to {public_id} (attempt {deal.withdraw_count}/3). Placed in 21-day cooldown."
            )
        except Exception as e:
            logger.warning("Failed to log withdrawal event: %s", e)


def scrape_sent_invitations(session) -> list[dict]:
    """Scrape all pending connection requests from LinkedIn Sent Invitations page."""
    if not hasattr(session, "page") or session.page is None:
        logger.info("No active browser page on session. Skipping Sent Invitations page scrape (test/mock mode).")
        return []

    page = session.page
    current_url = getattr(page, "url", "")
    logger.info("Navigating to LinkedIn Sent Invitations page...")

    if "/invitation-manager/sent/" not in current_url:
        try:
            page.goto("https://www.linkedin.com/mynetwork/invitation-manager/sent/")
            session.wait()
        except Exception as e:
            logger.error("Failed navigation to sent invitations page: %s", e)
            return []

    session.wait()

    last_height = page.evaluate("document.body.scrollHeight")
    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        session.wait(1.0, 1.5)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    sent_requests = page.evaluate(r"""() => {
        const results = [];
        const profileLinks = document.querySelectorAll('a[href*="/in/"]');
        const seen = new Set();
        for (const link of profileLinks) {
            const href = link.getAttribute('href');
            if (!href) continue;
            const match = href.match(/\/in\/([^\/\?#]+)/);
            if (!match) continue;
            const publicId = match[1];
            if (seen.has(publicId)) continue;
            seen.add(publicId);

            let parent = link.parentElement;
            let withdrawBtn = null;
            let sentText = "";
            
            for (let i = 0; i < 8 && parent; i++) {
                withdrawBtn = parent.querySelector('button, a');
                if (withdrawBtn && !withdrawBtn.textContent.toLowerCase().includes('withdraw')) {
                    withdrawBtn = null;
                }
                const sentEl = Array.from(parent.querySelectorAll('*')).find(el => 
                    el.childNodes.length === 1 && el.textContent.trim().toLowerCase().startsWith('sent ')
                );
                if (sentEl) {
                    sentText = sentEl.textContent.trim();
                }
                
                if (withdrawBtn && sentText) {
                    break;
                }
                parent = parent.parentElement;
            }
            
            if (withdrawBtn) {
                results.push({
                    public_id: publicId,
                    name: link.textContent.trim(),
                    sent_text: sentText
                });
            }
        }
        return results;
    }""")
    logger.info("Scraped %d pending sent invitation(s) from page.", len(sent_requests or []))
    return sent_requests or []


def sync_sent_invitations(session, campaign) -> list[dict]:
    """Phase A Synchronization: Make LinkedIn Sent Invitations the source of truth.

    1. Scrapes all pending invitations currently on LinkedIn.
    2. Discovers missing pending invitations on LinkedIn that aren't in local DB -> creates Deal/Lead.
    3. Repairs missing send timestamps (`connection_requested_at`) for existing deals.
    4. Promotes local PENDING deals that have accepted to CONNECTED.
    """
    from outreach_manager.crm.models import Deal, DealState
    from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
    from outreach_manager.core.db.deals import set_profile_state

    sent_requests = scrape_sent_invitations(session)
    connections = check_acceptances_page(session)

    linkedin_pending_pids = {req["public_id"]: req for req in sent_requests if req.get("public_id")}

    # 1. Discover & Synchronize Missing/Out-of-Sync Pending Deals
    for pid, req in linkedin_pending_pids.items():
        name = req.get("name", "")
        sent_text = req.get("sent_text", "")
        estimated_send_time = parse_sent_text_to_datetime(sent_text)

        deal = Deal.objects.filter(campaign=campaign, lead__public_identifier=pid).select_related("lead").first()

        if not deal:
            first_name = name.split()[0] if name else pid
            last_name = " ".join(name.split()[1:]) if name and len(name.split()) > 1 else ""
            profile_dict = {
                "first_name": first_name,
                "last_name": last_name,
            }
            url = f"https://www.linkedin.com/in/{pid}/"
            create_enriched_lead(session, url, profile_dict)
            promote_lead_to_deal(session, pid)
            deal = Deal.objects.filter(campaign=campaign, lead__public_identifier=pid).first()
            if deal:
                state_val = DealState.PENDING.value if hasattr(DealState.PENDING, "value") else DealState.PENDING
                deal.state = state_val
                deal.connection_requested_at = estimated_send_time
                deal.save(update_fields=["state", "connection_requested_at"])
                logger.info("[%s] Discovered missing pending invitation on LinkedIn for %s. Created local Deal.", campaign, pid)
        else:
            fields_to_update = []
            current_state = deal.state.value if hasattr(deal.state, "value") else str(deal.state)
            if current_state != DealState.PENDING.value and current_state != "PENDING":
                deal.state = DealState.PENDING.value if hasattr(DealState.PENDING, "value") else DealState.PENDING
                fields_to_update.append("state")
            if deal.connection_requested_at is None:
                deal.connection_requested_at = estimated_send_time
                fields_to_update.append("connection_requested_at")
            if fields_to_update:
                deal.save(update_fields=fields_to_update)

    # 2. Check local PENDING deals against scraped connections & missing sent status
    local_pending = Deal.objects.filter(campaign=campaign, state=DealState.PENDING, lead__disqualified=False).select_related("lead")
    for deal in local_pending:
        pid = deal.lead.public_identifier
        if pid in connections or (sent_requests and pid not in linkedin_pending_pids):
            logger.info("[%s] Pending invitation for %s is connected or no longer pending. Promoting to CONNECTED.", campaign, pid)
            set_profile_state(session, pid, DealState.CONNECTED.value)

    return sent_requests


def run_withdrawals_check(session, campaign, sent_requests: list[dict] | None = None):
    """Withdraw connection requests whose invitation is older than 7 days."""
    age_filter = _connection_request_age_filter()

    if sent_requests is None:
        sent_requests = scrape_sent_invitations(session)

    if not sent_requests:
        # Mock / fallback mode
        if not hasattr(session, "page") or session.page is None:
            pending_deals = Deal.objects.filter(
                campaign=campaign,
                state=DealState.PENDING,
                lead__disqualified=False,
            ).filter(age_filter).select_related("lead")

            for deal in pending_deals:
                withdraw_deal(session, campaign, deal.lead.public_identifier)
        return

    # Process scraped sent requests for withdrawals (> 7 days)
    page = session.page
    withdrawn_count = 0
    for req in sent_requests:
        public_id = req.get("public_id")
        sent_text = req.get("sent_text", "")
        if public_id and is_older_than_7_days(sent_text):
            if page:
                try:
                    container = page.locator("li, div").filter(
                        has=page.locator(f'a[href*="/in/{public_id}"]')
                    ).filter(
                        has=page.locator('button:has-text("Withdraw")')
                    ).first

                    withdraw_btn = container.locator('button:has-text("Withdraw")')
                    if withdraw_btn.count() > 0:
                        logger.info("[%s] Clicking Withdraw button for %s (sent text: %s)...", campaign, public_id, sent_text)
                        withdraw_btn.click()
                        session.wait(1.5, 2.5)

                        dialog = page.locator('[role="dialog"]')
                        if dialog.count() > 0:
                            confirm_btn = dialog.locator('button:has-text("Withdraw")')
                            if confirm_btn.count() > 0:
                                logger.info("[%s] Confirming withdrawal in modal for %s...", campaign, public_id)
                                confirm_btn.click()
                                session.wait(1.5, 2.5)
                except Exception as click_err:
                    logger.warning("Failed to perform click withdrawal for %s: %s", public_id, click_err)

            withdraw_deal(session, campaign, public_id)
            withdrawn_count += 1

    if withdrawn_count:
        logger.info("[%s] Withdrew %d stale connection invitation(s).", campaign, withdrawn_count)


def run_acceptance_sweep(session, campaign) -> int:
    """Acceptance & withdrawal sweep: scrapes the LinkedIn connections list page,
    promotes accepted pending leads to CONNECTED in the database, and withdraws
    stale invitations (>7 days).
    """
    before_connected = Deal.objects.filter(campaign=campaign, state=DealState.CONNECTED).count()
    sent_requests = sync_sent_invitations(session, campaign)
    after_connected = Deal.objects.filter(campaign=campaign, state=DealState.CONNECTED).count()
    promoted_count = max(0, after_connected - before_connected)

    try:
        run_withdrawals_check(session, campaign, sent_requests=sent_requests)
    except Exception as we:
        logger.exception("[%s] Withdrawal check failed: %s", campaign, we)

    return promoted_count
