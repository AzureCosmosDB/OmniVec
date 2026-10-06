"""Browser tests for the redesigned OmniVec console (web/static/index.html + web/static/app/*.js).

The API is fully mocked (see spa_mock.py); the ``spa_app`` fixture fails a test on any uncaught JS error.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from spa_mock import CHAT_MODEL, EMBED_MODEL, PIPELINE_COUNT


def visible_rows(page, scope: str = "#view"):
    return page.locator(f"{scope} table.tbl tbody tr:not([hidden]):not(.detail-row)")


def test_signin_screen_accepts_token_and_opens_home(spa_app):
    app = spa_app("#/home", token=None)
    page = app.page
    expect(page.locator("#signin h1")).to_have_text("Sign in to OmniVec")

    page.fill("#tok", "ovk_test")
    page.click("#sbtn")

    expect(page.locator("#view h1")).to_be_visible()
    logins = app.called("/api/auth/login", "POST")
    assert logins and logins[-1]["payload"] == {"token": "ovk_test"}
    assert page.evaluate("localStorage.getItem('omnivec_token')") == "ovk_test"
    assert "#/home" in page.url


def test_rejected_token_shows_inline_error(spa_app):
    app = spa_app("#/home", token=None, overrides={"/api/auth/login": (401, {"detail": "bad token"})})
    page = app.page
    page.fill("#tok", "nope")
    page.click("#sbtn")
    expect(page.locator("#serr")).to_have_text("This token was not accepted. Check it and try again.")
    assert page.evaluate("localStorage.getItem('omnivec_token')") is None


def test_home_summarises_fleet_health(spa_app):
    page = spa_app("#/home").page
    view = page.locator("#view")
    expect(view).to_contain_text("All pipelines are healthy.")
    expect(view).to_contain_text(f"{PIPELINE_COUNT} healthy")
    expect(view).to_contain_text("2 sources")
    expect(view).to_contain_text(f"{PIPELINE_COUNT} pipelines")


def test_home_flags_failing_pipelines(spa_app):
    from spa_mock import default_responses

    pipelines = default_responses()["/api/pipelines"]["pipelines"]
    pipelines[0]["stats"]["jobs"] = {"failed": 4}
    page = spa_app("#/home", overrides={"/api/pipelines": {"pipelines": pipelines}}).page
    expect(page.locator("#view")).to_contain_text("1 failing")

    page.evaluate("location.hash = '#/issues'")
    expect(page.locator("#view")).to_contain_text("1 pipeline reported failed documents")


def test_pipelines_list_paginates(spa_app):
    page = spa_app("#/pipelines").page
    expect(page.locator("#view h1")).to_have_text("Pipelines")
    pager = page.locator("#view .pager")
    expect(pager).to_contain_text(f"1–10 of {PIPELINE_COUNT}")
    expect(visible_rows(page)).to_have_count(10)

    pager.get_by_label("Next page").click()
    expect(pager).to_contain_text(f"11–20 of {PIPELINE_COUNT}")
    expect(visible_rows(page).first).to_contain_text("Pipeline 011")

    pager.locator("button.pn", has_text="3").click()
    expect(visible_rows(page)).to_have_count(PIPELINE_COUNT - 20)
    expect(pager.get_by_label("Next page")).to_be_disabled()


def test_pipeline_row_opens_detail_with_flow(spa_app):
    page = spa_app("#/pipelines").page
    visible_rows(page).first.click()
    expect(page).to_have_url(re.compile(r"#/pipelines/pip-001$"))
    view = page.locator("#view")
    expect(view.locator("h1")).to_have_text("Pipeline 001")
    expect(view).to_contain_text("Contracts blob")
    expect(view).to_contain_text("Vector store A")

    view.locator("a.fnode", has_text="Embed").click()
    expect(page).to_have_url(re.compile(r"#/models/mdl-embed$"))
    expect(page.locator("#view h1")).to_have_text(EMBED_MODEL["deployment"])


def test_connections_lists_sources_and_stores(spa_app):
    page = spa_app("#/connections").page
    view = page.locator("#view")
    expect(view.locator("h1")).to_have_text("Connections")
    expect(visible_rows(page)).to_have_count(2)
    expect(view).to_contain_text("Contracts blob")
    expect(view).to_contain_text("Orders feed")


def test_add_source_preselects_type_from_query(spa_app):
    page = spa_app("#/connections/new/source?type=azure-blob").page
    expect(page.locator("#view h1")).to_have_text("Add source")
    expect(page.locator("#achecks")).to_contain_text("Type chosen")
    expect(page.locator("#aform [data-k]").first).to_be_visible()
    expect(page.locator("#aguide")).not_to_contain_text("Pick a type")


def test_add_source_ignores_unknown_type(spa_app):
    page = spa_app("#/connections/new/source?type=<img src=x onerror=alert(1)>").page
    expect(page.locator("#view h1")).to_have_text("Add source")
    expect(page.locator("#aguide")).to_contain_text("Pick a type")
    expect(page.locator("#aform [data-k]")).to_have_count(0)


def test_models_tab_rows_open_model_details(spa_app):
    page = spa_app("#/models").page
    view = page.locator("#view")
    expect(view.locator("h1")).to_have_text("Models & recipes")
    view.locator("tr.clickrow", has_text=EMBED_MODEL["deployment"]).click()
    expect(page).to_have_url(re.compile(r"#/models/mdl-embed$"))
    expect(page.locator("#view")).to_contain_text("Embedding model")
    expect(page.locator("#view")).to_contain_text("Used by")

    page.go_back()
    page.locator("#view tr.clickrow", has_text=CHAT_MODEL["deployment"]).click()
    expect(page).to_have_url(re.compile(r"#/models/mdl-chat$"))
    expect(page.locator("#view")).to_contain_text("Chat models power the OmniVec agent")


def test_recipes_tab_rows_open_recipe_details(spa_app):
    page = spa_app("#/models/recipes").page
    visible_rows(page).first.click()
    expect(page).to_have_url(re.compile(r"#/models/recipes/pdf-layout$"))
    expect(page.locator("#view h1")).to_have_text("pdf-layout")
    expect(page.locator("#view")).to_contain_text("Use in a new pipeline")


def test_unknown_recipe_shows_not_found(spa_app):
    page = spa_app("#/models/recipes/missing").page
    expect(page.locator("#view")).to_contain_text("This recipe was not found")


def test_search_uses_selected_store(spa_app):
    app = spa_app("#/search?store=dst-vec", overrides={"POST /api/playground/search": {
        "results": [{"id": "doc-1", "source_ref": "contract-1.pdf", "score": 0.91, "text": "renewal terms",
                     "metadata": {"pipeline_id": "pip-001"}}],
        "indexes_searched": [{"index_name": "Vector store A", "result_count": 1, "search_time_ms": 12}],
        "total_search_time_ms": 15}})
    page = app.page
    expect(page.locator("#mselb")).to_contain_text("Vector store A")
    page.fill("#sq", "renewal terms")
    page.click("#sgo")
    expect(page.locator("#sout")).to_contain_text("1 result")
    expect(page.locator("#sout")).to_contain_text("contract-1.pdf")
    body = app.called("/api/playground/search", "POST")[-1]["payload"]
    assert body["destination_ids"] == ["dst-vec"] and body["query"] == "renewal terms"


def test_search_store_picker_can_clear_selection(spa_app):
    page = spa_app("#/search").page
    page.click("#mselb")
    pop = page.locator(".msel-pop")
    expect(pop).to_contain_text("1 selected")
    pop.locator("[data-clr]").click()
    expect(pop).to_contain_text("0 selected")
    expect(page.locator("#mselb")).to_contain_text("Choose vector stores")


def test_metrics_shows_totals_from_timeseries(spa_app):
    page = spa_app("#/metrics").page
    expect(page.locator("#view h1")).to_have_text("Metrics")
    expect(page.locator("#mstats")).to_contain_text("3,000")
    expect(page.locator("#mstats")).not_to_contain_text("Partial history")


def test_metrics_warns_when_history_is_partial(spa_app):
    page = spa_app("#/metrics", overrides={"/api/metrics/timeseries": {
        "source": "cosmos", "coverage": "recent", "granularity": "hour",
        "buckets": [{"t": "2026-09-26T10:00:00Z", "processed": 60, "failed": 0}]}}).page
    expect(page.locator("#mstats .callout.warn")).to_contain_text("Partial history")


def test_agent_is_locked_without_chat_model(spa_app):
    page = spa_app("#/home", overrides={"/api/models": {"models": [EMBED_MODEL]}}).page
    button = page.locator('.topbar [data-act="agent"]')
    expect(button).to_have_class(re.compile(r"needs-setup"))
    button.click()
    lock = page.locator(".agent-lock")
    expect(lock).to_be_visible()
    expect(lock).to_contain_text("No chat model is registered")
    lock.get_by_role("link", name="Register a chat model").click()
    expect(page).to_have_url(re.compile(r"#/models/new\?cat=chat"))


def test_agent_is_available_with_chat_model(spa_app):
    page = spa_app("#/home").page
    button = page.locator('.topbar [data-act="agent"]')
    expect(button).not_to_have_class(re.compile(r"needs-setup"))
    button.click()
    expect(page.locator(".agent-lock")).to_have_count(0)


def test_every_top_level_route_renders_without_errors(spa_app):
    app = spa_app("#/home")
    for hash_route, heading in [("#/pipelines", "Pipelines"), ("#/new", "New pipeline"), ("#/connections", "Connections"),
                                ("#/models", "Models & recipes"), ("#/search", "Search"), ("#/issues", "Issues"),
                                ("#/metrics", "Metrics")]:
        app.goto(hash_route)
        expect(app.page.locator("#view h1").first).to_have_text(heading)
    unmocked = {r["path"] for r in app.requests} - {k.split(" ")[-1] for k in app.responses}
    assert not unmocked, f"SPA called unmocked endpoints: {sorted(unmocked)}"


def test_api_client_rejects_unsafe_paths(spa_app):
    page = spa_app("#/home").page
    results = page.evaluate("""async () => {
        const out = {};
        for (const p of ['/api/../admin', 'https://evil.example/api/x', '/api/a b', '/api/a\\\\b', '/other']) {
            try { await OVX.api(p); out[p] = 'allowed'; } catch (e) { out[p] = e.message; }
        }
        return out;
    }""")
    assert set(results.values()) == {"Invalid API path"}, results


@pytest.mark.parametrize("order", ["insert-first", "delete-first"])
def test_cosmos_chunk_cleanup_order_is_sent_on_create(spa_app, order):
    app = spa_app("#/new?source=src-orders&reset=1", overrides={
        "POST /api/destinations/test-connection": {
            "success": True, "vector_indexes": [{"path": "/embedding", "dimensions": 1536}]},
        "POST /api/pipelines": {"pipeline": {"id": "pip-created"}},
    })
    page = app.page
    expect(page.locator("#wcko")).to_have_value("insert-first")
    page.select_option("#wcko", order)
    page.click("#wnext")
    page.click("#wback")
    expect(page.locator("#wcko")).to_have_value(order)
    page.click("#wnext")
    page.click("#wnext")
    page.click('[data-store="dst-vec"]')
    expect(page.locator("#wnext")).to_be_enabled()
    page.click("#wnext")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/pipelines")):
        page.click("#wcreate")
    body = app.called("/api/pipelines", "POST")[-1]["payload"]
    assert body["chunk_config"]["cleanup_order"] == order
    assert body["content_strategy"] == "chunk" and body["processing_mode"] == "queue"


def test_cleanup_order_only_shown_for_chunked_cosmos_sources(spa_app):
    page = spa_app("#/new?source=src-docs&reset=1").page
    expect(page.locator("#wcs")).to_be_visible()
    expect(page.locator("#wcko")).to_have_count(0)
    page = spa_app("#/new?source=src-orders&reset=1").page
    expect(page.locator("#wcko")).to_be_visible()
    page.locator("details", has=page.locator("#wcm")).locator("summary").click()
    page.select_option("#wcm", "http_url")
    expect(page.locator("#wcko")).to_have_count(0)
    page.locator("details", has=page.locator("#wcm")).locator("summary").click()
    page.select_option("#wcm", "field")
    expect(page.locator("#wcko")).to_be_visible()
    page.click('[data-strat="truncate"]')
    expect(page.locator("#wcko")).to_have_count(0)


@pytest.mark.parametrize("source_id", ["src-orders", "src-docs"])
def test_chunk_cleanup_order_is_editable_only_for_cosmos(spa_app, source_id):
    from spa_mock import default_responses

    pipeline = default_responses()["/api/pipelines"]["pipelines"][0]
    pipeline.update({
        "sources": [{"source_id": source_id, "content_mode": "field", "content_fields": ["content"]}],
        "content_strategy": "chunk",
        "chunk_config": {"chunk_size": 500, "chunk_overlap": 50, "chunk_unit": "chars",
                         "store_text": True, "cleanup_order": "delete-first"},
    })
    app = spa_app("#/pipelines/pip-001/settings", overrides={
        "/api/pipelines": {"pipelines": [pipeline]},
        "PUT /api/pipelines/pip-001": {"pipeline": pipeline},
    })
    page = app.page
    if source_id == "src-orders":
        expect(page.locator("#scko")).to_have_value("delete-first")
        page.select_option("#scko", "insert-first")
        with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/pipelines/pip-001")):
            page.click("#ssave")
        config = app.called("/api/pipelines/pip-001", "PUT")[-1]["payload"]["chunk_config"]
        assert config["cleanup_order"] == "insert-first"
        assert config["chunk_size"] == 500 and config["chunk_overlap"] == 50 and config["store_text"]
    else:
        expect(page.locator("#scko")).to_have_count(0)
