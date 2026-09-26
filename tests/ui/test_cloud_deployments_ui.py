import copy
import json

import pytest


SUB = "11111111-1111-4111-8111-111111111111"
RG = f"/subscriptions/{SUB}/resourceGroups/test"


@pytest.fixture
def cloud(ui_page):
    page, _ = ui_page
    state = {"enabled": False, "jobs": [], "writes": []}
    def route(request):
        path = request.request.url.split("/api/cloud-deployments", 1)[1]
        if request.request.method == "GET":
            result = {"jobs": state["jobs"], "capabilities": {"enabled": state["enabled"]}}
        elif path == "/plan":
            body = request.request.post_data_json
            state["writes"].append(body)
            job = {
                "id": "deploy-ui" + (str(len(state["jobs"])) if state["jobs"] else ""), "kind": body["kind"], "status": "awaiting_approval", "attempts": 0,
                "stage": "Review plan", "plan_hash": "hash", "history": [], "result": {},
                "plan": {
                    **body, "resources": [RG], "permissions": [],
                    "notice": "This plan creates billable Azure resources.",
                    "deployer_permissions": [],
                },
            }
            state["jobs"].append(job)
            result = job
        else:
            state["writes"].append(request.request.post_data_json)
            state["jobs"][-1]["status"] = "queued"
            result = state["jobs"][-1]
        request.fulfill(status=200, content_type="application/json", body=json.dumps(result))
    page.route("**/api/cloud-deployments**", route)
    page.route("**/api/destinations", lambda r: r.fulfill(content_type="application/json", body=json.dumps({
        "destinations": [{"id": "dst-1", "name": "Vectors", "type": "cosmosdb-vector"}]})))
    page.route("**/api/models", lambda r: r.fulfill(content_type="application/json", body=json.dumps({
        "models": [{"id": "mdl-1", "name": "Embedding", "type": "azure-openai", "model_category": "embedding"},
                   {"id": "mdl-chat", "name": "Chat", "type": "azure-openai", "model_category": "chat"}]})))
    page.evaluate("showMainApp(); showSection('mcp')")
    page.locator("#mcp-destination option[value='dst-1']").wait_for(state="attached")
    return page, state


def prepare_mcp(page):
    page.locator("#mcp-destination").select_option("dst-1")
    page.locator("#mcp-model").select_option("mdl-1")
    for name, value in {
        "subscription": SUB, "group": "test", "location": "eastus2",
        "cosmos": RG + "/providers/Microsoft.DocumentDB/databaseAccounts/cosmos",
        "openai": RG + "/providers/Microsoft.CognitiveServices/accounts/openai",
    }.items():
        page.locator("#mcp-" + name).fill(value)
    page.locator("#mcp-form").get_by_role("button", name="Prepare plan (no provisioning)").click()
    page.locator("#mcp-review").wait_for(state="visible")


def test_preview_cannot_deploy_when_disabled(cloud):
    page, state = cloud
    assert state["writes"] == []
    assert page.locator("#mcp-model option[value='mdl-chat']").count() == 0
    prepare_mcp(page)
    assert state["writes"][0]["resource_group_id"] == RG
    assert state["jobs"][0]["status"] == "awaiting_approval"
    page.locator("#mcp-consent").check()
    assert page.locator("#mcp-approve").is_disabled()
    assert "disabled" in page.locator("#mcp-capability").inner_text()
    page.locator("#mcp-refresh").click()
    assert len(state["writes"]) == 1


def test_approval_requires_explicit_consent_and_reappears_after_reload(cloud):
    page, state = cloud
    state["enabled"] = True
    prepare_mcp(page)
    assert page.locator("#mcp-approve").is_disabled()
    page.locator("#mcp-consent").check()
    page.locator("#mcp-approve").click()
    page.get_by_text("deploy-ui — queued — Review plan (attempt 0/3)", exact=True).wait_for()
    assert len(state["writes"]) == 2
    assert state["writes"][-1] == {"plan_hash": "hash", "approve_cost_and_permissions": True}
    assert page.locator("#mcp-approve").is_disabled()
    page.reload(wait_until="domcontentloaded")
    page.evaluate("showMainApp(); showSection('mcp')")
    page.get_by_text("deploy-ui — queued — Review plan (attempt 0/3)", exact=True).wait_for()
    assert len(state["writes"]) == 2


