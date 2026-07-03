"""Indeed job search — finds 'Easily apply' listings and returns Job objects."""

import asyncio
import random
import urllib.parse
from typing import Optional

from loguru import logger
from playwright.async_api import Page

from src.models.job import Job

_JOBS_BASE = "https://www.indeed.com/jobs"

# Indeed URL param for "Easily apply" filter
_EASY_APPLY_FILTER = "attr(DSQF7)"


class IndeedJobSearcher:
    """Searches Indeed for Easy-apply jobs and returns parsed Job objects."""

    def __init__(self, page: Page) -> None:
        self._page = page

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        location: str = "",
        max_jobs: int = 20,
        date_posted: str = "",
        remote_filter: str = "",
        job_type: str = "",
    ) -> list[Job]:
        """Return up to `max_jobs` Indeed Easy Apply jobs."""
        url = self._build_url(query, location, date_posted, remote_filter, job_type)
        logger.info(f"Indeed: navigating to job search: {url}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))
        await self._page.screenshot(path="data/screenshots/indeed_search_page.png")
        logger.debug("Indeed: search page screenshot → data/screenshots/indeed_search_page.png")

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        page_num = 0

        while len(jobs) < max_jobs:
            new_jobs = await self._scrape_page(seen_ids)
            jobs.extend(new_jobs)
            logger.info(f"Indeed: collected {len(jobs)} / {max_jobs} jobs")

            if len(jobs) >= max_jobs or not new_jobs:
                break

            if not await self._next_page(page_num):
                logger.info("Indeed: no more pages")
                break
            page_num += 1
            await asyncio.sleep(random.uniform(2, 4))

        return jobs[:max_jobs]

    # ── URL builder ────────────────────────────────────────────────────────────

    def _build_url(
        self,
        query: str,
        location: str,
        date_posted: str,
        remote_filter: str,
        job_type: str,
    ) -> str:
        params: dict[str, str] = {
            "q": query,
            "sc": f"0kf:{_EASY_APPLY_FILTER}:",
            "sort": "date",
        }
        if location:
            params["l"] = location
        # date_posted → fromage (days)
        _fromage = {"day": "1", "week": "7", "month": "30"}
        if date_posted in _fromage:
            params["fromage"] = _fromage[date_posted]
        # job_type
        _jt_map = {
            "full-time": "fulltime",
            "part-time": "parttime",
            "contract": "contract",
            "temporary": "temporary",
            "internship": "internship",
        }
        if job_type in _jt_map:
            params["jt"] = _jt_map[job_type]
        # remote
        if remote_filter == "remote":
            params["remotejob"] = "032b3046-06a3-4876-8dfd-474eb5e7ed11"

        return f"{_JOBS_BASE}?{urllib.parse.urlencode(params)}"

    # ── Page scraping ──────────────────────────────────────────────────────────

    async def _scrape_page(self, seen_ids: set[str]) -> list[Job]:
        """Scrape all job cards on the current results page."""
        # Scroll to trigger lazy loading
        await self._page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1)

        cards = await self._find_job_cards()
        if not cards:
            logger.warning("Indeed: no job cards found on this page")
            return []

        logger.debug(f"Indeed: found {len(cards)} job cards")
        jobs: list[Job] = []

        for card_locator in cards:
            job = await self._parse_card(card_locator, seen_ids)
            if job:
                jobs.append(job)
                seen_ids.add(job.job_id)
            await asyncio.sleep(random.uniform(1.0, 2.0))

        return jobs

    async def _find_job_cards(self) -> list:
        """Return a list of job card locators from the current page."""
        selectors = [
            "ul.jobsearch-ResultsList > li.css-1ac2h1w",
            "ul.jobsearch-ResultsList > li[data-testid]",
            "div[data-testid='slider_item']",
            "li[data-testid='jobListing']",
            "div.job_seen_beacon",
            "li[class*='job_seen_beacon']",
        ]
        for sel in selectors:
            try:
                items = self._page.locator(sel)
                count = await items.count()
                if count > 0:
                    logger.debug(f"Indeed: cards via '{sel}' ({count})")
                    return [items.nth(i) for i in range(count)]
            except Exception as e:
                logger.debug(f"Indeed: selector '{sel}' failed: {e}")

        # Fallback: any element with data-jk (job key)
        fallback = self._page.locator("[data-jk]")
        count = await fallback.count()
        if count > 0:
            logger.debug(f"Indeed: fallback [data-jk] ({count})")
            return [fallback.nth(i) for i in range(count)]

        return []

    async def _parse_card(self, card, seen_ids: set[str]) -> Optional[Job]:
        """Extract job data from a single card and check for Easy Apply."""
        try:
            job_id = await self._extract_job_id(card)
            if not job_id or job_id in seen_ids:
                return None

            title   = await self._card_text(card, [
                "h2[data-testid='jobTitle'] span",
                "h2.jobTitle span",
                "a.jcs-JobTitle span",
                "span[title]",
            ])
            company = await self._card_text(card, [
                "span[data-testid='company-name']",
                "[class*='companyName']",
                "a[data-testid='company-name']",
            ])
            location = await self._card_text(card, [
                "div[data-testid='text-location']",
                "[class*='companyLocation']",
            ])

            is_easy_apply = await self._card_has_easy_apply(card)
            if not is_easy_apply:
                return None

            # Click card to load detail and get description + URL
            await card.click()
            await asyncio.sleep(random.uniform(2, 3))
            description = await self._extract_description()
            job_url = self._page.url

            return Job(
                job_id=job_id,
                title=title or "Unknown Title",
                company=company or "Unknown Company",
                location=location or "",
                description=description,
                is_easy_apply=True,
                url=job_url,
            )
        except Exception as e:
            logger.warning(f"Indeed: failed to parse card: {e}")
            return None

    # ── Field helpers ──────────────────────────────────────────────────────────

    async def _extract_job_id(self, card) -> str:
        for attr in ["data-jk", "data-job-id", "id"]:
            val = await card.get_attribute(attr) or ""
            if val and val.strip():
                # Strip "job_" prefix if present
                return val.strip().lstrip("job_")
        # Try inner link href
        try:
            link = card.locator("a[href*='jk=']").first
            if await link.count() > 0:
                href = await link.get_attribute("href") or ""
                for part in href.split("&"):
                    if part.startswith("jk=") or "jk=" in part:
                        return part.split("jk=")[-1].split("&")[0]
        except Exception:
            pass
        return ""

    async def _card_text(self, card, selectors: list[str]) -> str:
        for sel in selectors:
            try:
                el = card.locator(sel).first
                if await el.count() > 0:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    async def _card_has_easy_apply(self, card) -> bool:
        """Return True if this card has an 'Easily apply' or 'Indeed Apply' badge."""
        try:
            badge = card.locator(
                ".iaLabel, "
                "[class*='easily-apply'], "
                "[aria-label*='Easily apply'], "
                "span:has-text('Easily apply'), "
                "span:has-text('Indeed Apply')"
            )
            return await badge.count() > 0
        except Exception:
            return False

    async def _extract_description(self) -> str:
        """Read job description from the detail panel / page."""
        selectors = [
            "#jobDescriptionText",
            ".jobsearch-jobDescriptionText",
            "[data-testid='jobDescriptionText']",
            ".job-description",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                if await el.count() > 0:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text[:3000]
            except Exception:
                continue
        return ""

    # ── Pagination ─────────────────────────────────────────────────────────────

    async def _next_page(self, current_page: int) -> bool:
        try:
            next_btn = self._page.locator(
                "a[aria-label='Next Page'], "
                "a[data-testid='pagination-page-next'], "
                "a:has-text('Next »')"
            ).first
            if await next_btn.count() == 0:
                return False
            await next_btn.click()
            await self._page.wait_for_load_state("domcontentloaded")
            return True
        except Exception as e:
            logger.debug(f"Indeed: pagination error: {e}")
            return False
