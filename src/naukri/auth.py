"""Naukri authentication with cookie-based session persistence."""

import asyncio
import json
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


class NaukriAuthError(Exception):
    """Raised when Naukri login cannot be completed."""


class NaukriAuth:
    """Handles Naukri login and session restore via cookies."""

    _HOME_URL  = "https://www.naukri.com/"
    _LOGIN_URL = "https://www.naukri.com/nlogin/login"

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
            logger.info("NaukriAuth: loaded saved cookies — verifying session…")
            await self._page.goto(self._HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            if await self._is_logged_in():
                logger.success("NaukriAuth: session restored — skipping login form")
                return True
            logger.info("NaukriAuth: cookies expired, falling back to form login")
        except Exception as e:
            logger.warning(f"NaukriAuth: cookie restore failed: {e}")
        return False

    async def _save_cookies(self, cookie_path: Path) -> None:
        try:
            cookies = await self._context.cookies()
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(
                json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.debug(f"NaukriAuth: saved {len(cookies)} cookies → {cookie_path}")
        except Exception as e:
            logger.warning(f"NaukriAuth: could not save cookies: {e}")

    # ── Form login ─────────────────────────────────────────────────────────────

    async def _login_form(self, email: str, password: str) -> None:
        logger.info("NaukriAuth: navigating to login page…")
        await self._page.goto(self._LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await self._page.screenshot(path="data/screenshots/naukri_login_page.png")

        await self._fill_email(email)
        await self._fill_password(password)
        await self._submit_login()
        await self._verify_login()

    async def _fill_email(self, email: str) -> None:
        selectors = [
            "input#usernameField",
            "input[placeholder*='Email' i]",
            "input[placeholder*='Username' i]",
            "input[name='username']",
            "input[type='email']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill(email)
                    logger.debug(f"NaukriAuth: filled email via {sel}")
                    return
            except Exception:
                continue
        raise NaukriAuthError("Could not find email/username input on Naukri login page")

    async def _fill_password(self, password: str) -> None:
        selectors = [
            "input#passwordField",
            "input[placeholder*='Password' i]",
            "input[type='password']",
            "input[name='password']",
        ]
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill(password)
                    logger.debug(f"NaukriAuth: filled password via {sel}")
                    return
            except Exception:
                continue
        raise NaukriAuthError("Could not find password input on Naukri login page")

    async def _submit_login(self) -> None:
        selectors = [
            "button[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "[data-ga-track*='login' i]",
        ]
        for sel in selectors:
            try:
                btn = self._page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(3)
                    logger.debug(f"NaukriAuth: clicked login button via {sel}")
                    return
            except Exception:
                continue
        # Fallback: press Enter on password field
        try:
            pwd = self._page.locator("input[type='password']").first
            if await pwd.count() > 0:
                await pwd.press("Enter")
                await asyncio.sleep(3)
        except Exception:
            pass

    async def _verify_login(self) -> None:
        await self._page.screenshot(path="data/screenshots/naukri_login_attempt.png")

        if await self._is_logged_in():
            logger.success("NaukriAuth: logged in successfully")
            return

        # Give user 60 s to complete any challenge (OTP, CAPTCHA, etc.)
        logger.warning(
            "NaukriAuth: not confirmed logged in. "
            "If a challenge appeared, complete it within 60 seconds…"
        )
        for _ in range(12):
            await asyncio.sleep(5)
            if await self._is_logged_in():
                logger.success("NaukriAuth: login confirmed after challenge")
                return

        raise NaukriAuthError(
            f"Naukri login failed. URL: {self._page.url} "
            "— check data/screenshots/naukri_login_attempt.png"
        )

    async def _is_logged_in(self) -> bool:
        """Return True if authenticated-only elements are visible on Naukri."""
        indicators = [
            # Profile/avatar elements visible only when logged in
            ".nI-gNb-drawer__bars",
            "[class*='nI-gNb-nav__user']",
            "[class*='user-name']",
            "a[href*='/mnjuser/profile']",
            "a[href*='/mnjuser/homepage']",
            "[data-ga-track*='profile' i]",
            "span.view-profile",
        ]
        for sel in indicators:
            try:
                if await self._page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass

        # Also consider: if URL moved away from login page, check for typical
        # post-login pages (dashboard, homepage, etc.)
        url = self._page.url
        if "nlogin" in url or "/login" in url:
            return False

        # If we're on naukri.com and NOT on login, assume logged in
        if "naukri.com" in url and "login" not in url:
            # Verify by checking the page doesn't show a login prompt
            try:
                login_btn = self._page.locator(
                    "a:has-text('Login'), button:has-text('Login')"
                ).first
                if await login_btn.count() > 0 and await login_btn.is_visible():
                    return False
            except Exception:
                pass
            return True

        return False
