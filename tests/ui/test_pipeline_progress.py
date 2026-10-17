import json

from playwright.sync_api import expect

from spa_mock import default_responses


def show_pipeline(page, pipeline):
    data = default_responses()
    data["/api/pipelines"]["pipelines"] = [pipeline]
    page.evaluate("""data => {
        sources = data['/api/sources'].sources;
        destinations = data['/api/destinations'].destinations;
        pipelines = data['/api/pipelines'].pipelines;
        showMainApp();
        showSection('pipelines');
        renderPipelines();
    }""", data)
    page.locator("#pipelines-table").get_by_text(pipeline["name"], exact=True).click()
    page.locator('#pipeline-detail-modal [data-tab="live"]').click()


def test_live_progress_reaches_only_confirmed_completion(ui_page):
    page, _ = ui_page
    pipeline = default_responses()["/api/pipelines"]["pipelines"][0]
    pipeline["stats"] = {"source_doc_count": 3, "embedded_count": 0, "jobs": {"failed": 0}}
    page.route("**/api/pipelines/pip-001", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(pipeline)))
    show_pipeline(page, pipeline)
    expect(page.locator("#live-counter")).to_have_text("0 / 3")
    pipeline["stats"]["embedded_count"] = 1
    page.locator('[onclick="liveDashboardManualRefresh()"]').click()
    expect(page.locator("#live-counter")).to_have_text("1 / 3")
    expect(page.locator("#live-pct")).to_have_text("33.3%")
    page.wait_for_timeout(4500)
    expect(page.locator("#live-counter")).to_have_text("1 / 3")
    pipeline["stats"]["embedded_count"] = 3
    page.locator('[onclick="liveDashboardManualRefresh()"]').click()
    expect(page.locator("#live-counter")).to_have_text("3 / 3")
    expect(page.locator("#live-pct")).to_have_text("100.0%")
    expect(page.locator("#live-eta")).to_contain_text("Complete")
    page.evaluate("stopLivePipelinePoll()")


def test_live_progress_does_not_invent_an_unknown_total(ui_page):
    page, _ = ui_page
    pipeline = default_responses()["/api/pipelines"]["pipelines"][0]
    pipeline["stats"] = {"source_doc_count": None, "embedded_count": 3, "jobs": {"failed": 0}}
    page.route("**/api/pipelines/pip-001", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(pipeline)))
    show_pipeline(page, pipeline)
    expect(page.locator("#live-counter")).to_have_text("3")
    expect(page.locator("#live-pct")).to_have_text("\u2014")
    expect(page.locator("#live-eta")).not_to_contain_text("Complete")
    page.evaluate("stopLivePipelinePoll()")


def test_pipeline_filter_hides_rows_without_changing_resources(ui_page):
    page, _ = ui_page
    data = default_responses()
    page.evaluate("""data => {
        sources = data['/api/sources'].sources;
        destinations = data['/api/destinations'].destinations;
        pipelines = data['/api/pipelines'].pipelines;
        showMainApp();
        showSection('pipelines');
        renderPipelines();
    }""", data)
    page.locator("#pipeline-filter").fill("pip-023")
    expect(page.locator("#pipelines-table tr")).to_have_count(1)
    expect(page.locator("#pipelines-table")).to_contain_text("Pipeline 023")
    assert page.evaluate("pipelines.length") == 23
    page.locator("#pipeline-filter").fill("No such pipeline")
    expect(page.locator("#pipelines-table")).to_contain_text("No matching pipelines")
    page.locator("#pipeline-filter").fill("")
    expect(page.locator("#pipelines-table tr")).to_have_count(10)
    assert page.evaluate("pipelines.length") == 23
