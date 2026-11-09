package main

import (
	"context"
	"log"
	"sync"
	"time"
)

const (
	initialBackoff = time.Second
	maximumBackoff = time.Minute
)

type HeartbeatQueue struct {
	mu    sync.Mutex
	items []Heartbeat
	limit int
	ready chan struct{}
}

func newHeartbeatQueue(limit int) *HeartbeatQueue {
	return &HeartbeatQueue{
		items: make([]Heartbeat, 0, limit),
		limit: limit,
		ready: make(chan struct{}, 1),
	}
}

// Push appends an item and reports whether the oldest item had to be discarded.
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

// PushFront returns a failed item to the front and preserves older data.
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

func (queue *HeartbeatQueue) Len() int {
	queue.mu.Lock()
	defer queue.mu.Unlock()
	return len(queue.items)
}

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

func (queue *HeartbeatQueue) notify() {
	select {
	case queue.ready <- struct{}{}:
	default:
	}
}

func runSender(ctx context.Context, client *APIClient, queue *HeartbeatQueue) {
	backoff := initialBackoff
	for queue.Wait(ctx) {
		heartbeat, ok := queue.Pop()
		if !ok {
			continue
		}

		if err := client.send(ctx, heartbeat); err != nil {
			queue.PushFront(heartbeat)
			log.Printf(
				"heartbeat failed: %v; retry in %s; queued=%d",
				err,
				backoff,
				queue.Len(),
			)
			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			backoff = min(backoff*2, maximumBackoff)
			continue
		}

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
