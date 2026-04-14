package main

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

var deploymentColumns = []Column{
	{Header: "NAME", Key: "name"},
	{Header: "READY", Key: "_ready"},
	{Header: "STATUS", Key: "status"},
	{Header: "IMAGE", Key: "image", Width: 55},
	{Header: "PODS", Key: "_pods"},
}

func newDeploymentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "deployment",
		Aliases: []string{"deployments", "deploy"},
		Short:   "Manage deployments (operations)",
	}
	cmd.AddCommand(
		newDeploymentListCmd(),
		newDeploymentScaleCmd(),
		newDeploymentRestartCmd(),
		newDeploymentPauseCmd(),
		newDeploymentResumeCmd(),
	)
	return cmd
}

func newDeploymentListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List deployments",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/operations/deployments", nil)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput != "table" {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
				return nil
			}

			// Deployments endpoint returns a raw array
			var items []map[string]any
			json.Unmarshal(data, &items)

			// Enrich with computed fields
			for _, item := range items {
				replicas := toFloat(item["replicas"])
				ready := toFloat(item["ready_replicas"])
				item["_ready"] = fmt.Sprintf("%.0f/%.0f", ready, replicas)
				if pods, ok := item["pods"].([]any); ok {
					item["_pods"] = fmt.Sprintf("%d", len(pods))
				} else {
					item["_pods"] = "0"
				}
			}
			outputList(items, deploymentColumns)
			return nil
		},
	}
}

func newDeploymentScaleCmd() *cobra.Command {
	var replicas int
	cmd := &cobra.Command{
		Use:   "scale <deployment-name>",
		Short: "Scale a deployment",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			body := map[string]any{"replicas": replicas}
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/operations/deployments/%s/scale", name), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if msg, ok := resp["message"].(string); ok {
				exitOK("%s", msg)
			} else {
				exitOK("Scaled %s to %d replicas", name, replicas)
			}
			return nil
		},
	}
	cmd.Flags().IntVarP(&replicas, "replicas", "r", 1, "Number of replicas")
	cmd.MarkFlagRequired("replicas")
	return cmd
}

func newDeploymentRestartCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "restart <deployment-name>",
		Short: "Rolling restart a deployment",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			if !yes && !confirmAction(fmt.Sprintf("Restart deployment %s?", name)) {
				return nil
			}
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/operations/deployments/%s/restart", name), nil)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if msg, ok := resp["message"].(string); ok {
				exitOK("%s", msg)
			} else {
				exitOK("Restarting %s", name)
			}
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newDeploymentPauseCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "pause <deployment-name>",
		Short: "Pause a deployment (scale to 0)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			body := map[string]any{"replicas": 0}
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/operations/deployments/%s/scale", name), body)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Deployment %s paused (scaled to 0)", name)
			return nil
		},
	}
}

func newDeploymentResumeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "resume <deployment-name>",
		Short: "Resume a deployment (scale to 1)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			body := map[string]any{"replicas": 1}
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/operations/deployments/%s/scale", name), body)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Deployment %s resumed (scaled to 1)", name)
			return nil
		},
	}
}

func toFloat(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	default:
		return 0
	}
}
