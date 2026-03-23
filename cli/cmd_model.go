package main

import (
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/spf13/cobra"
)

var modelColumns = []Column{
	{Header: "NAME", Key: "name"},
	{Header: "SOURCE", Key: "source"},
	{Header: "TYPE", Key: "type"},
	{Header: "STATUS", Key: "status"},
	{Header: "HEALTH", Key: "_health"},
}

func newModelCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "model",
		Aliases: []string{"models"},
		Short:   "Manage DocGrok embedding models",
	}
	cmd.AddCommand(
		newModelListCmd(),
		newModelAddCmd(),
		newModelDeleteCmd(),
		newModelStartCmd(),
		newModelStopCmd(),
		newModelRestartCmd(),
		newModelScaleCmd(),
		newModelLogsCmd(),
		newProviderListCmd(),
	)
	return cmd
}

func newModelListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all models",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/docgrok/models", nil)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput != "table" {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
				return nil
			}

			// Models can be returned as a list or wrapped
			var items []map[string]any
			if err := json.Unmarshal(data, &items); err != nil {
				// Try wrapped
				items = parseJSONList(data, "models")
			}
			_, _, _, mdlH := fetchHealthMap(c)
			enrichHealth(items, mdlH, "name")
			outputList(items, modelColumns)
			return nil
		},
	}
}

func newModelAddCmd() *cobra.Command {
	var provider, providerType, endpoint, apiKey, apiVersion, modelName string
	var dimensions int
	cmd := &cobra.Command{
		Use:   "add",
		Short: "Add an external embedding model",
		RunE: func(cmd *cobra.Command, args []string) error {
			if provider == "" {
				exitErr("--provider is required")
			}
			if providerType == "" {
				exitErr("--type is required (azure-openai, openai, cohere, custom)")
			}
			if endpoint == "" {
				exitErr("--endpoint is required")
			}
			if modelName == "" {
				exitErr("--model is required")
			}
			body := map[string]any{
				"name":     provider,
				"type":     providerType,
				"endpoint": endpoint,
				"model":    modelName,
			}
			if apiKey != "" {
				body["api_key"] = apiKey
			}
			if apiVersion != "" {
				body["api_version"] = apiVersion
			}
			if dimensions > 0 {
				body["dimensions"] = dimensions
			}
			c := getClient()
			_, err := c.Post("/api/models", body)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("External model added: %s (%s)", provider, modelName)
			return nil
		},
	}
	cmd.Flags().StringVar(&provider, "provider", "", "Provider name (e.g., my-azure-openai)")
	cmd.Flags().StringVarP(&providerType, "type", "t", "", "Provider type (azure-openai, openai, cohere, custom)")
	cmd.Flags().StringVar(&endpoint, "endpoint", "", "Endpoint URL")
	cmd.Flags().StringVar(&apiKey, "api-key", "", "API key")
	cmd.Flags().StringVar(&apiVersion, "api-version", "", "API version (Azure only)")
	cmd.Flags().StringVar(&modelName, "model", "", "Model/deployment name")
	cmd.Flags().IntVar(&dimensions, "dimensions", 0, "Embedding dimensions")
	return cmd
}

func newModelDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <provider-name>",
		Short: "Delete an external model provider",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			if !yes && !confirmAction(fmt.Sprintf("Delete model provider %s?", name)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/models/%s", name))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Provider %s deleted", name)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newModelStartCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "start <model-name>",
		Short: "Start/enable a model",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/docgrok/models/%s/enable", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Model %s started", args[0])
			return nil
		},
	}
}

func newModelStopCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stop <model-name>",
		Short: "Stop/disable a model",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/docgrok/models/%s/disable", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Model %s stopped", args[0])
			return nil
		},
	}
}

func newModelRestartCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "restart <model-name>",
		Short: "Restart a model",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/docgrok/models/%s/restart", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Model %s restarted", args[0])
			return nil
		},
	}
}

func newModelScaleCmd() *cobra.Command {
	var replicas int
	cmd := &cobra.Command{
		Use:   "scale <model-name>",
		Short: "Scale a model",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			body := map[string]any{"replicas": replicas}
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/docgrok/models/%s/scale", args[0]), body)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Model %s scaled to %d replicas", args[0], replicas)
			return nil
		},
	}
	cmd.Flags().IntVarP(&replicas, "replicas", "r", 1, "Number of replicas")
	cmd.MarkFlagRequired("replicas")
	return cmd
}

func newModelLogsCmd() *cobra.Command {
	var lines int
	cmd := &cobra.Command{
		Use:   "logs <model-name>",
		Short: "View model logs",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			params := map[string]string{}
			if lines > 0 {
				params["lines"] = strconv.Itoa(lines)
			}
			data, err := c.Get(fmt.Sprintf("/api/docgrok/logs/%s", args[0]), params)
			if err != nil {
				exitErr("%v", err)
			}
			// Logs might be raw text or JSON wrapped
			var logResp map[string]any
			if json.Unmarshal(data, &logResp) == nil {
				if logs, ok := logResp["logs"].(string); ok {
					fmt.Print(logs)
					return nil
				}
			}
			fmt.Println(string(data))
			return nil
		},
	}
	cmd.Flags().IntVarP(&lines, "lines", "n", 100, "Number of log lines")
	return cmd
}

func newProviderListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "providers",
		Short: "List external embedding providers",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/models", map[string]string{"source": "external"})
			if err != nil {
				exitErr("%v", err)
			}
			var raw any
			json.Unmarshal(data, &raw)
			outputResult(raw, nil)
			return nil
		},
	}
}
