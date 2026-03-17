package main

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

const defaultServer = "http://localhost:8080"

type Config struct {
	Server string `yaml:"server"`
}

func configDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".omnivec")
}

func configFile() string {
	return filepath.Join(configDir(), "config.yaml")
}

func loadConfig() Config {
	var cfg Config
	data, err := os.ReadFile(configFile())
	if err != nil {
		return cfg
	}
	yaml.Unmarshal(data, &cfg)
	return cfg
}

func saveConfig(cfg Config) error {
	if err := os.MkdirAll(configDir(), 0755); err != nil {
		return err
	}
	data, err := yaml.Marshal(cfg)
	if err != nil {
		return err
	}
	return os.WriteFile(configFile(), data, 0644)
}

func resolveServer(override string) string {
	if override != "" {
		return override
	}
	if env := os.Getenv("OMNIVEC_SERVER"); env != "" {
		return env
	}
	cfg := loadConfig()
	if cfg.Server != "" {
		return cfg.Server
	}
	return defaultServer
}

func printConfigView() {
	cfg := loadConfig()
	server := resolveServer(flagServer)
	source := "default"
	if flagServer != "" {
		source = "--server flag"
	} else if os.Getenv("OMNIVEC_SERVER") != "" {
		source = "OMNIVEC_SERVER env var"
	} else if cfg.Server != "" {
		source = configFile()
	}
	fmt.Printf("Server:  %s\n", server)
	fmt.Printf("Source:  %s\n", source)
	if cfg.Server != "" {
		fmt.Printf("Config:  %s\n", configFile())
	}
}
