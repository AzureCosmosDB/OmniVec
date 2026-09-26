package main

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

var cloudColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "KIND", Key: "kind"},
	{Header: "STATUS", Key: "status"},
	{Header: "STAGE", Key: "stage"},
	{Header: "ATTEMPTS", Key: "attempts"},
}

func newCloudCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "cloud",
		Short: "Plan, approve, and inspect MCP and Foundry cloud jobs",
	}
	cmd.AddCommand(
		newCloudListCmd(),
		newCloudShowCmd(),
		newCloudPermissionsCmd(),
		newCloudApproveCmd(),
		newCloudWaitCmd(),
		newCloudPlanCmd(),
	)
	return cmd
}

func listCloudJobs(c *Client) (map[string]any, []map[string]any, error) {
	raw, err := c.Get("/api/cloud-deployments", nil)
	if err != nil {
		return nil, nil, err
	}
	return parseJSONObject(raw), parseJSONList(raw, "jobs"), nil
}

func findCloudJob(c *Client, id string) (map[string]any, error) {
	raw, directErr := c.Get("/api/cloud-deployments/"+id, nil)
	if directErr == nil {
		job := parseJSONObject(raw)
		if job["id"] == id {
			return job, nil
		}
	}
	if directErr != nil && !strings.Contains(directErr.Error(), "[404]") {
		return nil, directErr
	}

	// Backward compatibility for servers that predate the single-job route.
	_, jobs, err := listCloudJobs(c)
	if err != nil {
		return nil, err
	}
	for _, job := range jobs {
		if job["id"] == id {
			return job, nil
		}
	}
	return nil, fmt.Errorf("cloud job %q not found", id)
}

func newCloudListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List cloud deployment jobs",
		RunE: func(_ *cobra.Command, _ []string) error {
			obj, jobs, err := listCloudJobs(getClient())
			if err != nil {
				return err
			}
			if flagOutput == "table" {
				if capabilities, ok := obj["capabilities"].(map[string]any); ok {
					fmt.Printf("Provisioning enabled: %v\n", capabilities["enabled"])
				}
				outputList(jobs, cloudColumns)
			} else {
				outputResult(obj, nil)
			}
			return nil
		},
	}
}

func newCloudShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <job-id>",
		Short: "Show a cloud deployment job and its plan",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			job, err := findCloudJob(getClient(), args[0])
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
}

func newCloudPermissionsCmd() *cobra.Command {
	var shell string
	cmd := &cobra.Command{
		Use:   "permissions <job-id>",
		Short: "Print the Azure roles and exact grant commands required by a prepared plan",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if shell != "powershell" && shell != "bash" {
				return fmt.Errorf("--shell must be powershell or bash")
			}
			job, err := findCloudJob(getClient(), args[0])
			if err != nil {
				return err
			}
			plan, _ := job["plan"].(map[string]any)
			items, _ := plan["deployer_permissions"].([]any)
			if len(items) == 0 {
				return fmt.Errorf("job does not contain deployment permission guidance")
			}
			for _, raw := range items {
				item, _ := raw.(map[string]any)
				cmd.Printf("Role: %v\nScope: %v\n", item["role"], item["scope"])
				commands, _ := item["commands"].(map[string]any)
				if command, _ := commands[shell].(string); command != "" {
					cmd.Printf("%s\n\n", command)
				} else {
					cmd.Println("No command is available because the deployment principal ID is not configured.")
				}
			}
			cmd.Println("OmniVec prints permission commands for administrator review; it never elevates its own identity.")
			return nil
		},
	}
	cmd.Flags().StringVar(&shell, "shell", "powershell", "Grant command syntax: powershell or bash")
	return cmd
}

func approveCloudJob(c *Client, job map[string]any) (map[string]any, error) {
	id, _ := job["id"].(string)
	hash, _ := job["plan_hash"].(string)
	if id == "" || hash == "" {
		return nil, fmt.Errorf("cloud job is missing its id or plan hash")
	}
	raw, err := c.Post("/api/cloud-deployments/"+id+"/approve", map[string]any{
		"plan_hash": hash, "approve_cost_and_permissions": true,
	})
	if err != nil {
		return nil, err
	}
	return parseJSONObject(raw), nil
}

