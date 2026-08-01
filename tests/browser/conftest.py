# tests/browser/conftest.py
"""Playwright fixtures for testing selectors against saved HTML pages."""
import pytest
from playwright.sync_api import sync_playwright

from linkedin_cli.conf import FIXTURE_PAGES_DIR


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def load_fixture(page, *path_parts: str):
    """Load an HTML fixture file into the Playwright page."""
    filepath = FIXTURE_PAGES_DIR / "/".join(path_parts)
    if not filepath.exists():
        pytest.skip(f"Fixture not found: {filepath}")
    page.set_content(filepath.read_text(encoding="utf-8"))
    return page
