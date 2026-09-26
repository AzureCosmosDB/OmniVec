from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "contracts" / "resource_connectors.json").read_text(encoding="utf-8"))


def test_sharepoint_source_wizard_guides_permissions_and_tests_runtime(ui_page):
    page, requests = ui_page
    page.evaluate("showAddSourceModal()")
    page.fill('#source-form input[name="name"]', "SharePoint policies")
    page.select_option('#source-form select[name="type"]', "sharepoint")

    assert page.locator(
        '#source-form input[name="auth_type"][value="connection-string"]'
    ).is_disabled()

    page.click("#source-wizard-next")
    page.fill('#source-form input[name="site_id"]', "contoso.sharepoint.com,site,web")
    page.fill('#source-form input[name="drive_id"]', "drive-id")
    page.click("#source-wizard-next")

    guidance = page.locator("#source-permission-preflight").inner_text()
    assert "Sites.Selected" in guidance
    assert "no separate app registration is required" in guidance
    assert "Files.Read.All" in guidance
    assert "Exact Graph grant commands are not yet supported" in guidance
    assert "YOUR_" not in guidance

    page.click("#source-wizard-next")
    assert page.locator("#source-wizard-create").is_disabled()
    page.click("#test-source-btn")
    page.locator("#source-test-result").get_by_text("Connection Successful").wait_for()
    assert page.locator("#source-wizard-create").is_enabled()

    test_request = next(item for item in requests if item["url"].endswith("/api/sources/test-connection"))
    assert test_request["payload"] == {
        "type": "sharepoint",
        "config": {
            "auth_type": "managed-identity",
            "site_id": "contoso.sharepoint.com,site,web",
            "drive_id": "drive-id",
            "folder_path": "",
            "poll_interval_seconds": 60,
            "file_types": ["txt", "json", "pdf", "docx", "md", "csv", "html", "xml"],
            "max_file_size_bytes": 50 * 1024 * 1024,
        },
    }


def test_cosmos_destination_wizard_scopes_role_and_checks_vectors(ui_page):
    page, requests = ui_page
    page.evaluate("showAddDestinationModal()")
    page.fill('#destination-form input[name="name"]', "Cosmos vectors")
    page.click("#destination-wizard-next")
    page.fill('#destination-form input[name="endpoint"]', "https://example.documents.azure.com")
    page.fill('#destination-form input[name="database"]', "vectors")
    page.fill('#destination-form input[name="container"]', "embeddings")
    page.click("#destination-wizard-next")

    guidance = page.locator("#destination-permission-preflight").inner_text()
    assert "Cosmos DB Built-in Data Contributor" in guidance
    assert "/dbs/vectors/colls/embeddings" in guidance

    page.click("#destination-wizard-next")
    assert page.locator("#destination-wizard-create").is_disabled()
    page.click("#test-dest-btn")
    page.locator("#dest-test-result").get_by_text("Connection Successful").wait_for()
    page.get_by_text("/embedding", exact=True).wait_for(state="visible")
    assert page.locator("#destination-wizard-create").is_enabled()

    test_request = next(
        item for item in requests if item["url"].endswith("/api/destinations/test-connection")
    )
    assert test_request["payload"] == {
        "type": "cosmosdb-vector",
        "config": {
            "auth_type": "managed-identity",
            "endpoint": "https://example.documents.azure.com",
            "database": "vectors",
            "container": "embeddings",
        },
    }


def test_portal_connector_types_and_required_fields_match_contract(ui_page):
    page, _ = ui_page
    page.evaluate("showAddSourceModal()")
    source_types = page.locator('#source-form select[name="type"] option').evaluate_all(
        "options => options.map(option => option.value)"
    )
    assert set(source_types) == set(CONTRACT["sources"])
    for connector_type, definition in CONTRACT["sources"].items():
        page.select_option('#source-form select[name="type"]', connector_type)
        rendered_fields = page.locator(
            "#source-config-fields input[name], #source-config-fields select[name]"
        ).evaluate_all("fields => fields.map(field => field.name)")
        assert set(definition["required_fields"]).issubset(rendered_fields)

    page.evaluate("closeModal('source-modal'); showAddDestinationModal()")
    destination_types = page.locator(
        '#destination-form select[name="type"] option'
    ).evaluate_all("options => options.map(option => option.value)")
    assert set(destination_types) == set(CONTRACT["destinations"])
    for connector_type, definition in CONTRACT["destinations"].items():
        page.select_option('#destination-form select[name="type"]', connector_type)
        rendered_fields = page.locator(
            "#destination-config-fields input[name], #destination-config-fields select[name]"
        ).evaluate_all("fields => fields.map(field => field.name)")
        assert set(definition["required_fields"]).issubset(rendered_fields)


