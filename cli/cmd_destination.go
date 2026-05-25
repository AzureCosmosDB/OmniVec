package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

var destColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "NAME", Key: "name"},
	{Header: "TYPE", Key: "type"},
	{Header: "HEALTH", Key: "_health"},
	{Header: "ENABLED", Key: "enabled"},
	{Header: "UPDATED", Key: "updated_at"},
}

func newDestinationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "destination",
		Aliases: []string{"destinations", "dst", "dest"},
		Short:   "Manage vector store destinations",
	}
	cmd.AddCommand(
		newDestListCmd(),
		newDestShowCmd(),
		newDestCreateCmd(),
		newDestUpdateCmd(),
		newDestDeleteCmd(),
		newDestTestCmd(),
		newDestEnableCmd(),
		newDestDisableCmd(),
	)
	return cmd
}

func newDestListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all destinations",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/destinations", nil)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "destinations")
			_, dstH, _, _ := fetchHealthMap(c)
			enrichHealth(items, dstH, "id")
			outputList(items, destColumns)
			return nil
		},
	}
}

func newDestShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <destination-id>",
		Short: "Show destination details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/destinations/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)
			outputResult(obj, destColumns)
			return nil
		},
	}
}

func newDestCreateCmd() *cobra.Command {
	var name, dstType, config, configFile string
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a new destination",
		RunE: func(cmd *cobra.Command, args []string) error {
			if name == "" {
				exitErr("--name is required")
			}
			if dstType == "" {
				exitErr("--type is required (cosmosdb-vector)")
			}
			cfg, err := parseConfig(config, configFile)
			if err != nil {
				exitErr("invalid config: %v", err)
			}
			body := map[string]any{
				"name":   name,
				"type":   dstType,
				"config": cfg,
			}
			c := getClient()
			data, err := c.Post("/api/destinations", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if dst, ok := resp["destination"].(map[string]any); ok {
				exitOK("Destination created: %s", dst["id"])
				if enabled, hasEnabled := dst["enabled"].(bool); hasEnabled && !enabled {
					fmt.Println("⚠  Destination is DISABLED — pipelines targeting it will skip docs until you enable it.")
				}
				if warns, ok := resp["warnings"].([]any); ok {
					for _, w := range warns {
						fmt.Printf("   ⚠ %v\n", w)
					}
				}
				if enabled, hasEnabled := dst["enabled"].(bool); hasEnabled && !enabled {
					fmt.Printf("   ↪ Run `omnivec destination enable %s` once the underlying resource is ready.\n", dst["id"])
				}
				if flagOutput != "table" {
					outputResult(dst, destColumns)
				}
			} else {
				exitOK("Destination created")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Destination name")
	cmd.Flags().StringVarP(&dstType, "type", "t", "", "Destination type (cosmosdb-vector)")
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON config string")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON config file path")
	return cmd
}

func newDestEnableCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "enable <destination-id>",
		Short: "Enable a destination (re-probes connectivity; replays change-feed for active pipelines)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			c := getClient()
			data, err := c.Patch(fmt.Sprintf("/api/destinations/%s", id), map[string]any{"enabled": true})
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			exitOK("Destination enabled: %s", id)
			if replayed, ok := resp["replayed_pipelines"].([]any); ok && len(replayed) > 0 {
				fmt.Printf("   ↻ Reset change-feed for %d pipeline(s): ", len(replayed))
				for i, p := range replayed {
					if i > 0 {
						fmt.Print(", ")
					}
					fmt.Print(p)
				}
				fmt.Println()
			}
			return nil
		},
	}
}

func newDestDisableCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "disable <destination-id>",
		Short: "Disable a destination (pipelines will skip docs targeting it)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			c := getClient()
			_, err := c.Patch(fmt.Sprintf("/api/destinations/%s", id), map[string]any{"enabled": false})
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Destination disabled: %s", id)
			return nil
		},
	}
}

func newDestUpdateCmd() *cobra.Command {
	var name, dstType, config, configFile string
	cmd := &cobra.Command{
		Use:   "update <destination-id>",
		Short: "Update a destination",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			c := getClient()
			existing, err := c.Get(fmt.Sprintf("/api/destinations/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			body := parseJSONObject(existing)
			delete(body, "id")
			delete(body, "created_at")
			delete(body, "updated_at")
			if name != "" {
				body["name"] = name
			}
			if dstType != "" {
				body["type"] = dstType
			}
			if config != "" || configFile != "" {
				cfg, err := parseConfig(config, configFile)
				if err != nil {
					exitErr("invalid config: %v", err)
				}
				body["config"] = cfg
			}
			data, err := c.Put(fmt.Sprintf("/api/destinations/%s", id), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if dst, ok := resp["destination"].(map[string]any); ok {
				exitOK("Destination updated: %s", dst["id"])
			} else {
				exitOK("Destination updated")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Destination name")
	cmd.Flags().StringVarP(&dstType, "type", "t", "", "Destination type")
	cmd.Flags().StringVarP(&config, "config", "c", "", "JSON config string")
	cmd.Flags().StringVarP(&configFile, "file", "f", "", "JSON config file path")
	return cmd
}

func newDestDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <destination-id>",
		Short: "Delete a destination",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			if !yes && !confirmAction(fmt.Sprintf("Delete destination %s?", id)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/destinations/%s", id))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Destination %s deleted", id)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newDestTestCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "test <destination-id>",
		Short: "Test destination connection",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "dst-")
			fmt.Printf("Testing connection to %s...\n", id)
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/destinations/%s/test", id), nil)
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
