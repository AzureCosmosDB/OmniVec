package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var sourceColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "NAME", Key: "name"},
	{Header: "TYPE", Key: "type"},
	{Header: "HEALTH", Key: "_health"},
	{Header: "ENABLED", Key: "enabled"},
	{Header: "UPDATED", Key: "updated_at"},
}

func newSourceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "source",
		Aliases: []string{"sources", "src"},
		Short:   "Manage data sources",
	}
	cmd.AddCommand(
		newSourceListCmd(),
		newSourceShowCmd(),
		newSourceCreateCmd(),
		newSourceUpdateCmd(),
		newSourceDeleteCmd(),
		newSourceTestCmd(),
		newSourceSyncCmd(),
	)
	return cmd
}

func newSourceListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all sources",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/sources", nil)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "sources")
			srcH, _, _, _ := fetchHealthMap(c)
			enrichHealth(items, srcH, "id")
			outputList(items, sourceColumns)
			return nil
		},
	}
}

func newSourceShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <source-id>",
		Short: "Show source details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "src-")
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/sources/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)
			outputResult(obj, sourceColumns)
			return nil
		},
	}
}

func newSourceCreateCmd() *cobra.Command {
	var name, srcType, config, configFile string
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a new source",
		RunE: func(cmd *cobra.Command, args []string) error {
			if name == "" {
				exitErr("--name is required")
			}
			if srcType == "" {
				exitErr("--type is required (azure-blob, cosmosdb)")
			}
			cfg, err := parseConfig(config, configFile)
			if err != nil {
				exitErr("invalid config: %v", err)
			}
			body := map[string]any{
				"name":   name,
				"type":   srcType,
				"config": cfg,
			}
			c := getClient()
			data, err := c.Post("/api/sources", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if src, ok := resp["source"].(map[string]any); ok {
				exitOK("Source created: %s", src["id"])
				if flagOutput != "table" {
					outputResult(src, sourceColumns)
				}
			} else {
				exitOK("Source created")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Source name")
	cmd.Flags().StringVarP(&srcType, "type", "t", "", "Source type (azure-blob, cosmosdb)")
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON config string")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON config file path")
	return cmd
}

func newSourceUpdateCmd() *cobra.Command {
	var name, srcType, config, configFile string
	cmd := &cobra.Command{
		Use:   "update <source-id>",
		Short: "Update a source",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "src-")
			c := getClient()
			// Fetch current state
			existing, err := c.Get(fmt.Sprintf("/api/sources/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			body := parseJSONObject(existing)
			// Remove read-only fields
			delete(body, "id")
			delete(body, "created_at")
			delete(body, "updated_at")
			// Apply overrides
			if name != "" {
				body["name"] = name
			}
			if srcType != "" {
				body["type"] = srcType
			}
			if config != "" || configFile != "" {
				cfg, err := parseConfig(config, configFile)
				if err != nil {
					exitErr("invalid config: %v", err)
				}
				body["config"] = cfg
			}
			data, err := c.Put(fmt.Sprintf("/api/sources/%s", id), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if src, ok := resp["source"].(map[string]any); ok {
				exitOK("Source updated: %s", src["id"])
			} else {
				exitOK("Source updated")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Source name")
	cmd.Flags().StringVarP(&srcType, "type", "t", "", "Source type")
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON config string")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON config file path")
	return cmd
}

func newSourceDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <source-id>",
		Short: "Delete a source",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "src-")
			if !yes && !confirmAction(fmt.Sprintf("Delete source %s?", id)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/sources/%s", id))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Source %s deleted", id)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newSourceTestCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "test <source-id>",
		Short: "Test source connection",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "src-")
			fmt.Printf("Testing connection to %s...\n", id)
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/sources/%s/test", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if success, _ := resp["success"].(bool); success {
				exitOK("Connection successful")
				if result, ok := resp["result"]; ok {
					printJSON(result)
				}
			} else {
				errMsg, _ := resp["error"].(string)
				exitErr("Connection failed: %s", errMsg)
			}
			return nil
		},
	}
}

func newSourceSyncCmd() *cobra.Command {
	var full bool
	cmd := &cobra.Command{
		Use:   "sync <source-id>",
		Short: "Trigger source sync",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "src-")
			body := map[string]any{"full_sync": full}
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/sources/%s/sync", id), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if msg, ok := resp["message"].(string); ok {
				exitOK("%s", msg)
			} else {
				exitOK("Sync triggered")
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&full, "full", false, "Full re-sync")
	return cmd
}

// Helpers

func ensurePrefix(id, prefix string) string {
	if len(id) > 0 && id[:min(len(prefix), len(id))] != prefix {
		return prefix + id
	}
	return id
}

func parseConfig(configStr, filePath string) (map[string]any, error) {
	var raw []byte
	if filePath != "" {
		var err error
		raw, err = os.ReadFile(filePath)
		if err != nil {
			return nil, fmt.Errorf("read file: %w", err)
		}
	} else if configStr != "" {
		raw = []byte(configStr)
	} else {
		return map[string]any{}, nil
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	return cfg, nil
}

func confirmAction(prompt string) bool {
	fmt.Printf("%s [y/N]: ", prompt)
	var resp string
	fmt.Scanln(&resp)
	return resp == "y" || resp == "Y" || resp == "yes"
}
