package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestCloudPlanApproveAndPermissions(t *testing.T) {
	var mu sync.Mutex
	job := map[string]any{
		"id": "deploy-test", "kind": "mcp", "status": "awaiting_approval", "plan_hash": "hash",
		"plan": map[string]any{"deployer_permissions": []any{map[string]any{
			"role": "Contributor", "scope": "/subscriptions/sub/resourceGroups/rg",
			"commands": map[string]any{"powershell": "az role assignment create --role Contributor"},
		}}},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/cloud-deployments":
			json.NewEncoder(w).Encode(map[string]any{"capabilities": map[string]any{"enabled": true}, "jobs": []any{job}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/cloud-deployments/deploy-test/approve":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			if body["plan_hash"] != "hash" || body["approve_cost_and_permissions"] != true {
				t.Errorf("unexpected approval body: %#v", body)
			}
			job["status"] = "succeeded"
			json.NewEncoder(w).Encode(job)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	permissions := newCloudPermissionsCmd()
	var output bytes.Buffer
	permissions.SetOut(&output)
	permissions.SetArgs([]string{"deploy-test"})
	if err := permissions.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "az role assignment create") {
		t.Fatalf("permission command not printed: %s", output.String())
	}

	approve := newCloudApproveCmd()
	approve.SetArgs([]string{"deploy-test", "--yes", "--wait", "--timeout=1s"})
	if err := approve.Execute(); err != nil {
		t.Fatal(err)
	}
}

func TestSharePointFoundryDemoOrchestration(t *testing.T) {
	var mu sync.Mutex
	jobs := map[string]map[string]any{}
	var planKinds []string
	var searchBody map[string]any
	var syncBody map[string]any
	runCalled := false
	next := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources":
			json.NewEncoder(w).Encode(map[string]any{"source": map[string]any{"id": "src-demo"}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources/src-demo/test":
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodGet && r.URL.Path == "/api/destinations/dst-demo":
			json.NewEncoder(w).Encode(map[string]any{"id": "dst-demo"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/destinations/dst-demo/test":
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodPost && r.URL.Path == "/api/pipelines":
			json.NewEncoder(w).Encode(map[string]any{"pipeline": map[string]any{"id": "pip-demo"}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/pipelines/pip-demo/run":
			runCalled = true
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources/src-demo/sync":
			json.NewDecoder(r.Body).Decode(&syncBody)
			json.NewEncoder(w).Encode(map[string]any{"operation_id": "sync-demo"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/playground/search":
			json.NewDecoder(r.Body).Decode(&searchBody)
			json.NewEncoder(w).Encode(map[string]any{"results": []any{map[string]any{"content": "NORTHSTAR-DEMO-TRAVEL-2026"}}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/cloud-deployments/plan":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			kind, _ := body["kind"].(string)
			planKinds = append(planKinds, kind)
			next++
			id := "deploy-" + kind
			result := map[string]any{}
			if kind == "verification" {
				result = map[string]any{
					"answer":            "47 USD; receipts over 18 USD; submit within 12 calendar days.",
					"source_references": []any{map[string]any{"source_ref": "01-travel-policy.txt"}},
				}
			}
			jobs[id] = map[string]any{
				"id": id, "kind": kind, "status": "awaiting_approval",
				"plan_hash": "hash-" + string(rune('0'+next)), "result": result,
			}
			json.NewEncoder(w).Encode(jobs[id])
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/approve"):
			id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/cloud-deployments/"), "/approve")
			jobs[id]["status"] = "succeeded"
			json.NewEncoder(w).Encode(jobs[id])
		case r.Method == http.MethodGet && r.URL.Path == "/api/cloud-deployments":
			all := []any{}
			for _, job := range jobs {
				all = append(all, job)
			}
			json.NewEncoder(w).Encode(map[string]any{"capabilities": map[string]any{"enabled": true}, "jobs": all})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	cmd := newSharePointFoundryDemoCmd()
	var output bytes.Buffer
	cmd.SetOut(&output)
	cmd.SetArgs([]string{
		"--site-id=site", "--drive-id=drive",
		"--sharepoint-tenant-id=11111111-1111-1111-1111-111111111111",
		"--sharepoint-client-id=22222222-2222-2222-2222-222222222222",
		"--destination=dst-demo", "--embedding-model=model-demo",
		"--resource-group-id=/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/demo",
		"--cosmos-account-id=/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/demo/providers/Microsoft.DocumentDB/databaseAccounts/cosmos",
		"--embedding-account-id=/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/openai",
		"--project-resource-id=/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/foundry/projects/demo",
		"--chat-deployment=gpt-4.1", "--timeout=2s", "--approve",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if strings.Join(planKinds, ",") != "mcp,foundry,verification" {
		t.Fatalf("unexpected plan order: %v", planKinds)
	}
	if !runCalled || syncBody["full_sync"] != true || syncBody["minimum_documents"] != float64(1) {
		t.Fatalf("pipeline activation or sync payload missing: run=%v sync=%#v", runCalled, syncBody)
	}
	if searchBody["source_id"] != "src-demo" || searchBody["pipeline_id"] != "pip-demo" {
		t.Fatalf("search was not source/pipeline scoped: %#v", searchBody)
	}
	if !strings.Contains(output.String(), "47 USD") || !strings.Contains(output.String(), "01-travel-policy.txt") {
		t.Fatalf("answer or retrieval source missing: %s", output.String())
	}
}

func TestSourceFoundryDemoReusesDeploymentAndScopesSearch(t *testing.T) {
	var searchBody map[string]any
	var pipelineBody map[string]any
	verification := map[string]any{
		"id": "deploy-verification", "kind": "verification", "status": "awaiting_approval",
		"plan_hash": "hash-verification",
		"result": map[string]any{
			"answer":            "The answer is indigo.",
			"source_references": []any{map[string]any{"source_ref": "policy.txt"}},
		},
	}
	jobs := []any{
		map[string]any{
			"id": "deploy-foundry", "kind": "foundry", "status": "succeeded",
			"plan": map[string]any{"mcp_deployment_id": "deploy-mcp"},
		},
		map[string]any{
			"id": "deploy-mcp", "kind": "mcp", "status": "succeeded",
			"plan": map[string]any{"destination_id": "dst-demo"},
		},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/sources/src-existing":
			json.NewEncoder(w).Encode(map[string]any{
				"id": "src-existing", "type": "azure-blob", "enabled": true,
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources/src-existing/test":
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodGet && r.URL.Path == "/api/destinations/dst-demo":
			json.NewEncoder(w).Encode(map[string]any{"id": "dst-demo"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/destinations/dst-demo/test":
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodPost && r.URL.Path == "/api/pipelines":
			json.NewDecoder(r.Body).Decode(&pipelineBody)
			json.NewEncoder(w).Encode(map[string]any{"pipeline": map[string]any{"id": "pip-demo"}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/pipelines/pip-demo/run":
			json.NewEncoder(w).Encode(map[string]any{"success": true})
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources/src-existing/sync":
			json.NewEncoder(w).Encode(map[string]any{"operation_id": "sync-demo"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/playground/search":
			json.NewDecoder(r.Body).Decode(&searchBody)
			json.NewEncoder(w).Encode(map[string]any{
				"results": []any{map[string]any{"content": "AURORA-MARMOT-INDIGO"}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/cloud-deployments":
			all := append([]any{}, jobs...)
			all = append(all, verification)
			json.NewEncoder(w).Encode(map[string]any{
				"capabilities": map[string]any{"enabled": true}, "jobs": all,
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/cloud-deployments/plan":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			if body["kind"] != "verification" || body["foundry_deployment_id"] != "deploy-foundry" {
				t.Errorf("unexpected verification plan: %#v", body)
			}
			json.NewEncoder(w).Encode(verification)
		case r.Method == http.MethodPost && r.URL.Path == "/api/cloud-deployments/deploy-verification/approve":
			verification["status"] = "succeeded"
			json.NewEncoder(w).Encode(verification)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	cmd := newSourceFoundryDemoCmd()
	var output bytes.Buffer
	cmd.SetOut(&output)
	cmd.SetArgs([]string{
		"--source=src-existing", "--destination=dst-demo", "--pipeline-model=text-azure",
		"--marker=AURORA-MARMOT-INDIGO", "--question=What is the approval key?",
		"--foundry-deployment=deploy-foundry", "--timeout=2s", "--approve",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if searchBody["source_id"] != "src-existing" || searchBody["pipeline_id"] != "pip-demo" {
		t.Fatalf("search was not source/pipeline scoped: %#v", searchBody)
	}
	if pipelineBody["processing_mode"] != "queue" || pipelineBody["process_existing"] != true ||
		pipelineBody["doc_id_pattern"] != "{source_hash}-{pipeline}" {
		t.Fatalf("pipeline is not configured for asynchronous replay: %#v", pipelineBody)
	}
	if !strings.Contains(output.String(), "indigo") || !strings.Contains(output.String(), "Type=azure-blob") {
		t.Fatalf("answer or source type missing: %s", output.String())
	}
}

func TestSourceSyncWaitsForReadyOperation(t *testing.T) {
	var syncBody map[string]any
	statusChecks := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/sources/src-demo/sync":
			json.NewDecoder(r.Body).Decode(&syncBody)
			json.NewEncoder(w).Encode(map[string]any{"operation_id": "sync-demo"})
		case r.Method == http.MethodGet && r.URL.Path == "/api/source-syncs/sync-demo":
			statusChecks++
			json.NewEncoder(w).Encode(map[string]any{"status": "ready"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	cmd := newSourceSyncCmd()
	cmd.SetArgs([]string{
		"src-demo", "--full", "--wait", "--minimum-documents=10", "--timeout=1s",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if syncBody["full_sync"] != true || syncBody["minimum_documents"] != float64(10) {
		t.Fatalf("unexpected sync payload: %#v", syncBody)
	}
	if statusChecks != 1 {
		t.Fatalf("expected one readiness check, got %d", statusChecks)
	}
}

func TestWaitForCloudJobTimesOut(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"jobs": []any{map[string]any{"id": "deploy-test", "status": "running"}}})
	}))
	defer server.Close()
	_, err := waitForCloudJob(NewClient(server.URL, ""), "deploy-test", time.Millisecond)
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("expected timeout, got %v", err)
	}
}

func TestReusedFoundryDeploymentMustMatchDestination(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"jobs": []any{
			map[string]any{
				"id": "deploy-foundry", "kind": "foundry", "status": "succeeded",
				"plan": map[string]any{"mcp_deployment_id": "deploy-mcp"},
			},
			map[string]any{
				"id": "deploy-mcp", "kind": "mcp", "status": "succeeded",
				"plan": map[string]any{"destination_id": "dst-other"},
			},
		}})
	}))
	defer server.Close()
	c := NewClient(server.URL, "")
	foundry, err := requireSucceededCloudJob(c, "deploy-foundry", "foundry")
	if err != nil {
		t.Fatal(err)
	}
	if err := requireCloudJobDestination(c, foundry, "dst-demo"); err == nil || !strings.Contains(err.Error(), "dst-other") {
		t.Fatalf("expected destination mismatch, got %v", err)
	}
}
