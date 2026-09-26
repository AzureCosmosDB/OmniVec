"""Opt-in desktop browser checks against an authenticated localhost AKS tunnel."""
import json
import importlib.util
import os
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest


BASE = os.getenv("OMNIVEC_E2E_BASE_URL", "")
SECTIONS = (
    "dashboard", "health", "agent", "sources", "destinations", "pipelines",
    "playground", "operations", "metrics", "dg-models", "dg-pipelines",
    "dg-health", "dg-deployments", "deployment",
)
pytestmark = pytest.mark.skipif(not BASE, reason="Set an explicitly authorized localhost E2E URL")


@pytest.fixture(scope="module")
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    parsed = urlparse(BASE)
    assert parsed.hostname in {"localhost", "127.0.0.1"} and parsed.scheme in {"http", "https"}
    token = os.environ["OMNIVEC_E2E_TOKEN"]
    assert token
    errors = []
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})

        def guard(route):
            request = route.request
            target = urlparse(request.url)
            read_posts = {"/api/auth/login", "/api/agent/diagnostics/pipeline", "/api/agent/chat"}
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if target.netloc != parsed.netloc or request.method != "POST" or target.path not in read_posts:
                    route.abort("blockedbyclient")
                    return
            preview = os.getenv("OMNIVEC_E2E_PREVIEW_ROOT")
            assets = {"/ui": ("classic.html", "text/html"), "/static/operations.css": ("operations.css", "text/css"),
                      "/static/operations.js": ("operations.js", "application/javascript")}
            if preview and target.netloc == parsed.netloc and target.path in assets:
                file, content_type = assets[target.path]
                route.fulfill(path=str(Path(preview) / file), content_type=content_type)
                return
            route.continue_()

        context.route("**/*", guard)
        current = context.new_page()
        current.set_default_timeout(12000)
        current.set_default_navigation_timeout(60000)
        current.on("pageerror", lambda error: errors.append(str(error).replace(token, "[REDACTED]")))
        try:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            context.add_init_script(
                "if (location.origin === " + json.dumps(origin) +
                " && !localStorage.getItem('omnivec_token')) localStorage.setItem('omnivec_token', " +
                json.dumps(token) + ");"
            )
            current.goto(BASE + "/ui", wait_until="domcontentloaded")
            playwright.expect(current.locator("#main-app")).to_be_visible(timeout=45000)
            playwright.expect(current.locator("#flow-pipelines")).to_have_text(re.compile(r"^\d+$"), timeout=60000)
        except Exception as error:
            browser.close()
            raise RuntimeError(str(error).replace(token, "[REDACTED]")) from None
        yield current
        browser.close()
    assert not errors, errors


def capture(page, name):
    output = os.getenv("OMNIVEC_E2E_SCREENSHOTS")
    if not output:
        return
    assert not page.evaluate("(token) => document.body.innerText.includes(token)", os.environ["OMNIVEC_E2E_TOKEN"])
    path = Path(output)
    path.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path / (name + ".png")), animations="disabled",
                    mask=[page.locator("input[type=password]:visible")])


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("section", SECTIONS)
def test_desktop_sections_in_both_themes(page, section, theme):
    is_dark = page.locator("html").get_attribute("data-theme") == "dark"
    if is_dark != (theme == "dark"):
        page.locator("#theme-toggle-btn").click()
    item = page.locator(f'.nav-item[onclick="showSection(\'{section}\')"]')
    item.click()
    assert item.get_attribute("aria-current") == "page"
    assert page.locator(f"#section-{section}").is_visible()
    assert page.locator(".section:visible").count() == 1
    page.wait_for_timeout(650)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    capture(page, theme + "-" + section)


def test_programmatic_navigation_and_keyboard_navigation(page):
    page.evaluate("showSection('pipelines')")
    assert page.locator('.nav-item[onclick="showSection(\'pipelines\')"]').get_attribute("aria-current") == "page"
    item = page.locator('.nav-item[onclick="showSection(\'dashboard\')"]')
    item.focus()
    item.press("Enter")
    assert page.locator("#section-dashboard").is_visible()


