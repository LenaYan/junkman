"""Real Chromium tests using intercepted HTTPS fixtures, without external services."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_junkman import DOC, PAGE, PDF, Response

import junkman as j
from junkman_browser import Browser, BrowserError

HTML = """<!doctype html><html><head><base href="/assets/"></head><body>
<button id="download">Download sample</button>
<button id="nothing">No download</button>
<button id="https" onclick="location.href='/attachment'">HTTPS download</button>
<script>
setTimeout(() => {
  const a = document.createElement('a');
  a.href = 'dynamic.pdf'; a.textContent = 'Dynamic sample'; a.id = 'dynamic';
  document.body.append(a);
}, 50);
document.querySelector('#download').onclick = () => {
  const blob = new Blob(['%PDF-1.4\\nBrowser sample\\n%%EOF\\n'], {type: 'application/pdf'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'sample.pdf'; a.click();
};
if (localStorage.getItem('access') === 'fixture' && document.cookie.includes('session=fixture')) {
  const a = document.createElement('a');
  a.id = 'authorized'; a.href = '/authorized.pdf'; a.textContent = 'Authorized';
  document.body.append(a);
}
</script></body></html>"""


def fixture(route):
    if route.request.url.startswith("https://www.bing.com/search?"):
        route.fulfill(
            status=200,
            content_type="text/html",
            body=f'<li class="b_algo"><h2><a id="dynamic" href="{DOC}">Sample</a></h2></li>',
        )
    elif route.request.url == PAGE:
        route.fulfill(status=200, content_type="text/html", body=HTML)
    elif route.request.url == "https://publisher.example/attachment":
        route.fulfill(
            status=200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="sample.pdf"',
            },
            body=PDF,
        )
    elif route.request.url.endswith(".pdf"):
        route.fulfill(status=200, content_type="application/pdf", body=PDF)
    else:
        route.fulfill(status=404, content_type="text/plain", body="not found")


def install_fixtures(browser):
    browser.context.unroute("**/*", browser._guard)
    browser.context.route("**/*", fixture)
    # The production guard remains the first handler, including for fixture requests.
    browser.context.route("**/*", browser._guard)


class ChromiumTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        public = patch.object(j, "public_url", side_effect=j.canonical_url)
        robots = patch.object(j.HTTP, "check_robots")
        public.start()
        robots.start()
        self.addCleanup(public.stop)
        self.addCleanup(robots.stop)

    def client(self, *options):
        args = j.build_parser().parse_args(
            [
                "run",
                "--browser",
                "--url",
                PAGE,
                "--allow-domain",
                "publisher.example",
                "--timeout",
                "3",
                "--wait-selector",
                "#dynamic",
                "--wait-ms",
                "0",
                *options,
            ]
        )
        args.delay = 0
        client = j.BrowserHTTP(args)
        self.addCleanup(client.browser.__exit__, None, None, None)
        client.browser.__enter__()
        install_fixtures(client.browser)
        return client

    def test_javascript_links_base_url_screenshot_and_blob_download(self):
        client = self.client("--click-selector", "#download", "--screenshot-dir", str(self.root / "screens"))
        records, errors = j.discover(
            client,
            [(PAGE, "")],
            domains=["publisher.example"],
            formats={"pdf"},
            max_pages=1,
            depth=0,
            limit=10,
        )
        self.assertEqual(errors, [])
        self.assertTrue(
            any(record.url == "https://publisher.example/assets/dynamic.pdf" for record in records)
        )
        action = next(record for record in records if record.selector)
        j.download_one(
            client, action, self.root / "files", domains=["publisher.example"], max_bytes=1024, force=False
        )
        self.assertTrue(Path(action.path).read_bytes().startswith(b"%PDF-"))
        metadata = json.loads((self.root / "files" / f"{action.id}.json").read_text())
        self.assertTrue(metadata["final_url"].startswith("blob:https://publisher.example/"))
        self.assertEqual(metadata["suggested_filename"], "sample.pdf")
        self.assertGreaterEqual(len(list((self.root / "screens").glob("*.png"))), 2)
        j.download_one(
            client, action, self.root / "files", domains=["publisher.example"], max_bytes=1024, force=False
        )
        self.assertEqual(action.status, "skipped")

    def test_size_limit_rejects_staged_download_and_leaves_no_output(self):
        client = self.client()
        resource = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#download")
        with self.assertRaisesRegex(BrowserError, "exceeds"):
            j.download_one(client, resource, self.root / "files", domains=[], max_bytes=4, force=False)
        self.assertFalse((self.root / "files").exists())

    def test_https_attachment_download(self):
        client = self.client()
        resource = j.Resource.create(PAGE, "Sample", PAGE, "pdf", "#https")
        j.download_one(client, resource, self.root / "files", domains=[], max_bytes=1024, force=False)
        self.assertEqual(Path(resource.path).read_bytes(), PDF)
        metadata = json.loads((self.root / "files" / f"{resource.id}.json").read_text())
        self.assertEqual(metadata["final_url"], "https://publisher.example/attachment")

    def test_browser_keyword_search_extracts_rendered_results(self):
        client = self.client()
        self.assertEqual(j.search_web(client, "Cambridge Power Up", 2), [(DOC, "Sample")])

    def test_non_download_button_times_out_without_success_marker(self):
        client = self.client()
        resource = j.Resource.create(PAGE, "", PAGE, "pdf", "#nothing")
        with self.assertRaisesRegex(BrowserError, "Button download failed"):
            j.download_one(client, resource, self.root / "files", domains=[], max_bytes=1024, force=False)
        self.assertFalse((self.root / "files").exists())

    def test_storage_state_restores_cookies_and_local_storage(self):
        state = self.root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "session",
                            "value": "fixture",
                            "domain": "publisher.example",
                            "path": "/",
                            "expires": -1,
                            "httpOnly": False,
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    ],
                    "origins": [
                        {
                            "origin": "https://publisher.example",
                            "localStorage": [{"name": "access", "value": "fixture"}],
                        }
                    ],
                }
            )
        )
        client = self.client("--storage-state", str(state))
        _, body, _ = client.read(PAGE, domains=["publisher.example"])
        self.assertIn(b'id="authorized"', body)
        self.assertEqual(next(iter(client.cookie_jar)).value, "fixture")
        saved = client.browser.storage_state()
        self.assertEqual(saved["origins"][0]["localStorage"][0]["value"], "fixture")

    def test_request_guard_blocks_out_of_scope_navigation(self):
        client = self.client()
        with self.assertRaisesRegex(BrowserError, "outside allowed domains"):
            client.read("https://outside.example/page", domains=["publisher.example"])

    def test_request_guard_also_blocks_out_of_scope_subresources(self):
        client = self.client()
        with patch.dict(globals(), {"HTML": HTML + '<img src="https://outside.example/image.png">'}):
            client.read(PAGE, domains=["publisher.example"])
        self.assertTrue(any("outside.example/image.png" in message for message in client.browser.blocked))

    def test_full_cli_pipeline_with_chromium(self):
        original_enter = Browser.__enter__

        def enter(browser):
            result = original_enter(browser)
            install_fixtures(browser)
            return result

        def request(http, url):
            return Response(PDF, url)

        manifest = self.root / "results.csv"
        with patch.object(Browser, "__enter__", enter), patch.object(j.HTTP, "_request", request):
            status = j.main(
                [
                    "run",
                    "--browser",
                    "--url",
                    PAGE,
                    "--wait-selector",
                    "#dynamic",
                    "--wait-ms",
                    "0",
                    "--click-selector",
                    "#download",
                    "--max-pages",
                    "1",
                    "--depth",
                    "0",
                    "--delay",
                    "0.01",
                    "--allow-domain",
                    "publisher.example",
                    "--manifest",
                    str(manifest),
                    "--outdir",
                    str(self.root / "files"),
                ]
            )
        self.assertEqual(status, 0)
        records = j.read_manifest(manifest)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.status == "downloaded" for record in records))