def test_resource_wizard_collapses_for_narrow_viewport(ui_page):
    page, _ = ui_page
    page.set_viewport_size({"width": 700, "height": 900})
    page.evaluate("showAddSourceModal()")
    grid_columns = page.locator("#source-form .resource-wizard").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns"
    )
    assert " " not in grid_columns.strip()
    assert page.locator("#source-form .resource-wizard-sidebar").evaluate(
        "element => getComputedStyle(element).display"
    ) == "flex"
    assert page.locator("#source-modal .modal").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )


def test_successful_connection_is_invalidated_when_configuration_changes(ui_page):
    page, _ = ui_page
    page.evaluate("showAddSourceModal()")
    page.fill('#source-form input[name="name"]', "Blob documents")
    page.click("#source-wizard-next")
    page.fill(
        '#source-form input[name="account_url"]',
        "https://example.blob.core.windows.net",
    )
    page.fill('#source-form input[name="container"]', "documents")
    page.click("#source-wizard-next")
    page.click("#source-wizard-next")
    page.click("#test-source-btn")
    page.locator("#source-test-result").get_by_text("Connection Successful").wait_for()
    assert page.locator("#source-wizard-create").is_enabled()

    page.click("#source-wizard-prev")
    page.click("#source-wizard-prev")
    page.fill('#source-form input[name="container"]', "changed-documents")
    page.click("#source-wizard-next")
    page.click("#source-wizard-next")
    assert page.locator("#source-wizard-create").is_disabled()

    page.check('#source-form input[name="create_without_test"]')
    assert page.locator("#source-wizard-create").is_enabled()


def test_sharepoint_source_detail_uses_sharepoint_fields_and_payload(ui_page):
    page, requests = ui_page
    page.evaluate(
        """
        sources = [{
            id: "src-sharepoint",
            name: "BAMI SharePoint same-tenant E2E",
            type: "sharepoint",
            enabled: true,
            config: {
                auth_type: "managed-identity",
                site_id: "contoso.sharepoint.com,site,web",
                drive_id: "drive-id",
                folder_path: "Policies",
                poll_interval_seconds: 90,
                file_types: ["pdf", "docx"],
                max_file_size_bytes: 26214400
            }
        }];
        showSourceDetail("src-sharepoint");
        """
    )

    page.locator("#source-detail-modal").wait_for(state="visible")
    page.locator("#source-detail-info").get_by_text(
        "SharePoint Online", exact=True
    ).wait_for(state="visible")
    assert page.locator("#detail-sp-site-id").input_value() == "contoso.sharepoint.com,site,web"
    assert page.locator("#detail-sp-drive-id").input_value() == "drive-id"
    assert page.locator("#detail-sp-folder-path").input_value() == "Policies"
    assert page.locator("#detail-sp-poll-interval").input_value() == "90"
    assert page.locator("#detail-sp-max-file-size").input_value() == "25"
    assert page.locator('.detail-sp-file-type[value="pdf"]').is_checked()
    assert not page.locator('.detail-sp-file-type[value="txt"]').is_checked()
    assert page.locator("#detail-endpoint").count() == 0
    assert page.locator("#detail-database").count() == 0

    page.click('#source-detail-modal [data-tab="auth"]')
    auth_panel = page.locator("#source-detail-config")
    auth_panel.get_by_text("Sites.Selected", exact=True).first.wait_for(
        state="visible"
    )
    auth_panel.get_by_text("Files.Read.All", exact=True).wait_for(state="visible")

    page.click('#source-detail-modal [data-tab="general"]')
    page.fill("#detail-sp-folder-path", "Policies/Published")
    page.fill("#detail-sp-poll-interval", "120")
    page.check('.detail-sp-file-type[value="txt"]')
    page.click("#source-detail-modal button:has-text('Test Connection')")
    page.locator("#source-detail-test-result").get_by_text("Source is accessible").wait_for()

    test_request = next(item for item in requests if item["url"].endswith("/api/sources/test-connection"))
    assert test_request["payload"] == {
        "type": "sharepoint",
        "source_id": "src-sharepoint",
        "config": {
            "auth_type": "managed-identity",
            "site_id": "contoso.sharepoint.com,site,web",
            "drive_id": "drive-id",
            "folder_path": "Policies/Published",
            "poll_interval_seconds": 120,
            "file_types": ["txt", "pdf", "docx"],
            "max_file_size_bytes": 25 * 1024 * 1024,
        },
    }


