package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Manage CLI configuration",
	}
	cmd.AddCommand(
		newConfigSetCmd(),
		newConfigViewCmd(),
	)
	return cmd
}

func newConfigSetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set <key> <value>",
		Short: "Set a config value (e.g., omnivec config set server http://...)",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			key, value := args[0], args[1]
			cfg := loadConfig()
			switch key {
			case "server":
				cfg.Server = value
			default:
				exitErr("Unknown config key: %s (valid keys: server)", key)
			}
			if err := saveConfig(cfg); err != nil {
				exitErr("Save config: %v", err)
			}
			fmt.Printf("Set %s = %s\n", bold(key), value)
			fmt.Printf("Config saved to %s\n", configFile())
			return nil
		},
	}
}

func newConfigViewCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "view",
		Short: "Show current configuration",
		RunE: func(cmd *cobra.Command, args []string) error {
			printConfigView()
			return nil
		},
	}
}
