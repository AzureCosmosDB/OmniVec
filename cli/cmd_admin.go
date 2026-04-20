package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

func newAdminCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "admin",
		Short: "Administrative operations (export/import deployment data)",
	}
	cmd.AddCommand(newExportCmd(), newImportCmd())
	return cmd
}

func newExportCmd() *cobra.Command {
	var (
		outFile            string
		include            string
		includeSecrets     bool
		includeCheckpoints bool
		pipelines          string
	)
	cmd := &cobra.Command{
		Use:   "export",
		Short: "Export sources, destinations, pipelines, models, assistants (and optionally checkpoints) as a JSON bundle",
		Long: `Export an OmniVec deployment to a JSON bundle for backup or migration.

By default, secrets (connection strings, API keys, passwords) are redacted ("***").
Use --include-secrets to include real values.

Examples:
  omnivec admin export --output omnivec.json
  omnivec admin export --output pips.json --pipelines pip-123,pip-456 --include-checkpoints
  omnivec admin export --include-secrets --output full-backup.json`,
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			params := map[string]string{
				"include":             include,
				"include_secrets":     boolStr(includeSecrets),
				"include_checkpoints": boolStr(includeCheckpoints),
			}
			if pipelines != "" {
				params["pipeline_ids"] = pipelines
			}
			data, err := c.Get("/api/admin/export", params)
			if err != nil {
				exitErr("%v", err)
			}

			// Pretty-print the bundle
			var obj any
			if err := json.Unmarshal(data, &obj); err == nil {
				pretty, _ := json.MarshalIndent(obj, "", "  ")
				data = pretty
			}

			if outFile == "" || outFile == "-" {
				fmt.Println(string(data))
			} else {
				if err := os.WriteFile(outFile, data, 0o600); err != nil {
					exitErr("Write %s: %v", outFile, err)
				}
				// Summary to stderr so redirection still works cleanly
				summarizeExport(data)
				fmt.Fprintf(os.Stderr, "%s Wrote %s (%d bytes)\n", green("OK:"), outFile, len(data))
			}
			return nil
		},
	}
	cmd.Flags().StringVarP(&outFile, "output-file", "f", "", "Write bundle to this path (default: stdout)")
	cmd.Flags().StringVar(&include, "include", "sources,destinations,pipelines,models,assistants",
		"Comma-separated resource types to include")
	cmd.Flags().BoolVar(&includeSecrets, "include-secrets", false,
		"Include real secrets instead of redacting them")
	cmd.Flags().BoolVar(&includeCheckpoints, "include-checkpoints", false,
		"Include pipeline/source checkpoints so runs can be resumed after import")
	cmd.Flags().StringVar(&pipelines, "pipelines", "",
		"Only export these pipeline IDs (csv) and their dependencies")
	return cmd
}

func newImportCmd() *cobra.Command {
	var (
		onConflict string
		dryRun     bool
	)
	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Import a JSON bundle produced by 'omnivec admin export'",
		Long: `Import OmniVec deployment data from a JSON bundle.

Conflict handling (--on-conflict):
  skip      - leave existing resources untouched (default, safest)
  overwrite - replace existing resources with the imported version
  rename    - create new copies with suffixed IDs & names (cross-refs rewritten)

Imported pipelines are always created in the 'paused' state; resume them
explicitly after import.

Examples:
  omnivec admin import omnivec.json --dry-run
  omnivec admin import omnivec.json --on-conflict overwrite
  omnivec admin import pips.json    --on-conflict rename`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			path := args[0]
			raw, err := os.ReadFile(path)
			if err != nil {
				exitErr("Read %s: %v", path, err)
			}
			var bundle any
			if err := json.Unmarshal(raw, &bundle); err != nil {
				exitErr("Parse %s: %v", path, err)
			}

			c := getClient()
			qs := fmt.Sprintf("?on_conflict=%s&dry_run=%s", onConflict, boolStr(dryRun))
			data, err := c.Post("/api/admin/import"+qs, bundle)
			if err != nil {
				exitErr("%v", err)
			}

			if flagOutput == "json" {
				printJSON(json.RawMessage(data))
				return nil
			}
			printImportSummary(data, dryRun)
			return nil
		},
	}
	cmd.Flags().StringVar(&onConflict, "on-conflict", "skip",
		"Conflict mode: skip | overwrite | rename")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"Don't write anything; just report what would happen")
	return cmd
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

// summarizeExport prints a short per-type count summary to stderr.
func summarizeExport(data []byte) {
	var obj map[string]any
	if err := json.Unmarshal(data, &obj); err != nil {
		return
	}
	res, _ := obj["resources"].(map[string]any)
	var parts []string
	for _, k := range []string{"sources", "destinations", "pipelines", "models", "assistants"} {
		if arr, ok := res[k].([]any); ok {
			parts = append(parts, fmt.Sprintf("%s=%d", k, len(arr)))
		}
	}
	if cps, ok := obj["checkpoints"].([]any); ok && len(cps) > 0 {
		parts = append(parts, fmt.Sprintf("checkpoints=%d", len(cps)))
	}
	if redacted, _ := obj["includes_secrets"].(bool); !redacted {
		parts = append(parts, "secrets=redacted")
	} else {
		parts = append(parts, "secrets=included")
	}
	fmt.Fprintf(os.Stderr, "%s %s\n", dim("bundle:"), strings.Join(parts, " "))
}

func printImportSummary(data json.RawMessage, dryRun bool) {
	var obj map[string]any
	if err := json.Unmarshal(data, &obj); err != nil {
		fmt.Println(string(data))
		return
	}
	if dryRun {
		fmt.Println(bold("Dry run — no changes applied."))
	}
	mode, _ := obj["on_conflict"].(string)
	fmt.Printf("Conflict mode: %s\n\n", bold(mode))

	summary, _ := obj["summary"].(map[string]any)
	order := []string{"sources", "destinations", "models", "assistants", "pipelines", "checkpoints"}
	fmt.Printf("%-14s %8s %12s %8s %8s\n", bold("Resource"), bold("Created"), bold("Overwritten"), bold("Skipped"), bold("Renamed"))
	for _, k := range order {
		s, ok := summary[k].(map[string]any)
		if !ok {
			continue
		}
		fmt.Printf("%-14s %8v %12v %8v %8v\n", k,
			intish(s["created"]), intish(s["overwritten"]),
			intish(s["skipped"]), intish(s["renamed"]))
	}

	if warns, _ := obj["warnings"].([]any); len(warns) > 0 {
		fmt.Printf("\n%s\n", yellow(fmt.Sprintf("Warnings (%d):", len(warns))))
		for _, w := range warns {
			fmt.Printf("  - %v\n", w)
		}
	}
	// Errors
	for _, k := range order {
		s, ok := summary[k].(map[string]any)
		if !ok {
			continue
		}
		if errs, _ := s["errors"].([]any); len(errs) > 0 {
			fmt.Printf("\n%s in %s:\n", red("Errors"), k)
			for _, e := range errs {
				fmt.Printf("  - %v\n", e)
			}
		}
	}
	if idMap, _ := obj["id_map"].(map[string]any); len(idMap) > 0 {
		fmt.Printf("\n%s\n", bold("Renamed IDs:"))
		for k, v := range idMap {
			fmt.Printf("  %s -> %v\n", k, v)
		}
	}
}

func intish(v any) any {
	if v == nil {
		return 0
	}
	return v
}