def test_foundry_only_accepts_successful_mcp_and_shows_failures_as_text(cloud):
    page, state = cloud
    prepare_mcp(page)
    job = state["jobs"][0]
    job.update(status="succeeded", result={"server_url": "https://test.azurewebsites.net/api/mcp"})
    failed = copy.deepcopy(job)
    failed.update(id="deploy-failed", status="failed", error={"code": "denied", "message": "<img src=x onerror=alert(1)>", "resource_id": RG})
    state["jobs"].append(failed)
    page.locator("#mcp-refresh").click()
    page.get_by_text("denied: <img src=x onerror=alert(1)>", exact=False).wait_for()
    assert page.locator("#mcp-jobs img").count() == 0
    page.evaluate("showSection('foundry')")
    page.locator("#foundry-mcp option[value='deploy-ui']").wait_for(state="attached")
    assert page.locator("#foundry-mcp option[value='deploy-failed']").count() == 0
    page.locator("#foundry-mcp").select_option("deploy-ui")
    project = RG + "/providers/Microsoft.CognitiveServices/accounts/foundry/projects/project"
    page.locator("#foundry-project").fill(project)
    page.locator("#foundry-chat").fill("chat-deployment")
    page.locator("#foundry-form").get_by_role("button", name="Prepare plan (no provisioning)").click()
    page.locator("#foundry-review").wait_for(state="visible")
    assert state["writes"][-1]["kind"] == "foundry"
    assert state["writes"][-1]["project_resource_id"] == project
    assert "never into the browser" in page.locator("#foundry-workspace").inner_text()


def test_isolated_container_can_be_prepared_without_any_provisioning(cloud):
    page, state = cloud
    page.get_by_text("Need a new isolated Cosmos vector container?", exact=True).click()
    page.locator("#mcp-container-account").fill(RG + "/providers/Microsoft.DocumentDB/databaseAccounts/cosmos")
    page.locator("#mcp-container-database").fill("e2e")
    page.locator("#mcp-container-name").fill("fresh-demo")
    page.locator("#mcp-container-model").select_option("mdl-1")
    page.get_by_role("button", name="Prepare container plan", exact=True).click()
    page.locator("#mcp-review").wait_for(state="visible")
    assert state["writes"][-1]["kind"] == "cosmos_container"
    assert state["jobs"][-1]["status"] == "awaiting_approval"
    assert page.locator("#mcp-approve").is_disabled()


def test_agent_test_is_a_separate_approval_plan(cloud):
    page, state = cloud
    state["jobs"].append({
        "id": "created-agent", "kind": "foundry", "status": "succeeded", "attempts": 1,
        "stage": "Completed", "plan": {"resource_group_id": RG, "location": "eastus2"},
        "result": {"agent_name": "fresh-agent"},
    })
    page.evaluate("showSection('foundry')")
    page.get_by_text("Test a deployed Foundry agent", exact=True).click()
    page.locator("#foundry-test-agent option[value='created-agent']").wait_for(state="attached")
    page.locator("#foundry-test-agent").select_option("created-agent")
    page.locator("#foundry-test-question").fill("What is the meal limit?")
    page.get_by_role("button", name="Prepare agent test plan", exact=True).click()
    page.locator("#foundry-review").wait_for(state="visible")
    assert state["writes"][-1]["kind"] == "verification"
    assert state["writes"][-1]["foundry_deployment_id"] == "created-agent"
    assert state["writes"][-1]["question"] == "What is the meal limit?"
    assert page.locator("#foundry-approve").is_disabled()
