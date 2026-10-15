# Junkman

A small Python CLI for discovering and downloading publicly accessible documents.
Inspired by the workflow in `../pubmed-crawler`: discovery produces a CSV manifest;
downloads are independently resumable and have per-file provenance.

**Use only resources you are authorized to download.** Public accessibility does
not establish an open license. Junkman does not bypass logins, paywalls, CAPTCHA,
DRM, or robots.txt restrictions. The Power Up example downloads a publisher-hosted
excerpt, not a complete commercial textbook.

## Quick start

Python 3.9+ is required. The default HTTP mode uses only the standard library;
installation and API keys are not required. Optional Playwright mode is described
below.

```bash
python3 junkman.py --help
python3 junkman.py search "Cambridge Power Up sample PDF" \
  --allow-domain cambridge.org \
  --manifest downloads/power-up/results.csv

# Inspect the manifest before downloading.
python3 junkman.py download \
  --manifest downloads/power-up/results.csv \
  --allow-domain cambridge.org \
  --outdir downloads/power-up/files
```

`run` combines discovery and download:

```bash
python3 junkman.py run "Cambridge Power Up sample PDF" \
  --allow-domain cambridge.org \
  --formats pdf \
  --manifest downloads/power-up/results.csv \
  --outdir downloads/power-up/files
```

Queries are search keywords, not natural-language instructions interpreted by an
LLM. For example, use `Cambridge Power Up sample PDF` rather than
`please find and download books for me`. Chinese keywords are also accepted.

### Search availability

Keyword discovery tries Bing RSS, then DuckDuckGo HTML, without a key. This is
**best-effort**, not a guaranteed search API. Search terms are sent to those
public services. Provider results, ordering, and availability can change.
The CLI obeys the providers' robots.txt rules, so some providers or regions may
be unusable. Empty results, bot challenges, denied robots access, and network
failures are reported, not silently interpreted as successful searches.

During the 2026-10-15 smoke test, Bing search was denied by robots.txt and the
DuckDuckGo connection was reset. Keyword-to-download could therefore not be
verified on that network. Official direct-URL download and verified resume both
succeeded. For reliable unattended keyword discovery, a supported search API
would be a future integration; none is bundled or required in this version.

### Verified Power Up example

The following Cambridge-hosted excerpt URL was successfully downloaded and
checked for a PDF signature on 2026-10-15. macOS PDFKit also reported 10 pages;
text from its first three pages contains both "Power Up" and "Cambridge":

```bash
python3 junkman.py run \
  --url https://assets.cambridge.org/97811084/13749/excerpt/9781108413749_excerpt.pdf \
  --allow-domain cambridge.org \
  --manifest downloads/power-up-official/results.csv \
  --outdir downloads/power-up-official/files
```

To resume, retry failures, or verify existing files without searching again:

```bash
python3 junkman.py download \
  --manifest downloads/power-up-official/results.csv \
  --allow-domain cambridge.org \
  --outdir downloads/power-up-official/files
```

Availability may change. The numeric identifier is the publisher's asset
identifier; this project does not infer the edition or completeness from a URL.

### Start from official resource pages

Use one or more `--url` options to avoid depending on a search provider:

```bash
python3 junkman.py search \
  --url https://www.example.org/teaching-resources/ \
  --allow-domain example.org \
  --depth 1 --max-pages 10 --limit 20 \
  --manifest downloads/example/results.csv
```

Replace the placeholder with a real resource page. Absolute and relative
document links are supported. HTML traversal stays on the current page's host;
linked documents may be on a CDN. `--allow-domain` restricts both discovery and
downloads, including redirects. Repeat it to allow multiple domains. A bare
domain includes its subdomains, but never lookalike suffixes. No allowlist means
any public HTTPS host is eligible; use an allowlist for publisher-only searches.

If both a query and explicit URLs are supplied, discovery still uses the URLs
when keyword search fails. The run returns a nonzero status and records that
partial failure.

## Optional Playwright browser mode

Use Chromium for JavaScript-rendered links, explicitly selected download buttons,
debug screenshots, and reuse of your own authorized login session. The default
HTTP workflow remains unchanged; Playwright is only imported when requested.
Firefox, WebKit, stealth plugins, and CAPTCHA-solving services are not included.

Install the optional dependency and browser in this project's virtual environment:

```bash
uv sync --extra browser --extra dev
export PLAYWRIGHT_BROWSERS_PATH=0
uv run playwright install chromium
```

