package main

import (
	"fmt"
	"strings"
	"sync"

	"github.com/spf13/cobra"
)

var pipelineColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "NAME", Key: "name"},
	{Header: "STATUS", Key: "status"},
	{Header: "HEALTH", Key: "_health"},
	{Header: "DESTINATION", Key: "destination_id"},
	{Header: "PROCESSED", Key: "stats.documents_processed"},
	{Header: "SUCCESS", Key: "stats.jobs.completed"},
	{Header: "FAILED", Key: "stats.jobs.failed"},
	{Header: "UPDATED", Key: "updated_at"},
}

func newPipelineCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "pipeline",
		Aliases: []string{"pipelines", "pip"},
		Short:   "Manage ingestion pipelines",
	}
	cmd.AddCommand(
		newPipelineListCmd(),
		newPipelineShowCmd(),
		newPipelineCreateCmd(),
		newPipelineUpdateCmd(),
		newPipelineDeleteCmd(),
		newPipelinePauseCmd(),
		newPipelineResumeCmd(),
		newPipelineRunCmd(),
		newPipelineResetCmd(),
	)
	return cmd
}

func newPipelineListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all pipelines",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/pipelines", nil)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "pipelines")
			_, _, pipH, _ := fetchHealthMap(c)
			enrichHealth(items, pipH, "id")
			outputList(items, pipelineColumns)
			return nil
		},
	}
}

// healthResult holds the outcome of a single health check.
type healthResult struct {
	name    string
	status  string // "ok", "error"
	detail  string
}

func newPipelineShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <pipeline-id>",
		Short: "Show pipeline details, health checks, and stats",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/pipelines/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)

			// For json/yaml output, enrich with health and return
			if flagOutput != "table" {
				health := runPipelineHealth(c, obj)
				obj["health"] = health
				outputResult(obj, pipelineColumns)
				return nil
			}

			// ── Pipeline Info ──
			fmt.Println(bold("Pipeline"))
			fmt.Printf("  %-22s %s\n", "ID:", obj["id"])
			fmt.Printf("  %-22s %s\n", "Name:", obj["name"])
			fmt.Printf("  %-22s %s\n", "Status:", colorStatus(fmt.Sprintf("%v", obj["status"])))
			fmt.Printf("  %-22s %s\n", "Model:", obj["docgrok_pipeline"])
			fmt.Printf("  %-22s %s\n", "Destination:", obj["destination_id"])
			if sources, ok := obj["sources"].([]any); ok {
				ids := []string{}
				for _, s := range sources {
					if m, ok := s.(map[string]any); ok {
						ids = append(ids, fmt.Sprintf("%v", m["source_id"]))
					}
				}
				fmt.Printf("  %-22s %s\n", "Sources:", strings.Join(ids, ", "))
			}
			fmt.Printf("  %-22s %s\n", "Created:", relativeTime(fmt.Sprintf("%v", obj["created_at"])))
			fmt.Printf("  %-22s %s\n", "Updated:", relativeTime(fmt.Sprintf("%v", obj["updated_at"])))

			// ── Stats ──
			if stats, ok := obj["stats"].(map[string]any); ok {
				fmt.Println()
				fmt.Println(bold("Stats"))
				// Prefer embedded_count / lifetime_embedded_count (true "docs vectorized" measure)
				// over jobs.completed, which is 0 for queue/changefeed pipelines that
				// don't create one job document per source row.
				embedded := toInt(stats["embedded_count"])
				lifetime := toInt(stats["lifetime_embedded_count"])
				sourceCount := toInt(stats["source_doc_count"])
				if embedded > 0 || lifetime > 0 || sourceCount > 0 {
					fmt.Printf("  %-22s %s\n", "Documents Embedded:", green(fmt.Sprintf("%d", embedded)))
					if lifetime > embedded {
						fmt.Printf("  %-22s %d\n", "Lifetime Embedded:", lifetime)
					}
					if sourceCount > 0 {
						fmt.Printf("  %-22s %d\n", "Source Docs:", sourceCount)
					}
					if pct, ok := stats["completion_pct"].(float64); ok && pct > 0 {
						fmt.Printf("  %-22s %.1f%%\n", "Completion:", pct)
					}
				}
				if jobs, ok := stats["jobs"].(map[string]any); ok {
					total := toInt(jobs["total"])
					completed := toInt(jobs["completed"])
					failed := toInt(jobs["failed"])
					pending := toInt(jobs["pending"])
					processing := toInt(jobs["processing"])
					if embedded == 0 && lifetime == 0 && sourceCount == 0 {
						fmt.Printf("  %-22s %s\n", "Documents Processed:", green(fmt.Sprintf("%d", completed)))
					}
					if failed > 0 {
						fmt.Printf("  %-22s %s\n", "Failed:", red(fmt.Sprintf("%d", failed)))
					} else {
						fmt.Printf("  %-22s %s\n", "Failed:", fmt.Sprintf("%d", failed))
					}
					fmt.Printf("  %-22s %d\n", "Pending:", pending)
					fmt.Printf("  %-22s %d\n", "Processing:", processing)
					fmt.Printf("  %-22s %d\n", "Total Jobs:", total)
				}
				if avg, ok := stats["avg_processing_time_ms"].(float64); ok && avg > 0 {
					fmt.Printf("  %-22s %.0fms\n", "Avg Processing Time:", avg)
				}
				if lastRun, ok := stats["last_run"].(string); ok && lastRun != "" {
					fmt.Printf("  %-22s %s\n", "Last Run:", relativeTime(lastRun))
				}
			}

			// ── Health Checks ──
			fmt.Println()
			fmt.Println(bold("Health Checks"))
			checks := runPipelineHealth(c, obj)
			for _, ch := range checks {
				mark := green("✓")
				detail := green(ch["detail"].(string))
				if ch["status"] == "error" {
					mark = red("✗")
					detail = red(ch["detail"].(string))
				}
				fmt.Printf("  %s %-22s %s\n", mark, ch["name"], detail)
			}

			// ── Recent Errors ──
			errJobs := getRecentErrors(c, id)
			if len(errJobs) > 0 {
				fmt.Println()
				fmt.Println(bold("Recent Errors"))
				for _, j := range errJobs {
					jobID, _ := j["id"].(string)
					sourceRef, _ := j["source_ref"].(string)
					errMsg, _ := j["error"].(string)
					created, _ := j["created_at"].(string)
					if len(errMsg) > 120 {
						errMsg = errMsg[:120] + "..."
					}
					fmt.Printf("  %s  %s  %s\n", red(jobID), dim(relativeTime(created)), sourceRef)
					fmt.Printf("    %s\n", red(errMsg))
				}
			}

			return nil
		},
	}
}

