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
		Short:   "Show live pipeline and system metrics",
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
		Short: "Show live processing metrics (throughput, latency, progress)",
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

			fmt.Println("OmniVec Live Metrics")
			fmt.Println("════════════════════════════════════════")

			// Primary: throughput, latency, progress
			fmt.Println("\n▸ Pipeline Progress")
			fmt.Printf("  Documents Embedded:  %.0f\n", toFloat(resp["events_processed"]))
			fmt.Printf("  Documents Failed:    %.0f\n", toFloat(resp["events_failed"]))
			fmt.Printf("  Throughput:          %s\n", formatThroughput(resp["throughput_docs_per_sec"]))
			fmt.Printf("  Jobs Created:        %.0f\n", toFloat(resp["jobs_created"]))
			fmt.Printf("  Changefeed Batches:  %.0f\n", toFloat(resp["changefeed_batches"]))

			fmt.Println("\n▸ Latency (5-min window)")
			if lat, ok := resp["latency"].(map[string]any); ok {
				printLatency("  Embedding", lat["embedding"])
				printLatency("  Search   ", lat["search"])
				printLatency("  Request  ", lat["request"])
			}

			// Secondary: tokens, skips, errors
			if tok, ok := resp["tokens"].(map[string]any); ok {
				fmt.Println("\n▸ Token Usage")
				fmt.Printf("  Embedding: %s\n", fmtInt(tok["embedding"]))
				fmt.Printf("  Search:    %s\n", fmtInt(tok["search"]))
				fmt.Printf("  Total:     %s\n", fmtInt(tok["total"]))
			}

			if skip, ok := resp["skipped"].(map[string]any); ok {
				total := toFloat(skip["total"])
				if total > 0 {
					fmt.Println("\n▸ Skipped Documents")
					fmt.Printf("  No Content: %.0f\n", toFloat(skip["no_content"]))
					fmt.Printf("  Unchanged:  %.0f\n", toFloat(skip["unchanged"]))
				}
			}

			if errs, ok := resp["errors"].(map[string]any); ok {
				e4 := toFloat(errs["client_4xx"])
				e5 := toFloat(errs["server_5xx"])
				if e4+e5 > 0 {
					fmt.Println("\n▸ Errors")
					fmt.Printf("  Client (4xx): %.0f\n", e4)
					fmt.Printf("  Server (5xx): %.0f\n", e5)
					if ft, ok := errs["failure_types"].(map[string]any); ok && len(ft) > 0 {
						fmt.Println("  Failure Types:")
						for k, v := range ft {
							fmt.Printf("    %s: %.0f\n", k, toFloat(v))
						}
					}
				}
			}

			// Per-pipeline
			if pipelines, ok := resp["pipelines"].(map[string]any); ok && len(pipelines) > 0 {
				fmt.Println("\n▸ Per Pipeline")
				fmt.Printf("  %-20s %8s %8s %8s %8s\n", "PIPELINE", "EMBEDDED", "FAILED", "SKIPPED", "TOKENS")
				for id, v := range pipelines {
					if pm, ok := v.(map[string]any); ok {
						short := id
						if len(short) > 18 {
							short = short[:18] + ".."
						}
						skipped := toFloat(pm["skipped_no_content"]) + toFloat(pm["skipped_unchanged"])
						fmt.Printf("  %-20s %8.0f %8.0f %8.0f %8s\n",
							short, toFloat(pm["embedded"]), toFloat(pm["failed"]),
							skipped, fmtInt(pm["tokens"]))
					}
				}
			}

			fmt.Printf("\nUptime: %ds\n", int(toFloat(resp["uptime_seconds"])))
			return nil
		},
	}
}

func newMetricsInsightsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "insights",
		Short: "Show Application Insights connection status",
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
				fmt.Println("  In-memory metrics are active via: omnivec metrics summary")
				fmt.Println("  Deploy with azd up to enable App Insights (persistent, multi-replica).")
				return nil
			}

			fmt.Println("Application Insights: ● enabled")
			if ikey, ok := resp["instrumentation_key"].(string); ok {
				fmt.Printf("  Key:    %s\n", ikey)
			}
			if url, ok := resp["portal_url"].(string); ok {
				fmt.Printf("  Portal: %s\n", url)
			}
			fmt.Println("\n  All metrics are dual-written:")
			fmt.Println("  • In-memory → /api/metrics (real-time dashboard)")
			fmt.Println("  • App Insights (persistent, aggregated across replicas)")
			return nil
		},
	}
}

func printLatency(label string, v any) {
	if v == nil {
		fmt.Printf("%s: —\n", label)
		return
	}
	lat, ok := v.(map[string]any)
	if !ok || toFloat(lat["count"]) == 0 {
		fmt.Printf("%s: —\n", label)
		return
	}
	fmt.Printf("%s: avg=%.0fms  p95=%.0fms  p99=%.0fms  (n=%.0f)\n",
		label, toFloat(lat["avg"]), toFloat(lat["p95"]), toFloat(lat["p99"]), toFloat(lat["count"]))
}

func formatThroughput(v any) string {
	if v == nil {
		return "—"
	}
	f := toFloat(v)
	if f == 0 {
		return "—"
	}
	return fmt.Sprintf("%.1f docs/sec", f)
}

func fmtInt(v any) string {
	f := toFloat(v)
	if f == 0 {
		return "0"
	}
	if f >= 1000000 {
		return fmt.Sprintf("%.1fM", f/1000000)
	}
	if f >= 1000 {
		return fmt.Sprintf("%.1fK", f/1000)
	}
	return fmt.Sprintf("%.0f", f)
}

