def test_startup_waits_visibly_then_shows_app(ui_page):
    page, _ = ui_page
    page.evaluate("localStorage.setItem('omnivec_token', 'test-only')")
    pending = []
    page.route("**/api/auth/login", lambda route: pending.append(route))
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Checking your session...", exact=True).wait_for()
    assert page.locator("#startup-screen").is_visible()
    assert not page.locator("#main-app").is_visible()
    assert pending
    pending[0].fulfill(status=200, content_type="application/json", body="{}")
    page.locator("#main-app").wait_for(state="visible")
    assert not page.locator("#startup-screen").is_visible()


def test_startup_server_error_keeps_token_and_offers_retry(ui_page):
    page, _ = ui_page
    page.evaluate("localStorage.setItem('omnivec_token', 'test-only')")
    page.route("**/api/auth/login", lambda route: route.fulfill(status=503, body="unavailable"))
    page.reload(wait_until="domcontentloaded")
    page.locator("#startup-retry").wait_for(state="visible")
    assert "HTTP 503" in page.locator("#startup-message").inner_text()
    assert page.evaluate("localStorage.getItem('omnivec_token')") == "test-only"
    assert page.locator("#startup-screen").get_attribute("aria-busy") == "false"


def test_startup_invalid_token_returns_to_login(ui_page):
    page, _ = ui_page
    page.evaluate("localStorage.setItem('omnivec_token', 'test-only')")
    page.route("**/api/auth/login", lambda route: route.fulfill(status=401, body="{}"))
    page.reload(wait_until="domcontentloaded")
    page.locator("#login-screen").wait_for(state="visible")
    assert page.evaluate("localStorage.getItem('omnivec_token')") is None
    assert not page.locator("#startup-screen").is_visible()


def test_startup_timeout_is_bounded_and_retry_keeps_session(ui_page):
    page, _ = ui_page
    page.clock.install()
    page.evaluate("localStorage.setItem('omnivec_token', 'test-only')")
    page.route("**/api/auth/login", lambda route: None)
    page.reload(wait_until="domcontentloaded")
    page.clock.run_for(16000)
    page.locator("#startup-retry").wait_for(state="visible")
    assert "timed out" in page.locator("#startup-message").inner_text()
    assert page.evaluate("localStorage.getItem('omnivec_token')") == "test-only"
    page.unroute("**/api/auth/login")
    page.locator("#startup-retry").click()
    page.locator("#main-app").wait_for(state="visible")


def test_inventory_summaries_fixed_height_and_pagination(ui_page):
    page, _ = ui_page
    page.evaluate("""() => {
        sources = Array.from({length: 23}, (_,i) => ({id:'src-'+i,name:'Source '+i,type:'cosmosdb',enabled:i!==0,config:{}}));
        destinations = Array.from({length: 23}, (_,i) => ({id:'dst-'+i,name:'Destination '+i,type:'cosmosdb-vector',config:{endpoint:'https://'+'long'.repeat(100)}}));
        pipelines = Array.from({length: 23}, (_,i) => ({id:'pip-'+i,name:'Pipeline '+i,status:i===0?'paused':'active',sources:[],destination_id:'dst-0',description:'long '.repeat(100)}));
        itemsPerPage = 50;
        showMainApp();
        renderSources(); renderDestinations(); renderPipelines();
    }""")
    for kind in ["sources", "destinations", "pipelines"]:
        page.evaluate("(kind) => showSection(kind)", kind)
        summary = page.locator(f"#{kind}-summary")
        assert summary.locator(".stat-card").count() == 4
        assert summary.locator(".stat-value").first.inner_text() == "23"
        table = page.locator(f"#section-{kind} .inventory-table")
        assert page.locator(f"#{kind}-table > tr").count() == 10
        first_height = table.bounding_box()["height"]
        assert table.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        page.set_viewport_size({"width": 800, "height": 1000})
        assert table.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        assert table.bounding_box()["height"] == first_height
        page.set_viewport_size({"width": 1440, "height": 1000})
        nav = page.locator(f"#pagination-{kind}")
        nav.get_by_role("button", name="Next", exact=True).click()
        nav.get_by_role("button", name="Next", exact=True).click()
        assert page.locator(f"#{kind}-table > tr").count() == 3
        assert table.bounding_box()["height"] == first_height
        assert "Page 3 of 3" in nav.inner_text()
        page.evaluate(f"{kind} = []; render{kind.capitalize()}()")
        assert "Showing 0-0 of 0" in nav.inner_text()
        assert table.bounding_box()["height"] == first_height
