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

    async def _login_form(self, email: str, password: str) -> None:
        logger.info("IndeedAuth: navigating to login page…")
        await self._page.goto(self._LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        await self._page.screenshot(path="data/screenshots/indeed_login_page.png")

        # Fill email
        await self._fill_email(email)

        # Fill password — Indeed may show it on the same page or a second step
        await self._fill_password(password)

        # Submit
        await self._submit_login()
        await self._verify_login()

    async def _fill_email(self, email: str) -> None:
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
                # Click Continue if it's a two-step flow
                await self._click_continue_if_present()
                return
        raise IndeedAuthError("Could not find email input on Indeed login page")

    async def _click_continue_if_present(self) -> None:
        """Click 'Continue with email' or 'Continue' button if present."""
        try:
            btn = self._page.locator(
                "button[type='submit'], "
                "button:has-text('Continue'), "
                "button[data-testid='email-form-submit']"
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                text = (await btn.inner_text()).strip().lower()
                if "continue" in text or "next" in text:
                    await btn.click()
                    await asyncio.sleep(1.5)
        except Exception:
            pass

    async def _fill_password(self, password: str) -> None:
        selectors = [
            "input#login-password-input",
            "input[name='__password']",
            "input[type='password']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                await el.wait_for(state="visible", timeout=8_000)
                await el.fill(password)
                logger.debug(f"IndeedAuth: filled password via {sel}")
                return
            except PlaywrightTimeoutError:
                continue
        raise IndeedAuthError("Could not find password input on Indeed login page")

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
        if await self._is_logged_in():
            logger.success("IndeedAuth: logged in successfully")
            return

        # Check for 2FA or CAPTCHA
        current_url = self._page.url
        if any(x in current_url for x in ["challenge", "captcha", "verify", "2fa"]):
            logger.warning(
                "IndeedAuth: 2FA/CAPTCHA detected. Please complete it manually "
                "in the browser within 60 seconds…"
            )
            for _ in range(12):
                await asyncio.sleep(5)
                if await self._is_logged_in():
                    logger.success("IndeedAuth: manual challenge completed")
                    return
            raise IndeedAuthError("2FA/CAPTCHA not completed within 60 seconds")

        raise IndeedAuthError(
            f"Indeed login failed. URL: {current_url}. "
            "Check data/screenshots/indeed_login_attempt.png"
        )

    async def _is_logged_in(self) -> bool:
        """Return True if the browser is on an authenticated Indeed page."""
        url = self._page.url
        if "secure.indeed.com" in url and "login" in url:
            return False
        # Look for elements that only appear when signed in
        indicators = [
            "a[href*='/my/jobs']",
            "a[href*='/account/view']",
            "[data-testid='header-user-menu-button']",
            "button[aria-label*='Account']",
            ".user-account-icon",
        ]
        for sel in indicators:
            try:
                if await self._page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False
