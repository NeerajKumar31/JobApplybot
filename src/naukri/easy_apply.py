"""Naukri Easy Apply handler — drives Naukri's apply modal/flow to submission."""

import asyncio
import random
import re
from pathlib import Path

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.models.applicant import Applicant
from src.models.job import Job

MAX_STEPS = 10


class NaukriApplyError(Exception):
    """Raised when the Naukri apply flow fails."""


class AlreadyAppliedError(Exception):
    """Raised when Naukri shows the job was already applied to."""


class NaukriEasyApplyHandler:
    """Drives Naukri's apply modal from open to submit."""

    def __init__(self, page: Page, applicant: Applicant, llm=None) -> None:
        self._page      = page
        self._applicant = applicant
        self._llm       = llm

    # ── Public API ─────────────────────────────────────────────────────────────

    async def apply(self, job: Job, resume_pdf_path: Path, cover_letter: str = "") -> None:
        """Open the Naukri apply form and drive it to submission."""
        # Navigate to the job page
        if job.url:
            await self._page.goto(job.url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2, 3))

        await self._dismiss_popups()
        await self._assert_not_already_applied(job)
        await self._page.screenshot(
            path=f"data/screenshots/naukri_before_apply_{job.job_id}.png"
        )

        # Click the Apply button
        await self._click_apply_button(job)
        await asyncio.sleep(2)

        # Drive the multi-step form
        await self._run_step_loop(job, resume_pdf_path, cover_letter)

    # ── Guard: already applied ─────────────────────────────────────────────────

    async def _assert_not_already_applied(self, job: Job) -> None:
        try:
            indicators = self._page.locator(
                "button:has-text('Applied'), "
                "span:has-text('Applied'), "
                "[class*='already-applied'], "
                "div:has-text('You have already applied')"
            )
            if await indicators.count() > 0:
                raise AlreadyAppliedError(f"Naukri job {job.job_id} already applied")
        except AlreadyAppliedError:
            raise
        except Exception:
            pass

    # ── Apply button ───────────────────────────────────────────────────────────

    async def _click_apply_button(self, job: Job) -> None:
        """Find and click the Apply / Apply Now button on the job detail page."""
        strategies = [
            self._page.locator("button#apply-button"),
            self._page.locator("button.apply-button"),
            self._page.locator("[class*='applyBtn']"),
            self._page.get_by_role("button", name="Apply"),
            self._page.get_by_role("button", name="Apply now"),
            self._page.locator("a#apply-button"),
            self._page.locator("a.apply-button"),
        ]
        for loc in strategies:
            try:
                count = await loc.count()
                for i in range(count):
                    btn = loc.nth(i)
                    if await btn.is_visible():
                        txt = (await btn.inner_text()).strip().lower()
                        # Skip "already applied" badges
                        if "already" in txt:
                            continue
                        await btn.click()
                        logger.debug(f"Naukri: clicked apply button: '{txt}'")
                        return
            except Exception:
                continue

        screenshot = f"data/screenshots/naukri_no_apply_btn_{job.job_id}.png"
        await self._page.screenshot(path=screenshot)
        raise NaukriApplyError(
            f"Could not find Apply button for {job.job_id} — see {screenshot}"
        )

    # ── Step loop ──────────────────────────────────────────────────────────────

    async def _run_step_loop(
        self,
        job: Job,
        resume_pdf_path: Path,
        cover_letter: str,
    ) -> None:
        """Drive the multi-step Naukri apply modal to completion."""
        for step_num in range(1, MAX_STEPS + 1):
            logger.debug(f"Naukri step {step_num}")

            if await self._is_submitted():
                logger.success(f"Naukri: application submitted for {job.title} @ {job.company}")
                return

            # Pause for CAPTCHA / OTP if present
            await self._wait_for_challenge(job, step_num)

            await self._fill_step(job, resume_pdf_path, cover_letter)

            action = await self._detect_action()
            if action == "submit":
                await self._click_action_button(
                    "Submit", "Submit application", "Apply"
                )
                await asyncio.sleep(2)
                if await self._is_submitted():
                    logger.success(f"Naukri: submitted {job.title} @ {job.company}")
                    return
            elif action == "continue":
                await self._click_action_button(
                    "Continue", "Next", "Save and continue", "Proceed"
                )
            else:
                # One final CAPTCHA check before giving up
                await self._wait_for_challenge(job, step_num, warn=False)
                action = await self._detect_action()
                if action in ("submit", "continue"):
                    continue

                try:
                    screenshot = f"data/screenshots/naukri_stuck_{job.job_id}_step{step_num}.png"
                    await self._page.screenshot(path=screenshot)
                    raise NaukriApplyError(
                        f"No action button on step {step_num}. Screenshot: {screenshot}"
                    )
                except NaukriApplyError:
                    raise
                except Exception:
                    raise NaukriApplyError(
                        f"No action button on step {step_num} (page may have closed)"
                    )

            await asyncio.sleep(random.uniform(1.0, 2.0))

        raise NaukriApplyError(f"Exceeded {MAX_STEPS} steps — aborting")

    # ── Submitted detection ────────────────────────────────────────────────────

    async def _is_submitted(self) -> bool:
        try:
            indicators = self._page.locator(
                "div:has-text('Application submitted'), "
                "div:has-text('application was sent'), "
                "div:has-text('Applied successfully'), "
                "h2:has-text('Thank you'), "
                "[class*='success-screen'], "
                "[class*='successModal']"
            )
            return await indicators.count() > 0
        except Exception:
            return False

    # ── Action detection ───────────────────────────────────────────────────────

    async def _detect_action(self) -> str:
        """Return 'submit', 'continue', or 'unknown'."""
        submit_labels = [
            "Submit", "Submit application", "Apply", "Apply now",
        ]
        continue_labels = [
            "Continue", "Next", "Save and continue", "Proceed",
            "Save", "Next step",
        ]

        for label in submit_labels:
            try:
                btn = self._page.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    logger.debug(f"Naukri: detected submit button '{label}'")
                    return "submit"
            except Exception:
                pass

        for label in continue_labels:
            try:
                btn = self._page.get_by_role("button", name=label)
                if await btn.count() > 0 and not await btn.first.is_disabled():
                    logger.debug(f"Naukri: detected continue button '{label}'")
                    return "continue"
            except Exception:
                pass

        # Fallback: any enabled submit-type button in a modal
        try:
            modal_btn = self._page.locator(
                "[class*='modal'] button[type='submit'], "
                "[class*='popup'] button[type='submit']"
            ).first
            if await modal_btn.count() > 0 and not await modal_btn.is_disabled():
                txt = (await modal_btn.inner_text()).strip()
                logger.debug(f"Naukri: modal submit fallback text='{txt}'")
                return "submit" if any(
                    w in txt.lower() for w in ("submit", "apply")
                ) else "continue"
        except Exception:
            pass

        return "unknown"

    # ── Step filling ───────────────────────────────────────────────────────────

    async def _fill_step(
        self, job: Job, resume_pdf_path: Path, cover_letter: str
    ) -> None:
        """Fill whatever form fields are visible in the current apply step."""
        # Resume upload
        file_inputs = self._page.locator("input[type='file']")
        if await file_inputs.count() > 0:
            try:
                await file_inputs.first.set_input_files(str(resume_pdf_path.resolve()))
                await asyncio.sleep(1.5)
                logger.debug(f"Naukri: uploaded resume {resume_pdf_path.name}")
            except Exception as e:
                logger.debug(f"Naukri: resume upload failed: {e}")
            return

        # Contact / profile fields
        await self._fill_contact_fields()

        # Cover note / message
        await self._fill_cover_note(job, cover_letter)

        # Screening questions
        await self._fill_questions(job)

    async def _fill_contact_fields(self) -> None:
        field_map = {
            "name":  self._applicant.name,
            "email": self._applicant.email,
            "phone": self._applicant.phone,
            "mobile": self._applicant.phone,
        }
        for keyword, value in field_map.items():
            try:
                field = self._page.locator(
                    f"input[name*='{keyword}' i], "
                    f"input[id*='{keyword}' i], "
                    f"input[placeholder*='{keyword}' i]"
                ).first
                if await field.count() > 0 and await field.is_visible():
                    existing = await field.input_value()
                    if not existing:
                        await field.fill(value)
                        logger.debug(f"Naukri: filled '{keyword}' field")
            except Exception as e:
                logger.debug(f"Naukri: could not fill '{keyword}': {e}")

    async def _fill_cover_note(self, job: Job, cover_letter: str) -> None:
        """Fill the cover note / message textarea."""
        try:
            ta = self._page.locator(
                "textarea[name*='cover' i], "
                "textarea[id*='cover' i], "
                "textarea[placeholder*='cover' i], "
                "textarea[name*='message' i]"
            ).first
            if await ta.count() > 0 and await ta.is_visible():
                existing = await ta.input_value()
                if not existing:
                    text = cover_letter or f"Please find my resume attached for the {job.title} position."
                    await ta.fill(text)
                    logger.debug("Naukri: filled cover note")
        except Exception as e:
            logger.debug(f"Naukri: cover note fill failed: {e}")

    async def _fill_questions(self, job: Job) -> None:
        """Answer any screening questions shown in the apply form."""
        # Radio buttons — prefer "Yes" for boolean questions
        radios = self._page.locator("input[type='radio'][value='Yes'], input[type='radio'][value='yes']")
        for i in range(await radios.count()):
            try:
                await radios.nth(i).click()
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Dropdowns
        selects = self._page.locator("select")
        for i in range(await selects.count()):
            try:
                sel = selects.nth(i)
                options = await sel.locator("option").all_inner_texts()
                valid = [o for o in options if o.strip() and "select" not in o.lower()]
                if valid:
                    await sel.select_option(label=valid[0])
            except Exception:
                pass

        # Text inputs (years of experience, salary, etc.)
        text_inputs = self._page.locator(
            "input[type='text']:not([name*='name' i]):not([name*='email' i]):not([name*='phone' i]), "
            "input[type='number']"
        )
        for i in range(await text_inputs.count()):
            try:
                field = text_inputs.nth(i)
                if await field.input_value():
                    continue
                if not await field.is_visible():
                    continue
                label = await self._get_label(field)
                answer = await self._get_answer(label, "text", job)
                await field.fill(answer)
            except Exception:
                pass

    # ── CAPTCHA / challenge wait ────────────────────────────────────────────────

    async def _wait_for_challenge(
        self, job: Job, step_num: int, warn: bool = True
    ) -> bool:
        """Wait up to 120 s if a CAPTCHA or OTP challenge is blocking the form."""
        challenge_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[title*='recaptcha' i]",
            "div.g-recaptcha",
            "iframe[src*='hcaptcha']",
            "div.h-captcha",
            "input[placeholder*='OTP' i]",
            "input[placeholder*='captcha' i]",
        ]

        async def _present() -> bool:
            for sel in challenge_selectors:
                try:
                    if await self._page.locator(sel).count() > 0:
                        return True
                except Exception:
                    pass
            return False

        if not await _present():
            return False

        if warn:
            logger.warning(
                f"Naukri: challenge detected on step {step_num} for "
                f"'{job.title} @ {job.company}'. "
                "Solve it in the browser window — waiting up to 120 seconds…"
            )

        for _ in range(40):
            await asyncio.sleep(3)
            if not await _present():
                logger.info("Naukri: challenge solved — resuming")
                await asyncio.sleep(1)
                return True

        logger.warning("Naukri: challenge not solved in 120 s — continuing anyway")
        return True

    # ── Popup dismissal ────────────────────────────────────────────────────────

    async def _dismiss_popups(self) -> None:
        for sel in [
            "[class*='crossIcon']", "[class*='closeIcon']",
            "button.close", "span.cross",
        ]:
            try:
                btn = self._page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

    # ── Click helpers ──────────────────────────────────────────────────────────

    async def _click_action_button(self, *labels: str) -> None:
        for label in labels:
            try:
                btn = self._page.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.click()
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    return
            except Exception:
                pass
        # Fallback: submit-type button in modal
        try:
            btn = self._page.locator(
                "[class*='modal'] button[type='submit'], "
                "button[type='submit']"
            ).first
            if await btn.count() > 0:
                await btn.click()
        except Exception:
            pass

    async def _get_label(self, input_locator) -> str:
        try:
            input_id = await input_locator.get_attribute("id") or ""
            if input_id:
                lbl = self._page.locator(f"label[for='{input_id}']")
                if await lbl.count() > 0:
                    return (await lbl.first.inner_text()).strip()
            # Try placeholder as label fallback
            return await input_locator.get_attribute("placeholder") or ""
        except Exception:
            return ""

    async def _get_answer(self, question: str, input_type: str, job: Job) -> str:
        if self._llm:
            try:
                return await self._llm_answer(question, input_type, job)
            except Exception as e:
                logger.warning(f"Naukri: LLM answer failed for '{question}': {e}")
        return self._heuristic_answer(question, input_type)

    async def _llm_answer(self, question: str, input_type: str, job: Job) -> str:
        type_instructions = {
            "yes_no": "Reply with exactly one word: Yes or No.",
            "number": "Reply with a single integer. No units, no words.",
            "text":   "Reply with 1-2 sentences only. No preamble, no sign-off.",
        }
        instruction = type_instructions.get(input_type, "Reply with a concise answer only.")
        prompt = (
            f"You are filling out a job application for {self._applicant.name}.\n"
            f"Job: {job.title} at {job.company}\n"
            f'Question: "{question}"\n'
            f"Instruction: {instruction}\n"
            "Do NOT include preamble. Answer (raw value only):"
        )
        raw = await self._llm.generate(prompt)
        raw = re.sub(r"(?i)^(here['\u2019]?s?\s+\w+\s+\w*:?\s*|answer\s*:\s*)", "", raw)
        return re.sub(r"^[\s\*_`#]+|[\s\*_`#]+$", "", raw).strip()

    def _heuristic_answer(self, question: str, input_type: str) -> str:
        q = question.lower()
        if input_type == "number":
            if "year" in q and "experience" in q:
                return "4"
            if "salary" in q or "ctc" in q:
                return "1200000"
            return "1"
        return "Please refer to my resume for details."
