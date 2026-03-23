package main

import (
	"encoding/json"

	"github.com/spf13/cobra"
)

func newSettingsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "settings",
		Short: "View and update system settings",
	}
	cmd.AddCommand(
		newSettingsViewCmd(),
		newSettingsSetCmd(),
		newSettingsMetricsCmd(),
	)
	return cmd
}

func newSettingsViewCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "view",
		Short: "View current system settings",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/settings", nil)
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

func newSettingsSetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "set <key> <value>",
		Short: "Update a system setting",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			// Get current settings
			data, err := c.Get("/api/settings", nil)
			if err != nil {
				exitErr("%v", err)
			}
			settings := parseJSONObject(data)
			settings[args[0]] = args[1]
			_, err = c.Put("/api/settings", settings)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Setting %s updated to %s", args[0], args[1])
			return nil
		},
	}
}

func newSettingsMetricsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "metrics",
		Short: "View system metrics",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/metrics", nil)
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