Keep `PLAYWRIGHT_BROWSERS_PATH=0` set when running the following commands. This
stores Chromium inside the installed Playwright package in `.venv`, rather than
installing it into a user-wide browser cache. Alternatively, omit the variable
for both installation and execution to use Playwright's standard cache location.
The package supports `pip install '.[browser]'` inside a virtual environment too.

### Render dynamic resource pages

```bash
uv run junkman search --browser \
  --url https://www.example.org/teaching-resources/ \
  --allow-domain example.org \
  --wait-selector 'a[href$=".pdf"]' \
  --wait-ms 500 \
  --screenshot-dir screenshots/example \
  --manifest downloads/browser/results.csv
```

Replace the placeholder URL and selector with the real site. Navigation waits
for DOMContentLoaded, then the optional `--wait-selector`, then `--wait-ms`
(default 500 milliseconds). Rendered anchor URLs are resolved using the page's
actual document base, including `<base href>`. A missing wait selector is a
reported timeout, not an empty successful search. `--headed` shows Chromium for
inspection; it does not disable access checks.

Keyword searches also use the browser when `--browser` is supplied:

```bash
uv run junkman search "Cambridge Power Up sample PDF" \
  --browser --headed --allow-domain cambridge.org \
  --manifest downloads/browser-search/results.csv
```

The browser tries Bing and DuckDuckGo result pages and extracts result links,
rather than parsing Bing RSS. Search-provider navigation is not restricted to
the publisher allowlist; subsequently discovered pages/files are restricted.
Browser mode still obeys robots.txt, and is not a promise that blocked search
providers will become accessible. Site layout changes can break result selectors.
The live browser smoke test on 2026-10-15 still encountered Bing robots denial
and a timeout fetching DuckDuckGo robots.txt; an explicit official document URL
remained discoverable and the partial search failure was persisted and returned
as a nonzero exit status.
Avoid resource-page-specific `--wait-selector` values on keyword search commands:
the same wait selector applies to provider pages as well.

### Download a file produced by a button

```bash
uv run junkman run --browser \
  --url https://www.example.org/teaching-resources/ \
  --allow-domain example.org \
  --click-selector '#download-sample' \
  --click-format pdf \
  --manifest downloads/buttons/results.csv \
  --outdir downloads/buttons/files
```

Only selectors you explicitly provide are considered; there is no "click every
button" heuristic. Each selector must match exactly one element on a matching
page. A selector absent from a page is ignored during discovery; multiple matches
are reported as an error. Repeat `--click-selector` for different download
buttons. Selectors must identify download-only actions: do not select purchase,
delete, submit, or other state-changing controls.

`search --browser --click-selector ...` records actions without clicking them;
`run` and `download --browser` perform the selected actions. The browser revisits
the source page before each action and expects a Playwright download event.
HTTPS attachments (including extensionless endpoints) and same-origin `blob:`
downloads are supported. A button that only opens an embedded viewer or new tab,
or requires further interactions, fails explicitly. `--click-format` specifies
the expected format, rather than trusting the suggested filename.

Button files are staged by Chromium, checked against `--max-mb` and the shared
format validator, then atomically published with SHA-256 provenance. Chromium
may finish the entire transfer before checking the size: **`--max-mb` bounds
published files, not browser temporary disk usage or network traffic**. Use
trusted resource pages and monitor free disk space. This difference is limited
to explicit button actions; ordinary linked files retain streaming HTTP limits.

### Capture and reuse your own session

```bash
uv run junkman login \
  --url https://www.example.org/ \
  --storage-state .auth/publisher.storage-state.json
```

This opens headed Chromium. Sign in yourself, then press Enter in the terminal.
No credentials are requested by the CLI and no login form is filled automatically.
The saved state file is atomically written with owner-only permissions (`0600`).
An existing state file can be loaded and refreshed by this same command.

```bash
uv run junkman run --browser \
  --url https://www.example.org/teaching-resources/ \
  --storage-state .auth/publisher.storage-state.json \
  --allow-domain example.org \
  --manifest downloads/authorized/results.csv \
  --outdir downloads/authorized/files
```

The session contains cookies and localStorage and is a **credential-equivalent
secret**. Never commit or share it. `.auth/`, `*.storage-state.json`, and the
default `screenshots/` directory are ignored; custom paths are your responsibility.
Screenshots may also contain private account information and are saved with
owner-only file permissions. Sessions expiring or lacking authorization remain
errors; there is no authentication bypass.