func newCloudApproveCmd() *cobra.Command {
	var yes, wait bool
	var timeout time.Duration
	cmd := &cobra.Command{
		Use:   "approve <job-id>",
		Short: "Approve the exact saved plan, including its listed cost and permission effects",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c := getClient()
			job, err := findCloudJob(c, args[0])
			if err != nil {
				return err
			}
			if !yes && !confirmAction("Approve the exact cloud plan, costs, and permissions?") {
				return fmt.Errorf("approval cancelled")
			}
			job, err = approveCloudJob(c, job)
			if err != nil {
				return err
			}
			if wait {
				job, err = waitForCloudJob(c, args[0], timeout)
				if err != nil {
					return err
				}
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Approve without an interactive prompt")
	cmd.Flags().BoolVar(&wait, "wait", false, "Wait for the job to finish")
	cmd.Flags().DurationVar(&timeout, "timeout", 5*time.Minute, "Maximum wait time")
	return cmd
}

func waitForCloudJob(c *Client, id string, timeout time.Duration) (map[string]any, error) {
	deadline := time.Now().Add(timeout)
	for {
		job, err := findCloudJob(c, id)
		if err != nil {
			return nil, err
		}
		status, _ := job["status"].(string)
		switch status {
		case "succeeded":
			return job, nil
		case "failed", "interrupted":
			if detail, _ := job["error"].(string); detail != "" {
				return job, fmt.Errorf("cloud job %s %s: %s", id, status, detail)
			}
			return job, fmt.Errorf("cloud job %s %s", id, status)
		}
		if time.Now().After(deadline) {
			return job, fmt.Errorf("timed out after %s waiting for cloud job %s (status %s)", timeout, id, status)
		}
		time.Sleep(2 * time.Second)
	}
}

func newCloudWaitCmd() *cobra.Command {
	var timeout time.Duration
	cmd := &cobra.Command{
		Use:   "wait <job-id>",
		Short: "Wait for a cloud deployment job to finish",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			job, err := waitForCloudJob(getClient(), args[0], timeout)
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().DurationVar(&timeout, "timeout", 5*time.Minute, "Maximum wait time")
	return cmd
}

func planCloudJob(c *Client, body map[string]any) (map[string]any, error) {
	raw, err := c.Post("/api/cloud-deployments/plan", body)
	if err != nil {
		return nil, err
	}
	return parseJSONObject(raw), nil
}

func newCloudPlanCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "plan", Short: "Prepare a cloud plan without provisioning resources"}
	cmd.AddCommand(newCloudPlanContainerCmd(), newCloudPlanMCPCmd(), newCloudPlanFoundryCmd(), newCloudPlanVerifyCmd())
	return cmd
}

func newCloudPlanContainerCmd() *cobra.Command {
	var cosmosAccountID, database, container, model, vectorField, partitionKeyPath, location string
	cmd := &cobra.Command{
		Use:   "container",
		Short: "Prepare an isolated Cosmos vector container",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := requireCloudFlags(map[string]string{
				"--cosmos-account-id": cosmosAccountID, "--database": database,
				"--container": container, "--embedding-model": model,
			}); err != nil {
				return err
			}
			job, err := planCloudJob(getClient(), map[string]any{
				"kind":              "cosmos_container",
				"resource_group_id": strings.Split(cosmosAccountID, "/providers/")[0],
				"location":          location, "cosmos_account_id": cosmosAccountID,
				"database": database, "container": container,
				"embedding_model_id": model, "vector_field": strings.TrimPrefix(vectorField, "/"),
				"partition_key_path": partitionKeyPath,
			})
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().StringVar(&cosmosAccountID, "cosmos-account-id", "", "Full existing Cosmos account ARM ID")
	cmd.Flags().StringVar(&database, "database", "", "Existing Cosmos database")
	cmd.Flags().StringVar(&container, "container", "", "New isolated container name")
	cmd.Flags().StringVar(&model, "embedding-model", "", "Registered embedding model ID")
	cmd.Flags().StringVar(&vectorField, "vector-field", "embedding", "Top-level vector field")
	cmd.Flags().StringVar(&partitionKeyPath, "partition-key-path", "/id", "Top-level Cosmos partition key path")
	cmd.Flags().StringVar(&location, "location", "eastus2", "Azure location")
	return cmd
}

func newCloudPlanMCPCmd() *cobra.Command {
	var resourceGroupID, location, destination, model, cosmosAccountID, embeddingAccountID, vectorField, fields string
	cmd := &cobra.Command{
		Use:   "mcp",
		Short: "Prepare a Cosmos MCP Function deployment",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := requireCloudFlags(map[string]string{
				"--resource-group-id": resourceGroupID, "--destination": destination, "--embedding-model": model,
				"--cosmos-account-id": cosmosAccountID, "--embedding-account-id": embeddingAccountID,
			}); err != nil {
				return err
			}
			job, err := planCloudJob(getClient(), map[string]any{
				"kind": "mcp", "resource_group_id": resourceGroupID, "location": location,
				"destination_id": resolveDestination(destination), "embedding_model_id": model,
				"cosmos_account_id": cosmosAccountID, "embedding_account_id": embeddingAccountID,
				"vector_field": strings.TrimPrefix(vectorField, "/"), "fields": fields,
			})
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().StringVar(&resourceGroupID, "resource-group-id", "", "Full target resource-group ARM ID")
	cmd.Flags().StringVar(&location, "location", "eastus2", "Azure Flex Consumption region")
	cmd.Flags().StringVar(&destination, "destination", "", "Cosmos vector destination ID or name")
	cmd.Flags().StringVar(&model, "embedding-model", "", "Registered Azure OpenAI embedding model ID")
	cmd.Flags().StringVar(&cosmosAccountID, "cosmos-account-id", "", "Full Cosmos account ARM ID")
	cmd.Flags().StringVar(&embeddingAccountID, "embedding-account-id", "", "Full Azure OpenAI account ARM ID")
	cmd.Flags().StringVar(&vectorField, "vector-field", "embedding", "Top-level vector field")
	cmd.Flags().StringVar(&fields, "fields", "id,title,content,source_ref", "Comma-separated returned fields")
	return cmd
}

func newCloudPlanFoundryCmd() *cobra.Command {
	var projectID, mcpID, chatDeployment, location string
	cmd := &cobra.Command{
		Use:   "foundry",
		Short: "Prepare a Foundry agent backed by a completed MCP deployment",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := requireCloudFlags(map[string]string{
				"--project-resource-id": projectID, "--mcp-deployment": mcpID, "--chat-deployment": chatDeployment,
			}); err != nil {
				return err
			}
			job, err := planCloudJob(getClient(), map[string]any{
				"kind": "foundry", "resource_group_id": strings.Split(projectID, "/providers/")[0],
				"location": location, "project_resource_id": projectID,
				"mcp_deployment_id": mcpID, "chat_deployment": chatDeployment,
			})
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().StringVar(&projectID, "project-resource-id", "", "Full existing Foundry project ARM ID")
	cmd.Flags().StringVar(&mcpID, "mcp-deployment", "", "Succeeded OmniVec MCP deployment job ID")
	cmd.Flags().StringVar(&chatDeployment, "chat-deployment", "", "Existing Foundry chat deployment name")
	cmd.Flags().StringVar(&location, "location", "eastus", "Foundry project location")
	return cmd
}

func newCloudPlanVerifyCmd() *cobra.Command {
	var foundryID, question, location string
	cmd := &cobra.Command{
		Use:   "verify",
		Short: "Prepare one billable Foundry question and retrieval verification",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := requireCloudFlags(map[string]string{
				"--foundry-deployment": foundryID, "--question": question,
			}); err != nil {
				return err
			}
			job, err := planCloudJob(getClient(), map[string]any{
				"kind":              "verification",
				"resource_group_id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/derived",
				"location":          location, "foundry_deployment_id": foundryID, "question": question,
			})
			if err != nil {
				return err
			}
			outputResult(job, cloudColumns)
			return nil
		},
	}
	cmd.Flags().StringVar(&foundryID, "foundry-deployment", "", "Succeeded OmniVec Foundry deployment job ID")
	cmd.Flags().StringVar(&question, "question", "", "Question to ask")
	cmd.Flags().StringVar(&location, "location", "eastus", "Foundry project location")
	return cmd
}

func requireCloudFlags(values map[string]string) error {
	missing := []string{}
	for flag, value := range values {
		if strings.TrimSpace(value) == "" {
			missing = append(missing, flag)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return fmt.Errorf("required flag(s): %s", strings.Join(missing, ", "))
	}
	return nil
}

func cloudJobID(job map[string]any) string {
	id, _ := job["id"].(string)
	return id
}
