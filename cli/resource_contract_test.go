package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"sort"
	"testing"
)

type connectorContractEntry struct {
	RequiredFields []string `json:"required_fields"`
}

type resourceConnectorContract struct {
	Sources      map[string]connectorContractEntry `json:"sources"`
	Destinations map[string]connectorContractEntry `json:"destinations"`
}

func loadResourceConnectorContract(t *testing.T) resourceConnectorContract {
	t.Helper()
	data, err := os.ReadFile("../contracts/resource_connectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract resourceConnectorContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	return contract
}

func normalizeConnectorFields(fields map[string][]string) map[string][]string {
	normalized := make(map[string][]string, len(fields))
	for connectorType, required := range fields {
		copyOfRequired := append([]string(nil), required...)
		sort.Strings(copyOfRequired)
		normalized[connectorType] = copyOfRequired
	}
	return normalized
}

func contractFields(entries map[string]connectorContractEntry) map[string][]string {
	fields := make(map[string][]string, len(entries))
	for connectorType, entry := range entries {
		fields[connectorType] = entry.RequiredFields
	}
	return normalizeConnectorFields(fields)
}

func TestCLIConnectorContractMatchesPortalContract(t *testing.T) {
	contract := loadResourceConnectorContract(t)
	if got, want := normalizeConnectorFields(sourceConnectorFields), contractFields(contract.Sources); !reflect.DeepEqual(got, want) {
		t.Fatalf("source connector contract drift:\nCLI: %#v\ncontract: %#v", got, want)
	}
	if got, want := normalizeConnectorFields(destinationConnectorFields), contractFields(contract.Destinations); !reflect.DeepEqual(got, want) {
		t.Fatalf("destination connector contract drift:\nCLI: %#v\ncontract: %#v", got, want)
	}
}

func TestValidateConnectorConfig(t *testing.T) {
	valid := map[string]any{"site_id": "site", "drive_id": "drive"}
	if err := validateConnectorConfig("source", "sharepoint", valid); err != nil {
		t.Fatalf("valid SharePoint config rejected: %v", err)
	}
	if err := validateConnectorConfig("source", "sharepoint", map[string]any{"site_id": "site"}); err == nil {
		t.Fatal("missing drive_id was accepted")
	}
	if err := validateConnectorConfig("destination", "unknown", map[string]any{}); err == nil {
		t.Fatal("unsupported destination type was accepted")
	}
}

func TestSourceAndDestinationCreatePayloadsMatchPortalContract(t *testing.T) {
	var requests []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		requests = append(requests, payload)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/api/sources" {
			json.NewEncoder(w).Encode(map[string]any{"source": map[string]any{"id": "src-test"}})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"destination": map[string]any{"id": "dst-test"}})
	}))
	defer server.Close()

	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	source := newSourceCreateCmd()
	source.SetArgs([]string{
		"--name=SharePoint policies",
		"--type=sharepoint",
		`--config={"site_id":"contoso.sharepoint.com,site,web","drive_id":"drive-id"}`,
	})
	if err := source.Execute(); err != nil {
		t.Fatal(err)
	}

	destination := newDestCreateCmd()
	destination.SetArgs([]string{
		"--name=Cosmos vectors",
		"--type=cosmosdb-vector",
		`--config={"endpoint":"https://example.documents.azure.com","database":"vectors","container":"embeddings"}`,
	})
	if err := destination.Execute(); err != nil {
		t.Fatal(err)
	}

	if len(requests) != 2 {
		t.Fatalf("expected two API requests, got %d", len(requests))
	}
	if requests[0]["type"] != "sharepoint" || requests[0]["name"] != "SharePoint policies" {
		t.Fatalf("unexpected source payload: %#v", requests[0])
	}
	sourceConfig := requests[0]["config"].(map[string]any)
	if sourceConfig["site_id"] != "contoso.sharepoint.com,site,web" || sourceConfig["drive_id"] != "drive-id" {
		t.Fatalf("unexpected source config: %#v", sourceConfig)
	}
	if requests[1]["type"] != "cosmosdb-vector" || requests[1]["name"] != "Cosmos vectors" {
		t.Fatalf("unexpected destination payload: %#v", requests[1])
	}
	destinationConfig := requests[1]["config"].(map[string]any)
	if destinationConfig["database"] != "vectors" || destinationConfig["container"] != "embeddings" {
		t.Fatalf("unexpected destination config: %#v", destinationConfig)
	}
}
