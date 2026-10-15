import io
import json
import socket
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import junkman as j

PDF = b"%PDF-1.4\nexample document\n%%EOF\n"
PAGE = "https://publisher.example/resources"
DOC = "https://publisher.example/sample.pdf"


class Response(io.BytesIO):
    def __init__(self, body, url=DOC, content_type="application/pdf", length=None):
        super().__init__(body)
        self.status = 200
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def geturl(self):
        return self.url


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    @contextmanager
    def open(self, url, **kwargs):
        self.calls.append(url)
        if kwargs.get("domains") and not j.allowed(url, kwargs["domains"]):
            raise j.CrawlError("URL outside allowed domains")
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        body, kind, length = route
        with Response(body, url, kind, length) as response:
            yield response

    def read(self, url, **kwargs):
        with self.open(url, **kwargs) as response:
            return (
                response.geturl(),
                j.bounded_read(response, j.HTML_LIMIT),
                response.headers.get_content_type(),
            )

    def discover_actions(self, url):
        return []


class URLTests(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(
            j.canonical_url("https://EXAMPLE.com:443/file.pdf#page=2"), "https://example.com/file.pdf"
        )

    def test_reject_unsafe_urls(self):
        for url in [
            "http://example.com/a",
            "file:///etc/passwd",
            "https://u:p@example.com/",
            "https://example.com:22/",
        ]:
            with self.subTest(url=url), self.assertRaises(j.CrawlError):
                j.canonical_url(url)

    def test_reject_private_and_mixed_dns(self):
        for addresses in [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"]]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
            with patch.object(j.socket, "getaddrinfo", return_value=records), self.assertRaises(j.CrawlError):
                j.public_url("https://example.com/")

    def test_domain_boundary(self):
        self.assertTrue(j.allowed("https://cdn.cambridge.org/a", ["cambridge.org"]))
        self.assertFalse(j.allowed("https://notcambridge.org/a", ["cambridge.org"]))
        self.assertFalse(j.allowed("https://cambridge.org.evil.example/a", ["cambridge.org"]))


class HTTPTests(unittest.TestCase):
    def test_redirect_checked_before_request(self):
        client = j.HTTP(delay=0)
        headers = Message()
        headers["Location"] = "https://private.example/a"
        client._request = Mock(side_effect=HTTPError(PAGE, 302, "redirect", headers, io.BytesIO()))
        with (
            patch.object(j, "public_url", side_effect=[PAGE, j.CrawlError("private")]),
            self.assertRaises(j.CrawlError),
            client.open(PAGE, robots=False),
        ):
            pass
        self.assertEqual(client._request.call_count, 1)

    def test_redirect_stays_in_domain_allowlist(self):
        client = j.HTTP(delay=0)
        headers = Message()
        headers["Location"] = "https://elsewhere.example/a.pdf"
        client._request = Mock(side_effect=HTTPError(PAGE, 302, "redirect", headers, io.BytesIO()))
        client.check_robots = Mock()
        with (
            patch.object(j, "public_url", side_effect=lambda url: url),
            self.assertRaises(j.CrawlError),
            client.open(PAGE, domains=["publisher.example"]),
        ):
            pass
        self.assertEqual(client._request.call_count, 1)

    def test_robots_disallow_and_cache(self):
        client = j.HTTP(delay=0)
        client._request = Mock(
            return_value=Response(b"User-agent: *\nDisallow: /resources\n", PAGE, "text/plain")
        )
        with patch.object(j, "public_url", side_effect=lambda url: url):
            for _ in range(2):
                with self.assertRaises(j.CrawlError):
                    client.check_robots(PAGE)
        self.assertEqual(client._request.call_count, 1)

    def test_robots_missing_is_allowed_but_forbidden_is_not(self):
        for code in (404, 403, 503):
            client = j.HTTP(delay=0)
            client._request = Mock(side_effect=HTTPError(PAGE, code, "error", Message(), io.BytesIO()))
            with patch.object(j, "public_url", side_effect=lambda url: url):
                if code == 404:
                    client.check_robots(PAGE)
                else:
                    with self.assertRaises(j.CrawlError):
                        client.check_robots(PAGE)

    def test_robots_crawl_delay(self):
        client = j.HTTP(delay=0)
        client._request = Mock(return_value=Response(b"User-agent: *\nCrawl-delay: 3\nAllow: /\n"))
        with patch.object(j, "public_url", side_effect=lambda url: url):
            client.check_robots(PAGE)
        self.assertEqual(client.delay, 3)

    def test_robots_html_is_not_permission(self):
        client = j.HTTP(delay=0)
        client._request = Mock(return_value=Response(b"<html>challenge</html>"))
        with patch.object(j, "public_url", side_effect=lambda url: url), self.assertRaises(j.CrawlError):
            client.check_robots(PAGE)

    def test_reject_unsolicited_partial_response(self):
        client = j.HTTP(delay=0)
        response = Response(PDF)
        response.status = 206
        client._request = Mock(return_value=response)
        with (
            patch.object(j, "public_url", side_effect=lambda url: url),
            self.assertRaises(j.CrawlError),
            client.open(DOC, robots=False),
        ):
            pass
        self.assertTrue(response.closed)

    def test_transient_retry_but_not_forbidden(self):
        client = j.HTTP(delay=0)
        client.opener = Mock()
        client.opener.open.side_effect = [
            HTTPError(DOC, 503, "busy", Message(), io.BytesIO()),
            Response(PDF),
        ]
        with patch.object(j.time, "sleep"):
            self.assertEqual(client._request(DOC).read(), PDF)
        self.assertEqual(client.opener.open.call_count, 2)
        client.opener.open.reset_mock()
        client.opener.open.side_effect = HTTPError(DOC, 403, "forbidden", Message(), io.BytesIO())
        with self.assertRaises(HTTPError):
            client._request(DOC)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_bounded_read(self):
        with self.assertRaises(j.CrawlError):
            j.bounded_read(io.BytesIO(b"abcd"), 3)


class DiscoveryTests(unittest.TestCase):
    def test_relative_links_dedup_depth_and_domain(self):
        html = b"""<a href="/sample.pdf#p1">Sample</a><a href="/sample.pdf">Again</a>
        <a href="/level2">More</a><a href="https://evil.example/a.pdf">Other</a>"""
        http = FakeHTTP(
            {
                PAGE: (html, "text/html", None),
                "https://publisher.example/level2": (
                    b'<a href="/workbook.pdf">Workbook</a>',
                    "text/html",
                    None,
                ),
            }
        )
        records, errors = j.discover(
            http,
            [(PAGE, "")],
            domains=["publisher.example"],
            formats={"pdf"},
            max_pages=2,
            depth=1,
            limit=10,
        )
        self.assertEqual([r.url for r in records], [DOC, "https://publisher.example/workbook.pdf"])
        self.assertEqual(records[0].source_url, PAGE)
        self.assertEqual(errors, [])

    def test_direct_document_does_not_fetch_until_download(self):
        http = FakeHTTP({})
        records, errors = j.discover(
            http,
            [(DOC, "Sample")],
            domains=[],
            formats={"pdf"},
            max_pages=1,
            depth=0,
            limit=1,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(http.calls, [])
        self.assertEqual(errors, [])

    def test_page_budget_depth_and_document_limit(self):
        routes = {
            PAGE: (
                b'<a href="/next">Next</a><a href="/a.pdf">A</a><a href="/b.pdf">B</a>',
                "text/html",
                None,
            ),
        }
        for depth, limit, count in [(0, 10, 2), (1, 1, 1)]:
            http = FakeHTTP(routes)
            records, _ = j.discover(
                http, [(PAGE, "")], domains=[], formats={"pdf"}, max_pages=1, depth=depth, limit=limit
            )
            self.assertEqual(len(records), count)
            self.assertEqual(http.calls, [PAGE])

    def test_failed_page_is_reported(self):
        http = FakeHTTP({PAGE: URLError("blocked")})
        records, errors = j.discover(
            http, [(PAGE, "")], domains=[], formats={"pdf"}, max_pages=1, depth=0, limit=1
        )
        self.assertEqual(records, [])
        self.assertIn("blocked", errors[0])

    def test_search_rss(self):
        http = Mock()
        http.read.return_value = (
            "",
            b"<rss><channel><item><title>Sample</title><link>https://publisher.example/sample.pdf</link></item></channel></rss>",
            "",
        )
        self.assertEqual(j.search_web(http, "Power Up", 2), [(DOC, "Sample")])

    def test_search_fallback_and_challenge_error(self):
        http = Mock()
        http.read.side_effect = [
            ("", b"<html>blocked</html>", ""),
            (
                "",
                b'<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpublisher.example%2Fsample.pdf">Sample</a>',
                "",
            ),
        ]
        self.assertEqual(j.search_web(http, "Power Up", 2), [(DOC, "Sample")])
        http.read.side_effect = [("", b"<html>blocked</html>", "")] * 2
        with self.assertRaises(j.CrawlError):
            j.search_web(http, "Power Up", 2)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.record = j.Resource.create(DOC, "Sample", PAGE, "pdf")
        self.http = FakeHTTP({DOC: (PDF, "application/pdf", len(PDF))})

    def download(self, **kwargs):
        return j.download_one(
            self.http,
            self.record,
            self.root,
            domains=kwargs.get("domains", []),
            max_bytes=kwargs.get("max_bytes", 1024),
            force=kwargs.get("force", False),
        )

    def test_download_provenance_and_verified_resume(self):
        self.download()
        self.assertEqual(Path(self.record.path).read_bytes(), PDF)
        meta = json.loads((self.root / f"{self.record.id}.json").read_text())
        self.assertEqual(meta["source_url"], PAGE)
        self.assertEqual(meta["sha256"], j.file_hash(Path(self.record.path)))
        self.download()
        self.assertEqual(self.record.status, "skipped")
        self.assertEqual(len(self.http.calls), 1)

    def test_corrupt_download_is_fetched_again(self):
        self.download()
        Path(self.record.path).write_bytes(b"%PDF-corrupt")
        self.download()
        self.assertEqual(self.record.status, "downloaded")
        self.assertEqual(Path(self.record.path).read_bytes(), PDF)
        self.assertEqual(len(self.http.calls), 2)

    def test_broken_metadata_is_fetched_again(self):
        self.download()
        (self.root / f"{self.record.id}.json").write_text("{broken")
        self.download()
        self.assertEqual(len(self.http.calls), 2)

    def test_failure_never_leaves_completed_or_partial_file(self):
        for body, kind, length, limit in [
            (b"<html>login</html>", "text/html", None, 1024),
            (b"<html>login</html>", "application/pdf", None, 1024),
            (PDF, "application/pdf", 999, 1024),
            (PDF, "application/pdf", None, 4),
            (PDF, "application/pdf", len(PDF), 4),
            (b"", "application/pdf", 0, 1024),
        ]:
            with self.subTest(body=body, kind=kind, length=length, limit=limit):
                self.http.routes[DOC] = (body, kind, length)
                with self.assertRaises(j.CrawlError):
                    self.download(max_bytes=limit)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_force_preserves_original_file(self):
        self.download()
        self.http.routes[DOC] = (b"challenge", "application/pdf", None)
        with self.assertRaises(j.CrawlError):
            self.download(force=True)
        self.assertEqual(Path(self.record.path).read_bytes(), PDF)
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_domain_filter_enforced(self):
        with self.assertRaises(j.CrawlError):
            self.download(domains=["cambridge.org"])
        self.assertEqual(self.http.calls, [])
        self.download()
        with self.assertRaises(j.CrawlError):
            self.download(domains=["cambridge.org"])

    def test_force_redownloads(self):
        self.download()
        self.download(force=True)
        self.assertEqual(len(self.http.calls), 2)

    def test_manifest_roundtrip_and_bad_record(self):
        manifest = self.root / "results.csv"
        j.write_manifest(manifest, [self.record])
        self.assertEqual(j.read_manifest(manifest), [self.record])
        self.record.id = "../../escape"
        j.write_manifest(manifest, [self.record])
        with self.assertRaises(j.CrawlError):
            j.read_manifest(manifest)

    def test_duplicate_and_truncated_manifest_rows(self):
        manifest = self.root / "results.csv"
        j.write_manifest(manifest, [self.record, self.record])
        with self.assertRaises(j.CrawlError):
            j.read_manifest(manifest)
        manifest.write_text(",".join(j.Resource.__dataclass_fields__) + "\na,b\n")
        with self.assertRaises(j.CrawlError):
            j.read_manifest(manifest)

    def test_failed_row_does_not_block_next_and_can_resume(self):
        bad = j.Resource.create("https://publisher.example/bad.pdf", "", PAGE, "pdf")
        self.http.routes[bad.url] = (b"login", "text/html", None)
        manifest = self.root / "results.csv"
        records = [bad, self.record]
        failures = j.download_all(
            self.http, records, manifest, self.root, domains=[], max_bytes=1024, force=False
        )
        self.assertEqual(failures, 1)
        saved = j.read_manifest(manifest)
        self.assertEqual([r.status for r in saved], ["error", "downloaded"])
        self.http.routes[bad.url] = (PDF, "application/pdf", None)
        failures = j.download_all(
            self.http, saved, manifest, self.root, domains=[], max_bytes=1024, force=False
        )
        self.assertEqual(failures, 0)
        self.assertEqual([r.status for r in saved], ["downloaded", "skipped"])

    def test_container_and_text_validation(self):
        for kind, members in [
            ("zip", {"readme.txt": b"hello"}),
            ("docx", {"[Content_Types].xml": b"", "word/document.xml": b""}),
            ("pptx", {"[Content_Types].xml": b"", "ppt/presentation.xml": b""}),
            ("xlsx", {"[Content_Types].xml": b"", "xl/workbook.xml": b""}),
            ("epub", {"META-INF/container.xml": b""}),
        ]:
            path = self.root / f"sample.{kind}"
            with zipfile.ZipFile(path, "w") as archive:
                for name, body in members.items():
                    archive.writestr(name, body)
            j.verify_document(path, kind)
            path.write_bytes(b"<html>not a zip</html>")
            with self.assertRaises(j.CrawlError):
                j.verify_document(path, kind)
        path = self.root / "sample.txt"
        path.write_text("hello")
        j.verify_document(path, "txt")
        path.write_text("<html>login</html>")
        with self.assertRaises(j.CrawlError):
            j.verify_document(path, "txt")


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "results.csv"
        self.http = FakeHTTP(
            {
                PAGE: (b'<a href="/sample.pdf">Sample</a>', "text/html", None),
                DOC: (PDF, "application/pdf", len(PDF)),
            }
        )

    def test_run_then_resume_end_to_end(self):
        with patch.object(j, "HTTP", return_value=self.http):
            args = [
                "run",
                "--url",
                PAGE,
                "--manifest",
                str(self.manifest),
                "--outdir",
                str(self.root / "files"),
            ]
            self.assertEqual(j.main(args), 0)
            self.assertEqual(j.read_manifest(self.manifest)[0].status, "downloaded")
            self.assertEqual(
                j.main(["download", "--manifest", str(self.manifest), "--outdir", str(self.root / "files")]),
                0,
            )
            self.assertEqual(j.read_manifest(self.manifest)[0].status, "skipped")
            self.assertEqual(self.http.calls.count(DOC), 1)
            self.assertEqual(j.main(args), 1)

    def test_search_is_discovery_only(self):
        with patch.object(j, "HTTP", return_value=self.http):
            self.assertEqual(j.main(["search", "--url", PAGE, "--manifest", str(self.manifest)]), 0)
        self.assertEqual(self.http.calls, [PAGE])
        self.assertEqual(j.read_manifest(self.manifest)[0].status, "discovered")

    def test_partial_search_failure_remains_nonzero_and_persisted(self):
        with (
            patch.object(j, "HTTP", return_value=self.http),
            patch.object(j, "search_web", side_effect=j.CrawlError("challenge")),
        ):
            status = j.main(["search", "query", "--url", PAGE, "--manifest", str(self.manifest)])
        self.assertEqual(status, 1)
        self.assertEqual(len(j.read_manifest(self.manifest)), 1)
        meta = json.loads(self.manifest.with_suffix(".discovery.json").read_text())
        self.assertIn("challenge", meta["errors"][0])

    def test_no_results_is_nonzero(self):
        self.http.routes[PAGE] = (b"<html>No links</html>", "text/html", None)
        with patch.object(j, "HTTP", return_value=self.http):
            self.assertEqual(j.main(["search", "--url", PAGE, "--manifest", str(self.manifest)]), 1)

    def test_invalid_numeric_options(self):
        for option, value in [("--limit", "0"), ("--depth", "-1"), ("--delay", "nan"), ("--timeout", "inf")]:
            with (
                self.subTest(option=option),
                patch("sys.stderr", new_callable=io.StringIO),
                self.assertRaises(SystemExit),
            ):
                j.build_parser().parse_args(["search", "query", option, value])


if __name__ == "__main__":
    unittest.main()
