package main

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/spf13/cobra"
)

type permissionReport struct {
	Status         string            `json:"status"`
	Summary        string            `json:"summary"`
	PrincipalID    string            `json:"principal_id"`
	IdentitySource string            `json:"identity_source"`
	ResourceID     string            `json:"resource_id"`
	Scope          string            `json:"scope"`
	Role           string            `json:"role"`
	Reason         string            `json:"reason"`
	Administrator  string            `json:"administrator"`
	Verification   string            `json:"verification"`
	Missing        []string          `json:"missing"`
	Limitations    []string          `json:"limitations"`
	Commands       map[string]string `json:"commands"`
}

func newPermissionsCmd(kind string) *cobra.Command {
	var resourceID, principalID, shell string
	cmd := &cobra.Command{
		Use:   "permissions <" + kind + "-id>",
		Short: "Explain access failures and print exact, customer-executed grant commands",
		Long: "Run a read-only access check. No role assignments or data changes are performed.\n" +
			"Supports managed-identity Cosmos sources/destinations and Blob sources.\n" +
			"Other connectors return explicit guidance without guessed commands.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if shell != "powershell" && shell != "bash" {
				return fmt.Errorf("--shell must be powershell or bash")
			}
			prefix := "src-"
			if kind == "destination" {
				prefix = "dst-"
			}
			id := ensurePrefix(args[0], prefix)
			data, err := getClient().Post("/api/permissions/check", map[string]any{
				"kind": kind, "resource_ref": id, "resource_id": resourceID, "principal_id": principalID,
			})
			if err != nil {
				return fmt.Errorf("access check unavailable: %w", err)
			}
			var report permissionReport
			if err := json.Unmarshal(data, &report); err != nil {
				return fmt.Errorf("invalid access-check response: %w", err)
			}
			if report.Status == "" || report.Summary == "" {
				return fmt.Errorf("incomplete access-check response")
			}
			if flagOutput == "json" || flagOutput == "yaml" {
				outputResult(parseJSONObject(data), nil)
			} else {
				cmd.Printf("%s: %s\n", strings.ToUpper(report.Status), report.Summary)
				for _, field := range [][2]string{
					{"Workload principal", report.PrincipalID}, {"Identity evidence", report.IdentitySource},
					{"Account resource", report.ResourceID}, {"Access scope", report.Scope},
					{"Required role", report.Role}, {"Why", report.Reason}, {"Who can run this", report.Administrator},
				} {
					if field[1] != "" {
						cmd.Printf("%s: %s\n", field[0], field[1])
					}
				}
				for _, missing := range report.Missing {
					cmd.Printf("Action needed: %s\n", missing)
				}
				if command := report.Commands[shell]; command != "" {
					cmd.Printf("\nReview, then run as the authorized administrator (%s):\n%s\n\n", shell, command)
				}
				cmd.Println(report.Verification)
				for _, limitation := range report.Limitations {
					cmd.Printf("Not verified: %s\n", limitation)
				}
				cmd.Printf("Recheck: omnivec %s permissions %s (reuse the same server and identity/resource flags)\n", kind, id)
			}
			if report.Status != "read_verified" {
				return fmt.Errorf("access not verified (%s)", report.Status)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&resourceID, "resource-id", "", "Full account ARM resource ID, if discovery is unavailable")
	cmd.Flags().StringVar(&principalID, "principal-id", "", "Workload identity Object/principal ID (not client ID), for older deployments")
	cmd.Flags().StringVar(&shell, "shell", "powershell", "Grant command syntax: powershell or bash")
	return cmd
}
