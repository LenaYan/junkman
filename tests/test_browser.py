import builtins
import csv
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.request import Request

from test_junkman import DOC, PAGE, PDF, FakeHTTP

import junkman as j
from junkman_browser import Browser, BrowserError, CapturedDownload


def browser_args(*extra):
    return j.build_parser().parse_args(["run", "--browser", "--url", PAGE, *extra])


class BrowserUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_optional_dependency_is_lazy_and_missing_is_actionable(self):
        original = builtins.__import__

        def missing(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("not installed")
            return original(name, *args, **kwargs)

        browser = Browser(check_url=Mock(), timeout=1, user_agent=j.USER_AGENT)
        with (
            patch("builtins.__import__", side_effect=missing),
            self.assertRaisesRegex(BrowserError, "extra browser"),
        ):
            browser.__enter__()

    def test_guard_aborts_denied_request(self):
        browser = Browser(
            check_url=Mock(side_effect=BrowserError("denied")), timeout=1, user_agent=j.USER_AGENT
        )
        route = Mock()
        route.request.url = DOC
        browser._guard(route)
        route.abort.assert_called_once_with("blockedbyclient")
        route.fallback.assert_not_called()
        self.assertIn("denied", browser.blocked[0])

    def test_guard_allows_valid_request(self):
        guard = Mock()
        browser = Browser(check_url=guard, timeout=1, user_agent=j.USER_AGENT)
        route = Mock()
        route.request.url = DOC
        browser._guard(route)
        guard.assert_called_once_with(DOC)
        route.fallback.assert_called_once()

    def test_scope_and_robots_checked_for_browser_requests(self):
        client = j.BrowserHTTP(browser_args("--allow-domain", "publisher.example"))
        client.check_robots = Mock()
        client.delay = 0
        with patch.object(j, "public_url", side_effect=lambda url: url):
            client.check_browser_url(PAGE)
            with self.assertRaisesRegex(BrowserError, "outside allowed"):
                client.check_browser_url("https://elsewhere.example/")
        client.check_robots.assert_called_once_with(PAGE)
        client.check_robots.side_effect = j.CrawlError("robots denied")
        with (
            patch.object(j, "public_url", side_effect=lambda url: url),
            self.assertRaisesRegex(BrowserError, "robots denied"),
        ):
            client.check_browser_url(PAGE)

    def test_browser_cookie_bridge_respects_host_only_scope(self):
        client = j.BrowserHTTP(browser_args())
        client.browser = Mock()
        client.browser.cookies.return_value = [
            {
                "name": "session",
                "value": "test-token",
                "domain": "publisher.example",
                "path": "/",
                "secure": True,
                "expires": -1,
                "httpOnly": True,
            }
        ]
        client.sync_cookies()
        request = Request(DOC)
        client.cookie_jar.add_cookie_header(request)
        self.assertEqual(request.get_header("Cookie"), "session=test-token")
        for url in ["https://elsewhere.example/a", "https://cdn.publisher.example/a"]:
            request = Request(url)
            client.cookie_jar.add_cookie_header(request)
            self.assertIsNone(request.get_header("Cookie"))

    def test_browser_search_fallback_and_redirect_unwrap(self):
        client = j.BrowserHTTP(browser_args())
        client.read = Mock(side_effect=[BrowserError("denied"), (PAGE, b"", "text/html")])
        client.browser = Mock()
        client.browser.result_links.return_value = [
            ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fpublisher.example%2Fsample.pdf", "Sample"),
        ]
        self.assertEqual(j.search_web(client, "query", 2), [(DOC, "Sample")])
        self.assertEqual(client.read.call_count, 2)

    def test_partitioned_cookies_are_not_downgraded_into_http_cookies(self):
        client = j.BrowserHTTP(browser_args())
        client.browser = Mock()
        client.browser.cookies.return_value = [
            {
                "name": "partitioned",
                "value": "test-token",
                "domain": "publisher.example",
                "partitionKey": "https://another.example",
            }
        ]
        with self.assertLogs("junkman", level="WARNING"):
            client.sync_cookies()
        self.assertEqual(list(client.cookie_jar), [])

    def test_all_browser_search_failures_are_explicit(self):
        client = j.BrowserHTTP(browser_args())
        client.read = Mock(side_effect=BrowserError("challenge"))
        with self.assertRaisesRegex(j.CrawlError, "All browser search providers failed"):
            j.search_web(client, "query", 2)

    def test_action_identity_legacy_manifest_and_resume(self):
        first = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#download")
        second = j.Resource.create(PAGE, "Workbook", PAGE, "pdf", "#workbook")
        self.assertNotEqual(first.id, second.id)
        manifest = self.root / "legacy.csv"
        legacy = j.Resource.create(DOC, "Sample", PAGE, "pdf")
        with manifest.open("w", newline="") as stream:
            fields = list(j.Resource.__dataclass_fields__)[:-1]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            row = j.asdict(legacy)
            del row["selector"]
            writer.writerow(row)
        self.assertEqual(j.read_manifest(manifest), [legacy])
        j.write_manifest(manifest, [first, second])
        self.assertEqual(j.read_manifest(manifest), [first, second])

    def test_action_records_fail_clearly_without_browser(self):
        resource = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#download")
        with self.assertRaisesRegex(j.CrawlError, "require --browser"):
            j.download_one(j.HTTP(), resource, self.root, domains=[], max_bytes=1024, force=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_login_state_is_private_and_browser_is_closed(self):
        state = self.root / ".auth" / "session.json"
        browser = MagicMock()
        browser.render.return_value = (PAGE, b"<html></html>", "text/html")
        browser.cookies.return_value = []
        browser.storage_state.return_value = {"cookies": [], "origins": []}
        with patch.object(j, "Browser", return_value=browser), patch("builtins.input", return_value=""):
            self.assertEqual(j.main(["login", "--url", PAGE, "--storage-state", str(state)]), 0)
        self.assertEqual(json.loads(state.read_text()), {"cookies": [], "origins": []})
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
        browser.__exit__.assert_called_once()

    def test_manual_login_pumps_requests_until_terminal_confirmation(self):
        browser = Browser(check_url=Mock(), timeout=1, user_agent=j.USER_AGENT)
        browser.error_type = RuntimeError
        browser.page = Mock()
        browser.page.wait_for_timeout.side_effect = lambda _: ready.set()
        ready = threading.Event()

        def confirm(_):
            self.assertTrue(ready.wait(2))
            return ""

        with patch("builtins.input", side_effect=confirm):
            browser.wait_for_login()
        browser.page.wait_for_timeout.assert_called()

    def test_manual_login_eof_never_saves_a_session(self):
        browser = Browser(check_url=Mock(), timeout=1, user_agent=j.USER_AGENT)
        browser.error_type = RuntimeError
        browser.page = Mock()
        with (
            patch("builtins.input", side_effect=EOFError("stdin closed")),
            self.assertRaisesRegex(BrowserError, "not confirmed"),
        ):
            browser.wait_for_login()

    def test_cli_rejects_browser_options_without_opt_in_or_missing_state(self):
        for args in [
            ["search", "query", "--headed"],
            ["run", "--url", PAGE, "--click-selector", "#download"],
            ["login", "--url", PAGE],
            ["search", "query", "--browser", "--storage-state", str(self.root / "missing.json")],
        ]:
            with (
                self.subTest(args=args),
                patch("sys.stderr", new_callable=io.StringIO),
                self.assertRaises(SystemExit),
            ):
                j.main(args)

    def test_action_discovery_respects_formats_and_limits(self):
        http = FakeHTTP({PAGE: (b"<html></html>", "text/html", None)})
        resource = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#download")
        http.discover_actions = Mock(return_value=[resource])
        for formats, expected in [({"pdf"}, [resource]), ({"zip"}, [])]:
            found, errors = j.discover(
                http, [(PAGE, "")], domains=[], formats=formats, max_pages=1, depth=0, limit=1
            )
            self.assertEqual(found, expected)
            self.assertEqual(errors, [])

    def test_action_integrity_and_skip_reuses_shared_download_pipeline(self):
        resource = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#download")
        source = self.root / "staged.pdf"
        source.write_bytes(PDF)
        capture = MagicMock()
        capture.__enter__.return_value = CapturedDownload(
            source, "blob:https://publisher.example/uuid", "sample.pdf"
        )
        http = Mock()
        http.download_action.return_value = capture
        j.download_one(http, resource, self.root / "files", domains=[], max_bytes=1024, force=False)
        self.assertEqual(Path(resource.path).read_bytes(), PDF)
        metadata = json.loads((self.root / "files" / f"{resource.id}.json").read_text())
        self.assertEqual(metadata["selector"], "#download")
        self.assertEqual(metadata["transfer"], "playwright")
        j.download_one(http, resource, self.root / "files", domains=[], max_bytes=1024, force=False)
        self.assertEqual(resource.status, "skipped")
        http.download_action.assert_called_once()
