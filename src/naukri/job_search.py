"""Naukri job search — finds apply-eligible listings and returns Job objects."""

import asyncio
import random
import urllib.parse
from typing import Optional

from loguru import logger
from playwright.async_api import Page

from src.models.job import Job


class NaukriJobSearcher:
    """Searches Naukri for jobs and returns parsed Job objects."""

    _BASE = "https://www.naukri.com"

    def __init__(self, page: Page) -> None:
        self._page = page

    # ── Public API ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        location: str = "",
        max_jobs: int = 20,
        experience: int = 0,
        date_posted: str = "",
        job_type: str = "",
    ) -> list[Job]:
        """Return up to `max_jobs` Naukri apply-eligible jobs."""
        url = self._build_url(query, location, experience, date_posted)
        logger.info(f"Naukri: navigating to job search: {url}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))
        await self._dismiss_popups()
        await self._page.screenshot(path="data/screenshots/naukri_search_page.png")

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        page_num = 1

        while len(jobs) < max_jobs:
            new_jobs = await self._scrape_page(seen_ids)
            jobs.extend(new_jobs)
            logger.info(f"Naukri: collected {len(jobs)} / {max_jobs} jobs (page {page_num})")

            if len(jobs) >= max_jobs or not new_jobs:
                break

            if not await self._next_page():
                logger.info("Naukri: no more pages")
                break
            page_num += 1
            await asyncio.sleep(random.uniform(2, 4))
            await self._dismiss_popups()

        return jobs[:max_jobs]

    # ── URL builder ────────────────────────────────────────────────────────────

    def _build_url(
        self,
        query: str,
        location: str,
        experience: int,
        date_posted: str,
    ) -> str:
        # Naukri slug format: /[keyword]-jobs-in-[location]
        slug = query.lower().replace(" ", "-")
        loc_slug = location.lower().replace(" ", "-") if location else ""
        job_path = f"{slug}-jobs-in-{loc_slug}" if loc_slug else f"{slug}-jobs"

        params: dict[str, str] = {}
        if experience:
            params["experience"] = str(experience)

        # jobAge in days
        _age_map = {"day": "1", "week": "7", "month": "30"}
        if date_posted in _age_map:
            params["jobAge"] = _age_map[date_posted]

        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        return f"{self._BASE}/{job_path}{qs}"

    # ── Popup dismissal ────────────────────────────────────────────────────────

    async def _dismiss_popups(self) -> None:
        """Close login prompts, notification banners, and cookie consent."""
        selectors = [
            # Naukri login nudge / close buttons
            "[class*='crossIcon']",
            "[class*='closeIcon']",
            "button.close",
            "span.cross",
            "[data-ga-track*='close' i]",
            # Cookie consent
            "button:has-text('Accept')",
            "button:has-text('Accept All')",
        ]
        for sel in selectors:
            try:
                btn = self._page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.5)
                    logger.debug(f"Naukri: dismissed popup via '{sel}'")
            except Exception:
                pass

    # ── Page scraping ──────────────────────────────────────────────────────────

    async def _scrape_page(self, seen_ids: set[str]) -> list[Job]:
        """Scrape all job cards on the current Naukri results page."""
        await self._page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(1)

        cards = await self._find_job_cards()
        if not cards:
            logger.warning("Naukri: no job cards found on this page")
            return []

        logger.debug(f"Naukri: found {len(cards)} job cards")
        jobs: list[Job] = []

        for card in cards:
            job = await self._parse_card(card, seen_ids)
            if job:
                jobs.append(job)
                seen_ids.add(job.job_id)
            await asyncio.sleep(random.uniform(0.5, 1.0))

        return jobs

    async def _find_job_cards(self) -> list:
        """Return a list of job card locators."""
        selectors = [
            "article.jobTuple",
            "div.srp-jobtuple-wrapper",
            ".cust-job-tuple",
            "[class*='jobTupleHeader']",
            "div.list > article",
        ]
        for sel in selectors:
            try:
                items = self._page.locator(sel)
                count = await items.count()
                if count > 0:
                    logger.debug(f"Naukri: cards via '{sel}' ({count})")
                    return [items.nth(i) for i in range(count)]
            except Exception as e:
                logger.debug(f"Naukri: selector '{sel}' failed: {e}")

        # Fallback: any element with data-job-id
        fallback = self._page.locator("[data-job-id]")
        count = await fallback.count()
        if count > 0:
            logger.debug(f"Naukri: fallback [data-job-id] ({count})")
            return [fallback.nth(i) for i in range(count)]

        return []

    async def _parse_card(self, card, seen_ids: set[str]) -> Optional[Job]:
        """Extract job data from a single Naukri card."""
        try:
            job_id = await self._extract_job_id(card)
            if not job_id or job_id in seen_ids:
                return None

            title   = await self._card_text(card, [
                "a.title",
                ".title.ellipsis",
                "a[data-ga-track*='title' i]",
                "h2.title",
            ])
            company = await self._card_text(card, [
                "a.comp-name",
                ".comp-name",
                "[class*='companyName']",
            ])
            location = await self._card_text(card, [
                "span.locWdth",
                ".location",
                "[class*='location']",
            ])

            # Get the job detail URL from the title link
            job_url = await self._extract_job_url(card)
            if not job_url:
                return None

            # Skip jobs that redirect to external company sites (no Naukri apply)
            if await self._is_external_apply(card):
                logger.debug(f"Naukri: skipping external-apply job {job_id}")
                return None

            # Fetch description by navigating to job detail page
            description = await self._fetch_description(job_url)

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
            logger.warning(f"Naukri: failed to parse card: {e}")
            return None

    # ── Field helpers ──────────────────────────────────────────────────────────

    async def _extract_job_id(self, card) -> str:
        for attr in ["data-job-id", "data-jobid", "id"]:
            val = await card.get_attribute(attr) or ""
            if val and val.strip():
                return val.strip()
        # Try title link href: /[company]-[title]-[id]
        try:
            link = card.locator("a.title, a[href*='naukri.com/job']").first
            if await link.count() > 0:
                href = await link.get_attribute("href") or ""
                # Naukri job IDs are typically a long number at the end of the URL
                parts = href.rstrip("/").split("-")
                if parts and parts[-1].isdigit():
                    return parts[-1]
                # Some URLs have ?src=... — extract path segment
                path = href.split("?")[0].rstrip("/")
                last = path.split("-")[-1]
                if last.isdigit():
                    return last
        except Exception:
            pass
        return ""

    async def _extract_job_url(self, card) -> str:
        try:
            link = card.locator("a.title, a[href*='-jobs-in-'], a[href*='naukri.com/job']").first
            if await link.count() > 0:
                href = await link.get_attribute("href") or ""
                if href:
                    return href if href.startswith("http") else f"{self._BASE}{href}"
        except Exception:
            pass
        return ""

    async def _is_external_apply(self, card) -> bool:
        """Return True if the card's apply button leads to a company site."""
        try:
            ext = card.locator(
                "a[href*='apply']:not([href*='naukri']), "
                "span:has-text('Apply on company site'), "
                "[class*='applyRedirect']"
            )
            return await ext.count() > 0
        except Exception:
            return False

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

    async def _fetch_description(self, job_url: str) -> str:
        """Open the job detail page in a new tab to get the description."""
        try:
            detail_page = await self._page.context.new_page()
            await detail_page.goto(job_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.5, 2.5))

            desc_selectors = [
                ".job-desc",
                "#job-desc",
                "[class*='jobDescription']",
                ".dang-inner-html",
                ".styles_job-desc-cont__YyzfA",
            ]
            description = ""
            for sel in desc_selectors:
                try:
                    el = detail_page.locator(sel).first
                    if await el.count() > 0:
                        description = (await el.inner_text()).strip()[:3000]
                        if description:
                            break
                except Exception:
                    continue

            await detail_page.close()
            return description
        except Exception as e:
            logger.debug(f"Naukri: could not fetch description for {job_url}: {e}")
            return ""

    # ── Pagination ─────────────────────────────────────────────────────────────

    async def _next_page(self) -> bool:
        try:
            next_btn = self._page.locator(
                "a[class*='pagination-next']",
            ).first
            if await next_btn.count() == 0:
                # Try the > arrow or "Next" link
                next_btn = self._page.locator(
                    "a[href*='&pageNo='], "
                    "span.pagination-btn:has-text('>')"
                ).last
            if await next_btn.count() > 0 and await next_btn.is_visible():
                await next_btn.click()
                await self._page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(random.uniform(2, 3))
                return True
        except Exception as e:
            logger.debug(f"Naukri: pagination error: {e}")
        return False
