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
    page.goto(f"{static_server_url}/index.html", wait_until="networkidle")
    yield page, requests
    assert page_errors == []
    page.close()
