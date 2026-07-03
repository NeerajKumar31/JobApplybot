"""Indeed authentication with cookie-based session persistence."""

import asyncio
import json
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


class IndeedAuthError(Exception):
    """Raised when Indeed login cannot be completed."""


class IndeedAuth:
    """Handles Indeed login and session restore via cookies."""

    _LOGIN_URL = "https://secure.indeed.com/account/login"
    _HOME_URL  = "https://www.indeed.com/"

    def __init__(self, page: Page, context: BrowserContext) -> None:
        self._page    = page
        self._context = context

    # ── Public API ─────────────────────────────────────────────────────────────

    async def login_with_session(
        self,
        email: str,
        password: str,
        cookie_path: Path,
    ) -> None:
        """Restore session from cookies when possible, fall back to form login."""
        if await self._try_restore_session(cookie_path):
            return
        await self._login_form(email, password)
        await self._save_cookies(cookie_path)

    # ── Session persistence ────────────────────────────────────────────────────

    async def _try_restore_session(self, cookie_path: Path) -> bool:
        if not cookie_path.exists():
            return False
        try:
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
            await self._context.add_cookies(cookies)
            logger.info("IndeedAuth: loaded saved cookies — verifying session…")
            await self._page.goto(self._HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            if await self._is_logged_in():
                logger.success("IndeedAuth: session restored — skipping login form")
                return True
            logger.info("IndeedAuth: cookies expired, falling back to form login")
        except Exception as e:
            logger.warning(f"IndeedAuth: cookie restore failed: {e}")
        return False

    async def _save_cookies(self, cookie_path: Path) -> None:
        try:
            cookies = await self._context.cookies()
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(
                json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.debug(f"IndeedAuth: saved {len(cookies)} cookies → {cookie_path}")
        except Exception as e:
            logger.warning(f"IndeedAuth: could not save cookies: {e}")

    # ── Form login ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_auth_url(url: str) -> bool:
        """Return True when the URL is an Indeed authentication / login page."""
        return "/auth" in url or "account/login" in url or "/login" in url

    async def _login_form(self, email: str, password: str) -> None:
        logger.info("IndeedAuth: navigating to login page…")
        await self._page.goto(self._LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        await self._page.screenshot(path="data/screenshots/indeed_login_page.png")

        # Fill email and click Continue.
        # _fill_email returns True when login is already complete (CAPTCHA flow),
        # False when the password step is still needed.
        login_complete = await self._fill_email(email)

        if not login_complete:
            await self._fill_password(password)
            await self._submit_login()

        await self._verify_login()

    async def _fill_email(self, email: str) -> bool:
        """Fill the email field and click Continue.

        Returns True when login is already complete after CAPTCHA (no password
        step required), False when the password step is still needed.
        """
        selectors = [
            "input#login-email-input",
            "input[name='__email']",
            "input[type='email']",
            "input[autocomplete='username']",
        ]
        for sel in selectors:
            el = self._page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill(email)
                logger.debug(f"IndeedAuth: filled email via {sel}")
                return await self._click_continue_button()
        raise IndeedAuthError("Could not find email input on Indeed login page")

    async def _click_continue_button(self) -> bool:
        """Click the email-form Continue button and wait for the next step.

        Indeed shows three "Continue" buttons: 'Continue with Google',
        'Continue with Apple', and the email-form submit button 'Continue →'.
        We must target button[type='submit'] — Google/Apple are type='button'.

        After clicking, Indeed sometimes shows a reCAPTCHA challenge before the
        password field.  We detect it and wait up to 120 s for manual resolution.

        Returns True when login is already complete (CAPTCHA redirected away from
        the auth flow), False when the password field appeared.
        """
        warned_captcha = False
        try:
            btn = self._page.locator("button[type='submit']").first
            if await btn.count() == 0:
                btn = self._page.locator(
                    "button[data-testid='email-form-submit'], "
                    "button[aria-label='Continue']"
                ).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                logger.debug("IndeedAuth: clicked email Continue — waiting for next step…")

                # Poll every 3 s for up to 120 s
                for _ in range(40):
                    await asyncio.sleep(3)

                    # Left the auth page → login complete (CAPTCHA or magic-link flow)
                    if not self._is_auth_url(self._page.url):
                        logger.success(
                            f"IndeedAuth: redirected to {self._page.url} — login complete"
                        )
                        return True

                    # Password field appeared → normal flow
                    pwd = self._page.locator(
                        "input[type='password'], input[name='__password']"
                    )
                    if await pwd.count() > 0 and await pwd.first.is_visible():
                        logger.debug("IndeedAuth: password step loaded")
                        return False

                    # Warn once if reCAPTCHA is visible
                    if not warned_captcha:
                        captcha = self._page.locator(
                            "iframe[src*='recaptcha'], iframe[title*='recaptcha'], "
                            "div.g-recaptcha, iframe[src*='google.com/recaptcha']"
                        )
                        if await captcha.count() > 0:
                            warned_captcha = True
                            logger.warning(
                                "IndeedAuth: reCAPTCHA detected! "
                                "Please solve it in the browser window. "
                                "Waiting up to 120 seconds…"
                            )

        except Exception as e:
            logger.debug(f"IndeedAuth: Continue click error: {e}")

        # Timed out — do a final URL check before assuming password is needed
        if not self._is_auth_url(self._page.url):
            logger.success(
                f"IndeedAuth: redirected to {self._page.url} after wait — login complete"
            )
            return True

        return False

    async def _fill_password(self, password: str) -> None:
        """Fill the password field.  Called only when _click_continue_button returned False."""
        selectors = [
            "input#login-password-input",
            "input[name='__password']",
            "input[type='password']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                await el.wait_for(state="visible", timeout=15_000)
                await el.fill(password)
                logger.debug(f"IndeedAuth: filled password via {sel}")
                return
            except PlaywrightTimeoutError:
                # Maybe login completed while we were waiting
                if not self._is_auth_url(self._page.url):
                    logger.debug(
                        f"IndeedAuth: redirected to {self._page.url} during password wait — login complete"
                    )
                    return
                continue

        # Final check: if we're no longer on an auth page, consider it a success
        if not self._is_auth_url(self._page.url):
            logger.debug(
                f"IndeedAuth: URL is {self._page.url} — skipping password raise"
            )
            return

        await self._page.screenshot(path="data/screenshots/indeed_password_fail.png")
        raise IndeedAuthError(
            f"Could not find password input. URL: {self._page.url} "
            "— check data/screenshots/indeed_password_fail.png"
        )

    async def _submit_login(self) -> None:
        try:
            # Press Enter on the password field — most reliable submit
            pwd = self._page.locator("input[type='password']").first
            if await pwd.count() > 0:
                await pwd.press("Enter")
                await asyncio.sleep(3)
                return
        except Exception:
            pass
        # Fallback: click the submit button
        btn = self._page.locator(
            "button[type='submit'], "
            "button[data-testid='login-submit-button'], "
            "button:has-text('Sign in')"
        ).first
        if await btn.count() > 0:
            await btn.click()
            await asyncio.sleep(3)

    async def _verify_login(self) -> None:
        await self._page.screenshot(path="data/screenshots/indeed_login_attempt.png")

        # Not on an auth page anymore → login complete
        if not self._is_auth_url(self._page.url):
            logger.success(f"IndeedAuth: login complete — now at {self._page.url}")
            return

        # Signed-in elements visible
        if await self._is_logged_in():
            logger.success("IndeedAuth: logged in successfully")
            return

        # Remaining challenge (2FA etc.) — give user 60 s
        logger.warning(
            f"IndeedAuth: still on auth page ({self._page.url}). "
            "Please complete any challenge in the browser within 60 seconds…"
        )
        for _ in range(12):
            await asyncio.sleep(5)
            if not self._is_auth_url(self._page.url):
                logger.success("IndeedAuth: redirected after challenge — login complete")
                return
            if await self._is_logged_in():
                logger.success("IndeedAuth: manual challenge completed")
                return

        raise IndeedAuthError(
            f"Indeed login failed. URL: {self._page.url}. "
            "Check data/screenshots/indeed_login_attempt.png"
        )

    async def _is_logged_in(self) -> bool:
        """Return True if the browser shows authenticated-only Indeed elements."""
        if self._is_auth_url(self._page.url):
            return False
        indicators = [
            "a[href*='/my/jobs']",
            "a[href*='/account/view']",
            "[data-testid='header-user-menu-button']",
            "button[aria-label*='Account']",
            ".user-account-icon",
            "a[data-gnav-element-name='UserMenu']",
        ]
        for sel in indicators:
            try:
                if await self._page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False