def test_all_connector_detail_pages_show_type_specific_fields(ui_page):
    page, _ = ui_page
    page.evaluate(
        """
        sources = [
            {id:"src-blob",name:"Blob",type:"azure-blob",enabled:true,config:{account_url:"https://a.blob.core.windows.net",container:"docs"}},
            {id:"src-sp",name:"SharePoint",type:"sharepoint",enabled:true,config:{site_id:"site",drive_id:"drive"}},
            {id:"src-lake",name:"OneLake",type:"onelake-iceberg",enabled:true,config:{warehouse:"workspace/lakehouse",namespace:["dbo"],table:"documents",content_fields:["title","body"],id_field:"doc_id",poll_interval_seconds:120}},
            {id:"src-cosmos",name:"Cosmos",type:"cosmosdb",enabled:true,config:{endpoint:"https://acct.documents.azure.com",database:"db",container:"docs"}},
            {id:"src-pg",name:"Postgres",type:"postgresql",enabled:true,config:{host:"pg.example",database:"db",table:"docs",user:"reader"}}
        ];
        destinations = [
            {id:"dst-cosmos",name:"Cosmos vectors",type:"cosmosdb-vector",enabled:true,config:{endpoint:"https://acct.documents.azure.com",database:"db",container:"vectors"}},
            {id:"dst-pg",name:"pgvector",type:"pgvector",enabled:true,config:{host:"pg.example",database:"db",table:"vectors",user:"writer"}},
            {id:"dst-lake",name:"OneLake output",type:"onelake-iceberg",enabled:true,config:{workspace_id:"workspace",lakehouse_item_id:"lakehouse",spark_job_definition_item_id:"job",staging_file_system:"workspace",staging_path:"lakehouse/Files/staging",target_table:"dbo.documents",mirror:{type:"garnet",config:{endpoint:"garnet:6380",vector_set:"vectors",tls:true}}}}
        ];
        """
    )

    source_expectations = {
        "src-blob": (["#detail-account-url", "#detail-container"], "Storage Blob Data Reader"),
        "src-sp": (["#detail-sp-site-id", "#detail-sp-drive-id"], "Sites.Selected"),
        "src-lake": ([], "Fabric workspace"),
        "src-cosmos": (["#detail-endpoint", "#detail-database", "#detail-container"], "Cosmos DB Built-in Data Reader"),
        "src-pg": (["#detail-pg-host", "#detail-pg-database", "#detail-pg-table"], "PostgreSQL Credentials"),
    }
    for source_id, (selectors, auth_text) in source_expectations.items():
        page.evaluate(f'showSourceDetail("{source_id}")')
        page.locator("#source-detail-modal").wait_for(state="visible")
        for selector in selectors:
            page.locator(selector).wait_for(state="visible")
        if source_id == "src-lake":
            general = page.locator("#source-detail-config").inner_text()
            for label in ["Warehouse", "Namespace", "Table", "Content Fields", "ID Field", "Poll Interval"]:
                assert label in general
        page.click('#source-detail-modal [data-tab="auth"]')
        assert auth_text in page.locator("#source-detail-config").inner_text()
        page.evaluate("sourceDetailDirty = false; closeSourceDetail()")

    destination_expectations = {
        "dst-cosmos": ["#dest-detail-endpoint", "#dest-detail-database", "#dest-detail-container"],
        "dst-pg": ["#dest-detail-pg-host", "#dest-detail-pg-database", "#dest-detail-pg-table"],
        "dst-lake": [],
    }
    for destination_id, selectors in destination_expectations.items():
        page.evaluate("closeModal('source-detail-modal'); closeModal('destination-detail-modal')")
        page.evaluate(f'showDestinationDetail("{destination_id}")')
        page.locator("#destination-detail-modal").wait_for(state="visible")
        for selector in selectors:
            page.locator(selector).wait_for(state="visible")
        if destination_id == "dst-lake":
            general = page.locator("#destination-detail-config").inner_text()
            for label in [
                "Workspace ID",
                "Lakehouse Item ID",
                "Spark Job Definition Item ID",
                "Staging File System",
                "Staging Path",
                "Target Table",
                "Garnet Endpoint",
                "Garnet Vector Set",
            ]:
                assert label in general
            page.click('#destination-detail-modal [data-tab="auth"]')
            assert "Fabric workspace" in page.locator("#destination-detail-config").inner_text()
            page.click('#destination-detail-modal [data-tab="vector"]')
            assert "Garnet Vector Mirror" in page.locator("#destination-detail-config").inner_text()
        page.evaluate("destDetailDirty = false; closeDestinationDetail()")
