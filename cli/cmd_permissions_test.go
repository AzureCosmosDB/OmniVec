package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPermissionCommandsBothResourceKinds(t *testing.T) {
	for _, kind := range []string{"source", "destination"} {
		t.Run(kind, func(t *testing.T) {
			var request map[string]any
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/permissions/check" || r.Method != http.MethodPost {
					t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
				}
				json.NewDecoder(r.Body).Decode(&request)
				json.NewEncoder(w).Encode(map[string]any{
					"status": "access_denied", "summary": "The service denied access.",
					"commands": map[string]string{"bash": "az exact-scoped-command"},
					"missing":  []string{}, "limitations": []string{"Writes not verified"},
				})
			}))
			defer server.Close()
			oldServer, oldToken, oldOutput := flagServer, flagToken, flagOutput
			flagServer, flagToken, flagOutput = server.URL, "test", "table"
			defer func() { flagServer, flagToken, flagOutput = oldServer, oldToken, oldOutput }()
			cmd := newPermissionsCmd(kind)
			var out bytes.Buffer
			cmd.SetOut(&out)
			cmd.SetErr(&out)
			cmd.SetArgs([]string{"sample", "--resource-id=/actual/account", "--principal-id=object-id", "--shell=bash"})
			if err := cmd.Execute(); err == nil {
				t.Fatal("denied check returned success")
			}
			if request["kind"] != kind || request["resource_id"] != "/actual/account" || request["principal_id"] != "object-id" {
				t.Fatalf("wrong request: %#v", request)
			}
			if !strings.Contains(out.String(), "az exact-scoped-command") || !strings.Contains(out.String(), "Writes not verified") {
				t.Fatalf("missing guidance: %s", out.String())
			}
		})
	}
}

func TestPermissionCommandRejectsUnknownShell(t *testing.T) {
	cmd := newPermissionsCmd("source")
	cmd.SetArgs([]string{"sample", "--shell=cmd"})
	if err := cmd.Execute(); err == nil {
		t.Fatal("invalid shell accepted")
	}
}
