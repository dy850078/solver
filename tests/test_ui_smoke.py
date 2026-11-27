"""Browser smoke test for the web UI — a real Chromium against a real server.

What Node cannot check (tests/test_ui_js.py covers the DOM-free modules):
that every page actually boots — module graph resolves, no exception on
load — that the page talks to the API, and that the .xlsx reader works on
the browser-native DecompressionStream / DOMParser it was written for.
It also enforces the "no CDN, no third-party script" rule mechanically:
any request leaving the test server fails the test.

Needs the optional `ui` extra (Playwright for Python) plus a Chromium:

    make install-ui          # pip-installs playwright into .venv
    make test-ui             # runs only this file

Browser lookup: $SOLVER_CHROMIUM (path to any chrome/chromium binary — the
closed-network route: use the distro package, nothing is downloaded), else
whatever Playwright has installed (`playwright install chromium`, needs
its CDN). Missing library or browser → SKIP, so `make test` stays green
on machines without one; set SOLVER_REQUIRE_BROWSER=1 (CI does) to turn
the skip into a failure.
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = ["index.html", "demand.html", "rollout.html", "report.html", "compare.html"]
REQUIRED = bool(os.environ.get("SOLVER_REQUIRE_BROWSER"))


def _unavailable(reason: str):
    if REQUIRED:
        pytest.fail(f"browser smoke test required (SOLVER_REQUIRE_BROWSER) but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def base_url():
    """The real app (`app.server:api`) with the UI enabled, on a free port.
    A subprocess rather than TestClient: the browser needs a socket."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.server:api",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env={**os.environ, "ENABLE_UI": "enable"},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                if proc.poll() is not None or time.time() > deadline:
                    raise RuntimeError("UI server did not start: "
                                       + proc.stderr.read().decode()[-2000:])
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        _unavailable("playwright is not installed (make install-ui)")
    with sync_playwright() as pw:
        exe = os.environ.get("SOLVER_CHROMIUM")
        try:
            # --disable-dev-shm-usage: containers often ship a tiny /dev/shm
            # and Chromium dies on start without it. --disable-gpu: no GPU
            # on a headless runner.
            b = pw.chromium.launch(
                executable_path=exe or None,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        except Error as exc:
            # The WHOLE message: Playwright appends the browser's own stderr
            # (missing shared library, sandbox refusal, …), which is the
            # only place the real reason appears.
            _unavailable(
                f"no Chromium could be launched (SOLVER_CHROMIUM={exe!r}; set it "
                f"to a chrome/chromium binary).\nTry it by hand:\n"
                f"  {exe or '<chromium>'} --headless --no-sandbox --disable-gpu "
                f"--dump-dom about:blank\nPlaywright said:\n{exc}")
        yield b
        b.close()


@pytest.fixture()
def page(browser, base_url):
    """A fresh page that records everything a user would call "broken":
    console errors, uncaught exceptions, failed or off-site requests."""
    ctx = browser.new_context()
    pg = ctx.new_page()
    problems: list[str] = []
    pg.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
    pg.on("pageerror", lambda e: problems.append(f"exception: {e}"))
    pg.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
    pg.on("request", lambda r: r.url.startswith((base_url, "data:", "blob:"))
          or problems.append(f"off-site request (no CDN allowed): {r.url}"))
    pg.on("response", lambda r: r.status >= 400
          and problems.append(f"HTTP {r.status}: {r.url}"))
    pg.problems = problems
    yield pg
    ctx.close()


@pytest.mark.parametrize("name", PAGES)
def test_page_boots_clean(page, base_url, name):
    page.goto(f"{base_url}/ui/{name}", wait_until="networkidle")
    assert page.title()
    assert page.problems == []


def test_solve_from_the_editor(page, base_url):
    """Paste a worked example, press Run, see the result render — the one
    path every other feature of the main page hangs off."""
    request = (ROOT / "examples" / "success_basic.json").read_text()
    page.goto(f"{base_url}/ui/index.html", wait_until="networkidle")
    page.fill("#json-editor", request)
    page.click("#run-btn")
    page.wait_for_selector("#stats:not(.hidden)", timeout=30_000)
    assert page.inner_text("#rack-container").strip()
    assert page.problems == []


def test_xlsx_round_trip_in_the_browser(page, base_url):
    """Writer → real ZIP bytes → reader, on the browser's own inflate and
    XML parser. Values chosen to hit the awkward cells: XML metacharacters,
    non-ASCII, numbers, booleans-as-text, blanks."""
    rows = [["name", "n", "note"],
            ['<a href="x">&amp;</a>', 3, "機櫃 αβγ"],
            ["", 0.5, "true"],
            ["tail ", -7, ""]]
    page.goto(f"{base_url}/ui/demand.html", wait_until="networkidle")
    got = page.evaluate(
        """async (rows) => {
            const { buildXlsx } = await import("/ui/js/xlsx.js");
            const { readXlsx } = await import("/ui/js/xlsx-read.js");
            const blob = buildXlsx([{ name: "Sheet A", rows, header: true }]);
            const sheets = await readXlsx(await blob.arrayBuffer());
            return sheets.map((s) => ({ name: s.name, rows: s.rows }));
        }""", rows)
    assert [s["name"] for s in got] == ["Sheet A"]
    normal = lambda grid: [[("" if c is None else c) for c in r] for r in grid]
    assert normal(got[0]["rows"]) == rows, json.dumps(got, ensure_ascii=False)
    assert page.problems == []
