package main

import (
	"context"
	"flag"
	"log"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	group := flag.String("group", "", "grupo deste computador")
	flag.Parse()

	config, err := loadConfig(*group)
	if err != nil {
		log.Fatalf("invalid configuration: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client := newAPIClient(config.APIURL, config.Token)
	queue := newHeartbeatQueue(1000)
	senderDone := make(chan struct{})
	go func() {
		defer close(senderDone)
		runSender(ctx, client, queue)
	}()
	interval := time.Duration(config.IntervalSeconds) * time.Second
	log.Printf("agent started; group=%s id=%s interval=%s", config.Group, config.AgentID, interval)

	collectAndQueue(config, queue)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			<-senderDone
			log.Printf("agent %s stopped", config.AgentID)
			return
		case <-ticker.C:
			collectAndQueue(config, queue)
		}
	}
}

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
