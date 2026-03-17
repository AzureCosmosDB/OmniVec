package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

func newStatusCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "status",
		Short: "Show system health and statistics",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/health", nil)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)

			if flagOutput != "table" {
				outputResult(resp, nil)
				return nil
			}

			// Service info
			status, _ := resp["status"].(string)
			version, _ := resp["version"].(string)
			docgrok, _ := resp["docgrok"].(string)

			fmt.Printf("%s\n", bold("OmniVec Platform Status"))
			fmt.Printf("%-20s %s\n", "Service:", colorStatus(status))
			fmt.Printf("%-20s %s\n", "Version:", version)
			fmt.Printf("%-20s %s\n", "DocGrok:", colorStatus(docgrok))
			fmt.Println()

			// Stats
			if stats, ok := resp["stats"].(map[string]any); ok {
				fmt.Printf("%s\n", bold("Resources"))
				fmt.Printf("%-20s %.0f\n", "Sources:", toFloat(stats["sources"]))
				fmt.Printf("%-20s %.0f\n", "Destinations:", toFloat(stats["destinations"]))
				fmt.Printf("%-20s %.0f\n", "Pipelines:", toFloat(stats["pipelines"]))
				fmt.Println()

				fmt.Printf("%s\n", bold("Processing"))
				fmt.Printf("%-20s %.0f\n", "Events Processed:", toFloat(stats["events_processed"]))
				fmt.Printf("%-20s %s\n", "Events Failed:", red(fmt.Sprintf("%.0f", toFloat(stats["events_failed"]))))

				if jobs, ok := stats["jobs"].(map[string]any); ok {
					fmt.Println()
					fmt.Printf("%s\n", bold("Jobs"))
					fmt.Printf("%-20s %.0f\n", "Total:", toFloat(jobs["total"]))
					fmt.Printf("%-20s %s\n", "Pending:", yellow(fmt.Sprintf("%.0f", toFloat(jobs["pending"]))))
					fmt.Printf("%-20s %s\n", "Processing:", cyan(fmt.Sprintf("%.0f", toFloat(jobs["processing"]))))
					fmt.Printf("%-20s %s\n", "Completed:", green(fmt.Sprintf("%.0f", toFloat(jobs["completed"]))))
					fmt.Printf("%-20s %s\n", "Failed:", red(fmt.Sprintf("%.0f", toFloat(jobs["failed"]))))
				}
			}
			return nil
		},
	}
}
