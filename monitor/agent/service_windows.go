//go:build windows

package main

import (
	"context"
	"fmt"
	"os/signal"
	"syscall"

	"golang.org/x/sys/windows/svc"
)

// serviceHandler responde aos comandos do gerenciador de serviços do Windows (iniciar,
// parar, consultar status) e guarda o grupo a repassar ao agente.
type serviceHandler struct {
	group string
}

// runAsServiceOrConsole detecta como o programa foi aberto: pelo gerenciador de serviços
// do Windows (roda como serviço) ou por uma pessoa no terminal (roda em modo console,
// parando com Ctrl+C). Isso permite testar o mesmo executável à mão.
func runAsServiceOrConsole(group string) error {
	isService, err := svc.IsWindowsService()
	if err != nil {
		return fmt.Errorf("detect Windows service: %w", err)
	}
	if isService {
		return svc.Run(windowsServiceName, &serviceHandler{group: group})
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return runAgent(ctx, group)
}

// Execute é chamado pelo Windows ao iniciar o serviço. Informa o estado (iniciando,
// rodando, parando), executa o agente em segundo plano e o encerra quando chega o
// comando de parar ou desligar. O retorno (true, 1) indica ao Windows que houve erro.
func (handler *serviceHandler) Execute(
	_ []string,
	requests <-chan svc.ChangeRequest,
	status chan<- svc.Status,
) (bool, uint32) {
	// Comandos que o serviço aceita do Windows: parar e desligar o computador.
	const accepted = svc.AcceptStop | svc.AcceptShutdown
	status <- svc.Status{State: svc.StartPending}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- runAgent(ctx, handler.group)
	}()

	status <- svc.Status{State: svc.Running, Accepts: accepted}
	for {
		select {
		case err := <-done:
			// O agente terminou sozinho (por exemplo, configuração inválida).
			if err != nil {
				return true, 1
			}
			return false, 0
		case request := <-requests:
			switch request.Cmd {
			case svc.Interrogate:
				// O Windows pergunta o estado atual; responde com o último informado.
				status <- request.CurrentStatus
			case svc.Stop, svc.Shutdown:
				// Parada pedida: cancela o contexto e espera o agente terminar de forma limpa.
				status <- svc.Status{State: svc.StopPending}
				cancel()
				if err := <-done; err != nil {
					return true, 1
				}
				return false, 0
			}
		}
	}
}
