"""Optional Playwright operations; importing this module does not load Playwright."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

LOG = logging.getLogger("junkman")


class BrowserError(Exception):
    """An actionable browser setup, navigation, or download failure."""


@dataclass
class CapturedDownload:
    path: Path
    url: str
    filename: str


class Browser:
    def __init__(
        self,
        *,
        check_url: Callable[[str], None],
        timeout: float,
        user_agent: str,
        headed: bool = False,
        storage_state: Path | None = None,
        screenshot_dir: Path | None = None,
        wait_selector: str | None = None,
        wait_ms: int = 500,
    ):
        self.check_url = check_url
        self.timeout_ms = timeout * 1000
        self.user_agent = user_agent
        self.headed = headed
        self.state_path = storage_state
        self.screenshot_dir = screenshot_dir
        self.wait_selector = wait_selector
        self.wait_ms = wait_ms
        self.stack = ExitStack()
        self.blocked: list[str] = []

    def __enter__(self):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise BrowserError("Install browser support first: uv sync --extra browser") from exc
        self.error_type = Error
        try:
            driver = sync_playwright().start()
            self.stack.callback(driver.stop)
            browser = driver.chromium.launch(headless=not self.headed)
            self.stack.callback(browser.close)
            options = {
                "accept_downloads": True,
                "service_workers": "block",
                "user_agent": self.user_agent,
            }
            if self.state_path is not None:
                options["storage_state"] = str(self.state_path)
            self.context = browser.new_context(**options)
            self.stack.callback(self.context.close)
            self.context.set_default_timeout(self.timeout_ms)
            self.context.route("**/*", self._guard)
            self.context.route_web_socket("**/*", lambda websocket: websocket.close())
            self.page = self.context.new_page()
            return self
        except (Error, OSError, ValueError) as exc:
            self.stack.close()
            raise BrowserError(
                "Cannot start Chromium. Check --storage-state and install the browser with "
                f"'uv run playwright install chromium'. Details: {exc}"
            ) from exc

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stack.close()
        except self.error_type as close_error:
            if exc is None:
                raise BrowserError(f"Cannot close browser: {close_error}") from close_error
            LOG.error("Browser cleanup failed: %s", close_error)

    def _guard(self, route) -> None:
        url = route.request.url
        try:
            self.check_url(url)
        except BrowserError as exc:
            message = f"{url}: {exc}"
            self.blocked.append(message)
            LOG.warning("Blocked browser request: %s", message)
            route.abort("blockedbyclient")
            return
        route.fallback()

    def screenshot(self) -> None:
        if self.screenshot_dir is None:
            return
        self.screenshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefix = hashlib.sha256(self.page.url.encode()).hexdigest()[:12] + "-"
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".png", dir=self.screenshot_dir)
        os.close(fd)
        try:
            self.page.screenshot(path=name, full_page=True, timeout=self.timeout_ms)
        except (self.error_type, OSError):
            Path(name).unlink(missing_ok=True)
            raise
        LOG.info("Screenshot: %s", Path(name).resolve())

    def render(self, url: str) -> tuple[str, bytes, str]:
        self.blocked.clear()
        try:
            self.check_url(url)
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is None or response.status != 200:
                status = response.status if response else "no response"
                raise BrowserError(f"Expected an HTML HTTP 200 response, received {status}: {url}")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise BrowserError(f"Expected an HTML page, received {content_type}: {url}")
            if self.wait_selector:
                self.page.wait_for_selector(self.wait_selector, state="attached", timeout=self.timeout_ms)
            if self.wait_ms:
                self.page.wait_for_timeout(self.wait_ms)
            # Resolve links against the document's actual base URL before serializing the DOM.
            self.page.locator("a[href]").evaluate_all(
                "(links) => links.forEach(a => a.setAttribute('href', a.href))"
            )
            body = self.page.content().encode("utf-8")
            self.screenshot()
            return self.page.url, body, "text/html"
        except (BrowserError, self.error_type, OSError) as exc:
            detail = "; ".join(self.blocked[-3:])
            try:
                self.screenshot()
            except (self.error_type, OSError) as screenshot_error:
                LOG.error("Failure screenshot could not be saved: %s", screenshot_error)
            raise BrowserError(f"Browser navigation failed for {url}: {exc}; blocked: {detail}") from exc

    def matching_selectors(self, selectors: list[str]) -> list[str]:
        try:
            result = []
            for selector in selectors:
                count = self.page.locator(selector).count()
                if count > 1:
                    raise BrowserError(f"Download selector must match exactly one element: {selector}")
                if count == 1:
                    result.append(selector)
            return result
        except self.error_type as exc:
            raise BrowserError(f"Cannot evaluate download selector: {exc}") from exc

    def result_links(self, selector: str) -> list[tuple[str, str]]:
        try:
            values = self.page.locator(selector).evaluate_all(
                "(links) => links.map(a => [a.href, a.textContent.trim()])"
            )
            return [(url, title) for url, title in values]
        except self.error_type as exc:
            raise BrowserError(f"Cannot read browser search results: {exc}") from exc

    def cookies(self) -> list[dict]:
        try:
            return self.context.cookies()
        except self.error_type as exc:
            raise BrowserError(f"Cannot read browser cookies: {exc}") from exc

    def storage_state(self) -> dict:
        try:
            return self.context.storage_state()
        except self.error_type as exc:
            raise BrowserError(f"Cannot save browser storage state: {exc}") from exc

    def wait_for_login(self) -> None:
        confirmed = threading.Event()
        errors: list[Exception] = []

        def confirm():
            try:
                input("Log in manually in Chromium, then press Enter here to save the session: ")
            except (EOFError, OSError) as exc:
                errors.append(exc)
            finally:
                confirmed.set()

        # Keep Playwright's event loop servicing guarded requests during manual login.
        threading.Thread(target=confirm, daemon=True).start()
        try:
            while not confirmed.wait(0.05):
                self.page.wait_for_timeout(100)
            if errors:
                raise BrowserError(f"Session capture was not confirmed: {errors[0]}")
            self.screenshot()
        except self.error_type as exc:
            raise BrowserError(f"Manual login browser closed or failed: {exc}") from exc

    @contextmanager
    def download(self, url: str, selector: str, max_bytes: int):
        captured = None
        try:
            final_url, _, _ = self.render(url)
            locator = self.page.locator(selector)
            if locator.count() != 1:
                raise BrowserError(f"Download selector must match exactly one element: {selector}")
            with self.page.expect_download(timeout=self.timeout_ms) as event:
                locator.click(timeout=self.timeout_ms)
            captured = event.value
            failure = captured.failure()
            if failure:
                raise BrowserError(f"Browser download failed: {failure}")
            target = captured.url
            if target.startswith("blob:"):
                blob_origin = urlsplit(target[5:])
                page_origin = urlsplit(final_url)
                if (blob_origin.scheme, blob_origin.netloc) != (page_origin.scheme, page_origin.netloc):
                    raise BrowserError("Blob download has a different origin from its source page")
            else:
                self.check_url(target)
            staged = captured.path()
            if staged is None or staged.stat().st_size > max_bytes:
                raise BrowserError(f"Browser download missing or exceeds the {max_bytes}-byte limit")
            yield CapturedDownload(staged, target, captured.suggested_filename)
            self.screenshot()
        except self.error_type as exc:
            raise BrowserError(f"Button download failed for {url}, selector {selector}: {exc}") from exc
        finally:
            if captured is not None:
                try:
                    captured.delete()
                except self.error_type as exc:
                    raise BrowserError(f"Cannot remove the temporary browser download: {exc}") from exc
