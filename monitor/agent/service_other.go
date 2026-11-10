//go:build !windows

package main

import (
	"context"
	"os/signal"
	"syscall"
)

func runAsServiceOrConsole(group string) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runAgent(ctx, group)
}
