package main

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

var transformColumns = []Column{
	{Header: "NAME", Key: "name"},
	{Header: "DESCRIPTION", Key: "description", Width: 50},
	{Header: "STEPS", Key: "_steps"},
}

func newTransformCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "transform",
		Aliases: []string{"transforms", "tp"},
		Short:   "Manage DocGrok transform pipelines",
	}
	cmd.AddCommand(
		newTransformListCmd(),
		newTransformShowCmd(),
		newTransformCreateCmd(),
		newTransformUpdateCmd(),
		newTransformDeleteCmd(),
	)
	return cmd
}

func newTransformListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List transform pipelines",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/docgrok/pipelines", nil)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput != "table" {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
				return nil
			}

			var items []map[string]any
			if err := json.Unmarshal(data, &items); err != nil {
				items = parseJSONList(data, "pipelines")
			}
			// Compute step count
			for _, item := range items {
				if steps, ok := item["steps"].([]any); ok {
					item["_steps"] = fmt.Sprintf("%d", len(steps))
				} else {
					item["_steps"] = "0"
				}
			}
			outputList(items, transformColumns)
			return nil
		},
	}
}

func newTransformShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <pipeline-name>",
		Short: "Show transform pipeline details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/docgrok/pipelines/%s", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)
			outputResult(obj, transformColumns)
			return nil
		},
	}
}

func newTransformCreateCmd() *cobra.Command {
	var config, configFile string
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a transform pipeline",
		Long:  "Create a transform pipeline from a JSON definition. Use --config or --file to provide the pipeline definition.",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := parseConfig(config, configFile)
			if err != nil {
				exitErr("invalid config: %v", err)
			}
			if len(cfg) == 0 {
				exitErr("--config or --file is required with pipeline definition JSON")
			}
			c := getClient()
			_, err = c.Post("/api/docgrok/pipelines", cfg)
			if err != nil {
				exitErr("%v", err)
			}
			name, _ := cfg["name"].(string)
			exitOK("Transform pipeline created: %s", name)
			return nil
		},
	}
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON pipeline definition")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON pipeline definition file")
	return cmd
}

func newTransformUpdateCmd() *cobra.Command {
	var config, configFile string
	cmd := &cobra.Command{
		Use:   "update <pipeline-name>",
		Short: "Update a transform pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			cfg, err := parseConfig(config, configFile)
			if err != nil {
				exitErr("invalid config: %v", err)
			}
			if len(cfg) == 0 {
				exitErr("--config or --file is required")
			}
			c := getClient()
			_, err = c.Put(fmt.Sprintf("/api/docgrok/pipelines/%s", name), cfg)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Transform pipeline %s updated", name)
			return nil
		},
	}
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON pipeline definition")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON pipeline definition file")
	return cmd
}

func newTransformDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <pipeline-name>",
		Short: "Delete a transform pipeline",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			if !yes && !confirmAction(fmt.Sprintf("Delete transform pipeline %s?", name)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/docgrok/pipelines/%s", name))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Transform pipeline %s deleted", name)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}
