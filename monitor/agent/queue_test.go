package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestHeartbeatQueueDropsOldestAtLimit(t *testing.T) {
	queue := newHeartbeatQueue(2)
	queue.Push(Heartbeat{AgentID: "first"})
	queue.Push(Heartbeat{AgentID: "second"})
	if !queue.Push(Heartbeat{AgentID: "third"}) {
		t.Fatal("expected the oldest item to be discarded")
	}

	first, _ := queue.Pop()
	second, _ := queue.Pop()
	if first.AgentID != "second" || second.AgentID != "third" {
		t.Fatalf("unexpected order: %q, %q", first.AgentID, second.AgentID)
	}
}

func TestHeartbeatQueueReturnsFailedItemToFront(t *testing.T) {
	queue := newHeartbeatQueue(2)
	queue.Push(Heartbeat{AgentID: "second"})
	queue.Push(Heartbeat{AgentID: "third"})
	if !queue.PushFront(Heartbeat{AgentID: "first"}) {
		t.Fatal("expected newest item to be discarded")
	}

	first, _ := queue.Pop()
	second, _ := queue.Pop()
	if first.AgentID != "first" || second.AgentID != "second" {
		t.Fatalf("unexpected order: %q, %q", first.AgentID, second.AgentID)
	}
}

func TestRunSenderRetriesFailedHeartbeat(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if requests.Add(1) == 1 {
			http.Error(response, "temporary failure", http.StatusServiceUnavailable)
			return
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	client := newAPIClient(server.URL, "test-token")
	queue := newHeartbeatQueue(10)
	queue.Push(Heartbeat{AgentID: "test-agent"})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		runSender(ctx, client, queue)
		close(done)
	}()

	deadline := time.After(3 * time.Second)
	for requests.Load() < 2 || queue.Len() != 0 {
		select {
		case <-deadline:
			cancel()
			t.Fatal("heartbeat was not retried successfully")
		case <-time.After(10 * time.Millisecond):
		}
	}
	cancel()
	<-done
}
