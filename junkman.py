"""Bounded document discovery and verified downloads using the standard library."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.client
import json
import logging
import os
import socket
import sys
import tempfile
import time
import zipfile
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from ipaddress import ip_address
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from junkman_browser import Browser, BrowserError

LOG = logging.getLogger("junkman")
USER_AGENT = "Junkman/0.1"
FORMATS = {"pdf", "docx", "pptx", "xlsx", "epub", "zip", "txt"}
HTML_LIMIT = 2 * 1024 * 1024
NETWORK_ERRORS = (URLError, OSError, http.client.HTTPException)


class CrawlError(Exception):
    """An actionable discovery, access, or integrity failure."""


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise CrawlError(f"Only public HTTPS URLs on port 443 are supported: {url}")
    host = parts.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def public_url(url: str) -> str:
    url = canonical_url(url)
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
        raise CrawlError(f"Non-public network destination is not allowed: {host}")
    return url


def allowed(url: str, domains: list[str]) -> bool:
    host = urlsplit(url).hostname or ""
    return not domains or any(host == domain or host.endswith("." + domain) for domain in domains)


def document_format(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in FORMATS else ""


def bounded_read(response, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise CrawlError(f"Response exceeds {limit} bytes")
    return body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def __init__(self, delay: float = 1.0, timeout: float = 30.0):
        self.delay = delay
        self.timeout = timeout
        self.last_request = 0.0
        self.robots: dict[str, RobotFileParser] = {}
        # Do not let proxy environment variables route public requests into a LAN.
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(), NoRedirect())

    def _request(self, url: str):
        for attempt in range(3):
            time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                return self.opener.open(
                    Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}),
                    timeout=self.timeout,
                )
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                wait = min(float(retry_after), 60.0) if retry_after.isdigit() else 2**attempt
                exc.close()
            except (URLError, OSError, http.client.HTTPException):
                if attempt == 2:
                    raise
                wait = 2**attempt
            LOG.warning("Retrying %s after a transient request failure", url)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            parser = RobotFileParser()
            robots_url = origin + "/robots.txt"
            try:
                with self.open(robots_url, robots=False) as response:
                    text = bounded_read(response, 512 * 1024).decode("utf-8", errors="replace")
                    if "<html" in text.lower() or "<!doctype html" in text.lower():
                        raise CrawlError(f"robots.txt returned HTML: {robots_url}")
                    parser.parse(text.splitlines())
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code in (401, 403):
                    parser.parse(["User-agent: *", "Disallow: /"])
                elif code in (404, 410):
                    parser.parse([])
                else:
                    raise CrawlError(f"Cannot read robots.txt: HTTP {code} at {robots_url}") from exc
            self.robots[origin] = parser
        parser = self.robots[origin]
        if not parser.can_fetch(USER_AGENT, url):
            raise CrawlError(f"robots.txt disallows access: {url}")
        crawl_delay = parser.crawl_delay(USER_AGENT)
        request_rate = parser.request_rate(USER_AGENT)
        self.delay = max(
            self.delay,
            float(crawl_delay or 0),
            request_rate.seconds / request_rate.requests if request_rate else 0,
        )

    @contextmanager
    def open(self, url: str, *, robots: bool = True, domains: list[str] | None = None):
        for _ in range(6):
            url = public_url(url)
            if domains and not allowed(url, domains):
                raise CrawlError(f"Redirect or URL outside allowed domains: {url}")
            if robots:
                self.check_robots(url)
            try:
                response = self._request(url)
            except HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    exc.close()
                    raise
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise CrawlError(f"Redirect without Location: {url}") from exc
                url = canonical_url(urljoin(url, location))
                continue
            try:
                if response.status != 200:
                    raise CrawlError(
                        f"Expected a complete HTTP 200 response, received {response.status}: {url}"
                    )
                yield response
            finally:
                response.close()
            return
        raise CrawlError(f"Too many redirects: {url}")

    def read(self, url: str, *, domains: list[str] | None = None) -> tuple[str, bytes, str]:
        with self.open(url, domains=domains) as response:
            return (
                response.geturl(),
                bounded_read(response, HTML_LIMIT),
                response.headers.get_content_type(),
            )

    def discover_actions(self, url: str) -> list[Resource]:
        return []

    def download_action(self, resource: Resource, max_bytes: int):
        raise CrawlError("Button-download records require --browser")


class BrowserHTTP(HTTP):
    def __init__(self, args):
        super().__init__(args.delay, args.timeout)
        self.domains = args.allow_domain
        self.active_domains = self.domains
        self.selectors = getattr(args, "click_selector", [])
        self.click_format = getattr(args, "click_format", "pdf")
        self.cookie_jar = CookieJar(
            policy=DefaultCookiePolicy(
                strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
            )
        )
        self.opener.add_handler(HTTPCookieProcessor(self.cookie_jar))
        self.browser = Browser(
            check_url=self.check_browser_url,
            timeout=args.timeout,
            user_agent=USER_AGENT,
            headed=args.headed,
            storage_state=args.storage_state if args.storage_state and args.storage_state.exists() else None,
            screenshot_dir=args.screenshot_dir,
            wait_selector=args.wait_selector,
            wait_ms=args.wait_ms,
        )

    def check_browser_url(self, url: str) -> None:
        try:
            url = public_url(url)
            if not allowed(url, self.active_domains):
                raise CrawlError(f"URL outside allowed domains: {url}")
            self.check_robots(url)
            time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
        except (CrawlError, ValueError, *NETWORK_ERRORS) as exc:
            raise BrowserError(str(exc)) from exc

    def sync_cookies(self) -> None:
        self.cookie_jar.clear()
        for cookie in self.browser.cookies():
            if cookie.get("partitionKey"):
                LOG.warning(
                    "Cannot transfer a partitioned cookie for %s to HTTP; use the browser download button",
                    cookie["domain"],
                )
                continue
            domain = cookie["domain"]
            expires = cookie.get("expires", -1)
            self.cookie_jar.set_cookie(
                Cookie(
                    version=0,
                    name=cookie["name"],
                    value=cookie["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=cookie["path"],
                    path_specified=True,
                    secure=cookie["secure"],
                    expires=int(expires) if expires > 0 else None,
                    discard=expires <= 0,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": cookie.get("httpOnly", False)},
                )
            )

    @contextmanager
    def open(self, url: str, *, robots: bool = True, domains: list[str] | None = None):
        self.sync_cookies()
        with super().open(url, robots=robots, domains=domains) as response:
            yield response

    def read(self, url: str, *, domains: list[str] | None = None) -> tuple[str, bytes, str]:
        self.active_domains = domains or []
        result = self.browser.render(url)
        if len(result[1]) > HTML_LIMIT:
            raise CrawlError(f"Rendered HTML exceeds {HTML_LIMIT} bytes: {url}")
        self.sync_cookies()
        return result

    def discover_actions(self, url: str) -> list[Resource]:
        return [
            Resource.create(url, f"Button: {selector}", url, self.click_format, selector=selector)
            for selector in self.browser.matching_selectors(self.selectors)
        ]

    def download_action(self, resource: Resource, max_bytes: int):
        self.active_domains = self.domains
        return self.browser.download(resource.url, resource.selector, max_bytes)

    def search_browser(self, query: str, limit: int) -> list[tuple[str, str]]:
        failures = []
        providers = [
            ("bing", "https://www.bing.com/search?" + urlencode({"q": query}), "li.b_algo h2 a"),
            ("duckduckgo", "https://html.duckduckgo.com/html/?" + urlencode({"q": query}), "a.result__a"),
        ]
        for name, url, selector in providers:
            try:
                self.read(url)
                found: dict[str, str] = {}
                for target, title in self.browser.result_links(selector):
                    parts = urlsplit(target)
                    params = parse_qs(parts.query)
                    if parts.hostname in ("duckduckgo.com", "html.duckduckgo.com") and "uddg" in params:
                        target = params["uddg"][0]
                    elif parts.hostname in ("www.bing.com", "cn.bing.com") and parts.path == "/ck/a":
                        encoded = params.get("u", [""])[0]
                        if not encoded.startswith("a1"):
                            continue
                        encoded = encoded[2:]
                        target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
                    if urlsplit(target).scheme == "https":
                        found.setdefault(canonical_url(target), title)
                if found:
                    return list(found.items())[:limit]
                raise BrowserError("No usable browser results (empty response or search challenge)")
            except (BrowserError, CrawlError, ValueError, *NETWORK_ERRORS) as exc:
                LOG.warning("%s browser search failed: %s", name, exc)
                failures.append(f"{name}: {exc}")
        raise CrawlError("All browser search providers failed; use --url. " + "; ".join(failures))


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join(" ".join(self.text).split())))
            self.href = None


def search_web(http: HTTP, query: str, limit: int) -> list[tuple[str, str]]:
    if isinstance(http, BrowserHTTP):
        return http.search_browser(query, limit)
    providers = [
        ("bing", "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})),
        ("duckduckgo", "https://html.duckduckgo.com/html/?" + urlencode({"q": query})),
    ]
    failures = []
    for name, url in providers:
        try:
            _, body, _ = http.read(url)
            if name == "bing":
                root = ElementTree.fromstring(body)
                if root.tag != "rss":
                    raise CrawlError("Search returned a non-RSS response")
                results = [
                    (item.findtext("link", ""), item.findtext("title", ""))
                    for item in root.findall("./channel/item")
                ]
            else:
                parser = Links()
                parser.feed(body.decode("utf-8", errors="replace"))
                results = []
                for href, title in parser.links:
                    target = parse_qs(urlsplit(href).query).get("uddg", [""])[0]
                    if target:
                        results.append((target, title))
            found: dict[str, str] = {}
            for target, title in results:
                if urlsplit(target).scheme == "https":
                    found.setdefault(canonical_url(target), title)
            if not found:
                raise CrawlError("No usable results (empty response or search challenge)")
            return list(found.items())[:limit]
        except (CrawlError, ValueError, ElementTree.ParseError, *NETWORK_ERRORS) as exc:
            LOG.warning("%s search failed: %s", name, exc)
            failures.append(f"{name}: {exc}")
    raise CrawlError("All search providers failed; use --url. " + "; ".join(failures))


@dataclass
class Resource:
    id: str
    title: str
    url: str
    source_url: str
    format: str
    status: str = "discovered"
    path: str = ""
    sha256: str = ""
    error: str = ""
    selector: str = ""

    @classmethod
    def create(cls, url: str, title: str, source_url: str, kind: str, selector: str = "") -> Resource:
        url = canonical_url(url)
        identity = url + ("\n" + selector if selector else "")
        return cls(
            hashlib.sha256(identity.encode()).hexdigest()[:20],
            title or url,
            url,
            source_url,
            kind,
            selector=selector,
        )


def discover(
    http: HTTP,
    seeds: list[tuple[str, str]],
    *,
    domains: list[str],
    formats: set[str],
    max_pages: int,
    depth: int,
    limit: int,
) -> tuple[list[Resource], list[str]]:
    queue = deque((canonical_url(url), title, 0, url) for url, title in seeds)
    visited: set[str] = set()
    resources: dict[str, Resource] = {}
    errors: list[str] = []
    pages = 0
    while queue and len(resources) < limit:
        url, title, level, source = queue.popleft()
        if url in visited or not allowed(url, domains):
            continue
        visited.add(url)
        kind = document_format(url)
        if kind:
            if kind in formats:
                resources[url] = Resource.create(url, title, source, kind)
            continue
        if pages >= max_pages:
            continue
        pages += 1
        try:
            final_url, body, content_type = http.read(url, domains=domains)
            if content_type != "text/html":
                raise CrawlError(f"Expected an HTML landing page, received {content_type}: {url}")
            parser = Links()
            parser.feed(body.decode("utf-8", errors="replace"))
            for resource in http.discover_actions(final_url):
                if resource.format in formats:
                    resources.setdefault(resource.id, resource)
                    if len(resources) >= limit:
                        break
            if len(resources) >= limit:
                continue
            for href, label in parser.links:
                target = urljoin(final_url, href)
                if urlsplit(target).scheme.lower() != "https":
                    continue
                target = canonical_url(target)
                if not allowed(target, domains):
                    continue
                kind = document_format(target)
                if kind in formats:
                    resources.setdefault(target, Resource.create(target, label, final_url, kind))
                    if len(resources) >= limit:
                        break
                elif not kind and level < depth and urlsplit(target).hostname == urlsplit(final_url).hostname:
                    queue.append((target, label, level + 1, final_url))
        except (CrawlError, BrowserError, ValueError, *NETWORK_ERRORS) as exc:
            message = f"{url}: {exc}"
            LOG.error("Discovery failed: %s", message)
            errors.append(message)
    return list(resources.values()), errors


@contextmanager
def atomic_file(path: Path, mode: str = "w", validate: Callable[[Path], None] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    temporary = Path(name)
    try:
        kwargs = {"encoding": "utf-8", "newline": ""} if mode == "w" else {}
        with os.fdopen(fd, mode, **kwargs) as stream:
            yield stream
        if validate:
            validate(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_manifest(path: Path, resources: list[Resource]) -> None:
    with atomic_file(path) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(Resource.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(resource) for resource in resources)


def read_manifest(path: Path) -> list[Resource]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(Resource.__dataclass_fields__)
        if reader.fieldnames not in (fields, fields[:-1]):
            raise CrawlError(f"Unexpected manifest columns: {path}")
        resources = []
        for row in reader:
            if None in row or any(not isinstance(value, str) for value in row.values()):
                raise CrawlError(f"Malformed manifest row at line {reader.line_num}")
            resources.append(Resource(**row))
    seen: set[str] = set()
    for resource in resources:
        url = canonical_url(resource.url)
        expected = Resource.create(url, "", "", resource.format, selector=resource.selector).id
        if resource.id != expected or resource.id in seen or resource.format not in FORMATS:
            raise CrawlError(f"Invalid or duplicate manifest record: {resource.id}")
        seen.add(resource.id)
    return resources


def verify_document(path: Path, kind: str) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(1024)
    if not prefix:
        raise CrawlError("Empty document")
    if kind == "pdf":
        if not prefix.startswith(b"%PDF-"):
            raise CrawlError("Not a PDF (possibly a login page or bot challenge)")
    elif kind in {"zip", "docx", "pptx", "xlsx", "epub"}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                required = {
                    "docx": "[Content_Types].xml",
                    "pptx": "[Content_Types].xml",
                    "xlsx": "[Content_Types].xml",
                    "epub": "META-INF/container.xml",
                }.get(kind)
                folder = {"docx": "word/", "pptx": "ppt/", "xlsx": "xl/"}.get(kind)
                if required and required not in names:
                    raise CrawlError(f"Not a valid {kind} container")
                if folder and not any(name.startswith(folder) for name in names):
                    raise CrawlError(f"Not a valid {kind} document")
        except zipfile.BadZipFile as exc:
            raise CrawlError(f"Not a valid {kind} archive") from exc
    elif kind == "txt":
        text = path.read_bytes().decode("utf-8-sig")
        if "\x00" in text or "<html" in text.lower() or "<!doctype html" in text.lower():
            raise CrawlError("Not UTF-8 plain text")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_limited(source, destination, max_bytes: int) -> int:
    total = 0
    while True:
        chunk = source.read(64 * 1024)
        if not chunk:
            return total
        total += len(chunk)
        if total > max_bytes:
            raise CrawlError(f"Document exceeds the {max_bytes}-byte limit")
        destination.write(chunk)


def download_one(
    http: HTTP,
    resource: Resource,
    outdir: Path,
    *,
    domains: list[str],
    max_bytes: int,
    force: bool,
) -> None:
    if not allowed(resource.url, domains):
        raise CrawlError(f"URL outside allowed domains: {resource.url}")
    destination = outdir / f"{resource.id}.{resource.format}"
    metadata = outdir / f"{resource.id}.json"
    if not force and destination.exists() and metadata.exists():
        try:
            record = json.loads(metadata.read_text(encoding="utf-8"))
            if (
                record["url"] == resource.url
                and record.get("selector", "") == resource.selector
                and record["sha256"] == file_hash(destination)
                and destination.stat().st_size <= max_bytes
            ):
                verify_document(destination, resource.format)
                resource.path = str(destination.resolve())
                resource.sha256 = record["sha256"]
                resource.status = "skipped"
                resource.error = ""
                return
            LOG.warning("Re-downloading %s: saved integrity or size check failed", resource.url)
        except (KeyError, TypeError, ValueError, CrawlError, OSError) as exc:
            LOG.warning("Re-downloading %s: invalid saved completion record: %s", resource.url, exc)
    suggested_filename = ""
    if resource.selector:
        with http.download_action(resource, max_bytes) as captured:
            with (
                captured.path.open("rb") as source,
                atomic_file(
                    destination,
                    "wb",
                    validate=lambda path: verify_document(path, resource.format),
                ) as stream,
            ):
                total = copy_limited(source, stream, max_bytes)
            final_url = captured.url
            suggested_filename = captured.filename
            content_type = "application/octet-stream"
    else:
        with http.open(resource.url, domains=domains) as response:
            content_type = response.headers.get_content_type()
            if content_type in ("text/html", "application/xhtml+xml"):
                raise CrawlError("Received HTML instead of a document")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise CrawlError("Unexpected compressed HTTP response")
            length = response.headers.get("Content-Length")
            expected_length = int(length) if length is not None else None
            if expected_length is not None and (expected_length < 0 or expected_length > max_bytes):
                raise CrawlError(f"Document exceeds the {max_bytes}-byte limit or has invalid length")
            final_url = response.geturl()
            with atomic_file(
                destination,
                "wb",
                validate=lambda path: verify_document(path, resource.format),
            ) as stream:
                total = copy_limited(response, stream, max_bytes)
                if expected_length is not None and total != expected_length:
                    raise CrawlError("Truncated response: Content-Length does not match")
    digest = file_hash(destination)
    record = {
        "url": resource.url,
        "final_url": final_url,
        "source_url": resource.source_url,
        "title": resource.title,
        "format": resource.format,
        "selector": resource.selector,
        "suggested_filename": suggested_filename,
        "transfer": "playwright" if resource.selector else "http",
        "sha256": digest,
        "bytes": total,
        "content_type": content_type,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "access_note": "Public URL; copyright and reuse license not verified.",
    }
    with atomic_file(metadata) as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    resource.path = str(destination.resolve())
    resource.sha256 = digest
    resource.status = "downloaded"
    resource.error = ""


def download_all(
    http: HTTP,
    resources: list[Resource],
    manifest: Path,
    outdir: Path,
    *,
    domains: list[str],
    max_bytes: int,
    force: bool,
) -> int:
    failures = 0
    for resource in resources:
        try:
            download_one(http, resource, outdir, domains=domains, max_bytes=max_bytes, force=force)
            LOG.info("%s %s", resource.status, resource.url)
        except (CrawlError, BrowserError, ValueError, *NETWORK_ERRORS) as exc:
            resource.status = "error"
            resource.path = ""
            resource.sha256 = ""
            resource.error = str(exc)
            failures += 1
            LOG.error("Download failed for %s: %s", resource.url, exc)
        write_manifest(manifest, resources)
    return failures


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def nonnegative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


def positive_float(value: str) -> float:
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def domain_name(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if not value or "/" in value or ":" in value or "@" in value or " " in value:
        raise argparse.ArgumentTypeError("use a bare domain, for example cambridge.org")
    return value


def browser_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--browser", action="store_true", help="Enable optional Playwright Chromium mode")
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window")
    parser.add_argument("--storage-state", type=Path, help="Sensitive Playwright session JSON")
    parser.add_argument("--screenshot-dir", type=Path, help="Save private debug screenshots")
    parser.add_argument("--wait-selector", help="Wait for a rendered CSS selector")
    parser.add_argument(
        "--wait-ms", type=nonnegative_int, default=500, help="Extra render wait in milliseconds"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("search", "download", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--manifest", type=Path, default=Path("results.csv"))
        sub.add_argument("--allow-domain", action="append", default=[], type=domain_name)
        sub.add_argument("--delay", type=positive_float, default=1.0)
        sub.add_argument("--timeout", type=positive_float, default=30.0)
        browser_options(sub)
        if command != "download":
            sub.add_argument("query", nargs="?", help="Search keywords, not an LLM instruction")
            sub.add_argument("--url", action="append", default=[], help="HTTPS page or document URL")
            sub.add_argument("--limit", type=positive_int, default=20)
            sub.add_argument("--max-pages", type=positive_int, default=20)
            sub.add_argument("--depth", type=nonnegative_int, default=1)
            sub.add_argument("--formats", default="pdf,docx,pptx,xlsx,epub,zip,txt")
            sub.add_argument("--overwrite-manifest", action="store_true")
            sub.add_argument(
                "--click-selector", action="append", default=[], help="One download button per selector"
            )
            sub.add_argument("--click-format", choices=sorted(FORMATS), default="pdf")
        if command != "search":
            sub.add_argument("--outdir", type=Path, default=Path("downloads"))
            sub.add_argument("--max-mb", type=positive_float, default=50.0)
            sub.add_argument("--force", action="store_true")
    login = commands.add_parser("login", help="Manually log in and save a private Playwright session")
    login.add_argument("--url", required=True)
    login.add_argument("--allow-domain", action="append", default=[], type=domain_name)
    login.add_argument("--delay", type=positive_float, default=1.0)
    login.add_argument("--timeout", type=positive_float, default=30.0)
    browser_options(login)
    login.set_defaults(browser=True, headed=True)
    return parser


def execute(args, http: HTTP, parser: argparse.ArgumentParser) -> int:
    if args.command == "login":
        if not isinstance(http, BrowserHTTP):
            raise CrawlError("login requires the browser backend")
        http.read(canonical_url(args.url), domains=args.allow_domain)
        http.browser.wait_for_login()
        with atomic_file(args.storage_state) as stream:
            json.dump(http.browser.storage_state(), stream)
            stream.write("\n")
        LOG.info("Sensitive session saved with owner-only permissions: %s", args.storage_state.resolve())
        return 0
    discovery_errors: list[str] = []
    if args.command == "download":
        resources = read_manifest(args.manifest)
    else:
        if not args.query and not args.url:
            parser.error("provide search keywords or at least one --url")
        formats = set(args.formats.lower().split(","))
        if not formats or not formats <= FORMATS:
            parser.error("supported formats: " + ",".join(sorted(FORMATS)))
        if args.manifest.exists() and not args.overwrite_manifest:
            raise CrawlError("Manifest exists; use download to resume, or --overwrite-manifest")
        seeds = [(canonical_url(url), "") for url in args.url]
        if args.query:
            query = args.query
            if args.allow_domain:
                query += " (" + " OR ".join("site:" + domain for domain in args.allow_domain) + ")"
            try:
                seeds.extend(search_web(http, query, args.limit))
            except CrawlError as exc:
                if not seeds:
                    raise
                discovery_errors.append(str(exc))
                LOG.error("Search failed; continuing with explicit URLs: %s", exc)
        resources, page_errors = discover(
            http,
            seeds,
            domains=args.allow_domain,
            formats=formats,
            max_pages=args.max_pages,
            depth=args.depth,
            limit=args.limit,
        )
        discovery_errors.extend(page_errors)
        write_manifest(args.manifest, resources)
        with atomic_file(args.manifest.with_suffix(".discovery.json")) as stream:
            json.dump(
                {
                    "query": args.query,
                    "seeds": [url for url, _ in seeds],
                    "errors": discovery_errors,
                    "resources": len(resources),
                    "searched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )
        LOG.info("Found %s documents; manifest: %s", len(resources), args.manifest.resolve())
    if not resources:
        LOG.error("No documents found; try an official resource page with --url")
        return 1
    failures = 0
    if args.command != "search":
        failures = download_all(
            http,
            resources,
            args.manifest,
            args.outdir,
            domains=args.allow_domain,
            max_bytes=int(args.max_mb * 1024 * 1024),
            force=args.force,
        )
        LOG.info(
            "Downloaded: %s; verified existing: %s; failed: %s",
            sum(r.status == "downloaded" for r in resources),
            sum(r.status == "skipped" for r in resources),
            failures,
        )
    return 1 if failures or discovery_errors else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "login" and args.storage_state is None:
        parser.error("login requires --storage-state PATH")
    if not args.browser and (
        args.headed
        or args.storage_state
        or args.screenshot_dir
        or args.wait_selector
        or args.wait_ms != 500
        or getattr(args, "click_selector", [])
        or getattr(args, "click_format", "pdf") != "pdf"
    ):
        parser.error("browser-specific options require --browser")
    if args.storage_state and not args.storage_state.is_file() and args.command != "login":
        parser.error("--storage-state must refer to an existing file")
    try:
        with ExitStack() as stack:
            if args.browser:
                http = BrowserHTTP(args)
                stack.enter_context(http.browser)
            else:
                http = HTTP(args.delay, args.timeout)
            return execute(args, http, parser)
    except (CrawlError, BrowserError, ValueError, TypeError, EOFError, *NETWORK_ERRORS) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        if args.command == "login":
            LOG.error("Interrupted; session capture was not completed")
        else:
            LOG.error("Interrupted; rerun download with the same manifest to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
