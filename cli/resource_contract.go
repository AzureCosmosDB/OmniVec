package main

import (
	"fmt"
	"sort"
	"strings"
)

var sourceConnectorFields = map[string][]string{
	"azure-blob":      {"account_url", "container"},
	"sharepoint":      {"site_id", "drive_id"},
	"onelake-iceberg": {"warehouse", "table"},
	"cosmosdb":        {"endpoint", "database", "container"},
	"postgresql":      {"host", "database", "table"},
}

var destinationConnectorFields = map[string][]string{
	"cosmosdb-vector": {"endpoint", "database", "container"},
	"pgvector":        {"host", "database", "table"},
	"onelake-iceberg": {
		"workspace_id",
		"lakehouse_item_id",
		"spark_job_definition_item_id",
		"staging_file_system",
		"target_table",
	},
}

func connectorFields(kind string) map[string][]string {
	if kind == "source" {
		return sourceConnectorFields
	}
	return destinationConnectorFields
}

func connectorTypeHelp(kind string) string {
	types := make([]string, 0, len(connectorFields(kind)))
	for connectorType := range connectorFields(kind) {
		types = append(types, connectorType)
	}
	sort.Strings(types)
	return strings.Join(types, ", ")
}

func validateConnectorConfig(kind, connectorType string, config map[string]any) error {
	required, supported := connectorFields(kind)[connectorType]
	if !supported {
		return fmt.Errorf("unsupported %s type %q (supported: %s)", kind, connectorType, connectorTypeHelp(kind))
	}
	var missing []string
	for _, field := range required {
		value, exists := config[field]
		if !exists || strings.TrimSpace(fmt.Sprint(value)) == "" {
			missing = append(missing, field)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("%s %q requires config field(s): %s", kind, connectorType, strings.Join(missing, ", "))
	}
	return nil
}
