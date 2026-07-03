import asyncio
import re
import random
from pathlib import Path

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.models.applicant import Applicant
from src.models.job import Job

MAX_STEPS = 12

# Questions that should receive the cover letter text when a textarea is detected
_COVER_LETTER_KEYWORDS = frozenset(
    ["cover letter", "motivation", "why this role", "why are you", "tell us about yourself"]
)


class EasyApplyError(Exception):
    """Raised when the Easy Apply flow fails at any step."""


class AlreadyAppliedError(Exception):
    """Raised when the job has already been applied to on LinkedIn."""


class EasyApplyHandler:
    """Drives LinkedIn's multi-step Easy Apply modal from open to submit.

    Pass an optional `llm` (OllamaClient) to enable intelligent screening-question
    answering. Without it the handler falls back to safe heuristic defaults.
    """

    _MODAL_SELECTOR = (
        ".jobs-easy-apply-modal, "
        "[data-test-modal], "
        "div[role='dialog']:has(button[aria-label*='Dismiss']), "
        "div[role='dialog']:has(h3)"
    )

    def __init__(self, page: Page, applicant: Applicant, llm=None) -> None:
        self._page = page
        self._applicant = applicant
        self._llm = llm
        self._cover_letter: str = ""

    async def apply(self, job: Job, resume_pdf_path: Path, cover_letter: str = "") -> None:
        """Complete the full Easy Apply flow for a given job.

        Steps:
        1. Detect "Already Applied" badge — raise AlreadyAppliedError if found.
        2. Click Easy Apply to open the modal.
        3. Loop through each modal step, detect form type, and fill it.
        4. Click Next / Review / Submit as appropriate.
        """
        self._cover_letter = cover_letter

        await self._assert_not_already_applied(job)
        await self._open_modal(job)

        for step_num in range(1, MAX_STEPS + 1):
            logger.debug(f"Easy Apply step {step_num}")
            step_type = await self._detect_step_type()

            if step_type == "submitted":
                logger.success(f"Application submitted for {job.title} at {job.company}")
                await self._close_modal()
                return
            elif step_type == "contact":
                await self._fill_contact_info()
            elif step_type == "resume":
                await self._upload_resume(resume_pdf_path)
            elif step_type == "questions":
                await self._fill_screening_questions(job)
            elif step_type == "review":
                await self._handle_review_step()
            else:
                logger.debug(f"Unknown step '{step_type}' — attempting generic fill")
                await self._fill_screening_questions(job)

            action = await self._detect_action_button()
            if action == "submit":
                await self._click_button_by_label("Submit application")
                await asyncio.sleep(2)
                logger.success(f"Submitted: {job.title} @ {job.company}")
                await self._close_modal()
                return
            elif action == "review":
                await self._click_button_by_label("Review your application")
            elif action == "next":
                await self._click_button_by_label("Continue to next step")
            else:
                screenshot = f"data/screenshots/stuck_{job.job_id}_step{step_num}.png"
                await self._page.screenshot(path=screenshot)
                raise EasyApplyError(
                    f"No action button found on step {step_num}. Screenshot: {screenshot}"
                )

            await asyncio.sleep(random.uniform(1.0, 2.0))

        raise EasyApplyError(f"Easy Apply exceeded {MAX_STEPS} steps — aborting")

    # ── Already Applied guard ──────────────────────────────────────────────────

    async def _assert_not_already_applied(self, job: Job) -> None:
        """Raise AlreadyAppliedError if LinkedIn shows this job was already applied to."""
        try:
            # "Applied" text near the apply button area
            applied_indicator = self._page.locator(
                ".jobs-s-apply__application-link, "
                "[class*='applied-badge'], "
                ".artdeco-inline-feedback:has-text('Applied')"
            )
            if await applied_indicator.count() > 0:
                raise AlreadyAppliedError(f"Job {job.job_id} already applied on LinkedIn")

            # The Easy Apply button replaced by a plain "Applied" button
            applied_btn = self._page.locator(
                "button[aria-label*='Applied'], button:has-text('Applied')"
            )
            if await applied_btn.count() > 0:
                raise AlreadyAppliedError(f"Job {job.job_id} already applied on LinkedIn")

        except AlreadyAppliedError:
            raise
        except Exception:
            pass

    # ── Modal lifecycle ────────────────────────────────────────────────────────

    async def _open_modal(self, job: Job) -> None:
        """Navigate to the search results, select the job card, click Easy Apply.

        LinkedIn's /jobs/view/ page renders the apply button as an <a> element
        which our button selectors cannot reach.  The split-panel search view
        reliably exposes it as a clickable element in the right detail panel —
        the same surface used by _detect_easy_apply() during scraping.
        """
        try:
            # Build the search URL that shows this specific job in the right panel.
            # job.url was captured from self._page.url right after the card was
            # clicked during scraping, so it already carries currentJobId.
            search_url = self._build_search_url_for_job(job)
            logger.debug(f"Navigating to search view for job {job.job_id}: {search_url}")
            await self._page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2, 3))

            # Click the specific job card so its detail loads in the right panel.
            await self._click_job_card(job)
            await asyncio.sleep(random.uniform(2, 3))

            await self._page.screenshot(
                path=f"data/screenshots/before_apply_{job.job_id}.png"
            )

            # Find the Easy Apply button in the right detail panel.
            apply_btn = await self._find_visible_apply_button()
            if apply_btn is None:
                raise PlaywrightTimeoutError("No visible Easy Apply button found in right panel")

            aria = await apply_btn.get_attribute("aria-label") or ""
            txt  = (await apply_btn.inner_text()).strip()
            logger.debug(f"Clicking Easy Apply | aria-label='{aria}' text='{txt}'")
            await apply_btn.click()

            await asyncio.sleep(2)
            await self._page.screenshot(
                path=f"data/screenshots/after_click_{job.job_id}.png"
            )
            dialogs = await self._page.locator("div[role='dialog']").count()
            logger.debug(f"Post-click URL: {self._page.url} | dialogs={dialogs}")

            modal = self._page.locator(self._MODAL_SELECTOR)
            await modal.wait_for(state="visible", timeout=15_000)
            logger.debug("Easy Apply modal opened")
        except PlaywrightTimeoutError as e:
            screenshot = f"data/screenshots/modal_fail_{job.job_id}.png"
            await self._page.screenshot(path=screenshot)
            raise EasyApplyError(
                f"Could not open Easy Apply modal for job {job.job_id}"
            ) from e

    async def _close_modal(self) -> None:
        """Dismiss the post-submit confirmation modal if present."""
        try:
            close_btn = self._page.get_by_role("button", name="Dismiss")
            if await close_btn.count() > 0:
                await close_btn.first.click()
        except Exception:
            pass

    def _build_search_url_for_job(self, job: Job) -> str:
        """Return the LinkedIn search URL with currentJobId so the right panel
        pre-loads the correct job detail."""
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

        base = "https://www.linkedin.com/jobs/search/"
        # Re-use the params from the stored URL if available, otherwise defaults
        if job.url and "linkedin.com/jobs/search" in job.url:
            parsed = urlparse(job.url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            # Force currentJobId to this job
            params["currentJobId"] = [job.job_id]
            # Remove f_LF (Easy Apply filter) so the job is always findable
            params.pop("f_LF", None)
            query = urlencode({k: v[0] for k, v in params.items()})
            return urlunparse(parsed._replace(query=query))

        return f"{base}?keywords=Software+Engineer&currentJobId={job.job_id}"

    async def _click_job_card(self, job: Job) -> None:
        """Click the job card matching job.job_id in the search results list."""
        selectors = [
            f"li[data-occludable-job-id='{job.job_id}']",
            f"li[data-job-id='{job.job_id}']",
            f"[data-entity-urn*='{job.job_id}']",
            f"a[href*='/jobs/view/{job.job_id}/']",
        ]
        for sel in selectors:
            card = self._page.locator(sel).first
            if await card.count() > 0:
                await card.click()
                logger.debug(f"Clicked job card via selector: {sel}")
                return
        # Card not in list (may have been auto-selected via currentJobId already)
        logger.debug(f"Job card {job.job_id} not found in list — relying on currentJobId pre-selection")

    async def _find_visible_apply_button(self):
        """Return the first Playwright Locator for a visible Easy Apply button.

        Playwright locators pierce Shadow DOM automatically, unlike
        document.querySelectorAll(). We try several selector strategies and
        return the first element that is_visible() returns True for.
        """
        strategies = [
            self._page.get_by_role("button", name="Easy Apply"),
            self._page.locator("button[aria-label*='Easy Apply']"),
            self._page.locator("a[aria-label*='Easy Apply']"),
            self._page.locator(".jobs-apply-button"),
            self._page.locator("[data-control-name='jobdetails_topcard_inapply']"),
            self._page.get_by_role("link", name="Easy Apply"),
        ]
        for locator in strategies:
            count = await locator.count()
            logger.debug(f"Strategy '{locator}': {count} matches")
            for i in range(count):
                candidate = locator.nth(i)
                if await candidate.is_visible():
                    return candidate
        return None

    async def _dismiss_premium_popup(self) -> None:
        """Close the 'Job search smarter with Premium' sidebar that appears on
        /jobs/view/ pages and can intercept pointer events."""
        try:
            close = self._page.locator(
                "button[aria-label='Dismiss'], "
                "button[aria-label='Close'], "
                "button.artdeco-card__dismiss, "
                "button.premium-upsell-link__dismiss-button"
            )
            if await close.count() > 0:
                await close.first.click()
                await asyncio.sleep(0.5)
                logger.debug("Dismissed Premium popup")
        except Exception:
            pass

    # ── Step detection ─────────────────────────────────────────────────────────

    async def _detect_step_type(self) -> str:
        """Infer the current modal step from visible form elements.

        Returns: submitted | review | resume | contact | questions | unknown
        """
        modal = self._page.locator(self._MODAL_SELECTOR)

        if await modal.locator("text=Your application was sent").count() > 0:
            return "submitted"
        if await modal.locator("text=Review your application").count() > 0:
            return "review"
        if await modal.locator("input[type='file'], label[for*='resume']").count() > 0:
            return "resume"

        for label in ["Phone country code", "Mobile phone number", "Email address"]:
            if await modal.get_by_label(label).count() > 0:
                return "contact"

        if await modal.locator("textarea, select, input[type='radio']").count() > 0:
            return "questions"

        return "unknown"

    async def _detect_action_button(self) -> str:
        """Return the active primary button label: submit | review | next | unknown."""
        modal = self._page.locator(self._MODAL_SELECTOR)

        for label in ["Submit application", "Submit Application"]:
            btn = modal.get_by_role("button", name=label)
            if await btn.count() > 0 and not await btn.is_disabled():
                return "submit"

        for label in ["Review your application", "Review"]:
            btn = modal.get_by_role("button", name=label)
            if await btn.count() > 0 and not await btn.is_disabled():
                return "review"

        for label in ["Continue to next step", "Next"]:
            btn = modal.get_by_role("button", name=label)
            if await btn.count() > 0 and not await btn.is_disabled():
                return "next"

        return "unknown"

    # ── Step handlers ──────────────────────────────────────────────────────────

    async def _fill_contact_info(self) -> None:
        """Populate phone / email fields in the contact info step."""
        modal = self._page.locator(self._MODAL_SELECTOR)

        phone_field = modal.get_by_label("Mobile phone number")
        if await phone_field.count() > 0:
            await phone_field.clear()
            await phone_field.fill(self._applicant.phone)

        email_field = modal.get_by_label("Email address")
        if await email_field.count() > 0:
            if not await email_field.input_value():
                await email_field.fill(self._applicant.email)

        logger.debug("Filled contact info")

    async def _upload_resume(self, resume_path: Path) -> None:
        """Upload the tailored PDF resume via the hidden file input.

        LinkedIn shows two file inputs on some steps: one for the resume and one
        for a cover letter.  Target the resume input specifically by aria-label,
        falling back to the first file input if no labelled one is found.
        """
        modal = self._page.locator(self._MODAL_SELECTOR)

        # Prefer the input labelled "Upload resume" to avoid the cover-letter input
        resume_input = modal.locator("input[type='file'][id*='resume']")
        if await resume_input.count() == 0:
            resume_input = modal.get_by_label("Upload resume", exact=True)
        if await resume_input.count() == 0:
            resume_input = modal.locator("input[type='file']").first

        if await resume_input.count() == 0:
            logger.warning("No file input on resume step — skipping upload")
            return

        await resume_input.set_input_files(str(resume_path.resolve()))
        await asyncio.sleep(1.5)
        logger.debug(f"Uploaded resume: {resume_path.name}")

    async def _fill_screening_questions(self, job: Job) -> None:
        """Answer every visible form question on the current step.

        Uses LLM if available; falls back to safe heuristics otherwise.
        Handles four input types: radio groups, dropdowns, text/number inputs,
        and textareas (including cover letter fields).
        """
        modal = self._page.locator(self._MODAL_SELECTOR)

        # Iterate through question groupings (each wraps one question + input)
        groups = modal.locator(
            ".jobs-easy-apply-form-section__grouping, "
            ".fb-dash-form-element, "
            "[data-test-form-element]"
        )
        group_count = await groups.count()

        if group_count > 0:
            for i in range(group_count):
                group = groups.nth(i)
                await self._fill_single_group(group, job)
        else:
            # Fallback: handle elements directly when no grouping containers exist
            await self._fill_ungrouped_elements(modal, job)

    async def _fill_single_group(self, group, job: Job) -> None:
        """Fill one form group (label + input pair)."""
        question = await self._extract_question_text(group)

        if await group.locator("input[type='radio']").count() > 0:
            await self._handle_radio(group, question, job)
        elif await group.locator("select").count() > 0:
            await self._handle_select(group, question, job)
        elif await group.locator("textarea").count() > 0:
            await self._handle_textarea(group, question, job)
        elif await group.locator("input[type='text'], input[type='number']").count() > 0:
            await self._handle_text_input(group, question, job)

    async def _fill_ungrouped_elements(self, modal, job: Job) -> None:
        """Handle forms that don't use grouping containers."""
        # Radio groups
        yes_radios = modal.locator("label:has-text('Yes')")
        for i in range(await yes_radios.count()):
            await yes_radios.nth(i).click()
            await asyncio.sleep(0.3)

        # Dropdowns
        selects = modal.locator("select")
        for i in range(await selects.count()):
            select = selects.nth(i)
            options = await select.locator("option").all_inner_texts()
            for opt in options:
                if opt.strip() and "select" not in opt.lower():
                    await select.select_option(label=opt)
                    break

        # Numeric inputs
        number_inputs = modal.locator("input[type='text'][id*='numeric'], input[type='number']")
        for i in range(await number_inputs.count()):
            field = number_inputs.nth(i)
            label = await self._get_field_label(field)
            value = await self._get_answer(label, "number", job)
            await field.clear()
            await field.fill(value)

        # Text areas
        textareas = modal.locator("textarea")
        for i in range(await textareas.count()):
            ta = textareas.nth(i)
            if not await ta.input_value():
                label = await self._get_field_label(ta)
                answer = await self._get_answer(label, "text", job)
                await ta.fill(answer)

    # ── Input type handlers ────────────────────────────────────────────────────

    async def _handle_radio(self, group, question: str, job: Job) -> None:
        answer = await self._get_answer(question, "yes_no", job)
        labels = group.locator("label")
        for i in range(await labels.count()):
            label = labels.nth(i)
            text = (await label.inner_text()).strip().lower()
            if text == answer.lower():
                # Always click the <label> — it intercepts pointer events on the <input>
                await label.click()
                logger.debug(f"Radio '{question}' → '{answer}'")
                return
        # Default: click the label of the first radio option (not the input itself)
        first_label = group.locator("label").first
        if await first_label.count() > 0:
            await first_label.click()
        else:
            first_input = group.locator("input[type='radio']").first
            if await first_input.count() > 0:
                await first_input.click(force=True)

    async def _handle_select(self, group, question: str, job: Job) -> None:
        select = group.locator("select").first
        options = await select.locator("option").all_inner_texts()
        valid = [o for o in options if o.strip() and "select" not in o.lower()]
        if not valid:
            return

        if self._llm:
            answer = await self._get_answer(
                f"{question} (options: {', '.join(valid)})", "select", job
            )
            # Match closest option (case-insensitive prefix)
            matched = next(
                (o for o in valid if answer.lower() in o.lower() or o.lower() in answer.lower()),
                valid[0],
            )
            await select.select_option(label=matched)
            logger.debug(f"Select '{question}' → '{matched}'")
        else:
            await select.select_option(label=valid[0])
            logger.debug(f"Select '{question}' → '{valid[0]}' (default first option)")

    async def _handle_textarea(self, group, question: str, job: Job) -> None:
        ta = group.locator("textarea").first
        if await ta.input_value():
            return  # already filled

        # Use stored cover letter if question is about motivation / cover letter
        if self._cover_letter and any(kw in question.lower() for kw in _COVER_LETTER_KEYWORDS):
            await ta.fill(self._cover_letter)
            logger.debug(f"Textarea '{question}' → [cover letter]")
            return

        answer = await self._get_answer(question, "text", job)
        await ta.fill(answer)
        logger.debug(f"Textarea '{question}' → filled")

    async def _handle_text_input(self, group, question: str, job: Job) -> None:
        field = group.locator("input[type='text'], input[type='number']").first
        if await field.input_value():
            return  # already filled

        input_type = "number" if "year" in question.lower() or "salary" in question.lower() \
            or "experience" in question.lower() else "text"
        answer = await self._get_answer(question, input_type, job)
        await field.clear()
        await field.fill(answer)
        logger.debug(f"Input '{question}' → '{answer}'")

    # ── LLM / heuristic answer dispatch ───────────────────────────────────────

    async def _get_answer(self, question: str, input_type: str, job: Job) -> str:
        """Return the best answer for a question — LLM if available, heuristics otherwise."""
        if self._llm:
            try:
                return await self._llm_answer(question, input_type, job)
            except Exception as e:
                logger.warning(f"LLM answer failed for '{question}': {e} — using heuristic")

        return self._heuristic_answer(question, input_type)

    async def _llm_answer(self, question: str, input_type: str, job: Job) -> str:
        """Ask Ollama to answer a screening question based on job and applicant context."""
        type_instructions = {
            "yes_no": "Reply with exactly one word: Yes or No.",
            "number": "Reply with a single integer. No units, no words.",
            "select": "Reply with exactly one of the provided option values, copied verbatim.",
            "text": "Reply with 1-2 sentences only. No preamble, no sign-off.",
        }
        instruction = type_instructions.get(input_type, "Reply with a concise answer only.")

        prompt = f"""\
You are filling out a job application for {self._applicant.name}.

Job: {job.title} at {job.company}
Question: "{question}"
Format: {input_type}

Instruction: {instruction}
Rules:
- Do NOT include any preamble like "Here is..." or "I am happy to...".
- Base answers on a {job.title} with 4+ years of experience.
- Never fabricate certifications, visa status, or legal claims.
- For authorization / background-check yes/no questions, answer Yes.

Answer (raw value only):\
"""
        raw = await self._llm.generate(prompt)
        # Strip preamble patterns like "Here's a professional answer: ..."
        raw = re.sub(r"(?i)^(here['\u2019]?s?\s+\w+\s+\w*:?\s*|answer\s*:\s*)", "", raw)
        return re.sub(r"^[\s\*_`#]+|[\s\*_`#]+$", "", raw).strip()

    def _heuristic_answer(self, question: str, input_type: str) -> str:
        """Rule-based fallback answers when the LLM is unavailable."""
        q = question.lower()

        if input_type == "yes_no":
            # Default Yes for positive/authorization questions
            return "Yes"

        if input_type == "number":
            if "year" in q and "experience" in q:
                return "4"
            if "salary" in q or "compensation" in q:
                return "120000"
            if "notice" in q:
                return "30"
            return "1"

        # text / select fallback
        return "Please refer to my resume and LinkedIn profile for details."

    async def _handle_review_step(self) -> None:
        """Log any validation errors shown on the review step."""
        modal = self._page.locator(self._MODAL_SELECTOR)
        errors = modal.locator(".artdeco-inline-feedback--error, [data-test-inline-feedback]")
        count = await errors.count()
        if count > 0:
            msgs = [await errors.nth(i).inner_text() for i in range(count)]
            logger.warning(f"Review step has {count} error(s): {msgs}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _click_button_by_label(self, label: str) -> None:
        modal = self._page.locator(self._MODAL_SELECTOR)
        btn = modal.get_by_role("button", name=label)
        await btn.first.click()
        await asyncio.sleep(random.uniform(0.5, 1.2))

    async def _extract_question_text(self, group) -> str:
        """Return label or legend text for a form group."""
        try:
            legend = group.locator("legend")
            if await legend.count() > 0:
                return (await legend.first.inner_text()).strip()
            label = group.locator("label").first
            if await label.count() > 0:
                return (await label.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def _get_field_label(self, input_locator) -> str:
        """Return the <label> text associated with an input element by id."""
        try:
            input_id = await input_locator.get_attribute("id") or ""
            if input_id:
                label = self._page.locator(f"label[for='{input_id}']")
                if await label.count() > 0:
                    return (await label.first.inner_text()).strip()
        except Exception:
            pass
        return ""
