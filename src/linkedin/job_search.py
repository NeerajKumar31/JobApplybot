import asyncio
import random
import urllib.parse

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.models.job import Job

JOBS_SEARCH_BASE = "https://www.linkedin.com/jobs/search/"

_REMOTE_MAP      = {"remote": "2", "on-site": "1", "hybrid": "3"}
_DATE_MAP        = {"day": "r86400", "week": "r604800", "month": "r2592000"}
_EXPERIENCE_MAP  = {
    "internship": "1", "entry": "2", "associate": "3",
    "mid-senior": "4", "director": "5", "executive": "6",
}
_JOB_TYPE_MAP    = {
    "full-time": "F", "part-time": "P", "contract": "C",
    "temporary": "T", "internship": "I",
}

# Ordered list of CSS selectors to try for the job-card list container.
# LinkedIn changes class names regularly; we try the most-current ones first.
_JOB_LIST_SELECTORS = [
    "ul.scaffold-layout__list-container",
    "ul.jobs-search-results__list",
    "ul.jobs-search__results-list",
    ".jobs-search-results-list ul",
    "div.scaffold-layout__list ul",
    "[data-view-name='job-search-results'] ul",
]

# Selectors for extracting the job-id from a card element
_JOB_ID_ATTRS = ["data-job-id", "data-occludable-job-id", "data-entity-urn"]


class JobSearchError(Exception):
    """Raised when the job search page fails to load or parse."""