def test_chat_models_exclude_embeddings_and_prompts_do_not_send(page):
    page.evaluate("showSection('agent')")
    page.evaluate("agentInit()")
    with page.expect_response(lambda response: urlparse(response.url).path == "/api/models") as reply:
        page.evaluate("agentRefreshModels()")
    assert reply.value.status == 200
    models = reply.value.json()["models"]
    expected_ids = [
        model["id"] for model in models
        if model.get("model_category") == "chat"
        and model.get("enabled") is not False
        and model.get("type") in {"azure-openai", "openai", "openai-compatible"}
    ]
    values = page.locator("#agent-model option").evaluate_all(
        "(options) => options.map(option => option.value)"
    )
    assert values == [""] + expected_ids
    labels = page.locator("#agent-model option").all_text_contents()
    assert all("text-embedding" not in label.lower() for label in labels)
    before = page.locator("#agent-transcript").inner_text()
    page.get_by_role("button", name="Inspect queues", exact=True).click()
    assert "queue backlog" in page.locator("#agent-input").input_value()
    assert page.locator("#agent-transcript").inner_text() == before


def test_metrics_show_runtime_and_historical_controls(page):
    page.evaluate("showSection('metrics')")
    page.locator("#metrics-custom-start-date").wait_for(state="visible")
    assert page.locator("#runtime-metrics .pulse-cell").count() == 8
    assert "Failed to load" not in page.locator("#metrics-section-content").inner_text()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_approval_card_is_readable_and_renders_metadata_as_text(page, theme):
    page.evaluate("showSection('agent')")
    page.evaluate("agentInit()")
    if (page.locator("html").get_attribute("data-theme") == "dark") != (theme == "dark"):
        page.locator("#theme-toggle-btn").click()
    page.evaluate("""() => _agentRenderApproval('ui-only-approval', 'resume_pipeline',
        {pipeline_id: 'ui-only-scope'}, 'low', '<img src=x onerror="window.unexpectedMarkup=true">')""")
    card = page.locator('[data-call-id="ui-only-approval"]')
    try:
        assert card.locator("img").count() == 0
        assert '<img src=x' in card.inner_text()
        assert card.locator("button").all_text_contents() == ["Deny", "Approve and run once"]
        assert "execution alone is not success" in card.inner_text()
        capture(page, theme + "-approval-card-ui-only")
    finally:
        card.evaluate("(node) => node.remove()")


@pytest.mark.parametrize("section,action,modal", [
    ("sources", "showAddSourceModal()", "source-modal"),
    ("destinations", "showAddDestinationModal()", "destination-modal"),
    ("pipelines", "showAddPipelineModal()", "pipeline-modal"),
])
def test_existing_configuration_dialogs_remain_usable(page, section, action, modal):
    page.evaluate("(section) => showSection(section)", section)
    page.locator(f'#section-{section} [onclick="{action}"]').first.click()
    page.locator("#" + modal).wait_for(state="visible")
    try:
        assert page.locator("#" + modal + " input, #" + modal + " select").count() > 0
        capture(page, "dialog-" + section)
    finally:
        page.locator("#" + modal + " .modal-close").click()
        page.locator("#" + modal).wait_for(state="hidden")


