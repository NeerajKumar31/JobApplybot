"""Indeed Easy Apply handler — drives the IndeedApply iframe modal to submission."""

import asyncio
import random
import re
from pathlib import Path

from loguru import logger
from playwright.async_api import FrameLocator, Page, TimeoutError as PlaywrightTimeoutError

from src.models.applicant import Applicant
from src.models.job import Job

MAX_STEPS = 10


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

    def __init__(self, page: Page, applicant: Applicant, llm=None) -> None:
        self._page      = page
        self._applicant = applicant
        self._llm       = llm

    # ── Public API ─────────────────────────────────────────────────────────────

    async def apply(self, job: Job, resume_pdf_path: Path, cover_letter: str = "") -> None:
        """Complete the full Indeed Easy Apply flow for a given job."""
        await self._assert_not_already_applied(job)
        frame = await self._open_apply_modal(job)

        for step_num in range(1, MAX_STEPS + 1):
            logger.debug(f"Indeed step {step_num}")

            if await self._is_submitted(frame):
                logger.success(f"Indeed: application submitted for {job.title} @ {job.company}")
                await self._close_modal()
                return

            await self._fill_step(frame, job, resume_pdf_path, cover_letter)

            action = await self._detect_action(frame)
            if action == "submit":
                await self._click_in_frame(frame, "Submit", "Submit your application")
                await asyncio.sleep(2)
                if await self._is_submitted(frame):
                    logger.success(f"Indeed: submitted {job.title} @ {job.company}")
                    await self._close_modal()
                    return
            elif action == "continue":
                await self._click_in_frame(frame, "Continue", "Next step")
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

    async def _open_apply_modal(self, job: Job) -> FrameLocator:
        """Click the Indeed Apply button and return the iframe FrameLocator."""
        try:
            # Navigate to the job page first
            if job.url and "indeed.com" in job.url:
                await self._page.goto(job.url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2, 3))

            await self._page.screenshot(
                path=f"data/screenshots/indeed_before_apply_{job.job_id}.png"
            )

            # Find and click the apply button
            apply_btn = await self._find_apply_button()
            if apply_btn is None:
                raise PlaywrightTimeoutError("No visible Apply button found on Indeed page")

            label = await apply_btn.get_attribute("aria-label") or ""
            logger.debug(f"Indeed: clicking apply button: '{label}'")
            await apply_btn.click()
            await asyncio.sleep(2)

            # Locate the iframe that Indeed loads the apply form in
            frame = await self._find_apply_iframe()
            logger.debug("Indeed: apply iframe located")
            return frame

        except PlaywrightTimeoutError as e:
            screenshot = f"data/screenshots/indeed_modal_fail_{job.job_id}.png"
            await self._page.screenshot(path=screenshot)
            raise IndeedApplyError(
                f"Could not open Indeed apply modal for {job.job_id}"
            ) from e

    async def _find_apply_button(self):
        """Find the first visible apply button on the Indeed job page."""
        strategies = [
            self._page.locator("button#indeedApplyButton"),
            self._page.locator("a[data-testid='apply-button-container']"),
            self._page.get_by_role("button", name="Apply now"),
            self._page.get_by_role("link",   name="Apply now"),
            self._page.locator("button.indeed-apply-button"),
            self._page.locator("[class*='applyButton']"),
            self._page.locator(".indeed-apply-widget button"),
        ]
        for loc in strategies:
            count = await loc.count()
            for i in range(count):
                btn = loc.nth(i)
                if await btn.is_visible():
                    return btn
        return None

    async def _find_apply_iframe(self) -> FrameLocator:
        """Wait for and return the IndeedApply iframe FrameLocator."""
        for sel in self._IFRAME_SELECTORS:
            try:
                iframe_el = self._page.locator(sel)
                await iframe_el.wait_for(state="attached", timeout=10_000)
                frame = self._page.frame_locator(sel)
                # Verify the iframe has content
                await frame.locator("body").wait_for(state="visible", timeout=8_000)
                return frame
            except PlaywrightTimeoutError:
                continue

        # Fallback: find any iframe that appeared after the click
        iframe_el = self._page.locator("iframe").last
        if await iframe_el.count() > 0:
            src = await iframe_el.get_attribute("src") or ""
            logger.debug(f"Indeed: using fallback iframe src={src}")
            return self._page.frame_locator("iframe:last-of-type")

        raise PlaywrightTimeoutError("IndeedApply iframe not found after clicking Apply")

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

    async def _is_submitted(self, frame: FrameLocator) -> bool:
        try:
            indicators = frame.locator(
                "h1:has-text('application was sent'), "
                "h2:has-text('application was sent'), "
                "[class*='success'], "
                "p:has-text('Your application has been submitted')"
            )
            return await indicators.count() > 0
        except Exception:
            return False

    async def _detect_action(self, frame: FrameLocator) -> str:
        """Return 'submit', 'continue', or 'unknown' based on visible buttons."""
        for label in ["Submit your application", "Submit"]:
            try:
                btn = frame.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    return "submit"
            except Exception:
                pass

        for label in ["Continue", "Next", "Review your application"]:
            try:
                btn = frame.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    return "continue"
            except Exception:
                pass

        return "unknown"

    # ── Step filling ───────────────────────────────────────────────────────────

    async def _fill_step(
        self,
        frame: FrameLocator,
        job: Job,
        resume_path: Path,
        cover_letter: str,
    ) -> None:
        """Fill whatever form elements are visible on the current step."""
        # Resume upload
        file_input = frame.locator("input[type='file']")
        if await file_input.count() > 0:
            await file_input.set_input_files(str(resume_path.resolve()))
            await asyncio.sleep(1.5)
            logger.debug(f"Indeed: uploaded resume {resume_path.name}")
            return

        # Contact info fields
        await self._fill_contact_fields(frame)

        # Screening questions
        await self._fill_questions(frame, job, cover_letter)

    async def _fill_contact_fields(self, frame: FrameLocator) -> None:
        field_map = {
            "first": self._applicant.first_name,
            "last":  self._applicant.last_name,
            "email": self._applicant.email,
            "phone": self._applicant.phone,
        }
        for keyword, value in field_map.items():
            try:
                field = frame.locator(
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
        self, frame: FrameLocator, job: Job, cover_letter: str
    ) -> None:
        """Answer screening questions inside the apply iframe."""
        # Radio buttons — prefer "Yes" for boolean questions
        radios = frame.locator("input[type='radio'][value='Yes'], label:has-text('Yes')")
        for i in range(await radios.count()):
            try:
                await radios.nth(i).click()
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Dropdowns
        selects = frame.locator("select")
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
        text_inputs = frame.locator("input[type='text'], input[type='number']")
        for i in range(await text_inputs.count()):
            try:
                field = text_inputs.nth(i)
                if await field.input_value():
                    continue
                label = await self._get_label(frame, field)
                answer = await self._get_answer(label, "text", job)
                await field.fill(answer)
            except Exception:
                pass

        # Textareas (cover letter / open-ended)
        textareas = frame.locator("textarea")
        for i in range(await textareas.count()):
            try:
                ta = textareas.nth(i)
                if await ta.input_value():
                    continue
                label = await self._get_label(frame, ta)
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

    async def _click_in_frame(self, frame: FrameLocator, *labels: str) -> None:
        for label in labels:
            try:
                btn = frame.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.click()
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    return
            except Exception:
                pass
        # Fallback: try submit-type button
        try:
            btn = frame.locator("button[type='submit']").first
            if await btn.count() > 0:
                await btn.click()
        except Exception:
            pass

    async def _get_label(self, frame: FrameLocator, input_locator) -> str:
        try:
            input_id = await input_locator.get_attribute("id") or ""
            if input_id:
                lbl = frame.locator(f"label[for='{input_id}']")
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