// runPipelineHealth runs all health checks in parallel and returns results.
func runPipelineHealth(c *Client, pipeline map[string]any) []map[string]any {
	var mu sync.Mutex
	var results []healthResult
	var wg sync.WaitGroup

	// 1. Source connections
	if sources, ok := pipeline["sources"].([]any); ok {
		for _, s := range sources {
			sm, ok := s.(map[string]any)
			if !ok {
				continue
			}
			srcID, _ := sm["source_id"].(string)
			wg.Add(1)
			go func(id string) {
				defer wg.Done()
				r := healthResult{name: fmt.Sprintf("Source (%s)", id)}
				resp, err := c.Post(fmt.Sprintf("/api/sources/%s/test", id), nil)
				if err != nil {
					r.status = "error"
					r.detail = fmt.Sprintf("%v", err)
				} else {
					obj := parseJSONObject(resp)
					if success, _ := obj["success"].(bool); success {
						r.status = "ok"
						r.detail = "connected"
					} else {
						r.status = "error"
						r.detail = fmt.Sprintf("%v", obj["error"])
					}
				}
				mu.Lock()
				results = append(results, r)
				mu.Unlock()
			}(srcID)
		}
	}

	// 2. Destination connection
	if dstID, ok := pipeline["destination_id"].(string); ok && dstID != "" {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := healthResult{name: fmt.Sprintf("Destination (%s)", dstID)}
			resp, err := c.Post(fmt.Sprintf("/api/destinations/%s/test", dstID), nil)
			if err != nil {
				r.status = "error"
				r.detail = fmt.Sprintf("%v", err)
			} else {
				obj := parseJSONObject(resp)
				if success, _ := obj["success"].(bool); success {
					r.status = "ok"
					r.detail = "connected"
				} else {
					r.status = "error"
					r.detail = fmt.Sprintf("%v", obj["error"])
				}
			}
			mu.Lock()
			results = append(results, r)
			mu.Unlock()
		}()
	}

	// 3. DocGrok / model health
	wg.Add(1)
	go func() {
		defer wg.Done()
		pipelineName, _ := pipeline["docgrok_pipeline"].(string)
		r := healthResult{name: fmt.Sprintf("Model (%s)", pipelineName)}
		resp, err := c.Get("/api/docgrok/health", nil)
		if err != nil {
			r.status = "error"
			r.detail = fmt.Sprintf("%v", err)
			mu.Lock()
			results = append(results, r)
			mu.Unlock()
			return
		}
		health := parseJSONObject(resp)

		// Check if the pipeline uses an external provider (name contains provider prefix)
		isExternal := false
		if extProviders, ok := health["external_providers"].(map[string]any); ok {
			for providerName, pv := range extProviders {
				if strings.Contains(strings.ToLower(pipelineName), strings.ToLower(providerName)) {
					isExternal = true
					if pm, ok := pv.(map[string]any); ok {
						st, _ := pm["status"].(string)
						if st == "configured" {
							r.status = "ok"
							r.detail = fmt.Sprintf("external provider %s: configured", providerName)
						} else {
							r.status = "error"
							r.detail = fmt.Sprintf("external provider %s: %s", providerName, st)
						}
					}
					break
				}
			}
		}

		if !isExternal {
			// Local model — check backends
			if backends, ok := health["backends"].(map[string]any); ok {
				// Find matching backend
				found := false
				for backendName, bv := range backends {
					if strings.Contains(strings.ToLower(pipelineName), strings.ToLower(backendName)) {
						found = true
						if bm, ok := bv.(map[string]any); ok {
							st, _ := bm["status"].(string)
							if st == "healthy" {
								gpu, _ := bm["gpu_name"].(string)
								r.status = "ok"
								r.detail = fmt.Sprintf("healthy (%s)", gpu)
							} else {
								r.status = "error"
								r.detail = st
							}
						}
						break
					}
				}
				if !found {
					svcStatus, _ := health["status"].(string)
					if svcStatus == "healthy" {
						r.status = "ok"
						r.detail = "DocGrok service healthy"
					} else {
						r.status = "error"
						r.detail = fmt.Sprintf("DocGrok: %s", svcStatus)
					}
				}
			}
		}

		// If pipeline uses an external model, also test the connection
		if isExternal {
			testResp, testErr := c.Get(fmt.Sprintf("/api/docgrok/pipelines/%s", pipelineName), nil)
			if testErr == nil {
				pipeObj := parseJSONObject(testResp)
				if provider, ok := pipeObj["provider"].(string); ok && provider != "" {
					r.detail += fmt.Sprintf(", pipeline: %s", pipelineName)
				}
			}
		}

		mu.Lock()
		results = append(results, r)
		mu.Unlock()
	}()

	// 4. Trigger / event bus status (only relevant in queue mode)
	procMode, _ := pipeline["processing_mode"].(string)
	if procMode != "" && procMode != "queue" {
		mu.Lock()
		results = append(results, healthResult{
			name:   "Triggers / Event Bus",
			status: "ok",
			detail: fmt.Sprintf("not applicable in %s mode", procMode),
		})
		mu.Unlock()
	} else {
	wg.Add(1)
	go func() {
		defer wg.Done()
		r := healthResult{name: "Triggers / Event Bus"}
		resp, err := c.Get("/api/triggers/status", nil)
		if err != nil {
			r.status = "error"
			r.detail = fmt.Sprintf("%v", err)
			mu.Lock()
			results = append(results, r)
			mu.Unlock()
			return
		}
		triggerStatus := parseJSONObject(resp)

		// Check which sources in this pipeline have triggers
		pipSources := map[string]bool{}
		if sources, ok := pipeline["sources"].([]any); ok {
			for _, s := range sources {
				if sm, ok := s.(map[string]any); ok {
					if sid, ok := sm["source_id"].(string); ok {
						pipSources[sid] = true
					}
				}
			}
		}

		details := []string{}
		// Check blob sources
		if blobs, ok := triggerStatus["blob_sources"].([]any); ok {
			for _, b := range blobs {
				bm, ok := b.(map[string]any)
				if !ok {
					continue
				}
				id, _ := bm["id"].(string)
				if !pipSources[id] {
					continue
				}
				st, _ := bm["status"].(string)
				details = append(details, fmt.Sprintf("eventgrid(%s): %s", id, st))
			}
		}
		// Check cosmosdb sources
		if cosmos, ok := triggerStatus["cosmosdb_sources"].([]any); ok {
			for _, cs := range cosmos {
				cm, ok := cs.(map[string]any)
				if !ok {
					continue
				}
				id, _ := cm["id"].(string)
				if !pipSources[id] {
					continue
				}
				st, _ := cm["status"].(string)
				details = append(details, fmt.Sprintf("change_feed(%s): %s", id, st))
			}
		}

		// Queue size
		if qs, ok := triggerStatus["event_queue_size"].(float64); ok {
			details = append(details, fmt.Sprintf("queue_size: %d", int(qs)))
		}

		if len(details) == 0 {
			r.status = "ok"
			r.detail = "no triggers for this pipeline"
		} else {
			r.status = "ok"
			r.detail = strings.Join(details, ", ")
			// Mark error if any source is "not_configured"
			for _, d := range details {
				if strings.Contains(d, "not_configured") {
					r.status = "error"
					break
				}
			}
		}

		mu.Lock()
		results = append(results, r)
		mu.Unlock()
	}()
	}

	wg.Wait()

	// Convert to maps for json/yaml output
	out := make([]map[string]any, len(results))
	for i, r := range results {
		out[i] = map[string]any{
			"name":   r.name,
			"status": r.status,
			"detail": r.detail,
		}
	}
	return out
}

