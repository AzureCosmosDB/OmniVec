package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

var jobColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "PIPELINE", Key: "pipeline_id"},
	{Header: "SOURCE REF", Key: "source_ref", Width: 45},
	{Header: "STATUS", Key: "status"},
	{Header: "ERROR", Key: "error", Width: 35},
	{Header: "CREATED", Key: "created_at"},
}

func newJobCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "job",
		Aliases: []string{"jobs"},
		Short:   "Manage jobs",
	}
	cmd.AddCommand(
		newJobListCmd(),
		newJobShowCmd(),
		newJobCancelCmd(),
		newJobRetryCmd(),
		newJobStatsCmd(),
	)
	return cmd
}

func newJobListCmd() *cobra.Command {
	var pipeline, status string
	var limit int
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List jobs",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			params := map[string]string{}
			if pipeline != "" {
				params["pipeline_id"] = ensurePrefix(pipeline, "pip-")
			}
			if status != "" {
				params["status"] = status
			}
			if limit > 0 {
				params["limit"] = fmt.Sprintf("%d", limit)
			}
			data, err := c.Get("/api/jobs", params)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "jobs")
			outputList(items, jobColumns)
			return nil
		},
	}
	cmd.Flags().StringVar(&pipeline, "pipeline", "", "Filter by pipeline ID")
	cmd.Flags().StringVar(&status, "status", "", "Filter by status (pending, processing, completed, failed, cancelled)")
	cmd.Flags().IntVarP(&limit, "limit", "n", 100, "Max results")
	return cmd
}

func newJobShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <job-id>",
		Short: "Show job details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "job-")
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/jobs/%s", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)
			outputResult(obj, jobColumns)
			return nil
		},
	}
}

func newJobCancelCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel <job-id>",
		Short: "Cancel a pending job",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "job-")
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/jobs/%s/cancel", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Job %s cancelled", id)
			return nil
		},
	}
}

func newJobRetryCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "retry <job-id>",
		Short: "Retry a failed job",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := ensurePrefix(args[0], "job-")
			c := getClient()
			_, err := c.Post(fmt.Sprintf("/api/jobs/%s/retry", id), nil)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Job %s reset to PENDING", id)
			return nil
		},
	}
}

func newJobStatsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "stats",
		Short: "Show job statistics",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/jobs/stats", nil)
			if err != nil {
				exitErr("%v", err)
			}
			obj := parseJSONObject(data)
			if flagOutput != "table" {
				outputResult(obj, nil)
			} else {
				fmt.Printf("Total:      %v\n", obj["total"])
				fmt.Printf("Pending:    %s\n", yellow(fmt.Sprintf("%v", obj["pending"])))
				fmt.Printf("Processing: %s\n", cyan(fmt.Sprintf("%v", obj["processing"])))
				fmt.Printf("Completed:  %s\n", green(fmt.Sprintf("%v", obj["completed"])))
				fmt.Printf("Failed:     %s\n", red(fmt.Sprintf("%v", obj["failed"])))
			}
			return nil
		},
	}
}
