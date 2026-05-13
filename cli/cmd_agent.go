package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

// newAgentCmd builds the `omnivec agent` command tree.
//
// Phase 1 of the OmniVec Agent: a read-only diagnostic AI-ops agent. The CLI
// exposes three thin wrappers around the API proxy at /api/agent/*:
//
//	omnivec agent chat       — open an SSE chat stream and print events
//	omnivec agent sessions   — list/get/delete persisted sessions
//	omnivec agent tools      — list tools the agent can call (gated by role)
func newAgentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "agent",
		Short: "Interact with the OmniVec diagnostic Agent (Phase 1 — read-only)",
	}
	cmd.AddCommand(newAgentChatCmd(), newAgentSessionsCmd(), newAgentToolsCmd())
	return cmd
}

func newAgentChatCmd() *cobra.Command {
	var sessionID, model string
	cmd := &cobra.Command{
		Use:   "chat <message>",
		Short: "Send a chat message and stream Agent events (SSE)",
		Args:  cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			payload := map[string]any{"message": strings.Join(args, " ")}
			if sessionID != "" {
				payload["session_id"] = sessionID
			}
			if model != "" {
				payload["model"] = model
			}
			return streamAgentChat(c, payload)
		},
	}
	cmd.Flags().StringVar(&sessionID, "session", "", "Reuse an existing session id")
	cmd.Flags().StringVar(&model, "model", "", "Override the default model deployment")
	return cmd
}

// streamAgentChat opens an SSE connection to /api/agent/chat, decodes the
// `event:` / `data:` framing line-by-line, and prints a human-readable
// transcript to stdout. Errors are surfaced as non-zero exit codes.
func streamAgentChat(c *Client, payload map[string]any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}
	req, err := http.NewRequest("POST", c.BaseURL+"/api/agent/chat", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("agent chat: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		buf, _ := readAll(resp.Body)
		return fmt.Errorf("agent chat: HTTP %d: %s", resp.StatusCode, string(buf))
	}

	r := bufio.NewReader(resp.Body)
	var eventType string
	for {
		line, err := r.ReadString('\n')
		if len(line) > 0 {
			line = strings.TrimRight(line, "\r\n")
			switch {
			case strings.HasPrefix(line, "event:"):
				eventType = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
			case strings.HasPrefix(line, "data:"):
				data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
				printAgentEvent(eventType, data)
				if eventType == "done" {
					return nil
				}
			}
		}
		if err != nil {
			return nil
		}
	}
}

func printAgentEvent(eventType, data string) {
	switch eventType {
	case "token":
		var m struct{ Text string }
		_ = json.Unmarshal([]byte(data), &m)
		fmt.Print(m.Text)
	case "tool_call":
		fmt.Fprintf(os.Stderr, "\n  → tool_call: %s\n", data)
	case "tool_result":
		fmt.Fprintf(os.Stderr, "  ← tool_result: %s\n", data)
	case "final":
		fmt.Println()
	case "error":
		fmt.Fprintf(os.Stderr, "\n[error] %s\n", data)
	case "session":
		fmt.Fprintf(os.Stderr, "[session] %s\n", data)
	}
}

func readAll(r interface{ Read([]byte) (int, error) }) ([]byte, error) {
	buf := make([]byte, 0, 1024)
	tmp := make([]byte, 1024)
	for {
		n, err := r.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			return buf, err
		}
	}
}

func newAgentSessionsCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "sessions", Short: "Manage agent sessions"}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "list",
			Short: "List your agent sessions",
			RunE: func(_ *cobra.Command, _ []string) error {
				raw, err := getClient().Get("/api/agent/sessions", nil)
				if err != nil {
					return err
				}
				return printAgentJSON(raw)
			},
		},
		&cobra.Command{
			Use:   "show <id>",
			Short: "Show a single agent session",
			Args:  cobra.ExactArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				raw, err := getClient().Get("/api/agent/sessions/"+args[0], nil)
				if err != nil {
					return err
				}
				return printAgentJSON(raw)
			},
		},
		&cobra.Command{
			Use:   "delete <id>",
			Short: "Delete an agent session",
			Args:  cobra.ExactArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				_, err := getClient().Delete("/api/agent/sessions/" + args[0])
				return err
			},
		},
	)
	return cmd
}

func newAgentToolsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "tools",
		Short: "List tools the Agent can call",
		RunE: func(_ *cobra.Command, _ []string) error {
			raw, err := getClient().Get("/api/agent/tools", nil)
			if err != nil {
				return err
			}
			return printAgentJSON(raw)
		},
	}
}

func printAgentJSON(raw json.RawMessage) error {
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, raw, "", "  "); err != nil {
		fmt.Println(string(raw))
		return nil
	}
	fmt.Println(pretty.String())
	return nil
}
