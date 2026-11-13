//go:build !windows

package main

import (
	"context"
	"os/signal"
	"syscall"
)

// runAsServiceOrConsole (versão não Windows): não há serviço do Windows, então sempre roda
// no console, parando com Ctrl+C ou sinal de término.
func runAsServiceOrConsole(group string) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runAgent(ctx, group)
}