class JobSearcher:
    """Searches LinkedIn Jobs for Easy Apply listings and extracts job details."""

    def __init__(self, page: Page) -> None:
        self._page = page

    async def search(
        self,
        query: str,
        location: str = "",
        max_jobs: int = 20,
        remote_filter: str = "",
        date_posted: str = "",
        experience_level: str = "",
        job_type: str = "",
    ) -> list[Job]:
        """Return up to `max_jobs` Easy Apply jobs matching `query` and filters."""
        url = self._build_search_url(
            query, location, remote_filter, date_posted, experience_level, job_type
        )
        logger.info(f"Navigating to job search: {url}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))

        # Save screenshot so we can inspect the page structure if nothing is found
        await self._page.screenshot(path="data/screenshots/job_search_page.png")
        logger.debug("Job search page screenshot → data/screenshots/job_search_page.png")

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        page_num = 0

        while len(jobs) < max_jobs:
            new_jobs = await self._scrape_current_page(seen_ids)
            jobs.extend(new_jobs)
            logger.info(f"Collected {len(jobs)} / {max_jobs} jobs so far")

            if len(jobs) >= max_jobs or not new_jobs:
                break

            if not await self._go_to_next_page(page_num):
                logger.info("No more pages available")
                break
            page_num += 1
            await asyncio.sleep(random.uniform(2, 4))

        return jobs[:max_jobs]

    def _build_search_url(
        self,
        query: str,
        location: str,
        remote_filter: str,
        date_posted: str,
        experience_level: str,
        job_type: str,
    ) -> str:
        params: dict[str, str] = {
            "keywords": query,
            "f_LF": "f_AL",
            "sortBy": "R",
        }
        if location:
            params["location"] = location
        if remote_filter in _REMOTE_MAP:
            params["f_WT"] = _REMOTE_MAP[remote_filter]
        if date_posted in _DATE_MAP:
            params["f_TPR"] = _DATE_MAP[date_posted]
        if experience_level in _EXPERIENCE_MAP:
            params["f_E"] = _EXPERIENCE_MAP[experience_level]
        if job_type in _JOB_TYPE_MAP:
            params["f_JT"] = _JOB_TYPE_MAP[job_type]
        return f"{JOBS_SEARCH_BASE}?{urllib.parse.urlencode(params)}"

    # ── Page scraping ──────────────────────────────────────────────────────────

    async def _scrape_current_page(self, seen_ids: set[str]) -> list[Job]:
        """Scrape all job cards visible on the current results page."""
        cards = await self._find_job_cards()
        if not cards:
            logger.warning(
                "No job cards found — LinkedIn may have changed its HTML structure. "
                "Check data/screenshots/job_search_page.png"
            )
            return []

        logger.debug(f"Found {len(cards)} job cards")
        jobs: list[Job] = []

        for card_locator in cards:
            job = await self._parse_card(card_locator, seen_ids)
            if job:
                jobs.append(job)
                seen_ids.add(job.job_id)
            await asyncio.sleep(random.uniform(1.5, 3.0))

        return jobs

    async def _find_job_cards(self) -> list:
        """Try each known list selector and return a list of card locators."""
        # First scroll down to trigger lazy loading
        await self._page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(1)

        for selector in _JOB_LIST_SELECTORS:
            try:
                ul = self._page.locator(selector)
                if await ul.count() == 0:
                    continue
                items = ul.first.locator("li")
                count = await items.count()
                if count > 0:
                    logger.debug(f"Job list found with selector '{selector}' ({count} items)")
                    return [items.nth(i) for i in range(count)]
            except Exception as e:
                logger.debug(f"Selector '{selector}' failed: {e}")

        # Last resort: any li that contains a job card link
        fallback = self._page.locator(
            "li:has(a[href*='/jobs/view/']), li:has([data-job-id]), li:has([data-occludable-job-id])"
        )
        count = await fallback.count()
        if count > 0:
            logger.debug(f"Job cards found via fallback selector ({count} items)")
            return [fallback.nth(i) for i in range(count)]

        return []

    async def _parse_card(self, card, seen_ids: set[str]) -> Job | None:
        """Click a job card and extract full details from the detail panel."""
        try:
            job_id = await self._extract_job_id(card)
            if not job_id or job_id in seen_ids:
                return None

            if await self._card_is_already_applied(card):
                logger.info(f"Skipping {job_id} — already applied")
                return None

            # Extract visible text fields from the card
            title    = await self._card_text(card, [
                ".job-card-list__title",
                ".artdeco-entity-lockup__title",
                "a[href*='/jobs/view/']",
            ])
            company  = await self._card_text(card, [
                ".job-card-container__company-name",
                ".artdeco-entity-lockup__subtitle",
                ".job-card-list__company-name",
            ])
            location = await self._card_text(card, [
                ".job-card-container__metadata-item",
                ".artdeco-entity-lockup__caption",
                ".job-search-card__location",
            ])

            # Click card to load description in right panel
            await card.click()
            await asyncio.sleep(random.uniform(2, 3))

            description  = await self._extract_description()
            is_easy_apply = await self._detect_easy_apply()
            job_url       = self._page.url

            return Job(
                job_id=job_id,
                title=title or "Unknown Title",
                company=company or "Unknown Company",
                location=location or "",
                description=description,
                is_easy_apply=is_easy_apply,
                url=job_url,
            )

        except Exception as e:
            logger.warning(f"Failed to parse job card: {e}")
            return None

    # ── Field helpers ──────────────────────────────────────────────────────────

    async def _extract_job_id(self, card) -> str:
        """Try multiple attributes to extract the job ID."""
        for attr in _JOB_ID_ATTRS:
            val = await card.get_attribute(attr) or ""
            if val:
                # data-entity-urn looks like "urn:li:jobPosting:1234" — extract the number
                return val.split(":")[-1] if ":" in val else val

        # Fallback: extract from the job link href
        try:
            link = card.locator("a[href*='/jobs/view/']").first
            if await link.count() > 0:
                href = await link.get_attribute("href") or ""
                # href like /jobs/view/1234567/?...
                parts = [p for p in href.split("/") if p.isdigit()]
                if parts:
                    return parts[0]
        except Exception:
            pass
        return ""

    async def _card_text(self, card, selectors: list[str]) -> str:
        """Return inner text from the first matching selector inside a card."""
        for selector in selectors:
            try:
                el = card.locator(selector).first
                if await el.count() > 0:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    async def _card_is_already_applied(self, card) -> bool:
        try:
            el = card.locator("[class*='applied'], li-icon[type='check-circle-icon']")
            if await el.count() > 0:
                text = (await el.first.inner_text()).strip().lower()
                return "applied" in text
        except Exception:
            pass
        return False

    async def _extract_description(self) -> str:
        """Read the full job description from the detail panel."""
        try:
            see_more = self._page.locator(
                "button[aria-label*='more description'], "
                "button[aria-label*='See more']"
            )
            if await see_more.count() > 0:
                await see_more.first.click()
                await asyncio.sleep(0.5)

            for selector in [
                ".jobs-description__content",
                ".jobs-box__html-content",
                "#job-details",
                ".job-details-jobs-unified-top-card__job-insight",
            ]:
                el = self._page.locator(selector).first
                if await el.count() > 0:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
        except Exception as e:
            logger.debug(f"Description extraction failed: {e}")
        return ""

    async def _detect_easy_apply(self) -> bool:
        """Return True if the detail panel shows an Easy Apply button."""
        try:
            btn = self._page.locator(
                "button[aria-label*='Easy Apply'], "
                ".jobs-apply-button[aria-label*='Easy Apply']"
            )
            return await btn.count() > 0
        except Exception:
            return False

    async def _go_to_next_page(self, current_page: int) -> bool:
        """Click the next-page pagination button."""
        try:
            # LinkedIn has two "Next" buttons (aria-label="View next page" and exact "Next").
            # Use exact=True to avoid strict-mode violation.
            next_btn = self._page.get_by_role("button", name="Next", exact=True).first
            if await next_btn.count() == 0:
                return False
            if await next_btn.is_disabled():
                return False
            await next_btn.click()
            await self._page.wait_for_load_state("domcontentloaded")
            return True
        except Exception as e:
            logger.debug(f"Pagination error: {e}")
            return False
