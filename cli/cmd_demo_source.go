package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

type sourceFoundryOptions struct {
	source, destination, pipelineModel, embeddingModel string
	tenantID, clientID                                 string
	resourceGroupID, location                          string
	cosmosAccountID, embeddingAccountID                string
	projectResourceID, chatDeployment                  string
	mcpDeploymentID, foundryDeploymentID               string
	question, marker                                   string
	timeout                                            time.Duration
	approve                                            bool
}

func newSourceFoundryDemoCmd() *cobra.Command {
	var o sourceFoundryOptions
	cmd := &cobra.Command{
		Use:   "source-foundry",
		Short: "Ingest an existing source and verify a Foundry answer through MCP",
		Long: "Creates and activates a queue pipeline for an existing source, starts an asynchronous full sync, " +
			"waits for a source- and pipeline-scoped marker, then reuses or creates MCP and Foundry resources.",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if !o.approve {
				return fmt.Errorf("--approve is required because this command may provision Azure resources and runs billable inference")
			}
			return runSourceFoundryDemo(cmd, o)
		},
	}
	cmd.Flags().StringVar(&o.source, "source", "", "Existing source ID")
	cmd.Flags().StringVar(&o.destination, "destination", "", "Existing Cosmos vector destination ID or name")
	cmd.Flags().StringVar(&o.pipelineModel, "pipeline-model", "", "DocGrok pipeline or embedding model ID")
	cmd.Flags().StringVar(&o.tenantID, "sharepoint-tenant-id", "", "Optional SharePoint workload-identity tenant ID")
	cmd.Flags().StringVar(&o.clientID, "sharepoint-client-id", "", "Optional SharePoint workload-identity client ID")
	cmd.Flags().StringVar(&o.embeddingModel, "embedding-model", "", "Registered Azure OpenAI embedding model ID")
	cmd.Flags().StringVar(&o.resourceGroupID, "resource-group-id", "", "Full resource-group ARM ID for MCP hosting")
	cmd.Flags().StringVar(&o.location, "location", "eastus2", "Azure Flex Consumption region")
	cmd.Flags().StringVar(&o.cosmosAccountID, "cosmos-account-id", "", "Full Cosmos account ARM ID")
	cmd.Flags().StringVar(&o.embeddingAccountID, "embedding-account-id", "", "Full Azure OpenAI account ARM ID")
	cmd.Flags().StringVar(&o.projectResourceID, "project-resource-id", "", "Full existing Foundry project ARM ID")
	cmd.Flags().StringVar(&o.chatDeployment, "chat-deployment", "", "Existing Foundry chat deployment name")
	cmd.Flags().StringVar(&o.mcpDeploymentID, "mcp-deployment", "", "Reuse a succeeded OmniVec MCP deployment job")
	cmd.Flags().StringVar(&o.foundryDeploymentID, "foundry-deployment", "", "Reuse a succeeded OmniVec Foundry deployment job")
	cmd.Flags().StringVar(&o.marker, "marker", "", "Unique text expected in vector search")
	cmd.Flags().StringVar(&o.question, "question", "", "Question to ask the Foundry agent")
	cmd.Flags().DurationVar(&o.timeout, "timeout", 5*time.Minute, "Total end-to-end time budget")
	cmd.Flags().BoolVar(&o.approve, "approve", false, "Approve resource creation, listed permissions, and one inference")
	return cmd
}

