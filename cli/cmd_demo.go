package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

const sharePointFoundryQuestion = "Under NORTHSTAR-DEMO-TRAVEL-2026, what is the domestic daily meal limit, when are individual meal receipts required, and how soon must the claim be submitted? Cite the source."

type sharePointFoundryOptions struct {
	siteID, driveID, folderPath                string
	tenantID, clientID                         string
	destination, pipelineModel, embeddingModel string
	resourceGroupID, location                  string
	cosmosAccountID, embeddingAccountID        string
	projectResourceID, chatDeployment          string
	mcpDeploymentID, foundryDeploymentID       string
	question, marker                           string
	timeout                                    time.Duration
	approve                                    bool
}

func newDemoCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "demo", Short: "Run opinionated end-to-end OmniVec demonstrations"}
	cmd.AddCommand(newSharePointFoundryDemoCmd(), newSourceFoundryDemoCmd())
	return cmd
}

func newSharePointFoundryDemoCmd() *cobra.Command {
	var o sharePointFoundryOptions
	cmd := &cobra.Command{
		Use:   "sharepoint-foundry",
		Short: "Ingest one SharePoint folder and verify a Foundry answer through MCP",
		Long: "Creates an isolated SharePoint source and pipeline, waits for a marker document, then creates " +
			"and approves MCP, Foundry-agent, and one-question verification plans. Azure role grants are never " +
			"self-applied; use `omnivec cloud permissions <job-id>` if a prepared plan reports missing access.",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if !o.approve {
				return fmt.Errorf("--approve is required because this command provisions Azure resources and runs billable inference")
			}
			return runSharePointFoundryDemo(cmd, o)
		},
	}
	cmd.Flags().StringVar(&o.siteID, "site-id", "", "Microsoft Graph SharePoint site ID")
	cmd.Flags().StringVar(&o.driveID, "drive-id", "", "Microsoft Graph document-library drive ID")
	cmd.Flags().StringVar(&o.folderPath, "folder", "OmniVec-Foundry-Demo-20260923", "Folder path within the document library")
	cmd.Flags().StringVar(&o.tenantID, "sharepoint-tenant-id", "", "SharePoint application tenant ID")
	cmd.Flags().StringVar(&o.clientID, "sharepoint-client-id", "", "SharePoint application client ID")
	cmd.Flags().StringVar(&o.destination, "destination", "", "Existing isolated Cosmos vector destination ID or name")
	cmd.Flags().StringVar(&o.pipelineModel, "pipeline-model", "text-azure", "DocGrok pipeline name")
	cmd.Flags().StringVar(&o.embeddingModel, "embedding-model", "", "Registered Azure OpenAI embedding model ID")
	cmd.Flags().StringVar(&o.resourceGroupID, "resource-group-id", "", "Full resource-group ARM ID for MCP hosting")
	cmd.Flags().StringVar(&o.location, "location", "eastus2", "Azure Flex Consumption region")
	cmd.Flags().StringVar(&o.cosmosAccountID, "cosmos-account-id", "", "Full Cosmos account ARM ID")
	cmd.Flags().StringVar(&o.embeddingAccountID, "embedding-account-id", "", "Full Azure OpenAI account ARM ID")
	cmd.Flags().StringVar(&o.projectResourceID, "project-resource-id", "", "Full existing Foundry project ARM ID")
	cmd.Flags().StringVar(&o.chatDeployment, "chat-deployment", "", "Existing Foundry chat deployment name")
	cmd.Flags().StringVar(&o.mcpDeploymentID, "mcp-deployment", "", "Reuse a succeeded OmniVec MCP deployment job")
	cmd.Flags().StringVar(&o.foundryDeploymentID, "foundry-deployment", "", "Reuse a succeeded OmniVec Foundry deployment job (fastest path)")
	cmd.Flags().StringVar(&o.question, "question", sharePointFoundryQuestion, "Question to ask the Foundry agent")
	cmd.Flags().StringVar(&o.marker, "marker", "NORTHSTAR-DEMO-TRAVEL-2026", "Unique text expected in vector search")
	cmd.Flags().DurationVar(&o.timeout, "timeout", 5*time.Minute, "Total end-to-end time budget")
	cmd.Flags().BoolVar(&o.approve, "approve", false, "Approve resource creation, listed permissions, and one inference")
	return cmd
}

