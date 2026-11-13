package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Garante que o ID do agente é repetível para o mesmo grupo e PC, e muda se o grupo mudar.
func TestStableAgentIDUsesGroupAndHostname(t *testing.T) {
	first := stableAgentID("Hotel Carmel", "RECEPCAO-01")
	second := stableAgentID("Hotel Carmel", "RECEPCAO-01")
	otherGroup := stableAgentID("Mercantil Centro", "RECEPCAO-01")

	if first != second {
		t.Fatal("agent ID must be stable")
	}
	if first == otherGroup {
		t.Fatal("agent ID must differ between groups")
	}
}

// Simula o técnico digitando o grupo no console e confere o texto lido.
func TestPromptGroup(t *testing.T) {
	group, err := promptGroup(strings.NewReader("Hotel Carmel\n"), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if group != "Hotel Carmel" {
		t.Fatalf("unexpected group: %q", group)
	}
}

// Confere que o grupo passado por --group é usado e gravado no config.json.
// Usa uma pasta temporária para não mexer em nenhum arquivo real.
func TestLoadConfigPersistsGroupOverride(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	initial := Config{
		APIURL:          "http://127.0.0.1:8009",
		Token:           "test-token",
		IntervalSeconds: 10,
	}
	data, err := json.Marshal(initial)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	t.Setenv("CONFIG_PATH", path)
	config, err := loadConfig("Hotel Carmel")
	if err != nil {
		t.Fatal(err)
	}
	if config.Group != "Hotel Carmel" {
		t.Fatalf("unexpected group: %q", config.Group)
	}

	saved, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var persisted Config
	if err := json.Unmarshal(saved, &persisted); err != nil {
		t.Fatal(err)
	}
	if persisted.Group != "Hotel Carmel" {
		t.Fatalf("group was not persisted: %q", persisted.Group)
	}
}

// Confere que URL e token vazios no arquivo são preenchidos com os padrões embutidos
// no executável, e que o resultado é gravado de volta no config.json.
func TestLoadConfigFillsEmbeddedDefaults(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	data := []byte(`{"api_url":"","group":"Taiba","token":"","interval_seconds":10}`)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	previousToken := defaultAgentToken
	defaultAgentToken = "embedded-test-token"
	t.Cleanup(func() { defaultAgentToken = previousToken })
	t.Setenv("CONFIG_PATH", path)

	config, err := loadConfig("")
	if err != nil {
		t.Fatal(err)
	}
	if config.APIURL != defaultAPIURL || config.Token != "embedded-test-token" {
		t.Fatalf("embedded defaults were not applied: %+v", config)
	}

	saved, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var persisted Config
	if err := json.Unmarshal(saved, &persisted); err != nil {
		t.Fatal(err)
	}
	if persisted.APIURL != defaultAPIURL || persisted.Token != "embedded-test-token" {
		t.Fatalf("embedded defaults were not persisted: %+v", persisted)
	}
}
