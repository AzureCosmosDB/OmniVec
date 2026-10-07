from playwright.sync_api import expect
import pytest


CAPS = {"mock_enabled": True, "allowed_source_types": ["mock"]}
SOURCE = {"id": "src-mock", "name": "Mock source", "type": "mock",
          "config": {"document_count": 100000, "document_size_bytes": 8, "documents_per_second": 0, "batch_size": 50}}
SINK = {"id": "dst-mock", "name": "Mock sink", "type": "mock",
        "config": {"embedding_dimensions": 1024, "accepted_documents_per_second": 0, "burst_documents": 2048}}
MODEL = {"id": "mdl-ext-mock", "name": "Mock embedding", "type": "mock-embedding",
         "model_category": "embedding", "embedding_dim": 1024}


def test_portal_mock_source_and_sink_configuration(spa_app):
    for kind, fields in [
        ("source", {"document_count": "100000", "document_size_bytes": "8", "documents_per_second": "1234"}),
        ("store", {"embedding_dimensions": "1024", "accepted_documents_per_second": "500"}),
    ]:
        app = spa_app(f"#/connections/new/{kind}?type=mock", overrides={"/api/capabilities": CAPS})
        page = app.page
        page.locator("[data-name]").fill("Mock " + kind)
        for field, value in fields.items():
            page.locator(f'[data-k="{field}"]').fill(value)
        if kind == "source":
            page.locator('[data-k="embedding_transport"]').select_option("fp32")
        page.locator("#atest").click()
        page.locator("#asave").click()
        path = "/api/sources" if kind == "source" else "/api/destinations"
        expect(page).to_have_url(__import__("re").compile("#/connections"))
        calls = app.called(path, "POST")
        assert len(calls) == 1 and calls[0]["payload"]["type"] == "mock"
        assert all(calls[0]["payload"]["config"][field] == int(value) for field, value in fields.items())
        if kind == "source":
            assert calls[0]["payload"]["config"]["embedding_transport"] == "fp32"


def test_portal_register_mock_embedding_dimension(spa_app):
    app = spa_app("#/models/new", overrides={"/api/capabilities": CAPS})
    page = app.page
    page.locator("#mprovider").select_option("mock-embedding")
    page.locator("#mn").fill("Mock 768")
    page.locator("#mdim").fill("768")
    page.locator("#msave").click()
    expect(page).to_have_url(__import__("re").compile("#/models$"))
    payload = app.called("/api/models", "POST")[0]["payload"]
    assert payload["provider_type"] == "mock-embedding" and payload["embedding_dim"] == 768


@pytest.mark.parametrize("model_type", ["mock-embedding", "openai"])
def test_portal_creates_registered_all_mock_pipeline(spa_app, model_type):
    app = spa_app("#/new", overrides={
        "/api/capabilities": CAPS, "/api/sources": {"sources": [SOURCE]},
        "/api/destinations": {"destinations": [SINK]}, "/api/models": {"models": [{**MODEL, "type": model_type}]},
        "/api/pipelines": {"pipelines": []}, "POST /api/pipelines": {"pipeline": {"id": "pip-mock"}},
    })
    page = app.page
    page.locator('[data-src="src-mock"]').click()
    page.locator("#wnext").click()
    page.locator("#wnext").click()
    page.locator('[data-model="mdl-ext-mock"]').click()
    page.locator("#wnext").click()
    page.locator('[data-store="dst-mock"]').click()
    expect(page.locator("#wnext")).to_be_enabled()
    page.locator("#wnext").click()
    page.locator("#wcreate").click()
    expect(page).to_have_url(__import__("re").compile("#/pipelines/pip-mock"))
    body = app.called("/api/pipelines", "POST")[0]["payload"]
    assert body["sources"][0]["source_id"] == "src-mock"
    assert body["destination_id"] == "dst-mock" and body["docgrok_pipeline"] == "mdl-ext-mock"
    assert body["processing_mode"] == "inline" and body["content_strategy"] == "truncate"
