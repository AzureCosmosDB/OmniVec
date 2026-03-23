package main

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
)

var tokenColumns = []Column{
	{Header: "ID", Key: "id"},
	{Header: "NAME", Key: "name"},
	{Header: "ROLE", Key: "role"},
	{Header: "CREATED", Key: "created_at"},
	{Header: "CREATED BY", Key: "created_by"},
}

func newAuthCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "auth",
		Short: "Manage authentication tokens",
	}
	cmd.AddCommand(
		newAuthLoginCmd(),
		newAuthCreateTokenCmd(),
		newAuthListTokensCmd(),
		newAuthRevokeTokenCmd(),
	)
	return cmd
}

func newAuthLoginCmd() *cobra.Command {
	var token, server string
	cmd := &cobra.Command{
		Use:   "login",
		Short: "Validate a token and save it to config",
		RunE: func(cmd *cobra.Command, args []string) error {
			if token == "" {
				exitErr("--token is required")
			}
			s := resolveServer(server)
			c := NewClient(s, token)
			data, err := c.Post("/api/auth/login", map[string]any{"token": token})
			if err != nil {
				exitErr("Login failed: %v", err)
			}
			resp := parseJSONObject(data)
			name, _ := resp["name"].(string)
			role, _ := resp["role"].(string)

			// Save token to config
			cfg := loadConfig()
			cfg.Token = token
			if s != defaultServer && server != "" {
				cfg.Server = s
			}
			if err := saveConfig(cfg); err != nil {
				exitErr("Failed to save config: %v", err)
			}
			exitOK("Logged in as %s (role: %s) — token saved to %s", name, role, configFile())
			return nil
		},
	}
	cmd.Flags().StringVar(&token, "token", "", "Access token")
	cmd.Flags().StringVar(&server, "server", "", "Server URL")
	return cmd
}

func newAuthCreateTokenCmd() *cobra.Command {
	var name, role string
	var expiresInDays int
	cmd := &cobra.Command{
		Use:   "create-token",
		Short: "Create a new access token (requires admin role)",
		RunE: func(cmd *cobra.Command, args []string) error {
			if name == "" {
				exitErr("--name is required")
			}
			body := map[string]any{
				"name": name,
				"role": role,
			}
			if expiresInDays > 0 {
				body["expires_in_days"] = expiresInDays
			}
			c := getClient()
			data, err := c.Post("/api/auth/tokens", body)
			if err != nil {
				exitErr("%v", err)
			}
			resp := parseJSONObject(data)
			if tok, ok := resp["token"].(string); ok {
				fmt.Println(bold("Token created — save this, it won't be shown again:"))
				fmt.Println()
				fmt.Println("  " + cyan(tok))
				fmt.Println()
				fmt.Printf("  ID:   %s\n", resp["id"])
				fmt.Printf("  Name: %s\n", resp["name"])
				fmt.Printf("  Role: %s\n", resp["role"])
			} else {
				var raw any
				json.Unmarshal(data, &raw)
				outputResult(raw, nil)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "Token name/description")
	cmd.Flags().StringVar(&role, "role", "user", "Token role (admin, user)")
	cmd.Flags().IntVar(&expiresInDays, "expires", 0, "Expiration in days (0 = no expiry)")
	return cmd
}

func newAuthListTokensCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "list-tokens",
		Short: "List all tokens (requires admin role)",
		RunE: func(cmd *cobra.Command, args []string) error {
			c := getClient()
			data, err := c.Get("/api/auth/tokens", nil)
			if err != nil {
				exitErr("%v", err)
			}
			items := parseJSONList(data, "tokens")
			outputList(items, tokenColumns)
			return nil
		},
	}
}

func newAuthRevokeTokenCmd() *cobra.Command {
	var yes bool
	cmd := &cobra.Command{
		Use:   "revoke-token <token-id>",
		Short: "Revoke an access token (requires admin role)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			id := args[0]
			if !yes && !confirmAction(fmt.Sprintf("Revoke token %s?", id)) {
				return nil
			}
			c := getClient()
			_, err := c.Delete(fmt.Sprintf("/api/auth/tokens/%s", id))
			if err != nil {
				exitErr("%v", err)
			}
			exitOK("Token %s revoked", id)
			return nil
		},
	}
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation")
	return cmd
}
