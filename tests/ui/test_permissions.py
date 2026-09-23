import json


def response(**overrides):
    return {
        "status": "access_denied", "summary": "The service denied this read operation.",
        "checks": [{"name": "Source read", "status": "failed"}],
        "missing": [], "limitations": ["Writes are not verified."],
        "verification": "Run Check again after permission propagation.",
        "resource_id": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/customer/providers/Microsoft.Storage/storageAccounts/example",
        "principal_id": "22222222-2222-4222-8222-222222222222",
        "scope": "documents", "role": "Storage Blob Data Reader",
        "commands": {"powershell": "az 'role' 'assignment' 'create' '--name' 'test'", "bash": "az role assignment create --name test"},
        **overrides,
    }


def open_check(page, data):
    page.route("**/api/permissions/check", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(data)))
    page.evaluate("showResourcePermissions('source', 'src-example')")
    dialog = page.locator("#resource-permissions-dialog")
    dialog.get_by_role("button", name="Check access", exact=True).click()
    dialog.get_by_text(data["summary"], exact=True).wait_for()
    return dialog


def test_permission_failure_shows_exact_plan_and_copy_buttons(ui_page):
    page, _ = ui_page
    dialog = open_check(page, response())
    assert dialog.get_by_role("button", name="Copy PowerShell", exact=True).is_visible()
    assert dialog.get_by_role("button", name="Copy Bash", exact=True).is_visible()
    assert dialog.get_by_role("button", name="Copy administrator instructions").is_visible()
    assert "customer" in dialog.inner_text() and "22222222" in dialog.inner_text()
    assert "Writes are not verified" in dialog.inner_text()


def test_missing_identity_never_offers_grant_commands(ui_page):
    page, _ = ui_page
    dialog = open_check(page, response(commands={}, missing=["Enter the principal/object ID."]))
    assert dialog.locator("details").get_attribute("open") is not None
    assert dialog.get_by_role("button", name="Copy PowerShell").count() == 0
    assert "YOUR_" not in dialog.inner_text()


def test_network_error_never_suggests_role_grant(ui_page):
    page, _ = ui_page
    dialog = open_check(page, response(status="network_unreachable", summary="Network unreachable; check DNS.",
                                      commands={}, role=None))
    assert dialog.get_by_role("button", name="Copy Bash", exact=True).count() == 0
    assert "Network unreachable" in dialog.inner_text()


def test_guidance_is_text_not_html_and_changes_invalidate_commands(ui_page):
    page, _ = ui_page
    dialog = open_check(page, response(summary='<img src=x onerror="window.injected=true">'))
    assert dialog.locator("img").count() == 0
    assert page.evaluate("window.injected") is None
    dialog.locator("summary").click()
    dialog.locator('input[name="permission_resource_id"]').fill("changed")
    assert dialog.get_by_role("button", name="Copy PowerShell", exact=True).count() == 0
    assert dialog.get_by_role("button", name="Check access", exact=True).is_enabled()


def test_saved_resource_request_does_not_send_stored_credentials(ui_page):
    page, _ = ui_page
    captured = []
    def handle(route):
        captured.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response(commands={})))
    page.route("**/api/permissions/check", handle)
    page.evaluate("showResourcePermissions('destination', 'dst-example')")
    page.locator("#resource-permissions-dialog").get_by_role("button", name="Check access", exact=True).click()
    page.get_by_text("The service denied this read operation.", exact=True).wait_for()
    assert captured == [{"kind": "destination", "resource_ref": "dst-example", "resource_id": "", "principal_id": ""}]