func runSharePointFoundryDemo(cmd *cobra.Command, o sharePointFoundryOptions) error {
	required := map[string]string{
		"--site-id": o.siteID, "--drive-id": o.driveID,
		"--destination": o.destination,
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
	dstID := resolveDestination(o.destination)
	suffix := fmt.Sprintf("%d", time.Now().Unix())

	cmd.Printf("[0s] Running destination and deployment preflight\n")
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

	cmd.Printf("[0s] Creating SharePoint source for %s\n", o.folderPath)
	sourceRaw, err := c.Post("/api/sources", map[string]any{
		"name": "SharePoint Foundry Demo " + suffix,
		"type": "sharepoint",
		"config": map[string]any{
			"site_id": o.siteID, "drive_id": o.driveID, "folder_path": o.folderPath,
			"file_types": []string{"txt"},
		},
	})
	if err != nil {
		return fmt.Errorf("create SharePoint source: %w", err)
	}
	source, _ := parseJSONObject(sourceRaw)["source"].(map[string]any)
	sourceID, _ := source["id"].(string)
	if sourceID == "" {
		return fmt.Errorf("create SharePoint source returned no source id")
	}
	testRaw, err := c.Post("/api/sources/"+sourceID+"/test", nil)
	if err != nil {
		return fmt.Errorf("SharePoint connection test failed; verify Sites.Selected and site read access: %w", err)
	}
	if success, _ := parseJSONObject(testRaw)["success"].(bool); !success {
		return fmt.Errorf("SharePoint connection test failed; run `omnivec source permissions %s`", sourceID)
	}

	cmd.Printf("[%s] Creating ingestion pipeline\n", time.Since(started).Round(time.Second))
	pipelineSource := map[string]any{
		"source_id": sourceID, "filters": map[string]any{}, "content_fields": []string{"content"},
		"content_mode": "field", "file_types": []string{"txt"},
	}
	if o.tenantID != "" {
		pipelineSource["sharepoint_identity"] = map[string]any{"tenant_id": o.tenantID, "client_id": o.clientID}
	}
	pipelineRaw, err := c.Post("/api/pipelines", map[string]any{
		"name":           "SharePoint Foundry Demo " + suffix,
		"sources":        []map[string]any{pipelineSource},
		"destination_id": dstID, "docgrok_pipeline": o.pipelineModel,
		"vector_index_path": "/embedding", "process_existing": true, "processing_mode": "queue",
		"store_content": true, "content_field": "content",
		"doc_id_pattern": "{source_hash}-{pipeline}",
	})
	if err != nil {
		return fmt.Errorf("create SharePoint pipeline: %w", err)
	}
	pipeline, _ := parseJSONObject(pipelineRaw)["pipeline"].(map[string]any)
	pipelineID, _ := pipeline["id"].(string)
	if pipelineID == "" {
		return fmt.Errorf("create pipeline returned no pipeline id")
	}

	cmd.Printf("[%s] Activating pipeline and starting full synchronization\n", time.Since(started).Round(time.Second))
	if _, err := c.Post("/api/pipelines/"+pipelineID+"/run", nil); err != nil {
		return fmt.Errorf("activate pipeline %s: %w", pipelineID, err)
	}
	syncRaw, err := c.Post("/api/sources/"+sourceID+"/sync", map[string]any{
		"full_sync": true, "minimum_documents": 1,
	})
	if err != nil {
		return fmt.Errorf("start source synchronization: %w", err)
	}
	syncOperationID, _ := parseJSONObject(syncRaw)["operation_id"].(string)
	if syncOperationID == "" {
		return fmt.Errorf("source synchronization returned no operation id")
	}

	cmd.Printf("[%s] Waiting for the marker document to be searchable\n", time.Since(started).Round(time.Second))
	for {
		if _, err := remaining(); err != nil {
			return fmt.Errorf(
				"%w; sync operation %s and pipeline %s may still be ingesting",
				err, syncOperationID, pipelineID)
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
		if err := requireCloudJobDestination(c, foundry, dstID); err != nil {
			return err
		}
		cmd.Printf("[%s] Reusing Foundry deployment %s\n", time.Since(started).Round(time.Second), o.foundryDeploymentID)
	} else {
		cmd.Printf("[%s] Creating Foundry agent\n", time.Since(started).Round(time.Second))
		foundry, err = planApproveWait(c, map[string]any{
			"kind": "foundry", "resource_group_id": strings.Split(o.projectResourceID, "/providers/")[0],
			"location": "eastus", "project_resource_id": o.projectResourceID,
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
		"location":          "eastus", "foundry_deployment_id": cloudJobID(foundry), "question": o.question,
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
	cmd.Printf("\nCompleted in %s. Source=%s Pipeline=%s MCP=%s Foundry=%s Verification=%s\n",
		time.Since(started).Round(time.Second), sourceID, pipelineID,
		cloudJobID(mcp), cloudJobID(foundry), cloudJobID(verification))
	return nil
}

func searchContains(response map[string]any, marker string) bool {
	results, _ := response["results"].([]any)
	marker = strings.ToLower(marker)
	for _, raw := range results {
		item, _ := raw.(map[string]any)
		for _, key := range []string{"text", "content", "source_ref", "title"} {
			if strings.Contains(strings.ToLower(fmt.Sprintf("%v", item[key])), marker) {
				return true
			}
		}
	}
	return false
}

func planApproveWait(c *Client, body map[string]any, remaining func() (time.Duration, error)) (map[string]any, error) {
	job, err := planCloudJob(c, body)
	if err != nil {
		return job, err
	}
	job, err = approveCloudJob(c, job)
	if err != nil {
		return job, err
	}
	left, err := remaining()
	if err != nil {
		return job, err
	}
	return waitForCloudJob(c, cloudJobID(job), left)
}

func demoCloudError(stage string, job map[string]any, err error) error {
	if len(job) == 0 {
		return fmt.Errorf("%s: %w", stage, err)
	}
	return fmt.Errorf("%s: %w; inspect with `omnivec cloud show %s` and `omnivec cloud permissions %s`",
		stage, err, cloudJobID(job), cloudJobID(job))
}

func requireSucceededCloudJob(c *Client, id, kind string) (map[string]any, error) {
	job, err := findCloudJob(c, id)
	if err != nil {
		return nil, err
	}
	if job["kind"] != kind || job["status"] != "succeeded" {
		return nil, fmt.Errorf("cloud job %s must be a succeeded %s deployment", id, kind)
	}
	return job, nil
}

func requireCloudJobDestination(c *Client, job map[string]any, destinationID string) error {
	plan, _ := job["plan"].(map[string]any)
	switch job["kind"] {
	case "mcp":
		if plan["destination_id"] != destinationID {
			return fmt.Errorf("MCP deployment %s targets destination %v, not %s", cloudJobID(job), plan["destination_id"], destinationID)
		}
	case "foundry":
		mcpID, _ := plan["mcp_deployment_id"].(string)
		if mcpID == "" {
			return fmt.Errorf("Foundry deployment %s does not identify its MCP deployment", cloudJobID(job))
		}
		mcp, err := requireSucceededCloudJob(c, mcpID, "mcp")
		if err != nil {
			return err
		}
		return requireCloudJobDestination(c, mcp, destinationID)
	default:
		return fmt.Errorf("cloud job %s cannot be matched to a destination", cloudJobID(job))
	}
	return nil
}
