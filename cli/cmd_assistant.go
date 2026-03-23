package main

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

var assistantColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "NAME", Key: "name"},
	{Header: "MODEL", Key: "model"},
	{Header: "DESTINATION", Key: "destination_id"},
	{Header: "CREATED", Key: "created_at"},
}

func newAssistantCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "assistant",
		Aliases: []string{"assistants"},
		Short:   "Manage RAG assistants",
	}
	cmd.AddCommand(
		newAssistantListCmd(),
		newAssistantShowCmd(),
		newAssistantCreateCmd(),
		newAssistantUpdateCmd(),
		newAssistantDeleteCmd(),
		newAssistantChatCmd(),
	)
	return cmd
}

func newAssistantListCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List all assistants",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/assistants", nil)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "assistants")
			outputList(items, assistantColumns)
			return nil
		},
	}
}

func newAssistantShowCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "show <assistant-id>",
		Short: "Show assistant details",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get(fmt.Sprintf("/api/assistants/%s", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			var raw any
			json.Unmarshal(data, &raw)
			outputResult(raw, assistantColumns)
			return nil
		},
	}
}

func newAssistantCreateCmd() *cobra.Command {
	var name, model, destination, systemPrompt string
	var topK int
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a new assistant",
		RunE: func(cmd *cobra.Command, args []string) error {
			if name == "" {
				exitErr("--name is required")
			}
			if destination == "" {
				exitErr("--destination is required")
			}
			body := map[string]any{
				"name":           name,
				"destination_id": ensurePrefix(destination, "dst-"),
			}
			if model != "" {
				body["model"] = model
			}
			if systemPrompt != "" {
				body["system_prompt"] = systemPrompt
			}
			if topK > 0 {
				body["top_k"] = topK
			}
			c := getClient()
			data, err := c.Post("/api/assistants", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if a, ok := resp["assistant"].(map[string]any); ok {
				exitOK("Assistant created: %s", a["id"])
			} else {
				exitOK("Assistant created")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Assistant name")
	cmd.Flags().StringVar(&model, "model", "", "LLM model to use")
	cmd.Flags().StringVar(&destination, "destination", "", "Vector store destination ID")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "", "System prompt")
	cmd.Flags().IntVar(&topK, "top-k", 5, "Number of context chunks to retrieve")
	return cmd
}

func newAssistantUpdateCmd() *cobra.Command {
	var name, model, systemPrompt string
	var topK int
	cmd := &cobra.Command{
		Use:   "update <assistant-id>",
		Short: "Update an assistant",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			existing, err := c.Get(fmt.Sprintf("/api/assistants/%s", args[0]), nil)
			if err != nil {
				exitErr("%v", err)
			}
			body := parseJSONObject(existing)
			delete(body, "id")
			delete(body, "created_at")
			delete(body, "updated_at")
			if name != "" {
				body["name"] = name
			}
			if model != "" {
				body["model"] = model
			}
			if systemPrompt != "" {
				body["system_prompt"] = systemPrompt
			}
			if topK > 0 {
				body["top_k"] = topK
			}
			_, err = c.Put(fmt.Sprintf("/api/assistants/%s", args[0]), body)
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Assistant %s updated", args[0])
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Assistant name")
	cmd.Flags().StringVar(&model, "model", "", "LLM model")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "", "System prompt")
	cmd.Flags().IntVar(&topK, "top-k", 0, "Number of context chunks")
	return cmd
}

func newAssistantDeleteCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "delete <assistant-id>",
		Short: "Delete an assistant",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !yes && !confirmAction(fmt.Sprintf("Delete assistant %s?", args[0])) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/assistants/%s", args[0]))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Assistant %s deleted", args[0])
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}

func newAssistantChatCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "chat <assistant-id> <message>",
		Short: "Chat with an assistant",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			body := map[string]any{"message": args[1]}
			c := getClient()
			data, err := c.Post(fmt.Sprintf("/api/assistants/%s/chat", args[0]), body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if reply, ok := resp["reply"].(string); ok {
				fmt.Println(reply)
			} else {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
			}
			return nil
		},
	}
}
