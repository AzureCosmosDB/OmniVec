package main

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

func newSearchCmd() *cobra.Command {
	var index string
	var topK int
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Search vectors in a destination index",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if index == "" {
				exitErr("--index is required (destination ID or name)")
			}
			query := args[0]
			dstID := resolveDestination(index)
			body := map[string]any{
				"query":           query,
				"destination_ids": []string{dstID},
				"top_k":           topK,
			}
			c := getClient()
			data, err := c.Post("/api/playground/search", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)

			if flagOutput != "table" {
				outputResult(resp, nil)
				return nil
			}

			// Display timing info
			embTime, _ := resp["embedding_time_ms"].(float64)
			searchTime, _ := resp["search_time_ms"].(float64)
			fmt.Printf("Query: %s\n", bold(query))
			fmt.Printf("Timing: embedding %.0fms, search %.0fms, total %.0fms\n\n",
				embTime, searchTime, embTime+searchTime)

			// Display results
			results, ok := resp["results"].([]any)
			if !ok || len(results) == 0 {
				fmt.Println("No results found.")
				return nil
			}

			for i, r := range results {
				item, ok := r.(map[string]any)
				if !ok {
					continue
				}
				score, _ := item["score"].(float64)
				sourceRef, _ := item["source_ref"].(string)
				source, _ := item["source"].(string)
				text, _ := item["text"].(string)

				// Truncate text
				if len(text) > 200 {
					text = text[:200] + "..."
				}

				fmt.Printf("%s  %s  %s\n",
					bold(fmt.Sprintf("#%d", i+1)),
					green(fmt.Sprintf("%.1f%%", score*100)),
					cyan(sourceRef))
				if source != "" {
					fmt.Printf("    Source: %s\n", source)
				}
				fmt.Printf("    %s\n\n", dim(text))
			}
			return nil
		},
	}
	cmd.Flags().StringVarP(&index, "index", "i", "", "Destination ID or name")
	cmd.Flags().IntVarP(&topK, "top-k", "k", 5, "Number of results")
	return cmd
}

// resolveDestination accepts a destination ID (dst-...) or a name and returns the ID.
func resolveDestination(input string) string {
	if strings.HasPrefix(input, "dst-") {
		return input
	}
	c := getClient()
	data, err := c.Get("/api/destinations", nil)
	if err != nil {
		exitErr("cannot resolve destination name: %v", err)
	}
	items := parseJSONList(data, "destinations")
	for _, item := range items {
		name, _ := item["name"].(string)
		if strings.EqualFold(name, input) {
			id, _ := item["id"].(string)
			return id
		}
	}
	exitErr("destination '%s' not found", input)
	return ""
}
