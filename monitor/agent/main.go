package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"time"
)

// windowsServiceName é o nome do serviço como aparece no gerenciador de serviços do Windows.
const windowsServiceName = "CarmelMonitorAgent"

// main é o ponto de entrada. A opção --group define o grupo do computador e
// substitui o valor salvo no config.json.
func main() {
	group := flag.String("group", "", "grupo deste computador")
	flag.Parse()
	if err := runAsServiceOrConsole(*group); err != nil {
		log.Fatal(err)
	}
}

// runAgent é o laço principal: coleta métricas a cada intervalo e as coloca na fila,
// enquanto outra goroutine (thread leve do Go) envia a fila para a API.
// Retorna quando ctx é cancelado (parada do serviço ou Ctrl+C).
func runAgent(ctx context.Context, group string) error {
	config, err := loadConfig(group)
	if err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}

	client := newAPIClient(config.APIURL, config.Token)
	// A fila guarda até 1000 heartbeats em memória; se a API ficar fora por muito tempo,
	// os mais antigos são descartados para não consumir memória sem limite.
	queue := newHeartbeatQueue(1000)
	// senderDone sinaliza que o envio terminou, para encerrar o agente de forma ordenada.
	senderDone := make(chan struct{})
	// O envio roda em segundo plano para que uma API lenta não atrase a coleta.
	go func() {
		defer close(senderDone)
		runSender(ctx, client, queue)
	}()
	interval := time.Duration(config.IntervalSeconds) * time.Second
	// A sonda roda em goroutine própria, separada da coleta: uma impressora lenta ou um
	// erro na sonda nunca pode atrasar nem derrubar o monitoramento deste computador.
	probeDone := make(chan struct{})
	if config.Probe {
		log.Printf("probe mode enabled")
		go func() {
			defer close(probeDone)
			// O recover garante que um pânico na sonda seja apenas registrado no log.
			defer func() {
				if recovered := recover(); recovered != nil {
					log.Printf("probe stopped by panic: %v", recovered)
				}
			}()
			runProbe(ctx, client, config.AgentID, interval)
		}()
	} else {
		close(probeDone)
	}
	log.Printf("agent started; group=%s id=%s interval=%s", config.Group, config.AgentID, interval)

	// Primeira coleta imediata, sem esperar o primeiro intervalo.
	collectAndQueue(config, queue)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			// Aguarda o envio terminar antes de sair.
			<-senderDone
			<-probeDone
			log.Printf("agent %s stopped", config.AgentID)
			return nil
		case <-ticker.C:
			collectAndQueue(config, queue)
		}
	}
}

// collectAndQueue faz uma coleta e enfileira o resultado. Se a coleta falhar, apenas
// registra no log e tenta de novo no próximo ciclo.
func collectAndQueue(config Config, queue *HeartbeatQueue) {
	heartbeat, err := collectMetrics(config.AgentID, config.Group, config.IntervalSeconds)
	if err != nil {
		log.Printf("metric collection failed: %v", err)
		return
	}
	if queue.Push(heartbeat) {
		log.Printf("heartbeat queue full; oldest item discarded")
	}
}
