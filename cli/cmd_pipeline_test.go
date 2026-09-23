package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestPipelineChunkCreateAndUpdatePayload(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			json.NewEncoder(w).Encode(payload)
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		json.NewEncoder(w).Encode(map[string]any{"pipeline": map[string]any{"id": "pip-test"}})
	}))
	defer server.Close()
	oldServer, oldToken := flagServer, flagToken
	flagServer, flagToken = server.URL, "offline-test-token"
	defer func() { flagServer, flagToken = oldServer, oldToken }()

	create := newPipelineCreateCmd()
	create.SetArgs([]string{"--name=test", "--source=test", "--destination=test", "--model=mdl-test",
		"--content-strategy=chunk", "--chunk-size=450", "--chunk-overlap=0", "--chunk-unit=tokens",
		"--store-text", "--text-field=passage", "--doc-id-pattern=custom-{source_hash}-{chunk}"})
	if err := create.Execute(); err != nil {
		t.Fatal(err)
	}
	cc := payload["chunk_config"].(map[string]any)
	if cc["chunk_overlap"] != float64(0) || cc["chunk_unit"] != "tokens" || cc["text_field"] != "passage" ||
		cc["store_text"] != true || cc["doc_id_pattern"] != "custom-{source_hash}-{chunk}" {
		t.Fatalf("incorrect create chunk payload: %#v", cc)
	}
	update := newPipelineUpdateCmd()
	update.SetArgs([]string{"test", "--chunk-size=500", "--chunk-overlap=0", "--chunk-doc-id-pattern=edited-{chunk}"})
	if err := update.Execute(); err != nil {
		t.Fatal(err)
	}
	cc = payload["chunk_config"].(map[string]any)
	if cc["chunk_size"] != float64(500) || cc["chunk_overlap"] != float64(0) ||
		cc["chunk_unit"] != "tokens" || cc["text_field"] != "passage" ||
		cc["store_text"] != true || cc["doc_id_pattern"] != "edited-{chunk}" {
		t.Fatalf("update failed to preserve configuration: %#v", cc)
	}
}

func TestPipelineChunkExplicitInvalidValuesReachAPI(t *testing.T) {
	cmd := newPipelineCreateCmd()
	if err := cmd.ParseFlags([]string{"--chunk-size=0", "--chunk-overlap=-1", "--store-text=false"}); err != nil {
		t.Fatal(err)
	}
	body := map[string]any{}
	applyPipelineChunkFlags(cmd, body)
	cc := body["chunk_config"].(map[string]any)
	if cc["chunk_size"] != 0 || cc["chunk_overlap"] != -1 || cc["store_text"] != false {
		t.Fatalf("explicit values silently dropped: %#v", cc)
	}
}
