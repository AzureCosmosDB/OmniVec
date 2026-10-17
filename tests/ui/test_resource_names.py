import re

from playwright.sync_api import expect

from spa_mock import default_responses


LONG_PREFIX = "Policy ingestion pipeline for cross-tenant SharePoint and Cosmos DB " * 3
PIPELINE_NAME = LONG_PREFIX + "12345"
SOURCE_NAME = LONG_PREFIX + "67890"
STORE_NAME = LONG_PREFIX + "abcde"
MODEL_NAME = LONG_PREFIX + "vwxyz"


def long_name_responses():
    responses = default_responses()
    source = responses["/api/sources"]["sources"][0]
    source["name"] = SOURCE_NAME
    store = responses["/api/destinations"]["destinations"][0]
    store["name"] = STORE_NAME
    model = responses["/api/models"]["models"][0]
    model["name"] = model["deployment"] = MODEL_NAME
    pipeline = responses["/api/pipelines"]["pipelines"][0]
    pipeline["name"] = PIPELINE_NAME
    responses["/api/pipelines"]["pipelines"] = [pipeline]
    return responses


def assert_middle_ellipsis(label, full_name):
    expect(label).to_have_attribute("title", full_name)
    assert label.text_content() == full_name
    expect(label.locator(".resource-name-suffix")).to_have_text(full_name[-5:])
    dimensions = label.evaluate("""el => {
        const prefix = el.querySelector('.resource-name-prefix');
        const suffix = el.querySelector('.resource-name-suffix');
        return {clipped: prefix.scrollWidth > prefix.clientWidth,
                ellipsis: getComputedStyle(prefix).textOverflow,
                suffixFits: suffix.getBoundingClientRect().right <= el.getBoundingClientRect().right + 1};
    }""")
    assert dimensions == {"clipped": True, "ellipsis": "ellipsis", "suffixFits": True}


def test_spa_long_pipeline_names_keep_suffix_and_original_navigation(spa_app):
    app = spa_app("#/pipelines", overrides=long_name_responses())
    page = app.page
    assert_middle_ellipsis(page.locator("#ptbl .nm .resource-name"), PIPELINE_NAME)
    assert_middle_ellipsis(page.locator("#ptbl .chip-flow .resource-name"), STORE_NAME)

    page.locator("#ptbl tr[data-id='pip-001']").click()
    expect(page).to_have_url(re.compile(r"#/pipelines/pip-001$"))
    expect(page.locator("#view h1 .resource-name")).to_have_attribute("title", PIPELINE_NAME)
    for href, name in [
        ("#/connections/source/src-docs", SOURCE_NAME),
        ("#/connections/store/dst-vec", STORE_NAME),
        ("#/models/mdl-embed", MODEL_NAME),
    ]:
        assert_middle_ellipsis(page.locator(f".fnode[href='{href}'] .resource-name"), name)

    app.goto("#/pipelines")
    page.locator("#psrc button").click()
    assert_middle_ellipsis(page.locator("#psrc .msel-it[data-v='src-docs'] .resource-name"), SOURCE_NAME)
    page.locator("#psrc .msel-it[data-v='src-docs']").click()
    expect(page.locator("#psrc button .resource-name")).to_have_attribute("title", SOURCE_NAME)
    expect(page.locator("#ptbl tr[data-id='pip-001']")).to_be_visible()


