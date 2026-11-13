package main

import (
	"context"
	"errors"
	"log"
	"sync"
	"time"
)

// Espera entre tentativas após uma falha: começa em 1 s e dobra até no máximo 1 minuto,
// para não sobrecarregar a API enquanto ela se recupera.
const (
	initialBackoff = time.Second
	maximumBackoff = time.Minute
)

// HeartbeatQueue é a fila em memória dos heartbeats ainda não enviados, com tamanho máximo.
// É protegida por mutex (trava) porque a coleta e o envio rodam em goroutines diferentes.
// Os dados se perdem se o agente for reiniciado.
type HeartbeatQueue struct {
	mu    sync.Mutex
	items []Heartbeat
	limit int
	// ready é um aviso de "chegou item novo", com capacidade 1, para acordar o envio.
	ready chan struct{}
}

// newHeartbeatQueue cria uma fila vazia que guarda no máximo limit itens.
func newHeartbeatQueue(limit int) *HeartbeatQueue {
	return &HeartbeatQueue{
		items: make([]Heartbeat, 0, limit),
		limit: limit,
		ready: make(chan struct{}, 1),
	}
}

// Push coloca um item no fim da fila e informa se foi preciso descartar o mais antigo
// (fila cheia). Prefere-se perder o dado antigo a perder o mais recente.
func (queue *HeartbeatQueue) Push(heartbeat Heartbeat) bool {
	queue.mu.Lock()
	dropped := len(queue.items) == queue.limit
	if dropped {
		copy(queue.items, queue.items[1:])
		queue.items[len(queue.items)-1] = heartbeat
	} else {
		queue.items = append(queue.items, heartbeat)
	}
	queue.mu.Unlock()
	queue.notify()
	return dropped
}

// PushFront devolve ao início da fila um item cujo envio falhou, mantendo a ordem original.
// Se a fila encheu nesse meio tempo, descarta o item mais novo (o do fim) para preservar
// os dados mais antigos.
func (queue *HeartbeatQueue) PushFront(heartbeat Heartbeat) bool {
	queue.mu.Lock()
	dropped := len(queue.items) == queue.limit
	if dropped {
		queue.items = queue.items[:len(queue.items)-1]
	}
	queue.items = append(queue.items, Heartbeat{})
	copy(queue.items[1:], queue.items[:len(queue.items)-1])
	queue.items[0] = heartbeat
	queue.mu.Unlock()
	queue.notify()
	return dropped
}

// Pop retira o item mais antigo da fila; o segundo valor é false se a fila estiver vazia.
// O espaço liberado é zerado para a memória do item poder ser recuperada.
func (queue *HeartbeatQueue) Pop() (Heartbeat, bool) {
	queue.mu.Lock()
	defer queue.mu.Unlock()
	if len(queue.items) == 0 {
		return Heartbeat{}, false
	}
	heartbeat := queue.items[0]
	queue.items[0] = Heartbeat{}
	queue.items = queue.items[1:]
	return heartbeat, true
}

// Len devolve quantos itens estão aguardando envio.
func (queue *HeartbeatQueue) Len() int {
	queue.mu.Lock()
	defer queue.mu.Unlock()
	return len(queue.items)
}

// Wait bloqueia até haver item na fila (true) ou até ctx ser cancelado (false).
func (queue *HeartbeatQueue) Wait(ctx context.Context) bool {
	if queue.Len() > 0 {
		return true
	}
	select {
	case <-ctx.Done():
		return false
	case <-queue.ready:
		return true
	}
}

// notify acorda quem espera em Wait. O "default" evita travar se o aviso já está pendente.
func (queue *HeartbeatQueue) notify() {
	select {
	case queue.ready <- struct{}{}:
	default:
	}
}

// runSender envia a fila para a API até ctx ser cancelado. Em falha temporária, devolve o
// item ao início da fila e espera (com tempo crescente) antes de tentar de novo; em recusa
// definitiva, descarta o item. Sucesso zera a espera.
func runSender(ctx context.Context, client *APIClient, queue *HeartbeatQueue) {
	backoff := initialBackoff
	for queue.Wait(ctx) {
		heartbeat, ok := queue.Pop()
		if !ok {
			continue
		}

		if err := client.send(ctx, heartbeat); err != nil {
			if errors.Is(err, errRejected) {
				// Sem isto, um envio recusado ficaria na frente da fila e bloquearia os seguintes.
				log.Printf("heartbeat discarded: %v", err)
				continue
			}
			// Falha temporária (rede fora, 5xx, 408, 429): o item volta ao início da fila.
			queue.PushFront(heartbeat)
			log.Printf(
				"heartbeat failed: %v; retry in %s; queued=%d",
				err,
				backoff,
				queue.Len(),
			)
			// Espera o tempo de backoff, mas sai na hora se o serviço for parado.
			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			// Dobra a espera da próxima falha, sem passar do máximo.
			backoff = min(backoff*2, maximumBackoff)
			continue
		}

		// A API voltou a responder: a espera volta ao valor inicial.
		backoff = initialBackoff
		log.Printf(
			"heartbeat sent; cpu=%.1f%% memory=%.1f%% disk=%.1f%% queued=%d",
			heartbeat.CPUPercent,
			heartbeat.MemoryPercent,
			heartbeat.DiskPercent,
			queue.Len(),
		)
	}
}