// getRecentErrors fetches recent failed jobs for a pipeline.
func getRecentErrors(c *Client, pipelineID string) []map[string]any {
	data, err := c.Get("/api/jobs", map[string]string{
		"pipeline_id": pipelineID,
		"status":      "failed",
		"limit":       "5",
	})
	if err != nil {
		return nil
	}
	return parseJSONList(data, "jobs")
}

// toInt converts a JSON number (float64) to int.
func toInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	default:
		return 0
	}
}

func newPipelineCreateCmd() *cobra.Command {
	var name, description, source, destination, model, contentFields, vectorIndexPath string
	var contentMode, contentStrategy, fileTypes, docIdPattern string
	var processingMode, embeddingField, storeContent, metadataFields, contentField string
	var chunkSize, chunkOverlap int
	var processExisting bool
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a new pipeline",
		RunE: func(cmd *cobra.Command, args []string) error {
			if name == "" {
				exitErr("--name is required")
			}
			if source == "" {
				exitErr("--source is required")
			}
			if destination == "" {
				exitErr("--destination is required")
			}
			if model == "" {
				exitErr("--model is required (DocGrok pipeline name)")
			}
			srcID := ensurePrefix(source, "src-")
			dstID := ensurePrefix(destination, "dst-")

			// Parse content fields
			fields := []string{"content"}
			if contentFields != "" {
				fields = strings.Split(contentFields, ",")
				for i := range fields {
					fields[i] = strings.TrimSpace(fields[i])
				}
			}

			// Build pipeline source entry
			srcEntry := map[string]any{
				"source_id":      srcID,
				"filters":        map[string]any{},
				"content_fields": fields,
			}
			if contentMode != "" {
				srcEntry["content_mode"] = contentMode
			}
			if fileTypes != "" {
				ft := strings.Split(fileTypes, ",")
				for i := range ft {
					ft[i] = strings.TrimSpace(ft[i])
				}
				srcEntry["file_types"] = ft
			}

			body := map[string]any{
				"name": name,
				"sources": []map[string]any{srcEntry},
				"destination_id":     dstID,
				"docgrok_pipeline":   model,
				"vector_index_path":  vectorIndexPath,
				"process_existing":   processExisting,
			}
			if description != "" {
				body["description"] = description
			}
			if processingMode != "" {
				body["processing_mode"] = processingMode
			}
			if embeddingField != "" {
				body["embedding_field"] = embeddingField
			}
			if contentStrategy != "" {
				body["content_strategy"] = contentStrategy
			}
			if chunkSize > 0 {
				if body["chunk_config"] == nil {
					body["chunk_config"] = map[string]any{}
				}
				body["chunk_config"].(map[string]any)["chunk_size"] = chunkSize
			}
			if chunkOverlap > 0 {
				if body["chunk_config"] == nil {
					body["chunk_config"] = map[string]any{}
				}
				body["chunk_config"].(map[string]any)["chunk_overlap"] = chunkOverlap
			}
			if docIdPattern != "" {
				body["doc_id_pattern"] = docIdPattern
			}
			switch strings.ToLower(strings.TrimSpace(storeContent)) {
			case "":
				// unset → omit (server default = per-destination)
			case "true", "1", "yes":
				body["store_content"] = true
			case "false", "0", "no":
				body["store_content"] = false
			default:
				exitErr("--store-content must be true or false")
			}
			if cf := strings.TrimSpace(contentField); cf != "" {
				body["content_field"] = cf
			}
			if mf := strings.TrimSpace(metadataFields); mf != "" {
				switch strings.ToLower(mf) {
				case "all", "default":
					// unset → server default (write all optional fields)
				case "none":
					body["metadata_fields"] = []string{}
				default:
					parts := []string{}
					for _, f := range strings.Split(mf, ",") {
						if t := strings.TrimSpace(f); t != "" {
							parts = append(parts, t)
						}
					}
					body["metadata_fields"] = parts
				}
			}
			c := getClient()
			data, err := c.Post("/api/pipelines", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if pip, ok := resp["pipeline"].(map[string]any); ok {
				exitOK("Pipeline created: %s (status: %s)", pip["id"], pip["status"])
				if flagOutput != "table" {
					outputResult(pip, pipelineColumns)
				}
			} else {
				exitOK("Pipeline created")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Pipeline name")
	cmd.Flags().StringVar(&description, "description", "", "Pipeline description")
	cmd.Flags().StringVar(&source, "source", "", "Source ID")
	cmd.Flags().StringVar(&destination, "destination", "", "Destination ID")
	cmd.Flags().StringVar(&model, "model", "", "DocGrok pipeline or model name")
	cmd.Flags().StringVar(&contentFields, "content-fields", "", "Comma-separated content field names (default: content)")
	cmd.Flags().StringVar(&contentMode, "content-mode", "", "Content extraction mode: field, blob_url, http_url, s3_url")
	cmd.Flags().StringVar(&fileTypes, "file-types", "", "Comma-separated file type filters (e.g., txt,pdf,md)")
	cmd.Flags().StringVar(&contentStrategy, "content-strategy", "", "Content strategy: truncate or chunk")
	cmd.Flags().IntVar(&chunkSize, "chunk-size", 0, "Chunk size in characters (used with --content-strategy=chunk)")
	cmd.Flags().IntVar(&chunkOverlap, "chunk-overlap", 0, "Chunk overlap in characters")
	cmd.Flags().StringVar(&docIdPattern, "doc-id-pattern", "", "Document ID pattern (e.g., {source}-chunk-{chunk})")
	cmd.Flags().StringVar(&vectorIndexPath, "vector-index-path", "", "Vector index path from destination's vector policy (required)")
	cmd.Flags().StringVar(&processingMode, "processing-mode", "", "Processing mode: inline or queue (default queue)")
	cmd.Flags().StringVar(&embeddingField, "embedding-field", "", "Destination field to write embedding into (default: embedding)")
	cmd.Flags().BoolVar(&processExisting, "process-existing", true, "Process existing documents on creation")
	cmd.Flags().StringVar(&storeContent, "store-content", "", "Persist embedded text on destination doc: true|false (default: per-destination — Postgres/MsSql=true, Cosmos=false)")
	cmd.Flags().StringVar(&contentField, "content-field", "", "Destination field name receiving the embedded text (Cosmos only; default: content)")
	cmd.Flags().StringVar(&metadataFields, "metadata-fields", "", "Optional metadata to write on dest docs: 'all' (default), 'none', or comma list (allowed: pipeline_name, embedding_dims, source_ref)")
	return cmd
}

func newPipelineUpdateCmd() *cobra.Command {
	var name, description, destination, model string
	var contentFields, contentStrategy, docIdPattern, vectorIndexPath, storeContent, contentField string
	var metadataFields string
	var chunkSize, chunkOverlap int
	cmd := &cobra.Command{
		Use:   "update <pipeline-id>",
		Short: "Update a pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			c := getClient()
			existing, err := c.Get(fmt.Sprintf("/api/pipelines/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			body := parseJSONObject(existing)
			delete(body, "id")
			delete(body, "created_at")
			delete(body, "updated_at")
			delete(body, "status")
			delete(body, "stats")
			if name != "" {
				body["name"] = name
			}
			if description != "" {
				body["description"] = description
			}
			if destination != "" {
				body["destination_id"] = ensurePrefix(destination, "dst-")
			}
			if model != "" {
				body["docgrok_pipeline"] = model
			}
			if contentStrategy != "" {
				body["content_strategy"] = contentStrategy
			}
			if vectorIndexPath != "" {
				body["vector_index_path"] = vectorIndexPath
			}
			if docIdPattern != "" {
				body["doc_id_pattern"] = docIdPattern
			}
			if contentFields != "" {
				fields := strings.Split(contentFields, ",")
				for i := range fields {
					fields[i] = strings.TrimSpace(fields[i])
				}
				if srcs, ok := body["sources"].([]any); ok && len(srcs) > 0 {
					if s, ok := srcs[0].(map[string]any); ok {
						s["content_fields"] = fields
					}
				}
			}
			if chunkSize > 0 || chunkOverlap > 0 {
				cc := map[string]any{}
				if existing, ok := body["chunk_config"].(map[string]any); ok {
					cc = existing
				}
				if chunkSize > 0 {
					cc["chunk_size"] = chunkSize
				}
				if chunkOverlap > 0 {
					cc["chunk_overlap"] = chunkOverlap
				}
				body["chunk_config"] = cc
			}
			switch strings.ToLower(strings.TrimSpace(storeContent)) {
			case "":
				// unset → keep existing value
			case "true", "1", "yes":
				body["store_content"] = true
			case "false", "0", "no":
				body["store_content"] = false
			default:
				exitErr("--store-content must be true or false")
			}
			if cf := strings.TrimSpace(contentField); cf != "" {
				body["content_field"] = cf
			}
			if mf := strings.TrimSpace(metadataFields); mf != "" {
				switch strings.ToLower(mf) {
				case "all", "default":
					body["metadata_fields"] = nil
				case "none":
					body["metadata_fields"] = []string{}
				default:
					parts := []string{}
					for _, f := range strings.Split(mf, ",") {
						if t := strings.TrimSpace(f); t != "" {
							parts = append(parts, t)
						}
					}
					body["metadata_fields"] = parts
				}
			}
			data, err := c.Put(fmt.Sprintf("/api/pipelines/%s", id), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if pip, ok := resp["pipeline"].(map[string]any); ok {
				exitOK("Pipeline updated: %s", pip["id"])
			} else {
				exitOK("Pipeline updated")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Pipeline name")
	cmd.Flags().StringVar(&description, "description", "", "Pipeline description")
	cmd.Flags().StringVar(&destination, "destination", "", "Destination ID")
	cmd.Flags().StringVar(&model, "model", "", "DocGrok pipeline name")
	cmd.Flags().StringVar(&contentFields, "content-fields", "", "Comma-separated content field names")
	cmd.Flags().StringVar(&contentStrategy, "content-strategy", "", "Content strategy: truncate or chunk")
	cmd.Flags().IntVar(&chunkSize, "chunk-size", 0, "Chunk size in characters")
	cmd.Flags().IntVar(&chunkOverlap, "chunk-overlap", 0, "Chunk overlap in characters")
	cmd.Flags().StringVar(&docIdPattern, "doc-id-pattern", "", "Document ID pattern")
	cmd.Flags().StringVar(&vectorIndexPath, "vector-index-path", "", "Vector index path")
	cmd.Flags().StringVar(&storeContent, "store-content", "", "Persist embedded text on destination doc: true|false")
	cmd.Flags().StringVar(&contentField, "content-field", "", "Destination field name receiving the embedded text (Cosmos only)")
	cmd.Flags().StringVar(&metadataFields, "metadata-fields", "", "Optional metadata to write on dest docs: 'all', 'none', or comma list (allowed: pipeline_name, embedding_dims, source_ref)")
	return cmd
}

func newPipelineDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <pipeline-id>",
		Short: "Delete a pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			if !yes && !confirmAction(fmt.Sprintf("Delete pipeline %s?", id)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/pipelines/%s", id))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Pipeline %s deleted", id)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newPipelinePauseCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "pause <pipeline-id>",
		Short: "Pause a pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/pipelines/%s/pause", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Pipeline %s paused", id)
			return nil
		},
	}
}

func newPipelineResumeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "resume <pipeline-id>",
		Short: "Resume a paused pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/pipelines/%s/resume", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Pipeline %s resumed", id)
			return nil
		},
	}
}

func newPipelineRunCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "run <pipeline-id>",
		Short: "Run/activate a pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/pipelines/%s/run", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if msg, ok := resp["message"].(string); ok {
				exitOK("%s", msg)
			} else {
				exitOK("Pipeline %s activated", id)
			}
			return nil
		},
	}
}

func newPipelineResetCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "reset <pipeline-id>",
		Short: "Reset a pipeline (clears all jobs and stats)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "pip-")
			if !yes && !confirmAction(fmt.Sprintf("Reset pipeline %s? This will clear all jobs and stats.", id)) {
				return nil
			}
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/pipelines/%s/reset", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if msg, ok := resp["message"].(string); ok {
				exitOK("%s", msg)
			} else {
				exitOK("Pipeline %s reset", id)
			}
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}