def test_spa_wizard_and_search_picker_preserve_full_resource_values(spa_app):
    responses = long_name_responses()
    responses["/api/destinations"]["destinations"].append({
        **responses["/api/destinations"]["destinations"][0],
        "id": "dst-second",
        "name": "Second vector store",
    })
    app = spa_app("#/new", overrides=responses)
    page = app.page
    assert_middle_ellipsis(page.locator("[data-src='src-docs'] .resource-name"), SOURCE_NAME)
    page.locator("[data-src='src-docs']").click()
    expect(page.locator("#wsum .resource-name").first).to_have_attribute("title", SOURCE_NAME)
    assert app.responses["/api/sources"]["sources"][0]["name"] == SOURCE_NAME

    app.goto("#/search")
    assert_middle_ellipsis(page.locator("#mselb .resource-name"), STORE_NAME)
    page.locator("#mselb").click()
    assert_middle_ellipsis(page.locator(".msel-it .resource-name").first, STORE_NAME)
    expect(page.locator("[data-sid='dst-vec']")).to_be_checked()
    page.locator("[data-sid='dst-second']").check()
    assert_middle_ellipsis(page.locator("#mselb .resource-name"), STORE_NAME)
    badge = page.locator("#mselb .badge")
    expect(badge).to_have_text("+1")
    assert badge.evaluate("""el => {
        const bounds = el.getBoundingClientRect();
        const host = el.parentElement.getBoundingClientRect();
        return bounds.left >= host.left && bounds.right <= host.right &&
               bounds.top >= host.top && bounds.bottom <= host.bottom;
    }""")
    page.locator("[data-done]").click()
    page.locator("#sq").fill("Find policy")
    page.locator("#sgo").click()
    page.wait_for_function("document.querySelector('#sgo').textContent.trim() === 'Search'")
    search = next(request for request in app.requests if request["path"] == "/api/playground/search")
    assert search["payload"]["destination_ids"] == ["dst-vec", "dst-second"]


def test_classic_tables_flows_and_native_options_keep_identifiers(ui_page):
    page, _ = ui_page
    responses = long_name_responses()
    page.evaluate("""data => {
        sources = data['/api/sources'].sources;
        destinations = data['/api/destinations'].destinations;
        pipelines = data['/api/pipelines'].pipelines;
        dgAllModels = data['/api/models'].models;
        showMainApp();
        showSection('pipelines');
        renderPipelines();
    }""", responses)
    assert_middle_ellipsis(page.locator("#pipelines-table td:first-child .resource-name"), PIPELINE_NAME)
    for kind, name in [("source", SOURCE_NAME), ("destination", STORE_NAME)]:
        assert_middle_ellipsis(page.locator(f"#pipelines-table .pipeline-node.{kind} .resource-name"), name)

    page.locator('#section-pipelines .header button[onclick="showAddPipelineModal()"]').click()
    source = page.locator("#pipeline-source-select option[value='src-docs']")
    store = page.locator("#pipeline-destination option[value='dst-vec']")
    expect(source).to_have_attribute("title", SOURCE_NAME)
    expect(store).to_have_attribute("title", STORE_NAME)
    assert source.text_content() == SOURCE_NAME[:32] + "..." + SOURCE_NAME[-5:] + " (azure-blob)"
    assert store.text_content() == STORE_NAME[:32] + "..." + STORE_NAME[-5:]
    page.locator("#pipeline-next-btn").click()
    page.locator("#pipeline-source-select").select_option("src-docs")
    expect(page.locator("#pipeline-source-select")).to_have_value("src-docs")


def test_label_resizes_without_changing_short_or_escaped_unicode_names(ui_page):
    page, _ = ui_page
    name = '<Policy & "owner">' + LONG_PREFIX + "\U0001f600wxyz"
    page.evaluate("""name => {
        const host = document.createElement('div');
        host.id = 'name-test';
        host.style.cssText = 'position:fixed;top:0;left:0;width:160px;font:16px monospace';
        host.innerHTML = OVNames.render(name) + '<div id="short-name">' + OVNames.render('Policies') + '</div>';
        document.body.appendChild(host);
    }""", name)
    label = page.locator("#name-test > .resource-name")
    assert_middle_ellipsis(label, name)
    expect(label.locator("img,script")).to_have_count(0)
    narrow = label.locator(".resource-name-prefix").evaluate("el => el.clientWidth")
    page.locator("#name-test").evaluate("el => el.style.width = '300px'")
    assert_middle_ellipsis(label, name)
    assert label.locator(".resource-name-prefix").evaluate("el => el.clientWidth") > narrow
    short = page.locator("#short-name .resource-name")
    assert short.text_content() == "Policies"
    assert short.locator(".resource-name-prefix").evaluate("el => el.scrollWidth === el.clientWidth")
    assert page.evaluate("name => OVNames.shorten(name).endsWith('...\\u{1f600}wxyz')", name)
