package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/fatih/color"
	"gopkg.in/yaml.v3"
)

type Column struct {
	Header string
	Key    string
	Width  int // 0 = auto
}

var (
	green   = color.New(color.FgGreen).SprintFunc()
	red     = color.New(color.FgRed).SprintFunc()
	yellow  = color.New(color.FgYellow).SprintFunc()
	cyan    = color.New(color.FgCyan).SprintFunc()
	dim     = color.New(color.Faint).SprintFunc()
	bold    = color.New(color.Bold).SprintFunc()
	boldRed = color.New(color.Bold, color.FgRed).SprintFunc()
)

func colorStatus(s string) string {
	switch strings.ToLower(s) {
	case "active", "running", "completed", "healthy", "connected":
		return green(s)
	case "paused", "pending", "degraded", "warning":
		return yellow(s)
	case "processing":
		return cyan(s)
	case "failed", "error", "unhealthy":
		return red(s)
	case "cancelled", "stopped", "disabled":
		return dim(s)
	default:
		return s
	}
}

func colorEnabled(v any) string {
	switch b := v.(type) {
	case bool:
		if b {
			return green("Yes")
		}
		return red("No")
	default:
		return fmt.Sprintf("%v", v)
	}
}

func relativeTime(ts string) string {
	if ts == "" {
		return "-"
	}
	t, err := time.Parse(time.RFC3339Nano, ts)
	if err != nil {
		t, err = time.Parse(time.RFC3339, ts)
		if err != nil {
			// Try without timezone
			t, err = time.Parse("2006-01-02T15:04:05", ts)
			if err != nil {
				return ts
			}
		}
	}
	d := time.Since(t)
	if d < 0 {
		d = -d
	}
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds ago", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		days := int(math.Round(d.Hours() / 24))
		return fmt.Sprintf("%dd ago", days)
	}
}

func cellValue(item map[string]any, key string) string {
	// Support nested keys like "stats.jobs.completed"
	parts := strings.Split(key, ".")
	var current any = item
	for _, p := range parts {
		m, ok := current.(map[string]any)
		if !ok {
			return "-"
		}
		current, ok = m[p]
		if !ok {
			return "-"
		}
	}
	if current == nil {
		return "-"
	}
	return fmt.Sprintf("%v", current)
}

func printTable(items []map[string]any, columns []Column) {
	if len(items) == 0 {
		fmt.Println("No resources found.")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)

	// Header
	headers := make([]string, len(columns))
	for i, col := range columns {
		headers[i] = bold(col.Header)
	}
	fmt.Fprintln(w, strings.Join(headers, "\t"))

	// Rows
	for _, item := range items {
		vals := make([]string, len(columns))
		for i, col := range columns {
			v := cellValue(item, col.Key)
			switch col.Key {
			case "status", "_health":
				v = colorStatus(v)
			case "enabled":
				v = colorEnabled(item[col.Key])
			case "updated_at", "created_at":
				v = relativeTime(v)
			}
			if col.Width > 0 && len(v) > col.Width {
				v = v[:col.Width-3] + "..."
			}
			vals[i] = v
		}
		fmt.Fprintln(w, strings.Join(vals, "\t"))
	}
	w.Flush()
}

func printDetail(item map[string]any) {
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	for k, v := range item {
		if k == "status" {
			fmt.Fprintf(w, "%s:\t%s\n", bold(k), colorStatus(fmt.Sprintf("%v", v)))
		} else if k == "enabled" {
			fmt.Fprintf(w, "%s:\t%s\n", bold(k), colorEnabled(v))
		} else if k == "config" || k == "metadata" || k == "sources" || k == "result" || k == "stats" {
			data, _ := json.MarshalIndent(v, "  ", "  ")
			fmt.Fprintf(w, "%s:\t%s\n", bold(k), string(data))
		} else {
			fmt.Fprintf(w, "%s:\t%v\n", bold(k), v)
		}
	}
	w.Flush()
}

func printJSON(data any) {
	out, _ := json.MarshalIndent(data, "", "  ")
	fmt.Println(string(out))
}

func printYAML(data any) {
	// Convert through JSON to get clean types
	j, _ := json.Marshal(data)
	var clean any
	json.Unmarshal(j, &clean)
	out, _ := yaml.Marshal(clean)
	fmt.Print(string(out))
}

func outputResult(data any, columns []Column) {
	switch flagOutput {
	case "json":
		printJSON(data)
	case "yaml":
		printYAML(data)
	default:
		switch v := data.(type) {
		case []map[string]any:
			printTable(v, columns)
		case map[string]any:
			printDetail(v)
		default:
			printJSON(data)
		}
	}
}

func outputList(items []map[string]any, columns []Column) {
	switch flagOutput {
	case "json":
		printJSON(items)
	case "yaml":
		printYAML(items)
	default:
		printTablePaginated(items, columns)
	}
}

func printTablePaginated(items []map[string]any, columns []Column) {
	if len(items) == 0 {
		fmt.Println("No resources found.")
		return
	}
	perPage := flagPerPage
	if perPage <= 0 || len(items) <= perPage {
		printTable(items, columns)
		return
	}
	totalPages := (len(items) + perPage - 1) / perPage
	reader := bufio.NewReader(os.Stdin)
	for page := 0; page < totalPages; page++ {
		start := page * perPage
		end := start + perPage
		if end > len(items) {
			end = len(items)
		}
		printTable(items[start:end], columns)
		if page < totalPages-1 {
			fmt.Printf("\n%s", dim(fmt.Sprintf("Page %d/%d (%d items) — press Enter for next page, q to quit: ",
				page+1, totalPages, len(items))))
			input, _ := reader.ReadString('\n')
			input = strings.TrimSpace(input)
			if input == "q" || input == "Q" {
				return
			}
			fmt.Println()
		} else {
			fmt.Printf("\n%s\n", dim(fmt.Sprintf("Page %d/%d (%d items)", page+1, totalPages, len(items))))
		}
	}
}

func exitErr(msg string, args ...any) {
	fmt.Fprintf(os.Stderr, boldRed("Error: ")+msg+"\n", args...)
	os.Exit(1)
}

func exitOK(msg string, args ...any) {
	fmt.Printf(green("OK: ")+msg+"\n", args...)
}

func parseJSONList(data json.RawMessage, key string) []map[string]any {
	if key != "" {
		var wrapper map[string]json.RawMessage
		if err := json.Unmarshal(data, &wrapper); err == nil {
			if inner, ok := wrapper[key]; ok {
				data = inner
			}
		}
	}
	var items []map[string]any
	json.Unmarshal(data, &items)
	return items
}

func parseJSONObject(data json.RawMessage) map[string]any {
	var obj map[string]any
	json.Unmarshal(data, &obj)
	return obj
}
