# linkedin_cli/actions/search.py
"""LinkedIn Search Action Handler."""
from __future__ import annotations

import logging
import urllib.parse

logger = logging.getLogger(__name__)


def search_people(page_or_session, query: str, *args, **kwargs) -> list[dict]:
    """Search people on LinkedIn and return rich profile result dicts."""
    try:
        page = getattr(page_or_session, "page", page_or_session)
        if not page or getattr(page, "is_closed", lambda: True)():
            logger.warning("search_people: Page unavailable")
            return []

        search_url = f"https://www.linkedin.com/search/results/people/?keywords={urllib.parse.quote(query)}"
        logger.info("Navigating to LinkedIn search: %s", search_url)
        page.goto(search_url)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        # Scrape profile links + card text from search results page
        profiles_raw = page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            const links = Array.from(document.querySelectorAll("a[href*='/in/']"));
            for (const a of links) {
                const href = a.href || "";
                if (!href.includes("/in/")) continue;
                const clean = href.split("?")[0].replace(/\\/$/, "") + "/";
                const parts = clean.split("/in/");
                if (parts.length < 2) continue;
                const pub_id = parts[1].replace(/\\/$/, "");
                if (!pub_id || seen.has(pub_id) || pub_id.startsWith("ACoAA") || ["edit", "me", "settings"].includes(pub_id)) continue;
                seen.add(pub_id);

                const container = a.closest("li, div.entity-result, div[data-chameleon-result-urn]") || a.parentElement;
                let name = a.innerText ? a.innerText.trim() : "";
                let headline = "";
                if (container) {
                    const sub = container.querySelector("div[class*='primary-subtitle'], div[class*='subtitle'], div.text-body-small");
                    if (sub) headline = sub.innerText ? sub.innerText.trim() : "";
                }

                results.push({
                    url: clean,
                    public_identifier: pub_id,
                    name: name,
                    headline: headline
                });
            }
            return results;
        }""")

        profiles = []
        for item in profiles_raw:
            pub_id = item.get("public_identifier")
            clean = item.get("url")
            headline = item.get("headline") or f"{query.title()} on LinkedIn"
            name = item.get("name") or pub_id.replace("-", " ").title()
            profiles.append({
                "url": clean,
                "public_identifier": pub_id,
                "headline": headline,
                "name": name,
                "keyword": query,
            })

        logger.info("search_people: Discovered %d profiles for query '%s'", len(profiles), query)
        return profiles
    except Exception as exc:
        logger.warning("search_people error for '%s': %s", query, exc)
        return []


def visit_profile(page_or_session, profile_url: str, *args, **kwargs) -> bool:
    """Navigate to a LinkedIn profile URL."""
    try:
        page = getattr(page_or_session, "page", page_or_session)
        if not page or getattr(page, "is_closed", lambda: True)():
            return False
        page.goto(profile_url)
        page.wait_for_load_state("domcontentloaded")
        return True
    except Exception:
        return False
