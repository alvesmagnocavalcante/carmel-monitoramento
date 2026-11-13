package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// errRejected marca uma recusa definitiva da API (ex.: 422): repetir o mesmo envio nunca resolve.
var errRejected = errors.New("heartbeat rejected")

// APIClient conversa com a API autenticando com o token: envia heartbeats (POST em
// /api/heartbeat) e, no modo sonda, busca alvos e envia resultados de ping.
type APIClient struct {
	baseURL    string
	token      string
	httpClient *http.Client
}

// newAPIClient monta o cliente. O limite de 30 s evita que um envio fique preso para sempre
// numa conexão que não responde.
func newAPIClient(apiURL, token string) *APIClient {
	return &APIClient{
		baseURL: apiURL,
		token:   token,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// send envia um heartbeat. Retorna nil se a API aceitou (2xx); erro comum se vale tentar
// de novo depois; ou erro marcado com errRejected se a API recusou de forma definitiva.
func (client *APIClient) send(ctx context.Context, heartbeat Heartbeat) error {
	body, err := json.Marshal(heartbeat)
	if err != nil {
		return fmt.Errorf("encode heartbeat: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, client.baseURL+"/api/heartbeat", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+client.token)
	request.Header.Set("Content-Type", "application/json")

	response, err := client.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("send heartbeat: %w", err)
	}
	defer response.Body.Close()

	// Qualquer status fora de 2xx é falha. Lê só 1 KB da resposta para registrar o motivo no log.
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		message, _ := io.ReadAll(io.LimitReader(response.Body, 1024))
		err := fmt.Errorf("API returned %s: %s", response.Status, bytes.TrimSpace(message))
		if isPermanentRejection(response.StatusCode) {
			return fmt.Errorf("%w: %w", errRejected, err)
		}
		return err
	}
	return nil
}

// isPermanentRejection diz se o status HTTP indica recusa definitiva. Erros 4xx são do
// conteúdo ou da credencial, e repetir não adianta; só 408 (tempo esgotado) e 429
// (muitas requisições) valem uma nova tentativa. Erros 5xx também são repetidos.
func isPermanentRejection(status int) bool {
	return status >= 400 && status < 500 &&
		status != http.StatusRequestTimeout && status != http.StatusTooManyRequests
}

// fetchProbeTargets pede à API a lista de dispositivos que esta sonda deve verificar.
// Lista vazia significa "nada a fazer neste ciclo". Qualquer falha vira erro e o chamador
// apenas registra no log.
func (client *APIClient) fetchProbeTargets(ctx context.Context, agentID string) ([]probeTarget, error) {
	endpoint := client.baseURL + "/api/probe/targets?agent_id=" + url.QueryEscape(agentID)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("create probe targets request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+client.token)

	response, err := client.httpClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("fetch probe targets: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		message, _ := io.ReadAll(io.LimitReader(response.Body, 1024))
		return nil, fmt.Errorf("API returned %s: %s", response.Status, bytes.TrimSpace(message))
	}
	var targets []probeTarget
	if err := json.NewDecoder(response.Body).Decode(&targets); err != nil {
		return nil, fmt.Errorf("decode probe targets: %w", err)
	}
	return targets, nil
}

// sendProbeResults envia todos os resultados do ciclo em um único POST. Não há nova
// tentativa: o próximo ciclo traz leituras novas, então qualquer falha só vira erro.
func (client *APIClient) sendProbeResults(ctx context.Context, agentID string, results []probeResult) error {
	body, err := json.Marshal(probeReport{ProbeID: agentID, Results: results})
	if err != nil {
		return fmt.Errorf("encode probe results: %w", err)
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, client.baseURL+"/api/probe/results", bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("create probe results request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+client.token)
	request.Header.Set("Content-Type", "application/json")

	response, err := client.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("send probe results: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		message, _ := io.ReadAll(io.LimitReader(response.Body, 1024))
		return fmt.Errorf("API returned %s: %s", response.Status, bytes.TrimSpace(message))
	}
	return nil
}
