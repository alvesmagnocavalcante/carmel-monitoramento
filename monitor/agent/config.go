package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	defaultInterval = 60 * time.Second
	defaultAPIURL   = "https://monitoramento-carmel.fastapicloud.dev"
)

// Set at build time with: -X main.defaultAgentToken=<token>.
// This keeps deployment credentials out of the source code.
var defaultAgentToken string

type Config struct {
	APIURL          string `json:"api_url"`
	Group           string `json:"group"`
	Token           string `json:"token"`
	IntervalSeconds int    `json:"interval_seconds"`
	AgentID         string `json:"-"`
}

func loadConfig(groupOverride string) (Config, error) {
	config := Config{IntervalSeconds: int(defaultInterval.Seconds())}
	path := configPath()
	configChanged := false

	data, err := os.ReadFile(path)
	if err == nil {
		if err := json.Unmarshal(data, &config); err != nil {
			return Config{}, fmt.Errorf("parse %s: %w", path, err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return Config{}, fmt.Errorf("read %s: %w", path, err)
	}

	if err := applyEnvironment(&config); err != nil {
		return Config{}, err
	}
	if strings.TrimSpace(config.APIURL) == "" {
		config.APIURL = defaultAPIURL
		configChanged = true
	}
	if strings.TrimSpace(config.Token) == "" && defaultAgentToken != "" {
		config.Token = defaultAgentToken
		configChanged = true
	}
	if strings.TrimSpace(groupOverride) != "" {
		config.Group = strings.TrimSpace(groupOverride)
		configChanged = true
	} else if strings.TrimSpace(config.Group) == "" {
		group, err := promptGroup(os.Stdin, os.Stdout)
		if err != nil {
			return Config{}, err
		}
		config.Group = group
		configChanged = true
	}
	if err := config.validate(); err != nil {
		return Config{}, err
	}
	config.APIURL = strings.TrimRight(config.APIURL, "/")
	if configChanged {
		if err := saveConfig(path, config); err != nil {
			return Config{}, err
		}
	}
	hostname, err := os.Hostname()
	if err != nil {
		return Config{}, fmt.Errorf("read hostname: %w", err)
	}
	config.AgentID = stableAgentID(config.Group, hostname)
	return config, nil
}

func promptGroup(reader io.Reader, writer io.Writer) (string, error) {
	if _, err := fmt.Fprint(writer, "Informe o grupo deste computador: "); err != nil {
		return "", fmt.Errorf("show group prompt: %w", err)
	}
	value, err := bufio.NewReader(reader).ReadString('\n')
	group := strings.TrimSpace(value)
	if err != nil && group == "" {
		return "", fmt.Errorf("read group: %w", err)
	}
	if group == "" {
		return "", errors.New("group cannot be empty")
	}
	return group, nil
}

func saveConfig(path string, config Config) error {
	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return fmt.Errorf("encode configuration: %w", err)
	}
	if err := os.WriteFile(path, append(data, '\n'), 0o600); err != nil {
		return fmt.Errorf("save %s: %w", path, err)
	}
	return nil
}

func stableAgentID(group, hostname string) string {
	value := strings.ToLower(strings.TrimSpace(group)) + "\x00" + strings.ToLower(hostname)
	sum := sha256.Sum256([]byte(value))
	return "agt_" + hex.EncodeToString(sum[:12])
}

func configPath() string {
	if path := os.Getenv("CONFIG_PATH"); path != "" {
		return path
	}
	if _, err := os.Stat("config.json"); err == nil {
		return "config.json"
	}
	executable, err := os.Executable()
	if err != nil {
		return "config.json"
	}
	return filepath.Join(filepath.Dir(executable), "config.json")
}

func applyEnvironment(config *Config) error {
	if value := os.Getenv("API_URL"); value != "" {
		config.APIURL = value
	}
	if value := os.Getenv("AGENT_GROUP"); value != "" {
		config.Group = value
	}
	if value := os.Getenv("AGENT_TOKEN"); value != "" {
		config.Token = value
	}
	if value := os.Getenv("INTERVAL_SECONDS"); value != "" {
		seconds, err := strconv.Atoi(value)
		if err != nil {
			return fmt.Errorf("INTERVAL_SECONDS must be an integer: %w", err)
		}
		config.IntervalSeconds = seconds
	}
	return nil
}

func (config Config) validate() error {
	parsedURL, err := url.ParseRequestURI(config.APIURL)
	if err != nil || (parsedURL.Scheme != "http" && parsedURL.Scheme != "https") || parsedURL.Host == "" {
		return errors.New("api_url must be a valid HTTP or HTTPS URL")
	}
	if strings.TrimSpace(config.Group) == "" {
		return errors.New(`group is required; run monitor-agent.exe --group "Nome do grupo"`)
	}
	if strings.TrimSpace(config.Token) == "" {
		return errors.New("token is required")
	}
	if config.IntervalSeconds < 1 {
		return errors.New("interval_seconds must be greater than zero")
	}
	return nil
}
