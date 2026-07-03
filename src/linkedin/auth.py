import asyncio
import json
import random
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed"

# Ordered list of selectors to try for the email field — most specific first
_EMAIL_SELECTORS = [
    "input#username",
    "input[name='session_key']",
    "input[autocomplete='username']",
    "input[type='email']",
]
_PASSWORD_SELECTORS = [
    "input#password",
    "input[name='session_password']",
    "input[autocomplete='current-password']",
    "input[type='password']",
]


class LinkedInAuthError(Exception):
    """Raised when LinkedIn login fails or is challenged."""


class LinkedInAuth:
    """Handles LinkedIn authentication including cookie restore, 2FA, and CAPTCHA pause."""

    def __init__(self, page: Page, context: BrowserContext | None = None) -> None:
        self._page = page
        self._context = context

    async def login_with_session(
        self, email: str, password: str, cookie_path: Path
    ) -> None:
        """Try restoring a saved session first; fall back to full credential login."""
        if await self._try_restore_session(cookie_path):
            return

        logger.info("No valid session found — logging in with credentials")
        await self.login(email, password)
        await self._save_session(cookie_path)

    async def login(self, email: str, password: str) -> None:
        """Full credential login — navigate, fill form, handle challenges."""
        logger.info("Navigating to LinkedIn login page")
        await self._page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")

        # Dismiss any cookie-consent overlay that might be blocking the form
        await self._dismiss_overlays()

        await self._fill_credentials(email, password)
        await self._submit_login()
        await self._verify_login()

    # ── Session helpers ────────────────────────────────────────────────────────

    async def _try_restore_session(self, cookie_path: Path) -> bool:
        if not cookie_path.exists() or self._context is None:
            return False
        try:
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
            await self._context.add_cookies(cookies)
            logger.info("Loaded saved session cookies — verifying...")
            await self._page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            if "/feed" in self._page.url:
                logger.success("Session restored — skipping login form")
                return True
            logger.info("Saved session expired — will log in fresh")
            await self._context.clear_cookies()
            return False
        except Exception as e:
            logger.warning(f"Could not restore session: {e}")
            return False

    async def _save_session(self, cookie_path: Path) -> None:
        if self._context is None:
            return
        try:
            cookies = await self._context.cookies()
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
            logger.info(f"Session cookies saved → {cookie_path}")
        except Exception as e:
            logger.warning(f"Could not save session cookies: {e}")

    # ── Overlay / consent dismissal ────────────────────────────────────────────

    async def _dismiss_overlays(self) -> None:
        """Click away cookie-consent banners or modals that cover the login form."""
        dismiss_labels = [
            "Accept", "Accept all", "Accept cookies",
            "Reject", "Reject all", "Decline",
            "Close", "Dismiss", "Got it", "OK",
        ]
        for label in dismiss_labels:
            try:
                btn = self._page.get_by_role("button", name=label)
                if await btn.first.is_visible():
                    await btn.first.click()
                    logger.debug(f"Dismissed overlay: '{label}'")
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

    # ── Credential filling ─────────────────────────────────────────────────────

    async def _fill_credentials(self, email: str, password: str) -> None:
        """Fill email and password using the most robust method available.

        Strategy (tried in order):
        1. Find a truly visible input and type into it normally.
        2. Force-fill (bypasses Playwright visibility check) for partially hidden forms.
        3. JavaScript injection — sets React-compatible synthetic events directly.

        A screenshot is saved to data/screenshots/login_page.png so you can
        diagnose what the page looked like if login still fails.
        """
        logger.debug("Filling login credentials")
        await asyncio.sleep(1.5)  # allow JS hydration

        await self._page.screenshot(path="data/screenshots/login_page.png")
        logger.debug("Login page screenshot → data/screenshots/login_page.png")

        await self._smart_fill(_EMAIL_SELECTORS, email, field_name="email")
        await asyncio.sleep(random.uniform(0.4, 0.9))
        await self._smart_fill(_PASSWORD_SELECTORS, password, field_name="password")
        await asyncio.sleep(random.uniform(0.3, 0.7))

    async def _smart_fill(self, selectors: list[str], value: str, field_name: str) -> None:
        """Fill the first matching input using the best available method.

        1. Try each selector; for each match check if it is visible and type normally.
           Uses triple-click to select-all before typing to clear any pre-filled value.
        2. If no selector yields a visible element, force-fill the first attached match.
        3. If force-fill fails, fall back to React-compatible JS injection + Tab blur
           to trigger React's onChange so the form state is updated.
        """
        # ── Pass 1: visible + click + clear + type ────────────────────────────
        for selector in selectors:
            locator = self._page.locator(selector)
            count = await locator.count()
            for i in range(count):
                el = locator.nth(i)
                try:
                    if await el.is_visible():
                        await el.click()
                        await el.press("Control+a")   # select all existing text
                        await el.press("Backspace")   # clear it
                        await self._human_type(el, value)
                        logger.debug(f"{field_name}: typed via visible input '{selector}'[{i}]")
                        return
                except Exception:
                    continue

        # ── Pass 2: force-fill ─────────────────────────────────────────────────
        for selector in selectors:
            locator = self._page.locator(selector)
            if await locator.count() > 0:
                try:
                    await locator.last.fill(value, force=True)
                    logger.debug(f"{field_name}: force-filled via '{selector}'")
                    return
                except Exception as e:
                    logger.debug(f"Force-fill failed for '{selector}': {e}")

        # ── Pass 3: React-compatible JS injection + Tab to trigger onChange ────
        for selector in selectors:
            try:
                filled = await self._js_fill(selector, value)
                if filled:
                    # Tab-key blur so React's synthetic onChange fires
                    await self._page.keyboard.press("Tab")
                    await asyncio.sleep(0.3)
                    logger.debug(f"{field_name}: JS-injected via '{selector}'")
                    return
            except Exception as e:
                logger.debug(f"JS fill failed for '{selector}': {e}")

        raise LinkedInAuthError(
            f"Could not fill {field_name} field — tried {selectors}. "
            "Check data/screenshots/login_page.png to see what the page looks like."
        )

    async def _js_fill(self, selector: str, value: str) -> bool:
        """Fill an input using JS — works even when Playwright considers it non-interactive.

        Uses React's native input value setter so controlled components pick up the change.
        """
        return await self._page.evaluate(
            """([selector, value]) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            [selector, value],
        )

    async def _submit_login(self) -> None:
        """Submit the login form.

        Pressing Enter on the password field is the most reliable way to submit
        React-controlled forms — it fires the form's onSubmit handler regardless
        of how the field values were set.  Clicking the button is used as fallback.
        """
        # Primary: press Enter on the password field — triggers React's onSubmit
        for selector in _PASSWORD_SELECTORS:
            locator = self._page.locator(selector)
            if await locator.count() > 0:
                try:
                    await locator.last.press("Enter")
                    logger.debug("Submitted login form via Enter key on password field")
                    await asyncio.sleep(2)
                    return
                except Exception:
                    continue

        # Fallback 1: click the Sign In button
        for label in ["Sign in", "Sign In", "Log in"]:
            btn = self._page.get_by_role("button", name=label)
            if await btn.count() > 0:
                try:
                    await btn.first.click()
                    logger.debug(f"Clicked login button: '{label}'")
                    await asyncio.sleep(2)
                    return
                except Exception:
                    continue

        # Fallback 2: JS form submit
        await self._page.evaluate(
            "document.querySelector('form')?.dispatchEvent(new Event('submit', {bubbles:true}))"
        )
        logger.debug("Submitted login form via JS fallback")

    async def _verify_login(self) -> None:
        """Wait for successful navigation away from the login page."""
        try:
            await self._page.wait_for_url("**/feed**", timeout=30_000)
            logger.success("Logged in successfully")
            return
        except PlaywrightTimeoutError:
            pass

        current_url = self._page.url

        # Still on the login page — check for a visible error message first
        if "login" in current_url:
            error_msg = await self._get_login_error()
            if error_msg:
                raise LinkedInAuthError(
                    f"LinkedIn rejected credentials: '{error_msg}'. "
                    "Check your email/password in .env."
                )

        # Security challenge / CAPTCHA
        if any(k in current_url for k in ("checkpoint", "challenge", "uas/login")):
            logger.warning(
                "LinkedIn security challenge detected. "
                "Complete it manually in the browser window, then press ENTER here."
            )
            input(">>> Press ENTER after completing the challenge: ")
            await self._page.wait_for_url("**/feed**", timeout=60_000)
            logger.success("Challenge passed — logged in")
            return

        # Email / phone verification
        if any(k in current_url for k in ("verification", "pin", "add-phone")):
            logger.warning(
                "LinkedIn 2FA / email PIN required. "
                "Enter the code in the browser window, then press ENTER here."
            )
            input(">>> Press ENTER after entering the verification code: ")
            await self._page.wait_for_url("**/feed**", timeout=60_000)
            logger.success("2FA passed — logged in")
            return

        # Unknown state — pause for manual intervention
        screenshot_path = "data/screenshots/login_failed.png"
        await self._page.screenshot(path=screenshot_path)
        logger.warning(
            f"Unexpected URL after login: {current_url}. "
            f"Screenshot saved to {screenshot_path}. "
            "Complete login manually in the browser, then press ENTER."
        )
        input(">>> Press ENTER once you are logged in and on the LinkedIn feed: ")
        if "/feed" not in self._page.url:
            raise LinkedInAuthError("Still not on the feed after manual intervention.")
        logger.success("Logged in (manual)")

    async def _get_login_error(self) -> str:
        """Return the visible LinkedIn login error message, or empty string if none."""
        error_selectors = [
            "[data-test-id='alert-dialog']",
            ".alert-content",
            "#error-for-username",
            "#error-for-password",
            "[role='alert']",
            ".form__label--error",
        ]
        for selector in error_selectors:
            try:
                el = self._page.locator(selector).first
                if await el.is_visible():
                    return (await el.inner_text()).strip()
            except Exception:
                continue
        return ""

    async def _human_type(self, locator, text: str) -> None:
        """Type character-by-character with random delays to mimic human input."""
        for char in text:
            await locator.press(char)
            await asyncio.sleep(random.uniform(0.05, 0.15))
