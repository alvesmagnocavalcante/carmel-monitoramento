package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// errRejected marca uma recusa definitiva da API (ex.: 422): repetir o mesmo envio nunca resolve.
var errRejected = errors.New("heartbeat rejected")

type APIClient struct {
	url        string
	token      string
	httpClient *http.Client
}

func newAPIClient(apiURL, token string) *APIClient {
	return &APIClient{
		url:   apiURL + "/api/heartbeat",
		token: token,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (client *APIClient) send(ctx context.Context, heartbeat Heartbeat) error {
	body, err := json.Marshal(heartbeat)
	if err != nil {
		return fmt.Errorf("encode heartbeat: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, client.url, bytes.NewReader(body))
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

// Erros 4xx são do conteúdo ou da credencial; só 408 e 429 valem uma nova tentativa.
func isPermanentRejection(status int) bool {
	return status >= 400 && status < 500 &&
		status != http.StatusRequestTimeout && status != http.StatusTooManyRequests
}
