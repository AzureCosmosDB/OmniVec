package main

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

func newMetricsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "metrics",
		Aliases: []string{"metric", "stats"},
		Short:   "Show pipeline and system metrics",
	}
	cmd.AddCommand(
		newMetricsSummaryCmd(),
		newMetricsInsightsCmd(),
	)
	return cmd
}

func newMetricsSummaryCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "summary",
		Short: "Show pipeline processing summary",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/metrics", nil)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput != "table" {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
				return nil
			}

			resp := parseJSONObject(data)
			processed := toFloat(resp["events_processed"])
			failed := toFloat(resp["events_failed"])
			avgTime := resp["avg_processing_time_ms"]

			fmt.Println("OmniVec Processing Metrics")
			fmt.Println("─────────────────────────────")
			fmt.Printf("Documents Processed: %.0f\n", processed)
			fmt.Printf("Documents Failed:    %.0f\n", failed)
			if avgTime != nil {
				fmt.Printf("Avg Processing Time: %.1fms\n", toFloat(avgTime))
			}

			if today, ok := resp["today"].(map[string]any); ok {
				fmt.Println("\nToday:")
				fmt.Printf("  Processed: %.0f\n", toFloat(today["processed"]))
				fmt.Printf("  Failed:    %.0f\n", toFloat(today["failed"]))
			}

			if pipelines, ok := resp["pipelines"].(map[string]any); ok && len(pipelines) > 0 {
				fmt.Println("\nPer Pipeline:")
				for id, v := range pipelines {
					if pm, ok := v.(map[string]any); ok {
						fmt.Printf("  %s: %.0f processed, %.0f failed\n",
							id, toFloat(pm["processed"]), toFloat(pm["failed"]))
					}
				}
			}
			return nil
		},
	}
}

func newMetricsInsightsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "insights",
		Short: "Show Application Insights metrics and pipeline health",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/metrics/insights", nil)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput != "table" {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
				return nil
			}

			resp := parseJSONObject(data)

			enabled, _ := resp["enabled"].(bool)
			if !enabled {
				fmt.Println("Application Insights: not configured")
				fmt.Println("  Deploy with azd up to enable telemetry.")
				return nil
			}

			fmt.Println("Application Insights: enabled")
			if ikey, ok := resp["instrumentation_key"].(string); ok {
				fmt.Printf("  Instrumentation Key: %s\n", ikey)
			}
			if url, ok := resp["portal_url"].(string); ok {
				fmt.Printf("  Azure Portal: %s\n", url)
			}

			// Custom metrics
			if cm, ok := resp["custom_metrics"].(map[string]any); ok && len(cm) > 0 {
				fmt.Println("\nTracked Metrics:")
				for k := range cm {
					fmt.Printf("  ✓ %s\n", k)
				}
			}

			// Pipeline health
			if pipelines, ok := resp["pipelines"].([]any); ok && len(pipelines) > 0 {
				fmt.Println("\nPipeline Health:")
				fmt.Printf("  %-16s %-20s %-8s %8s %8s %6s %10s\n", "ID", "NAME", "STATUS", "EMBEDDED", "FAILED", "PCT", "THROUGHPUT")
				for _, p := range pipelines {
					if pm, ok := p.(map[string]any); ok {
						name := pm["name"]
						if s, ok := name.(string); ok && len(s) > 18 {
							name = s[:18] + ".."
						}
						fmt.Printf("  %-16s %-20s %-8s %8.0f %8.0f %5.1f%% %8s\n",
							pm["id"], name, pm["status"],
							toFloat(pm["embedded_count"]),
							toFloat(pm["jobs_failed"]),
							toFloat(pm["completion_pct"]),
							formatThroughput(pm["throughput_docs_per_sec"]),
						)
					}
				}
			}
			return nil
		},
	}
}

func formatThroughput(v any) string {
	if v == nil {
		return "—"
	}
	f := toFloat(v)
	if f == 0 {
		return "—"
	}
	return fmt.Sprintf("%.1f/s", f)
}


