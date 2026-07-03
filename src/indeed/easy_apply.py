"""Indeed Easy Apply handler — drives the IndeedApply iframe modal to submission."""

import asyncio
import random
import re
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, FrameLocator, Page, TimeoutError as PlaywrightTimeoutError

from src.models.applicant import Applicant
from src.models.job import Job

MAX_STEPS = 10

# Indeed's dedicated apply domain (full-page flow, no iframe)
_SMARTAPPLY_HOST = "smartapply.indeed.com"


class IndeedApplyError(Exception):
    """Raised when the Indeed apply flow fails."""


class AlreadyAppliedError(Exception):
    """Raised when Indeed shows the job was already applied to."""


class IndeedEasyApplyHandler:
    """Drives Indeed's multi-step IndeedApply iframe modal from open to submit.

    Indeed loads its apply flow inside an iframe. All interactions are scoped
    to that iframe's FrameLocator so the main page is never touched.
    """

    # Selectors for the IndeedApply iframe
    _IFRAME_SELECTORS = [
        "iframe[id*='indeedapply']",
        "iframe[title*='Apply']",
        "iframe[src*='indeedapply']",
        "iframe[src*='indeed.com/apply']",
        "div[id*='indeedapply'] iframe",
    ]

    def __init__(self, page: Page, applicant: Applicant, llm=None, context: BrowserContext | None = None) -> None:
        self._page      = page
        self._applicant = applicant
        self._llm       = llm
        self._context   = context

    # ── Public API ─────────────────────────────────────────────────────────────

    async def apply(self, job: Job, resume_pdf_path: Path, cover_letter: str = "") -> None:
        """Complete the full Indeed Easy Apply flow for a given job."""
        await self._assert_not_already_applied(job)

        # _open_apply_modal returns either:
        #   FrameLocator  – modal opened inside an iframe on the current page
        #   None          – apply opened as a full-page navigation (smartapply flow)
        frame = await self._open_apply_modal(job)

        if frame is None:
            # Smartapply / page-navigation flow — interact directly with self._page
            await self._run_step_loop(job, resume_pdf_path, cover_letter, frame=None)
        else:
            await self._run_step_loop(job, resume_pdf_path, cover_letter, frame=frame)

    async def _run_step_loop(
        self,
        job: Job,
        resume_pdf_path: Path,
        cover_letter: str,
        frame,           # FrameLocator or None (page-based)
    ) -> None:
        """Drive the multi-step apply form to completion."""
        # When frame is None we wrap self._page in a thin adapter so the rest of
        # the code works identically for both iframe and page-based flows.
        ctx = frame if frame is not None else _PageAsFrame(self._page)

        for step_num in range(1, MAX_STEPS + 1):
            logger.debug(f"Indeed step {step_num} (url={self._page.url})")

            if await self._is_submitted(ctx):
                logger.success(f"Indeed: application submitted for {job.title} @ {job.company}")
                if frame is not None:
                    await self._close_modal()
                return

            await self._fill_step(ctx, job, resume_pdf_path, cover_letter)

            action = await self._detect_action(ctx)
            if action == "submit":
                await self._click_in_frame(ctx,
                    "Submit your application", "Submit", "Apply")
                await asyncio.sleep(2)
                if await self._is_submitted(ctx):
                    logger.success(f"Indeed: submitted {job.title} @ {job.company}")
                    if frame is not None:
                        await self._close_modal()
                    return
            elif action == "continue":
                await self._click_in_frame(ctx,
                    "Continue", "Next", "Continue to apply",
                    "Review your application", "Next step")
            else:
                screenshot = f"data/screenshots/indeed_stuck_{job.job_id}_step{step_num}.png"
                await self._page.screenshot(path=screenshot)
                raise IndeedApplyError(
                    f"No action button on step {step_num}. Screenshot: {screenshot}"
                )

            await asyncio.sleep(random.uniform(1.0, 2.0))

        raise IndeedApplyError(f"Exceeded {MAX_STEPS} steps — aborting")

    # ── Guard: already applied ─────────────────────────────────────────────────

    async def _assert_not_already_applied(self, job: Job) -> None:
        try:
            indicator = self._page.locator(
                "span:has-text('Applied'), "
                "[class*='applied-label'], "
                "button:has-text('Applied')"
            )
            if await indicator.count() > 0:
                raise AlreadyAppliedError(f"Indeed job {job.job_id} already applied")
        except AlreadyAppliedError:
            raise
        except Exception:
            pass

    # ── Modal lifecycle ────────────────────────────────────────────────────────

    async def _open_apply_modal(self, job: Job):
        """Click the Apply button and return the iframe FrameLocator.

        Returns None when Indeed opens the apply flow as a full-page navigation
        to smartapply.indeed.com (i.e. no iframe, just a new URL).
        """
        try:
            if job.url and "indeed.com" in job.url:
                await self._page.goto(job.url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2, 3))

            await self._page.screenshot(
                path=f"data/screenshots/indeed_before_apply_{job.job_id}.png"
            )

            apply_btn = await self._find_apply_button()
            if apply_btn is None:
                raise PlaywrightTimeoutError("No visible Apply button found on Indeed page")

            label = await apply_btn.get_attribute("aria-label") or await apply_btn.inner_text() or ""
            logger.debug(f"Indeed: clicking apply button: '{label.strip()}'")

            # Watch for a new tab that some jobs open for external apply
            async with self._page.context.expect_page(timeout=4_000) as new_page_info:
                await apply_btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            logger.info(f"Indeed: apply opened in new tab → {new_page.url}")
            # Replace self._page so all subsequent interactions target the new tab
            self._page = new_page
            return None  # page-based flow in new tab

        except Exception:
            # No new tab opened — continue normally
            pass

        # Not a new tab — click and wait
        apply_btn = await self._find_apply_button()
        if apply_btn is None:
            screenshot = f"data/screenshots/indeed_modal_fail_{job.job_id}.png"
            await self._page.screenshot(path=screenshot)
            raise IndeedApplyError(f"No Apply button for {job.job_id} — see {screenshot}")

        await apply_btn.click()
        await asyncio.sleep(3)

        # Check for page navigation (smartapply full-page flow)
        if _SMARTAPPLY_HOST in self._page.url or "/apply/" in self._page.url:
            logger.info(f"Indeed: apply navigated to {self._page.url} — page-based flow")
            return None

        # Look for the IndeedApply iframe
        frame = await self._find_apply_iframe()
        if frame is not None:
            logger.debug("Indeed: apply iframe located")
            return frame

        # Still on the original page with no iframe — log what we see
        screenshot = f"data/screenshots/indeed_modal_fail_{job.job_id}.png"
        await self._page.screenshot(path=screenshot)
        raise IndeedApplyError(
            f"Could not open Indeed apply modal for {job.job_id} — see {screenshot}"
        )

    async def _find_apply_button(self):
        """Find the first visible Apply button on the Indeed job detail panel."""
        strategies = [
            self._page.locator("button#indeedApplyButton"),
            self._page.locator("[data-testid='indeedApplyButton']"),
            self._page.locator("a[data-testid='apply-button-container']"),
            self._page.get_by_role("button", name="Apply now"),
            self._page.get_by_role("link",   name="Apply now"),
            self._page.get_by_role("button", name="Apply"),
            self._page.locator("button.indeed-apply-button"),
            self._page.locator("[class*='applyButton']"),
            self._page.locator(".indeed-apply-widget button"),
        ]
        for loc in strategies:
            try:
                count = await loc.count()
                for i in range(count):
                    btn = loc.nth(i)
                    if await btn.is_visible():
                        return btn
            except Exception:
                continue
        return None

    async def _find_apply_iframe(self) -> FrameLocator | None:
        """Wait for and return the IndeedApply iframe FrameLocator, or None."""
        selectors = [
            "iframe[id*='indeedapply']",
            "iframe[title*='Apply']",
            "iframe[name*='apply']",
            "iframe[src*='indeedapply']",
            "iframe[src*='indeed.com/apply']",
            "iframe[src*='smartapply']",
            "div[id*='indeedapply'] iframe",
            "div[class*='indeed-apply'] iframe",
        ]
        for sel in selectors:
            try:
                iframe_el = self._page.locator(sel).first
                await iframe_el.wait_for(state="attached", timeout=8_000)
                frame = self._page.frame_locator(sel)
                await frame.locator("body").wait_for(state="visible", timeout=6_000)
                logger.debug(f"Indeed: iframe found via '{sel}'")
                return frame
            except PlaywrightTimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Indeed: iframe selector '{sel}' error: {e}")
                continue
        return None

    async def _close_modal(self) -> None:
        try:
            close = self._page.locator(
                "button[aria-label='close'], "
                "button[aria-label='Close'], "
                "button.icl-CloseButton"
            ).first
            if await close.count() > 0:
                await close.click()
        except Exception:
            pass

    # ── Step detection ─────────────────────────────────────────────────────────

    async def _is_submitted(self, ctx) -> bool:
        try:
            indicators = ctx.locator(
                "h1:has-text('application was sent'), "
                "h2:has-text('application was sent'), "
                "h1:has-text('Application submitted'), "
                "h2:has-text('Application submitted'), "
                "[class*='PostApply'], "
                "[data-testid*='success'], "
                "p:has-text('Your application has been submitted'), "
                "p:has-text('application has been sent')"
            )
            return await indicators.count() > 0
        except Exception:
            return False

    async def _detect_action(self, ctx) -> str:
        """Return 'submit', 'continue', or 'unknown' based on visible buttons."""
        submit_labels = [
            "Submit your application", "Submit application",
            "Submit", "Apply", "Send application",
        ]
        continue_labels = [
            "Continue", "Next", "Continue to apply",
            "Review your application", "Next step", "Save and continue",
        ]

        for label in submit_labels:
            try:
                btn = ctx.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    logger.debug(f"Indeed: detected submit button '{label}'")
                    return "submit"
            except Exception:
                pass

        for label in continue_labels:
            try:
                btn = ctx.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    logger.debug(f"Indeed: detected continue button '{label}'")
                    return "continue"
            except Exception:
                pass

        # Last resort: any enabled submit-type button
        try:
            btn = ctx.locator("button[type='submit']").first
            if await btn.count() > 0 and not await btn.is_disabled():
                txt = (await btn.inner_text()).strip()
                logger.debug(f"Indeed: fallback submit button text='{txt}'")
                return "submit" if any(
                    w in txt.lower() for w in ("submit", "apply", "send")
                ) else "continue"
        except Exception:
            pass

        return "unknown"

    # ── Step filling ───────────────────────────────────────────────────────────

    async def _fill_step(
        self,
        ctx,
        job: Job,
        resume_path: Path,
        cover_letter: str,
    ) -> None:
        """Fill whatever form elements are visible on the current step."""
        # Resume upload
        file_input = ctx.locator("input[type='file']")
        if await file_input.count() > 0:
            await file_input.set_input_files(str(resume_path.resolve()))
            await asyncio.sleep(1.5)
            logger.debug(f"Indeed: uploaded resume {resume_path.name}")
            return

        # Contact info fields
        await self._fill_contact_fields(ctx)

        # Screening questions
        await self._fill_questions(ctx, job, cover_letter)

    async def _fill_contact_fields(self, ctx) -> None:
        field_map = {
            "first": self._applicant.first_name,
            "last":  self._applicant.last_name,
            "email": self._applicant.email,
            "phone": self._applicant.phone,
        }
        for keyword, value in field_map.items():
            try:
                field = ctx.locator(
                    f"input[name*='{keyword}' i], "
                    f"input[id*='{keyword}' i], "
                    f"input[placeholder*='{keyword}' i]"
                ).first
                if await field.count() > 0:
                    existing = await field.input_value()
                    if not existing:
                        await field.fill(value)
                        logger.debug(f"Indeed: filled {keyword} field")
            except Exception as e:
                logger.debug(f"Indeed: could not fill {keyword}: {e}")

    async def _fill_questions(
        self, ctx, job: Job, cover_letter: str
    ) -> None:
        """Answer screening questions inside the apply form."""
        # Radio buttons — prefer "Yes" for boolean questions
        radios = ctx.locator("input[type='radio'][value='Yes'], label:has-text('Yes')")
        for i in range(await radios.count()):
            try:
                await radios.nth(i).click()
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Dropdowns
        selects = ctx.locator("select")
        for i in range(await selects.count()):
            try:
                sel = selects.nth(i)
                options = await sel.locator("option").all_inner_texts()
                valid = [o for o in options if o.strip() and "select" not in o.lower()]
                if valid:
                    await sel.select_option(label=valid[0])
            except Exception:
                pass

        # Text inputs
        text_inputs = ctx.locator("input[type='text'], input[type='number']")
        for i in range(await text_inputs.count()):
            try:
                field = text_inputs.nth(i)
                if await field.input_value():
                    continue
                label = await self._get_label(ctx, field)
                answer = await self._get_answer(label, "text", job)
                await field.fill(answer)
            except Exception:
                pass

        # Textareas (cover letter / open-ended)
        textareas = ctx.locator("textarea")
        for i in range(await textareas.count()):
            try:
                ta = textareas.nth(i)
                if await ta.input_value():
                    continue
                label = await self._get_label(ctx, ta)
                if cover_letter and any(
                    kw in label.lower() for kw in ["cover", "motivation", "why"]
                ):
                    await ta.fill(cover_letter)
                else:
                    answer = await self._get_answer(label, "text", job)
                    await ta.fill(answer)
            except Exception:
                pass

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _click_in_frame(self, ctx, *labels: str) -> None:
        for label in labels:
            try:
                btn = ctx.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.click()
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    return
            except Exception:
                pass
        # Fallback: any enabled submit-type button
        try:
            btn = ctx.locator("button[type='submit']").first
            if await btn.count() > 0:
                await btn.click()
        except Exception:
            pass

    async def _get_label(self, ctx, input_locator) -> str:
        try:
            input_id = await input_locator.get_attribute("id") or ""
            if input_id:
                lbl = ctx.locator(f"label[for='{input_id}']")
                if await lbl.count() > 0:
                    return (await lbl.first.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def _get_answer(self, question: str, input_type: str, job: Job) -> str:
        if self._llm:
            try:
                return await self._llm_answer(question, input_type, job)
            except Exception as e:
                logger.warning(f"Indeed: LLM answer failed for '{question}': {e}")
        return self._heuristic_answer(question, input_type)

    async def _llm_answer(self, question: str, input_type: str, job: Job) -> str:
        type_instructions = {
            "yes_no": "Reply with exactly one word: Yes or No.",
            "number": "Reply with a single integer. No units, no words.",
            "text":   "Reply with 1-2 sentences only. No preamble, no sign-off.",
        }
        instruction = type_instructions.get(input_type, "Reply with a concise answer only.")
        prompt = f"""\
You are filling out a job application for {self._applicant.name}.
Job: {job.title} at {job.company}
Question: "{question}"
Instruction: {instruction}
Do NOT include preamble. Answer (raw value only):\
"""
        raw = await self._llm.generate(prompt)
        raw = re.sub(r"(?i)^(here['\u2019]?s?\s+\w+\s+\w*:?\s*|answer\s*:\s*)", "", raw)
        return re.sub(r"^[\s\*_`#]+|[\s\*_`#]+$", "", raw).strip()

    def _heuristic_answer(self, question: str, input_type: str) -> str:
        q = question.lower()
        if input_type == "number":
            if "year" in q and "experience" in q:
                return "4"
            if "salary" in q or "compensation" in q:
                return "120000"
            return "1"
        return "Please refer to my resume for details."


class _PageAsFrame:
    """Thin adapter so `Page` can be used anywhere a `FrameLocator` is expected.

    FrameLocator and Page share the same `.locator()` / `.get_by_role()` API,
    so we just forward every attribute access to the underlying Page object.
    This lets `_run_step_loop` work identically for both iframe and page flows.
    """

    def __init__(self, page: Page) -> None:
        self._page = page

    def __getattr__(self, name: str):
        return getattr(self._page, name)
