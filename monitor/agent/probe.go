package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"net"
	"strconv"
	"sync"
	"time"
)

// Limites da sonda. O prazo do ciclo evita que alvos lentos atrasem o ciclo seguinte;
// a concorrência limitada evita abrir centenas de conexões de uma vez numa rede de hotel;
// 500 é o máximo de resultados que a API aceita por envio.
const (
	probeCycleTimeout = 25 * time.Second
	probeConcurrency  = 10
	probeMaxTargets   = 500
	probeICMPTimeout  = 2 * time.Second
	probeTCPTimeout   = 3 * time.Second
)

// probeTarget é um dispositivo a verificar, como a API o descreve.
// Port é ponteiro porque só existe no método "tcp" (no "ping" vem null).
type probeTarget struct {
	ID      string `json:"id"`
	Address string `json:"address"`
	Method  string `json:"method"`
	Port    *int   `json:"port"`
}

// probeResult é a leitura de um alvo. LatencyMS é ponteiro e omitido quando ok=false.
type probeResult struct {
	DeviceID  string   `json:"device_id"`
	OK        bool     `json:"ok"`
	LatencyMS *float64 `json:"latency_ms,omitempty"`
}

// probeReport é o corpo do POST de resultados.
type probeReport struct {
	ProbeID string        `json:"probe_id"`
	Results []probeResult `json:"results"`
}

// checkTarget faz a verificação de um alvo. É variável de pacote para que os testes
// possam trocá-la por uma versão falsa, sem depender de rede.
var checkTarget = checkTargetReal

// runProbe repete o ciclo da sonda a cada intervalo (o mesmo do heartbeat) até ctx ser
// cancelado. Roda o primeiro ciclo logo na partida, sem esperar o primeiro intervalo.
func runProbe(ctx context.Context, client *APIClient, agentID string, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		runProbeCycle(ctx, client, agentID)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// runProbeCycle faz um ciclo: busca alvos, verifica todos e envia um único POST.
// Não há fila de reenvio de propósito: um resultado de ping envelhece em segundos e o
// próximo ciclo já traz uma leitura nova; reenviar atrasado só mostraria dado velho.
func runProbeCycle(ctx context.Context, client *APIClient, agentID string) {
	targets, err := client.fetchProbeTargets(ctx, agentID)
	if err != nil {
		log.Printf("probe: %v", err)
		return
	}
	if len(targets) == 0 {
		return
	}
	if len(targets) > probeMaxTargets {
		log.Printf("probe: %d targets received; checking only the first %d", len(targets), probeMaxTargets)
		targets = targets[:probeMaxTargets]
	}

	cycleCtx, cancel := context.WithTimeout(ctx, probeCycleTimeout)
	results := checkAll(cycleCtx, targets)
	cancel()

	if err := client.sendProbeResults(ctx, agentID, results); err != nil {
		log.Printf("probe: %v", err)
		return
	}
	up := 0
	for _, result := range results {
		if result.OK {
			up++
		}
	}
	log.Printf("probe results sent; targets=%d up=%d", len(results), up)
}

// checkAll verifica os alvos em paralelo, no máximo probeConcurrency por vez, e devolve
// um resultado por alvo, na mesma ordem. Alvos que não chegaram a rodar antes do prazo
// do ciclo contam como falha (ok=false).
func checkAll(ctx context.Context, targets []probeTarget) []probeResult {
	results := make([]probeResult, len(targets))
	slots := make(chan struct{}, probeConcurrency)
	var group sync.WaitGroup
	for index, target := range targets {
		results[index] = probeResult{DeviceID: target.ID}
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			// Prazo esgotado: os alvos restantes ficam com ok=false.
			continue
		}
		group.Add(1)
		go func() {
			defer group.Done()
			defer func() { <-slots }()
			// Um pânico em um alvo não pode derrubar os demais; o alvo fica ok=false.
			defer func() {
				if recovered := recover(); recovered != nil {
					log.Printf("probe: check of %s panicked: %v", target.ID, recovered)
					results[index] = probeResult{DeviceID: target.ID}
				}
			}()
			results[index] = checkTarget(ctx, target)
		}()
	}
	group.Wait()
	return results
}

// checkTargetReal executa a verificação real conforme o método do alvo.
func checkTargetReal(_ context.Context, target probeTarget) probeResult {
	result := probeResult{DeviceID: target.ID}
	var latency float64
	var err error
	switch target.Method {
	case "tcp":
		latency, err = checkTCP(target)
	case "ping":
		latency, err = checkPing(target.Address)
	default:
		err = fmt.Errorf("unknown method %q", target.Method)
	}
	if err != nil {
		return result
	}
	result.OK = true
	result.LatencyMS = &latency
	return result
}

// checkTCP tenta abrir uma conexão em endereço:porta e mede o tempo. Conectou = no ar.
func checkTCP(target probeTarget) (float64, error) {
	if target.Port == nil || *target.Port < 1 || *target.Port > 65535 {
		return 0, fmt.Errorf("invalid port for %s", target.ID)
	}
	address := net.JoinHostPort(target.Address, strconv.Itoa(*target.Port))
	start := time.Now()
	connection, err := net.DialTimeout("tcp", address, probeTCPTimeout)
	if err != nil {
		return 0, fmt.Errorf("connect %s: %w", address, err)
	}
	elapsed := time.Since(start)
	connection.Close()
	return roundMS(elapsed), nil
}

// checkPing resolve o endereço para IPv4 e envia um ICMP echo. O ICMP em si é
// específico de cada sistema (ver ping_windows.go e ping_other.go).
func checkPing(address string) (float64, error) {
	ip, err := resolveIPv4(address)
	if err != nil {
		return 0, err
	}
	return pingICMP(ip, probeICMPTimeout)
}

// resolveIPv4 devolve o IPv4 do endereço: literal vai direto; nome é resolvido por DNS
// e só um endereço IPv4 serve (a API de ICMP usada aceita apenas IPv4).
func resolveIPv4(address string) (net.IP, error) {
	if ip := net.ParseIP(address); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			return v4, nil
		}
		return nil, fmt.Errorf("%s is not an IPv4 address", address)
	}
	ips, err := net.LookupIP(address)
	if err != nil {
		return nil, fmt.Errorf("resolve %s: %w", address, err)
	}
	for _, ip := range ips {
		if v4 := ip.To4(); v4 != nil {
			return v4, nil
		}
	}
	return nil, fmt.Errorf("no IPv4 address for %s", address)
}

// roundMS converte a duração em milissegundos com uma casa decimal.
func roundMS(duration time.Duration) float64 {
	return math.Round(float64(duration)/float64(time.Millisecond)*10) / 10
}