Browser cookies are copied into the HTTP download cookie jar, respecting
host-only scope. Partitioned cookies are not downgraded into unpartitioned HTTP
cookies; they produce a warning and require browser button downloads instead.
localStorage remains in Chromium; it is not converted into
custom HTTP Authorization headers. For downloads requiring JavaScript-managed
tokens, use the site's authorized download button instead. SessionStorage,
IndexedDB-only authentication, existing personal Chrome profiles, and automatic
session refresh are not supported. Changes to an imported session are only
persisted by an explicit `login` capture.

All routed browser requests, including scripts, frames, and redirects, are
checked for public HTTPS destinations, domain scope, and robots permissions.
An allowlist may need extra CDN or identity-provider domains. Denied subresources
are logged; if the desired content then fails to render, use the required wait
selector to make that failure explicit. Service workers and WebSockets are
disabled to avoid bypassing request routing. This is not an isolated network
sandbox; it should not be exposed as a service for untrusted URLs.

## Commands and limits

| Option | Commands | Default | Meaning |
|---|---|---|---|
| `--manifest PATH` | All | `results.csv` | Discovery input/output and download status |
| `--allow-domain DOMAIN` | All | Unrestricted public hosts | Repeatable domain allowlist |
| `--delay SECONDS` | All | `1` | Minimum spacing between requests; robots delays may increase it |
| `--timeout SECONDS` | All | `30` | Per-network-operation timeout, not a whole-run deadline |
| `--url HTTPS_URL` | `search`, `run` | None | Repeatable landing page or direct document |
| `--limit N` | `search`, `run` | `20` | Maximum discovered documents; also caps search seeds |
| `--max-pages N` | `search`, `run` | `20` | Discovery-page budget; button replays are separate |
| `--depth N` | `search`, `run` | `1` | HTML link depth beyond each seed; zero checks only seed pages |
| `--formats LIST` | `search`, `run` | All supported formats | Comma-separated extensions |
| `--overwrite-manifest` | `search`, `run` | Off | Explicitly replace an existing manifest and discovery report |
| `--outdir PATH` | `download`, `run` | `downloads` | Download destination |
| `--max-mb N` | `download`, `run` | `50` | Per-document limit in MiB |
| `--force` | `download`, `run` | Off | Redownload even verified completed files |
| `--browser` | All | Off, except `login` | Enable optional Playwright backend |
| `--headed` | All | Off, except `login` | Display Chromium |
| `--storage-state PATH` | All | None | Load session; `login` saves it after manual confirmation |
| `--screenshot-dir PATH` | All | None | Save private PNG debugging screenshots |
| `--wait-selector SELECTOR` | All | None | Wait for a rendered element |
| `--wait-ms N` | All | `500` | Additional browser rendering wait |
| `--click-selector SELECTOR` | `search`, `run` | None | Repeatable, explicit download-button selector |
| `--click-format FORMAT` | `search`, `run` | `pdf` | Expected format for button actions |

Supported formats: PDF, DOCX, PPTX, XLSX, EPUB, ZIP, UTF-8 TXT.
Discovery identifies formats by the URL path extension, ignoring query strings
and fragments. Extensionless endpoints need an explicit browser button action;
JavaScript-rendered links require `--browser`. Legacy DOC/PPT/XLS files, embedded
viewers, audio, and video are not supported.
HTTP-only URLs and nonstandard HTTPS ports are deliberately rejected.

Discovery records candidates, not verified downloads. Only `download`/`run`
checks robots permissions and file content at each document URL.
Search is not a semantic relevance classifier: inspect candidate titles and
source pages, especially on broad landing pages.

## Outputs and integrity

```text
downloads/power-up-official/
  results.csv
  results.discovery.json
  files/
    <url-id>.pdf
    <url-id>.json
```

CSV columns:

| Column | Meaning |
|---|---|
| `id` | First 20 hex characters of SHA-256 of the canonical URL; actions also include the selector |
| `title` | Link/search title, falling back to the URL |
| `url` | Canonical document URL, or source page URL for button actions |
| `source_url` | Page that linked the document, or the supplied direct URL |
| `format` | Expected file format |
| `status` | `discovered`, `downloaded`, `skipped`, or `error` |
| `path` | Absolute local path after success |
| `sha256` | Hash of the downloaded bytes |
| `error` | Last download failure, otherwise empty |
| `selector` | Download-button selector; empty for ordinary file links |