func runSourceFoundryDemo(cmd *cobra.Command, o sourceFoundryOptions) error {
	required := map[string]string{
		"--source": o.source, "--destination": o.destination,
		"--pipeline-model": o.pipelineModel, "--marker": o.marker, "--question": o.question,
	}
	if (o.tenantID == "") != (o.clientID == "") {
		return fmt.Errorf("--sharepoint-tenant-id and --sharepoint-client-id must be supplied together")
	}
	if o.foundryDeploymentID == "" {
		required["--project-resource-id"] = o.projectResourceID
		required["--chat-deployment"] = o.chatDeployment
	}
	if o.foundryDeploymentID == "" && o.mcpDeploymentID == "" {
		required["--embedding-model"] = o.embeddingModel
		required["--resource-group-id"] = o.resourceGroupID
		required["--cosmos-account-id"] = o.cosmosAccountID
		required["--embedding-account-id"] = o.embeddingAccountID
	}
	if err := requireCloudFlags(required); err != nil {
		return err
	}

	started := time.Now()
	deadline := started.Add(o.timeout)
	remaining := func() (time.Duration, error) {
		left := time.Until(deadline)
		if left <= 0 {
			return 0, fmt.Errorf("demo time budget exhausted")
		}
		return left, nil
	}
	c := getClient()
	sourceID := ensurePrefix(o.source, "src-")
	dstID := resolveDestination(o.destination)

	cmd.Printf("[0s] Running source, destination, and deployment preflight\n")
	sourceRaw, err := c.Get("/api/sources/"+sourceID, nil)
	if err != nil {
		return fmt.Errorf("source preflight: %w", err)
	}
	source := parseJSONObject(sourceRaw)
	if sourceObject, ok := source["source"].(map[string]any); ok {
		source = sourceObject
	}
	if enabled, exists := source["enabled"].(bool); exists && !enabled {
		return fmt.Errorf("source %s is disabled", sourceID)
	}
	sourceType, _ := source["type"].(string)
	testRaw, err := c.Post("/api/sources/"+sourceID+"/test", nil)
	if err != nil {
		return fmt.Errorf("source connection preflight: %w", err)
	}
	if success, _ := parseJSONObject(testRaw)["success"].(bool); !success {
		return fmt.Errorf("source connection preflight failed for %s", sourceID)
	}
	if _, err := c.Get("/api/destinations/"+dstID, nil); err != nil {
		return fmt.Errorf("destination preflight: %w", err)
	}
	destinationTest, err := c.Post("/api/destinations/"+dstID+"/test", nil)
	if err != nil {
		return fmt.Errorf("destination connection preflight: %w", err)
	}
	if success, _ := parseJSONObject(destinationTest)["success"].(bool); !success {
		return fmt.Errorf("destination connection preflight failed for %s", dstID)
	}
	if o.foundryDeploymentID != "" {
		foundry, err := requireSucceededCloudJob(c, o.foundryDeploymentID, "foundry")
		if err != nil {
			return err
		}
		if err := requireCloudJobDestination(c, foundry, dstID); err != nil {
			return err
		}
	}

	cmd.Printf("[%s] Creating and activating ingestion pipeline\n", time.Since(started).Round(time.Second))
	pipelineSource := map[string]any{
		"source_id": sourceID, "filters": map[string]any{},
		"content_fields": []string{"content"}, "content_mode": "field",
		"file_types": []string{"txt", "json", "pdf", "docx", "md", "csv"},
	}
	if o.tenantID != "" {
		pipelineSource["sharepoint_identity"] = map[string]any{
			"tenant_id": o.tenantID, "client_id": o.clientID,
		}
	}
	suffix := fmt.Sprintf("%d", time.Now().Unix())
	pipelineRaw, err := c.Post("/api/pipelines", map[string]any{
		"name":           "Source Foundry Demo " + suffix,
		"sources":        []map[string]any{pipelineSource},
		"destination_id": dstID, "docgrok_pipeline": o.pipelineModel,
		"vector_index_path": "/embedding", "process_existing": true,
		"processing_mode": "queue", "store_content": true, "content_field": "content",
		"doc_id_pattern": "{source_hash}-{pipeline}",
	})
	if err != nil {
		return fmt.Errorf("create pipeline: %w", err)
	}
	pipeline, _ := parseJSONObject(pipelineRaw)["pipeline"].(map[string]any)
	pipelineID, _ := pipeline["id"].(string)
	if pipelineID == "" {
		return fmt.Errorf("create pipeline returned no pipeline id")
	}
	if _, err := c.Post("/api/pipelines/"+pipelineID+"/run", nil); err != nil {
		return fmt.Errorf("activate pipeline %s: %w", pipelineID, err)
	}
	syncRaw, err := c.Post("/api/sources/"+sourceID+"/sync", map[string]any{
		"full_sync": true, "minimum_documents": 1,
	})
	if err != nil {
		return fmt.Errorf("start source synchronization: %w", err)
	}
	syncID, _ := parseJSONObject(syncRaw)["operation_id"].(string)
	if syncID == "" {
		return fmt.Errorf("source synchronization returned no operation id")
	}

	cmd.Printf("[%s] Waiting for source-scoped marker retrieval\n", time.Since(started).Round(time.Second))
	for {
		if _, err := remaining(); err != nil {
			return fmt.Errorf("%w; sync=%s pipeline=%s", err, syncID, pipelineID)
		}
		searchRaw, searchErr := c.Post("/api/playground/search", map[string]any{
			"query": o.marker, "destination_ids": []string{dstID}, "top_k": 5,
			"source_id": sourceID, "pipeline_id": pipelineID,
		})
		if searchErr == nil && searchContains(parseJSONObject(searchRaw), o.marker) {
			break
		}
		time.Sleep(2 * time.Second)
	}

	mcp := map[string]any{}
	if o.foundryDeploymentID == "" {
		if o.mcpDeploymentID != "" {
			mcp, err = requireSucceededCloudJob(c, o.mcpDeploymentID, "mcp")
			if err != nil {
				return err
			}
			if err := requireCloudJobDestination(c, mcp, dstID); err != nil {
				return err
			}
			cmd.Printf("[%s] Reusing MCP deployment %s\n", time.Since(started).Round(time.Second), o.mcpDeploymentID)
		} else {
			cmd.Printf("[%s] Deploying Cosmos MCP server\n", time.Since(started).Round(time.Second))
			mcp, err = planApproveWait(c, map[string]any{
				"kind": "mcp", "resource_group_id": o.resourceGroupID, "location": o.location,
				"destination_id": dstID, "embedding_model_id": o.embeddingModel,
				"cosmos_account_id": o.cosmosAccountID, "embedding_account_id": o.embeddingAccountID,
				"vector_field": "embedding", "fields": "id,title,content,source_ref",
			}, remaining)
			if err != nil {
				return demoCloudError("MCP deployment", mcp, err)
			}
		}
	}

	foundry := map[string]any{}
	if o.foundryDeploymentID != "" {
		foundry, err = requireSucceededCloudJob(c, o.foundryDeploymentID, "foundry")
		if err != nil {
			return err
		}
		cmd.Printf("[%s] Reusing Foundry deployment %s\n", time.Since(started).Round(time.Second), o.foundryDeploymentID)
	} else {
		cmd.Printf("[%s] Creating Foundry agent\n", time.Since(started).Round(time.Second))
		foundry, err = planApproveWait(c, map[string]any{
			"kind": "foundry", "resource_group_id": strings.Split(o.projectResourceID, "/providers/")[0],
			"location": o.location, "project_resource_id": o.projectResourceID,
			"mcp_deployment_id": cloudJobID(mcp), "chat_deployment": o.chatDeployment,
		}, remaining)
		if err != nil {
			return demoCloudError("Foundry agent creation", foundry, err)
		}
	}

	cmd.Printf("[%s] Asking the Foundry agent\n", time.Since(started).Round(time.Second))
	verification, err := planApproveWait(c, map[string]any{
		"kind":              "verification",
		"resource_group_id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/derived",
		"location":          o.location, "foundry_deployment_id": cloudJobID(foundry), "question": o.question,
	}, remaining)
	if err != nil {
		return demoCloudError("Foundry verification", verification, err)
	}
	result, _ := verification["result"].(map[string]any)
	answer, _ := result["answer"].(string)
	if answer == "" {
		data, _ := json.Marshal(verification)
		return fmt.Errorf("verification succeeded without an answer: %s", string(data))
	}
	cmd.Printf("\nAnswer:\n%s\n", answer)
	if refs := result["source_references"]; refs != nil {
		refsJSON, _ := json.MarshalIndent(refs, "", "  ")
		cmd.Printf("\nSources:\n%s\n", refsJSON)
	}
	cmd.Printf("\nCompleted in %s. Source=%s Type=%s Pipeline=%s MCP=%s Foundry=%s Verification=%s\n",
		time.Since(started).Round(time.Second), sourceID, sourceType, pipelineID,
		cloudJobID(mcp), cloudJobID(foundry), cloudJobID(verification))
	return nil
}
