package main

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var (
	flagServer  string
	flagToken   string
	flagOutput  string
	flagPerPage int
)

func main() {
	rootCmd := &cobra.Command{
		Use:   "omnivec",
		Short: "OmniVec CLI - Universal Vector Ingestion Platform",
		Long:  "Manage sources, destinations, pipelines, jobs, models, and deployments.",
	}

	rootCmd.PersistentFlags().StringVarP(&flagServer, "server", "s", "", "OmniVec server URL (overrides config)")
	rootCmd.PersistentFlags().StringVar(&flagToken, "token", "", "Bearer token for authentication (overrides config)")
	rootCmd.PersistentFlags().StringVarP(&flagOutput, "output", "o", "table", "Output format: table, json, yaml")
	rootCmd.PersistentFlags().IntVar(&flagPerPage, "per-page", 15, "Rows per page in table output (0 = no pagination)")

	rootCmd.AddCommand(
		newAuthCmd(),
		newSourceCmd(),
		newDestinationCmd(),
		newPipelineCmd(),
		newJobCmd(),
		newDeploymentCmd(),
		newModelCmd(),
		newTransformCmd(),
		newSearchCmd(),
		newStatusCmd(),
		newSettingsCmd(),
		newConfigCmd(),
	)

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func getClient() *Client {
	server := resolveServer(flagServer)
	token := resolveToken(flagToken)
	return NewClient(server, token)
}

// fetchHealthMap fetches /api/health/checks and returns lookup maps keyed by id.
// Returns maps for sources, destinations, pipelines, and models.
func fetchHealthMap(c *Client) (srcH, dstH, pipH, mdlH map[string]string) {
	srcH = map[string]string{}
	dstH = map[string]string{}
	pipH = map[string]string{}
	mdlH = map[string]string{}

	data, err := c.Get("/api/health/checks", nil)
	if err != nil {
		return
	}
	obj := parseJSONObject(data)

	extractByID := func(key string) map[string]string {
		m := map[string]string{}
		if arr, ok := obj[key].([]any); ok {
			for _, item := range arr {
				if im, ok := item.(map[string]any); ok {
					id, _ := im["id"].(string)
					status, _ := im["status"].(string)
					if id != "" {
						m[id] = status
					}
				}
			}
		}
		return m
	}

	srcH = extractByID("sources")
	dstH = extractByID("destinations")
	pipH = extractByID("pipelines")

	// Models use "name" as key, not "id"
	if arr, ok := obj["models"].([]any); ok {
		for _, item := range arr {
			if im, ok := item.(map[string]any); ok {
				name, _ := im["name"].(string)
				status, _ := im["status"].(string)
				if name != "" {
					mdlH[name] = status
				}
			}
		}
	}
	return
}

// enrichHealth adds "_health" field to items using a lookup map.
func enrichHealth(items []map[string]any, healthMap map[string]string, keyField string) {
	for _, item := range items {
		id, _ := item[keyField].(string)
		if h, ok := healthMap[id]; ok {
			item["_health"] = h
		} else {
			item["_health"] = "-"
		}
	}
}
