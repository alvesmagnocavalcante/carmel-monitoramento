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

// Valores padrão: intervalo de 60 s entre heartbeats e endereço da API em produção.
const (
	defaultInterval = 60 * time.Second
	defaultAPIURL   = "https://monitoramento-carmel.fastapicloud.dev"
)

// defaultAgentToken é o token de acesso à API, injetado na compilação com
// -X main.defaultAgentToken=<token>. Assim a credencial não fica no código-fonte.
var defaultAgentToken string

// Config reúne as configurações do agente, lidas do config.json (campos com "json").
// AgentID é calculado em tempo de execução e nunca gravado no arquivo ("-").
// Probe liga o "modo sonda" (ping em dispositivos sem agente); fica desligado por padrão
// e agentes antigos simplesmente ignoram o campo desconhecido.
type Config struct {
	APIURL          string `json:"api_url"`
	Group           string `json:"group"`
	Token           string `json:"token"`
	IntervalSeconds int    `json:"interval_seconds"`
	Probe           bool   `json:"probe"`
	AgentID         string `json:"-"`
}

// loadConfig monta a configuração final. Ordem de prioridade: opção --group, variáveis
// de ambiente, config.json e, por último, os padrões. Se algo foi preenchido
// automaticamente (URL, token, grupo), o config.json é regravado para a próxima execução.
func loadConfig(groupOverride string) (Config, error) {
	config := Config{IntervalSeconds: int(defaultInterval.Seconds())}
	path := configPath()
	// configChanged indica se é preciso salvar o arquivo ao final.
	configChanged := false

	// Arquivo inexistente não é erro (primeira execução); qualquer outra falha é.
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
	// O grupo vem da opção --group; se não houver, pergunta no console na primeira vez.
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
	// Remove a "/" final para não gerar "//" ao montar o endereço do heartbeat.
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

// promptGroup pergunta o grupo do computador ao técnico e lê a resposta (uma linha).
// Recebe leitor e escritor como parâmetros para poder ser testada sem teclado.
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

// saveConfig grava o config.json. A permissão 0o600 (só o dono lê) protege o token.
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

// stableAgentID gera um identificador fixo a partir do grupo e do nome do computador.
// Assim o mesmo PC mantém o mesmo ID entre reinícios, sem precisar gravá-lo em disco.
func stableAgentID(group, hostname string) string {
	value := strings.ToLower(strings.TrimSpace(group)) + "\x00" + strings.ToLower(hostname)
	sum := sha256.Sum256([]byte(value))
	return "agt_" + hex.EncodeToString(sum[:12])
}

// configPath decide onde está o config.json: variável CONFIG_PATH, depois a pasta
// atual e, por fim, a pasta do executável (o caso normal quando roda como serviço).
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

// applyEnvironment sobrescreve a configuração com variáveis de ambiente, quando definidas
// (API_URL, AGENT_GROUP, AGENT_TOKEN, INTERVAL_SECONDS, AGENT_PROBE). Útil para testes e ajustes rápidos.
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
	if value := os.Getenv("AGENT_PROBE"); value != "" {
		// ParseBool aceita true/false/1/0 (e variações como T/F); qualquer outra coisa é erro claro.
		probe, err := strconv.ParseBool(value)
		if err != nil {
			return fmt.Errorf("AGENT_PROBE must be true, false, 1 or 0: %w", err)
		}
		config.Probe = probe
	}
	return nil
}

// validate confere se a configuração final é utilizável, para falhar já na partida
// com uma mensagem clara em vez de errar a cada envio.
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