def test_diagnostic_failure_is_visible_and_retry_is_available(page):
    page.evaluate("showSection('health')")
    page.select_option("#diagnostic-scope", "")
    pattern = "**/api/agent/diagnostics/system"
    page.route(pattern, lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":"unavailable"}'))
    try:
        page.locator("#diagnostic-run").click()
        page.locator("#diagnostic-run:not([disabled])").wait_for(state="visible")
        assert "HTTP 503" in page.locator("#diagnostic-results").inner_text()
        assert not page.locator("#diagnostic-scope").is_disabled()
        capture(page, "diagnostic-error-state")
    finally:
        page.unroute(pattern)


def test_live_scoped_diagnostics(page):
    if os.getenv("OMNIVEC_E2E_PREVIEW_ROOT"):
        pytest.skip("Preview tests static UI only; real diagnostic proxy is tested after deployment")
    page.evaluate("showSection('health')")
    options = page.locator("#diagnostic-scope option").evaluate_all("(options) => options.map(option => option.value).filter(Boolean)")
    assert options, "A registered synthetic pipeline is required for scoped live diagnostics"
    page.select_option("#diagnostic-scope", options[0])
    with page.expect_response(lambda response: urlparse(response.url).path == "/api/agent/diagnostics/pipeline",
                              timeout=65000) as reply:
        page.locator("#diagnostic-run").click()
    page.locator("#diagnostic-run:not([disabled])").wait_for(state="visible", timeout=65000)
    assert reply.value.status == 200
    text = page.locator("#diagnostic-results").inner_text()
    assert "Scope: " + options[0] in text
    assert "Prioritized next steps" in text
    assert page.locator("#diagnostic-results button").all_text_contents() == ["Investigate with agent"]
    capture(page, "live-scoped-diagnostics")


def test_live_agent_chat_uses_real_diagnostic_tool(page):
    if not os.getenv("OMNIVEC_E2E_CHAT") or os.getenv("OMNIVEC_E2E_PREVIEW_ROOT"):
        pytest.skip("Real chat inference requires an explicitly enabled, approved chat deployment")
    page.evaluate("showSection('agent')")
    page.evaluate("agentInit()")
    page.evaluate("""() => {
        const original = _agentConsumeStream;
        const capture = {finished: false, text: ''};
        const transcript = document.getElementById('agent-transcript');
        transcript.removeAttribute('data-e2e-stream-finished');
        window.operationsStreamCapture = capture;
        window.operationsOriginalStream = original;
        _agentConsumeStream = async (response, bubble, version) => {
            const decoder = new TextDecoder();
            const stream = response.body.pipeThrough(new TransformStream({
                transform(chunk, controller) {
                    capture.text += decoder.decode(chunk, {stream: true});
                    controller.enqueue(chunk);
                }
            }));
            try {
                await original(new Response(stream, {
                    status: response.status, headers: response.headers
                }), bubble, version);
            } finally {
                capture.text += decoder.decode();
                capture.finished = true;
                transcript.setAttribute('data-e2e-stream-finished', 'true');
            }
        };
    }""")
    page.locator("#agent-input").fill("Use diagnose_system for a read-only diagnostic summary. Do not request or execute mutations. Distinguish missing evidence from failures.")
    try:
        with page.expect_response(lambda response: urlparse(response.url).path == "/api/agent/chat", timeout=180000) as reply:
            page.get_by_role("button", name="Send", exact=True).click()
        assert reply.value.status == 200
        page.locator('#agent-transcript[data-e2e-stream-finished="true"]').wait_for(
            state="visible", timeout=180000)
        stream_text = page.evaluate("window.operationsStreamCapture.text")
    finally:
        page.evaluate("""() => {
            _agentConsumeStream = window.operationsOriginalStream;
            delete window.operationsOriginalStream;
            delete window.operationsStreamCapture;
            document.getElementById('agent-transcript').removeAttribute('data-e2e-stream-finished');
        }""")
    spec = importlib.util.spec_from_file_location("operations_sse", Path(__file__).resolve().parents[2] / "scripts" / "agent_recovery_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    events = probe.parse_sse(stream_text.splitlines())
    assert not any(event["type"] in {"error", "approval_required"} for event in events)
    assert any(event["type"] == "done" for event in events)
    assert any(event["type"] == "tool_result" and event.get("name") == "diagnose_system"
               and event.get("result", {}).get("status") in {"HEALTHY", "READY_IDLE", "UNKNOWN", "UNHEALTHY", "BLOCKED"}
               for event in events)
    final = next(event for event in events if event["type"] == "final")
    assert final.get("text")
    page.locator("#agent-transcript").get_by_text(final["text"], exact=True).wait_for(state="visible")
    text = page.locator("#agent-transcript").inner_text()
    assert "no chat model configured" not in text and "requires admin" not in text
    assert "[error]" not in text and "[network error]" not in text
    capture(page, "live-agent-diagnosis")