Legacy nine-column manifests without `selector` remain readable and preserve
their URL-derived IDs. Writing a manifest adds the new optional-action column.
Button action IDs include the selector so different buttons on a page remain
distinct. Replaying button records requires `download --browser`; the selector
and format are read from the manifest. Cookies are never stored in manifests.

The discovery report stores the query, seeds, page/search errors, resource count,
and timestamp when discovery reaches the manifest-writing stage. A search that
fails before producing seeds reports the failure to stderr without creating a
new manifest. Each downloaded file has a JSON completion record containing the
original/final URLs, source page, title, format, size, MIME type, hash, timestamp,
and an explicit note that reuse licensing has not been verified.
Button provenance also stores the selector, suggested filename, actual download
URL (possibly `blob:`), and `transfer: playwright`. Ordinary linked-file transfers
remain HTTP even when browser mode is used for discovery.

Downloads stream to unique `.part` files. Size, advertised Content-Length, and
format are checked before atomic replacement. PDF checks the `%PDF-` signature;
ZIP-based formats check the container and expected entries; TXT rejects binary
and HTML content. These checks detect common wrong-file/login-page responses,
but are **not** a full document parser, archive CRC audit, or malware scan.
Archives are never extracted.

Resume skips a file only when its completion record matches the original URL,
its SHA-256 matches, and its format and current size limit pass. Missing or
corrupt completion records/files trigger a redownload with a warning.
This is **file-level resume**, not HTTP Range continuation of partial bytes.
An interrupted or failed forced download preserves the previous completed file.
The manifest is updated atomically after every processed row.

Exit codes: `0` means all requested work succeeded, `1` means no results or a
partial/total operational failure, `2` means invalid CLI arguments, and `130`
means interruption. Download failures do not prevent attempts on later rows;
rerunning `download` retries failed rows and verifies completed ones.
Do not run multiple writers against the same manifest/output directory.

## Network behavior

- HTTPS certificate verification stays enabled. User agent: `Junkman/0.1`.
- Sequential requests, bounded redirects, and up to two retries on connection
  failures, HTTP 429, and selected transient 5xx responses.
- No retry/bypass of HTTP 401/403, login, CAPTCHA, or DRM.
- robots.txt is cached per origin. A 404/410 permits access; 401/403 denies it.
  Other failures and HTML challenge responses fail closed. Standard integer
  crawl delays and request rates can increase the minimum request spacing.
- Every redirect is checked for URL scheme, public DNS addresses, domain scope,
  and robots permissions. Proxy environment variables are intentionally ignored.
- Private, loopback, link-local, and other non-global DNS destinations are
  rejected. This is a local CLI, **not an SSRF-isolated service**: DNS validation
  does not pin the subsequent socket address. Do not expose it as a server to
  untrusted callers.
- HTML is limited to 2 MiB and robots.txt to 512 KiB per response. A timeout
  applies to individual network operations; a slowly streaming host can keep a
  run active longer. Use Ctrl-C to interrupt and resume later.

## Development

No runtime dependency installation is necessary for tests:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile junkman.py
```

Optional packaging and development checks with `uv`:

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -q
uv build
uv run junkman --help
```

Additional real-Chromium integration tests:

```bash
uv sync --extra browser --extra dev
export PLAYWRIGHT_BROWSERS_PATH=0
uv run playwright install chromium
uv run python -m unittest discover -s tests -p browser_e2e.py -v
```

These tests render real JavaScript in Chromium against intercepted HTTPS
fixtures, leaving the production route guard in the request chain. They cover
dynamic/base-relative links, HTTPS and blob button downloads, screenshots,
size rejection, non-download timeouts, cookie/localStorage reuse, rendered
search extraction, and the full CLI flow. They require a working Chromium
installation and do not skip missing-browser failures. Human login and live
search-provider availability are not established by these fixture tests.

The package also supports standard `python3 -m pip install .` in a virtual
environment. Its installed console command is `junkman`.

Tests use deterministic mock responses, covering provider fallback, robots and
redirect enforcement, page/depth/document limits, domain boundaries, signature
and size failures, atomic writes, provenance, integrity-based resume, manifest
validation, and the CLI discovery/download/retry flow. Live search is deliberately
not part of the offline test suite.
