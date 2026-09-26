from __future__ import annotations

from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest
from playwright.sync_api import Browser, Page, Playwright, Route, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "web" / "static"


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def translate_path(self, path: str) -> str:
        if path.startswith("/static/"):
            path = path[len("/static"):]
        return super().translate_path(path)


@pytest.fixture(scope="module")
def static_server_url() -> Iterator[str]:
    handler = lambda *args, **kwargs: QuietStaticHandler(  # noqa: E731
        *args, directory=str(STATIC_ROOT), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture(scope="module")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="module")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    browser = playwright_instance.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture
def ui_page(browser: Browser, static_server_url: str) -> Iterator[tuple[Page, list[dict]]]:
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    requests: list[dict] = []
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def handle_api(route: Route) -> None:
        request = route.request
        payload = request.post_data_json if request.post_data else None
        requests.append({"url": request.url, "method": request.method, "payload": payload})
        if request.url.endswith("/api/sources/test-connection"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "message": "Source is accessible"}),
            )
            return
        if request.url.endswith("/api/destinations/test-connection"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "success": True,
                        "message": "Destination is accessible",
                        "vector_indexes": [
                            {
                                "path": "/embedding",
                                "dimensions": 1536,
                                "dataType": "float32",
                                "distanceFunction": "cosine",
                                "indexType": "diskANN",
                            }
                        ],
                    }
                ),
            )
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", handle_api)
    page.route("https://**/*", lambda route: route.abort())
    page.goto(f"{static_server_url}/classic.html", wait_until="networkidle")
    yield page, requests
    assert page_errors == []
    page.close()


class SpaApp:
    """Handle for the redesigned SPA served from web/static/index.html with a mocked API."""

    def __init__(self, page: Page, base_url: str, responses: dict, requests: list[dict], errors: list[str]):
        self.page, self.base_url, self.responses, self.requests, self.errors = page, base_url, responses, requests, errors

    def goto(self, hash_route: str = "#/home") -> Page:
        self.page.evaluate("h => { location.hash = h; }", hash_route)
        self.page.wait_for_selector("#view .page", state="attached")
        return self.page

    def called(self, path: str, method: str = "GET") -> list[dict]:
        return [r for r in self.requests if r["path"] == path and r["method"] == method]


@pytest.fixture
def spa_app(browser: Browser, static_server_url: str):
    from urllib.parse import urlparse

    from spa_mock import default_responses

    opened: list[Page] = []
    apps: list[SpaApp] = []

    def open_app(route: str = "#/home", overrides: dict | None = None, token: str | None = "test-token",
                 viewport: dict | None = None) -> SpaApp:
        responses = default_responses()
        responses.update(overrides or {})
        page = browser.new_page(viewport=viewport or {"width": 1440, "height": 1000})
        opened.append(page)
        requests: list[dict] = []
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle_api(api_route: Route) -> None:
            request = api_route.request
            path = urlparse(request.url).path
            payload = request.post_data_json if request.post_data else None
            requests.append({"path": path, "url": request.url, "method": request.method, "payload": payload})
            body = responses.get(f"{request.method} {path}", responses.get(path, {}))
            status = 200
            if isinstance(body, tuple):
                status, body = body
            api_route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        page.route("**/api/**", handle_api)
        page.route("https://**/*", lambda r: r.abort())
        if token is not None:
            page.add_init_script(f"localStorage.setItem('omnivec_token', {json.dumps(token)});"
                                 "localStorage.setItem('ov-theme', 'light');")
        page.goto(f"{static_server_url}/index.html{route}", wait_until="domcontentloaded")
        page.wait_for_selector("#view .page, .signin, form", state="attached")
        app = SpaApp(page, static_server_url, responses, requests, errors)
        apps.append(app)
        return app

    yield open_app
    try:
        for app in apps:
            assert app.errors == [], f"JavaScript errors: {app.errors}"
    finally:
        for page in opened:
            page.close()
